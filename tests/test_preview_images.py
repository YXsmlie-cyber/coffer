import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import preview_image_store, scraper
from backend.database import Base, Model, ModelPreviewVideo
from backend.preview_service import (
    backfill_profile_previews, preview_wire, warm_preview_images,
)
import backend.preview_service as preview_service


def _jpeg(width=640, height=360) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (32, 96, 180)).save(
        output, format="JPEG", quality=92)
    return output.getvalue()


class PreviewImageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        preview_image_store._failure_cache.clear()
        await preview_image_store.close_clients()

    async def asyncTearDown(self):
        preview_image_store._failure_cache.clear()
        await preview_image_store.close_clients()

    async def test_ensure_transcodes_to_640_webp_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(preview_image_store.settings, "preview_image_dir", directory), \
                patch.object(
                    preview_image_store, "_resolve_poster_url",
                    new=AsyncMock(return_value=("https://cdn.example/poster.jpg", "https://cn.pornhub.com/view_video.php?viewkey=abc123def4567")),
                ) as resolve, \
                patch.object(
                    preview_image_store, "_download_image",
                    new=AsyncMock(return_value=_jpeg()),
                ) as download:
            first = await preview_image_store.ensure_preview_image("abc123def4567")
            second = await preview_image_store.ensure_preview_image("abc123def4567")

            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            with Image.open(first) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (640, 360))
            resolve.assert_awaited_once()
            download.assert_awaited_once()

    async def test_invalid_viewkey_is_rejected_before_filesystem_access(self):
        with self.assertRaises(preview_image_store.InvalidPreviewViewkey):
            await preview_image_store.ensure_preview_image("../escape")

    async def test_transient_failure_ttl_suppresses_duplicate_network_work(self):
        resolve = AsyncMock(side_effect=
                            preview_image_store.PreviewImageTransientError("429"))
        with patch.object(preview_image_store, "preview_path", return_value=None), \
                patch.object(preview_image_store, "_resolve_poster_url", new=resolve), \
                patch.object(preview_image_store.time, "monotonic", return_value=100.0):
            for _ in range(2):
                with self.assertRaises(
                        preview_image_store.PreviewImageTransientError):
                    await preview_image_store.ensure_preview_image("retry1234567")

        resolve.assert_awaited_once()
        expires_at, error_type, _detail = preview_image_store._failure_cache[
            "retry1234567"]
        self.assertEqual(error_type, preview_image_store.PreviewImageTransientError)
        self.assertEqual(
            expires_at, 100.0 + preview_image_store._TRANSIENT_FAILURE_TTL)

    async def test_image_client_is_lifecycle_shared_and_closed(self):
        with patch.object(
                preview_image_store, "_image_client",
                wraps=preview_image_store._image_client) as factory:
            first = await preview_image_store._get_image_client()
            second = await preview_image_store._get_image_client()
            self.assertIs(first, second)
            factory.assert_called_once_with()
            self.assertFalse(first.is_closed)
            await preview_image_store.close_clients()
            self.assertTrue(first.is_closed)


class PreviewPayloadTests(unittest.TestCase):
    def test_materialized_preview_exposes_local_clear_image_endpoint(self):
        payload = preview_wire(SimpleNamespace(
            viewkey="abc123def4567",
            title="Title",
            url="https://cn.pornhub.com/view_video.php?viewkey=abc123def4567",
            thumbnail="https://cdn.example/150.jpg",
            duration=120,
            source="video",
        ))
        self.assertEqual(
            payload["preview_image_url"],
            "/api/previews/abc123def4567/image",
        )

    def test_playlist_order_keeps_dom_high_resolution_thumbnail(self):
        playlist = [
            {"viewkey": "one123456", "thumbnail": "https://cdn/150-one.jpg", "title": "One"},
            {"viewkey": "two123456", "thumbnail": "https://cdn/150-two.jpg", "title": "Two"},
        ]
        dom = [
            {"viewkey": "two123456", "thumbnail": "https://cdn/320-two.jpg"},
            {"viewkey": "one123456", "thumbnail": "https://cdn/320-one.jpg"},
        ]

        merged = scraper._merge_playlist_with_dom_videos(
            playlist, dom, max_videos=50)

        self.assertEqual([item["viewkey"] for item in merged], ["one123456", "two123456"])
        self.assertEqual(
            [item["thumbnail"] for item in merged],
            ["https://cdn/320-one.jpg", "https://cdn/320-two.jpg"],
        )
        # 输入是抓取结果快照，合并不得原地改写，避免后续审计数据被污染。
        self.assertEqual(playlist[0]["thumbnail"], "https://cdn/150-one.jpg")


class PreviewBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await preview_service.shutdown_preview_warmups()
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "previews.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.session() as session:
            missing = Model(
                ph_id="missing", profile_type="pornstar", profile_slug="missing",
                name="Missing", url="https://cn.pornhub.com/pornstar/missing",
            )
            ready = Model(
                ph_id="ready", profile_type="pornstar", profile_slug="ready",
                name="Ready", url="https://cn.pornhub.com/pornstar/ready",
            )
            session.add_all([missing, ready])
            await session.flush()
            session.add(ModelPreviewVideo(
                model_id=ready.id, ordinal=0, viewkey="ready1234567",
                title="Ready", url="https://example/ready",
                thumbnail="https://cdn.example/ready.jpg", source="video",
            ))
            await session.commit()
            self.missing_id = missing.id
            self.ready_id = ready.id

    async def asyncTearDown(self):
        await preview_service.shutdown_preview_warmups()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_backfill_only_scrapes_models_with_no_materialized_preview(self):
        videos = [{
            "viewkey": "fresh1234567", "title": "Fresh",
            "url": "https://example/fresh", "duration": 120,
            "thumbnail": "https://cdn.example/fresh.jpg",
        }]
        scrape = AsyncMock(return_value=videos)
        with patch.object(scraper, "scrape_model_videos", new=scrape):
            result = await backfill_profile_previews(
                [self.missing_id, self.ready_id], session_factory=self.session)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["filled_models"], 1)
        self.assertEqual(result["added"], 1)
        scrape.assert_awaited_once_with(
            "https://cn.pornhub.com/pornstar/missing",
            max_videos=4,
            include_playlist=False,
        )
        async with self.session() as session:
            rows = (await session.execute(select(ModelPreviewVideo).where(
                ModelPreviewVideo.model_id == self.missing_id
            ))).scalars().all()
        self.assertEqual([(row.viewkey, row.source) for row in rows], [
            ("fresh1234567", "profile_preview"),
        ])

    async def test_warmup_continues_when_one_image_is_unavailable(self):
        async with self.session() as session:
            session.add(ModelPreviewVideo(
                model_id=self.missing_id, ordinal=0, viewkey="broken123456",
                title="Broken", url="https://example/broken",
                thumbnail="https://cdn.example/broken.jpg", source="video",
            ))
            await session.commit()

        async def ensure(viewkey):
            if viewkey == "broken123456":
                raise preview_image_store.PreviewImageUnavailable("gone")
            return Path(self.temp.name) / f"{viewkey}.webp"

        with patch.object(preview_image_store, "preview_path", return_value=None), \
                patch.object(
                    preview_image_store, "ensure_preview_image",
                    new=AsyncMock(side_effect=ensure),
                ):
            result = await warm_preview_images(
                [self.missing_id, self.ready_id], session_factory=self.session)
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)

    async def test_profile_preview_backfill_has_finite_per_model_timeout(self):
        blocker = asyncio.Event()

        async def never_finishes(*_args, **_kwargs):
            await blocker.wait()

        with patch.object(
                scraper, "scrape_model_videos", side_effect=never_finishes), \
                patch.object(
                    preview_service, "_PROFILE_PREVIEW_TIMEOUT_SECONDS", .01):
            result = await backfill_profile_previews(
                [self.missing_id], session_factory=self.session)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["filled_models"], 0)
        self.assertEqual(result["failed"], 1)

    async def test_schedule_backfill_coalesces_refreshes_and_cools_attempts(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def ensure(ids, **_kwargs):
            started.set()
            await release.wait()
            return len(ids)

        ensure_mock = AsyncMock(side_effect=ensure)
        profile_mock = AsyncMock(return_value={
            "attempted": 0, "filled_models": 0, "added": 0, "failed": 0,
        })
        with patch.object(preview_service, "ensure_previews", new=ensure_mock), \
                patch.object(
                    preview_service, "backfill_profile_previews", new=profile_mock):
            self.assertTrue(preview_service.schedule_preview_backfill(
                [self.missing_id, self.ready_id], session_factory=self.session))
            await started.wait()
            running = preview_service._preview_backfill_task

            # 页面再次刷新只把新ID并入同一个任务，不复制正在处理的ID。
            self.assertTrue(preview_service.schedule_preview_backfill(
                [self.missing_id, self.ready_id, 999], session_factory=self.session))
            self.assertIs(preview_service._preview_backfill_task, running)

            release.set()
            await running
            self.assertEqual(ensure_mock.await_count, 2)
            attempted = {
                model_id
                for call in ensure_mock.await_args_list
                for model_id in call.args[0]
            }
            self.assertEqual(
                attempted, {self.missing_id, self.ready_id, 999})

            # 同一进程内短时间刷新不重新抓取刚成功或失败过的模特。
            self.assertFalse(preview_service.schedule_preview_backfill(
                [self.missing_id, self.ready_id, 999], session_factory=self.session))

    async def test_schedule_warmup_merges_repeated_page_requests(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[list[int]] = []

        async def warm(ids, **_kwargs):
            calls.append(list(ids))
            if len(calls) == 1:
                started.set()
                await release.wait()
            return {"requested": len(ids), "completed": len(ids),
                    "cached": 0, "failed": 0}

        with patch.object(
                preview_service, "warm_preview_images",
                new=AsyncMock(side_effect=warm)):
            self.assertTrue(preview_service.schedule_preview_warmup(
                [self.missing_id], session_factory=self.session))
            await started.wait()
            running = preview_service._preview_warmup_task
            self.assertTrue(preview_service.schedule_preview_warmup(
                [self.missing_id, self.ready_id], session_factory=self.session))
            self.assertIs(preview_service._preview_warmup_task, running)
            release.set()
            await running

        self.assertEqual(calls[0], [self.missing_id])
        self.assertEqual(calls[1], [self.ready_id])


if __name__ == "__main__":
    unittest.main()
