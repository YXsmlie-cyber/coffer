"""模特头像的安全下载、标准化与内容寻址存储。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

import httpx

from .config import settings
from . import image_assets
from .scraper import validate_media_url, validate_ph_url


AVATAR_SIZE = 256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AvatarStoreError(Exception):
    """头像下载、解码、校验或落盘失败。"""


class AvatarFetchError(AvatarStoreError):
    """头像传输暂时失败，可以在同一权威 URL 上有限重试。"""


@dataclass(frozen=True)
class AvatarAsset:
    source_url: str
    sha256: str
    byte_size: int
    width: int = AVATAR_SIZE
    height: int = AVATAR_SIZE


async def _guard_avatar_request(request: httpx.Request):
    validate_media_url(str(request.url))


def create_client() -> httpx.AsyncClient:
    """创建头像下载客户端；每次重定向都经过媒体 URL 安全校验。"""
    return image_assets.create_image_client(_guard_avatar_request)


def avatar_root() -> Path:
    return image_assets.storage_root(str(settings.avatar_dir))


def avatar_path(sha256: str, *, require_exists: bool = True) -> Path | None:
    digest = str(sha256 or "").strip().casefold()
    if not _SHA256_RE.fullmatch(digest):
        return None
    path = avatar_root() / f"{digest}.webp"
    if require_exists and not image_assets.is_regular_file(path):
        return None
    return path


async def _download(client: httpx.AsyncClient, source_url: str,
                    *, referer: str | None = None) -> tuple[bytes, str]:
    source_url = validate_media_url(source_url)
    request_headers = None
    if referer:
        request_headers = {"Referer": validate_ph_url(referer)}
    try:
        async with client.stream("GET", source_url, headers=request_headers) as response:
            if response.status_code != 200:
                error = f"头像请求返回 HTTP {response.status_code}"
                if response.status_code in {403, 408, 425, 429} or response.status_code >= 500:
                    raise AvatarFetchError(error)
                raise AvatarStoreError(error)
            content_type = image_assets.content_type_of(response)
            if not image_assets.is_acceptable_image_type(content_type):
                raise AvatarStoreError(f"头像响应类型无效：{content_type or '缺失'}")
            limit = image_assets.source_limit(settings.avatar_max_bytes)
            chunks = await image_assets.read_limited(
                response, limit,
                lambda: AvatarStoreError(f"头像源文件超过 {limit} 字节限制"))
            if not chunks:
                raise AvatarFetchError("头像响应为空")
            final_url = validate_media_url(str(response.url))
            return chunks, final_url
    except AvatarStoreError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise AvatarFetchError(f"头像下载失败：{detail}") from exc


def _transcode_webp(raw: bytes) -> bytes:
    return image_assets.transcode_webp(
        raw, size=(AVATAR_SIZE, AVATAR_SIZE), quality=88,
        error=AvatarStoreError, label="头像")


def _persist(encoded: bytes) -> tuple[str, int]:
    digest = hashlib.sha256(encoded).hexdigest()
    root = avatar_root()
    target = root / f"{digest}.webp"
    if target.exists():
        existing = target.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise AvatarStoreError(f"本地头像文件校验失败：{target.name}")
        return digest, len(existing)

    def verify(temporary: Path) -> None:
        if image_assets.sha256_file(temporary) != digest:
            raise AvatarStoreError("头像临时文件写入后校验失败")

    image_assets.atomic_write(target, encoded, prefix=digest, verify=verify)
    return digest, len(encoded)


async def cache_avatar(source_url: str, *, client: httpx.AsyncClient | None = None,
                       referer: str | None = None) -> AvatarAsset:
    """下载头像，验证并转换为本地 WebP；任何失败都不会产生数据库状态。"""
    async def download(active_client):
        delay = 0.8
        for attempt in range(4):
            try:
                return await _download(active_client, source_url, referer=referer)
            except AvatarFetchError:
                if attempt >= 3:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 1.8, 4.0)
        raise AvatarFetchError("头像下载重试意外结束")

    if client is None:
        async with create_client() as owned_client:
            raw, final_url = await download(owned_client)
    else:
        raw, final_url = await download(client)
    encoded = await asyncio.to_thread(_transcode_webp, raw)
    digest, byte_size = await asyncio.to_thread(_persist, encoded)
    return AvatarAsset(source_url=final_url, sha256=digest, byte_size=byte_size)
