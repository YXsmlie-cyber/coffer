"""FastAPI 主应用：生命周期装配、中间件、诊断与 WebSocket。

业务路由按领域拆分在 `routes_*.py`，请求模型在 `api_schemas.py`，
共用序列化在 `api_wire.py`；本模块按原顺序注册它们并保留旧导出名。
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path as _Path
import sys as _sys
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_DIR
from .diagnostics import DiagnosticsMiddleware, diagnostics, install_sql_observer, read_build_identity
# 保留 main 的旧导出，现有调用和测试仍可从这里导入。
from .websocket_hub import WSHub, _WSClientState  # noqa: F401
from .database import init_db, engine
from .cloud115 import cloud
from . import avatar_service, preview_image_store
from .task_manager import manager
from .model_sync_coordinator import model_sync_coordinator
from .patrol import patrol
from .notifier import notifier
from .daily_digest import DailyDigest
from . import remote_auth
from .library_index import library_index
from .taste_model import taste_model_service
from .preview_service import shutdown_preview_warmups
# 兼容导出：旧调用方经 backend.main 访问的共享对象。
from fastapi import HTTPException  # noqa: F401
from .database import async_session  # noqa: F401
from . import avatar_store, scraper  # noqa: F401
from . import (
    routes_auth, routes_cloud, routes_discovery, routes_library, routes_models,
    routes_ops, routes_taste, routes_tasks,
)
# 兼容导出：旧脚本与测试仍可从 backend.main 导入路由函数和请求模型。
from .api_schemas import *  # noqa: F401,F403
from .api_wire import (  # noqa: F401
    _lookup_model_payload, _model_avatar_ready, _model_avatar_url, _model_wire,
    _pipeline_by_model, _reject_model_in_library,
)
from .routes_auth import remote_auth_login, remote_auth_logout, remote_auth_status  # noqa: F401
from .routes_cloud import (  # noqa: F401
    _plugin_status_projection, _require_plugin_loopback, cloud_login_cookie, cloud_qrcode,
    cloud_qrcode_poll, cloud_status, plugin_directory_ready, plugin_magnet_submit,
)
from .routes_discovery import discovery_sources, get_discovery, scrape_discovery  # noqa: F401
from .routes_library import (  # noqa: F401
    bind_library_folder, list_library_models, refresh_library, unbind_library_folder,
)
from .routes_models import (  # noqa: F401
    _decode_model_cursor, _encode_model_cursor, _find_exact_model, _model_lookup_lock,
    _model_progress_key, _select_download_videos, _sync_model_core, add_download,
    create_model_sync_job, delete_model, get_model_sync_job, list_models, lookup_model,
    model_avatar, model_videos, preview_image, scrape_ranking, sync_model,
)
from .routes_ops import (  # noqa: F401
    notify_config, notify_status, notify_test, patrol_config, patrol_run_now, patrol_status,
    patrol_toggle,
)
from .routes_taste import (  # noqa: F401
    _taste_lock, get_taste_liked_models, get_taste_model_state, get_taste_pair,
    record_taste_comparison, record_taste_result_feedback, start_taste_model_run,
    undo_latest_taste_comparison,
)
from .routes_tasks import (  # noqa: F401
    _parse_task_cursor, get_tasks, task_delete, task_retry, tasks_delete_all,
    tasks_delete_batch, tasks_pause_all, tasks_reconcile_downloads, tasks_retry_failed,
    tasks_scrub_errors, tasks_start_all,
)


# ---------- WebSocket 广播 ----------
hub = WSHub()
install_sql_observer(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    library_index.set_broadcast(hub.broadcast)
    taste_model_service.set_broadcast(hub.broadcast)
    await library_index.ensure_legacy_index()

    async def refresh_library_once():
        try:
            await library_index.refresh()
        except Exception:
            # 最近一次成功索引和 cloud_cid 继续生效；启动不能被非官方115接口阻塞。
            return

    library_refresh_task = asyncio.create_task(refresh_library_once())
    await notifier.restore_config()
    manager.set_broadcast(hub.broadcast)
    # 页面保存的并发/限速/时段先生效，restore 后的首次调度即按新上限派发。
    await manager.restore_download_settings()
    await manager.restore()
    model_sync_coordinator.set_broadcast(hub.broadcast)
    patrol.set_broadcast(hub.broadcast)
    await patrol.restore_config()
    if patrol.enabled:
        patrol.start()
    # 每天 9:00 推送过去 24 小时摘要（Bark“每日摘要”开关控制，无动静不发）。
    daily_digest = DailyDigest(
        session_factory=async_session, notify=notifier.notify_daily_digest,
        outage_totals=manager.link_outage_totals)
    daily_digest.start()
    diagnostics.start()
    try:
        yield
    finally:
        await diagnostics.stop()
        await daily_digest.stop()
        if not library_refresh_task.done():
            library_refresh_task.cancel()
        await asyncio.gather(library_refresh_task, return_exceptions=True)
        await shutdown_preview_warmups()
        await patrol.stop()
        await model_sync_coordinator.shutdown()
        await manager.shutdown()
        await taste_model_service.close()
        await notifier.close()
        await avatar_service.close_clients()
        await preview_image_store.close_clients()
        await cloud.close()
        await hub.close()
        # 所有会访问数据库的后台任务都已停止后再释放连接池。
        await engine.dispose()


app = FastAPI(title="PH-115 同步工具", lifespan=lifespan)
# 前端在开发、打包和远程反代下都与 API 同源，不需要开放跨域。
# 大任务列表压缩后通常可缩小约 85%，远程访问时可显著减少隧道上行流量。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


@app.middleware("http")
async def remote_session_guard(request: Request, call_next):
    """外网 API 必须携带应用会话；静态登录页与三个会话端点公开。"""
    path = request.url.path
    if (path.startswith("/api/") and remote_auth.is_remote(request.headers)
            and path not in remote_auth.PUBLIC_API_PATHS):
        expires_at = remote_auth.session_expiry(
            request.cookies.get(remote_auth.COOKIE_NAME))
        if expires_at is None:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "外网登录已过期，请重新登录",
                    "code": "REMOTE_AUTH_REQUIRED",
                },
                headers={"Cache-Control": "no-store"},
            )
    return await call_next(request)


app.add_middleware(DiagnosticsMiddleware)


@app.get("/api/diagnostics")
async def runtime_diagnostics():
    """下载不含 Cookie、路径、媒体标识或日志正文的运行诊断。"""
    build = await asyncio.to_thread(read_build_identity, APP_DIR)
    return JSONResponse({
        "schema_version": 1,
        "build": build,
        **diagnostics.snapshot(),
        "websocket": hub.statistics(),
        "tasks": manager.runtime_statistics(),
    }, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="ph115-diagnostics.json"',
    })


# ---------- 路由 ----------
# 注册顺序与拆分前一致；OpenAPI 契约测试校验路径与模型未变化。
for _router_module in (
        routes_auth, routes_cloud, routes_discovery, routes_taste, routes_library,
        routes_models, routes_tasks, routes_ops):
    app.include_router(_router_module.router)


# ---------- WebSocket ----------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if (remote_auth.is_remote(ws.headers)
            and remote_auth.session_expiry(
                ws.cookies.get(remote_auth.COOKIE_NAME)) is None):
        await ws.close(code=4401, reason="Remote login required")
        return
    if not _websocket_origin_allowed(ws):
        await ws.close(code=1008, reason="Origin not allowed")
        return
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)


def _websocket_origin_allowed(ws: WebSocket) -> bool:
    """浏览器 WS 仅接受同源；本机 Vite 代理允许回环 Origin。"""
    origin = ws.headers.get("origin", "")
    client_host = (ws.client.host if ws.client else "").strip("[]").lower()
    loopbacks = {"127.0.0.1", "localhost", "::1"}
    if not origin:
        return client_host in loopbacks
    try:
        parsed = urlparse(origin)
        origin_host = (parsed.hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not origin_host:
        return False
    if origin_host in loopbacks and client_host in loopbacks:
        return True
    return parsed.netloc.lower() == ws.headers.get("host", "").lower()


# ---------- 前端静态文件（必须在所有 /api、/ws 路由注册之后挂载，否则根挂载会吞掉它们）----------
# 前端是 hash router + 相对 /api + location.host 连 WS，所以同端口伺服 dist 即可，
# 不需要 SPA history fallback。开发模式下若无 dist 也无妨（用 vite dev server）。


def _frontend_dir() -> _Path | None:
    if getattr(_sys, "frozen", False):
        cand = _Path(_sys._MEIPASS) / "frontend_dist"   # 打包：随包内置
    else:
        cand = _Path(__file__).resolve().parent.parent / "frontend" / "dist"
    return cand if cand.exists() else None


_fe_dir = _frontend_dir()
if _fe_dir:
    app.mount("/", StaticFiles(directory=str(_fe_dir), html=True), name="frontend")
