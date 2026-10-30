"""下载运行参数：校验、时段计算、持久化与运行中调整并发上限。"""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings
from backend.database import Base, RuntimeSetting
from backend.download_settings import DownloadSettings
from backend.downloader import MIN_FRAGMENT_RATE_LIMIT, fragment_rate_limit
from backend.network_health import LinkHealth
from backend.resizable_limiter import ResizableLimiter
from backend.task_manager import TaskManager


def values(**overrides):
    base = dict(max_concurrent_downloads=5, max_connections=50, rate_limit_mbps=0.0,
                window_start="", window_end="", keep_awake=True)
    base.update(overrides)
    return DownloadSettings(**base)


class DownloadSettingsRuleTests(unittest.TestCase):
    def test_validation_messages(self):
        for overrides, message in (
            ({"max_concurrent_downloads": 0}, "同时下载数"),
            ({"max_connections": 1}, "总连接数"),
            ({"rate_limit_mbps": -1}, "限速"),
            ({"window_start": "22:00"}, "同时填写"),
            ({"window_start": "25:00", "window_end": "08:00"}, "HH:MM"),
            ({"window_start": "08:00", "window_end": "08:00"}, "不能相同"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    values(**overrides).validated()
        cleaned = values(window_start=" 22:00 ", window_end="08:00", rate_limit_mbps=2.345).validated()
        self.assertEqual((cleaned.window_start, cleaned.rate_limit_mbps), ("22:00", 2.35))

    def test_daytime_and_overnight_windows(self):
        day = values(window_start="09:00", window_end="18:00")
        at = lambda hh, mm: datetime(2026, 10, 19, hh, mm)
        self.assertFalse(day.window_open(at(8, 59)))
        self.assertTrue(day.window_open(at(9, 0)))
        self.assertTrue(day.window_open(at(17, 59)))
        self.assertFalse(day.window_open(at(18, 0)))
        self.assertEqual(day.next_window_open(at(19, 0)), datetime(2026, 10, 20, 9, 0))
        self.assertIsNone(day.next_window_open(at(10, 0)))
        night = values(window_start="22:00", window_end="08:00")
        self.assertTrue(night.window_open(at(23, 30)))
        self.assertTrue(night.window_open(at(7, 59)))
        self.assertFalse(night.window_open(at(8, 0)))
        self.assertEqual(night.next_window_open(at(21, 0)), datetime(2026, 10, 19, 22, 0))
        self.assertTrue(values().window_open(at(3, 0)))
        self.assertEqual(night.window_text(), "22:00–08:00")

    def test_storage_round_trip_ignores_corrupt_items(self):
        saved = values(max_concurrent_downloads=3, rate_limit_mbps=8, window_start="01:00",
                       window_end="07:30", keep_awake=False).validated()
        restored = DownloadSettings.from_storage(values(), saved.to_storage())
        self.assertEqual(restored, saved)
        stored = {**saved.to_storage(), "download.max_concurrent": "lots",
                  "download.window_end": "99:99"}
        tolerant = DownloadSettings.from_storage(values(), stored)
        self.assertEqual(tolerant.max_concurrent_downloads, 5)
        self.assertFalse(tolerant.has_window)
        self.assertTrue(tolerant.window_open(datetime(2026, 10, 19, 12, 0)))

    def test_effective_concurrency_matches_connection_budget(self):
        self.assertEqual(values(max_concurrent_downloads=8, max_connections=10).effective_concurrency(), 5)
        self.assertEqual(values(max_concurrent_downloads=3, max_connections=50).effective_concurrency(), 3)

    def test_rate_limit_is_split_across_tasks_and_threads(self):
        with patch.object(settings, "download_rate_limit_mbps", 10.0), \
                patch.object(settings, "max_concurrent_downloads", 5), \
                patch.object(settings, "download_max_connections", 50):
            self.assertEqual(fragment_rate_limit(4), int(10 * 1024 ** 2 / 5 / 4))
            self.assertEqual(fragment_rate_limit(1000), MIN_FRAGMENT_RATE_LIMIT)
        with patch.object(settings, "download_rate_limit_mbps", 0.0):
            self.assertEqual(fragment_rate_limit(4), 0)


class ResizableLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resize_up_wakes_waiters_and_down_holds_new_acquires(self):
        limiter = ResizableLimiter(1)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        limiter.resize(2)
        await asyncio.wait_for(waiter, 1)
        self.assertEqual((limiter.in_use, limiter._value), (2, 0))
        limiter.resize(1)
        limiter.release()
        blocked = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        self.assertFalse(blocked.done(), "占用仍等于新上限时不能再获取")
        limiter.release()
        await asyncio.wait_for(blocked, 1)
        limiter.release()
        self.assertEqual(limiter.in_use, 0)

    async def test_cancelled_waiter_never_leaks_a_permit(self):
        limiter = ResizableLimiter(1)
        await limiter.acquire()
        first = asyncio.create_task(limiter.acquire())
        second = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        # 许可刚交给 first 就被取消：应转交给 second。
        limiter.release()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, 1)
        self.assertEqual(limiter.in_use, 1)
        limiter.release()
        self.assertEqual(limiter.in_use, 0)


class TaskManagerSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/settings.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.saved = {name: getattr(settings, name) for name in (
            "max_concurrent_downloads", "download_max_connections", "download_rate_limit_mbps",
            "download_window_start", "download_window_end", "keep_awake_while_busy")}
        self.manager = TaskManager(session_factory=self.session,
                                   link_health=LinkHealth(probe=AsyncMock(return_value=True)))
        self.manager._request_pump = lambda: None

    async def asyncTearDown(self):
        for name, value in self.saved.items():
            setattr(settings, name, value)
        if self.manager._window_wakeup:
            self.manager._window_wakeup.cancel()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_update_persists_applies_and_restores(self):
        self.manager._now = lambda: datetime(2026, 10, 19, 12, 0)
        result = await self.manager.update_download_settings(values(
            max_concurrent_downloads=3, max_connections=20, rate_limit_mbps=4,
            window_start="22:00", window_end="08:00", keep_awake=False))
        self.assertEqual(result["effective_concurrency"], 3)
        self.assertEqual(self.manager._download_sem.limit, 3)
        self.assertEqual(settings.download_rate_limit_mbps, 4)
        self.assertFalse(self.manager._keep_awake.enabled)
        blocked = self.manager.download_blocked_reason()
        self.assertEqual(blocked["code"], "download_window")
        self.assertIn("22:00–08:00", blocked["message"])
        self.assertIsNotNone(self.manager._window_wakeup, "时段外应安排到点唤醒")
        async with self.session() as session:
            stored = {row.key: row.value for row in (await session.execute(
                RuntimeSetting.__table__.select())).all()}
        self.assertEqual(stored["download.max_concurrent"], "3")

        fresh = TaskManager(session_factory=self.session,
                            link_health=LinkHealth(probe=AsyncMock(return_value=True)))
        fresh._request_pump = lambda: None
        fresh._now = lambda: datetime(2026, 10, 19, 23, 0)
        await fresh.restore_download_settings()
        self.assertEqual(fresh.download_settings()["window_text"], "22:00–08:00")
        self.assertIsNone(fresh.download_blocked_reason())
        if fresh._window_wakeup:
            fresh._window_wakeup.cancel()

    async def test_invalid_update_is_rejected_without_side_effects(self):
        before = self.manager.download_settings()
        with self.assertRaises(ValueError):
            await self.manager.update_download_settings(values(window_start="22:00"))
        self.assertEqual(self.manager.download_settings(), before)
