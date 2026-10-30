"""存量模特头像资产化命令：可中断、可重复执行、按批提交。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.exc import OperationalError

from . import avatar_service, avatar_store, scraper
from .config import DATA_DIR, settings
from .database import (
    Model, _normalize_model_name_key, async_session, engine, init_db,
)


def _model_input(model: Model) -> dict:
    return {
        "_model_id": model.id,
        # 网络准备可能持续数秒；提交时必须确认这仍是当初选中的同一版记录，
        # 否则较慢的旧修复会覆盖期间完成的规范主页修复。
        "_expected_ph_id": model.ph_id,
        "_expected_name": model.name,
        "_expected_url": model.url,
        "_expected_avatar_sha256": str(model.avatar_sha256 or ""),
        "_expected_avatar_status": str(model.avatar_status or ""),
        "ph_id": model.ph_id,
        "name": model.name,
        "url": model.url,
        "thumbnail": model.thumbnail,
        "rank": model.rank,
        "scrape_order": model.scrape_order,
        "ph_video_count": model.ph_video_count,
        "views_text": model.views_text,
    }


def _needs_repair(model: Model) -> bool:
    return avatar_store.avatar_path(model.avatar_sha256) is None


def _failure_log_path() -> Path:
    return DATA_DIR / "avatar-repair-failures.jsonl"


def _append_failure_checkpoint(failures: list[dict]) -> None:
    """失败明细只追加本批，避免每个 checkpoint 重写全部历史失败。"""
    if not failures:
        return
    path = _failure_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for failure in failures:
            stream.write(json.dumps(failure, ensure_ascii=False, default=str))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_report(report: dict, *, compact_checkpoint: bool = False) -> Path:
    path = DATA_DIR / "avatar-repair-report.json"
    temporary = path.with_suffix(".json.tmp")
    payload = report
    if compact_checkpoint and report.get("failed"):
        # 长修复任务中失败可能持续累积。checkpoint 只保存计数和少量尾部样例，
        # 完整明细按批写入 JSONL；最终报告仍一次性写入完整 failed 列表。
        payload = {
            **report,
            "failed_count": len(report["failed"]),
            "failed_tail": report["failed"][-20:],
            "failure_log_path": str(_failure_log_path()),
        }
        payload.pop("failed", None)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


async def _commit_prepared(prepared: list[dict]) -> tuple[int, list[dict], int]:
    """原子提交一批头像；运行中后端短暂持锁时有限退避重试。"""
    delay = 0.25
    lock_retries = 0
    for attempt in range(8):
        identity_failures: list[dict] = []
        try:
            async with async_session() as session:
                target_ids = sorted({int(item["_model_id"]) for item in prepared})
                current_rows = []
                for offset in range(0, len(target_ids), 900):
                    current_rows.extend((await session.execute(select(
                        Model.id,
                        Model.ph_id,
                        Model.name,
                        Model.url,
                        Model.avatar_sha256,
                        Model.avatar_status,
                    ).where(Model.id.in_(target_ids[offset:offset + 900])))).all())
                current_identity = {
                    int(row.id): (
                        str(row.ph_id), str(row.name), str(row.url),
                        str(row.avatar_sha256 or ""),
                        str(row.avatar_status or ""),
                    )
                    for row in current_rows
                }
                valid: list[dict] = []
                for item in prepared:
                    state = current_identity.get(int(item["_model_id"]))
                    expected = (
                        str(item.get("_expected_ph_id", item["ph_id"])),
                        str(item.get("_expected_name", state[1] if state else "")),
                        str(item.get("_expected_url", state[2] if state else "")),
                        str(item.get(
                            "_expected_avatar_sha256", state[3] if state else "")),
                        str(item.get(
                            "_expected_avatar_status", state[4] if state else "")),
                    )
                    if state != expected:
                        identity_failures.append({
                            "index": -1,
                            "ph_id": item["ph_id"],
                            "name": item["name"],
                            "url": item["url"],
                            "error": "数据库中的模特身份已变化，拒绝更新头像",
                        })
                    else:
                        valid.append(item)
                if valid:
                    table = Model.__table__
                    statement = update(table).where(
                        table.c.id == bindparam("_target_id"),
                        table.c.ph_id == bindparam("_expected_ph_id"),
                        table.c.name == bindparam("_expected_name"),
                        table.c.url == bindparam("_expected_url"),
                        func.coalesce(table.c.avatar_sha256, "")
                        == bindparam("_expected_avatar_sha256"),
                        func.coalesce(table.c.avatar_status, "")
                        == bindparam("_expected_avatar_status"),
                    ).values(
                        name=bindparam("_name"),
                        library_name_key=bindparam("_library_name_key"),
                        url=bindparam("_url"),
                        avatar_source_url=bindparam("_avatar_source_url"),
                        avatar_sha256=bindparam("_avatar_sha256"),
                        avatar_updated_at=bindparam("_avatar_updated_at"),
                        avatar_status="ready",
                        avatar_error="",
                        avatar_checked_at=bindparam("_avatar_checked_at"),
                    )
                    await session.execute(statement, [{
                        "_target_id": int(item["_model_id"]),
                        "_expected_ph_id": str(item.get(
                            "_expected_ph_id", item["ph_id"])),
                        "_expected_name": str(item.get(
                            "_expected_name", current_identity[
                                int(item["_model_id"])][1])),
                        "_expected_url": str(item.get(
                            "_expected_url", current_identity[
                                int(item["_model_id"])][2])),
                        "_expected_avatar_sha256": str(item.get(
                            "_expected_avatar_sha256", current_identity[
                                int(item["_model_id"])][3])),
                        "_expected_avatar_status": str(item.get(
                            "_expected_avatar_status", current_identity[
                                int(item["_model_id"])][4])),
                        "_name": item["name"],
                        "_library_name_key": _normalize_model_name_key(item["name"]),
                        "_url": item["url"],
                        "_avatar_source_url": item["avatar_source_url"],
                        "_avatar_sha256": item["avatar_sha256"],
                        "_avatar_updated_at": item["avatar_updated_at"],
                        "_avatar_checked_at": item["avatar_checked_at"],
                    } for item in valid])

                # executemany 的 rowcount 在不同 SQLite 驱动上可能为 -1。一次性
                # 回读身份+hash 既能得到精确提交数，也能识别预取后发生的身份竞争。
                verified_rows = []
                valid_ids = sorted({int(item["_model_id"]) for item in valid})
                for offset in range(0, len(valid_ids), 900):
                    verified_rows.extend((await session.execute(select(
                        Model.id, Model.ph_id, Model.name, Model.url,
                        Model.library_name_key, Model.avatar_sha256,
                        Model.avatar_status,
                    ).where(Model.id.in_(valid_ids[offset:offset + 900])))).all())
                verified = {
                    int(row.id): (
                        str(row.ph_id), str(row.name), str(row.url),
                        str(row.library_name_key or ""),
                        str(row.avatar_sha256 or ""), str(row.avatar_status or ""),
                    )
                    for row in verified_rows
                }
                committed = 0
                for item in valid:
                    state = verified.get(int(item["_model_id"]))
                    if state == (
                        str(item.get("_expected_ph_id", item["ph_id"])),
                        item["name"], item["url"],
                        _normalize_model_name_key(item["name"]),
                        item["avatar_sha256"], "ready",
                    ):
                        committed += 1
                    else:
                        identity_failures.append({
                            "index": -1,
                            "ph_id": item["ph_id"],
                            "name": item["name"],
                            "url": item["url"],
                            "error": "头像批量提交期间身份发生变化，拒绝确认更新",
                        })
                await session.commit()
            return committed, identity_failures, lock_retries
        except OperationalError as exc:
            if "database is locked" not in str(exc).casefold() or attempt >= 7:
                raise
            lock_retries += 1
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, 4.0)
    raise RuntimeError("头像批次数据库提交重试意外结束")


async def repair_avatars(*, limit: int | None = None, batch_size: int = 20,
                         concurrency: int | None = None,
                         force_profile: bool = False) -> dict:
    await init_db()
    async with async_session() as session:
        models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
    pending = [_model_input(model) for model in models if _needs_repair(model)]
    if limit is not None:
        pending = pending[:max(0, limit)]

    report = {
        "started_at": datetime.now().isoformat(),
        "avatar_dir": str(Path(settings.avatar_dir).resolve()),
        "selected": len(pending),
        "force_profile": force_profile,
        "completed": 0,
        "failed": [],
        "batches": 0,
    }
    failure_log = _failure_log_path()
    failure_log.parent.mkdir(parents=True, exist_ok=True)
    # 每次命令是一个新的可审计 run。上次若在最终报告落盘前中断，其 JSONL
    # 是唯一完整失败明细，不能直接 unlink；先按时间戳归档再开启新文件。
    if failure_log.exists():
        archived = failure_log.with_name(
            f"{failure_log.stem}.{datetime.now():%Y%m%d-%H%M%S-%f}{failure_log.suffix}"
        )
        failure_log.replace(archived)
        report["previous_failure_log"] = str(archived)
    _write_report(report)

    batch_size = max(1, int(batch_size))
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        prepared, failures = await avatar_service.prepare_models(
            batch, concurrency=concurrency, force_profile=force_profile)
        committed, identity_failures, lock_retries = await _commit_prepared(prepared)
        failures.extend(identity_failures)
        report["completed"] += committed
        report["database_lock_retries"] = (
            report.get("database_lock_retries", 0) + lock_retries)
        report["failed"].extend(failures)
        _append_failure_checkpoint(failures)
        report["batches"] += 1
        report["updated_at"] = datetime.now().isoformat()
        _write_report(report, compact_checkpoint=True)
        print(json.dumps({
            "batch": report["batches"],
            "processed": min(offset + len(batch), len(pending)),
            "selected": len(pending),
            "completed": report["completed"],
            "failed": len(report["failed"]),
        }, ensure_ascii=False), flush=True)

    async with async_session() as session:
        current = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
    report["remaining_without_local_avatar"] = sum(
        1 for model in current if _needs_repair(model))
    report["finished_at"] = datetime.now().isoformat()
    report_path = _write_report(report)
    report["report_path"] = str(report_path)
    return report


async def repair_canonical_avatar(model_id: int, canonical_url: str) -> dict:
    """人工确认旧账号跳转后，用新权威主页修复头像，但保留历史 ph_id。"""
    await init_db()
    async with async_session() as session:
        model = await session.get(Model, int(model_id))
        if model is None:
            raise ValueError(f"模特不存在：{model_id}")
        database_ph_id = model.ph_id

    async with scraper.create_page_client() as profile_client:
        profile = await scraper.fetch_model_profile_url(
            canonical_url, client=profile_client)
    async with avatar_store.create_client() as image_client:
        asset = await avatar_store.cache_avatar(
            profile["thumbnail"], client=image_client, referer=profile["url"])

    checked_at = datetime.now()
    prepared = [{
        "_model_id": int(model_id),
        # ph_id 是数据库身份边界。已确认的规范主页只更新展示名、URL 和头像，
        # 不改历史身份，避免与已有视频/任务关联脱节。
        "ph_id": database_ph_id,
        "_expected_ph_id": database_ph_id,
        "_expected_name": model.name,
        "_expected_url": model.url,
        "_expected_avatar_sha256": str(model.avatar_sha256 or ""),
        "_expected_avatar_status": str(model.avatar_status or ""),
        "name": profile["name"],
        "url": profile["url"],
        "avatar_source_url": asset.source_url,
        "avatar_sha256": asset.sha256,
        "avatar_updated_at": checked_at,
        "avatar_checked_at": checked_at,
    }]
    committed, failures, lock_retries = await _commit_prepared(prepared)
    if committed != 1 or failures:
        raise RuntimeError(f"规范主页头像提交失败：{failures}")
    return {
        "model_id": int(model_id),
        "database_ph_id": database_ph_id,
        "canonical_ph_id": profile["ph_id"],
        "name": profile["name"],
        "url": profile["url"],
        "avatar_sha256": asset.sha256,
        "database_lock_retries": lock_retries,
    }


def _parse_args():
    parser = argparse.ArgumentParser(description="回填并本地化模特头像")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条；默认全部")
    parser.add_argument("--batch-size", type=int, default=20, help="每批数据库提交条数")
    parser.add_argument("--concurrency", type=int, default=None, help="头像抓取并发")
    parser.add_argument(
        "--force-profile", action="store_true",
        help="忽略旧缩略图地址，全部从个人主页刷新头像来源",
    )
    parser.add_argument("--canonical-model-id", type=int, default=None,
                        help="修复已人工确认发生账号跳转的模特 ID")
    parser.add_argument("--canonical-url", default=None,
                        help="与 --canonical-model-id 配套的规范个人主页 URL")
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    try:
        if (args.canonical_model_id is None) != (args.canonical_url is None):
            raise ValueError("--canonical-model-id 与 --canonical-url 必须同时提供")
        if args.canonical_model_id is not None:
            result = await repair_canonical_avatar(
                args.canonical_model_id, args.canonical_url)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
        report = await repair_avatars(
            limit=args.limit,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            force_profile=args.force_profile,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if report["remaining_without_local_avatar"] == 0 else 2
    finally:
        try:
            await avatar_service.close_clients()
        finally:
            await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
