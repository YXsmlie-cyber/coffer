"""在当前本地数据库上生成并发布“探索”页首批视觉结果。"""
from __future__ import annotations

import asyncio
import json

from backend.database import engine, init_db
from backend.taste_model import taste_model_service


async def main():
    await init_db()

    async def report(message: dict):
        print(json.dumps(message, ensure_ascii=False), flush=True)

    taste_model_service.set_broadcast(report)
    try:
        state = await taste_model_service.run_now()
        run = state.get("run") or {}
        print(json.dumps({
            "status": run.get("status"),
            "run_id": run.get("id"),
            "quality_passed": run.get("quality_passed"),
            "eligible_count": run.get("eligible_count"),
            "scored_count": run.get("scored_count"),
            "excluded_count": run.get("excluded_count"),
            "metrics": run.get("metrics"),
            "result_count": len(state.get("results") or []),
            "error": run.get("error"),
        }, ensure_ascii=False, indent=2), flush=True)
    finally:
        await taste_model_service.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
