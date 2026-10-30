import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import model_sync_coordinator as coordinator_module
from backend.database import Base, Model
from backend.model_sync_coordinator import ModelSyncCoordinator


class FakeCloud:
    def __init__(self, directories=None, error=None):
        self.directories = directories or []
        self.error = error
        self.root_calls = 0

    async def list_model_dirs(self):
        self.root_calls += 1
        if self.error:
            raise self.error
        return list(self.directories)


class ModelSyncCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "jobs.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def _add_models(self, rows):
        async with self.session() as session:
            models = [Model(
                ph_id=name.casefold(),
                name=name,
                url=f"https://example/{name}",
                cloud_cid=cid,
            ) for name, cid in rows]
            session.add_all(models)
            await session.commit()
            return [model.id for model in models]

    async def test_batch_reads_root_once_and_passes_fresh_cid_hints(self):
        alpha, beta, bound = await self._add_models([
            ("Alpha", ""), ("Beta", ""), ("Bound", "cid-bound"),
        ])
        cloud = FakeCloud([{"name": " alpha ", "cid": "cid-alpha"}])
        calls = []
        cloud_clients = []

        async def sync(model_id, **kwargs):
            calls.append((model_id, kwargs.get("resolved_cloud_cid")))
            cloud_clients.append(kwargs.get("cloud_client"))
            return {"status": "synced", "timings": {"ph_ms": 1}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=cloud, sync_func=sync)
        events = []
        coordinator.set_broadcast(events.append)
        job = await coordinator.create_job([alpha, beta, bound, 999999, alpha])
        await job.task

        self.assertEqual(cloud.root_calls, 1)
        self.assertEqual(dict(calls), {
            alpha: "cid-alpha",
            beta: None,
            bound: "cid-bound",
        })
        self.assertTrue(all(client is cloud for client in cloud_clients))
        self.assertEqual(job.total, 4)
        self.assertEqual((job.done, job.succeeded, job.failed), (4, 3, 1))
        self.assertEqual(job.not_found_ids, [999999])
        self.assertTrue(any(event["phase"] == "job_finished" for event in events))

    async def test_two_jobs_never_reuse_previous_root_snapshot(self):
        (model_id,) = await self._add_models([("Alpha", "")])
        cloud = FakeCloud([{"name": "Alpha", "cid": "cid-alpha"}])
        calls = 0

        async def sync(_model_id, **_kwargs):
            nonlocal calls
            calls += 1
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=cloud, sync_func=sync)
        first = await coordinator.create_job([model_id])
        await first.task
        second = await coordinator.create_job([model_id])
        await second.task

        self.assertEqual(cloud.root_calls, 2)
        self.assertEqual(calls, 2)

    async def test_root_failure_preserves_bound_work_and_only_fails_unbound(self):
        unbound, bound = await self._add_models([
            ("Unbound", ""), ("Bound", "cid-bound"),
        ])
        cloud = FakeCloud(error=RuntimeError("115 unavailable"))
        calls = []

        async def sync(model_id, **_kwargs):
            calls.append(model_id)
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=cloud, sync_func=sync)
        job = await coordinator.create_job([unbound, bound])
        await job.task

        self.assertEqual(calls, [bound])
        self.assertEqual((job.succeeded, job.failed), (1, 1))
        self.assertIn("读取 115 根目录失败", job.errors[0]["error"])

    async def test_global_batch_concurrency_uses_configured_cap(self):
        ids = await self._add_models([
            (f"Bound {index}", f"cid-{index}") for index in range(6)
        ])
        active = 0
        maximum = 0

        async def sync(_model_id, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"status": "synced", "timings": {}}

        with patch.object(coordinator_module.settings, "model_sync_concurrency", 2):
            coordinator = ModelSyncCoordinator(
                session_factory=self.session,
                cloud_client=FakeCloud(),
                sync_func=sync,
            )
            job = await coordinator.create_job(ids)
            await job.task

        self.assertEqual(maximum, 2)
        self.assertEqual(job.succeeded, 6)

    async def test_identical_active_job_is_reused(self):
        ids = await self._add_models([
            ("Bound A", "cid-a"), ("Bound B", "cid-b"),
        ])
        release = asyncio.Event()
        calls = 0

        async def sync(_model_id, **_kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
        partition = AsyncMock(wraps=coordinator._partition_model_ids)
        coordinator._partition_model_ids = partition
        first = await coordinator.create_job(ids)
        second = await coordinator.create_job(ids)
        self.assertIs(second, first)
        self.assertEqual(second.id, first.id)
        self.assertEqual(partition.await_count, 1)
        release.set()
        await first.task
        self.assertEqual(calls, 2)

    async def test_deleted_model_between_acceptance_and_loading_is_accounted(self):
        (model_id,) = await self._add_models([("Deleted", "cid-deleted")])

        async def sync(_model_id, **_kwargs):
            self.fail("已删除上传者不应进入同步函数")

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
        allow_load = asyncio.Event()
        load_models = coordinator._load_models

        async def delayed_load(model_ids):
            await allow_load.wait()
            return await load_models(model_ids)

        coordinator._load_models = delayed_load
        job = await coordinator.create_job([model_id])
        async with self.session() as session:
            model = await session.get(Model, model_id)
            await session.delete(model)
            await session.commit()
        allow_load.set()
        await job.task

        self.assertEqual(job.state, "completed")
        self.assertEqual((job.total, job.done), (1, 1))
        self.assertEqual((job.succeeded, job.failed), (0, 1))
        self.assertEqual(job.not_found_ids, [model_id])
        self.assertEqual(job.errors, [{
            "model_id": model_id,
            "error": "上传者在同步开始前被删除",
        }])

    async def test_overlapping_jobs_share_same_model_inflight(self):
        shared, other = await self._add_models([
            ("Shared", "cid-shared"), ("Other", "cid-other"),
        ])
        shared_started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def sync(model_id, **_kwargs):
            calls.append(model_id)
            if model_id == shared:
                shared_started.set()
                await release.wait()
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
        first = await coordinator.create_job([shared])
        await shared_started.wait()
        shared_task = coordinator._inflight[shared]
        joined = asyncio.Event()
        original_shield = asyncio.shield
        original_load = coordinator._load_models

        async def delayed_load(model_ids):
            # 模拟慢磁盘：固定次数的 sleep(0) 不能证明异步 SQLite 读取已完成。
            await asyncio.sleep(0.05)
            return await original_load(model_ids)

        def observe_join(task):
            waiting = original_shield(task)
            if task is shared_task:
                joined.set()
            return waiting

        coordinator._load_models = delayed_load
        with patch.object(coordinator_module.asyncio, "shield", observe_join):
            second = await coordinator.create_job([shared, other])
            try:
                # 确认第二个 worker 确实等待同一个 inflight，再释放同步工作。
                await asyncio.wait_for(joined.wait(), timeout=10)
            finally:
                release.set()
                await asyncio.gather(first.task, second.task)
        self.assertEqual(calls.count(shared), 1)
        self.assertEqual(calls.count(other), 1)
        self.assertEqual((first.succeeded, second.succeeded), (1, 2))

    async def test_per_model_timeout_cancels_core_and_releases_slot(self):
        (model_id,) = await self._add_models([("Stuck", "cid-stuck")])
        cancelled = asyncio.Event()

        async def sync(_model_id, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(coordinator_module.settings,
                         "model_sync_timeout_seconds", 0.03),
            patch.object(coordinator_module.settings, "model_sync_concurrency", 1),
        ):
            coordinator = ModelSyncCoordinator(
                session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
            job = await coordinator.create_job([model_id])
            await asyncio.wait_for(job.task, timeout=1)

        self.assertTrue(cancelled.is_set())
        self.assertEqual((job.done, job.succeeded, job.failed), (1, 0, 1))
        self.assertEqual(job.error_groups[0]["scope"], "model_timeout")
        self.assertEqual(job.error_groups[0]["affected"], 1)
        self.assertEqual(coordinator._sync_sem._value, 1)
        await asyncio.sleep(0)
        self.assertNotIn(model_id, coordinator._inflight)

    async def test_queue_wait_does_not_consume_each_models_budget(self):
        ids = await self._add_models([
            (f"Queued {index}", f"cid-{index}") for index in range(5)
        ])

        async def sync(_model_id, **_kwargs):
            # 单次执行与 timeout 留出充足调度余量；整批串行墙钟时间仍明确
            # 超过 timeout，避免 Windows CI 高负载把本测试误判成业务超时。
            await asyncio.sleep(0.05)
            return {"status": "synced", "timings": {}}

        with (
            patch.object(coordinator_module.settings,
                         "model_sync_timeout_seconds", 0.2),
            patch.object(coordinator_module.settings, "model_sync_concurrency", 1),
        ):
            coordinator = ModelSyncCoordinator(
                session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
            job = await coordinator.create_job(ids)
            await asyncio.wait_for(job.task, timeout=1)

        # 整批墙钟时间超过单模特预算，但每个实际运行阶段都在预算内。
        self.assertGreater(job.timings["wall_ms"], 200)
        self.assertEqual((job.succeeded, job.failed), (len(ids), 0))

    async def test_all_joiners_observe_same_timeout_result(self):
        (model_id,) = await self._add_models([("Joiners", "cid-joiners")])
        calls = 0

        async def sync(_model_id, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

        with patch.object(
                coordinator_module.settings, "model_sync_timeout_seconds", 0.02):
            coordinator = ModelSyncCoordinator(
                session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
            results = await asyncio.gather(
                coordinator.sync_model(model_id), coordinator.sync_model(model_id),
                return_exceptions=True,
            )
        self.assertEqual(calls, 1)
        self.assertTrue(all(isinstance(item, asyncio.TimeoutError) for item in results))

    async def test_last_abandoned_waiter_stops_shared_sync_without_error_log(self):
        """巡查总预算到期后，无人等待的同步应停止，而不是跑满自身超时再刷 ERROR。"""
        (model_id,) = await self._add_models([("Abandoned", "cid-abandoned")])
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def sync(_model_id, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        try:
            with patch.object(coordinator_module.settings, "model_sync_concurrency", 1):
                coordinator = ModelSyncCoordinator(
                    session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(coordinator.sync_model(model_id), timeout=0.05)
                await asyncio.wait_for(cancelled.wait(), timeout=1)
                for _ in range(5):
                    await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        self.assertTrue(started.is_set())
        self.assertEqual(reported, [])
        self.assertEqual(coordinator._sync_sem._value, 1)
        self.assertNotIn(model_id, coordinator._inflight)
        self.assertEqual(coordinator._waiters, {})

    async def test_remaining_waiter_keeps_shared_sync_running(self):
        (model_id,) = await self._add_models([("Shared Wait", "cid-shared-wait")])
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def sync(_model_id, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
        impatient = asyncio.create_task(coordinator.sync_model(model_id))
        patient = asyncio.create_task(coordinator.sync_model(model_id))
        await started.wait()
        impatient.cancel()
        await asyncio.gather(impatient, return_exceptions=True)
        release.set()

        result = await asyncio.wait_for(patient, timeout=1)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(calls, 1)

    async def test_new_waiter_never_joins_a_sync_that_is_being_cancelled(self):
        (model_id,) = await self._add_models([("Restarted", "cid-restarted")])
        first_started = asyncio.Event()
        calls = 0

        async def sync(_model_id, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # 模拟退出前的清理耗时：取消尚未完成时新请求已经到达。
                    await asyncio.sleep(0.02)
                    raise
            return {"status": "synced", "timings": {}}

        coordinator = ModelSyncCoordinator(
            session_factory=self.session, cloud_client=FakeCloud(), sync_func=sync)
        abandoned = asyncio.create_task(coordinator.sync_model(model_id))
        await first_started.wait()
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)

        result = await asyncio.wait_for(coordinator.sync_model(model_id), timeout=1)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
