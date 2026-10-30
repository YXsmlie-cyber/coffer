"""每模特四封面物化服务。"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import case, delete, func, select

from . import preview_image_store, scraper
from .config import settings
from .database import (
    DiscoveryItem,
    Model,
    ModelPreviewVideo,
    Video,
    async_session,
)


_warmup_tasks: set[asyncio.Task] = set()
_warming_viewkeys: set[str] = set()
_preview_warmup_task: asyncio.Task | None = None
_preview_warmup_pending: dict[int, tuple[int, object]] = {}
_preview_warmup_active: dict[int, int] = {}
_preview_backfill_task: asyncio.Task | None = None
_preview_backfill_pending: set[int] = set()
_preview_backfill_active: set[int] = set()
_preview_backfill_attempted_at: dict[int, float] = {}
_PROFILE_PREVIEW_TIMEOUT_SECONDS = 75.0
_PROFILE_BACKFILL_BATCH_SIZE = 12
_PROFILE_BACKFILL_RETRY_COOLDOWN_SECONDS = 6 * 60 * 60


def preview_wire(item: ModelPreviewVideo) -> dict:
    return {
        "viewkey": item.viewkey,
        "title": item.title,
        "url": item.url,
        "thumbnail": item.thumbnail,
        # 浏览器优先加载本机清晰封面；接口不可用时前端自动回退 thumbnail。
        "preview_image_url": f"/api/previews/{item.viewkey}/image",
        "duration": item.duration,
        "source": item.source,
    }


async def previews_by_model(session, model_ids: list[int]) -> dict[int, list[dict]]:
    if not model_ids:
        return {}
    rows = (await session.execute(select(ModelPreviewVideo).where(
        ModelPreviewVideo.model_id.in_(model_ids)
    ).order_by(
        ModelPreviewVideo.model_id.asc(), ModelPreviewVideo.ordinal.asc()
    ))).scalars().all()
    result: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        result[row.model_id].append(preview_wire(row))
    return dict(result)


async def refresh_for_models(
    model_ids: list[int], *, session_factory=async_session, session=None,
    commit: bool = True,
) -> int:
    """从完整Video优先、DiscoveryItem补足；内容不变时不产生预览 DML。"""
    ids = sorted({int(value) for value in model_ids if int(value) > 0})
    if not ids:
        return 0

    # 每个模特预取至多12条，以便跳过重复封面后仍能得到4条。
    video_order = (
        case((Video.source_order <= 0, 1), else_=0),
        Video.source_order.asc(),
        Video.id.asc(),
    )
    ranked_videos = select(
        Video.model_id.label("model_id"),
        Video.viewkey.label("viewkey"),
        Video.title.label("title"),
        Video.url.label("url"),
        Video.thumbnail.label("thumbnail"),
        Video.duration.label("duration"),
        func.row_number().over(
            partition_by=Video.model_id, order_by=video_order
        ).label("rn"),
    ).where(
        Video.model_id.in_(ids),
        Video.thumbnail != "",
    ).subquery()
    ranked_discovery = select(
        DiscoveryItem.model_id.label("model_id"),
        DiscoveryItem.viewkey.label("viewkey"),
        DiscoveryItem.title.label("title"),
        DiscoveryItem.url.label("url"),
        DiscoveryItem.thumbnail.label("thumbnail"),
        DiscoveryItem.duration.label("duration"),
        DiscoveryItem.source.label("source"),
        func.row_number().over(
            partition_by=DiscoveryItem.model_id,
            order_by=(DiscoveryItem.fetched_at.desc(), DiscoveryItem.source_order.asc()),
        ).label("rn"),
    ).where(
        DiscoveryItem.model_id.in_(ids),
        DiscoveryItem.thumbnail != "",
    ).subquery()

    async def refresh(active_session) -> int:
        video_rows = (await active_session.execute(select(ranked_videos).where(
            ranked_videos.c.rn <= 12
        ))).mappings().all()
        discovery_rows = (await active_session.execute(select(ranked_discovery).where(
            ranked_discovery.c.rn <= 12
        ))).mappings().all()

        selected: dict[int, list[dict]] = {model_id: [] for model_id in ids}
        seen_keys: dict[int, set[str]] = defaultdict(set)
        seen_thumbnails: dict[int, set[str]] = defaultdict(set)

        def add(row, source: str):
            model_id = int(row["model_id"])
            if len(selected[model_id]) >= 4:
                return
            viewkey = str(row.get("viewkey") or "")
            thumbnail = str(row.get("thumbnail") or "")
            identity = viewkey or str(row.get("url") or "")
            if not thumbnail or (identity and identity in seen_keys[model_id]):
                return
            if thumbnail in seen_thumbnails[model_id]:
                return
            if identity:
                seen_keys[model_id].add(identity)
            seen_thumbnails[model_id].add(thumbnail)
            selected[model_id].append({
                "viewkey": viewkey,
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "thumbnail": thumbnail,
                "duration": max(0, int(row.get("duration") or 0)),
                "source": source,
            })

        for row in video_rows:
            add(row, "video")
        for row in discovery_rows:
            add(row, str(row.get("source") or "discovery"))

        existing_rows = (await active_session.execute(select(ModelPreviewVideo).where(
            ModelPreviewVideo.model_id.in_(ids)
        ).order_by(
            ModelPreviewVideo.model_id.asc(), ModelPreviewVideo.ordinal.asc()
        ))).scalars().all()
        existing: dict[int, list[tuple]] = defaultdict(list)
        for row in existing_rows:
            existing[int(row.model_id)].append((
                int(row.ordinal), str(row.viewkey or ""), str(row.title or ""),
                str(row.url or ""), str(row.thumbnail or ""), int(row.duration or 0),
                str(row.source or ""),
            ))
        changed_ids: list[int] = []
        for model_id in ids:
            desired = [(
                ordinal, item["viewkey"], item["title"], item["url"],
                item["thumbnail"], item["duration"], item["source"],
            ) for ordinal, item in enumerate(selected[model_id])]
            if existing.get(model_id, []) != desired:
                changed_ids.append(model_id)
        if not changed_ids:
            return sum(len(items) for items in selected.values())

        await active_session.execute(delete(ModelPreviewVideo).where(
            ModelPreviewVideo.model_id.in_(changed_ids)
        ))
        now = datetime.now()
        for model_id in changed_ids:
            items = selected[model_id]
            for ordinal, item in enumerate(items):
                active_session.add(ModelPreviewVideo(
                    model_id=model_id,
                    ordinal=ordinal,
                    updated_at=now,
                    **item,
                ))
        return sum(len(items) for items in selected.values())

    if session is not None:
        result = await refresh(session)
        if commit:
            await session.commit()
        return result
    async with session_factory() as owned_session:
        result = await refresh(owned_session)
        if commit:
            await owned_session.commit()
        return result


async def ensure_previews(model_ids: list[int], *, session_factory=async_session):
    """仅为尚无任何物化预览的模特补数据。"""
    ids = sorted({int(value) for value in model_ids if int(value) > 0})
    if not ids:
        return 0
    async with session_factory() as session:
        existing = set((await session.execute(select(
            ModelPreviewVideo.model_id
        ).where(ModelPreviewVideo.model_id.in_(ids)).distinct())).scalars().all())
    missing = [model_id for model_id in ids if model_id not in existing]
    total = 0
    for start in range(0, len(missing), 200):
        total += await refresh_for_models(
            missing[start:start + 200], session_factory=session_factory)
    return total


async def backfill_profile_previews(
    model_ids: list[int], *, session_factory=async_session,
) -> dict:
    """仅为完全没有预览来源的模特轻量读取本人作品首页，最多保存4条。

    这里不写 Video、不运行完整同步，也不创建下载任务。已有任意物化预览的模特
    会直接跳过，避免每次推荐刷新都重复访问主页。
    """
    ids = list(dict.fromkeys(
        int(value) for value in model_ids if int(value) > 0))
    if not ids:
        return {"attempted": 0, "filled_models": 0, "added": 0, "failed": 0}
    async with session_factory() as session:
        existing = set((await session.execute(select(
            ModelPreviewVideo.model_id
        ).where(ModelPreviewVideo.model_id.in_(ids)).distinct())).scalars().all())
        missing_ids = [model_id for model_id in ids if model_id not in existing]
        models = {
            model.id: model for model in (await session.execute(select(Model).where(
                Model.id.in_(missing_ids),
                Model.profile_type.in_(("model", "pornstar")),
            ))).scalars().all()
        } if missing_ids else {}

    limit = max(1, min(3, int(settings.preview_image_fetch_concurrency)))
    semaphore = asyncio.Semaphore(limit)

    async def fetch(model: Model):
        async with semaphore:
            try:
                videos = await asyncio.wait_for(
                    scraper.scrape_model_videos(
                        model.url, max_videos=4, include_playlist=False),
                    timeout=_PROFILE_PREVIEW_TIMEOUT_SECONDS,
                )
                return model.id, videos[:4], ""
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return model.id, [], str(error).strip() or error.__class__.__name__

    fetched = await asyncio.gather(*(fetch(model) for model in models.values()))
    now = datetime.now()
    filled_models = 0
    added = 0
    failed = 0
    async with session_factory() as session:
        # 抓取期间其他请求可能已经写入预览；提交前重新检查，绝不覆盖新数据。
        now_existing = set((await session.execute(select(
            ModelPreviewVideo.model_id
        ).where(ModelPreviewVideo.model_id.in_(list(models))).distinct())).scalars().all()) \
            if models else set()
        for model_id, videos, error in fetched:
            if error:
                failed += 1
                continue
            if model_id in now_existing or not videos:
                continue
            seen_keys: set[str] = set()
            seen_thumbnails: set[str] = set()
            ordinal = 0
            for video in videos:
                viewkey = str(video.get("viewkey") or "")
                thumbnail = str(video.get("thumbnail") or "")
                if (not viewkey or not thumbnail or viewkey in seen_keys
                        or thumbnail in seen_thumbnails):
                    continue
                session.add(ModelPreviewVideo(
                    model_id=model_id,
                    ordinal=ordinal,
                    viewkey=viewkey,
                    title=str(video.get("title") or ""),
                    url=str(video.get("url") or ""),
                    thumbnail=thumbnail,
                    duration=max(0, int(video.get("duration") or 0)),
                    source="profile_preview",
                    updated_at=now,
                ))
                ordinal += 1
                added += 1
                seen_keys.add(viewkey)
                seen_thumbnails.add(thumbnail)
                if ordinal >= 4:
                    break
            if ordinal:
                filled_models += 1
        await session.commit()
    return {
        "attempted": len(models),
        "filled_models": filled_models,
        "added": added,
        "failed": failed,
    }


async def warm_preview_images(
    model_ids: list[int], *, max_per_model: int = 1,
    session_factory=async_session,
) -> dict:
    """后台预生成指定模特的本地640x360 WebP；单图失败不影响其他封面。"""
    ids = list(dict.fromkeys(
        int(value) for value in model_ids if int(value) > 0))
    if not ids:
        return {"requested": 0, "completed": 0, "cached": 0, "failed": 0}
    async with session_factory() as session:
        rows = (await session.execute(select(ModelPreviewVideo).where(
            ModelPreviewVideo.model_id.in_(ids)
        ).order_by(
            ModelPreviewVideo.model_id.asc(), ModelPreviewVideo.ordinal.asc()
        ))).scalars().all()
    by_model: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        if row.viewkey:
            by_model[row.model_id].append(row.viewkey)
    # 先让每位模特拥有第一张可见封面，再补第二至第四张；避免前几位的四张图
    # 把后续几十张卡片的首图长期堵在队尾。
    cover_limit = max(1, min(4, int(max_per_model)))
    viewkeys = list(dict.fromkeys(
        by_model[model_id][ordinal]
        for ordinal in range(cover_limit)
        for model_id in ids
        if len(by_model.get(model_id, ())) > ordinal
    ))

    completed = 0
    cached = 0
    failed = 0

    async def ensure(viewkey: str):
        nonlocal completed, cached, failed
        try:
            if preview_image_store.preview_path(viewkey) is not None:
                cached += 1
                return
            await preview_image_store.ensure_preview_image(viewkey)
            completed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            failed += 1
        finally:
            _warming_viewkeys.discard(viewkey)

    # 每批只创建少量协程；底层仍受全局3路（可配置）网络信号量限制。
    for start in range(0, len(viewkeys), 16):
        batch = []
        for viewkey in viewkeys[start:start + 16]:
            if viewkey in _warming_viewkeys:
                continue
            _warming_viewkeys.add(viewkey)
            batch.append(viewkey)
        if batch:
            await asyncio.gather(*(ensure(viewkey) for viewkey in batch))
    return {
        "requested": len(viewkeys),
        "completed": completed,
        "cached": cached,
        "failed": failed,
    }


def schedule_preview_warmup(
    model_ids: list[int], *, max_per_model: int = 1,
    session_factory=async_session,
) -> bool:
    """把页面封面并入全局单例加热队列；频繁 GET 不重复建任务/查同一页。"""
    global _preview_warmup_task
    ids = list(dict.fromkeys(
        int(value) for value in model_ids if int(value) > 0))
    if not ids:
        return False
    requested_limit = max(1, min(4, int(max_per_model)))
    for model_id in ids:
        previous = _preview_warmup_pending.get(model_id)
        covered_limit = max(
            previous[0] if previous is not None else 0,
            _preview_warmup_active.get(model_id, 0),
        )
        if covered_limit < requested_limit:
            _preview_warmup_pending[model_id] = (requested_limit, session_factory)

    if _preview_warmup_task is not None and not _preview_warmup_task.done():
        return True

    async def run():
        while _preview_warmup_pending:
            batch_ids = list(_preview_warmup_pending)[:200]
            entries = [
                (model_id, *_preview_warmup_pending.pop(model_id))
                for model_id in batch_ids
            ]
            for model_id, cover_limit, _factory in entries:
                _preview_warmup_active[model_id] = cover_limit
            # 测试或离线任务可注入独立 session_factory；按对象身份分组，生产
            # 请求通常只形成一个默认组。
            groups: list[tuple[object, list[tuple[int, int]]]] = []
            for model_id, cover_limit, factory in entries:
                group = next((item for item in groups if item[0] is factory), None)
                if group is None:
                    group = (factory, [])
                    groups.append(group)
                group[1].append((model_id, cover_limit))
            try:
                for factory, values in groups:
                    for cover_limit in sorted({value[1] for value in values}):
                        selected_ids = [model_id for model_id, limit_value in values
                                        if limit_value == cover_limit]
                        try:
                            await warm_preview_images(
                                selected_ids, max_per_model=cover_limit,
                                session_factory=factory)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # 单批 DB/文件系统异常不阻塞随后页面的封面；失败缓存由
                            # preview_image_store 按类型决定重试冷却。
                            continue
            finally:
                for model_id, _cover_limit, _factory in entries:
                    _preview_warmup_active.pop(model_id, None)

    task = asyncio.create_task(run(), name="preview-image-warmup")
    _preview_warmup_task = task
    _warmup_tasks.add(task)

    def finished(done: asyncio.Task):
        global _preview_warmup_task
        _warmup_tasks.discard(done)
        if _preview_warmup_task is done:
            _preview_warmup_task = None
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            # 暖缓存永远不能影响页面、推荐发布或服务生命周期。
            pass

    task.add_done_callback(finished)
    return True


def schedule_preview_backfill(
    model_ids: list[int], *, session_factory=async_session,
) -> bool:
    """把缺封面的模特加入全局单例队列，不因页面刷新重复抓取。

    单个模特在同一轮只会处于 pending/active 其中一种状态；完成或失败后进入
    六小时冷却。这样频繁的列表请求只合并任务，不会叠加独立的外网抓取批次。
    """
    global _preview_backfill_task
    now = time.monotonic()
    eligible = {
        int(value) for value in model_ids
        if int(value) > 0
        and int(value) not in _preview_backfill_pending
        and int(value) not in _preview_backfill_active
        and now - _preview_backfill_attempted_at.get(int(value), float("-inf"))
        >= _PROFILE_BACKFILL_RETRY_COOLDOWN_SECONDS
    }
    if not eligible:
        return False
    _preview_backfill_pending.update(eligible)

    async def run():
        while _preview_backfill_pending:
            batch = sorted(_preview_backfill_pending)[:_PROFILE_BACKFILL_BATCH_SIZE]
            _preview_backfill_pending.difference_update(batch)
            _preview_backfill_active.update(batch)
            try:
                await ensure_previews(batch, session_factory=session_factory)
                await backfill_profile_previews(
                    batch, session_factory=session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单批数据库或网络异常不应终止剩余队列；本批进入冷却，稍后再试。
                pass
            finally:
                attempted_at = time.monotonic()
                for model_id in batch:
                    _preview_backfill_attempted_at[model_id] = attempted_at
                _preview_backfill_active.difference_update(batch)

    if _preview_backfill_task is not None and not _preview_backfill_task.done():
        return True

    task = asyncio.create_task(run(), name="preview-profile-backfill")
    _preview_backfill_task = task
    _warmup_tasks.add(task)

    def finished(done: asyncio.Task):
        global _preview_backfill_task
        _warmup_tasks.discard(done)
        if _preview_backfill_task is done:
            _preview_backfill_task = None
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            pass

    task.add_done_callback(finished)
    return True


async def shutdown_preview_warmups():
    global _preview_backfill_task, _preview_warmup_task
    tasks = list(_warmup_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _warmup_tasks.clear()
    _warming_viewkeys.clear()
    _preview_warmup_pending.clear()
    _preview_warmup_active.clear()
    _preview_backfill_pending.clear()
    _preview_backfill_active.clear()
    _preview_backfill_attempted_at.clear()
    _preview_backfill_task = None
    _preview_warmup_task = None
