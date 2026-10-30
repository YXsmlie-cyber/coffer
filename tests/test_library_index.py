from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, CloudModelFolder, Model
from backend.library_index import LibraryIndex, normalize_library_name
from backend.scraper import _collect_pornstars
import backend.library_index as library_module


class LibraryIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "library.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.session() as session:
            session.add_all([
                Model(ph_id="anna", name="Anna  Cloud", url="https://example/anna"),
                Model(ph_id="other", name="Other", url="https://example/other"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_successful_scan_binds_exact_and_preserves_unbound(self):
        index = LibraryIndex()
        dirs = [{"cid": "1", "name": " anna cloud "},
                {"cid": "2", "name": "Not Yet Known"}]
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs", new=AsyncMock(return_value=dirs)):
            result = await index.refresh()
            membership = await index.membership()
        self.assertEqual(result["folder_count"], 2)
        self.assertEqual(result["bound_count"], 1)
        self.assertEqual(len(membership.model_ids), 1)
        async with self.session() as session:
            unbound = (await session.execute(select(CloudModelFolder).where(
                CloudModelFolder.cid == "2"))).scalar_one()
        self.assertIsNone(unbound.model_id)

    async def test_failed_scan_keeps_last_success_and_marks_stale(self):
        index = LibraryIndex()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(return_value=[{"cid": "1", "name": "Other"}])):
            await index.refresh()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(side_effect=RuntimeError("timeout"))):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                await index.refresh()
            membership = await index.membership()
        self.assertTrue(membership.stale)
        self.assertIn(2, membership.model_ids)

    async def test_empty_scan_cannot_replace_existing_successful_index(self):
        index = LibraryIndex()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(return_value=[{"cid": "1", "name": "Other"}])):
            await index.refresh()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
                await index.refresh()
            membership = await index.membership()

        self.assertTrue(membership.stale)
        self.assertIn(2, membership.model_ids)
        async with self.session() as session:
            folder = await session.get(CloudModelFolder, "1")
        self.assertTrue(folder.active)
        self.assertEqual(folder.model_id, 2)

    async def test_first_scan_may_publish_a_genuinely_empty_library(self):
        index = LibraryIndex()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(return_value=[])):
            result = await index.refresh()
            membership = await index.membership()
        self.assertEqual(result["folder_count"], 0)
        self.assertFalse(membership.stale)
        self.assertEqual(membership.model_ids, frozenset())

    async def test_refresh_preserves_manual_binding_before_same_name_inference(self):
        async with self.session() as session:
            anna = (await session.execute(select(Model).where(
                Model.ph_id == "anna"
            ))).scalar_one()
            anna.cloud_cid = "z-manual"
            session.add_all([
                CloudModelFolder(
                    cid="a-duplicate",
                    name="Anna Cloud",
                    normalized_name="anna cloud",
                    model_id=None,
                    match_method="unbound",
                    active=True,
                ),
                CloudModelFolder(
                    cid="z-manual",
                    name="Anna Cloud",
                    normalized_name="anna cloud",
                    model_id=anna.id,
                    match_method="manual",
                    active=True,
                ),
            ])
            await session.commit()
            anna_id = anna.id

        index = LibraryIndex()
        dirs = [
            {"cid": "a-duplicate", "name": "Anna Cloud"},
            {"cid": "z-manual", "name": "Anna Cloud"},
        ]
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs",
                new=AsyncMock(return_value=dirs)):
            result = await index.refresh()

        async with self.session() as session:
            duplicate = await session.get(CloudModelFolder, "a-duplicate")
            manual = await session.get(CloudModelFolder, "z-manual")
            anna = await session.get(Model, anna_id)
        self.assertEqual(manual.model_id, anna_id)
        self.assertEqual(manual.match_method, "manual")
        self.assertIsNone(duplicate.model_id)
        self.assertIn("同一模特已绑定", duplicate.error)
        self.assertEqual(anna.cloud_cid, "z-manual")
        self.assertEqual(result["error_count"], 1)

    async def test_refresh_releases_old_unique_binding_before_replacement(self):
        async with self.session() as session:
            anna = (await session.execute(select(Model).where(
                Model.ph_id == "anna"
            ))).scalar_one()
            anna.cloud_cid = "z-old"
            session.add_all([
                CloudModelFolder(
                    cid="z-old",
                    name="Anna Cloud",
                    normalized_name="anna cloud",
                    model_id=anna.id,
                    match_method="manual",
                    active=True,
                ),
                CloudModelFolder(
                    cid="a-replacement",
                    name="Anna Cloud",
                    normalized_name="anna cloud",
                    model_id=None,
                    match_method="unbound",
                    active=False,
                ),
            ])
            await session.commit()
            anna_id = anna.id

        index = LibraryIndex()
        with patch.object(library_module, "async_session", self.session), patch.object(
                library_module.cloud, "list_model_dirs", new=AsyncMock(return_value=[
                    {"cid": "a-replacement", "name": "Anna Cloud"},
                ])):
            result = await index.refresh()

        async with self.session() as session:
            old = await session.get(CloudModelFolder, "z-old")
            replacement = await session.get(CloudModelFolder, "a-replacement")
            anna = await session.get(Model, anna_id)
        self.assertFalse(old.active)
        self.assertIsNone(old.model_id)
        self.assertTrue(replacement.active)
        self.assertEqual(replacement.model_id, anna_id)
        self.assertEqual(replacement.match_method, "exact_name")
        self.assertEqual(anna.cloud_cid, "a-replacement")
        self.assertEqual(result["bound_count"], 1)

    def test_name_normalization_is_deliberately_conservative(self):
        self.assertEqual(normalize_library_name(" Anna   Cloud "), "anna cloud")
        self.assertNotEqual(normalize_library_name("Anna-Cloud"), "anna cloud")

    async def test_record_binding_changes_match_method_once_then_is_write_free(self):
        async with self.session() as session:
            model = (await session.execute(select(Model).where(
                Model.ph_id == "anna"))).scalar_one()
            model.cloud_cid = "upload-cid"
            session.add(CloudModelFolder(
                cid="upload-cid", name=model.name,
                normalized_name=normalize_library_name(model.name),
                model_id=model.id, match_method="exact_name", active=True,
            ))
            await session.commit()
            model_id = model.id

        index = LibraryIndex()
        broadcast = AsyncMock()
        index.set_broadcast(broadcast)
        dml: list[str] = []

        def capture(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ")):
                dml.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
        try:
            first = await index.record_binding(
                model_id, "upload-cid", "Anna  Cloud",
                session_factory=self.session)
            first_dml_count = len(dml)
            second = await index.record_binding(
                model_id, "upload-cid", "Anna  Cloud",
                session_factory=self.session)
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", capture)

        self.assertEqual(first["match_method"], "upload")
        self.assertEqual(second["match_method"], "upload")
        self.assertGreater(first_dml_count, 0)
        self.assertEqual(len(dml), first_dml_count)
        broadcast.assert_awaited_once()

    async def test_membership_cache_is_scoped_by_engine_object(self):
        index = LibraryIndex()
        async with self.session() as session:
            first = await index.membership(session=session)

        second_temp = tempfile.TemporaryDirectory()
        second_path = (Path(second_temp.name) / "other-library.db").as_posix()
        second_engine = create_async_engine(f"sqlite+aiosqlite:///{second_path}")
        second_factory = async_sessionmaker(second_engine, expire_on_commit=False)
        try:
            async with second_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with second_factory() as session:
                model = Model(
                    ph_id="different", name="Different",
                    url="https://example/different")
                session.add(model)
                await session.flush()
                session.add(CloudModelFolder(
                    cid="different-cid", name=model.name,
                    normalized_name="different", model_id=model.id,
                    match_method="manual", active=True,
                ))
                await session.commit()
            async with second_factory() as session:
                second = await index.membership(session=session)
        finally:
            await second_engine.dispose()
            second_temp.cleanup()

        self.assertNotEqual(first.normalized_names, second.normalized_names)
        self.assertEqual(len(index._membership_cache), 2)
        self.assertTrue(all(not isinstance(key, int)
                            for key in index._membership_cache))

    async def test_invalidation_during_read_does_not_recache_stale_snapshot(self):
        index = LibraryIndex()
        invalidated = False

        def invalidate_during_first_select(_conn, _cursor, statement, *_args):
            nonlocal invalidated
            if not invalidated and statement.lstrip().upper().startswith("SELECT "):
                invalidated = True
                index._invalidate_membership()

        event.listen(
            self.engine.sync_engine,
            "before_cursor_execute",
            invalidate_during_first_select,
        )
        try:
            async with self.session() as session:
                await index.membership(session=session)
        finally:
            event.remove(
                self.engine.sync_engine,
                "before_cursor_execute",
                invalidate_during_first_select,
            )

        self.assertTrue(invalidated)
        self.assertEqual(index._membership_cache, {})
        async with self.session() as session:
            await index.membership(session=session)
        self.assertEqual(len(index._membership_cache), 1)


class RankingParserTests(unittest.TestCase):
    def test_ranking_parser_does_not_union_sidebar_cards(self):
        soup = BeautifulSoup("""
        <ul class="pornstarsList">
          <li class="pornstarLi"><a href="/pornstar/verified-woman">Verified Woman</a></li>
        </ul>
        <ul class="pornstarsSort">
          <li><a href="/pornstar/sidebar-man">Sidebar Man</a></li>
        </ul>
        """, "lxml")
        models = []
        found = _collect_pornstars(soup, models, set(), 100)
        self.assertEqual(found, 1)
        self.assertEqual([item["ph_id"] for item in models], ["verified-woman"])


if __name__ == "__main__":
    unittest.main()
