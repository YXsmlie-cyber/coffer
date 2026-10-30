"""模特页面的只读投影；由调用方持有会话。"""
from sqlalchemy import select, case, and_, or_, func, exists
from .database import Task, Video


async def pipeline_by_model(session, model_ids: list[int] | None = None) -> dict[int, dict]:
    if model_ids is not None and not model_ids:
        return {}
    blocked_statuses = ("queued", "running", "paused", "completed")
    pending_statuses = ("queued", "running", "paused", "failed")
    relevant = select(
        Task.model_id.label("model_id"),
        Task.viewkey.label("viewkey"),
        func.max(case((and_(
            Task.task_type == "download",
            Task.status.in_(blocked_statuses),
        ), 1), else_=0)).label("blocked_download"),
        func.max(case((and_(
            Task.task_type == "upload",
            Task.status.in_(pending_statuses),
        ), 1), else_=0)).label("pending_upload"),
    ).where(
        Task.model_id.is_not(None),
        or_(
            and_(Task.task_type == "download", Task.status.in_(blocked_statuses)),
            and_(Task.task_type == "upload", Task.status.in_(pending_statuses)),
        ),
    )
    if model_ids is not None:
        relevant = relevant.where(Task.model_id.in_(model_ids))
    # SQLite may inline a plain CTE and reorder the join back to the much larger
    # videos table.  Explicit materialization keeps the selective task scan as the
    # driving side; supported by every SQLite version accepted by this project.
    relevant = (
        relevant.group_by(Task.model_id, Task.viewkey)
        .cte("relevant_tasks")
        .prefix_with("MATERIALIZED")
    )
    query = (
        select(
            relevant.c.model_id,
            func.sum(relevant.c.blocked_download).label("blocked_downloads"),
            func.sum(relevant.c.pending_upload).label("pending_uploads"),
        )
        .select_from(relevant)
        # EXISTS keeps relevant_tasks as the outer/driver relation.  A regular
        # JOIN is legally reordered by SQLite and was observed by EXPLAIN to scan
        # every missing video first even with a materialized CTE.
        .where(exists().where(
            Video.model_id == relevant.c.model_id,
            Video.viewkey == relevant.c.viewkey,
            Video.dedup_status == "missing",
        ))
        .group_by(relevant.c.model_id)
    )
    if model_ids is not None:
        query = query.where(relevant.c.model_id.in_(model_ids))
    rows = (await session.execute(query)).all()
    return {
        model_id: {
            "blocked_downloads": int(blocked or 0),
            "pending_uploads": int(pending or 0),
        }
        for model_id, blocked, pending in rows
    }
