"""审美校准接口（仅本地记录，不触发同步/下载/上传）。"""
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .database import async_session, Model
from . import taste_service
from .library_index import library_index
from .taste_model import TasteModelUnavailable, taste_model_service
from .taste_visual import VisualModelUnavailable
from .preview_service import previews_by_model
from .api_schemas import (
    TasteComparisonRequest, TasteResultFeedbackBatchRequest, TasteResultFeedbackRequest,
)
from .api_wire import _model_wire, _pipeline_by_model


router = APIRouter()

_taste_lock = asyncio.Lock()

@router.get("/api/taste/pair")
async def get_taste_pair():
    return await taste_service.get_state(session_factory=async_session)


@router.post("/api/taste/comparisons")
async def record_taste_comparison(body: TasteComparisonRequest):
    if body.left_model_id == body.right_model_id:
        raise HTTPException(400, "左右候选不能是同一位模特")
    try:
        return await taste_service.record_comparison(
            body.left_model_id,
            body.right_model_id,
            body.choice,
            lock=_taste_lock,
            session_factory=async_session,
        )
    except taste_service.TasteUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except taste_service.TasteConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/api/taste/comparisons/latest")
async def undo_latest_taste_comparison():
    try:
        return await taste_service.undo_latest(
            lock=_taste_lock, session_factory=async_session)
    except taste_service.TasteUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/taste/model")
async def get_taste_model_state():
    return await taste_model_service.get_state(session_factory=async_session)


@router.post("/api/taste/model/run", status_code=202)
async def start_taste_model_run():
    """启动本地图像缓存、留组验证与统一排行；不进入媒体任务队列。"""
    try:
        return await taste_model_service.start(
            session_factory=async_session, mode="library")
    except (TasteModelUnavailable, VisualModelUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/taste/model/feedback")
async def record_taste_result_feedback(body: TasteResultFeedbackRequest):
    try:
        return await taste_model_service.record_feedback(
            body.model_id, body.action, session_factory=async_session)
    except TasteModelUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/taste/model/feedback/batch")
async def record_taste_result_feedback_batch(body: TasteResultFeedbackBatchRequest):
    """批量记录当前批次的评价；任一模特不在本批则整批拒绝，不创建任务。"""
    try:
        return await taste_model_service.record_feedback_many(
            body.model_ids, body.action, session_factory=async_session)
    except TasteModelUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/taste/liked")
async def get_taste_liked_models():
    """列出当前有效的“符合”模型及其媒体流水线状态。

    该接口只读本地偏好、视频快照、任务和115目录索引；不会刷新源站、创建
    下载任务或触发上传。真正同步与下载继续使用既有显式操作接口。
    """
    feedback = await taste_model_service.liked_feedback(
        session_factory=async_session)
    model_ids = [int(item["model_id"]) for item in feedback]
    if not model_ids:
        return {
            "models": [],
            "summary": {
                "liked_models": 0,
                "synced_models": 0,
                "unsynced_models": 0,
                "bound_models": 0,
                "total_videos": 0,
                "cloud_videos": 0,
                "maybe_videos": 0,
                "missing_videos": 0,
                "downloadable_videos": 0,
                "pending_upload_videos": 0,
            },
            "library_filter": "include_liked",
            "scan_generation": None,
            "last_success_at": None,
            "stale": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "safety": {
                "listing_creates_tasks": False,
                "sync_creates_tasks": False,
                "download_requires_confirmation": True,
                "upload_auto_after_download": True,
            },
        }

    async with async_session() as session:
        models = (await session.execute(select(Model).where(
            Model.id.in_(model_ids)
        ))).scalars().all()
        membership = await library_index.membership(session=session)
        pipeline = await _pipeline_by_model(session, model_ids)
        previews = await previews_by_model(session, model_ids)

    by_id = {int(model.id): model for model in models}
    items = []
    for record in feedback:
        model = by_id.get(int(record["model_id"]))
        if model is None:
            continue
        cloud_bound = membership.contains(model)
        items.append({
            **_model_wire(model, pipeline.get(model.id)),
            "preview_videos": previews.get(model.id, [])[:4],
            "feedback_id": int(record["feedback_id"]),
            "feedback_run_id": record["run_id"],
            "liked_at": record["liked_at"],
            "cloud_bound": cloud_bound,
            "cloud_folder": membership.folders_by_model.get(model.id),
        })

    summary = {
        "liked_models": len(items),
        "synced_models": sum(bool(item["synced_at"]) for item in items),
        "unsynced_models": sum(not item["synced_at"] for item in items),
        "bound_models": sum(bool(item["cloud_bound"]) for item in items),
        "total_videos": sum(int(item["total_videos"] or 0) for item in items),
        "cloud_videos": sum(int(item["cloud_videos"] or 0) for item in items),
        "maybe_videos": sum(int(item["maybe_videos"] or 0) for item in items),
        "missing_videos": sum(int(item["missing_videos"] or 0) for item in items),
        "downloadable_videos": sum(
            int(item["downloadable_videos"] or 0) for item in items),
        "pending_upload_videos": sum(
            int(item["pending_upload_videos"] or 0) for item in items),
    }
    return {
        "models": items,
        "summary": summary,
        "library_filter": "include_liked",
        "scan_generation": membership.scan_generation,
        "last_success_at": membership.last_success_at,
        "stale": membership.stale,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "listing_creates_tasks": False,
            "sync_creates_tasks": False,
            "download_requires_confirmation": True,
            "upload_auto_after_download": True,
        },
    }
