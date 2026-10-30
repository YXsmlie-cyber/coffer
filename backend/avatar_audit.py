"""审计仍无本地头像的模特主页；仅显式传参时写入审计状态。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from sqlalchemy import bindparam, func, select, update
from sqlalchemy.exc import OperationalError

from . import avatar_store, scraper
from .config import DATA_DIR
from .database import Model, async_session, engine, init_db


def _page_evidence(response, model: Model) -> dict:
    final_url = str(getattr(response, "url", model.url))
    html = str(getattr(response, "text", "") or "")
    soup = BeautifulSoup(html, "lxml")
    header = soup.select_one("section.topProfileHeader, .topProfileHeader")
    avatar = (soup.select_one("img#getAvatar") or soup.select_one("img.avatar"))
    og_image = soup.select_one("meta[property='og:image'], meta[name='twitter:image']")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.select_one("h1")
    return {
        "id": model.id,
        "ph_id": model.ph_id,
        "name": model.name,
        "requested_url": model.url,
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "final_url": final_url,
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "title": title[:300],
        "h1": h1.get_text(" ", strip=True)[:300] if h1 else "",
        "has_profile_header": header is not None,
        "avatar_attribute": (
            (avatar.get("data-src") or avatar.get("data-thumb_url")
             or avatar.get("data-image") or avatar.get("src") or "")[:1000]
            if avatar else ""
        ),
        "og_image": (og_image.get("content") or "")[:1000] if og_image else "",
    }


async def audit_missing_avatars(*, concurrency: int = 4) -> dict:
    await init_db()
    async with async_session() as session:
        models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
    pending = [model for model in models if avatar_store.avatar_path(model.avatar_sha256) is None]
    queue: asyncio.Queue[Model] = asyncio.Queue()
    for model in pending:
        queue.put_nowait(model)
    results: list[dict] = []

    async def worker():
        async with scraper.create_page_client() as client:
            while True:
                try:
                    model = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    response = await scraper.fetch_profile_page(client, model.url)
                    if response is None:
                        results.append({
                            "id": model.id, "ph_id": model.ph_id,
                            "name": model.name, "requested_url": model.url,
                            "error": "页面请求未穿透反爬",
                        })
                    else:
                        results.append(_page_evidence(response, model))
                except Exception as exc:
                    results.append({
                        "id": model.id, "ph_id": model.ph_id,
                        "name": model.name, "requested_url": model.url,
                        "error": str(exc) or exc.__class__.__name__,
                    })
                finally:
                    queue.task_done()

    workers = min(len(pending), max(1, int(concurrency))) if pending else 0
    await asyncio.gather(*(worker() for _ in range(workers)))
    results.sort(key=lambda item: item["id"])
    report = {
        "audited_at": datetime.now().isoformat(),
        "count": len(results),
        "results": results,
    }
    path = DATA_DIR / "avatar-audit-report.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    report["report_path"] = str(path)
    return report


def _classify_evidence(item: dict) -> tuple[str, str]:
    """把审计证据转换为可展示的源站事实，不生成任何兜底头像。"""
    if item.get("error"):
        return "fetch_error", f"源站头像审计失败：{item['error']}"
    final_path = urlparse(str(item.get("final_url") or "")).path.rstrip("/")
    h1 = str(item.get("h1") or "").strip()
    if final_path == "/pornstars" or h1 == "此页面不可用":
        return "source_unavailable", "源站主页不可用或已删除"
    if item.get("has_profile_header") and not item.get("avatar_attribute"):
        return "source_no_avatar", "源站主页未提供头像"
    if item.get("has_profile_header") and item.get("avatar_attribute"):
        # 本命令只审计修复流程结束后仍缺少本地资产的记录；此时有源地址但
        # 无法通过下载/解码校验，属于源头像损坏，不可伪装成可用头像。
        return "source_broken", "源站头像地址不可用"
    if int(item.get("status_code") or 0) == 200:
        # 源站的“此页面不可用”文案会因响应编码偶发乱码；缺少个人主页头部且
        # 没有头像字段，已经足以证明返回的不是可用个人主页。
        return "source_unavailable", "源站主页不可用或已删除"
    return "fetch_error", "源站页面结构无法确认头像状态"


async def apply_audit_statuses(report: dict) -> dict:
    """按 model id + ph_id 原子写入审计状态，并跳过已被并发修复的记录。"""
    classified = []
    for item in report.get("results", []):
        status, error = _classify_evidence(item)
        classified.append((item, status, error))

    delay = 0.25
    lock_retries = 0
    for attempt in range(8):
        try:
            applied = 0
            skipped_ready = 0
            identity_changed = 0
            async with async_session() as session:
                # 先分块取回全部身份与头像摘要，避免每条审计结果各做一次
                # session.get()。仍在 UPDATE 的 WHERE 中复核 id + ph_id，保留
                # 审计报告与数据库身份之间的并发安全边界。
                model_ids = sorted({
                    int(item["id"])
                    for item, _status, _error in classified
                })
                current: dict[
                    int,
                    tuple[str, str | None, str, datetime | None, datetime | None],
                ] = {}
                for offset in range(0, len(model_ids), 900):
                    rows = (await session.execute(select(
                        Model.id,
                        Model.ph_id,
                        Model.avatar_sha256,
                        Model.avatar_status,
                        Model.avatar_updated_at,
                        Model.avatar_checked_at,
                    ).where(Model.id.in_(model_ids[offset:offset + 900])))).all()
                    current.update({
                        int(row.id): (
                            str(row.ph_id), row.avatar_sha256,
                            str(row.avatar_status or ""),
                            row.avatar_updated_at,
                            row.avatar_checked_at,
                        )
                        for row in rows
                    })
                checked_at = datetime.now()
                pending_updates: list[dict] = []
                for item, status, error in classified:
                    model_id = int(item["id"])
                    identity = current.get(model_id)
                    if identity is None or identity[0] != item.get("ph_id"):
                        identity_changed += 1
                        continue
                    if avatar_store.avatar_path(identity[1]) is not None:
                        skipped_ready += 1
                        continue
                    pending_updates.append({
                        "_target_id": model_id,
                        "_expected_ph_id": identity[0],
                        "_expected_avatar_sha256": str(identity[1] or ""),
                        "_expected_avatar_status": identity[2],
                        "_expected_avatar_updated_at": identity[3],
                        "_expected_avatar_checked_at": identity[4],
                        "_avatar_status": status,
                        "_avatar_error": error,
                        "_avatar_checked_at": checked_at,
                    })
                if pending_updates:
                    table = Model.__table__
                    statement = update(table).where(
                        table.c.id == bindparam("_target_id"),
                        table.c.ph_id == bindparam("_expected_ph_id"),
                        func.coalesce(table.c.avatar_sha256, "")
                        == bindparam("_expected_avatar_sha256"),
                        func.coalesce(table.c.avatar_status, "")
                        == bindparam("_expected_avatar_status"),
                        table.c.avatar_updated_at.is_(
                            bindparam("_expected_avatar_updated_at")),
                        table.c.avatar_checked_at.is_(
                            bindparam("_expected_avatar_checked_at")),
                    ).values(
                        avatar_status=bindparam("_avatar_status"),
                        avatar_error=bindparam("_avatar_error"),
                        avatar_checked_at=bindparam("_avatar_checked_at"),
                    )
                    await session.execute(statement, pending_updates)
                    states = []
                    pending_ids = sorted({
                        int(item["_target_id"]) for item in pending_updates
                    })
                    for offset in range(0, len(pending_ids), 900):
                        states.extend((await session.execute(select(
                            Model.id,
                            Model.ph_id,
                            Model.avatar_sha256,
                            Model.avatar_status,
                            Model.avatar_error,
                        ).where(Model.id.in_(
                            pending_ids[offset:offset + 900]
                        )))).all())
                    by_id = {int(row.id): row for row in states}
                    for item in pending_updates:
                        state = by_id.get(item["_target_id"])
                        if state is None or str(state.ph_id) != item["_expected_ph_id"]:
                            identity_changed += 1
                        elif (state.avatar_status == item["_avatar_status"]
                              and state.avatar_error == item["_avatar_error"]):
                            applied += 1
                        elif avatar_store.avatar_path(state.avatar_sha256) is not None:
                            # 预取后另一个修复任务已写入真实资产；即使内容哈希
                            # 与旧引用相同，也不能让审计状态覆盖 ready。
                            skipped_ready += 1
                        else:
                            identity_changed += 1
                await session.commit()
            return {
                "applied": applied,
                "skipped_ready": skipped_ready,
                "identity_changed": identity_changed,
                "database_lock_retries": lock_retries,
            }
        except OperationalError as exc:
            if "database is locked" not in str(exc).casefold() or attempt >= 7:
                raise
            lock_retries += 1
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, 4.0)
    raise RuntimeError("头像审计状态提交重试意外结束")


def _parse_args():
    parser = argparse.ArgumentParser(description="审计无本地头像的模特主页")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--apply-status", action="store_true",
        help="把审计结论写入 avatar_status；默认保持只读",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    try:
        report = await audit_missing_avatars(concurrency=args.concurrency)
        if args.apply_status:
            report["status_write"] = await apply_audit_statuses(report)
            path = Path(report["report_path"])
            temporary = path.with_suffix(".json.tmp")
            serializable = {key: value for key, value in report.items()
                            if key != "report_path"}
            temporary.write_text(
                json.dumps(serializable, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
