"""显式服务依赖与独立实例的行为回归。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Model, Task, Video
from backend.task_manager import TaskManager
from backend.patrol import Patrol


class ServiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/isolated.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_task_reads_and_state_writes_use_injected_database(self):
        async with self.session() as session:
            task = Task(viewkey="isolated", title="Example", model_name="Example",
                        url="https://example.invalid", status="queued")
            session.add(task)
            await session.commit()
        manager = TaskManager(session_factory=self.session)
        with patch("backend.task_manager.async_session", side_effect=AssertionError("global database")):
            await manager._set_status(task.id, status="paused", _task_type="download")
            rows = await manager.list_tasks("download", limit=10)
            counts = await manager.task_counts("download")
        self.assertEqual([(row["id"], row["status"]) for row in rows], [(task.id, "paused")])
        self.assertEqual(counts["paused"], 1)

    async def test_cloud_directory_caches_belong_to_injected_instance(self):
        left_cloud = SimpleNamespace(list_all_files=AsyncMock(return_value=[]))
        right_cloud = SimpleNamespace(list_all_files=AsyncMock(return_value=[]))
        left = TaskManager(cloud_client=left_cloud)
        right = TaskManager(cloud_client=right_cloud)
        with patch("backend.task_manager.cloud.list_all_files", side_effect=AssertionError("global cloud")):
            await left._get_dedup_index("same")
            await left._get_dedup_index("same")
            await right._get_dedup_index("same")
        self.assertEqual(left_cloud.list_all_files.await_count, 1)
        self.assertEqual(right_cloud.list_all_files.await_count, 1)
        self.assertIsNot(left._pump_lock, right._pump_lock)

    async def test_patrol_uses_injected_sync_and_media_manager(self):
        sync = AsyncMock(return_value={"total": 3})
        media = SimpleNamespace(reconcile_missing_pipeline=AsyncMock(
            return_value={"queued_downloads": 2}))
        patrol = Patrol(session_factory=self.session, sync_func=sync, task_manager=media)
        with patch("backend.patrol.sync_model_core", side_effect=AssertionError("global sync")):
            self.assertEqual(await patrol._sync_with_retry(4), {"total": 3})
            self.assertEqual(await patrol._queue_missing(4), 2)
        sync.assert_awaited_once_with(4)
        media.reconcile_missing_pipeline.assert_awaited_once_with(4, auto_repair=patrol.auto_download)

    async def test_uploaded_video_inventory_remains_idempotent(self):
        async with self.session() as session:
            model = Model(ph_id="inventory", name="Example", url="https://example.invalid",
                          total_videos=1, missing_videos=1, snapshot_hash="previous")
            session.add(model)
            await session.flush()
            session.add(Video(model_id=model.id, viewkey="one", title="Example",
                              url="https://example.invalid", dedup_status="missing"))
            await session.commit()
        manager = TaskManager(session_factory=self.session)
        broadcast = AsyncMock()
        manager.set_broadcast(broadcast)
        await manager._mark_video_exists("one", model.id, model.name)
        await manager._mark_video_exists("one", model.id, model.name)
        async with self.session() as session:
            saved = await session.get(Model, model.id)
            self.assertEqual((saved.cloud_videos, saved.missing_videos, saved.snapshot_hash), (1, 0, ""))
        self.assertEqual(broadcast.await_count, 1)
