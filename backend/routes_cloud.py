"""115 登录状态、二维码/Cookie 登录与本机插件桥接口。"""
from fastapi import APIRouter, HTTPException, Request, Response

from .cloud115 import cloud, Cloud115Error
from . import remote_auth
from .api_schemas import (
    CookieLogin, QRCodePoll, PluginDirectoryRequest, PluginMagnetSubmitRequest,
)


router = APIRouter()


def _require_plugin_loopback(request: Request) -> None:
    """Plugin bridge is deliberately unavailable through authenticated remote proxies."""
    client_host = request.client.host if request.client is not None else None
    if remote_auth.is_remote(request.headers) or not remote_auth.proxy_source_verified(client_host):
        raise HTTPException(404, "Not found")


def _plugin_status_projection(value: object, allowed: set[str], fallback: str) -> dict[str, str]:
    status = value.get("status") if isinstance(value, dict) else None
    return {"status": status if status in allowed else fallback}


@router.get("/api/115/status")
async def cloud_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    ok = await cloud.check_login()
    return {"logged_in": ok}


@router.post("/api/115/plugin/directory-ready")
async def plugin_directory_ready(
    body: PluginDirectoryRequest, request: Request, response: Response,
):
    _require_plugin_loopback(request)
    response.headers["Cache-Control"] = "no-store"
    result = await cloud.plugin_directory_ready(body.directory_id)
    return _plugin_status_projection(
        result,
        {"READY", "REJECTED_DIRECTORY", "REJECTED_LOGIN", "DIRECTORY_VALIDATION_UNKNOWN"},
        "DIRECTORY_VALIDATION_UNKNOWN",
    )


@router.post("/api/115/plugin/magnet-submit")
async def plugin_magnet_submit(
    body: PluginMagnetSubmitRequest, request: Request, response: Response,
):
    _require_plugin_loopback(request)
    response.headers["Cache-Control"] = "no-store"
    result = await cloud.plugin_submit_magnet(body.magnet, body.directory_id)
    return _plugin_status_projection(
        result,
        {"CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", "UNKNOWN_RESULT"},
        "UNKNOWN_RESULT",
    )


@router.post("/api/115/login/cookie")
async def cloud_login_cookie(body: CookieLogin, response: Response):
    response.headers["Cache-Control"] = "no-store"
    candidate = body.cookie.strip()
    ok = await cloud.check_cookie(candidate)
    if not ok:
        raise HTTPException(400, "Cookie无效或已过期")
    cloud.set_cookie(candidate)
    return {"logged_in": True}


@router.post("/api/115/login/qrcode")
async def cloud_qrcode(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await cloud.qrcode_start()
    except Cloud115Error as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/115/login/qrcode/poll")
async def cloud_qrcode_poll(body: QRCodePoll, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await cloud.qrcode_poll(body.uid, body.time, body.sign)
        # 接口边界再做一次白名单投影，底层库未来即使附带 Cookie 也不会出浏览器。
        return {"status": int(result.get("status", 0))}
    except Cloud115Error as e:
        raise HTTPException(500, str(e)) from e
