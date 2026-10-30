import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import model_sync, scraper
from backend.database import Base, Model, ModelPreviewVideo, Video
import backend.main as main_module
from api_patch import patch_api


class ModelSyncRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = (Path(self.temp.name) / "sync.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_binding_created_during_scrape_refreshes_cloud_snapshot(self):
        """真实 SQLite 会话交错：空批次 hint 不能覆盖上传刚创建的绑定。"""
        async with self.session() as session:
            model = Model(ph_id="binding-race", name="Binding race",
                          url="https://cn.pornhub.com/model/binding-race")
            session.add(model)
            await session.commit()
            model_id = model.id
        scrape_started = asyncio.Event()
        binding_committed = asyncio.Event()
        video = {"viewkey": "abcdef1234567", "title": "Video",
                 "url": "https://cn.pornhub.com/view_video.php?viewkey=abcdef1234567",
                 "duration": 60, "thumbnail": ""}

        async def scrape(_url):
            scrape_started.set()
            await binding_committed.wait()
            return [video]

        listing = AsyncMock(return_value=[{
            "fid": "fixture-file", "n": "Video [abcdef1234567].mp4",
            "play_long": 60,
        }])
        with patch.object(model_sync.scraper, "scrape_model_videos", new=scrape), \
                patch.object(model_sync.cloud, "list_all_files", new=listing):
            syncing = asyncio.create_task(model_sync.sync_model_core(
                model_id, session_factory=self.session, resolved_cloud_cid=None))
            await asyncio.wait_for(scrape_started.wait(), 2)
            try:
                async with self.session() as session:
                    await session.execute(update(Model).where(Model.id == model_id)
                                          .values(cloud_cid="cid-created-during-sync"))
                    await session.commit()
            finally:
                binding_committed.set()
            result = await asyncio.wait_for(syncing, 3)

        async with self.session() as session:
            saved = await session.get(Model, model_id)
            saved_video = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalar_one()
        self.assertEqual(saved.cloud_cid, "cid-created-during-sync")
        self.assertEqual(saved_video.dedup_status, "exists")
        self.assertEqual((saved.cloud_videos, saved.missing_videos), (1, 0))
        self.assertEqual(result["cloud_cid"], saved.cloud_cid)
        self.assertEqual(result["counts"], {"exists": 1, "maybe": 0, "missing": 0})
        listing.assert_awaited_once_with("cid-created-during-sync", only_video=True)

    async def test_binding_changed_while_waiting_to_publish_recomputes_counts(self):
        async with self.session() as session:
            model = Model(ph_id="publish-race", name="Publish race",
                          url="https://cn.pornhub.com/model/publish-race",
                          cloud_cid="cid-old")
            session.add(model)
            await session.commit()
            model_id = model.id
        video = {"viewkey": "abcdef1234567", "title": "Video", "url": "fixture",
                 "duration": 60, "thumbnail": ""}
        scanned = asyncio.Event()
        semaphore = asyncio.Semaphore(0)

        async def listing(cid, *, only_video):
            if cid == "cid-old":
                scanned.set()
                return []
            return [{"fid": "fixture-file", "n": "Video [abcdef1234567].mp4"}]

        listing_mock = AsyncMock(side_effect=listing)
        with patch.object(model_sync.scraper, "scrape_model_videos",
                          new=AsyncMock(return_value=[video])), \
                patch.object(model_sync.cloud, "list_all_files", new=listing_mock):
            syncing = asyncio.create_task(model_sync.sync_model_core(
                model_id, session_factory=self.session, write_semaphore=semaphore))
            await asyncio.wait_for(scanned.wait(), 2)
            try:
                async with self.session() as session:
                    await session.execute(update(Model).where(Model.id == model_id)
                                          .values(cloud_cid="cid-new"))
                    await session.commit()
            finally:
                semaphore.release()
            result = await asyncio.wait_for(syncing, 3)
        self.assertEqual(result["cloud_cid"], "cid-new")
        self.assertEqual(result["counts"], {"exists": 1, "maybe": 0, "missing": 0})
        self.assertEqual([call.args[0] for call in listing_mock.await_args_list],
                         ["cid-old", "cid-new"])
        async with self.session() as session:
            saved = await session.get(Model, model_id)
            saved_video = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalar_one()
        self.assertEqual(saved.cloud_cid, "cid-new")
        self.assertEqual(saved.missing_videos, 0)
        self.assertEqual(saved_video.dedup_status, "exists")

    async def test_repeated_binding_changes_leave_existing_snapshot_intact(self):
        async with self.session() as session:
            model = Model(ph_id="repeated-race", name="Repeated race",
                          url="https://cn.pornhub.com/model/repeated-race",
                          cloud_cid="cid-1", total_videos=1, cloud_videos=1,
                          snapshot_hash="original-hash")
            session.add(model)
            await session.flush()
            session.add(Video(model_id=model.id, viewkey="1111111111111",
                              title="Original", url="fixture", dedup_status="exists"))
            await session.commit()
            model_id = model.id
        calls = []

        async def listing(cid, *, only_video):
            calls.append(cid)
            async with self.session() as session:
                await session.execute(update(Model).where(Model.id == model_id)
                                      .values(cloud_cid=f"cid-{len(calls) + 1}"))
                await session.commit()
            return []

        with patch.object(model_sync.scraper, "scrape_model_videos", new=AsyncMock(
                return_value=[{"viewkey": "abcdef1234567", "title": "New",
                               "url": "fixture", "duration": 60}])), \
                patch.object(model_sync.cloud, "list_all_files", new=listing):
            with self.assertRaisesRegex(scraper.ScrapeError, "绑定连续变化"):
                await model_sync.sync_model_core(model_id, session_factory=self.session)
        self.assertEqual(calls, ["cid-1", "cid-2"])
        async with self.session() as session:
            saved = await session.get(Model, model_id)
            saved_video = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalar_one()
        self.assertEqual(saved.cloud_cid, "cid-3")
        self.assertEqual(saved.snapshot_hash, "original-hash")
        self.assertEqual((saved.total_videos, saved.cloud_videos), (1, 1))
        self.assertEqual(saved_video.viewkey, "1111111111111")

    async def test_cancelled_binding_refresh_holds_no_database_or_publish_lock(self):
        async with self.session() as session:
            model = Model(ph_id="cancel-refresh", name="Original",
                          url="https://cn.pornhub.com/model/cancel-refresh",
                          snapshot_hash="original-hash")
            session.add(model)
            await session.commit()
            model_id = model.id
        refresh_started = asyncio.Event()
        wait_forever = asyncio.Event()
        semaphore = asyncio.Semaphore(1)

        async def scrape(_url):
            async with self.session() as session:
                await session.execute(update(Model).where(Model.id == model_id)
                                      .values(cloud_cid="cid-new"))
                await session.commit()
            return [{"viewkey": "abcdef1234567", "title": "New", "url": "fixture"}]

        async def listing(cid, *, only_video):
            refresh_started.set()
            await wait_forever.wait()
            return []

        async def another_writer():
            async with self.session() as session:
                await session.execute(update(Model).where(Model.id == model_id)
                                      .values(name="Updated independently"))
                await session.commit()

        with patch.object(model_sync.scraper, "scrape_model_videos", new=scrape), \
                patch.object(model_sync.cloud, "list_all_files", new=listing):
            syncing = asyncio.create_task(model_sync.sync_model_core(
                model_id, session_factory=self.session, resolved_cloud_cid=None,
                write_semaphore=semaphore))
            try:
                await asyncio.wait_for(refresh_started.wait(), 2)
                await asyncio.wait_for(another_writer(), 1)
                await asyncio.wait_for(semaphore.acquire(), 1)
                semaphore.release()
            finally:
                syncing.cancel()
                cancelled = await asyncio.gather(syncing, return_exceptions=True)
        self.assertIsInstance(cancelled[0], asyncio.CancelledError)
        async with self.session() as session:
            saved = await session.get(Model, model_id)
            saved_videos = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalars().all()
        self.assertEqual(saved.cloud_cid, "cid-new")
        self.assertEqual(saved.name, "Updated independently")
        self.assertEqual(saved.snapshot_hash, "original-hash")
        self.assertEqual(saved_videos, [])

    async def test_stale_alias_is_repaired_only_by_exact_ph_id(self):
        async with self.session() as session:
            model = Model(
                ph_id="verified-slug",
                name="Verified",
                url="https://cn.pornhub.com/model/old-alias",
                thumbnail="",
                cloud_cid="cid-1",
            )
            session.add(model)
            await session.commit()
            model_id = model.id

        video = {
            "viewkey": "abcdef1234567",
            "title": "Video",
            "url": "https://cn.pornhub.com/view_video.php?viewkey=abcdef1234567",
            "duration": 60,
            "thumbnail": "",
        }
        scrape = AsyncMock(side_effect=[scraper.ScrapeError("旧地址失效"), [video]])
        lookup = AsyncMock(return_value={
            "ph_id": "verified-slug",
            "name": "Verified",
            "url": "https://cn.pornhub.com/pornstar/verified-slug",
            "thumbnail": "https://cdn.example/avatar.jpg",
        })
        with patch.object(model_sync.scraper, "scrape_model_videos", new=scrape), \
                patch.object(model_sync.scraper, "lookup_model_profile", new=lookup), \
                patch.object(model_sync.cloud, "list_all_files",
                             new=AsyncMock(return_value=[])):
            result = await model_sync.sync_model_core(
                model_id, session_factory=self.session)

        async with self.session() as session:
            saved = await session.get(Model, model_id)
            videos = (await session.execute(select(Video))).scalars().all()
        self.assertEqual(result["counts"]["missing"], 1)
        self.assertEqual(saved.url, "https://cn.pornhub.com/pornstar/verified-slug")
        self.assertEqual([item.viewkey for item in videos], ["abcdef1234567"])
        lookup.assert_awaited_once_with("verified-slug")

    async def test_unavailable_profile_preserves_existing_snapshot(self):
        async with self.session() as session:
            model = Model(
                ph_id="removed",
                name="Removed",
                url="https://cn.pornhub.com/model/removed",
                thumbnail="",
                cloud_cid="cid-1",
                total_videos=1,
                cloud_videos=1,
            )
            session.add(model)
            await session.flush()
            session.add(Video(
                model_id=model.id,
                viewkey="kept123456789",
                title="Kept",
                url="https://cn.pornhub.com/view_video.php?viewkey=kept123456789",
                duration=60,
                dedup_status="exists",
            ))
            await session.commit()
            model_id = model.id

        with patch.object(model_sync.scraper, "scrape_model_videos", new=AsyncMock(
                side_effect=scraper.ModelProfileUnavailable(
                    "主页不可用或已删除"))), patch.object(
                model_sync.cloud, "list_all_files", new=AsyncMock()) as listing:
            result = await model_sync.sync_model_core(
                model_id, session_factory=self.session)

        async with self.session() as session:
            saved = await session.get(Model, model_id)
            videos = (await session.execute(select(Video))).scalars().all()
        self.assertEqual(result["status"], "source_unavailable")
        self.assertEqual(saved.total_videos, 1)
        self.assertEqual([item.viewkey for item in videos], ["kept123456789"])
        listing.assert_not_awaited()

    async def test_injected_cloud_client_handles_lookup_and_listing(self):
        async with self.session() as session:
            model = Model(
                ph_id="injected-cloud",
                name="Injected Cloud",
                url="https://cn.pornhub.com/model/injected-cloud",
                thumbnail="",
                cloud_cid="",
            )
            session.add(model)
            await session.commit()
            model_id = model.id

        videos = [{
            "viewkey": "injected12345",
            "title": "Injected",
            "url": "https://cn.pornhub.com/view_video.php?viewkey=injected12345",
            "duration": 60,
            "thumbnail": "",
        }]
        injected_cloud = AsyncMock()
        injected_cloud.find_subdir.return_value = "cid-injected"
        injected_cloud.list_all_files.return_value = []
        with (
            patch.object(model_sync.scraper, "scrape_model_videos",
                         new=AsyncMock(return_value=videos)),
            patch.object(model_sync.cloud, "find_subdir", new=AsyncMock(
                side_effect=AssertionError("不应访问全局 115 客户端"))),
            patch.object(model_sync.cloud, "list_all_files", new=AsyncMock(
                side_effect=AssertionError("不应访问全局 115 客户端"))),
        ):
            result = await model_sync.sync_model_core(
                model_id,
                session_factory=self.session,
                cloud_client=injected_cloud,
            )

        self.assertEqual(result["cloud_cid"], "cid-injected")
        injected_cloud.find_subdir.assert_awaited_once_with(
            model_sync.settings.video_hub_cid, "Injected Cloud")
        injected_cloud.list_all_files.assert_awaited_once_with(
            "cid-injected", only_video=True)

    async def test_identical_snapshot_preserves_video_and_preview_rows(self):
        async with self.session() as session:
            model = Model(
                ph_id="stable", name="Stable",
                url="https://cn.pornhub.com/model/stable", cloud_cid="cid-stable",
            )
            session.add(model)
            await session.commit()
            model_id = model.id
        videos = [{
            "viewkey": "stable1234567",
            "title": "Stable Video",
            "url": "https://cn.pornhub.com/view_video.php?viewkey=stable1234567",
            "duration": 61,
            "thumbnail": "https://cdn.example/stable.jpg",
        }]
        with (
            patch.object(model_sync.scraper, "scrape_model_videos",
                         new=AsyncMock(return_value=videos)),
            patch.object(model_sync.cloud, "list_all_files",
                         new=AsyncMock(return_value=[])),
        ):
            first = await model_sync.sync_model_core(
                model_id, session_factory=self.session)
            async with self.session() as session:
                video_before = (await session.execute(select(Video).where(
                    Video.model_id == model_id))).scalar_one()
                preview_before = (await session.execute(
                    select(ModelPreviewVideo).where(
                        ModelPreviewVideo.model_id == model_id)
                )).scalar_one()
                identity_before = (
                    video_before.id, preview_before.ordinal,
                    preview_before.updated_at,
                )
            second = await model_sync.sync_model_core(
                model_id, session_factory=self.session)

        async with self.session() as session:
            video_after = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalar_one()
            preview_after = (await session.execute(select(ModelPreviewVideo).where(
                ModelPreviewVideo.model_id == model_id))).scalar_one()
        self.assertTrue(first["snapshot_changed"])
        self.assertFalse(second["snapshot_changed"])
        self.assertEqual(
            (video_after.id, preview_after.ordinal, preview_after.updated_at),
            identity_before,
        )

    async def test_legacy_zero_source_order_only_backfills_hash(self):
        async with self.session() as session:
            model = Model(
                ph_id="legacy-hash", name="Legacy Hash",
                url="https://cn.pornhub.com/model/legacy-hash",
                cloud_cid="cid-legacy", total_videos=1, snapshot_hash="",
            )
            session.add(model)
            await session.flush()
            video = Video(
                model_id=model.id, viewkey="legacyhash1234", title="Legacy",
                url="https://example.test/legacy", thumbnail="",
                duration=12, source_order=0, dedup_status="missing",
                matched_cloud_name="",
            )
            session.add(video)
            await session.commit()
            model_id, video_id = model.id, video.id
        scraped = [{
            "viewkey": "legacyhash1234", "title": "Legacy",
            "url": "https://example.test/legacy", "thumbnail": "",
            "duration": 12,
        }]
        with (
            patch.object(model_sync.scraper, "scrape_model_videos",
                         new=AsyncMock(return_value=scraped)),
            patch.object(model_sync.cloud, "list_all_files",
                         new=AsyncMock(return_value=[])),
        ):
            result = await model_sync.sync_model_core(
                model_id, session_factory=self.session)

        async with self.session() as session:
            saved = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalar_one()
            saved_model = await session.get(Model, model_id)
        self.assertFalse(result["snapshot_changed"])
        self.assertEqual(saved.id, video_id)
        self.assertTrue(saved_model.snapshot_hash.startswith("v1:"))

    async def test_preview_failure_does_not_rollback_main_snapshot(self):
        async with self.session() as session:
            model = Model(
                ph_id="preview-failure", name="Preview Failure",
                url="https://cn.pornhub.com/model/preview-failure",
                cloud_cid="cid-preview", total_videos=1,
            )
            session.add(model)
            await session.flush()
            session.add(Video(
                model_id=model.id, viewkey="old1234567890", title="Old",
                url="https://example.test/old", thumbnail="https://img/old.jpg",
                duration=10, source_order=1, dedup_status="missing",
            ))
            session.add(ModelPreviewVideo(
                model_id=model.id, ordinal=0, viewkey="old1234567890",
                title="Old preview", url="https://example.test/old",
                thumbnail="https://img/old.jpg", duration=10,
            ))
            await session.commit()
            model_id = model.id

        changed = [{
            "viewkey": "new1234567890", "title": "New",
            "url": "https://example.test/new", "thumbnail": "https://img/new.jpg",
            "duration": 20,
        }]
        with (
            patch.object(model_sync.scraper, "scrape_model_videos",
                         new=AsyncMock(return_value=changed)),
            patch.object(model_sync.cloud, "list_all_files",
                         new=AsyncMock(return_value=[])),
            patch.object(model_sync, "refresh_for_models", new=AsyncMock(
                side_effect=RuntimeError("preview materialization failed"))),
        ):
            result = await model_sync.sync_model_core(
                model_id, session_factory=self.session)

        async with self.session() as session:
            saved_videos = (await session.execute(select(Video).where(
                Video.model_id == model_id))).scalars().all()
            saved_preview = (await session.execute(select(ModelPreviewVideo).where(
                ModelPreviewVideo.model_id == model_id))).scalar_one()
        self.assertEqual(result["status"], "synced")
        self.assertTrue(result["snapshot_changed"])
        self.assertEqual([item.viewkey for item in saved_videos], ["new1234567890"])
        self.assertEqual(saved_preview.viewkey, "old1234567890")

    def test_snapshot_hash_accepts_legacy_null_source_order(self):
        row = {
            "viewkey": "legacy1234567", "title": "Legacy",
            "url": "https://example.test/legacy", "thumbnail": "",
            "duration": 12, "source_order": None,
            "dedup_status": "missing", "matched_cloud_name": "",
        }
        digest = model_sync._snapshot_hash([row])
        self.assertEqual(
            digest,
            model_sync._snapshot_hash([{**row, "source_order": 1}]),
        )

    async def test_single_model_route_maps_sync_timeout_to_504(self):
        with patch_api(
                "_sync_model_core", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            with self.assertRaises(main_module.HTTPException) as raised:
                await main_module.sync_model(123)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertIn("时间预算", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
