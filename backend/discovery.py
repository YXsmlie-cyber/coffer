"""视频发现快照服务：保存来源结果，并把上传者幂等并入既有同步对象。"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime

from sqlalchemy import delete, func, select

from .database import DiscoveryItem, Model, async_session


async def save_snapshot(result: dict, *, session_factory=async_session) -> dict:
    """原子替换单个来源快照，保留既有 Model/Task/Video 身份。"""
    source = str(result["source"])
    raw_items = list(result.get("items") or [])
    creators: OrderedDict[str, dict] = OrderedDict()
    for item in raw_items:
        creator = dict(item["creator"])
        creators.setdefault(str(creator["ph_id"]), creator)

    fetched_at = datetime.now()
    model_ids: dict[str, int] = {}
    async with session_factory() as session:
        last_order = (await session.execute(
            select(func.max(Model.scrape_order))
        )).scalar_one_or_none() or 0
        next_order = int(last_order)
        existing_by_identity = {
            model.ph_id: model for model in (await session.execute(select(Model).where(
                Model.ph_id.in_(list(creators))
            ))).scalars().all()
        } if creators else {}

        for identity, creator in creators.items():
            model = existing_by_identity.get(identity)
            if model is None:
                next_order += 1
                model = Model(
                    ph_id=identity,
                    profile_type=creator["profile_type"],
                    profile_slug=creator["profile_slug"],
                    name=creator["name"],
                    url=creator["url"],
                    thumbnail="",
                    avatar_status="pending",
                    rank=0,
                    scrape_order=next_order,
                )
                session.add(model)
                existing_by_identity[identity] = model
            else:
                # 发现来源只更新权威身份文字与当前主页，不覆盖榜单名次、同步结果或头像。
                model.profile_type = creator["profile_type"]
                model.profile_slug = creator["profile_slug"]
                model.name = creator["name"]
                model.url = creator["url"]
        # 新上传者一次 flush 分配全部主键；避免每位上传者 SELECT + flush 的 N+1。
        await session.flush()
        for identity, model in existing_by_identity.items():
            if identity in creators:
                model_ids[identity] = model.id

        await session.execute(delete(DiscoveryItem).where(DiscoveryItem.source == source))
        seen_items: set[tuple[int, str]] = set()
        saved_items = 0
        for item in raw_items:
            model_id = model_ids[str(item["creator"]["ph_id"])]
            item_key = str(item.get("viewkey") or item.get("url") or item.get("source_order"))
            unique_key = (model_id, item_key)
            if unique_key in seen_items:
                continue
            seen_items.add(unique_key)
            session.add(DiscoveryItem(
                source=source,
                model_id=model_id,
                viewkey=str(item.get("viewkey") or ""),
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                thumbnail=str(item.get("thumbnail") or ""),
                duration=max(0, int(item.get("duration") or 0)),
                source_order=max(0, int(item.get("source_order") or 0)),
                fetched_at=fetched_at,
            ))
            saved_items += 1
        await session.commit()

    return {
        "source": source,
        "label": result.get("label", source),
        "url": result.get("url", ""),
        "model_ids": list(model_ids.values()),
        "creator_count": len(model_ids),
        "item_count": saved_items,
        "requested_max": int(result.get("requested_max") or len(model_ids)),
        "reached_limit": bool(result.get("reached_limit")),
        "page_count": max(0, int(result.get("page_count") or 0)),
        "fetched_at": fetched_at.isoformat(),
    }
