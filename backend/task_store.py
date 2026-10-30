"""任务状态的持久化边界。

SQLite 同一时刻只能有一个写事务。任务调度器只描述状态变化，本模块负责把
这些变化串行写入，并对 SQLite 明确报告的瞬时锁做有限重试。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from .database import Task
from .diagnostics import record_timing


def is_sqlite_locked(error: BaseException) -> bool:
    """仅识别 SQLite 的写锁冲突，不吞掉其他数据库错误。"""
    detail = str(error).casefold()
    return "database is locked" in detail or "database table is locked" in detail


class TaskStore:
    """集中处理任务状态写入，避免调度器散落事务与锁重试代码。"""

    def __init__(
        self,
        session_factory: Callable,
        *,
        max_lock_retries: int = 2,
        retry_delay: float = 0.05,
    ) -> None:
        self._session_factory = session_factory
        self._max_lock_retries = max(0, int(max_lock_retries))
        self._retry_delay = max(0.0, float(retry_delay))
        self._write_lock = asyncio.Lock()

    async def update(
        self,
        task_id: int,
        fields: dict,
        *,
        return_task: bool = False,
    ) -> Task | None:
        """串行更新任务；锁冲突重试使用新的 Session/事务。"""
        waiting = time.monotonic()
        async with self._write_lock:
            record_timing("sqlite.task_write_wait", time.monotonic() - waiting)
            for retry in range(self._max_lock_retries + 1):
                started = time.monotonic()
                try:
                    return await self._update_once(
                        task_id,
                        fields,
                        return_task=return_task,
                    )
                except OperationalError as error:
                    if not is_sqlite_locked(error) or retry >= self._max_lock_retries:
                        raise
                    await asyncio.sleep(self._retry_delay * (2 ** retry))
                finally:
                    record_timing("sqlite.task_write_attempt", time.monotonic() - started)
        return None

    async def _update_once(
        self,
        task_id: int,
        fields: dict,
        *,
        return_task: bool,
    ) -> Task | None:
        async with self._session_factory() as session:
            await session.execute(
                update(Task).where(Task.id == task_id).values(**fields)
            )
            await session.commit()
            if not return_task:
                return None
            return (
                await session.execute(select(Task).where(Task.id == task_id))
            ).scalar_one_or_none()
