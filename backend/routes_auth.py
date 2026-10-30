"""外网应用内登录会话接口。"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response

from .config import settings
from . import remote_auth
from .api_schemas import RemoteLoginRequest


router = APIRouter()


@router.get("/api/remote-auth/status")
async def remote_auth_status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    required = remote_auth.is_remote(request.headers)
    expires_at = (remote_auth.session_expiry(
        request.cookies.get(remote_auth.COOKIE_NAME)) if required else None)
    return {
        "required": required,
        "authenticated": not required or expires_at is not None,
        "expires_at": (
            datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
            if expires_at else None
        ),
        "remember_days": max(1, min(int(settings.remote_remember_days), 90)),
    }


@router.post("/api/remote-auth/login")
async def remote_auth_login(
    body: RemoteLoginRequest, request: Request, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    if not remote_auth.is_remote(request.headers):
        return {"authenticated": True, "required": False, "expires_at": None}
    # 该头只能由反代的精确登录 location 在 htpasswd 校验成功后写入；
    # Nginx 同时会清空转发给应用的 Authorization，应用永远接触不到密码。
    client_host = request.client.host if request.client else None
    if not remote_auth.proxy_header_verified(request.headers):
        raise HTTPException(403, "外网反向代理未传递登录验证标记")
    if not remote_auth.proxy_source_verified(client_host):
        raise HTTPException(403, "外网登录请求的代理来源不受信任")
    try:
        token, expires_at, max_age = remote_auth.issue_session(body.remember)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(500, "无法初始化外网登录会话") from exc
    response.set_cookie(
        key=remote_auth.COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=datetime.fromtimestamp(expires_at, timezone.utc),
        path=remote_auth.cookie_path(request.headers),
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return {
        "authenticated": True,
        "required": True,
        "expires_at": datetime.fromtimestamp(
            expires_at, timezone.utc).isoformat(),
    }


@router.post("/api/remote-auth/logout")
async def remote_auth_logout(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        remote_auth.COOKIE_NAME,
        path=remote_auth.cookie_path(request.headers),
        secure=remote_auth.is_remote(request.headers),
        httponly=True,
        samesite="lax",
    )
    return {"authenticated": False}
