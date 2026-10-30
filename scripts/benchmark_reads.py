"""隔离 SQLite 任务读取基准；可与指定 Git 基线比较，不访问运行库或网络。"""
import argparse
import ast
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
import tracemalloc
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from backend.database import Base, Task
from backend.task_manager import _failure_metadata, _wire_value
from backend.task_queries import TaskQueries


def legacy_queries(ref, session):
    source = subprocess.check_output(["git", "show", f"{ref}:backend/task_manager.py"],
                                     text=True, encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskManager")
    methods = [ast.get_source_segment(source, node) for node in owner.body
               if isinstance(node, ast.AsyncFunctionDef) and node.name in {"list_tasks", "task_counts"}]
    namespace = {"asyncio": asyncio, "Task": Task, "select": select, "func": func,
                 "async_session": session, "SimpleNamespace": SimpleNamespace,
                 "_failure_metadata": _failure_metadata, "_wire_value": _wire_value}
    # 只执行本仓库选定基线的两个只读方法；外部依赖绑定为临时会话。
    body = "class LegacyQueries:\n" + "\n".join(textwrap.indent(
        textwrap.dedent(method), "    ") for method in methods)
    exec(compile(body, "<baseline-read-methods>", "exec"), namespace)
    return namespace["LegacyQueries"]()


async def measured(action, runs):
    await action()
    durations = []
    for _ in range(runs):
        started = time.perf_counter()
        result = await action()
        durations.append((time.perf_counter() - started) * 1000)
    tracemalloc.start()
    await action()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {"median_ms": round(statistics.median(durations), 3),
                    "max_ms": round(max(durations), 3), "peak_python_kib": round(peak / 1024, 1)}


async def benchmark(args):
    results = []
    for size in args.sizes:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine(f"sqlite+aiosqlite:///{Path(directory).as_posix()}/bench.db")
            session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    for start in range(0, size, 2000):
                        await connection.execute(insert(Task), [{
                            "viewkey": f"fixture-{index}", "title": "Example", "model_name": "Example",
                            "url": "https://example.invalid/" + "x" * 2048,
                            "m3u8_url": "https://example.invalid/" + "y" * 2048,
                            "task_type": "download", "status": "failed" if index % 5 == 0 else "completed",
                            "error": "ERROR: unable to download video data: HTTP Error 503" if index % 10 == 0 else "timeline mismatch",
                        } for index in range(start, min(size, start + 2000))])
                services = {"current": TaskQueries(session, _failure_metadata)}
                if args.baseline_ref:
                    services["baseline"] = legacy_queries(args.baseline_ref, session)
                outputs = {}
                row = {"tasks": size, "failed": size // 5, "runs": args.runs}
                for name, service in services.items():
                    page, page_stats = await measured(lambda: service.list_tasks("download", limit=200), args.runs)
                    counts, count_stats = await measured(lambda: service.task_counts("download"), args.runs)
                    row[name] = {"first_page": page_stats, "counts": count_stats}
                    outputs[name] = (page, counts)
                if len(outputs) == 2 and outputs["current"] != outputs["baseline"]:
                    raise AssertionError("读取结果与基线不一致")
                row["equivalent"] = len(outputs) == 2
                results.append(row)
                print(json.dumps(row), flush=True)
            finally:
                await engine.dispose()
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1 or any(size < 1 for size in args.sizes):
        parser.error("数据规模和重复次数必须为正数")
    asyncio.run(benchmark(args))
