"""在当前本地数据库上执行审美视觉留组验证，不生成候选排行。"""
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
        state = await taste_model_service.validate_now()
        run = state.get("run") or {}
        print(json.dumps({
            "status": run.get("status"),
            "run_id": run.get("id"),
            "quality_passed": run.get("quality_passed"),
            "metrics": run.get("metrics"),
        }, ensure_ascii=False, indent=2), flush=True)
    finally:
        await taste_model_service.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
