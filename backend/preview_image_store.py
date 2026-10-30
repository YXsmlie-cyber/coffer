"""视频封面的安全按需下载、标准化与 viewkey 本地缓存。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
import re
import time

import httpx

from .config import settings
from . import image_assets, scraper


PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
_VIEWKEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
_fetch_slots = asyncio.Semaphore(settings.preview_image_fetch_concurrency)
# 固定分片锁避免同一封面被并发重复抓取，同时不会随 45 万条历史视频无限增长。
_cache_locks = tuple(asyncio.Lock() for _ in range(32))
_client_lock = asyncio.Lock()
_page_client: httpx.AsyncClient | None = None
_image_http_client: httpx.AsyncClient | None = None
_failure_cache: OrderedDict[str, tuple[float, type[Exception], str]] = OrderedDict()
_FAILURE_CACHE_LIMIT = 4096
_TRANSIENT_FAILURE_TTL = 5 * 60.0
_PERMANENT_FAILURE_TTL = 24 * 60 * 60.0
_INVALID_IMAGE_FAILURE_TTL = 6 * 60 * 60.0


class PreviewImageError(Exception):
    """封面解析、传输、解码或落盘失败。"""


class InvalidPreviewViewkey(PreviewImageError):
    """viewkey 不符合本地缓存键的安全边界。"""


class PreviewImageUnavailable(PreviewImageError):
    """视频页面已不可用或未提供可验证的封面。"""


class PreviewImageTransientError(PreviewImageError):
    """网络、限流或上游 5xx；短冷却后可以重试。"""


def remember_failure(viewkey: str, detail: str, ttl: float) -> None:
    """调用方自己的超时等失败也进入冷却，避免同一封面在每轮批处理里反复白等。"""
    try:
        value = normalize_viewkey(viewkey)
    except InvalidPreviewViewkey:
        return
    _failure_cache[value] = (
        time.monotonic() + max(1.0, float(ttl)), PreviewImageTransientError, str(detail))
    _failure_cache.move_to_end(value)
    while len(_failure_cache) > _FAILURE_CACHE_LIMIT:
        _failure_cache.popitem(last=False)


def normalize_viewkey(viewkey: str) -> str:
    value = str(viewkey or "").strip()
    if not _VIEWKEY_RE.fullmatch(value):
        raise InvalidPreviewViewkey("视频 viewkey 格式无效")
    return value


def preview_root() -> Path:
    return image_assets.storage_root(str(settings.preview_image_dir))


def preview_path(viewkey: str, *, require_exists: bool = True) -> Path | None:
    value = normalize_viewkey(viewkey)
    path = preview_root() / f"{value}.webp"
    if require_exists and not image_assets.is_regular_file(path, min_size=128):
        return None
    return path


async def _resolve_poster_url(viewkey: str) -> tuple[str, str]:
    result = await scraper.resolve_video_poster(await _get_page_client(), viewkey)
    if result is None:
        raise PreviewImageUnavailable("视频页面不可用，无法取得清晰封面")
    if not result[0]:
        raise PreviewImageUnavailable("视频页面没有提供清晰封面")
    return result


async def _guard_image_request(request: httpx.Request):
    scraper.validate_media_url(str(request.url))


def _image_client() -> httpx.AsyncClient:
    return image_assets.create_image_client(_guard_image_request)


async def _get_page_client() -> httpx.AsyncClient:
    global _page_client
    if _page_client is not None and not _page_client.is_closed:
        return _page_client
    async with _client_lock:
        if _page_client is None or _page_client.is_closed:
            # 页面请求与图片 CDN 使用独立连接池，避免大图片流占满页面重试连接。
            _page_client = scraper.create_page_client()
        return _page_client


async def _get_image_client() -> httpx.AsyncClient:
    global _image_http_client
    if _image_http_client is not None and not _image_http_client.is_closed:
        return _image_http_client
    async with _client_lock:
        if _image_http_client is None or _image_http_client.is_closed:
            _image_http_client = _image_client()
        return _image_http_client


async def close_clients():
    """在 FastAPI lifespan 尾部关闭预览专用页面/CDN keep-alive 池。"""
    global _page_client, _image_http_client
    async with _client_lock:
        clients = [client for client in (_page_client, _image_http_client)
                   if client is not None]
        _page_client = None
        _image_http_client = None
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


async def _download_image(source_url: str, referer: str) -> bytes:
    limit = image_assets.source_limit(settings.preview_image_max_bytes)
    try:
        client = await _get_image_client()
        async with client.stream(
                "GET", source_url, headers={"Referer": referer}) as response:
            if response.status_code != 200:
                detail = f"清晰封面请求返回 HTTP {response.status_code}"
                if response.status_code in {404, 410}:
                    raise PreviewImageUnavailable(detail)
                if response.status_code in {403, 408, 425, 429} or response.status_code >= 500:
                    raise PreviewImageTransientError(detail)
                raise PreviewImageError(detail)
            content_type = image_assets.content_type_of(response)
            if not image_assets.is_acceptable_image_type(content_type):
                raise PreviewImageError(
                    f"清晰封面响应类型无效：{content_type or '缺失'}")
            chunks = await image_assets.read_limited(
                response, limit,
                lambda: PreviewImageError(f"清晰封面超过 {limit} 字节限制"))
            scraper.validate_media_url(str(response.url))
            if not chunks:
                raise PreviewImageError("清晰封面响应为空")
            return chunks
    except PreviewImageError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise PreviewImageTransientError(f"清晰封面下载失败：{detail}") from exc


def _transcode_webp(raw: bytes) -> bytes:
    # PH 封面通常已经是 16:9；fit 只处理少量比例异常并统一浏览器缓存尺寸。
    return image_assets.transcode_webp(
        raw, size=(PREVIEW_WIDTH, PREVIEW_HEIGHT), quality=90,
        error=PreviewImageError, label="清晰封面")


def _persist(viewkey: str, encoded: bytes) -> Path:
    target = preview_path(viewkey, require_exists=False)
    assert target is not None
    image_assets.atomic_write(target, encoded, prefix=viewkey)
    return target


async def ensure_preview_image(viewkey: str) -> Path:
    """返回本地清晰封面；仅在缓存缺失时访问视频详情页。"""
    value = normalize_viewkey(viewkey)
    cached = preview_path(value)
    if cached is not None:
        return cached
    lock = _cache_locks[sum(value.encode("utf-8")) % len(_cache_locks)]
    async with lock:
        cached = preview_path(value)
        if cached is not None:
            return cached
        failure = _failure_cache.get(value)
        now = time.monotonic()
        if failure is not None:
            expires_at, error_type, detail = failure
            if expires_at > now:
                _failure_cache.move_to_end(value)
                raise error_type(detail)
            _failure_cache.pop(value, None)
        try:
            async with _fetch_slots:
                source_url, referer = await _resolve_poster_url(value)
                raw = await _download_image(source_url, referer)
            encoded = await asyncio.to_thread(_transcode_webp, raw)
        except PreviewImageError as error:
            if isinstance(error, PreviewImageTransientError):
                ttl = _TRANSIENT_FAILURE_TTL
            elif isinstance(error, PreviewImageUnavailable):
                ttl = _PERMANENT_FAILURE_TTL
            else:
                ttl = _INVALID_IMAGE_FAILURE_TTL
            _failure_cache[value] = (
                now + ttl, type(error), str(error).strip() or error.__class__.__name__)
            _failure_cache.move_to_end(value)
            while len(_failure_cache) > _FAILURE_CACHE_LIMIT:
                _failure_cache.popitem(last=False)
            raise
        target = await asyncio.to_thread(_persist, value, encoded)
        _failure_cache.pop(value, None)
        return target
