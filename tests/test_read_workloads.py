"""页面与审美只读工作负载的结果等价及事件循环边界回归。"""
import asyncio
import difflib
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import avatar_store, image_assets, preview_image_store, taste_visual
from backend.database import Base, CloudModelFolder, Model, ModelPreviewVideo
from backend.library_index import library_index, normalize_library_name
import backend.main as main_module
from api_patch import patch_api
from backend.taste_model import TasteModelService, build_label_dataset


class ReadWorkloadDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/reads.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        library_index._invalidate_membership()

    async def asyncTearDown(self):
        library_index._invalidate_membership()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _seed_suggestions(self, *, duplicate=False):
        names = ["Alpha", "Alpha Beta", "Straße", "Ωmega"]
        if duplicate:
            names.append("  STRASSE  ")
        async with self.session() as session:
            session.add_all([Model(
                ph_id=f"suggestion-{index}", name=name,
                profile_type="model", url=f"https://example.test/{index}",
                rank=20-index, missing_videos=index,
            ) for index, name in enumerate(names)])
            session.add_all([CloudModelFolder(
                cid=f"folder-{index}", name=name,
                normalized_name=normalize_library_name(name), active=True,
            ) for index, name in enumerate(("Alpha Beta", "Strasse", "ZZZZZZ"))])
            await session.commit()

    async def _library_page(self):
        with (
            patch_api("async_session", self.session),
            patch_api("ensure_previews", new=AsyncMock()),
            patch_api("schedule_preview_warmup"),
        ):
            return await main_module.list_library_models(
                binding="unbound", query="", page=1, page_size=72)

    async def test_binding_suggestions_preserve_legacy_matches_off_event_loop(self):
        await self._seed_suggestions(duplicate=True)
        async with self.session() as session:
            models = (await session.execute(select(Model).where(
                Model.profile_type.in_(("model", "pornstar"))
            ))).scalars().all()
        # 重名覆盖、Unicode、相似度与并列顺序以原有查询和 difflib 为准。
        known = {normalize_library_name(model.name): model for model in models}
        original_matches = difflib.get_close_matches
        expected = {}
        for name in ("alpha beta", "strasse", "zzzzzz"):
            expected[name] = [{
                "model_id": known[key].id,
                "name": known[key].name,
                "confidence": round(difflib.SequenceMatcher(None, name, key).ratio(), 4),
                "automatic": False,
            } for key in original_matches(name, list(known), n=5, cutoff=0.55)]
        loop_thread = threading.get_ident()
        matching_threads = []

        def matching(*args, **kwargs):
            matching_threads.append(threading.get_ident())
            return original_matches(*args, **kwargs)

        with patch.object(difflib, "get_close_matches", side_effect=matching):
            page = await self._library_page()
        self.assertEqual(page["total"], 3)
        for item in page["items"]:
            self.assertEqual(item["binding_suggestions"],
                             expected[item["folder"]["normalized_name"]])
        self.assertTrue(matching_threads)
        self.assertTrue(all(value != loop_thread for value in matching_threads))

    async def test_unique_suggestions_do_not_load_unused_model_columns(self):
        await self._seed_suggestions()
        queries = []

        def capture(_connection, _cursor, statement, *_args):
            if "FROM models" in statement and "models.profile_type IN" in statement:
                queries.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
        try:
            page = await self._library_page()
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", capture)
        self.assertEqual(page["total"], 3)
        self.assertTrue(queries)
        self.assertTrue(all("models.avatar_sha256" not in query for query in queries))

    async def test_generation_candidate_file_checks_preserve_filters_off_loop(self):
        async with self.session() as session:
            models = [Model(
                ph_id=f"candidate-{index}", name=f"Candidate {index}",
                profile_type="model", url=f"https://example.test/{index}",
                avatar_status="ready", avatar_sha256=f"{index:064x}",
            ) for index in range(1, 6)]
            session.add_all(models)
            await session.flush()
            models[1].cloud_cid = "bound"
            session.add_all([ModelPreviewVideo(
                model_id=model.id, ordinal=0, viewkey=f"preview{model.id:08d}",
                thumbnail="https://example.test/cover.jpg",
            ) for model in models])
            await session.commit()
        dataset = build_label_dataset([SimpleNamespace(
            id=1, left_model_id=models[3].id, right_model_id=99999, choice="skip")])
        loop_thread = threading.get_ident()
        checked = []

        def avatar_path(digest):
            checked.append((digest, threading.get_ident()))
            return None if digest == models[2].avatar_sha256 else Path(self.temp.name)

        with patch.object(avatar_store, "avatar_path", side_effect=avatar_path):
            result = await TasteModelService()._eligible_candidates(
                dataset, session_factory=self.session)
        self.assertEqual([model.id for model in result], [models[0].id, models[4].id])
        self.assertEqual([digest for digest, _ in checked],
                         [models[index].avatar_sha256 for index in (0, 2, 4)])
        self.assertTrue(all(thread_id != loop_thread for _, thread_id in checked))


class LocalImageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_oversized_avatar_rejected_before_verify_or_full_decode(self):
        probe = MagicMock()
        probe.size = (8193, 128)
        probe.__enter__.return_value = probe
        with patch.object(image_assets.Image, "open", return_value=probe):
            with self.assertRaisesRegex(avatar_store.AvatarStoreError, "头像尺寸异常"):
                avatar_store._transcode_webp(b"header-only-test-double")
        probe.verify.assert_not_called()
        probe.load.assert_not_called()

    def test_oversized_preview_rejected_with_preview_error_type(self):
        probe = MagicMock()
        probe.size = (640, 8193)
        probe.__enter__.return_value = probe
        with patch.object(image_assets.Image, "open", return_value=probe):
            with self.assertRaisesRegex(
                    preview_image_store.PreviewImageError, "清晰封面尺寸异常"):
                preview_image_store._transcode_webp(b"header-only-test-double")
        probe.verify.assert_not_called()

    async def test_visual_preparation_batches_local_io_and_keeps_full_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.webp"
            cover.write_bytes(b"generated-cover-bytes" * 20)
            models = {index: SimpleNamespace(id=index, avatar_sha256=f"{index:064x}")
                      for index in range(1, 258)}
            previews = {index: SimpleNamespace(model_id=index, viewkey="shared123")
                        for index in models}
            loop_thread = threading.get_ident()
            io_threads = []
            original_hash = taste_visual._sha256
            original_to_thread = asyncio.to_thread

            def local_avatar(_digest):
                io_threads.append(threading.get_ident())
                return Path(directory) / "avatar.webp"

            def local_preview(_viewkey):
                io_threads.append(threading.get_ident())
                return cover

            def full_hash(path):
                io_threads.append(threading.get_ident())
                return original_hash(path)

            service = taste_visual.TasteVisualFeatures()
            with (
                patch.object(service, "_models_and_first_previews", new=AsyncMock(
                    return_value=(models, previews))),
                patch.object(avatar_store, "avatar_path", side_effect=local_avatar),
                patch.object(taste_visual.preview_image_store, "preview_path",
                             side_effect=local_preview),
                patch.object(taste_visual, "_sha256", side_effect=full_hash),
                patch.object(asyncio, "to_thread", wraps=original_to_thread) as dispatch,
            ):
                assets, ready, fetch_total, succeeded, deferred = await service._prepare_assets(
                    list(reversed(models)), fetch_missing_covers=False,
                    progress=None, session_factory=None)
            cover_hash = hashlib.sha256(cover.read_bytes()).hexdigest()
            expected_input = hashlib.sha256(f"shared123:{cover_hash}".encode()).hexdigest()
            self.assertEqual(ready, set(models))
            self.assertEqual((fetch_total, succeeded, deferred), (0, 0, 0))
            self.assertEqual([asset.model_id for asset in assets[::2]], list(models))
            self.assertTrue(all(asset.input_hash == expected_input for asset in assets[1::2]))
            self.assertTrue(all(asset.input_hash == f"{asset.model_id:064x}"
                                for asset in assets[::2]))
            self.assertTrue(io_threads)
            self.assertTrue(all(thread_id != loop_thread for thread_id in io_threads))
            self.assertLess(dispatch.call_count, 20, "不能每张图片提交一次线程任务")

    async def test_visual_preparation_keeps_missing_cover_and_fetch_failure_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.webp"
            cover.write_bytes(b"generated-cover" * 20)
            models = {index: SimpleNamespace(id=index, avatar_sha256=f"{index:064x}")
                      for index in range(1, 5)}
            previews = {index: SimpleNamespace(model_id=index, viewkey=viewkey)
                        for index, viewkey in ((1, "cached123"), (2, "missing123"), (4, "bad"))}

            def cached_path(viewkey):
                if viewkey == "bad":
                    raise taste_visual.preview_image_store.InvalidPreviewViewkey("invalid")
                return cover if viewkey == "cached123" else None

            async def fetch(viewkey):
                if viewkey == "bad":
                    raise taste_visual.preview_image_store.InvalidPreviewViewkey("invalid")
                return cover

            service = taste_visual.TasteVisualFeatures()
            with (
                patch.object(service, "_models_and_first_previews", new=AsyncMock(
                    return_value=(models, previews))),
                patch.object(avatar_store, "avatar_path", return_value=cover),
                patch.object(taste_visual.preview_image_store, "preview_path",
                             side_effect=cached_path),
                patch.object(taste_visual.preview_image_store, "ensure_preview_image",
                             new=AsyncMock(side_effect=fetch)) as ensure,
            ):
                assets, ready, fetch_total, succeeded, deferred = await service._prepare_assets(
                    list(models), fetch_missing_covers=True, progress=None,
                    session_factory=None)
            self.assertEqual(ready, {1, 2})
            self.assertEqual([asset.model_id for asset in assets], [1, 1, 2, 2])
            # 未设覆盖率门槛时不提前收手，全部封面都会尝试。
            self.assertEqual((fetch_total, succeeded, deferred), (2, 1, 0))
            self.assertEqual([call.args[0] for call in ensure.await_args_list],
                             ["missing123", "bad"])

    async def test_visual_preparation_does_not_hide_hash_read_errors(self):
        service = taste_visual.TasteVisualFeatures()
        model = SimpleNamespace(id=1, avatar_sha256="1" * 64)
        preview = SimpleNamespace(model_id=1, viewkey="missing123")
        with (
            patch.object(service, "_models_and_first_previews", new=AsyncMock(
                return_value=({1: model}, {1: preview}))),
            patch.object(avatar_store, "avatar_path", return_value=Path("avatar.webp")),
            patch.object(taste_visual.preview_image_store, "preview_path",
                         return_value=Path("cover.webp")),
            patch.object(taste_visual, "_sha256", side_effect=FileNotFoundError("removed")),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "removed"):
                await service._prepare_assets(
                    [1], fetch_missing_covers=False, progress=None,
                    session_factory=None)


if __name__ == "__main__":
    unittest.main()
