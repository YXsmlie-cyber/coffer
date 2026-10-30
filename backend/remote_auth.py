"""外网访问会话。

Nginx 只在登录接口用既有 htpasswd 校验一次，并通过受信头通知应用；应用随后
签发 HMAC 保护的 HttpOnly Cookie。密码不会进入应用进程，也不会写入本地文件。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import time
from urllib.parse import urlparse

from .config import DATA_DIR, settings


COOKIE_NAME = "ph115_remote_session"
REMOTE_HEADER = "x-ph115-remote"
VERIFIED_HEADER = "x-ph115-auth-verified"
PUBLIC_API_PATHS = frozenset({
    "/api/remote-auth/status",
    "/api/remote-auth/login",
    "/api/remote-auth/logout",
})

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PREFIX_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")
_SECRET_PATH = DATA_DIR / "remote_auth_secret"
_secret_lock = threading.Lock()
_secret_cache: bytes | None = None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request_host(headers) -> str:
    raw = str(headers.get("host", "") or "").strip()
    if not raw:
        return ""
    try:
        return (urlparse(f"//{raw}").hostname or "").strip("[]").lower()
    except ValueError:
        return ""


def is_remote(headers) -> bool:
    """显式反代标记优先；非回环 Host 作为漏配标记时的安全兜底。"""
    if str(headers.get(REMOTE_HEADER, "")).strip() == "1":
        return True
    host = _request_host(headers)
    return bool(host and host not in _LOOPBACK_HOSTS)


def _is_loopback(value: str | None) -> bool:
    host = str(value or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def proxy_header_verified(headers) -> bool:
    return str(headers.get(VERIFIED_HEADER, "")).strip() == "1"


def proxy_source_verified(client_host: str | None) -> bool:
    return _is_loopback(client_host)


def proxy_verified(headers, client_host: str | None) -> bool:
    """受信头必须来自本机 Nginx/SSH 隧道，公网直连不能靠伪造头绕过。"""
    return proxy_source_verified(client_host) and proxy_header_verified(headers)


def cookie_path(headers) -> str:
    prefix = str(headers.get("x-forwarded-prefix", "") or "").strip()
    if not prefix or not _PREFIX_RE.fullmatch(prefix):
        return "/"
    normalized = "/" + prefix.strip("/")
    return normalized if normalized != "/" else "/"


def _read_secret() -> bytes:
    try:
        value = _SECRET_PATH.read_bytes()
    except FileNotFoundError:
        return b""
    return value if len(value) >= 32 else b""


def session_secret() -> bytes:
    """惰性建立本机随机密钥；文件只含随机字节，不含账号或密码。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    with _secret_lock:
        if _secret_cache is not None:
            return _secret_cache
        existing = _read_secret()
        if existing:
            _secret_cache = existing
            return existing

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_bytes(32)
        try:
            fd = os.open(_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # 极窄的多进程首次启动竞态；另一进程写完后再读。
            for _ in range(20):
                existing = _read_secret()
                if existing:
                    _secret_cache = existing
                    return existing
                time.sleep(0.01)
            raise RuntimeError("外网会话密钥文件无效")
        else:
            try:
                os.write(fd, generated)
            finally:
                os.close(fd)
            _secret_cache = generated
            return generated


def issue_session(
    remember: bool,
    *,
    now: int | None = None,
    secret: bytes | None = None,
) -> tuple[str, int, int]:
    current = int(time.time() if now is None else now)
    if remember:
        max_age = max(1, min(int(settings.remote_remember_days), 90)) * 86400
    else:
        max_age = max(1, min(int(settings.remote_session_hours), 72)) * 3600
    expires_at = current + max_age
    payload = f"v1.{expires_at}.{_b64(secrets.token_bytes(18))}"
    signature = _b64(hmac.new(
        secret or session_secret(), payload.encode("ascii"), hashlib.sha256
    ).digest())
    return f"{payload}.{signature}", expires_at, max_age


def session_expiry(
    token: str | None,
    *,
    now: int | None = None,
    secret: bytes | None = None,
) -> int | None:
    if not token:
        return None
    try:
        version, expiry_text, nonce, supplied = token.split(".", 3)
        expires_at = int(expiry_text)
        if version != "v1" or not nonce or expires_at <= int(
                time.time() if now is None else now):
            return None
        payload = f"{version}.{expiry_text}.{nonce}"
        expected = _b64(hmac.new(
            secret or session_secret(), payload.encode("ascii"), hashlib.sha256
        ).digest())
        return expires_at if hmac.compare_digest(supplied, expected) else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
