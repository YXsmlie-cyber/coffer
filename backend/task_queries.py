"""任务列表和统计的只读边界；分类器由应用提供，不反向依赖调度器。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func, or_, select

from .database import Task
from .task_contracts import wire_value


READ_BATCH_SIZE = 128
TASK_LIST_FIELDS = (
    "id", "model_id", "viewkey", "title", "model_name", "task_type", "status",
    "progress", "speed", "speed_bps", "downloaded_bytes", "total_bytes", "phase",
    "attempt", "last_progress_at", "auto_retry_count", "next_retry_at",
    "remote_missing_count", "patrol_recovery_count", "error", "local_path",
)
FAILURE_FIELDS = (
    "id", "task_type", "phase", "error", "local_path", "model_name", "title", "viewkey",
    "artifact_scope",
)


def _filtered(query, task_type, statuses, q):
    """类型、状态与关键字筛选；关键字按标题、模特名、viewkey 子串匹配。"""
    if task_type:
        query = query.where(Task.task_type == task_type)
    if statuses:
        query = query.where(Task.status.in_(tuple(statuses)))
    keyword = str(q or "").strip()
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.where(or_(
            Task.title.ilike(pattern, escape="\\"),
            Task.model_name.ilike(pattern, escape="\\"),
            Task.viewkey.ilike(pattern, escape="\\"),
        ))
    return query


class TaskQueries:
    def __init__(self, session_factory, classify_failure):
        self._session_factory = session_factory
        self._classify_failure = classify_failure

    async def list_tasks(self, task_type=None, limit=None, offset=0, cursor=None, *,
                         statuses=None, failure=None, q=None):
        """``failure`` 依赖逐行恢复分类，只能在读取失败行后过滤，分页随之在内存完成。"""
        fields = (*TASK_LIST_FIELDS, "artifact_scope")
        query = _filtered(
            select(*(getattr(Task, name) for name in fields)),
            task_type, ("failed",) if failure else statuses, q,
        ).order_by(Task.status_rank, Task.id.desc())
        if cursor is not None:
            rank, task_id = cursor
            query = query.where((Task.status_rank > rank) | (
                (Task.status_rank == rank) & (Task.id < task_id)))
        elif offset and not failure:
            query = query.offset(offset)
        if limit is not None and not failure:
            query = query.limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(query)).mappings().all()
        payload = await self._with_recovery(rows)
        if failure:
            wanted = failure == "recoverable"
            payload = [row for row in payload if bool(row["bulk_recoverable"]) == wanted]
            if offset and cursor is None:
                payload = payload[offset:]
            if limit is not None:
                payload = payload[:limit]
        return payload

    async def count_matching(self, task_type=None, *, statuses=None, failure=None, q=None):
        """筛选视图的命中总数；失败分类同样需要逐行判断。"""
        if failure:
            return len(await self.list_tasks(
                task_type, statuses=statuses, failure=failure, q=q))
        query = _filtered(select(func.count(Task.id)), task_type, statuses, q)
        async with self._session_factory() as session:
            return int((await session.execute(query)).scalar_one())

    async def today_completed(self, task_type, now=None):
        """本地自然日内完成的任务数、字节数与最后完成时刻；updated_at 按本地时间写入。"""
        start = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        query = select(
            func.count(Task.id), func.coalesce(func.sum(Task.total_bytes), 0),
            func.max(Task.updated_at),
        ).where(Task.task_type == task_type, Task.status == "completed",
                Task.updated_at >= start)
        async with self._session_factory() as session:
            count, size, last = (await session.execute(query)).one()
        return {"completed": int(count or 0), "bytes": int(size or 0),
                "last_completed_at": wire_value(last)}

    async def _with_recovery(self, rows):
        payload = []
        for start in range(0, len(rows), READ_BATCH_SIZE):
            batch = [dict(row) for row in rows[start:start + READ_BATCH_SIZE]]
            failed = [row for row in batch if row["status"] == "failed"]
            recovery = await asyncio.to_thread(self._classify_rows, failed) if failed else {}
            for row in batch:
                payload.append({
                    **{name: wire_value(row[name]) for name in TASK_LIST_FIELDS},
                    **recovery.get(row["id"], {
                        "failure_kind": "", "recovery_action": "", "bulk_recoverable": False,
                    }),
                })
        return payload

    def _classify_rows(self, rows):
        return {row["id"]: self._classify_failure(SimpleNamespace(**row)) for row in rows}

    def _count_failures(self, rows):
        recoverable = reconcile = 0
        for row in rows:
            disposition = self._classify_failure(SimpleNamespace(**row))
            recoverable += int(disposition["bulk_recoverable"])
            reconcile += int(disposition["recovery_action"] == "reconcile_final")
        return len(rows), recoverable, reconcile

    async def task_counts(self, task_type=None):
        query = select(Task.status, func.count(Task.id)).group_by(Task.status)
        failures = select(*(getattr(Task, name) for name in FAILURE_FIELDS)).where(
            Task.status == "failed")
        if task_type:
            query = query.where(Task.task_type == task_type)
            failures = failures.where(Task.task_type == task_type)
        total_failed = recoverable = reconcile = 0
        async with self._session_factory() as session:
            counts = dict((await session.execute(query)).all())
            stream = await session.stream(failures.execution_options(yield_per=READ_BATCH_SIZE))
            try:
                # 保留同一次读取的游标；线程只拿标量字典。取消后最多完成当前
                # 128 项文件检查，不把所有失败行/分类结果同时堆在内存中。
                async for batch in stream.mappings().partitions(READ_BATCH_SIZE):
                    size, recovered, reconciled = await asyncio.to_thread(
                        self._count_failures, [dict(row) for row in batch])
                    total_failed += size
                    recoverable += recovered
                    reconcile += reconciled
            finally:
                await stream.close()
        counts.update(failed_recoverable=recoverable,
                      failed_manual=total_failed - recoverable,
                      failed_reconcile=reconcile)
        return counts
