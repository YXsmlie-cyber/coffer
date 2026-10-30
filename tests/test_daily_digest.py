"""每日摘要：过去 24 小时的完成/跳过、当前排队失败与断流差值；无动静不发送。"""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.daily_digest import DailyDigest, format_bytes, seconds_until
from backend.database import Base, Task


class DigestScheduleTests(unittest.TestCase):
    def test_next_run_is_today_or_tomorrow_at_hour(self):
        self.assertEqual(seconds_until(datetime(2026, 10, 19, 8, 30), 9), 1800)
        self.assertEqual(seconds_until(datetime(2026, 10, 19, 9, 0), 9), 86400)
        self.assertEqual(seconds_until(datetime(2026, 10, 19, 23, 0), 9), 10 * 3600)

    def test_format_bytes(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(int(8.4 * 1024 ** 3)), "8.4 GB")


class DailyDigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/digest.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.now = datetime(2026, 10, 20, 9, 0)
        self.outages = [(2, 120.0)]

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def seed(self, rows):
        async with self.session() as session:
            session.add_all([Task(viewkey=f"v{i}", title="T", model_name="M", url="u",
                                  task_type=kind, status=status, total_bytes=size,
                                  updated_at=self.now - age)
                             for i, (kind, status, size, age) in enumerate(rows)])
            await session.commit()

    def digest(self, notify=None):
        return DailyDigest(session_factory=self.session, notify=notify or AsyncMock(),
                           outage_totals=lambda: self.outages[-1], clock=lambda: self.now)

    async def test_summary_counts_last_24_hours_and_outage_delta(self):
        hour = timedelta(hours=1)
        await self.seed([
            ("download", "completed", 3 * 1024 ** 3, 3 * hour),
            ("download", "completed", 1024 ** 3, 10 * hour),
            ("download", "completed", 9 * 1024 ** 3, 30 * hour),     # 超出 24 小时
            ("upload", "completed", 0, 2 * hour),
            ("upload", "skipped", 0, 2 * hour),
            ("download", "queued", 0, 1 * hour),
            ("download", "failed", 0, 1 * hour),
        ])
        notify = AsyncMock()
        digest = self.digest(notify)
        self.outages.append((8, 420.0))                             # 期间新增 6 次、300 秒
        self.assertTrue(await digest.send_once())
        title, body = notify.await_args.args
        self.assertEqual(title, "PH-115 每日摘要 · 下载 2 / 上传 1")
        self.assertIn("下载完成 2 个（4.0 GB），上传完成 1 个，跳过 1 个", body)
        self.assertIn("当前：排队 1 个，失败 1 个", body)
        self.assertIn("网络断流 6 次，共约 5 分钟", body)

    async def test_quiet_day_sends_nothing_and_baseline_advances(self):
        notify = AsyncMock()
        digest = self.digest(notify)
        self.assertFalse(await digest.send_once())
        notify.assert_not_awaited()
        self.outages.append((3, 150.0))
        content = await digest.build(self.now)
        self.assertIn("网络断流 1 次", content[1])
        self.assertIsNone(await digest.build(self.now), "同一段断流不重复计入下一次摘要")
