"""自动巡查与 Bark 通知接口。"""
from fastapi import APIRouter, HTTPException, Response

from .patrol import patrol
from .notifier import notifier
from .api_schemas import PatrolToggle, PatrolConfig, NotifyConfig


router = APIRouter()


@router.get("/api/patrol/status")
async def patrol_status():
    return patrol.status()


@router.post("/api/patrol/toggle")
async def patrol_toggle(body: PatrolToggle):
    await patrol.set_enabled(body.enabled)
    return patrol.status()


@router.post("/api/patrol/config")
async def patrol_config(body: PatrolConfig):
    await patrol.set_interval(body.interval_hours)
    return patrol.status()


@router.post("/api/patrol/run-now")
async def patrol_run_now():
    """手动触发一轮巡查（异步执行，立即返回当前状态）。"""
    await patrol.trigger_now()
    return patrol.status()


# ---------- Bark 通知 ----------
@router.get("/api/notify/status")
async def notify_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return notifier.status()


@router.post("/api/notify/config")
async def notify_config(body: NotifyConfig, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        await notifier.set_config(
            enabled=body.enabled, bark_url=body.bark_url, events=body.events)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return notifier.status()


@router.post("/api/notify/test")
async def notify_test(response: Response):
    """立即推一条测试消息，便于用户验证 Bark URL 是否正确。"""
    response.headers["Cache-Control"] = "no-store"
    if not notifier.bark_url.strip():
        raise HTTPException(400, "尚未配置 Bark 推送地址")
    ok = await notifier.push_test()
    if not ok:
        raise HTTPException(502, "测试推送失败，请检查 Bark 地址是否正确、网络是否可达")
    return {"ok": True}
