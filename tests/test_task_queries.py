"""任务读取的有限批次与当前文件证明；不依赖调度器运行状态。"""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Task
from backend.task_queries import TaskQueries, READ_BATCH_SIZE


class TaskQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/query.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def seed(self, size, path=""):
        async with self.session() as session:
            session.add_all([Task(viewkey=f"example-{i}", title="Example", model_name="Example",
                                 url="https://example.invalid", status="failed", local_path=path,
                                 m3u8_url="unused-signed-url") for i in range(size)])
            await session.commit()

    async def seed_mixed(self):
        rows = [
            ("running", "Alpha morning", "Ann", "aaa111"),
            ("queued", "Bravo 100% real", "Bob", "bbb222"),
            ("completed", "Charlie", "Ann", "ccc333"),
            ("failed", "Delta under_score", "Cat", "ddd444"),
            ("failed", "Echo", "Cat", "eee555"),
            ("failed", "Foxtrot", "Dan", "fff666"),
        ]
        async with self.session() as session:
            session.add_all([Task(viewkey=viewkey, title=title, model_name=model,
                                  url="https://example.invalid", status=status,
                                  error="network" if viewkey != "eee555" else "manual")
                             for status, title, model, viewkey in rows])
            await session.commit()
        return TaskQueries(self.session, lambda row: {
            "bulk_recoverable": row.error == "network",
            "recovery_action": "retry_network" if row.error == "network" else "manual",
            "failure_kind": row.error,
        })

    async def test_status_filter_keeps_failed_reachable_before_completed_history(self):
        queries = await self.seed_mixed()
        rows = await queries.list_tasks(
            "download", limit=10, statuses=("running", "queued", "paused", "failed"))
        self.assertEqual([row["status"] for row in rows],
                         ["running", "queued", "failed", "failed", "failed"])
        self.assertEqual(await queries.count_matching(
            "download", statuses=("running", "queued", "paused", "failed")), 5)

    async def test_keyword_matches_title_model_and_viewkey_with_literal_wildcards(self):
        queries = await self.seed_mixed()
        self.assertEqual({row["viewkey"] for row in await queries.list_tasks(q="ann")},
                         {"aaa111", "ccc333"})
        self.assertEqual([row["viewkey"] for row in await queries.list_tasks(q="FFF6")],
                         ["fff666"])
        # % 与 _ 按字面匹配，不能变成通配符把所有任务都选中。
        self.assertEqual([row["viewkey"] for row in await queries.list_tasks(q="100%")],
                         ["bbb222"])
        # 若 _ 被当作通配符，"a_m" 会命中 "Alpha morning"。
        self.assertEqual([row["viewkey"] for row in await queries.list_tasks(q="a_m")], [])
        self.assertEqual([row["viewkey"] for row in await queries.list_tasks(q="r_sc")],
                         ["ddd444"])
        self.assertEqual(await queries.count_matching(q="cat"), 2)

    async def test_failure_filter_classifies_before_paging(self):
        queries = await self.seed_mixed()
        first = await queries.list_tasks("download", limit=1, failure="recoverable")
        self.assertEqual([row["viewkey"] for row in first], ["fff666"])
        rest = await queries.list_tasks(
            "download", limit=5, failure="recoverable", cursor=(4, first[0]["id"]))
        self.assertEqual([row["viewkey"] for row in rest], ["ddd444"])
        manual = await queries.list_tasks("download", limit=5, failure="manual")
        self.assertEqual([row["viewkey"] for row in manual], ["eee555"])
        self.assertEqual(await queries.count_matching("download", failure="recoverable"), 2)
        self.assertEqual(await queries.count_matching(
            "download", failure="recoverable", q="delta"), 1)

    async def test_today_summary_counts_only_this_local_day_and_type(self):
        from datetime import datetime, timedelta
        now = datetime(2026, 10, 19, 21, 40)
        rows = [
            ("download", "completed", now - timedelta(hours=2), 300),
            ("download", "completed", now - timedelta(minutes=5), 700),
            ("download", "completed", now - timedelta(days=1), 999),
            ("download", "failed", now - timedelta(minutes=1), 50),
            ("upload", "completed", now - timedelta(minutes=1), 1000),
        ]
        async with self.session() as session:
            session.add_all([Task(viewkey=f"t{i}", title="T", model_name="M", url="u",
                                  task_type=kind, status=status, updated_at=at, total_bytes=size)
                             for i, (kind, status, at, size) in enumerate(rows)])
            await session.commit()
        today = await TaskQueries(self.session, lambda row: {}).today_completed("download", now=now)
        self.assertEqual((today["completed"], today["bytes"]), (2, 1000))
        self.assertTrue(today["last_completed_at"].startswith("2026-10-19T21:35"))

    def test_status_parameter_rejects_unknown_values(self):
        from fastapi import HTTPException
        from backend.routes_tasks import _parse_task_statuses

        self.assertEqual(_parse_task_statuses(" running, failed,running "),
                         ("running", "failed"))
        self.assertIsNone(_parse_task_statuses(" "))
        with self.assertRaises(HTTPException):
            _parse_task_statuses("running,unknown")

    async def test_failure_statistics_only_dispatch_finite_scalar_batches(self):
        await self.seed(257)
        batches = []

        class Queries(TaskQueries):
            def _count_failures(self, rows):
                batches.append(len(rows))
                return super()._count_failures(rows)

        queries = Queries(self.session, lambda row: {
            "bulk_recoverable": True, "recovery_action": "retry", "failure_kind": "network"})
        counts = await queries.task_counts("download")
        self.assertEqual(counts["failed_recoverable"], 257)
        self.assertEqual(counts["failed_manual"], 0)
        self.assertEqual(batches, [READ_BATCH_SIZE, READ_BATCH_SIZE, 1])

    async def test_each_read_rechecks_files_and_list_omits_unused_columns(self):
        final = Path(self.temp.name) / "final.mp4"
        final.write_bytes(b"fixture")
        await self.seed(1, str(final))
        queries = TaskQueries(self.session, lambda row: {
            "bulk_recoverable": Path(row.local_path).is_file(),
            "recovery_action": "reconcile_final" if Path(row.local_path).is_file() else "manual",
            "failure_kind": "example",
        })
        first = await queries.task_counts("download")
        self.assertEqual(first["failed_reconcile"], 1)
        final.unlink()
        second = await queries.task_counts("download")
        self.assertEqual(second["failed_reconcile"], 0)
        self.assertEqual(second["failed_manual"], 1)
        statements = []
        event.listen(self.engine.sync_engine, "before_cursor_execute",
                     lambda _c, _cu, statement, *_args: statements.append(statement))
        rows = await queries.list_tasks("download", limit=10)
        self.assertFalse(rows[0]["bulk_recoverable"])
        self.assertNotIn("m3u8_url", rows[0])
        self.assertFalse(any("tasks.m3u8_url" in statement for statement in statements))
