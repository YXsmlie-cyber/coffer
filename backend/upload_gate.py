"""115 上传闸门：区分“115 拒绝本机的所有上传”与单个文件自己的上传失败。

115 的 OSS 存储桶策略可以整体拒绝某个出口发来的上传（AccessDenied: Access denied by
bucket policy）。这时换临时凭证、换网页表单上传都会被拒，大文件甚至只表现为连接被重置；
逐个任务重试只会把自动重试次数耗光，恢复后还得手动重试。

上传在传输阶段失败后，用一次无副作用的探测确认（申请临时凭证、对随机对象发起分块上传并
立即取消，不在网盘留下任何文件）。确认被拒就关闭闸门：任务退回队列且不计次，调度器暂停
派发上传，只由一个后台循环按间隔复查；复查放行或任一上传成功即重新开闸并唤醒调度。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Literal


logger = logging.getLogger(__name__)

ProbeVerdict = Literal["allowed", "denied", "unknown"]
Probe = Callable[[], Awaitable[ProbeVerdict]]

# 被拒期间的复查间隔：先 5 分钟，之后每 10 分钟一次。
RECHECK_DELAYS = (300.0, 600.0)
WAITING_TEXT = "115 暂时拒绝本机上传，已暂停上传，恢复后自动继续"


class UploadGate:
    def __init__(self, *, probe: Probe,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 recheck_delays: tuple[float, ...] = RECHECK_DELAYS):
        self._probe = probe
        self._clock = clock
        self._sleep = sleep
        self._recheck_delays = recheck_delays
        self._blocked_since: float | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None
        self._listeners: list[Callable[[str, float], None]] = []

    @property
    def blocked(self) -> bool:
        return self._blocked_since is not None

    def blocked_seconds(self) -> float:
        if self._blocked_since is None:
            return 0.0
        return max(0.0, self._clock() - self._blocked_since)

    def add_listener(self, callback: Callable[[str, float], None]) -> None:
        """回调 ``(event, seconds)``：event 为 ``blocked`` 或 ``restored``。"""
        self._listeners.append(callback)

    def public(self) -> dict:
        return {"blocked": self.blocked, "blocked_seconds": round(self.blocked_seconds(), 1)}

    async def confirm_denied(self) -> bool:
        """上传传输失败后调用；返回 True 表示 115 正在拒绝本机上传（闸门已关闭）。"""
        if self.blocked:
            return True
        async with self._lock:
            if self.blocked:
                return True
            if await self._safe_probe() != "denied":
                return False
            self._close()
            return True

    def record_success(self) -> None:
        """任一上传经文件 ID 确认成功，说明 115 已放行，立即开闸。"""
        if self.blocked:
            self._open()

    async def _safe_probe(self) -> ProbeVerdict:
        try:
            return await self._probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("115 上传探测异常")
            return "unknown"

    def _close(self) -> None:
        self._blocked_since = self._clock()
        logger.warning("115 拒绝本机上传（存储桶策略），暂停上传并等待恢复")
        self._emit("blocked", 0.0)
        self._monitor = asyncio.create_task(self._monitor_until_allowed())

    async def _monitor_until_allowed(self) -> None:
        index = 0
        try:
            while self.blocked:
                await self._sleep(self._recheck_delays[min(index, len(self._recheck_delays) - 1)])
                index += 1
                if not self.blocked:
                    return
                # 网络不通（unknown）不能证明已放行，继续等下一轮。
                if await self._safe_probe() == "allowed":
                    self._open()
                    return
        except asyncio.CancelledError:
            raise

    def _open(self) -> None:
        if self._blocked_since is None:
            return
        duration = self.blocked_seconds()
        self._blocked_since = None
        monitor = self._monitor
        self._monitor = None
        if monitor is not None and not monitor.done() and monitor is not asyncio.current_task():
            monitor.cancel()
        logger.warning("115 已恢复接受上传，暂停约 %.0f 秒", duration)
        self._emit("restored", duration)

    def _emit(self, event: str, seconds: float) -> None:
        for callback in list(self._listeners):
            try:
                callback(event, seconds)
            except Exception:
                logger.exception("上传闸门状态回调失败")

    async def close(self) -> None:
        monitor = self._monitor
        self._monitor = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
