"""从权威个人主页准备完整头像资产，供榜单入库与存量修复共用。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .config import settings
from . import avatar_store, scraper


_client_lock = asyncio.Lock()
_profile_client = None
_image_client = None
_AVATAR_REUSE_TTL = timedelta(days=7)


async def _shared_clients():
    global _profile_client, _image_client
    if (_profile_client is not None and not _profile_client.is_closed
            and _image_client is not None and not _image_client.is_closed):
        return _profile_client, _image_client
    async with _client_lock:
        if _profile_client is None or _profile_client.is_closed:
            _profile_client = scraper.create_page_client()
        if _image_client is None or _image_client.is_closed:
            _image_client = avatar_store.create_client()
        return _profile_client, _image_client


async def close_clients():
    global _profile_client, _image_client
    async with _client_lock:
        clients = [client for client in (_profile_client, _image_client)
                   if client is not None]
        _profile_client = None
        _image_client = None
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


def _reuse_existing(item: dict, existing: dict | None) -> dict | None:
    if not existing or str(existing.get("avatar_status") or "") != "ready":
        return None
    digest = str(existing.get("avatar_sha256") or "").strip().casefold()
    checked_at = existing.get("avatar_checked_at")
    now = (datetime.now(checked_at.tzinfo)
           if isinstance(checked_at, datetime) and checked_at.tzinfo else datetime.now())
    if (not digest or not isinstance(checked_at, datetime)
            or now - checked_at > _AVATAR_REUSE_TTL
            or avatar_store.avatar_path(digest) is None):
        return None
    return {
        **item,
        "avatar_source_url": str(existing.get("avatar_source_url") or ""),
        "avatar_sha256": digest,
        "avatar_updated_at": existing.get("avatar_updated_at") or checked_at,
        "avatar_status": "ready",
        "avatar_error": "",
        "avatar_checked_at": checked_at,
    }


async def _prepare_one(item: dict, profile_client, image_client,
                       *, force_profile: bool = False) -> dict:
    source_url = str(item.get("thumbnail") or "").strip()
    if source_url and not force_profile:
        # 标准榜单已经给出该模特的头像；直接资产化，避免为每张卡片再请求一次主页。
        profile = item
    else:
        # discover 只提供上传者身份，必须读取个人主页才能得到真实头像。
        profile = await scraper.fetch_model_profile_url(item["url"], client=profile_client)
        if profile["ph_id"].casefold() != str(item["ph_id"]).casefold():
            raise scraper.ModelLookupUnavailable("个人主页返回的模特身份与榜单不一致")
        source_url = profile["thumbnail"]
    asset = await avatar_store.cache_avatar(
        source_url, client=image_client, referer=profile["url"])
    return {
        **item,
        "ph_id": profile["ph_id"],
        "name": profile["name"],
        "url": profile["url"],
        "thumbnail": profile["thumbnail"],
        "avatar_source_url": asset.source_url,
        "avatar_sha256": asset.sha256,
        "avatar_updated_at": datetime.now(),
        "avatar_status": "ready",
        "avatar_error": "",
        "avatar_checked_at": datetime.now(),
    }


async def prepare_models(models: list[dict], *, concurrency: int | None = None,
                         force_profile: bool = False,
                         existing_assets: dict[str, dict] | None = None,
                         profile_client=None, image_client=None,
                         ) -> tuple[list[dict], list[dict]]:
    """并发准备头像，返回保持原顺序的成功项和结构化失败项。"""
    if not models:
        return [], []
    worker_count = min(
        len(models), max(1, int(concurrency or settings.avatar_fetch_concurrency)))
    queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
    for index, item in enumerate(models):
        queue.put_nowait((index, item))
    prepared: list[dict | None] = [None] * len(models)
    failures: list[dict] = []

    if not force_profile and existing_assets:
        # 首次榜单可能要 stat 数千个本地摘要文件；整批放到线程，TTL 命中
        # 时仍然只做轻量 Python 判断，不占用 API 事件循环。
        def collect_reusable():
            return {
                index: reused
                for index, item in enumerate(models)
                if (reused := _reuse_existing(
                    item,
                    existing_assets.get(
                        str(item.get("ph_id") or "").casefold()),
                )) is not None
            }

        for index, reused in (await asyncio.to_thread(collect_reusable)).items():
            prepared[index] = reused
    if all(item is not None for item in prepared):
        return [item for item in prepared if item is not None], []

    if profile_client is None or image_client is None:
        profile_client, image_client = await _shared_clients()

    async def worker():
        while True:
            try:
                index, item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if prepared[index] is not None:
                queue.task_done()
                continue
            try:
                prepared[index] = await _prepare_one(
                    item, profile_client, image_client,
                    force_profile=force_profile)
            except Exception as exc:
                failures.append({
                    "index": index,
                    "ph_id": str(item.get("ph_id") or ""),
                    "name": str(item.get("name") or ""),
                    "url": str(item.get("url") or ""),
                    "error": str(exc) or exc.__class__.__name__,
                })
            finally:
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(worker_count)))
    failures.sort(key=lambda item: item["index"])
    return [item for item in prepared if item is not None], failures
