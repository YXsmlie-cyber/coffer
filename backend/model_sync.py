"""单个模特的视频与 115 文件夹同步服务。

HTTP 路由和自动巡查都依赖这里，避免业务逻辑反向依赖 FastAPI 主应用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update

from . import dedup, scraper
from .cloud115 import Cloud115Client, cloud
from .config import settings
from .database import Model, Video, async_session
from .preview_service import refresh_for_models


_UNRESOLVED_CID = object()
logger = logging.getLogger(__name__)
_SNAPSHOT_HASH_VERSION = "video-snapshot-v1"


class _CloudBindingChanged(Exception):
    """发布前发现绑定已变化；退出短事务后再读取该 CID 的云端快照。"""

    def __init__(self, cid: str):
        super().__init__("同步期间 115 目录绑定已变化")
        self.cid = cid


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _snapshot_hash(rows: list[dict]) -> str:
    """对所有会影响 Video/Preview 的字段生成版本化稳定摘要。"""
    digest = hashlib.sha256()
    digest.update(
        f"{_SNAPSHOT_HASH_VERSION};duration_tolerance={settings.duration_tolerance}\n".encode())
    for index, row in enumerate(rows, start=1):
        raw_source_order = row.get("source_order") if "source_order" in row else index
        source_order = index if raw_source_order is None else max(0, int(raw_source_order))
        payload = [
            str(row.get("viewkey") or ""), str(row.get("title") or ""),
            str(row.get("url") or ""), str(row.get("thumbnail") or ""),
            max(0, int(row.get("duration") or 0)),
            source_order,
            str(row.get("dedup_status") or "missing"),
            str(row.get("matched_cloud_name") or ""),
        ]
        digest.update(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        digest.update(b"\n")
    return f"v1:{digest.hexdigest()}"


async def sync_model_core(
    model_id: int,
    *,
    session_factory=async_session,
    cloud_client: Cloud115Client = cloud,
    resolved_cloud_cid: str | None | object = _UNRESOLVED_CID,
    write_semaphore: asyncio.Semaphore | None = None,
) -> dict | None:
    """爬视频、扫描 115、去重并原子替换该模特的视频快照。"""
    timings = {
        "ph_ms": 0.0,
        "cloud_ms": 0.0,
        "dedup_ms": 0.0,
        "db_ms": 0.0,
    }
    async with session_factory() as session:
        model = (
            await session.execute(select(Model).where(Model.id == model_id))
        ).scalar_one_or_none()
    if model is None:
        return None

    resolved_url = model.url
    ph_started = time.perf_counter()
    try:
        try:
            videos = await scraper.scrape_model_videos(resolved_url)
        except scraper.ScrapeError as first_error:
            profile_type = str(model.profile_type or "model").casefold()
            profile_slug = str(model.profile_slug or model.ph_id).split(":", 1)[-1].casefold()
            # 频道/普通用户没有 performer 的别名解析路径；保留原快照并把真实错误交给调用方。
            if profile_type not in {"model", "pornstar"}:
                raise first_error
            # 榜单中的旧别名偶尔会被 PH 改成同账号的新 /model/ 或 /pornstar/ 地址。
            # 只按数据库里精确的 ph_id 重新验证，不按显示名猜测，也不接受身份变化跳转。
            if scraper.model_slug_from_url(resolved_url).casefold() == profile_slug:
                raise first_error
            try:
                profile = await scraper.lookup_model_profile(profile_slug)
            except scraper.ScrapeError:
                raise first_error
            canonical_url = str(profile.get("url") or "").rstrip("/")
            if not canonical_url or canonical_url == resolved_url.rstrip("/"):
                raise first_error
            videos = await scraper.scrape_model_videos(canonical_url)
            resolved_url = canonical_url
    except scraper.ModelProfileUnavailable as error:
        timings["ph_ms"] = _elapsed_ms(ph_started)
        # 站点会以 HTTP 200 返回“主页不可用”外壳，其中仍混有顶部推荐视频。
        # 这是明确的源站状态，不应重试、不应清空既有快照，也不应计为巡查故障。
        return {
            "model_id": model_id,
            "status": "source_unavailable",
            "error": str(error) or "主页不可用或已删除",
            "cloud_cid": model.cloud_cid or "",
            "total": model.total_videos or 0,
            "timings": timings,
        }
    timings["ph_ms"] = _elapsed_ms(ph_started)
    # 抓取抖动或 DOM 变化不能覆盖已有快照，否则会把全量视频误判为缺失。
    if not videos and (model.total_videos or 0) > 0:
        raise scraper.ScrapeError(
            f"抓取返回空但该模特原有 {model.total_videos} 个视频记录，"
            "疑似反爬/DOM 问题，跳过本次写库以保护已有数据。"
        )

    # 已绑定模特优先使用稳定 CID。批量任务会传入同一批次刚读取的根目录匹配结果；
    # 单卡片同步没有 hint，仍在本次调用中实时扫描根目录。
    cloud_started = time.perf_counter()
    if model.cloud_cid:
        cid = model.cloud_cid
    elif resolved_cloud_cid is _UNRESOLVED_CID:
        cid = await cloud_client.find_subdir(settings.video_hub_cid, model.name)
    else:
        cid = str(resolved_cloud_cid or "").strip() or None
    timings["cloud_ms"] = _elapsed_ms(cloud_started)
    observed_cloud_cid = model.cloud_cid or ""

    async def write_snapshot() -> bool:
        db_started = time.perf_counter()
        async with session_factory() as session:
            # SQLite 的 SELECT 不提供行锁。先按读取时的 CID 做条件空更新，
            # 原子验证绑定并取得本次短写事务；后续上传绑定只能在提交后写入。
            # 不跨网络持锁，也不以旧空目录快照覆盖刚由上传创建的新 CID。
            guarded = await session.execute(update(Model).where(
                Model.id == model_id,
                func.coalesce(Model.cloud_cid, "") == observed_cloud_cid,
            ).values(cloud_cid=Model.cloud_cid)
                .execution_options(synchronize_session=False))
            if not guarded.rowcount:
                binding = (await session.execute(select(
                    Model.cloud_cid,
                ).where(Model.id == model_id))).one_or_none()
                if binding is None:
                    return False
                raise _CloudBindingChanged(binding.cloud_cid or "")
            current = (
                await session.execute(select(Model).where(Model.id == model_id))
            ).scalar_one_or_none()
            if current is None:
                return False
            snapshot_changed = current.snapshot_hash != incoming_snapshot_hash
            # 首次升级旧库时先比较既有行；内容已经相同就只补摘要，不制造一次
            # 30k+ 行的无意义 DELETE/INSERT。
            if snapshot_changed and not str(current.snapshot_hash or ""):
                existing_rows = (await session.execute(select(
                    Video.viewkey, Video.title, Video.url, Video.thumbnail,
                    Video.duration, Video.source_order, Video.dedup_status,
                    Video.matched_cloud_name,
                ).where(Video.model_id == model_id).order_by(
                    Video.source_order.asc(), Video.id.asc()
                ))).all()
                existing_payload = [{
                    "viewkey": row.viewkey,
                    "title": row.title,
                    "url": row.url,
                    "thumbnail": row.thumbnail,
                    "duration": row.duration,
                    # 旧库全为 0/NULL 时，查询已按 id 稳定兜底；用该顺序参与
                    # 首次摘要，内容相同即可补 hash 而不重写整张 Video 表。
                    "source_order": int(row.source_order or 0) or index,
                    "dedup_status": row.dedup_status,
                    "matched_cloud_name": row.matched_cloud_name,
                } for index, row in enumerate(existing_rows, start=1)]
                snapshot_changed = _snapshot_hash(existing_payload) != incoming_snapshot_hash

            current.url = resolved_url
            current.cloud_cid = cid or ""
            current.total_videos = len(deduped)
            current.cloud_videos = counts["exists"]
            current.maybe_videos = counts["maybe"]
            current.missing_videos = counts["missing"]
            current.synced_at = datetime.now()
            current.snapshot_hash = incoming_snapshot_hash

            if snapshot_changed:
                # Video 与由它派生的四封面在同一事务发布；外部不会看到跨代数据。
                await session.execute(delete(Video).where(Video.model_id == model_id))
                if snapshot_rows:
                    await session.execute(insert(Video), [{
                    "model_id": model_id,
                    "viewkey": video["viewkey"],
                    "title": video["title"],
                    "url": video["url"],
                    "thumbnail": video.get("thumbnail", ""),
                    "duration": video.get("duration", 0),
                    "source_order": video["source_order"],
                    "dedup_status": video["dedup_status"],
                    "matched_cloud_name": video.get("matched_cloud_name", ""),
                    } for video in snapshot_rows])
                try:
                    # 预览物化置于 SAVEPOINT：成功则与 Video 同事务发布；若展示层
                    # 异常，只回滚预览 DML 并保留旧预览，主快照仍按历史语义成功。
                    async with session.begin_nested():
                        await refresh_for_models(
                            [model_id], session_factory=session_factory,
                            session=session, commit=False)
                except Exception:
                    logger.exception("刷新模特四封面失败 model_id=%s", model_id)
            await session.commit()
        timings["db_ms"] = _elapsed_ms(db_started)
        timings["snapshot_changed"] = bool(snapshot_changed)
        return True

    # 只为一次并发绑定变化补读云端，不重新抓 PH 或增加网络重试额度。
    # 若绑定反复变化，保留已提交快照并明确失败，避免无限追赶或发布旧计数。
    for binding_attempt in range(2):
        cloud_started = time.perf_counter()
        cloud_files = (
            await cloud_client.list_all_files(cid, only_video=True) if cid else []
        )
        timings["cloud_ms"] += _elapsed_ms(cloud_started)
        dedup_started = time.perf_counter()
        deduped = dedup.dedup_model_videos(videos, cloud_files)
        snapshot_rows = [{**video, "source_order": index}
                         for index, video in enumerate(deduped, start=1)]
        incoming_snapshot_hash = _snapshot_hash(snapshot_rows)
        counts = dedup.count_missing(deduped)
        timings["dedup_ms"] += _elapsed_ms(dedup_started)
        try:
            if write_semaphore is None:
                written = await write_snapshot()
            else:
                async with write_semaphore:
                    written = await write_snapshot()
        except _CloudBindingChanged as changed:
            if binding_attempt:
                raise scraper.ScrapeError(
                    "同步期间 115 目录绑定连续变化，已保留原快照，请稍后重试"
                ) from None
            observed_cloud_cid = changed.cid
            cid = changed.cid or None
        else:
            break
    if not written:
        return None

    return {
        "model_id": model_id,
        "status": "synced",
        "counts": counts,
        "cloud_cid": cid,
        "total": len(deduped),
        "snapshot_changed": bool(timings.pop("snapshot_changed", False)),
        "timings": timings,
    }
