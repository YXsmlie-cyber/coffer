"""全库污染扫描（离线诊断，只读不删）。

仅把“同一 model_id 下、状态为 completed 的上传任务”视为工具曾确认上传的记录；
skipped 只表示云端原本已存在，绝不能作为工具创建文件的证据。

报告写入 data/private/pollution_scan.v2.json。v2 使用稳定 model_id 和当时的
cloud_cid，供清理脚本做双重身份核对；旧版按 model_name 关联的报告不再接受。
"""
import asyncio
from contextlib import closing
import json
import os
import sqlite3
import sys
from pathlib import Path

from backend.scraper import scrape_model_videos

DB = Path("data/data.db")
OUT = Path("data/private/pollution_scan.v2.json")
SCHEMA_VERSION = 2
CHECKPOINT_EVERY = 10
JOURNAL = OUT.with_suffix(OUT.suffix + ".journal.jsonl")


def _console_print(message: object = "", *, flush: bool = False) -> None:
    """在任意 Windows 控制台编码下安全输出诊断信息。

    GitHub Windows runner 可能把 stdout 暴露为 cp1252；模特名和中文提示一旦
    含有该编码无法表示的字符，普通 ``print`` 会让只读扫描本身崩溃。这里在
    写入前按实际控制台编码做 backslashreplace，UTF-8/GBK 控制台仍保留原文，
    其他控制台也至少能看到可逆的 Unicode 转义。
    """
    stream = sys.stdout
    text = f"{message}\n"
    encoding = getattr(stream, "encoding", None)
    if encoding:
        try:
            text = text.encode(encoding, errors="backslashreplace").decode(encoding)
        except LookupError:
            pass
    stream.write(text)
    if flush:
        stream.flush()


def load_progress() -> dict:
    if OUT.exists():
        with OUT.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"schema_version": SCHEMA_VERSION, "models": {}}
    if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("models"), dict):
        raise RuntimeError(f"拒绝续用未知格式的扫描报告: {OUT}")
    # 中断恢复只重放追加式小日志；损坏的最后半行最多导致该模特重扫，绝不
    # 把不完整记录交给后续删除计划。
    if JOURNAL.exists():
        with JOURNAL.open(encoding="utf-8") as stream:
            for raw_line in stream:
                try:
                    checkpoint = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if (checkpoint.get("schema_version") != SCHEMA_VERSION
                        or not isinstance(checkpoint.get("result"), dict)):
                    continue
                item = checkpoint["result"]
                try:
                    model_id = str(int(item["model_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
                data["models"][model_id] = item
    return data


def save_progress(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(OUT.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, OUT)


def append_checkpoint(items: list[dict]) -> None:
    """追加本批结果，写放大与已扫描总量无关。"""
    if not items:
        return
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as stream:
        for item in items:
            stream.write(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "result": item,
            }, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def require_task_identity_column(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
    if "model_id" not in columns:
        raise RuntimeError("数据库尚未迁移 model_id；请先启动一次新版应用，再运行扫描")


async def main():
    # sqlite3 连接只服务于启动时的只读快照，进入漫长网络扫描前立即关闭，
    # 避免离线工具数小时持有数据库文件句柄。
    # sqlite3.Connection 自身的 with 只管理事务，不关闭 Windows 文件句柄。
    database_uri = DB.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        # 两次 SELECT 必须来自同一瞬时快照，避免在线后端恰好在中间改动任务；
        # 读事务只持续到内存快照完成，网络扫描开始前即随 close 释放。
        conn.execute("BEGIN")
        require_task_identity_column(conn)
        models = list(conn.execute(
            "select id,name,url,coalesce(cloud_cid,'') from models order by id"
        ))
        uploaded: dict[int, set[str]] = {}
        for model_id, viewkey in conn.execute(
            "select model_id,viewkey from tasks "
            "where task_type='upload' and status='completed' and model_id is not null"
        ):
            uploaded.setdefault(int(model_id), set()).add(str(viewkey).lower())

    report = load_progress()
    result = report["models"]
    total = len(models)
    pending_checkpoint: list[dict] = []
    try:
        for i, (model_id, name, url, cloud_cid) in enumerate(models, 1):
            uploaded_keys = uploaded.get(int(model_id), set())
            if not uploaded_keys:
                continue
            if str(model_id) in result:
                _console_print(f"[{i}/{total}] skip {name}（已扫描）")
                continue
            _console_print(
                f"[{i}/{total}] scanning {name} ... 已完成上传 {len(uploaded_keys)} 个",
                flush=True,
            )
            try:
                videos = await scrape_model_videos(url, max_videos=5000)
            except Exception as exc:
                _console_print(f"    ERROR {name}: {exc}", flush=True)
                continue
            whitelist = {str(video["viewkey"]).lower() for video in videos}
            polluted = sorted(uploaded_keys - whitelist)
            clean = sorted(uploaded_keys & whitelist)
            item = {
                "model_id": int(model_id),
                "name": name,
                "url": url,
                "cloud_cid": str(cloud_cid),
                "own_uploaded_total": len(whitelist),
                "completed_upload_count": len(uploaded_keys),
                "clean_count": len(clean),
                "polluted_count": len(polluted),
                "polluted_viewkeys": polluted,
            }
            result[str(model_id)] = item
            pending_checkpoint.append(item)
            # 全量 JSON 只在正常结束时原子压实；运行中按小批追加 journal，
            # 把逐步保存从 O(n²) 写放大降为 O(n)。
            if len(pending_checkpoint) >= CHECKPOINT_EVERY:
                append_checkpoint(pending_checkpoint)
                pending_checkpoint.clear()
            _console_print(
                f"    -> 本人上传库 {len(whitelist)}，完成记录中干净 {len(clean)}，"
                f"候选污染 {len(polluted)}",
                flush=True,
            )
    finally:
        if pending_checkpoint:
            append_checkpoint(pending_checkpoint)

    # 只有完整扫描自然结束才发布供 clean_pollution 使用的全量报告。若进程
    # 中断，journal 保留并由下一次扫描重放。
    save_progress(report)
    JOURNAL.unlink(missing_ok=True)

    polluted_total = sum(item["polluted_count"] for item in result.values())
    uploaded_total = sum(item["completed_upload_count"] for item in result.values())
    _console_print("\n===== 汇总 =====", flush=True)
    _console_print(f"已扫描模特: {len(result)}", flush=True)
    _console_print(f"完成上传记录: {uploaded_total}", flush=True)
    _console_print(
        f"候选污染: {polluted_total} "
        f"({polluted_total * 100 // max(uploaded_total, 1)}%)",
        flush=True,
    )
    _console_print(f"报告: {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
