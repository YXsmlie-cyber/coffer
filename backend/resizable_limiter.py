"""运行中可调整上限的并发许可（替代固定的 asyncio.Semaphore）。

已持有的许可不受影响；上限调低后，新的获取要等占用降到新上限以下，调高则立即
唤醒等待者。release 与 asyncio.Semaphore 一样是同步的，取消路径不会丢许可。
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress


class ResizableLimiter:
    def __init__(self, limit: int):
        self._limit = max(1, int(limit))
        self._in_use = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def _value(self) -> int:
        """与 asyncio.Semaphore._value 同义：当前可立即获取的许可数。"""
        return max(0, self._limit - self._in_use)

    def _wake(self) -> None:
        while self._waiters and self._in_use < self._limit:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            # 直接把许可交给等待者，避免被后来者插队。
            self._in_use += 1
            waiter.set_result(None)

    async def acquire(self) -> None:
        if self._in_use < self._limit and not self._waiters:
            self._in_use += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                # 许可已经交到手上却被取消：归还，交给下一个等待者。
                self._in_use -= 1
                self._wake()
            else:
                with suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        self._in_use = max(0, self._in_use - 1)
        self._wake()

    def resize(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._wake()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_exc):
        self.release()
        return False
