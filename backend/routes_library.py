"""115 模特目录索引与绑定接口。"""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func

from .database import async_session, CloudModelFolder, Model
from .cloud115 import Cloud115Error
from .library_index import library_index, normalize_library_name
from .library_suggestions import build_binding_suggestions, suggestion_model_names
from .preview_service import ensure_previews, previews_by_model, schedule_preview_warmup
from .api_schemas import LibraryBindingRequest
from .api_wire import _model_wire, _pipeline_by_model


router = APIRouter()


@router.get("/api/library/models")
async def list_library_models(
    binding: Literal["bound", "unbound", "error"] = "bound",
    query: str = Query(default="", max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=36, ge=1, le=72),
):
    normalized_query = normalize_library_name(query)
    async with async_session() as session:
        folder_filters = [CloudModelFolder.active.is_(True)]
        if binding == "bound":
            folder_filters.append(CloudModelFolder.model_id.is_not(None))
        elif binding == "error":
            folder_filters.append(func.coalesce(CloudModelFolder.error, "") != "")
        else:
            folder_filters.extend((
                CloudModelFolder.model_id.is_(None),
                func.coalesce(CloudModelFolder.error, "") == "",
            ))
        if normalized_query:
            escaped_query = (normalized_query.replace("\\", "\\\\")
                             .replace("%", "\\%").replace("_", "\\_"))
            folder_filters.append(CloudModelFolder.normalized_name.like(
                f"%{escaped_query}%", escape="\\"))
        total = int((await session.execute(
            select(func.count()).select_from(CloudModelFolder).where(*folder_filters)
        )).scalar_one())
        page_folders = (await session.execute(select(CloudModelFolder).where(
            *folder_filters
        ).order_by(CloudModelFolder.name, CloudModelFolder.cid).offset(
            (page - 1) * page_size
        ).limit(page_size))).scalars().all()
        membership = await library_index.membership(session=session)
        model_ids = [int(folder.model_id) for folder in page_folders
                     if folder.model_id is not None]

    await ensure_previews(model_ids)
    async with async_session() as session:
        models = {
            model.id: model for model in (await session.execute(select(Model).where(
                Model.id.in_(model_ids)
            ))).scalars().all()
        } if model_ids else {}
        pipeline = await _pipeline_by_model(session, model_ids)
        previews = await previews_by_model(session, model_ids)
        suggestion_models = []
        if binding != "bound" and page_folders:
            suggestion_models = await suggestion_model_names(session)

    unresolved = [(folder.cid, folder.normalized_name) for folder in page_folders
                  if not models.get(folder.model_id)]
    suggestions_by_folder = await asyncio.to_thread(
        build_binding_suggestions, unresolved, suggestion_models,
    ) if unresolved and suggestion_models else {}
    items = []
    for folder in page_folders:
        model = models.get(folder.model_id) if folder.model_id else None
        items.append({
            "folder": library_index.folder_wire(folder),
            "model": _model_wire(model, pipeline.get(model.id)) if model else None,
            "preview_videos": previews.get(model.id, [])[:4] if model else [],
            "binding_suggestions": suggestions_by_folder.get(folder.cid, []),
        })
    schedule_preview_warmup(model_ids, session_factory=async_session)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "scan_generation": membership.scan_generation,
        "last_success_at": membership.last_success_at,
        "stale": membership.stale,
    }


@router.post("/api/library/refresh")
async def refresh_library():
    try:
        return await library_index.refresh()
    except Cloud115Error as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"115目录刷新失败：{exc}") from exc


@router.put("/api/library/folders/{cid}/binding")
async def bind_library_folder(cid: str, body: LibraryBindingRequest):
    try:
        return await library_index.bind(cid, body.model_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/api/library/folders/{cid}/binding")
async def unbind_library_folder(cid: str):
    try:
        return await library_index.unbind(cid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
