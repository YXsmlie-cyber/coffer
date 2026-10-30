import asyncio
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    CloudModelFolder,
    Model,
    ModelPreviewVideo,
    Task,
    TasteComparison,
)
from backend.main import TasteComparisonRequest
import backend.main as main_module
from api_patch import patch_api
import backend.taste_service as taste_service


class TasteCalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "taste.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.session_patch = patch_api("async_session", self.session)
        self.lock_patch = patch_api("_taste_lock", asyncio.Lock())
        self.avatar_patch = patch.object(
            taste_service.avatar_store,
            "avatar_path",
            return_value=Path(self.temp.name) / "verified.webp",
        )
        self.session_patch.start()
        self.lock_patch.start()
        self.avatar_patch.start()

        async with self.session() as session:
            eligible = [
                Model(
                    ph_id=f"eligible-{index}",
                    profile_type="model" if index % 2 else "pornstar",
                    profile_slug=f"eligible-{index}",
                    name=f"Eligible {index}",
                    url=f"https://example/eligible-{index}",
                    avatar_status="ready",
                    avatar_sha256=str(index) * 64,
                    rank=index,
                )
                for index in range(1, 5)
            ]
            in_library = Model(
                ph_id="in-library", profile_type="model", name="In Library",
                url="https://example/in-library", avatar_status="ready",
                avatar_sha256="a" * 64,
            )
            channel = Model(
                ph_id="channel:hidden", profile_type="channel", name="Channel",
                url="https://example/channel", avatar_status="ready",
                avatar_sha256="b" * 64,
            )
            no_preview = Model(
                ph_id="no-preview", profile_type="model", name="No Preview",
                url="https://example/no-preview", avatar_status="ready",
                avatar_sha256="c" * 64,
            )
            no_avatar = Model(
                ph_id="no-avatar", profile_type="model", name="No Avatar",
                url="https://example/no-avatar", avatar_status="pending",
                avatar_sha256="",
            )
            session.add_all([*eligible, in_library, channel, no_preview, no_avatar])
            await session.flush()
            self.eligible_ids = {model.id for model in eligible}
            session.add(CloudModelFolder(
                cid="library-cid", name=in_library.name,
                normalized_name="in library", model_id=in_library.id,
                match_method="manual", active=True,
            ))
            for model in [*eligible, in_library, channel, no_avatar]:
                session.add(ModelPreviewVideo(
                    model_id=model.id,
                    ordinal=0,
                    viewkey=f"preview{model.id:06d}",
                    title=f"Preview {model.name}",
                    url=f"https://example/video/{model.id}",
                    thumbnail=f"https://example/image/{model.id}.jpg",
                    duration=60 + model.id,
                ))
            await session.commit()

    async def asyncTearDown(self):
        self.avatar_patch.stop()
        self.lock_patch.stop()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_pair_only_uses_ready_unbound_performers_with_previews(self):
        payload = await main_module.get_taste_pair()
        pair_ids = {payload["pair"]["left"]["id"], payload["pair"]["right"]["id"]}

        self.assertTrue(pair_ids <= self.eligible_ids)
        self.assertEqual(payload["stats"]["available_candidates"], 4)
        self.assertEqual(payload["stats"]["answered_count"], 0)
        for candidate in payload["pair"].values():
            self.assertTrue(candidate["avatar_url"].startswith("/api/models/"))
            self.assertEqual(len(candidate["preview_videos"]), 1)

    async def test_record_advances_pair_and_never_creates_task(self):
        initial = await main_module.get_taste_pair()
        left_id = initial["pair"]["left"]["id"]
        right_id = initial["pair"]["right"]["id"]
        result = await main_module.record_taste_comparison(TasteComparisonRequest(
            left_model_id=left_id,
            right_model_id=right_id,
            choice="left",
        ))

        next_ids = {result["pair"]["left"]["id"], result["pair"]["right"]["id"]}
        self.assertFalse(next_ids & {left_id, right_id})
        self.assertEqual(result["stats"]["reviewed_count"], 1)
        self.assertEqual(result["stats"]["answered_count"], 1)
        self.assertEqual(result["stats"]["breakdown"]["left"], 1)

        async with self.session() as session:
            task_count = (await session.execute(
                select(func.count()).select_from(Task))).scalar_one()
            comparison = (await session.execute(
                select(TasteComparison))).scalar_one()
        self.assertEqual(task_count, 0)
        self.assertEqual(comparison.pair_key, f"{left_id}:{right_id}")

    async def test_stale_pair_is_rejected_and_latest_choice_can_be_undone(self):
        initial = await main_module.get_taste_pair()
        body = TasteComparisonRequest(
            left_model_id=initial["pair"]["left"]["id"],
            right_model_id=initial["pair"]["right"]["id"],
            choice="both",
        )
        await main_module.record_taste_comparison(body)

        with self.assertRaises(HTTPException) as stale_error:
            await main_module.record_taste_comparison(body)
        self.assertEqual(stale_error.exception.status_code, 409)

        restored = await main_module.undo_latest_taste_comparison()
        self.assertEqual(restored["pair"], initial["pair"])
        self.assertEqual(restored["stats"]["reviewed_count"], 0)
        self.assertFalse(restored["stats"]["has_undo"])

    async def test_skip_is_reviewed_but_does_not_count_toward_goal(self):
        initial = await main_module.get_taste_pair()
        result = await main_module.record_taste_comparison(TasteComparisonRequest(
            left_model_id=initial["pair"]["left"]["id"],
            right_model_id=initial["pair"]["right"]["id"],
            choice="skip",
        ))
        self.assertEqual(result["stats"]["reviewed_count"], 1)
        self.assertEqual(result["stats"]["answered_count"], 0)
        self.assertEqual(result["stats"]["skipped_count"], 1)
        self.assertEqual(result["stats"]["goal_remaining"], 20)

    async def test_same_model_and_empty_undo_are_rejected(self):
        initial = await main_module.get_taste_pair()
        model_id = initial["pair"]["left"]["id"]
        with self.assertRaises(HTTPException) as same_error:
            await main_module.record_taste_comparison(TasteComparisonRequest(
                left_model_id=model_id,
                right_model_id=model_id,
                choice="left",
            ))
        self.assertEqual(same_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as undo_error:
            await main_module.undo_latest_taste_comparison()
        self.assertEqual(undo_error.exception.status_code, 404)

    async def test_initial_avatar_file_scan_runs_off_event_loop(self):
        taste_service._avatar_path_cache.clear()
        event_loop_thread = threading.get_ident()
        stat_threads: list[int] = []

        def avatar_path(_digest):
            stat_threads.append(threading.get_ident())
            return Path(self.temp.name) / "verified.webp"

        taste_service.avatar_store.avatar_path.side_effect = avatar_path
        taste_service.avatar_store.avatar_path.return_value = None
        payload = await main_module.get_taste_pair()
        self.assertIsNotNone(payload["pair"])
        self.assertTrue(stat_threads)
        self.assertTrue(all(thread_id != event_loop_thread
                            for thread_id in stat_threads))


if __name__ == "__main__":
    unittest.main()
