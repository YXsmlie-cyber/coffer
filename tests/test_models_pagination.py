from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Model, Task, Video
import backend.main as main_module
from api_patch import patch_api


class ModelsKeysetPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "models-keyset.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.session() as session:
            models = [
                Model(
                    ph_id=f"model-{index}", profile_type="model",
                    profile_slug=f"model-{index}", name=name,
                    url=f"https://example.test/{index}", rank=index,
                    scrape_order=index, missing_videos=10 - index,
                    total_videos=10, cloud_videos=index,
                )
                for index, name in enumerate((
                    "Straße", "STRASSE", "  Alpha   Beta  ", "Élodie", "Ωmega",
                ), start=1)
            ]
            session.add_all(models)
            await session.commit()
            self.models = list(models)
        main_module.library_index._invalidate_membership()

    async def asyncTearDown(self):
        main_module.library_index._invalidate_membership()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _call(self, **kwargs):
        pipeline = AsyncMock(return_value={})
        previews = AsyncMock(return_value={})
        with (
            patch_api("async_session", self.session),
            patch_api("_pipeline_by_model", pipeline),
            patch_api("previews_by_model", previews),
            patch_api("schedule_preview_warmup"),
            patch_api("schedule_preview_backfill"),
        ):
            result = await main_module.list_models(**kwargs)
        return result, pipeline, previews

    async def test_unicode_name_keyset_has_no_skips_or_duplicates(self):
        expected = [
            model.id for model in sorted(
                self.models,
                key=lambda model: (model.library_name_key, model.id),
            )
        ]
        found: list[int] = []
        cursor = None
        while True:
            result, _pipeline, _previews = await self._call(
                limit=2, cursor=cursor, query="", sort="name")
            self.assertEqual(result["total"], len(expected))
            found.extend(item["id"] for item in result["models"])
            if not result["has_more"]:
                self.assertIsNone(result["next_cursor"])
                break
            cursor = result["next_cursor"]
            self.assertTrue(cursor)

        self.assertEqual(found, expected)
        self.assertEqual(len(found), len(set(found)))
        # Straße 与 STRASSE 都 casefold 为 strasse，必须靠 id 稳定破同键。
        strasse_ids = [model.id for model in self.models
                       if model.library_name_key == "strasse"]
        self.assertEqual(
            [model_id for model_id in found if model_id in strasse_ids],
            sorted(strasse_ids),
        )

    async def test_search_uses_same_whitespace_and_casefold_key(self):
        result, _pipeline, _previews = await self._call(
            limit=20, cursor=None, query="  ALPHA    BETA ", sort="name")
        self.assertEqual(
            [item["name"] for item in result["models"]],
            ["  Alpha   Beta  "],
        )
        self.assertEqual(result["total"], 1)

    async def test_cursor_is_bound_to_sort_and_query(self):
        first, _pipeline, _previews = await self._call(
            limit=1, cursor=None, query="", sort="name")
        cursor = first["next_cursor"]
        with self.assertRaises(HTTPException) as sort_error:
            await self._call(limit=1, cursor=cursor, query="", sort="rank")
        self.assertEqual(sort_error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as query_error:
            await self._call(limit=1, cursor=cursor, query="alpha", sort="name")
        self.assertEqual(query_error.exception.status_code, 400)

    async def test_page_only_queries_pipeline_and_previews_and_backfills_missing(self):
        pipeline = AsyncMock(return_value={})
        previews = AsyncMock()
        warmup = AsyncMock()
        # schedule_* 是同步函数；单独用普通 mock 以检查精确页集合。
        from unittest.mock import Mock
        warmup = Mock()
        backfill = Mock()

        async def preview_result(_session, model_ids):
            return {model_ids[0]: [{"viewkey": "first"}]}

        previews.side_effect = preview_result
        with (
            patch_api("async_session", self.session),
            patch_api("_pipeline_by_model", pipeline),
            patch_api("previews_by_model", previews),
            patch_api("schedule_preview_warmup", warmup),
            patch_api("schedule_preview_backfill", backfill),
        ):
            result = await main_module.list_models(
                limit=2, cursor=None, query="", sort="rank")

        page_ids = [item["id"] for item in result["models"]]
        self.assertEqual(pipeline.await_args.args[1], page_ids)
        self.assertEqual(previews.await_args.args[1], page_ids)
        warmup.assert_called_once_with(
            page_ids, session_factory=self.session)
        backfill.assert_called_once_with(
            [page_ids[1]], session_factory=self.session)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["total"], len(self.models))
        self.assertIn("library_filter", result)


class PipelineQueryPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_materializes_tasks_and_probes_video_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "pipeline.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with factory() as session:
                    model = Model(
                        ph_id="pipeline-model", profile_type="model",
                        name="Pipeline", url="https://example.test/pipeline",
                    )
                    session.add(model)
                    await session.flush()
                    session.add(Video(
                        model_id=model.id, viewkey="abc1234567890",
                        title="Missing", url="https://example.test/video",
                        dedup_status="missing",
                    ))
                    session.add(Task(
                        model_id=model.id, viewkey="abc1234567890",
                        model_name=model.name, title="Missing",
                        url="https://example.test/video", task_type="download",
                        status="queued",
                    ))
                    await session.commit()

                    statements: list[tuple[str, object]] = []

                    def capture(_conn, _cursor, statement, parameters, *_args):
                        if "relevant_tasks" in statement and not statement.startswith(
                                "EXPLAIN"):
                            statements.append((statement, parameters))

                    event.listen(engine.sync_engine, "before_cursor_execute", capture)
                    try:
                        result = await main_module._pipeline_by_model(
                            session, [model.id])
                    finally:
                        event.remove(engine.sync_engine, "before_cursor_execute", capture)

                    self.assertEqual(result[model.id]["blocked_downloads"], 1)
                    statement, parameters = statements[-1]
                    self.assertIn("AS MATERIALIZED", statement)
                    self.assertIn("EXISTS (SELECT", statement)
                    connection = await session.connection()
                    plan = (await connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    )).fetchall()
                    details = "\n".join(str(row[3]) for row in plan)
                    self.assertIn("MATERIALIZE relevant_tasks", details)
                    self.assertIn("SCAN relevant_tasks", details)
                    # 旧版 SQLite 显示为相关标量子查询，3.5x 起显示为 EXISTS 索引探测；
                    # 两者都表示按 (model_id, viewkey) 逐条探测，而不是全扫 videos。
                    self.assertTrue(
                        "CORRELATED SCALAR SUBQUERY" in details
                        or "SEARCH videos EXISTS USING INDEX" in details, details)
                    self.assertNotIn("SCAN videos", details)
                    self.assertIn("model_id=? AND viewkey=?", details)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
