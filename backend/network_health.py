"""共享网络链路健康：区分“整条代理/网络断流”与单个任务自身的网络错误。

下载失败后先用同一出口探测一次目标主机：能拿到任何 HTTP 响应就说明链路正常，
按原策略计次重试；探测也失败则判定为共享链路断流，任务不消耗尝试次数，释放
下载槽等待恢复，调度器在断流期间暂缓派发新下载。断流期间只由一个后台探测循环
复查，避免每个任务各自轮询。
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 6.0
# 刚探测通过的结果短时复用：同一秒内多个任务一起失败时只探测一次。
PROBE_CACHE_SECONDS = 3.0
# 断流期间的复查间隔，逐步放宽到上限。
RECHECK_DELAYS = (3.0, 5.0, 8.0, 12.0, 15.0)
# 统计“近一小时断流次数/时长”的窗口。
HISTORY_WINDOW_SECONDS = 3600.0


@dataclass(frozen=True)
class ProbeTarget:
    """探测目标：页面或媒体主机的源站地址，以及该路径实际使用的代理。"""

    url: str
    proxy: str = ""

    @classmethod
    def for_url(cls, url: str, proxy: str = "") -> "ProbeTarget | None":
        parts = urlparse(str(url or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        port = f":{parts.port}" if parts.port else ""
        return cls(f"{parts.scheme}://{parts.hostname}{port}/", proxy or "")

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


Probe = Callable[[ProbeTarget], Awaitable[bool]]


async def probe_https(target: ProbeTarget) -> bool:
    """任何 HTTP 响应（含 403/404）都证明 TLS 与代理链路可用；传输错误才算不通。

    与页面/清单请求一样沿用环境代理变量（httpx 默认 trust_env），保证探测的是
    下载实际经过的同一出口。
    """
    try:
        async with httpx.AsyncClient(
            proxy=target.proxy or None, timeout=PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            await client.head(target.url)
        return True
    except (httpx.HTTPError, OSError):
        return False


class LinkHealth:
    def __init__(self, *, probe: Probe | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 recheck_delays: tuple[float, ...] = RECHECK_DELAYS):
        self._probe = probe or probe_https
        self._clock = clock
        self._sleep = sleep
        self._recheck_delays = recheck_delays
        self._outage_since: float | None = None
        self._outage_target: ProbeTarget | None = None
        self._restored: asyncio.Event | None = None
        self._monitor: asyncio.Task | None = None
        self._probe_lock = asyncio.Lock()
        self._last_ok: dict[str, float] = {}
        self._history: deque[tuple[float, float]] = deque(maxlen=128)
        self._listeners: list[Callable[[str, float], None]] = []
        # 进程启动以来的累计断流次数/秒数，供每日摘要按差值统计（不受一小时窗口限制）。
        self._total_outages = 0
        self._total_outage_seconds = 0.0

    @property
    def outage_active(self) -> bool:
        return self._outage_since is not None

    def outage_seconds(self) -> float:
        return 0.0 if self._outage_since is None else max(0.0, self._clock() - self._outage_since)

    def totals(self) -> tuple[int, float]:
        """累计断流次数与秒数，含正在进行的这一次。"""
        return (self._total_outages + int(self.outage_active),
                self._total_outage_seconds + self.outage_seconds())

    def add_listener(self, callback: Callable[[str, float], None]) -> None:
        """回调 ``(event, seconds)``：event 为 ``outage`` 或 ``restored``。"""
        self._listeners.append(callback)

    def public(self) -> dict:
        """有界、无主机名的状态摘要，供任务接口与诊断使用。"""
        now = self._clock()
        recent = [(start, end) for start, end in self._history
                  if end >= now - HISTORY_WINDOW_SECONDS]
        seconds = sum(end - max(start, now - HISTORY_WINDOW_SECONDS) for start, end in recent)
        return {
            "outage": self.outage_active,
            "outage_seconds": round(self.outage_seconds(), 1),
            "recent_outages": len(recent) + int(self.outage_active),
            "recent_outage_seconds": round(seconds + self.outage_seconds(), 1),
        }

    async def check(self, target: ProbeTarget | None) -> bool:
        """链路可用返回 True；不可用时进入断流状态并启动唯一的复查循环。"""
        if target is None:
            return True
        if self.outage_active:
            return False
        now = self._clock()
        if now - self._last_ok.get(target.url, float("-inf")) < PROBE_CACHE_SECONDS:
            return True
        async with self._probe_lock:
            if self.outage_active:
                return False
            if self._clock() - self._last_ok.get(target.url, float("-inf")) < PROBE_CACHE_SECONDS:
                return True
            if await self._probe(target):
                self._last_ok[target.url] = self._clock()
                return True
            self._begin_outage(target)
            return False

    async def wait_restored(self, timeout: float) -> bool:
        """等待断流结束；超时返回 False，由调用方回到原有计次策略。"""
        if not self.outage_active or self._restored is None:
            return True
        try:
            await asyncio.wait_for(self._restored.wait(), timeout=max(0.0, timeout))
            return True
        except TimeoutError:
            return False

    def _begin_outage(self, target: ProbeTarget) -> None:
        self._outage_since = self._clock()
        self._outage_target = target
        self._restored = asyncio.Event()
        logger.warning("网络链路探测失败（%s），进入断流等待", target.host)
        self._emit("outage", 0.0)
        self._monitor = asyncio.create_task(self._monitor_until_restored())

    async def _monitor_until_restored(self) -> None:
        index = 0
        try:
            while self.outage_active:
                await self._sleep(self._recheck_delays[min(index, len(self._recheck_delays) - 1)])
                index += 1
                target = self._outage_target
                if target is not None and await self._probe(target):
                    self._end_outage(target)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("网络链路复查异常，结束断流等待以免任务永久挂起")
            self._end_outage(self._outage_target)

    def _end_outage(self, target: ProbeTarget | None) -> None:
        if self._outage_since is None:
            return
        now = self._clock()
        duration = max(0.0, now - self._outage_since)
        self._history.append((self._outage_since, now))
        self._total_outages += 1
        self._total_outage_seconds += duration
        self._outage_since = None
        self._outage_target = None
        if target is not None:
            self._last_ok[target.url] = now
        if self._restored is not None:
            self._restored.set()
        logger.warning("网络链路已恢复，断流约 %.0f 秒", duration)
        self._emit("restored", duration)

    def _emit(self, event: str, seconds: float) -> None:
        for callback in list(self._listeners):
            try:
                callback(event, seconds)
            except Exception:
                logger.exception("网络链路状态回调失败")

    async def close(self) -> None:
        monitor = self._monitor
        self._monitor = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
