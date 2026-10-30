import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.patrol as patrol_module
from backend.database import Base, Model
from backend.patrol import Patrol


async def _wait_until(predicate, timeout: float = 1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class PatrolSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_retry_uses_service_without_importing_main(self):
        patrol = Patrol()
        sync = AsyncMock(side_effect=[RuntimeError("temporary"), {"total": 1}])
        with patch.object(patrol_module, "sync_model_core", sync), patch.object(
            patrol_module.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            result = await patrol._sync_with_retry(7)

        self.assertEqual(result, {"total": 1})
        self.assertEqual(sync.await_count, 2)

    async def test_sync_retry_has_one_overall_deadline(self):
        patrol = Patrol()

        async def never_finishes(_model_id):
            await asyncio.Event().wait()

        with patch.object(patrol_module, "sync_model_core", never_finishes), \
                patch.object(
                    patrol_module.settings,
                    "model_sync_timeout_seconds",
                    0.01,
                ):
            with self.assertRaisesRegex(TimeoutError, "总预算"):
                await patrol._sync_with_retry(7)

    async def test_inner_timeout_is_not_reclassified_or_counted_twice(self):
        patrol = Patrol()
        sync = AsyncMock(side_effect=TimeoutError("上游连接超时"))
        failure = AsyncMock()
        with patch.object(patrol_module, "sync_model_core", sync), patch.object(
            patrol_module.asyncio,
            "sleep",
            new=AsyncMock(),
        ), patch.object(patrol, "_record_sync_failure", failure):
            with self.assertRaisesRegex(TimeoutError, "上游连接超时"):
                await patrol._sync_with_retry(7)

        self.assertEqual(sync.await_count, 2)
        failure.assert_awaited_once()

    async def test_new_missing_notifications_are_aggregated_once(self):
        patrol = Patrol()
        summary = AsyncMock()
        missing_summary = AsyncMock()
        result = {
            "details": [
                {"model": "A", "newly_missing": 2, "missing": 5},
                {"model": "B", "newly_missing": 1, "missing": 3},
                {"model": "C", "newly_missing": 0, "missing": 9},
            ],
        }
        with patch.object(
                patrol_module.notifier, "notify_patrol_summary", summary), \
                patch.object(
                    patrol_module.notifier,
                    "notify_new_missing_summary",
                    missing_summary,
                ):
            await patrol._push_summary_notifications(result)

        summary.assert_awaited_once_with(result)
        missing_summary.assert_awaited_once_with([
            {"model_name": "A", "count": 2, "total_missing": 5},
            {"model_name": "B", "count": 1, "total_missing": 3},
        ])

    async def test_interval_change_reschedules_without_running(self):
        patrol = Patrol()
        patrol.interval_hours = 100.0
        calls = 0
        first_finished = asyncio.Event()

        async def run_once():
            nonlocal calls
            calls += 1
            first_finished.set()
            return {"scanned": 0}

        patrol.run_once = run_once
        patrol.start()
        try:
            await asyncio.wait_for(first_finished.wait(), timeout=1)
            await _wait_until(lambda: patrol.next_run_at is not None)
            self.assertTrue(patrol.status()["scheduler_alive"])
            old_next = patrol.next_run_at

            await patrol.set_interval(200.0, persist=False)
            await _wait_until(lambda: patrol.next_run_at != old_next)
            await asyncio.sleep(0.02)

            self.assertEqual(calls, 1)
            self.assertGreater(
                (patrol.next_run_at - datetime.now()).total_seconds(),
                199.0 * 3600,
            )
        finally:
            await patrol.stop()
        self.assertFalse(patrol.status()["scheduler_alive"])

    async def test_run_now_is_not_lost_and_multiple_requests_coalesce(self):
        patrol = Patrol()
        patrol.interval_hours = 100.0
        calls = 0
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()

        async def run_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
            elif calls == 2:
                second_finished.set()
            return {"scanned": 0}

        patrol.run_once = run_once
        patrol.start()
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.gather(
                patrol.trigger_now(),
                patrol.trigger_now(),
                patrol.trigger_now(),
            )
            release_first.set()
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            await asyncio.sleep(0.02)

            self.assertEqual(calls, 2)
            self.assertFalse(patrol.status()["run_now_pending"])
        finally:
            release_first.set()
            await patrol.stop()

    async def test_disabled_scheduler_still_runs_one_manual_round(self):
        patrol = Patrol()
        patrol.enabled = False
        calls = 0
        finished = asyncio.Event()

        async def run_once():
            nonlocal calls
            calls += 1
            finished.set()
            return {"scanned": 0}

        patrol.run_once = run_once
        await patrol.trigger_now()
        await asyncio.wait_for(finished.wait(), timeout=1)
        await _wait_until(lambda: patrol._manual_task is None)

        self.assertEqual(calls, 1)
        self.assertFalse(patrol.enabled)

    async def test_runtime_configuration_survives_new_patrol_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "settings.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                with patch.object(patrol_module, "async_session", session):
                    first = Patrol()
                    await first.set_interval(2.5)
                    await first.set_enabled(False)

                    restored = Patrol()
                    await restored.restore_config()

                self.assertFalse(restored.enabled)
                self.assertEqual(restored.interval_hours, 2.5)
            finally:
                await engine.dispose()

    async def test_unavailable_profile_is_reported_separately_from_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "patrol.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with session() as db:
                    db.add(Model(
                        ph_id="removed",
                        name="Removed",
                        url="https://cn.pornhub.com/model/removed",
                        cloud_cid="cid-1",
                    ))
                    await db.commit()

                patrol = Patrol()
                patrol._missing_viewkeys = AsyncMock(return_value=set())
                with patch.object(patrol_module, "async_session", session), \
                        patch.object(patrol_module.cloud, "check_login",
                                     new=AsyncMock(return_value=True)), \
                        patch.object(patrol_module, "sync_model_core", new=AsyncMock(
                            return_value={
                                "status": "source_unavailable",
                                "error": "主页不可用或已删除",
                            })), \
                        patch.object(patrol_module.manager, "reconcile_missing_pipeline",
                                     new=AsyncMock()) as reconcile:
                    result = await patrol.run_once()

                self.assertEqual(result["source_unavailable"], 1)
                self.assertEqual(result["errors"], 0)
                self.assertEqual(result["scanned"], 0)
                reconcile.assert_not_awaited()
            finally:
                await engine.dispose()

    async def test_patrol_scans_with_two_bounded_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "patrol-concurrency.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with session() as db:
                    db.add_all([Model(
                        ph_id=f"bound-{index}",
                        name=f"Bound {index}",
                        url=f"https://example/{index}",
                        cloud_cid=f"cid-{index}",
                    ) for index in range(4)])
                    await db.commit()

                active = 0
                maximum = 0

                async def sync(_model_id):
                    nonlocal active, maximum
                    active += 1
                    maximum = max(maximum, active)
                    await asyncio.sleep(0.01)
                    active -= 1
                    return {"status": "synced"}

                reconciliation = {
                    "missing": 0,
                    "queued_downloads": 0,
                    "pipeline": 0,
                    "repaired_uploads": 0,
                    "repaired_downloads": 0,
                    "confirmation_pending": 0,
                    "blocked": 0,
                    "unmanaged": 0,
                }
                patrol = Patrol()
                patrol._missing_viewkeys = AsyncMock(return_value=set())
                with patch.object(patrol_module, "async_session", session), \
                        patch.object(patrol_module.settings, "patrol_sync_concurrency", 2), \
                        patch.object(patrol_module.cloud, "check_login",
                                     new=AsyncMock(return_value=True)), \
                        patch.object(patrol_module, "sync_model_core", new=sync), \
                        patch.object(patrol_module.manager, "reconcile_missing_pipeline",
                                     new=AsyncMock(return_value=reconciliation)):
                    result = await patrol.run_once()

                self.assertEqual(result["scanned"], 4)
                self.assertEqual(maximum, 2)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
