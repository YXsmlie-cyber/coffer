"""清理历史污染视频，采用候选计划 + 人工逐 fid 批准的两阶段流程。

第一阶段（只读）：
  python -m scripts.clean_pollution
会生成 data/private/pollution_delete_plan.json。候选必须同时满足：
  - v2 扫描报告中的 model_id/name/url/cloud_cid 与当前数据库完全一致；
  - 当前任务是该 model_id 下 completed 的上传；
  - 115 文件名精确等于工具的“标题 [viewkey].mp4”命名。

第二阶段（删除）：人工检查计划后，把确实要删的 fid 填入 approved_fids，再运行：
  python -m scripts.clean_pollution --execute
脚本会重新扫描并验证报告摘要和全部候选未变化，只删除明确批准的 fid。skipped
任务、按名字猜测的目录、未批准文件一律不删。
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from backend.cloud115 import cloud
from backend.dedup import extract_viewkeys
from backend.downloader import build_remote_filename

DB = Path("data/data.db")
SCAN = Path("data/private/pollution_scan.v2.json")
PLAN = Path("data/private/pollution_delete_plan.json")
SCAN_VERSION = 2
PLAN_VERSION = 1


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_scan() -> dict:
    with SCAN.open(encoding="utf-8") as f:
        report = json.load(f)
    if report.get("schema_version") != SCAN_VERSION or not isinstance(report.get("models"), dict):
        raise RuntimeError("拒绝使用旧版或损坏的污染报告；请重新运行 python -m scripts.scan_pollution")
    return report


def load_approval() -> dict:
    if not PLAN.exists():
        raise RuntimeError(f"找不到批准计划 {PLAN}；请先执行不带 --execute 的 dry-run")
    with PLAN.open(encoding="utf-8") as f:
        plan = json.load(f)
    if plan.get("schema_version") != PLAN_VERSION:
        raise RuntimeError("删除计划版本不匹配，请重新生成")
    return plan


def save_plan(plan: dict) -> None:
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAN.with_suffix(PLAN.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, PLAN)


async def build_candidates(
    conn: sqlite3.Connection, report: dict
) -> tuple[list[dict], list[str]]:
    candidates: list[dict] = []
    skipped: list[str] = []
    for key, info in report["models"].items():
        model_id = int(key)
        if info.get("model_id") != model_id:
            raise RuntimeError(f"报告 model_id 自相矛盾: {key}")
        current = conn.execute(
            "select name,url,coalesce(cloud_cid,'') from models where id=?", (model_id,)
        ).fetchone()
        expected = (info.get("name"), info.get("url"), str(info.get("cloud_cid") or ""))
        if current is None or tuple(current) != expected or not expected[2]:
            skipped.append(f"{info.get('name', model_id)}：身份或 CID 已变化")
            continue

        polluted = {str(value).lower() for value in info.get("polluted_viewkeys", [])}
        if not polluted:
            continue
        task_names: dict[str, set[str]] = {}
        for viewkey, title in conn.execute(
            "select lower(viewkey),title from tasks where model_id=? "
            "and task_type='upload' and status='completed'", (model_id,)
        ):
            if viewkey in polluted:
                task_names.setdefault(viewkey, set()).add(build_remote_filename(title, viewkey))
        if not task_names:
            continue

        try:
            files = await cloud.list_all_files(expected[2], only_video=True)
        except Exception as exc:
            skipped.append(f"{info['name']}：列目录失败 {exc}")
            continue
        for item in files:
            fid = str(item.get("fid") or "")
            filename = str(item.get("n") or "")
            if not fid:
                continue
            for viewkey in sorted(extract_viewkeys(filename) & polluted):
                # 文件名必须是该 completed 任务会生成的精确远端名；仅 viewkey 命中不够。
                if filename not in task_names.get(viewkey, set()):
                    continue
                candidates.append({
                    "model_id": model_id,
                    "model_name": info["name"],
                    "cloud_cid": expected[2],
                    "fid": fid,
                    "filename": filename,
                    "viewkey": viewkey,
                })
    candidates.sort(key=lambda item: (
        item["model_id"], item["viewkey"], item["fid"], item["filename"]
    ))
    return candidates, skipped


async def main():
    execute = "--execute" in sys.argv
    report = load_scan()
    conn = sqlite3.connect(DB)
    columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
    if "model_id" not in columns:
        raise RuntimeError("数据库缺少 tasks.model_id，请先启动一次新版应用")

    candidates, skipped = await build_candidates(conn, report)
    report_digest = canonical_digest(report)
    candidate_digest = canonical_digest(candidates)

    if not execute:
        plan = {
            "schema_version": PLAN_VERSION,
            "scan_digest": report_digest,
            "candidate_digest": candidate_digest,
            "approved_fids": [],
            "candidates": candidates,
        }
        save_plan(plan)
        print("===== 污染清理 DRY-RUN =====")
        print(f"候选文件: {len(candidates)}")
        for item in candidates[:20]:
            print(f"  fid={item['fid']} model={item['model_name']} "
                  f"vk={item['viewkey']} name={item['filename'][:70]}")
        if len(candidates) > 20:
            print(f"  ... 另有 {len(candidates) - 20} 个，详见计划文件")
        print(f"\n计划已写入 {PLAN}")
        print("人工核对后，把要删除的 fid 填入 approved_fids，再加 --execute。")
        if skipped:
            print(f"跳过: {skipped}")
        return

    approval = load_approval()
    if approval.get("scan_digest") != report_digest:
        raise RuntimeError("扫描报告已变化，原批准失效；请重新 dry-run")
    if approval.get("candidate_digest") != candidate_digest:
        raise RuntimeError("115 或数据库候选已变化，原批准失效；请重新 dry-run")
    if approval.get("candidates") != candidates:
        raise RuntimeError("计划候选内容被修改，拒绝执行")
    approved_fids = {str(fid) for fid in approval.get("approved_fids", [])}
    known_fids = {item["fid"] for item in candidates}
    if not approved_fids:
        raise RuntimeError("approved_fids 为空；未批准任何文件，拒绝删除")
    unknown = approved_fids - known_fids
    if unknown:
        raise RuntimeError(f"approved_fids 含非候选 fid: {sorted(unknown)}")

    approved = [item for item in candidates if item["fid"] in approved_fids]
    by_model: dict[tuple[int, str], list[dict]] = {}
    for item in approved:
        by_model.setdefault((item["model_id"], item["cloud_cid"]), []).append(item)

    deleted_files = deleted_tasks = deleted_videos = 0
    for (model_id, _cid), items in by_model.items():
        # cloud.delete_files 本身串行调用；同一模型一次批量提交。
        await cloud.delete_files([item["fid"] for item in items])
        deleted_files += len(items)
        viewkeys = sorted({item["viewkey"] for item in items})
        for viewkey in viewkeys:
            cur = conn.execute(
                "delete from tasks where model_id=? and lower(viewkey)=?",
                (model_id, viewkey),
            )
            deleted_tasks += cur.rowcount
            cur = conn.execute(
                "delete from videos where model_id=? and lower(viewkey)=?",
                (model_id, viewkey),
            )
            deleted_videos += cur.rowcount
        conn.commit()

    print("===== 污染清理完成 =====")
    print(f"已批准并删除 115 文件: {deleted_files}")
    print(f"已清 tasks: {deleted_tasks}，videos: {deleted_videos}")
    if skipped:
        print(f"跳过: {skipped}")


if __name__ == "__main__":
    asyncio.run(main())
