"""PH 视频 CDN 限流（HTTP 429）退避：整体暂停新下载，恢复后降并发逐步爬升。

429 是 CDN 按本机出口统计的请求过多，不是某个视频坏了，也不是链路断了（链路探测
能拿到 HTTP 响应，会判为正常）。如果仍按普通网络错误逐个任务重试，每个任务 3 次
尝试很快耗光，整条队列会被批量刷成失败，而持续的分片请求又会让限流迟迟不解除。

任一下载因 429 失败就进入冷却：调度器暂停派发新下载，被限流的任务退回队列且不消耗
重试次数。冷却期间再收到的 429 属于同一轮，不叠加；冷却结束后第一次又收到 429 才
升一档，冷却时间 5 → 10 → 20 → 30 分钟。档位越高，恢复后的并发上限越低（每档减半，
至少 1 个任务）；每成功下载一个视频降一档，并发逐步回到设置值。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable


logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = (300.0, 600.0, 1200.0, 1800.0)
WAITING_TEXT = "PH 视频 CDN 限流（HTTP 429），已暂停新下载，冷却后自动继续（不计次）"


class DownloadThrottle:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 cooldowns: tuple[float, ...] = COOLDOWN_SECONDS):
        self._clock = clock
        self._cooldowns = cooldowns
        self._level = 0
        self._until: float | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[str, float], None]] = []

    @property
    def level(self) -> int:
        return self._level

    @property
    def blocked(self) -> bool:
        return self._until is not None and self._clock() < self._until

    def remaining_seconds(self) -> float:
        if self._until is None:
            return 0.0
        return max(0.0, self._until - self._clock())

    def add_listener(self, callback: Callable[[str, float], None]) -> None:
        """回调 ``(event, seconds)``：event 为 ``throttled``（seconds=冷却时长）或 ``resumed``。"""
        self._listeners.append(callback)

    def public(self) -> dict:
        return {
            "throttled": self.blocked,
            "seconds_left": round(self.remaining_seconds(), 1),
            "level": self._level,
        }

    def concurrency_cap(self, limit: int) -> int:
        """当前允许同时下载的任务数：每档减半，至少 1。"""
        limit = max(1, int(limit))
        if self._level <= 0:
            return limit
        return max(1, limit >> self._level)

    def report(self) -> float:
        """一次下载因 429 失败；返回本轮冷却还剩多少秒。"""
        if self.blocked:
            return self.remaining_seconds()
        self._level = min(self._level + 1, len(self._cooldowns))
        seconds = self._cooldowns[self._level - 1]
        self._until = self._clock() + seconds
        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._timer = loop.call_later(seconds, self._resume)
        logger.warning(
            "PH 视频 CDN 限流（HTTP 429），暂停新下载 %.0f 分钟（第 %d 档）",
            seconds / 60, self._level)
        self._emit("throttled", seconds)
        return seconds

    def record_success(self) -> None:
        """一个视频完整下载成功，说明限流已放松，降一档。"""
        if self._level > 0 and not self.blocked:
            self._level -= 1

    def _resume(self) -> None:
        self._timer = None
        if self._until is None:
            return
        self._until = None
        logger.warning("CDN 限流冷却结束，恢复下载（同时下载上限 %d 档减半）", self._level)
        self._emit("resumed", 0.0)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _emit(self, event: str, seconds: float) -> None:
        for callback in list(self._listeners):
            try:
                callback(event, seconds)
            except Exception:
                logger.exception("下载限流状态回调失败")

    def close(self) -> None:
        self._cancel_timer()
