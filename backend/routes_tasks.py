"""下载/上传任务队列接口。"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .task_contracts import TaskType, TASK_STATUS_RANK as _TASK_STATUS_RANK
from .task_manager import manager
from .api_schemas import BatchDeleteRequest, DownloadSettingsRequest
from .download_settings import DownloadSettings


router = APIRouter()


def _parse_task_cursor(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        rank_text, id_text = value.split(":", 1)
        rank, task_id = int(rank_text), int(id_text)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "无效的任务分页游标") from exc
    if not 0 <= rank <= 6 or task_id < 1:
        raise HTTPException(400, "无效的任务分页游标")
    return rank, task_id



def _parse_task_statuses(value: str | None) -> tuple[str, ...] | None:
    if value is None or not value.strip():
        return None
    statuses = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not statuses or any(status not in _TASK_STATUS_RANK for status in statuses):
        raise HTTPException(400, "无效的任务状态筛选")
    return statuses


@router.get("/api/tasks")
async def get_tasks(
    type: TaskType | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, max_length=64),
    include_counts: bool = False,
    status: str | None = Query(default=None, max_length=80,
                               description="逗号分隔的状态筛选，如 running,queued"),
    failure: Literal["recoverable", "manual"] | None = Query(
        default=None, description="只看可批量重试或需逐项处理的失败任务"),
    q: str | None = Query(default=None, max_length=100,
                          description="按标题、模特名或 viewkey 子串搜索"),
):
    """任务列表；不传分页与筛选参数时保持旧接口行为。"""
    parsed_cursor = _parse_task_cursor(cursor)
    if parsed_cursor is not None and offset:
        raise HTTPException(400, "cursor 与 offset 不能同时使用")
    filters = {
        "statuses": _parse_task_statuses(status),
        "failure": failure,
        "q": (q or "").strip() or None,
    }
    filtered = any(filters.values())
    tasks = await manager.list_tasks(
        type, limit=limit, offset=offset, cursor=parsed_cursor,
        **(filters if filtered else {}))
    response = {"tasks": tasks}
    if limit is not None:
        if len(tasks) == limit:
            last = tasks[-1]
            rank = _TASK_STATUS_RANK.get(last.get("status"), 6)
            response["next_cursor"] = f"{rank}:{last['id']}"
        else:
            response["next_cursor"] = None
    if include_counts:
        counts = await manager.task_counts(type)
        response["counts"] = counts
        if type in (None, "download"):
            response["download_blocked"] = manager.download_blocked_reason()
            response["network"] = manager.link_health()
        if type in (None, "upload"):
            response["upload_blocked"] = manager.upload_blocked_reason()
        response["total"] = sum(
            int(counts.get(name, 0))
            for name in ("running", "queued", "paused", "completed", "failed", "skipped")
        )
        if filtered:
            response["matched"] = await manager.count_matching_tasks(type, **filters)
        if type:
            # 队列清空时的“今天完成”小结；全局概况（不带 type）不需要。
            response["today"] = await manager.task_today(type)
    return response


@router.post("/api/tasks/start-all")
async def tasks_start_all(type: TaskType | None = None):
    await manager.start_all(type)
    return {"ok": True}


@router.post("/api/tasks/pause-all")
async def tasks_pause_all(type: TaskType | None = None):
    await manager.pause_all(type)
    return {"ok": True}


@router.post("/api/tasks/resume-all")
async def tasks_resume_all(type: TaskType | None = None):
    await manager.resume_all(type)
    return {"ok": True}


@router.post("/api/tasks/reconcile-downloads")
async def tasks_reconcile_downloads(apply: bool = False):
    """离线复核已有最终 MP4；默认 dry-run，显式 apply 才修改任务状态。"""
    result = await manager.reconcile_failed_downloads(apply=apply)
    return {"ok": True, "applied": apply, **result}


@router.post("/api/tasks/scrub-errors")
async def tasks_scrub_errors(apply: bool = False):
    """默认只报告需脱敏数量；显式 apply 才覆盖历史错误文本。"""
    result = await manager.scrub_stored_task_errors(apply=apply)
    return {"ok": True, "applied": apply, **result}


@router.post("/api/tasks/{task_id}/retry")
async def task_retry(task_id: int):
    ok = await manager.retry_task(task_id)
    if not ok:
        raise HTTPException(400, "该任务无法重试（仅失败/跳过的任务可重试）")
    return {"ok": True}


@router.post("/api/tasks/retry-failed")
async def tasks_retry_failed(type: TaskType | None = None):
    result = await manager.retry_failed(type)
    return {"ok": True, **result}


@router.delete("/api/tasks/{task_id}")
async def task_delete(task_id: int):
    if not await manager.delete_task(task_id):
        raise HTTPException(409, "运行中的上传无法安全删除，请等待上传结束")
    return {"ok": True}


@router.post("/api/tasks/delete-batch")
async def tasks_delete_batch(body: BatchDeleteRequest):
    result = await manager.delete_tasks(body.task_ids)
    return {"ok": True, **result}


@router.post("/api/tasks/delete-all")
async def tasks_delete_all(type: TaskType | None = None):
    result = await manager.delete_all_tasks(type)
    return {"ok": True, **result}


@router.get("/api/settings/download")
async def get_download_settings():
    """下载运行参数（页面可调）与当前是否处于下载时段。"""
    return {**manager.download_settings(), "blocked": manager.download_blocked_reason()}


@router.post("/api/settings/download")
async def update_download_settings(body: DownloadSettingsRequest):
    try:
        values = await manager.update_download_settings(DownloadSettings(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**values, "blocked": manager.download_blocked_reason()}
