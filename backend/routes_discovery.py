"""视频发现来源、抓取与快照读取接口。"""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func

from .database import async_session, DiscoveryItem, Model, Video
from . import discovery as discovery_service, scraper
from .library_index import library_index
from .preview_service import previews_by_model, schedule_preview_warmup
from .api_schemas import DiscoveryRequest
from .api_wire import _model_wire, _pipeline_by_model


router = APIRouter()


@router.get("/api/discovery/sources")
async def discovery_sources():
    return {"sources": [{
        "key": item["key"], "label": item["label"], "url": item["url"],
    } for item in scraper.discovery_source_catalog()]}


@router.post("/api/discovery/scrape")
async def scrape_discovery(body: DiscoveryRequest):
    try:
        result = await scraper.scrape_discovery(body.source, body.max_creators)
    except scraper.InvalidScrapeURL as exc:
        raise HTTPException(400, str(exc)) from exc
    except scraper.ScrapeError as exc:
        raise HTTPException(502, str(exc)) from exc
    saved = await discovery_service.save_snapshot(result)
    async with async_session() as session:
        membership = await library_index.membership(session=session)
        rows = (await session.execute(select(Model).where(
            Model.id.in_(saved.get("model_ids") or [-1])
        ))).scalars().all()
    allowed = [model.id for model in rows if not membership.contains(model)]
    saved["model_ids"] = allowed
    saved["library_filter"] = "exclude"
    saved["library_scan_generation"] = membership.scan_generation
    return saved


@router.get("/api/discovery")
async def get_discovery(source: str = Query(min_length=1, max_length=40)):
    catalog = {item["key"]: item for item in scraper.discovery_source_catalog()}
    definition = catalog.get(source)
    if definition is None:
        raise HTTPException(400, "未知的视频发现来源")

    async with async_session() as session:
        membership = await library_index.membership(session=session)
        rows = (await session.execute(
            select(DiscoveryItem, Model)
            .join(Model, Model.id == DiscoveryItem.model_id)
            .where(DiscoveryItem.source == source)
            .order_by(DiscoveryItem.source_order, DiscoveryItem.id)
        )).all()
        rows = [(item, model) for item, model in rows
                if not membership.contains(model)]
        model_ids = list(dict.fromkeys(model.id for _, model in rows))
        pipeline = await _pipeline_by_model(session, model_ids)
        materialized_previews = await previews_by_model(session, model_ids)

        groups: dict[int, dict] = {}
        for item, model in rows:
            group = groups.setdefault(model.id, {
                **_model_wire(model, pipeline.get(model.id)),
                "source_order": item.source_order,
                "discovery_count": 0,
                "discovery_videos": [],
                "fetched_at": item.fetched_at.isoformat(),
            })
            group["source_order"] = min(group["source_order"], item.source_order)
            group["discovery_count"] += 1
            if item.viewkey or item.thumbnail or item.title:
                group["discovery_videos"].append({
                    "viewkey": item.viewkey,
                    "title": item.title,
                    "url": item.url,
                    "thumbnail": item.thumbnail,
                    "duration": item.duration,
                })

        # 频道目录页本身没有作品封面时只读物化预览，不扫描完整Video。
        fallback_ids = [model_id for model_id, group in groups.items()
                        if not any(video.get("thumbnail")
                                   for video in group["discovery_videos"])]
        for model_id in fallback_ids:
            groups[model_id]["discovery_videos"] = (
                materialized_previews.get(model_id, [])[:4]
            )
        unresolved_ids = [model_id for model_id in fallback_ids
                          if not groups[model_id]["discovery_videos"]]
        if unresolved_ids:
            ranked = select(
                Video.model_id,
                Video.viewkey,
                Video.title,
                Video.url,
                Video.thumbnail,
                Video.duration,
                func.row_number().over(
                    partition_by=Video.model_id,
                    order_by=Video.id,
                ).label("row_number"),
            ).where(
                Video.model_id.in_(unresolved_ids), Video.thumbnail != ""
            ).subquery()
            fallback_rows = (await session.execute(
                select(ranked).where(ranked.c.row_number <= 4)
            )).mappings().all()
            for video in fallback_rows:
                groups[video["model_id"]]["discovery_videos"].append({
                    "viewkey": video["viewkey"],
                    "title": video["title"],
                    "url": video["url"],
                    "thumbnail": video["thumbnail"],
                    "duration": video["duration"],
                    "preview_image_url": f"/api/previews/{video['viewkey']}/image",
                })

    creators = sorted(groups.values(), key=lambda item: (item["source_order"], item["id"]))
    schedule_preview_warmup(model_ids, session_factory=async_session)
    # source_order 是原始作品卡片序号；同一上传者合并后可能出现 1、4、7 的跳号。
    # 单独下发连续名次，既保留稳定排序依据，也让发现页榜单名次符合用户预期。
    for source_rank, creator in enumerate(creators, start=1):
        creator["source_rank"] = source_rank
    fetched_at = max((item["fetched_at"] for item in creators), default=None)
    return {
        "source": source,
        "label": definition["label"],
        "url": definition["url"],
        "fetched_at": fetched_at,
        "library_filter": "exclude",
        "library_scan_generation": membership.scan_generation,
        "library_stale": membership.stale,
        "creators": creators,
    }
