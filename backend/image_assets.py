"""头像与封面共用的图片安全原语：受控下载、解码校验、WebP 标准化与原子落盘。

两个存储模块（`avatar_store`、`preview_image_store`）只保留各自的缓存键、
重试与错误分类；涉及解码炸弹、内容类型、体积上限和原子替换的规则只在这里维护一份。
调用方传入自己的异常类型与文案前缀，因此对外抛出的异常类和提示保持原样。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
import hashlib
from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings
from .scraper import HEADERS


IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*;q=0.8"
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 8192
# 只接受位图；SVG/ICO 可能携带脚本或绕过尺寸校验，直接拒绝。
_REJECTED_CONTENT_TYPES = frozenset({"image/svg+xml", "image/svg", "image/x-icon"})
_MIN_SOURCE_LIMIT = 64 * 1024


def create_image_client(
        guard: Callable[[httpx.Request], Awaitable[None]]) -> httpx.AsyncClient:
    """创建图片 CDN 客户端；每次请求（含重定向）都先经过调用方的 URL 守卫。"""
    return httpx.AsyncClient(
        headers={**HEADERS, "Accept": IMAGE_ACCEPT},
        timeout=httpx.Timeout(40.0, connect=12.0),
        follow_redirects=True,
        proxy=settings.proxy or None,
        cookies={"age_verified": "1", "platform": "pc"},
        event_hooks={"request": [guard]},
    )


@lru_cache(maxsize=8)
def _resolved_root(configured: str) -> Path:
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def storage_root(configured: str) -> Path:
    """解析并确保缓存根目录存在；目录被外部删除后会自动重建。"""
    root = _resolved_root(configured)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    return root


def is_regular_file(path: Path, *, min_size: int = 0) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size >= min_size


def source_limit(configured: int) -> int:
    return max(_MIN_SOURCE_LIMIT, int(configured))


def content_type_of(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()


def is_acceptable_image_type(content_type: str) -> bool:
    return content_type.startswith("image/") and content_type not in _REJECTED_CONTENT_TYPES


def declared_length(response: httpx.Response) -> int:
    try:
        return int(response.headers.get("content-length") or 0)
    except ValueError:
        return 0


async def read_limited(response: httpx.Response, limit: int,
                       oversize: Callable[[], Exception]) -> bytes:
    """流式读取响应体；声明或实际体积超过上限时立即抛出 `oversize()`。"""
    if declared_length(response) > limit:
        raise oversize()
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunks) + len(chunk) > limit:
            raise oversize()
        chunks.extend(chunk)
    return bytes(chunks)


def _check_dimensions(size: tuple[int, int], error: type[Exception], label: str) -> None:
    width, height = size
    if (width < 1 or height < 1 or width > MAX_DIMENSION
            or height > MAX_DIMENSION or width * height > MAX_PIXELS):
        raise error(f"{label}尺寸异常：{width}x{height}")


def transcode_webp(raw: bytes, *, size: tuple[int, int], quality: int,
                   error: type[Exception], label: str) -> bytes:
    """先 verify 再完整解码，按 EXIF 方向居中裁切为固定尺寸 WebP。"""
    try:
        with Image.open(BytesIO(raw)) as probe:
            _check_dimensions(probe.size, error, label)
            probe.verify()
        with Image.open(BytesIO(raw)) as source:
            _check_dimensions(source.size, error, label)
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(
                image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            output = BytesIO()
            image.save(output, format="WEBP", quality=quality, method=6, exact=True)
            encoded = output.getvalue()
    except error:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise error(f"{label}不是可安全解码的图片：{exc}") from exc
    if not encoded:
        raise error(f"{label}转换后为空")
    return encoded


def atomic_write(target: Path, data: bytes, *, prefix: str,
                 verify: Callable[[Path], None] | None = None) -> None:
    """同目录临时文件 + fsync + `os.replace`；`verify` 失败时不会替换目标。"""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f"{prefix}.", suffix=".tmp",
                dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if verify is not None:
            verify(temporary)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
