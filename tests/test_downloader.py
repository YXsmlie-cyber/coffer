import asyncio
import os
import io
import json
import queue
import tempfile
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from backend.scraper import InvalidScrapeURL

from backend.downloader import (
    DownloadArtifactError,
    DownloadEnvironmentError,
    DownloadError,
    DownloadIntegrityError,
    DownloadProcessingError,
    NetworkDownloadError,
    M3U8Downloader,
    _acquire_output_lock,
    build_remote_filename,
    _classify_ytdlp_download_error,
    _ffmpeg_failure,
    has_partial_artifacts,
    download_concurrency_limit,
    fragment_workers_for_attempt,
    _retry_delay,
    _rolling_speed,
    _legacy_staging_path,
    _mark_resume_safe,
    _plan_sidecar_path,
    _prepare_plan_state,
    _processing_watchdog_for_artifacts,
    _release_output_lock,
    _restrict_request_director_to_urllib,
    _staging_path,
    _scan_packet_timelines,
    _validate_staging_with_ffprobe,
    _validate_with_ffprobe,
    _validated_plan,
    _validate_worker_media_url,
    _yt_dlp_worker,
)
from backend.download_errors import (
    classify_stored_download_failure,
    is_retryable_stored_download_failure,
)


def _hls_plan(fingerprint: str = "a" * 64) -> dict:
    return {
        "schema_version": 2,
        "m3u8_url": "https://cdn.example.test/hls/video.m3u8?token=short-lived",
        "manifest_text": (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXTINF:6.0,\n"
            "segment-001.ts\n"
            "#EXT-X-ENDLIST\n"
        ),
        "fingerprint": fingerprint,
        "segment_count": 1,
        "unit_count": 1,
        "media_duration": 6.0,
        "target_duration": 6.0,
        "quality": 1080,
        "expect_audio": True,
    }


class DownloaderUtilityTests(unittest.TestCase):
    def test_remote_filename_strips_edge_dots_and_spaces(self):
        self.assertEqual(
            build_remote_filename(" . G A M E . O V E R . ", "68975e3f7e78e"),
            "G A M E . O V E R [68975e3f7e78e].mp4",
        )
        self.assertEqual(
            build_remote_filename("...", "viewkey"),
            "video [viewkey].mp4",
        )

    def test_global_connection_budget_is_split_across_tasks(self):
        from backend.config import settings

        old_budget = settings.download_max_connections
        old_tasks = settings.max_concurrent_downloads
        old_cap = settings.download_max_fragment_workers_per_task
        try:
            settings.max_concurrent_downloads = 5
            settings.download_max_fragment_workers_per_task = 12
            expected = {15: 2, 30: 5, 40: 7, 50: 9, 60: 11}
            for budget, workers in expected.items():
                with self.subTest(budget=budget):
                    settings.download_max_connections = budget
                    self.assertEqual(download_concurrency_limit(), 5)
                    self.assertEqual(fragment_workers_for_attempt(1), workers)
                    self.assertEqual(
                        M3U8Downloader._config(1)["connections"], workers)
        finally:
            settings.download_max_connections = old_budget
            settings.max_concurrent_downloads = old_tasks
            settings.download_max_fragment_workers_per_task = old_cap

    def test_fragment_workers_step_down_across_complete_attempts(self):
        from backend.config import settings

        old_budget = settings.download_max_connections
        old_tasks = settings.max_concurrent_downloads
        old_cap = settings.download_max_fragment_workers_per_task
        try:
            settings.download_max_connections = 60
            settings.max_concurrent_downloads = 5
            settings.download_max_fragment_workers_per_task = 12
            self.assertEqual(
                [fragment_workers_for_attempt(n) for n in (1, 2, 3, 4)],
                [11, 6, 2, 2],
            )

            settings.download_max_fragment_workers_per_task = 4
            self.assertEqual(fragment_workers_for_attempt(1), 4)
            self.assertEqual(fragment_workers_for_attempt(2), 2)
            self.assertEqual(fragment_workers_for_attempt(3), 2)
        finally:
            settings.download_max_connections = old_budget
            settings.max_concurrent_downloads = old_tasks
            settings.download_max_fragment_workers_per_task = old_cap

    def test_conservative_retry_forces_one_fragment_and_keeps_resume(self):
        config = M3U8Downloader._config(1, conservative=True)
        self.assertEqual(config["connections"], 1)
        self.assertTrue(config["continuedl"])

    def test_rolling_speed_uses_recent_byte_delta(self):
        samples = deque([(0.0, 0), (5.0, 5000)])
        self.assertEqual(_rolling_speed(samples, 5.0, 5000), 1000)

    def test_fragment_retry_delays_accept_ytdlp_keyword(self):
        self.assertEqual([_retry_delay(n=n) for n in range(6)], [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])
        # 默认重试次数下累计退避要能覆盖实测 4–16 秒的代理短断流。
        from backend.config import Settings
        retries = Settings.model_fields["download_fragment_retries"].default
        self.assertGreaterEqual(sum(_retry_delay(n=n) for n in range(retries)), 20.0)

    def test_cleanup_partials_keeps_completed_output(self):
        downloader = M3U8Downloader()
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            staging = _staging_path(output)
            root, _ = os.path.splitext(output)
            paths = [
                output,
                staging + ".part",
                staging + ".ytdl",
                staging + ".part-Frag1",
                root + ".native.temp.mp4",
                root + ".processing.mp4.invalid-123",
                output + ".part",
            ]
            for path in paths:
                with open(path, "wb") as handle:
                    handle.write(b"x")

            downloader.cleanup_partials(output)

            self.assertTrue(os.path.exists(output))
            for path in paths[1:]:
                self.assertFalse(os.path.exists(path), path)

    def test_partial_detection_ignores_metadata_without_media(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            staging = _staging_path(output)
            with open(staging + ".hls-plan.json", "w", encoding="utf-8") as handle:
                handle.write("{}")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.assertFalse(has_partial_artifacts(output))

            with open(staging + ".part", "wb") as handle:
                handle.write(b"media")
            self.assertTrue(has_partial_artifacts(output))

    def test_matching_plan_sidecar_reuses_native_v2_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            plan = _hls_plan()

            self.assertFalse(_prepare_plan_state(output, plan))
            staging = _staging_path(output)
            with open(staging + ".part", "wb") as handle:
                handle.write(b"partial")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                json.dump({
                    "downloader": {"current_fragment": {"index": 1}},
                }, handle)

            self.assertTrue(_mark_resume_safe(output, plan))
            self.assertTrue(_prepare_plan_state(output, plan))
            self.assertTrue(os.path.isfile(staging + ".part"))
            self.assertTrue(os.path.isfile(staging + ".ytdl"))
            self.assertTrue(os.path.isfile(_plan_sidecar_path(output)))

    def test_resume_discards_only_incomplete_concurrent_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            plan = _hls_plan()
            plan["manifest_text"] = (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6.0,\nsegment-001.ts\n"
                "#EXTINF:6.0,\nsegment-002.ts\n"
                "#EXTINF:6.0,\nsegment-003.ts\n"
                "#EXT-X-ENDLIST\n"
            )
            plan["segment_count"] = 3
            plan["unit_count"] = 3
            plan["media_duration"] = 18.0
            _prepare_plan_state(output, plan)

            staging = _staging_path(output)
            main_part = staging + ".part"
            completed_fragment = staging + ".part-Frag2"
            incomplete_fragment = staging + ".part-Frag3.part"
            for path, value in (
                (main_part, b"complete-prefix"),
                (completed_fragment, b"complete-fragment"),
                (incomplete_fragment, b"partial-fragment"),
            ):
                with open(path, "wb") as handle:
                    handle.write(value)
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                json.dump({
                    "downloader": {"current_fragment": {"index": 1}},
                }, handle)

            self.assertTrue(_mark_resume_safe(output, plan))
            self.assertTrue(os.path.isfile(main_part))
            self.assertTrue(os.path.isfile(completed_fragment))
            self.assertFalse(os.path.exists(incomplete_fragment))
            self.assertTrue(_prepare_plan_state(output, plan))
            self.assertTrue(os.path.isfile(main_part))
            self.assertTrue(os.path.isfile(completed_fragment))

    def test_changed_plan_discards_only_v2_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            first_plan = _hls_plan("a" * 64)
            second_plan = _hls_plan("b" * 64)
            _prepare_plan_state(output, first_plan)

            staging = _staging_path(output)
            legacy = _legacy_staging_path(output) + ".part"
            for path in (staging + ".part", legacy):
                with open(path, "wb") as handle:
                    handle.write(b"partial")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                json.dump({
                    "downloader": {"current_fragment": {"index": 1}},
                }, handle)

            self.assertFalse(_prepare_plan_state(output, second_plan))
            self.assertFalse(os.path.exists(staging + ".part"))
            self.assertFalse(os.path.exists(staging + ".ytdl"))
            self.assertTrue(os.path.isfile(legacy))
            with open(_plan_sidecar_path(output), encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["fingerprint"], "b" * 64)

    def test_delete_failure_never_rebinds_old_partial_to_new_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            first_plan = _hls_plan("a" * 64)
            second_plan = _hls_plan("b" * 64)
            _prepare_plan_state(output, first_plan)
            with open(_staging_path(output) + ".part", "wb") as handle:
                handle.write(b"locked-old-partial")

            with patch("backend.downloader.Path.unlink",
                       side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(DownloadIntegrityError, "拒绝把旧断点绑定"):
                    _prepare_plan_state(output, second_plan)

            with open(_plan_sidecar_path(output), encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["fingerprint"], "a" * 64)

    def test_corrupt_ytdl_state_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            plan = _hls_plan()
            _prepare_plan_state(output, plan)
            staging = _staging_path(output)
            with open(staging + ".part", "wb") as handle:
                handle.write(b"partial")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                handle.write("not-json")

            self.assertFalse(_prepare_plan_state(output, plan))
            self.assertFalse(os.path.exists(staging + ".part"))
            self.assertFalse(os.path.exists(staging + ".ytdl"))

    def test_resume_without_sidecar_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            staging = _staging_path(output)
            with open(staging + ".part", "wb") as handle:
                handle.write(b"unbound-partial")

            self.assertFalse(_prepare_plan_state(output, _hls_plan()))
            self.assertFalse(os.path.exists(staging + ".part"))
            self.assertTrue(os.path.isfile(_plan_sidecar_path(output)))

    def test_dirty_unit_count_in_sidecar_is_discarded_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            plan = _hls_plan()
            _prepare_plan_state(output, plan)
            staging = _staging_path(output)
            with open(staging + ".part", "wb") as handle:
                handle.write(b"partial")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                json.dump({
                    "downloader": {"current_fragment": {"index": 1}},
                }, handle)
            with open(_plan_sidecar_path(output), "r+", encoding="utf-8") as handle:
                sidecar = json.load(handle)
                sidecar["unit_count"] = "bad"
                handle.seek(0)
                json.dump(sidecar, handle)
                handle.truncate()

            self.assertFalse(_prepare_plan_state(output, plan))
            self.assertFalse(os.path.exists(staging + ".part"))
            self.assertFalse(os.path.exists(staging + ".ytdl"))

    def test_map_playlist_partial_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            plan = _hls_plan()
            plan["manifest_text"] = (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:6.0,\nsegment-001.m4s\n#EXT-X-ENDLIST\n"
            )
            plan["unit_count"] = 2
            _prepare_plan_state(output, plan)
            staging = _staging_path(output)
            with open(staging + ".part", "wb") as handle:
                handle.write(b"partial")
            with open(staging + ".ytdl", "w", encoding="utf-8") as handle:
                json.dump({
                    "downloader": {"current_fragment": {"index": 1}},
                }, handle)

            self.assertFalse(_mark_resume_safe(output, plan))
            self.assertFalse(_prepare_plan_state(output, plan))
            self.assertFalse(os.path.exists(staging + ".part"))

    def test_ytdlp_errors_are_classified_by_cause_not_blanket_retry(self):
        from yt_dlp.networking.exceptions import TransportError
        from yt_dlp.utils import DownloadError as YTDLPDownloadError

        network = TransportError("connection reset")
        network_error = YTDLPDownloadError(
            "network", (TransportError, network, None))
        disk = OSError("disk full")
        disk_error = YTDLPDownloadError("disk", (OSError, disk, None))
        protocol_error = YTDLPDownloadError("This format is DRM protected")

        self.assertIsInstance(
            _classify_ytdlp_download_error(network_error, "network"),
            NetworkDownloadError,
        )
        self.assertIsInstance(
            _classify_ytdlp_download_error(disk_error, "disk"),
            DownloadEnvironmentError,
        )
        self.assertIsInstance(
            _classify_ytdlp_download_error(protocol_error, "DRM"),
            DownloadIntegrityError,
        )

    def test_network_wrapper_is_not_misclassified_by_nested_oserror(self):
        from urllib.error import URLError
        from yt_dlp.networking.exceptions import TransportError
        from yt_dlp.utils import DownloadError as YTDLPDownloadError

        transport = TransportError("connection reset")
        transport.__cause__ = URLError(ConnectionResetError("reset by peer"))
        wrapped = YTDLPDownloadError(
            "network", (TransportError, transport, None))

        self.assertIsInstance(
            _classify_ytdlp_download_error(wrapped, "network"),
            NetworkDownloadError,
        )

    def test_pinned_hls_http_error_text_is_network_failure(self):
        from yt_dlp.utils import DownloadError as YTDLPDownloadError

        wrapped = YTDLPDownloadError(
            "ERROR: [download] Got error: HTTP Error 503: Service Unavailable")
        self.assertIsInstance(
            _classify_ytdlp_download_error(wrapped, str(wrapped)),
            NetworkDownloadError,
        )

    def test_pinned_hls_flat_timeout_and_incomplete_read_are_network_failures(self):
        from yt_dlp.utils import DownloadError as YTDLPDownloadError

        details = (
            "ERROR: [download] Got error: timed out",
            "ERROR: [download] Got error: 0 bytes read, 4096 more expected",
            "ERROR: [download] Got error: [WinError 10054] connection reset",
        )
        for detail in details:
            with self.subTest(detail=detail):
                wrapped = YTDLPDownloadError(detail)
                self.assertIsInstance(
                    _classify_ytdlp_download_error(wrapped, detail),
                    NetworkDownloadError,
                )

    def test_pinned_hls_ssl_eof_is_a_retryable_network_failure(self):
        from yt_dlp.utils import DownloadError as YTDLPDownloadError

        detail = (
            "ERROR: [download] Got error: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1082). "
            "Giving up after 3 retries"
        )
        wrapped = YTDLPDownloadError(detail)

        classified = _classify_ytdlp_download_error(wrapped, detail)

        self.assertIsInstance(classified, NetworkDownloadError)
        self.assertTrue(classified.retryable)

    def test_short_http_response_is_retryable_with_typed_or_flat_error(self):
        from yt_dlp.utils import ContentTooShortError, DownloadError as YTDLPDownloadError

        for downloaded, expected in ((1092858, 1095288), (1056934, 1663800),
                                     (1114112, 1155448), (3379794, 3402424)):
            with self.subTest(downloaded=downloaded, expected=expected):
                cause = ContentTooShortError(downloaded, expected)
                typed = YTDLPDownloadError("transfer failed", (type(cause), cause, None))
                self.assertIsInstance(
                    _classify_ytdlp_download_error(typed, str(typed)), NetworkDownloadError)
                detail = f"ERROR: \r[download] Got error: {cause}. Giving up after 3 retries"
                flat = YTDLPDownloadError(detail)
                self.assertIsInstance(
                    _classify_ytdlp_download_error(flat, detail), NetworkDownloadError)
                stored = classify_stored_download_failure("DownloadIntegrityError: " + detail)
                self.assertEqual(stored.recovery_action, "retry_network")
                self.assertTrue(stored.bulk_recoverable)

    def test_short_response_policy_does_not_retry_other_size_mismatches(self):
        from yt_dlp.utils import ContentTooShortError

        for downloaded, expected in ((4097, 4096), (4096, 4096), (-1, 4096)):
            cause = ContentTooShortError(downloaded, expected)
            self.assertIsInstance(
                _classify_ytdlp_download_error(cause, str(cause)), DownloadIntegrityError)
            detail = f"ERROR: [download] Got error: {cause}"
            self.assertFalse(classify_stored_download_failure(detail).bulk_recoverable)
        self.assertFalse(classify_stored_download_failure(
            "本地成品校验: Downloaded 100 bytes, expected 200 bytes").bulk_recoverable)

    def test_bulk_retry_policy_keeps_deterministic_integrity_failures(self):
        self.assertTrue(is_retryable_stored_download_failure(
            "DownloadIntegrityError: ERROR: [download] Got error: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        ))
        self.assertTrue(is_retryable_stored_download_failure(
            "HLSManifestFetchError: HLS 清单请求失败: timeout"
        ))
        self.assertTrue(is_retryable_stored_download_failure(
            "OperationalError: database is locked"
        ))
        self.assertFalse(is_retryable_stored_download_failure(
            "DownloadIntegrityError: 成品视频时间轴与 HLS 清单差异超过协议边界"
        ))

    def test_stored_failure_policy_has_finite_recovery_actions(self):
        artifact = classify_stored_download_failure(
            "DownloadIntegrityError: 下载 staging 的视频时间戳倒退 83.1s，"
            "分片顺序无法证明完整"
        )
        self.assertEqual(artifact.recovery_action, "retry_clean")
        self.assertTrue(artifact.bulk_recoverable)
        repeated = classify_stored_download_failure(
            "DownloadArtifactError: 下载 staging 的视频时间戳倒退 83.1s",
            "failed_clean_retry",
        )
        self.assertEqual(repeated.recovery_action, "manual")
        lock = classify_stored_download_failure(
            "DownloadError: 同一输出文件正由任务 123 写入"
        )
        self.assertEqual(lock.recovery_action, "reconcile_final")
        scrape = classify_stored_download_failure(
            "ScrapeFetchError: 视频页请求失败：反爬拦截，多次重试仍未放行"
        )
        self.assertEqual(scrape.kind, "transient_network")
        self.assertEqual(scrape.recovery_action, "retry_network")
        self.assertTrue(scrape.bulk_recoverable)

    def test_ytdlp_no_data_blocks_is_retryable_network_failure(self):
        detail = "ERROR: Did not get any data blocks"
        classified = _classify_ytdlp_download_error(Exception(detail), detail)
        self.assertIsInstance(classified, NetworkDownloadError)

    def test_encryption_flag_must_match_frozen_manifest(self):
        plan = _hls_plan()
        plan["manifest_text"] = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:6,\nsegment-001.ts\n#EXT-X-ENDLIST\n"
        )
        with self.assertRaisesRegex(DownloadIntegrityError, "加密标记"):
            _validated_plan(plan)

    def test_worker_gate_blocks_all_download_setup_until_lock_handoff(self):
        class ClosedGate:
            def wait(self, timeout=None):
                self.timeout = timeout
                return False

        class Events:
            def __init__(self):
                self.items = []

            def put(self, value):
                self.items.append(value)

        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            staging = _staging_path(output)
            events = Events()
            with patch("yt_dlp.YoutubeDL") as ydl:
                _yt_dlp_worker(
                    _hls_plan(), staging, output, {}, True,
                    ClosedGate(), SimpleNamespace(is_set=lambda: False), events,
                )
            ydl.assert_not_called()
            self.assertFalse(os.path.exists(staging))
            self.assertEqual(events.items, [])

    def test_missing_crypto_fails_before_ffmpeg_delegate_or_network(self):
        from yt_dlp.dependencies import Cryptodome
        from yt_dlp.downloader.external import FFmpegFD

        class OpenGate:
            def wait(self, timeout=None):
                return True

        class Events:
            def __init__(self):
                self.items = []

            def put(self, value):
                self.items.append(value)

            def close(self):
                pass

            def join_thread(self):
                pass

        plan = _hls_plan()
        plan.update({
            "manifest_text": (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
                "#EXTINF:6,\nsegment-001.ts\n#EXT-X-ENDLIST\n"
            ),
            "encrypted": True,
        })
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            events = Events()
            with patch.object(Cryptodome, "AES", None), \
                    patch.object(FFmpegFD, "real_download") as delegated:
                with self.assertRaises(SystemExit):
                    _yt_dlp_worker(
                        plan, _staging_path(output), output, {}, True,
                        OpenGate(), SimpleNamespace(is_set=lambda: False), events,
                    )
            delegated.assert_not_called()
            self.assertFalse(os.path.exists(_staging_path(output)))
            self.assertEqual(events.items[-1]["code"], "environment_error")

    def test_ffmpeg_permission_error_retries_only_local_processing(self):
        self.assertIsInstance(
            _ffmpeg_failure("output.mp4: Permission denied"),
            DownloadProcessingError,
        )
        self.assertIsInstance(
            _ffmpeg_failure("Invalid data found when processing input"),
            DownloadIntegrityError,
        )

    def test_ffprobe_file_busy_retries_only_local_processing(self):
        with patch(
                "backend.downloader.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1, stdout="", stderr="Permission denied")):
            with self.assertRaises(DownloadProcessingError):
                _validate_with_ffprobe("unused.mp4", _hls_plan())

    def test_request_director_has_no_backend_fallback(self):
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            _restrict_request_director_to_urllib(ydl)
            self.assertEqual(set(ydl._request_director.handlers), {"Urllib"})

    def test_output_lock_rejects_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            token = _acquire_output_lock(output, "first")
            try:
                with self.assertRaises(DownloadError):
                    _acquire_output_lock(output, "second")
            finally:
                _release_output_lock(output, token)

    def test_output_lock_ignores_reused_pid_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "video.mp4")
            first_token = _acquire_output_lock(output, "first")
            lock_path = os.path.splitext(output)[0] + ".download-v2.lock"
            with open(lock_path, encoding="utf-8") as handle:
                record = json.load(handle)
            record["pid_identities"] = {str(os.getpid()): "reused-pid-instance"}
            with open(lock_path, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            with patch("backend.downloader._pid_identity", return_value="current-instance"):
                second_token = _acquire_output_lock(output, "second")
            self.assertNotEqual(first_token, second_token)
            _release_output_lock(output, second_token)

    def test_worker_redirect_guard_rejects_private_cross_origin(self):
        with self.assertRaises(InvalidScrapeURL):
            _validate_worker_media_url(
                "http://127.0.0.1/private.ts",
                "https://media.example/segment.ts",
            )
        self.assertEqual(
            _validate_worker_media_url(
                "http://127.0.0.1:8765/next.ts",
                "http://127.0.0.1:8765/fixture.m3u8",
            ),
            "http://127.0.0.1:8765/next.ts",
        )

    def test_timeline_cannot_hide_one_missing_full_segment(self):
        plan = _hls_plan()
        plan.update({
            "manifest_text": (
                "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                "#EXTINF:10,\nsegment-001.ts\n"
                "#EXTINF:10,\nsegment-002.ts\n#EXT-X-ENDLIST\n"
            ),
            "segment_count": 2,
            "unit_count": 2,
            "media_duration": 20.0,
            "target_duration": 10.0,
        })
        probe = {
            "format": {"duration": "10", "format_name": "mov,mp4"},
            "streams": [
                {"codec_type": "video", "duration": "10"},
                {"codec_type": "audio", "duration": "10"},
            ],
        }
        with patch("backend.downloader.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(probe), stderr="")):
            with self.assertRaisesRegex(DownloadIntegrityError, "视频时间轴"):
                _validate_with_ffprobe("unused.mp4", plan)

    def test_short_audio_track_is_rejected(self):
        plan = _hls_plan()
        plan.update({
            "manifest_text": (
                "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                "#EXTINF:10,\nsegment-001.ts\n"
                "#EXTINF:10,\nsegment-002.ts\n#EXT-X-ENDLIST\n"
            ),
            "segment_count": 2,
            "unit_count": 2,
            "media_duration": 20.0,
            "target_duration": 10.0,
        })
        probe = {
            "format": {"duration": "20", "format_name": "mov,mp4"},
            "streams": [
                {"codec_type": "video", "duration": "20"},
                {"codec_type": "audio", "duration": "1"},
            ],
        }
        with patch("backend.downloader.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(probe), stderr="")):
            with self.assertRaisesRegex(DownloadIntegrityError, "音频时间轴"):
                _validate_with_ffprobe("unused.mp4", plan)

    def test_source_short_audio_is_accepted_only_when_mp4_preserves_it(self):
        plan = _hls_plan()
        plan.update({
            "manifest_text": (
                "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                "#EXTINF:10,\nsegment-001.ts\n"
                "#EXTINF:10,\nsegment-002.ts\n#EXT-X-ENDLIST\n"
            ),
            "segment_count": 2,
            "unit_count": 2,
            "media_duration": 20.0,
            "target_duration": 10.0,
        })
        source_probe = {
            "format": {"duration": "20", "format_name": "mpegts"},
            "streams": [
                {"index": 0, "codec_type": "video", "duration": "20"},
                {"index": 1, "codec_type": "audio", "duration": "15"},
            ],
        }
        output_probe = {
            "format": {"duration": "20", "format_name": "mov,mp4"},
            "streams": [
                {"index": 0, "codec_type": "video", "duration": "20"},
                {"index": 1, "codec_type": "audio", "duration": "15"},
            ],
        }
        packet_proof = {
            "video": {"stream_count": 1, "packet_count": 500, "duration": 20.0},
            "audio": {"stream_count": 1, "packet_count": 300, "duration": 15.0},
        }
        with patch("backend.downloader._ffprobe_json",
                   side_effect=[source_probe, output_probe]), patch(
                "backend.downloader._scan_packet_timelines",
                side_effect=[packet_proof, packet_proof]):
            source = _validate_staging_with_ffprobe("source.ts", plan)
            output = _validate_with_ffprobe(
                "output.mp4", plan, source_evidence=source)

        self.assertEqual(output["media_proof"]["audio_packet_count"], 300)
        self.assertEqual(output["audio_duration"], 15.0)

    def test_staging_timestamp_reversal_is_rejected(self):
        probe = {
            "streams": [{"index": 0, "codec_type": "video"}],
        }
        packet_rows = "0,0.000,0.040,\n0,2.000,0.040,\n0,1.000,0.040,\n"
        process = SimpleNamespace(
            stdout=io.StringIO(packet_rows),
            wait=lambda timeout=None: 0,
            poll=lambda: 0,
            kill=lambda: None,
        )
        with patch("backend.downloader.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(DownloadArtifactError, "时间戳倒退"):
                _scan_packet_timelines("unused.ts", probe)

    def test_processing_watchdog_recomputes_after_large_staging_appears(self):
        plan = _hls_plan()
        plan["media_duration"] = 0
        staging = "late-large.native"
        output = "late-large.mp4"
        sizes = {}

        def getsize(path):
            if path not in sizes:
                raise FileNotFoundError(path)
            return sizes[path]

        with patch("backend.downloader.os.path.getsize", side_effect=getsize):
            before = _processing_watchdog_for_artifacts(staging, output, plan)
            sizes[staging] = 8 * 1024 ** 3
            after = _processing_watchdog_for_artifacts(staging, output, plan)

        self.assertGreater(after, before)
        self.assertGreater(after, 60 * 60)


class DownloaderProcessingLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_wait_excludes_watchdog_and_cancel_recovers_permit(self):
        """处理槽由父进程租赁：排队不超时，worker 强停后令牌也不会泄漏。"""

        class FakeEvent:
            def __init__(self):
                self._event = threading.Event()

            def set(self):
                self._event.set()

            def clear(self):
                self._event.clear()

            def is_set(self):
                return self._event.is_set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

        class FakeQueue:
            def __init__(self):
                self.items = [
                    {"kind": "phase", "phase": "processing"},
                    {"kind": "processing_waiting"},
                ]

            def get_nowait(self):
                if not self.items:
                    raise queue.Empty
                return self.items.pop(0)

            def cancel_join_thread(self):
                pass

            def close(self):
                pass

        class FakeProcess:
            def __init__(self, *, args, **_kwargs):
                self.args = args
                self.pid = 4242
                self.exitcode = 0
                self.started = False
                self.closed = False

            def start(self):
                self.started = True

            def is_alive(self):
                stop_event = self.args[6]
                return self.started and not self.closed and not stop_event.is_set()

            def join(self, timeout=None):
                return None

            def close(self):
                self.closed = True

            def kill(self):
                self.closed = True

            def terminate(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.events = []
                self.queue = FakeQueue()

            def Queue(self):
                return self.queue

            def Event(self):
                event = FakeEvent()
                self.events.append(event)
                return event

            def Process(self, **kwargs):
                return FakeProcess(**kwargs)

        lock_entered = threading.Event()
        allow_lock = threading.Event()

        def acquire_lock(_output_path, _key):
            lock_entered.set()
            if not allow_lock.wait(2):
                raise AssertionError("测试未放行输出锁")
            return "lock-token"

        instance = M3U8Downloader()
        instance._processing_limit = 1
        instance._context = FakeContext()
        with tempfile.TemporaryDirectory() as directory, patch(
                "backend.downloader._acquire_output_lock",
                side_effect=acquire_lock), patch(
                "backend.downloader._assign_output_lock_to_worker"), patch(
                "backend.downloader._release_output_lock"), patch(
                "backend.downloader._processing_watchdog_for_artifacts",
                return_value=0.05):
            output = os.path.join(directory, "leased.mp4")
            task = asyncio.create_task(instance.download(
                _hls_plan(), output, task_key="lease-test"))
            self.assertTrue(await asyncio.to_thread(lock_entered.wait, 1))

            limiter = instance._processing_limiter()
            await limiter.acquire()  # 模拟另一个 worker 正在做 ffprobe/remux。
            allow_lock.set()

            async with asyncio.timeout(1):
                while len(instance._context.events) < 3:
                    await asyncio.sleep(0)
            processing_ready = instance._context.events[2]

            # 即使动态 watchdog 只有 50ms，排队 200ms 也不能误杀 worker。
            await asyncio.sleep(0.2)
            self.assertFalse(task.done())
            self.assertFalse(processing_ready.is_set())

            limiter.release()
            async with asyncio.timeout(1):
                while not processing_ready.is_set():
                    await asyncio.sleep(0.01)

            # 模拟用户暂停/看门狗强停：子进程来不及发 released，父 finally 仍归还。
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # 关机与用户操作重复取消也不能穿透租约回收。
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(limiter.acquire(), timeout=0.5)
            limiter.release()


if __name__ == "__main__":
    unittest.main()
