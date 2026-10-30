"""多个路由共用的模特序列化与流水线投影。"""
from fastapi import HTTPException

from . import avatar_store
from .database import Model
from .model_queries import pipeline_by_model


def _model_avatar_url(model: Model) -> str:
    digest = str(model.avatar_sha256 or "").strip().casefold()
    return f"/api/models/{model.id}/avatar?v={digest[:16]}" if digest else ""


def _model_avatar_ready(model: Model) -> bool:
    return avatar_store.avatar_path(model.avatar_sha256) is not None


def _model_identity_wire(model: Model) -> dict:
    """模特卡片与精确查找共用的身份字段；统计字段由 `_model_wire` 追加。"""
    public_slug = str(model.profile_slug or model.ph_id).split(":", 1)[-1]
    return {
        "id": model.id,
        "ph_id": public_slug,
        "profile_type": model.profile_type or "model",
        "profile_slug": public_slug,
        "name": model.name,
        "url": model.url,
        "thumbnail": model.thumbnail,
        "avatar_url": _model_avatar_url(model),
        "avatar_status": model.avatar_status,
        "rank": model.rank,
    }


def _synced_at_wire(model: Model) -> str | None:
    return model.synced_at.isoformat() if model.synced_at else None


def _lookup_model_payload(model: Model, created: bool) -> dict:
    return {
        "model": {**_model_identity_wire(model), "synced_at": _synced_at_wire(model)},
        "created": created,
    }


def _reject_model_in_library(model: Model, membership) -> None:
    """精确查找只返回普通列表模特；冲突响应在每次快照复核中保持一致。"""
    if not membership.contains(model):
        return
    raise HTTPException(409, detail={
        "code": "already_in_library",
        "message": "该模特已在115，请到“115模特”页面查看",
        "folder": membership.folders_by_model.get(model.id),
    })


def _model_wire(model: Model, pipeline: dict | None = None) -> dict:
    state = pipeline or {}
    blocked = int(state.get("blocked_downloads", 0) or 0)
    pending = int(state.get("pending_uploads", 0) or 0)
    return {
        **_model_identity_wire(model),
        "ph_video_count": model.ph_video_count,
        "views_text": model.views_text,
        "total_videos": model.total_videos,
        "cloud_videos": model.cloud_videos,
        "maybe_videos": model.maybe_videos,
        "missing_videos": model.missing_videos,
        "pending_upload_videos": pending,
        "downloadable_videos": max(0, int(model.missing_videos or 0) - blocked),
        "synced_at": _synced_at_wire(model),
    }


async def _pipeline_by_model(session, model_ids: list[int] | None = None) -> dict[int, dict]:
    """保留旧查询入口；实现只依赖调用方提供的会话。"""
    return await pipeline_by_model(session, model_ids)
