import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy.exc import OperationalError

from backend.downloader import (
    DownloadArtifactError,
    DownloadEnvironmentError,
    DownloadError,
    DownloadIntegrityError,
    MediaToolsStatus,
    DownloadProcessingError,
    NetworkDownloadError,
)
from backend.task_manager import (
    MEDIA_TOOLS_WAITING_PREFIX,
    NETWORK_RETRY_DELAYS,
    TaskManager,
    _select_output_path_for_task,
    download_space_available,
    select_download_dir,
)
from backend.scraper import HLSPlanError, HLSSourceUnavailableError, VideoUnavailableError
from backend.network_health import LinkHealth

_real_sleep = asyncio.sleep


def make_plan(
    url: str,
    *,
    duration: float = 120.0,
    quality: int = 1080,
) -> dict:
    return {
        "schema_version": 2,
        "m3u8_url": url,
        "manifest_text": (
            "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10,\nsegment.ts\n#EXT-X-ENDLIST\n"
        ),
        "fingerprint": "a" * 64,
        "segment_count": 1,
        "unit_count": 1,
        "media_duration": duration,
        "target_duration": 10.0,
        "media_sequence": 0,
        "quality": quality,
        "expect_audio": True,
    }


def resolved(
    url: str,
    *,
    duration: float = 120.0,
    quality: int = 1080,
) -> dict:
    plan = make_plan(url, duration=duration, quality=quality)
    return {"m3u8_url": url, "quality": quality, "hls_plan": plan}


class TaskManagerDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 单元测试不触网：链路探测固定为可用，网络失败按原有计次策略处理。
        self.link_probe = AsyncMock(return_value=True)
        self.manager = TaskManager(link_health=LinkHealth(probe=self.link_probe))
        self.task = SimpleNamespace(
            id=1,
            url="https://example.invalid/view",
            model_name="Model",
            title="Title",
            viewkey="abc123",
            duration=120,
        )
        self.status_calls = []

        async def fake_set_status(task_id, **fields):
            self.status_calls.append((task_id, dict(fields)))
            if fields.get("_return_task"):
                return self.task
            return None

        self.manager._set_status = fake_set_status
        self.manager._create_upload_task = AsyncMock()

        def discard_background(coro):
            coro.close()

        self.manager._spawn_bg = discard_background

    def test_download_space_reserve_blocks_new_downloads(self):
        from backend.config import settings

        old_reserve = settings.download_min_free_space_gb
        old_fallbacks = settings.download_fallback_dirs
        try:
            settings.download_min_free_space_gb = 20
            settings.download_fallback_dirs = ""
            with patch("backend.task_manager.os.makedirs"), \
                    patch("backend.task_manager.shutil.disk_usage",
                          return_value=SimpleNamespace(free=19 * 1024 ** 3)):
                self.assertFalse(download_space_available())
            with patch("backend.task_manager.os.makedirs"), \
                    patch("backend.task_manager.shutil.disk_usage",
                          return_value=SimpleNamespace(free=20 * 1024 ** 3)):
                self.assertTrue(download_space_available())
        finally:
            settings.download_min_free_space_gb = old_reserve
            settings.download_fallback_dirs = old_fallbacks

    def test_download_space_automatically_selects_fallback(self):
        from backend.config import settings

        old_dir = settings.download_dir
        old_fallbacks = settings.download_fallback_dirs
        old_reserve = settings.download_min_free_space_gb
        try:
            # 用路径前缀区分两块“磁盘”，不依赖 Windows 盘符，Linux/macOS 上同样成立。
            primary = os.path.abspath(os.path.join("ph115-disk-a", "primary-downloads"))
            fallback = os.path.abspath(os.path.join("ph115-disk-b", "fallback-downloads"))
            settings.download_dir = primary
            settings.download_fallback_dirs = fallback
            settings.download_min_free_space_gb = 20

            def disk_usage(path):
                on_primary = os.path.normcase(os.path.abspath(path)).startswith(
                    os.path.normcase(primary))
                free_gb = 19 if on_primary else 100
                return SimpleNamespace(free=free_gb * 1024 ** 3)

            with patch("backend.task_manager.os.makedirs"), patch(
                    "backend.task_manager.shutil.disk_usage", side_effect=disk_usage):
                selected = select_download_dir()

            self.assertEqual(
                os.path.normcase(selected or ""),
                os.path.normcase(fallback),
            )
        finally:
            settings.download_dir = old_dir
            settings.download_fallback_dirs = old_fallbacks
            settings.download_min_free_space_gb = old_reserve

    def test_existing_partial_is_not_split_across_disks(self):
        from backend.config import settings

        old_dir = settings.download_dir
        old_fallbacks = settings.download_fallback_dirs
        old_reserve = settings.download_min_free_space_gb
        with tempfile.TemporaryDirectory() as directory:
            primary = os.path.join(directory, "primary")
            fallback = os.path.join(directory, "fallback")
            task = SimpleNamespace(
                model_name="Model", title="Title", viewkey="abc123",
                artifact_scope="", local_path="", downloaded_bytes=0,
            )
            output = os.path.join(primary, "Model", "abc123.mp4")
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(os.path.splitext(output)[0] + ".native-v2.mp4.part", "wb") as handle:
                handle.write(b"partial-media")

            try:
                settings.download_dir = primary
                settings.download_fallback_dirs = fallback
                settings.download_min_free_space_gb = 20

                def disk_usage(path):
                    is_primary = os.path.commonpath([
                        os.path.abspath(path), os.path.abspath(primary),
                    ]) == os.path.abspath(primary)
                    return SimpleNamespace(
                        free=(19 if is_primary else 100) * 1024 ** 3)

                with patch(
                        "backend.task_manager.shutil.disk_usage",
                        side_effect=disk_usage):
                    selected = _select_output_path_for_task(task)
                self.assertIsNone(selected)
            finally:
                settings.download_dir = old_dir
                settings.download_fallback_dirs = old_fallbacks
                settings.download_min_free_space_gb = old_reserve

    async def test_partial_websocket_payload_contains_structured_telemetry(self):
        broadcast = AsyncMock()
        self.manager.set_broadcast(broadcast)
        stamp = datetime.now(timezone.utc)

        await self.manager._notify_fields(1, {
            "phase": "downloading",
            "speed_bps": 2048.0,
            "downloaded_bytes": 4096,
            "total_bytes": 8192,
            "attempt": 1,
            "last_progress_at": stamp,
        }, "download")

        payload = broadcast.await_args.args[0]["task"]
        self.assertEqual(payload["task_type"], "download")
        self.assertEqual(payload["speed_bps"], 2048.0)
        self.assertEqual(payload["last_progress_at"], stamp.isoformat())

    async def test_progress_is_broadcast_live_but_persisted_periodically(self):
        self.manager._task_store.update = AsyncMock()
        self.manager._notify_fields = AsyncMock()

        with patch("backend.task_manager.time.monotonic", side_effect=[10.0, 11.0, 15.0]):
            await self.manager._set_progress(1, {"phase": "downloading", "progress": 1}, "download")
            await self.manager._set_progress(1, {"phase": "downloading", "progress": 2}, "download")
            await self.manager._set_progress(1, {"phase": "downloading", "progress": 3}, "download")

        self.assertEqual(self.manager._notify_fields.await_count, 3)
        self.assertEqual(self.manager._task_store.update.await_count, 2)
        persisted = self.manager._task_store.update.await_args.args[1]
        self.assertEqual(persisted["progress"], 3)

    async def test_progress_lock_does_not_abort_active_download(self):
        locked = OperationalError("UPDATE tasks", {}, Exception("database is locked"))
        self.manager._task_store.update = AsyncMock(side_effect=locked)
        self.manager._notify_fields = AsyncMock()

        await self.manager._set_progress(
            1,
            {"phase": "downloading", "progress": 1},
            "download",
        )

        self.manager._notify_fields.assert_awaited_once()
        self.assertNotIn(1, self.manager._progress_persisted_at)

    async def test_retry_refreshes_signed_m3u8_url(self):
        resolver = AsyncMock(side_effect=[
            resolved("https://cdn.invalid/one.m3u8"),
            resolved("https://cdn.invalid/two.m3u8"),
        ])
        download = AsyncMock(side_effect=[NetworkDownloadError("断流"), "done"])
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(
            [call.args[0]["m3u8_url"] for call in download.await_args_list],
            ["https://cdn.invalid/one.m3u8", "https://cdn.invalid/two.m3u8"],
        )
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))
        self.manager._create_upload_task.assert_awaited_once_with(1)

    async def test_corrupt_staging_gets_one_single_connection_clean_retry(self):
        resolver = AsyncMock(side_effect=[
            resolved("https://cdn.invalid/one.m3u8"),
            resolved("https://cdn.invalid/two.m3u8"),
        ])
        download = AsyncMock(side_effect=[
            DownloadArtifactError("下载 staging 的视频时间戳倒退 80s"),
            "done",
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 2)
        self.assertFalse(download.await_args_list[0].kwargs["clean_start"])
        self.assertTrue(download.await_args_list[1].kwargs["clean_start"])
        self.assertFalse(download.await_args_list[0].kwargs["conservative"])
        self.assertTrue(download.await_args_list[1].kwargs["conservative"])
        self.assertTrue(any(
            fields.get("status") == "completed" for _, fields in self.status_calls
        ))

    async def test_repeated_artifact_error_falls_back_one_quality(self):
        resolver = AsyncMock(side_effect=[
            resolved("https://cdn.invalid/1080-first.m3u8"),
            resolved("https://cdn.invalid/1080-clean.m3u8"),
            resolved("https://cdn.invalid/720-clean.m3u8", quality=720),
        ])
        download = AsyncMock(side_effect=[
            DownloadArtifactError("下载 staging 的视频时间戳倒退 80s"),
            DownloadArtifactError("下载 staging 的视频时间戳倒退 80s"),
            "done",
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 3)
        self.assertEqual(
            [call.kwargs["clean_start"] for call in download.await_args_list],
            [False, True, True],
        )
        self.assertEqual(resolver.await_args_list[2].kwargs, {"max_quality": 1079})
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_artifact_error_stops_after_single_quality_fallback(self):
        async def resolve(_url, *, max_quality=None):
            quality = 720 if max_quality is not None else 1080
            return resolved(
                f"https://cdn.invalid/{quality}.m3u8", quality=quality)

        resolver = AsyncMock(side_effect=resolve)
        download = AsyncMock(side_effect=DownloadArtifactError(
            "下载 staging 的视频时间戳倒退 80s"))
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 3)
        self.assertEqual(resolver.await_count, 3)
        self.assertTrue(any(
            fields.get("status") == "failed"
            and fields.get("phase") == "failed_clean_retry"
            for _, fields in self.status_calls
        ))

    async def test_persisted_quality_fallback_resumes_clean_at_cap(self):
        self.task.phase = "queued_quality_retry:1079"
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/720.m3u8", quality=720))
        download = AsyncMock(return_value="done")
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096):
            await self.manager._run_download(1)

        resolver.assert_awaited_once_with(
            self.task.url, max_quality=1079)
        self.assertTrue(download.await_args.kwargs["clean_start"])
        self.assertTrue(download.await_args.kwargs["conservative"])
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_persisted_clean_resume_keeps_new_partial(self):
        self.task.phase = "queued_clean_resume"
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/1080.m3u8", quality=1080))
        download = AsyncMock(return_value="done")
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096):
            await self.manager._run_download(1)

        self.assertFalse(download.await_args.kwargs["clean_start"])
        self.assertTrue(download.await_args.kwargs["conservative"])
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_persisted_quality_resume_keeps_partial_and_cap(self):
        self.task.phase = "queued_quality_resume:1079"
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/720.m3u8", quality=720))
        download = AsyncMock(return_value="done")
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096):
            await self.manager._run_download(1)

        resolver.assert_awaited_once_with(self.task.url, max_quality=1079)
        self.assertFalse(download.await_args.kwargs["clean_start"])
        self.assertTrue(download.await_args.kwargs["conservative"])
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_pause_after_clean_attempt_started_resumes_without_cleanup(self):
        self.task.phase = "queued_clean_retry"
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/1080.m3u8", quality=1080))
        download = AsyncMock(side_effect=asyncio.CancelledError())
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._run_download(1)

        self.assertTrue(download.await_args.kwargs["clean_start"])
        self.assertTrue(download.await_args.kwargs["conservative"])
        self.assertTrue(any(
            fields.get("status") == "paused"
            and fields.get("phase") == "paused_clean_resume"
            for _, fields in self.status_calls
        ))

    async def test_historical_clean_retry_can_use_remaining_second_fallback(self):
        self.task.phase = "queued_clean_retry"
        resolver = AsyncMock(side_effect=[
            resolved("https://cdn.invalid/1080.m3u8", quality=1080),
            resolved("https://cdn.invalid/720.m3u8", quality=720),
            resolved("https://cdn.invalid/480.m3u8", quality=480),
        ])
        download = AsyncMock(side_effect=[
            DownloadArtifactError("1080p 源分片损坏"),
            DownloadArtifactError("720p 源分片损坏"),
            "done",
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(
            [call.kwargs for call in resolver.await_args_list],
            [{}, {"max_quality": 1079}, {"max_quality": 719}],
        )
        self.assertTrue(all(
            call.kwargs["clean_start"] for call in download.await_args_list
        ))
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_worker_exit_race_during_pause_is_not_recorded_as_failure(self):
        self.manager._download_pause_requests.add(1)
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=DownloadError(
            "yt-dlp 子进程异常退出(code=4294967295)，未返回完成事件"))
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download):
            await self.manager._run_download(1)

        self.assertTrue(any(
            fields.get("status") == "paused"
            and fields.get("error") == ""
            for _, fields in self.status_calls
        ))
        self.assertFalse(any(
            fields.get("status") == "failed"
            for _, fields in self.status_calls
        ))
        self.assertNotIn(1, self.manager._download_pause_requests)

    async def test_download_uses_persisted_fallback_path(self):
        assigned = os.path.abspath("F:/fallback/Model-1/abc123.mp4")
        self.task.local_path = assigned
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8"))
        download = AsyncMock(return_value=assigned)
        with patch("backend.task_manager.get_best_m3u8", resolver), patch(
                "backend.task_manager.build_output_path") as build_path, patch(
                "backend.task_manager.downloader.download", download), patch(
                "backend.task_manager.os.path.getsize", return_value=4096):
            await self.manager._run_download(1)

        build_path.assert_not_called()
        self.assertEqual(download.await_args.args[1], assigned)
        self.assertTrue(any(
            fields.get("status") == "completed"
            and fields.get("local_path") == assigned
            for _, fields in self.status_calls
        ))

    async def test_complete_attempt_releases_slot_during_retry_backoff(self):
        resolver = AsyncMock(side_effect=[
            resolved("https://cdn.invalid/one.m3u8"),
            resolved("https://cdn.invalid/two.m3u8"),
        ])
        download = AsyncMock(side_effect=[NetworkDownloadError("永久断流"), "done"])
        sleep_started = asyncio.Event()
        resume_retry = asyncio.Event()
        initial_slots = self.manager._download_sem._value

        async def controlled_sleep(_delay):
            sleep_started.set()
            await resume_retry.wait()

        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=controlled_sleep):
            running = asyncio.create_task(self.manager._run_download(1))
            self.manager._running[1] = running
            await asyncio.wait_for(sleep_started.wait(), timeout=1)

            self.assertNotIn(1, self.manager._running)
            self.assertIs(self.manager._download_retry_waiters.get(1), running)
            self.assertEqual(self.manager._download_sem._value, initial_slots)

            resume_retry.set()
            await asyncio.wait_for(running, timeout=1)

        self.assertEqual(download.await_count, 2)
        self.assertTrue(any(
            fields.get("status") == "completed"
            for _, fields in self.status_calls
        ))

    async def test_final_download_failure_is_not_silently_requeued(self):
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=NetworkDownloadError("永久失败"))
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()), \
                patch.object(self.manager, "_schedule_auto_requeue") as requeue:
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 3)
        self.assertEqual(download.await_count, 3)
        requeue.assert_not_called()
        self.assertTrue(any(
            fields.get("status") == "failed" and fields.get("phase") == "failed"
            for _, fields in self.status_calls
        ))

    async def test_network_retries_back_off_long_enough_to_ride_out_link_drops(self):
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=NetworkDownloadError(
            "[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING]"))
        sleep = AsyncMock()
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.asyncio.sleep", new=sleep):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list],
                         list(NETWORK_RETRY_DELAYS))

    def use_link(self, probe):
        async def tick(_seconds):
            await _real_sleep(0)
        health = LinkHealth(probe=probe, sleep=tick)
        health.add_listener(self.manager._on_link_event)
        self.manager._link_health = health
        return health

    async def test_shared_link_outage_retries_same_attempt_without_spending_it(self):
        probes = []

        async def probe(target):
            probes.append(target.url)
            return len(probes) > 1          # 首次探测断流，第一次复查即恢复

        health = self.use_link(probe)
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=[NetworkDownloadError(
            "[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING]"), "done"])
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.os.path.getsize", return_value=4096),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.manager._run_download(1)

        # 失败发生在媒体下载阶段，探测走媒体主机；整个过程没有网络退避计次。
        self.assertEqual(probes[0], "https://cdn.invalid/")
        sleep.assert_not_awaited()
        waits = [fields for _, fields in self.status_calls if fields.get("phase") == "network_wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["attempt"], 1)
        self.assertIn("不计次", waits[0]["error"])
        attempts = [fields["attempt"] for _, fields in self.status_calls
                    if fields.get("phase") == "resolving" and "attempt" in fields]
        self.assertEqual(attempts, [1, 1])
        self.assertTrue(any(fields.get("status") == "completed" for _, fields in self.status_calls))
        self.assertFalse(health.outage_active)
        await health.close()

    async def test_repeated_outages_never_fail_the_task(self):
        answers = iter([False, True] * 3)
        health = self.use_link(AsyncMock(side_effect=lambda _target: next(answers)))
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        failure = NetworkDownloadError("[download] Got error: timed out")
        download = AsyncMock(side_effect=[failure, failure, failure, "done"])
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.os.path.getsize", return_value=4096),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 4)
        self.assertFalse(any(fields.get("status") == "failed" for _, fields in self.status_calls))
        self.assertTrue(any(fields.get("status") == "completed" for _, fields in self.status_calls))
        await health.close()

    async def test_outage_longer_than_budget_falls_back_to_counted_retries(self):
        health = self.use_link(AsyncMock(return_value=False))
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=NetworkDownloadError("[download] Got error: timed out"))
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.NETWORK_OUTAGE_MAX_WAIT_SECONDS", 0.05),                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(download.await_count, 3)
        self.assertTrue(any(fields.get("status") == "failed" for _, fields in self.status_calls))
        self.assertEqual(self.manager.download_blocked_reason()["code"], "network_outage")
        await health.close()

    async def test_source_410_is_not_treated_as_link_outage(self):
        probe = AsyncMock(return_value=False)
        health = self.use_link(probe)
        resolver = AsyncMock(side_effect=HLSSourceUnavailableError("HLS 清单请求返回 HTTP 410"))
        with patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)
        probe.assert_not_awaited()
        self.assertFalse(health.outage_active)

    async def test_missing_media_tool_requeues_instead_of_failing(self):
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=DownloadEnvironmentError(
            "未找到 ffprobe，无法校验下载成品"))
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(
            ok=False, missing=("ffprobe",))
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        # 环境错误不消耗网络重试，也不落成失败；断点交给工具恢复后的下一次调度。
        self.assertEqual(download.await_count, 1)
        final = self.status_calls[-1][1]
        self.assertEqual(final["status"], "queued")
        self.assertTrue(final["error"].startswith(MEDIA_TOOLS_WAITING_PREFIX))
        self.assertFalse(any(
            fields.get("status") == "failed" for _, fields in self.status_calls))
        self.assertIsNotNone(self.manager.download_blocked_reason())

    async def test_environment_error_with_working_tools_still_fails(self):
        resolver = AsyncMock(return_value=resolved("https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=DownloadEnvironmentError("[WinError 112] 磁盘空间不足"))
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)
        with tempfile.TemporaryDirectory() as directory,                 patch("backend.task_manager.get_best_m3u8", resolver),                 patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"),                 patch("backend.task_manager.downloader.download", download),                 patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(self.status_calls[-1][1]["status"], "failed")

    async def test_deterministic_integrity_error_is_not_redownloaded(self):
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=DownloadIntegrityError("清单时间轴不一致"))
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 1)
        self.assertEqual(download.await_count, 1)
        self.assertFalse(any(
            fields.get("phase") == "retrying" for _, fields in self.status_calls
        ))

    async def test_processing_error_retries_only_local_staging(self):
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8"))
        download = AsyncMock(side_effect=[
            DownloadProcessingError("文件暂时占用"),
            "done",
        ])
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 1)
        self.assertEqual(download.await_count, 2)
        self.assertNotIn("allow_network", download.await_args_list[0].kwargs)
        self.assertFalse(download.await_args_list[1].kwargs["allow_network"])
        self.assertTrue(any(
            fields.get("status") == "completed" for _, fields in self.status_calls
        ))

    async def test_page_duration_is_not_passed_as_download_truth(self):
        self.task.duration = 665
        resolver = AsyncMock(return_value=resolved(
            "https://cdn.invalid/video.m3u8", duration=625.24))
        download = AsyncMock(return_value="done")
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.os.path.getsize", return_value=4096):
            await self.manager._run_download(1)

        plan = download.await_args.args[0]
        self.assertEqual(plan["media_duration"], 625.24)
        self.assertNotEqual(plan["media_duration"], self.task.duration)

    async def test_resolve_timeout_is_the_only_resolver_failure_retried(self):
        resolver = AsyncMock(side_effect=TimeoutError("slow resolver"))
        download = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 3)
        download.assert_not_awaited()
        self.assertEqual(sum(
            fields.get("phase") == "retrying" for _, fields in self.status_calls
        ), 2)

    async def test_deterministic_hls_plan_error_is_not_retried(self):
        resolver = AsyncMock(side_effect=HLSPlanError("missing ENDLIST"))
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 1)
        self.assertFalse(any(
            fields.get("phase") == "retrying" for _, fields in self.status_calls
        ))

    async def test_unavailable_video_is_skipped_without_retry(self):
        resolver = AsyncMock(side_effect=VideoUnavailableError("此视频已下架"))
        download = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.task_manager.get_best_m3u8", resolver), \
                patch("backend.task_manager.build_output_path",
                      return_value=f"{directory}/abc123.mp4"), \
                patch("backend.task_manager.downloader.download", download), \
                patch("backend.task_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager._run_download(1)

        self.assertEqual(resolver.await_count, 1)
        download.assert_not_awaited()
        self.assertTrue(any(
            fields.get("status") == "skipped"
            and fields.get("phase") == "skipped"
            and "此视频已下架" in fields.get("error", "")
            for _, fields in self.status_calls
        ))
        self.assertFalse(any(
            fields.get("phase") == "retrying" for _, fields in self.status_calls
        ))
        self.manager._create_upload_task.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
