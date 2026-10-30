"""榜单抓取、模特查找/同步/列表、头像与封面、下载入队接口。"""
import asyncio
import base64
import hashlib
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, delete, update, case, and_, or_, func, cast, exists, Integer
from sqlalchemy.exc import IntegrityError

from .config import settings
from .database import async_session, CloudModelFolder, DiscoveryItem, Model, Video, Task
from . import avatar_service, avatar_store, preview_image_store, scraper
from .task_manager import manager
from .model_sync_coordinator import model_sync_coordinator
from .library_index import library_index, normalize_library_name
from .preview_service import (
    previews_by_model, schedule_preview_backfill, schedule_preview_warmup,
)
from .api_schemas import (
    RankingRequest, ModelLookupRequest, ModelSyncJobRequest, DownloadRequest,
)
from .api_wire import (
    _lookup_model_payload, _model_avatar_ready, _model_avatar_url, _model_wire,
    _pipeline_by_model, _reject_model_in_library,
)


router = APIRouter()

# 查找是低频交互；串行化“外部确认 + 插入”即可消除并发双击造成的重复行。
# models.ph_id 的唯一约束仍是跨进程并发的最后一道保护。
_model_lookup_lock = asyncio.Lock()


_MODEL_CURSOR_VERSION = 1


def _model_query_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _encode_model_cursor(sort: str, query: str, key: list) -> str:
    raw = json.dumps({
        "v": _MODEL_CURSOR_VERSION,
        "s": sort,
        "q": _model_query_digest(query),
        "k": key,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_model_cursor(value: str, *, sort: str, query: str) -> list:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (not isinstance(payload, dict)
                or payload.get("v") != _MODEL_CURSOR_VERSION
                or payload.get("s") != sort
                or payload.get("q") != _model_query_digest(query)
                or not isinstance(payload.get("k"), list)):
            raise ValueError
        return payload["k"]
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "models cursor 无效或已不适用于当前查询") from exc


def _model_progress_key(model: Model) -> int:
    total = max(0, int(model.total_videos or 0))
    return int((max(0, int(model.cloud_videos or 0)) * 100.0 / total) + 0.5) \
        if total else 0


async def _find_exact_model(session, query: str) -> Model | None:
    name_query = " ".join(query.strip().split()).casefold()
    ph_query = query.strip().removeprefix("@").strip().casefold()
    if not name_query:
        return None
    return (await session.execute(select(Model).where(
        Model.profile_type.in_(("model", "pornstar")),
        or_(
            func.lower(Model.name) == name_query,
            func.lower(Model.profile_slug) == ph_query,
            func.lower(Model.ph_id) == ph_query,
        ),
    ).limit(1))).scalar_one_or_none()



@router.post("/api/scrape/ranking")
async def scrape_ranking(body: RankingRequest):
    """爬榜单 -> 验证个人主页 -> 本地化头像 -> 入库模特。"""
    try:
        models = await scraper.scrape_ranking(body.url, body.max_models)
    except scraper.InvalidScrapeURL as e:
        raise HTTPException(400, str(e)) from e
    ranking_ids = {
        str(item.get("ph_id") or "") for item in models
        if str(item.get("ph_id") or "").strip()
    }
    async with async_session() as prefetch_session:
        existing_rows = (await prefetch_session.execute(select(Model).where(
            Model.ph_id.in_(ranking_ids)
        ))).scalars().all() if ranking_ids else []
    existing_assets = {
        str(model.ph_id or "").casefold(): {
            "avatar_source_url": model.avatar_source_url,
            "avatar_sha256": model.avatar_sha256,
            "avatar_updated_at": model.avatar_updated_at,
            "avatar_status": model.avatar_status,
            "avatar_checked_at": model.avatar_checked_at,
        } for model in existing_rows
    }
    prepared, avatar_failures = await avatar_service.prepare_models(
        models, existing_assets=existing_assets)
    if models and not prepared:
        raise HTTPException(502, detail={
            "message": "榜单模特的个人主页或头像均未通过验证，本次未写入数据库",
            "failures": avatar_failures,
        })
    saved = []
    async with async_session() as s:
        membership = await library_index.membership(session=s)
        prepared_ids = {str(item["ph_id"]) for item in prepared}
        existing_by_ph_id = {
            row.ph_id: row for row in (await s.execute(select(Model).where(
                Model.ph_id.in_(prepared_ids)
            ))).scalars().all()
        } if prepared_ids else {}
        objects: list[Model] = []
        for m in prepared:
            identity = scraper.creator_identity_from_url(m["url"])
            profile_type = identity["profile_type"] if identity else "model"
            profile_slug = identity["profile_slug"] if identity else m["ph_id"]
            existing = existing_by_ph_id.get(m["ph_id"])
            if existing:
                existing.name = m["name"]
                existing.url = m["url"]
                existing.profile_type = profile_type
                existing.profile_slug = profile_slug
                existing.rank = m.get("rank", 0)
                existing.scrape_order = m.get("scrape_order", 0)
                existing.ph_video_count = m.get("ph_video_count", 0)
                existing.views_text = m.get("views_text", "")
                existing.avatar_source_url = m["avatar_source_url"]
                existing.avatar_sha256 = m["avatar_sha256"]
                existing.avatar_updated_at = m["avatar_updated_at"]
                existing.avatar_status = "ready"
                existing.avatar_error = ""
                existing.avatar_checked_at = m["avatar_checked_at"]
                obj = existing
            else:
                obj = Model(ph_id=m["ph_id"], profile_type=profile_type,
                            profile_slug=profile_slug, name=m["name"], url=m["url"],
                            thumbnail=m["thumbnail"],
                            avatar_source_url=m["avatar_source_url"],
                            avatar_sha256=m["avatar_sha256"],
                            avatar_updated_at=m["avatar_updated_at"],
                            avatar_status="ready", avatar_error="",
                            avatar_checked_at=m["avatar_checked_at"],
                            rank=m.get("rank", 0), scrape_order=m.get("scrape_order", 0),
                            ph_video_count=m.get("ph_video_count", 0),
                            views_text=m.get("views_text", ""))
                s.add(obj)
                existing_by_ph_id[m["ph_id"]] = obj
            objects.append(obj)
        await s.flush()
        for obj in objects:
            if not membership.contains(obj):
                saved.append({"id": obj.id, "ph_id": obj.ph_id, "name": obj.name,
                              "url": obj.url, "thumbnail": obj.thumbnail,
                              "avatar_url": _model_avatar_url(obj)})
        await s.commit()
    return {
        "models": saved,
        "avatar_failures": avatar_failures,
        "library_filter": "exclude",
        "library_scan_generation": membership.scan_generation,
    }


@router.post("/api/models/lookup")
async def lookup_model(body: ModelLookupRequest):
    """精确查找一个本地或 PH 模特主页，并幂等保存为未入榜模特。"""
    query = " ".join(body.query.strip().split())
    if not query:
        raise HTTPException(422, "请输入模特名字或账号")

    # 已有且本地头像文件完整时不访问外部站点；旧记录缺头像时在原地修复。
    async with async_session() as session:
        existing = await _find_exact_model(session, query)
        membership = await library_index.membership(session=session)
    if existing:
        _reject_model_in_library(existing, membership)
    if existing and _model_avatar_ready(existing):
        return _lookup_model_payload(existing, False)

    try:
        slug = scraper.normalize_model_query(query)
    except scraper.InvalidModelQuery as exc:
        raise HTTPException(422, str(exc)) from exc

    async with _model_lookup_lock:
        # 等待并发的同一查找完成后再次检查，确保只创建一行。
        async with async_session() as session:
            existing = await _find_exact_model(session, query)
            if not existing:
                existing = (await session.execute(select(Model).where(
                    func.lower(Model.ph_id) == slug,
                ).limit(1))).scalar_one_or_none()
            locked_membership = await library_index.membership(session=session)
        if existing:
            # 等锁期间另一请求可能刚创建模型，片库刷新也可能刚把它纳入115；
            # 必须在头像就绪的快速返回之前使用锁内新快照复核。
            _reject_model_in_library(existing, locked_membership)
        if existing and _model_avatar_ready(existing):
            return _lookup_model_payload(existing, False)

        try:
            profile = await scraper.lookup_model_profile(existing.ph_id if existing else slug)
        except scraper.InvalidModelQuery as exc:
            raise HTTPException(422, str(exc)) from exc
        except scraper.ModelLookupNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except scraper.ModelLookupUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc

        try:
            asset = await avatar_store.cache_avatar(
                profile["thumbnail"], referer=profile["url"])
        except (avatar_store.AvatarStoreError, scraper.InvalidScrapeURL) as exc:
            raise HTTPException(502, f"头像下载或校验失败：{exc}") from exc

        existing_id = existing.id if existing else None
        created = existing_id is None
        identity = scraper.creator_identity_from_url(profile["url"])
        profile_type = identity["profile_type"] if identity else "model"
        profile_slug = identity["profile_slug"] if identity else profile["ph_id"]
        async with async_session() as session:
            # 外部资料确认后再按 canonical ph_id 复核一次，兼容别名和并发写入。
            if existing_id is not None:
                model = (await session.execute(select(Model).where(
                    Model.id == existing_id,
                ).limit(1))).scalar_one_or_none()
            else:
                model = (await session.execute(select(Model).where(
                    func.lower(Model.ph_id) == profile["ph_id"].casefold(),
                ).limit(1))).scalar_one_or_none()
            if model:
                created = False
                model.name = profile["name"]
                model.url = profile["url"]
                model.profile_type = profile_type
                model.profile_slug = profile_slug
                model.avatar_source_url = asset.source_url
                model.avatar_sha256 = asset.sha256
                model.avatar_updated_at = datetime.now()
                model.avatar_status = "ready"
                model.avatar_error = ""
                model.avatar_checked_at = datetime.now()
                await session.commit()
                await session.refresh(model)
            else:
                last_order = (await session.execute(
                    select(func.max(Model.scrape_order))
                )).scalar_one_or_none() or 0
                model = Model(
                    ph_id=profile["ph_id"],
                    profile_type=profile_type,
                    profile_slug=profile_slug,
                    name=profile["name"],
                    url=profile["url"],
                    thumbnail=profile["thumbnail"],
                    avatar_source_url=asset.source_url,
                    avatar_sha256=asset.sha256,
                    avatar_updated_at=datetime.now(),
                    avatar_status="ready",
                    avatar_error="",
                    avatar_checked_at=datetime.now(),
                    rank=0,
                    scrape_order=int(last_order) + 1,
                    ph_video_count=profile.get("ph_video_count", 0),
                    views_text=profile.get("views_text", ""),
                )
                session.add(model)
                try:
                    await session.commit()
                except IntegrityError:
                    # 唯一索引处理极少见的多进程竞态；返回先写入的同一模特。
                    await session.rollback()
                    model = (await session.execute(select(Model).where(
                        func.lower(Model.ph_id) == profile["ph_id"].casefold(),
                    ).limit(1))).scalar_one()
                    model.name = profile["name"]
                    model.url = profile["url"]
                    model.profile_type = profile_type
                    model.profile_slug = profile_slug
                    model.avatar_source_url = asset.source_url
                    model.avatar_sha256 = asset.sha256
                    model.avatar_updated_at = datetime.now()
                    model.avatar_status = "ready"
                    model.avatar_error = ""
                    model.avatar_checked_at = datetime.now()
                    await session.commit()
                    created = False
                else:
                    await session.refresh(model)
            current_membership = await library_index.membership(session=session)
        _reject_model_in_library(model, current_membership)
        return _lookup_model_payload(model, created)


@router.post("/api/scrape/model/{model_id}/sync")
async def sync_model(model_id: int):
    """爬该模特视频列表 + 同步115 + 去重，更新缺失数。"""
    try:
        result = await _sync_model_core(model_id)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            504,
            f"单模特同步超过 {int(settings.model_sync_timeout_seconds)} 秒时间预算",
        ) from exc
    except scraper.ScrapeError as exc:
        # PH 限流/挑战页/暂时性结构异常属于上游不可用，不应刷成应用内部 500。
        raise HTTPException(502, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "模特不存在")
    if result.get("status") == "source_unavailable":
        raise HTTPException(410, result.get("error") or "源站主页不可用或已删除")
    return result


async def _sync_model_core(model_id: int) -> dict | None:
    """兼容旧内部调用；业务实现位于独立同步服务。"""
    return await model_sync_coordinator.sync_model(model_id)


@router.post("/api/model-sync/jobs", status_code=202)
async def create_model_sync_job(body: ModelSyncJobRequest):
    """创建后台批量同步；进度通过 WebSocket 和状态查询返回。"""
    job = await model_sync_coordinator.create_job(body.model_ids)
    return job.wire()


@router.get("/api/model-sync/jobs/{job_id}")
async def get_model_sync_job(job_id: str):
    job = model_sync_coordinator.get_job(job_id)
    if job is None:
        raise HTTPException(404, "同步任务不存在或后端已重启")
    return job.wire()


@router.get("/api/models")
async def list_models(
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    query: str = Query(default="", max_length=200),
    sort: Literal["rank", "missing", "progress", "name"] = "rank",
):
    """模特列表；不传 limit 保留旧全量响应，传 limit 时使用稳定 keyset。"""
    # 旧内部调用和少量单元测试会直接调用路由函数，此时 FastAPI 的 Query
    # 描述对象不会经过依赖注入；按 HTTP 默认值解释，保持向后兼容。
    limit = limit if isinstance(limit, int) else None
    cursor = cursor if isinstance(cursor, str) else None
    query = query if isinstance(query, str) else ""
    if cursor is not None and limit is None:
        raise HTTPException(400, "cursor 必须与 limit 一起使用")
    search = normalize_library_name(query)
    # 迁移会把这些历史 NULL 统一回填为 0；保持裸列表达式，使分页排序
    # 能命中 models 的复合索引，而不是被 coalesce 包装后退化为临时排序。
    rank_key = Model.rank
    scrape_order_key = Model.scrape_order
    missing_key = Model.missing_videos
    total_key = func.coalesce(Model.total_videos, 0)
    cloud_key = func.coalesce(Model.cloud_videos, 0)
    unknown_rank = case((rank_key == 0, 1), else_=0)
    progress_key = case(
        (total_key <= 0, 0),
        else_=cast(
            (cloud_key * 100.0 / total_key) + 0.5,
            Integer,
        ),
    )
    sort_orders = {
        "rank": (unknown_rank.asc(), rank_key.asc(), scrape_order_key.asc(), Model.id.asc()),
        "missing": (missing_key.desc(), Model.id.asc()),
        "progress": (progress_key.desc(), Model.id.asc()),
        # SQLite lower() only folds ASCII.  The persisted key is produced by the
        # same Python whitespace-collapse + casefold rule as LibraryMembership,
        # so Unicode ordering and the cursor comparison cannot disagree.
        "name": (Model.library_name_key.asc(), Model.id.asc()),
    }
    async with async_session() as s:
        membership = await library_index.membership(session=s)
        filters = [
            Model.profile_type.in_(("model", "pornstar")),
            func.coalesce(func.trim(Model.cloud_cid), "") == "",
        ]
        # 不把数千个成员展开成两组 NOT IN 参数。相关 NOT EXISTS 既避免
        # SQLite 变量上限，也与 LibraryMembership.contains 的 id/name OR
        # 语义一致；cloud_cid 仍由上面的 coalesce 兼容旧库 NULL。
        active_library_member = exists().where(
            CloudModelFolder.active.is_(True),
            or_(
                CloudModelFolder.model_id == Model.id,
                and_(
                    CloudModelFolder.normalized_name != "",
                    CloudModelFolder.normalized_name == Model.library_name_key,
                ),
            ),
        )
        filters.append(~active_library_member)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            filters.append(or_(
                Model.library_name_key.like(pattern, escape="\\"),
                func.lower(Model.ph_id).like(pattern, escape="\\"),
                func.lower(Model.profile_slug).like(pattern, escape="\\"),
            ))

        total = int((await s.execute(
            select(func.count(Model.id)).where(*filters)
        )).scalar_one())
        page_filters = list(filters)
        if cursor is not None:
            key = _decode_model_cursor(cursor, sort=sort, query=search)
            try:
                if sort == "rank" and len(key) == 4:
                    rank_unknown, rank_value, scrape_order, model_id = map(int, key)
                    page_filters.append(or_(
                        unknown_rank > rank_unknown,
                        and_(unknown_rank == rank_unknown, rank_key > rank_value),
                        and_(unknown_rank == rank_unknown, rank_key == rank_value,
                             scrape_order_key > scrape_order),
                        and_(unknown_rank == rank_unknown, rank_key == rank_value,
                             scrape_order_key == scrape_order, Model.id > model_id),
                    ))
                elif sort == "missing" and len(key) == 2:
                    missing, model_id = map(int, key)
                    page_filters.append(or_(
                        missing_key < missing,
                        and_(missing_key == missing, Model.id > model_id),
                    ))
                elif sort == "progress" and len(key) == 2:
                    progress, model_id = map(int, key)
                    page_filters.append(or_(
                        progress_key < progress,
                        and_(progress_key == progress, Model.id > model_id),
                    ))
                elif sort == "name" and len(key) == 2:
                    name, model_id = str(key[0]), int(key[1])
                    page_filters.append(or_(
                        Model.library_name_key > name,
                        and_(Model.library_name_key == name, Model.id > model_id),
                    ))
                else:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "models cursor 排序键无效") from exc

        statement = select(Model).where(*page_filters).order_by(*sort_orders[sort])
        if limit is not None:
            statement = statement.limit(limit + 1)
        rows = (await s.execute(statement)).scalars().all()
        has_more = limit is not None and len(rows) > limit
        models = rows[:limit] if limit is not None else rows
        # “缺失”只代表 115 中尚不存在，其中一部分视频其实已经下载完成、正在等待上传。
        # 从任务表反查仍为 missing 的视频，给卡片提供互不混淆的流水线状态：
        #   pending_upload_videos：本地已下载，但上传尚未完成；
        #   downloadable_videos：尚无有效下载任务，可以真正加入下载队列。
        # 查询从较小的 tasks 表出发，避免每次刷新模特页都全扫二十多万条 videos。
        pipeline = await _pipeline_by_model(s, [model.id for model in models])
        page_ids = [model.id for model in models]
        previews = await previews_by_model(s, page_ids)
    schedule_preview_warmup(page_ids[:72], session_factory=async_session)
    schedule_preview_backfill([
        model_id for model_id in page_ids if not previews.get(model_id)
    ], session_factory=async_session)
    next_cursor = None
    if has_more and models:
        last = models[-1]
        if sort == "rank":
            cursor_key = [
                1 if int(last.rank or 0) == 0 else 0,
                int(last.rank or 0), int(last.scrape_order or 0), int(last.id),
            ]
        elif sort == "missing":
            cursor_key = [int(last.missing_videos or 0), int(last.id)]
        elif sort == "progress":
            cursor_key = [_model_progress_key(last), int(last.id)]
        else:
            cursor_key = [str(last.library_name_key or ""), int(last.id)]
        next_cursor = _encode_model_cursor(sort, search, cursor_key)
    return {
        "models": [{
            **_model_wire(model, pipeline.get(model.id)),
            "preview_videos": previews.get(model.id, [])[:4],
        } for model in models],
        "library_filter": "exclude",
        "total": total,
        "next_cursor": next_cursor,
        "has_more": bool(has_more),
        "scan_generation": membership.scan_generation,
        "last_success_at": membership.last_success_at,
        "stale": membership.stale,
    }


@router.get("/api/models/{model_id}/avatar")
async def model_avatar(model_id: int):
    async with async_session() as s:
        model = (await s.execute(select(Model).where(Model.id == model_id))).scalar_one_or_none()
    if not model or not model.avatar_sha256:
        raise HTTPException(404, "该模特尚无已验证的本地头像")
    path = avatar_store.avatar_path(model.avatar_sha256)
    if path is None:
        raise HTTPException(404, "头像数据库记录存在，但本地文件缺失")
    return FileResponse(
        path=str(path),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{model.avatar_sha256}"',
        },
    )


@router.get("/api/previews/{viewkey}/image")
async def preview_image(viewkey: str):
    """按需取得清晰本地WebP；失败时由前端回退历史缩略图。"""
    try:
        path = await preview_image_store.ensure_preview_image(viewkey)
    except preview_image_store.InvalidPreviewViewkey as exc:
        raise HTTPException(400, str(exc)) from exc
    except preview_image_store.PreviewImageUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc
    except preview_image_store.PreviewImageError as exc:
        raise HTTPException(502, str(exc)) from exc
    return FileResponse(
        path=str(path),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/models/{model_id}/videos")
async def model_videos(model_id: int):
    async with async_session() as s:
        videos = (await s.execute(select(Video).where(Video.model_id == model_id))).scalars().all()
    return {"videos": [{
        "viewkey": v.viewkey, "title": v.title, "url": v.url, "thumbnail": v.thumbnail,
        "duration": v.duration, "dedup_status": v.dedup_status,
        "matched_cloud_name": v.matched_cloud_name,
    } for v in videos]}


@router.delete("/api/models/{model_id}")
async def delete_model(model_id: int):
    """删除模特及其视频记录（bulk delete 绕过 ORM cascade，需显式先删 videos）。"""
    async with async_session() as s:
        m = (await s.execute(select(Model).where(Model.id == model_id))).scalar_one_or_none()
        if not m:
            raise HTTPException(404, "模特不存在")
        # 任务历史允许保留；解除可空关联，避免新库的 ON DELETE SET NULL 与旧库行为不一致。
        await s.execute(update(Task).where(Task.model_id == model_id).values(model_id=None))
        await s.execute(delete(DiscoveryItem).where(DiscoveryItem.model_id == model_id))
        await s.execute(delete(Video).where(Video.model_id == model_id))
        await s.execute(delete(Model).where(Model.id == model_id))
        await s.commit()
    return {"ok": True}


# ---------- 下载 ----------
def _select_download_videos(videos: list[Video], body: DownloadRequest) -> list[Video]:
    """按请求筛选可下载视频；任何模式都不能绕过明确 exists 去重结果。"""
    selected: list[Video] = []
    requested_viewkeys = set(body.viewkeys)
    for video in videos:
        if body.mode == "all" and video.dedup_status in ("missing", "maybe"):
            selected.append(video)
        elif body.mode == "missing" and video.dedup_status == "missing":
            selected.append(video)
        elif (
            body.mode == "selected"
            and video.viewkey in requested_viewkeys
            and video.dedup_status in ("missing", "maybe")
        ):
            # 手选也不能绕过去重状态下载明确已存在的视频；前端会禁用这类
            # 复选框，这里仍做服务端兜底，避免旧页面或直接请求浪费带宽。
            selected.append(video)
    return selected


@router.post("/api/download")
async def add_download(body: DownloadRequest):
    """把选定视频加入下载队列。"""
    async with async_session() as s:
        model = (await s.execute(select(Model).where(Model.id == body.model_id))).scalar_one_or_none()
        if not model:
            raise HTTPException(404, "模特不存在")
        q = select(Video).where(Video.model_id == body.model_id)
        videos = (await s.execute(q)).scalars().all()

    selected = _select_download_videos(videos, body)

    payload = [{
        "viewkey": v.viewkey, "title": v.title, "url": v.url, "duration": v.duration,
        "model_id": model.id, "model_name": model.name,
        "model_cloud_cid": model.cloud_cid,
    } for v in selected]
    ids = await manager.add_download_tasks(payload)
    return {"added": len(ids), "task_ids": ids}
