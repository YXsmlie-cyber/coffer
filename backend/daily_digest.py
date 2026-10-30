"""每日 Bark 摘要：每天固定时刻汇总过去 24 小时的下载、上传、失败与网络断流。

只读任务表；到点前只睡一次，不做周期轮询。全天都没有动静时不发送，避免噪音。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Awaitable, Callable

from sqlalchemy import func, select

from .database import Task


logger = logging.getLogger(__name__)

DIGEST_HOUR = 9
DIGEST_WINDOW = timedelta(hours=24)


def seconds_until(now: datetime, hour: int = DIGEST_HOUR) -> float:
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def format_bytes(size: int) -> str:
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DailyDigest:
    def __init__(self, *, session_factory, notify: Callable[[str, str], Awaitable[None]],
                 outage_totals: Callable[[], tuple[int, float]],
                 clock: Callable[[], datetime] = datetime.now, hour: int = DIGEST_HOUR):
        self._session_factory = session_factory
        self._notify = notify
        self._outage_totals = outage_totals
        self._clock = clock
        self._hour = hour
        self._task: asyncio.Task | None = None
        # 断流按“距上次摘要”的累计差值统计；首次摘要从进程启动算起。
        self._outage_baseline = outage_totals()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="daily-digest")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(self._clock(), self._hour))
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("每日摘要生成失败，明天同一时刻再试")
            # 防止系统时钟回拨导致同一时刻重复发送。
            await asyncio.sleep(60)

    async def send_once(self) -> bool:
        content = await self.build(self._clock())
        if content is None:
            return False
        await self._notify(*content)
        return True

    async def build(self, now: datetime) -> tuple[str, str] | None:
        since = now - DIGEST_WINDOW
        async with self._session_factory() as session:
            done = (await session.execute(
                select(Task.task_type, Task.status, func.count(Task.id),
                       func.coalesce(func.sum(Task.total_bytes), 0))
                .where(Task.updated_at >= since,
                       Task.status.in_(("completed", "skipped")))
                .group_by(Task.task_type, Task.status))).all()
            current = dict((await session.execute(
                select(Task.status, func.count(Task.id))
                .where(Task.status.in_(("queued", "failed")))
                .group_by(Task.status))).all())
        finished = {(kind, status): (int(count), int(size)) for kind, status, count, size in done}
        downloads, download_bytes = finished.get(("download", "completed"), (0, 0))
        uploads, _ = finished.get(("upload", "completed"), (0, 0))
        skipped = finished.get(("upload", "skipped"), (0, 0))[0] \
            + finished.get(("download", "skipped"), (0, 0))[0]
        queued, failed = int(current.get("queued", 0)), int(current.get("failed", 0))

        total_count, total_seconds = self._outage_totals()
        base_count, base_seconds = self._outage_baseline
        self._outage_baseline = (total_count, total_seconds)
        outages = max(0, total_count - base_count)
        outage_minutes = max(0.0, total_seconds - base_seconds) / 60

        if not any((downloads, uploads, skipped, failed, outages)):
            return None
        lines = [f"过去 24 小时：下载完成 {downloads} 个"
                 + (f"（{format_bytes(download_bytes)}）" if download_bytes else "")
                 + f"，上传完成 {uploads} 个"
                 + (f"，跳过 {skipped} 个" if skipped else "")]
        lines.append(f"当前：排队 {queued} 个，失败 {failed} 个"
                     + ("，请到下载/上传页查看“需处理”" if failed else ""))
        if outages:
            lines.append(f"网络断流 {outages} 次，共约 {max(1, round(outage_minutes))} 分钟"
                         "（断流期间不计重试次数）")
        title = f"PH-115 每日摘要 · 下载 {downloads} / 上传 {uploads}"
        return title, "\n".join(lines)
