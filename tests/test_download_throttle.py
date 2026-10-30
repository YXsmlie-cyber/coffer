"""PH 视频 CDN 限流（429）：整体冷却、任务退回队列不计次，恢复后降并发逐步爬升。"""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Task
from backend.download_errors import (
    NetworkDownloadError,
    RateLimitedDownloadError,
    SourceUnavailableDownloadError,
    classify_stored_download_failure,
    classify_ytdlp_download_error,
)
from backend.download_throttle import DownloadThrottle, WAITING_TEXT
from backend.downloader import MediaToolsStatus, _worker_failure
from backend.network_health import LinkHealth
from backend.scraper import HLSManifestFetchError, HLSRateLimitedError, _fetch_hls_playlist
from backend.task_manager import TaskManager

DETAIL_429 = ("ERROR: [download] Got error: HTTP Error 429: Too Many Requests. "
              "Giving up after 3 retries")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DownloadThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_burst_of_429_is_one_cooldown_and_later_ones_escalate(self):
        clock = FakeClock()
        throttle = DownloadThrottle(clock=clock, cooldowns=(300.0, 600.0))
        events = []
        throttle.add_listener(lambda event, seconds: events.append((event, seconds)))
        self.assertEqual(throttle.report(), 300.0)
        clock.now += 10
        throttle.report()                      # 同一轮里其它任务的 429 不叠加
        self.assertEqual(throttle.level, 1)
        self.assertTrue(throttle.blocked)
        self.assertEqual(events, [("throttled", 300.0)])

        clock.now += 300
        self.assertFalse(throttle.blocked)
        self.assertEqual(throttle.report(), 600.0)
        self.assertEqual(throttle.level, 2)
        clock.now += 600
        throttle.report()
        self.assertEqual(throttle.level, 2, "档位到顶后保持最长冷却")
        throttle.close()

    def test_concurrency_halves_per_level_and_recovers_on_success(self):
        clock = FakeClock()
        throttle = DownloadThrottle(clock=clock, cooldowns=(1.0, 1.0, 1.0))
        self.assertEqual(throttle.concurrency_cap(5), 5)
        for expected in (2, 1, 1):
            throttle.report()
            clock.now += 2
            self.assertEqual(throttle.concurrency_cap(5), expected)
        throttle.record_success()
        throttle.record_success()
        self.assertEqual(throttle.concurrency_cap(5), 2)
        throttle.record_success()
        self.assertEqual(throttle.concurrency_cap(5), 5)
        throttle.record_success()
        self.assertEqual(throttle.level, 0)

    async def test_cooldown_end_emits_resumed(self):
        throttle = DownloadThrottle(cooldowns=(0.01,))
        resumed = asyncio.Event()
        throttle.add_listener(lambda event, _s: resumed.set() if event == "resumed" else None)
        throttle.report()
        await asyncio.wait_for(resumed.wait(), timeout=1)
        self.assertFalse(throttle.blocked)
        self.assertEqual(throttle.public()["level"], 1)


class RateLimitClassificationTests(unittest.TestCase):
    def test_429_is_rate_limited_in_chain_flat_text_and_worker_boundary(self):
        from yt_dlp.networking.exceptions import HTTPError
        typed = HTTPError(SimpleNamespace(status=429, reason="Too Many Requests",
                                          close=lambda: None))
        self.assertIsInstance(classify_ytdlp_download_error(typed, "传输失败"),
                              RateLimitedDownloadError)
        flat = classify_ytdlp_download_error(Exception(DETAIL_429), DETAIL_429)
        self.assertIsInstance(flat, RateLimitedDownloadError)
        self.assertTrue(flat.retryable)
        self.assertIsInstance(_worker_failure(flat.action, flat.code, DETAIL_429),
                              RateLimitedDownloadError)
        gone = "ERROR: [download] Got error: HTTP Error 410: Gone"
        self.assertIsInstance(classify_ytdlp_download_error(Exception(gone), gone),
                              SourceUnavailableDownloadError)
        other = "ERROR: [download] Got error: HTTP Error 403: Forbidden"
        self.assertNotIsInstance(classify_ytdlp_download_error(Exception(other), other),
                                 RateLimitedDownloadError)

    def test_old_429_failures_stay_bulk_recoverable(self):
        for stored in ("NetworkDownloadError: " + DETAIL_429,
                       "DownloadError: write to closed file | " + DETAIL_429):
            self.assertTrue(classify_stored_download_failure(stored).bulk_recoverable, stored)


class StreamClosedRaceTests(unittest.TestCase):
    """HlsFD 并发分片：分片 429 耗尽重试时工作线程关闭输出流，主线程报 write to closed file。"""

    def run_worker(self, error, tail):
        from backend.downloader import _staging_path, _yt_dlp_worker
        from yt_dlp.networking import _urllib as ytdlp_urllib
        try:
            from test_task_manager_download import make_plan
        except ImportError:                     # 以 tests.xxx 模块方式单独运行
            from tests.test_task_manager_download import make_plan

        def fake_download(_plan, _staging, _config, _stop, _events, logger):
            if tail:
                logger.error(tail)
            raise error

        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "worker.mp4")
            events = []
            channel = SimpleNamespace(put=events.append, close=lambda: None,
                                      join_thread=lambda: None)
            gate = SimpleNamespace(wait=lambda **kwargs: True)
            stop = SimpleNamespace(is_set=lambda: False)
            with patch.object(ytdlp_urllib, "RedirectHandler", ytdlp_urllib.RedirectHandler), \
                    patch("backend.downloader._download_native_hls", side_effect=fake_download), \
                    patch("backend.downloader._mark_resume_safe", return_value=True) as mark:
                with self.assertRaises(SystemExit):
                    _yt_dlp_worker(make_plan("https://cdn.example/media.m3u8", duration=10),
                                   _staging_path(output), output, {}, True, gate, stop, channel)
        return events[-1], mark

    def test_closed_stream_after_429_is_retried_but_never_resumed(self):
        event, mark = self.run_worker(ValueError("write to closed file"), DETAIL_429)
        self.assertEqual((event["action"], event["code"]), ("retry_network", "rate_limited"))
        # .ytdl 已记下没写进去的分片，断点会缺片：不得标记续传安全。
        mark.assert_not_called()
        self.assertFalse(event["resume_checkpoint"])

    def test_unrelated_value_error_still_fails_closed(self):
        event, mark = self.run_worker(ValueError("write to closed file"), "")
        self.assertEqual(event["code"], "unexpected_worker_error")
        event, _mark = self.run_worker(ValueError("bad plan"), DETAIL_429)
        self.assertEqual(event["code"], "unexpected_worker_error")


class ManifestRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_429_and_silent_transport_errors(self):
        import httpx

        def client_for(handler):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        url = "https://ev-h.phncdn.com/hls/videos/a/index.m3u8"
        async with client_for(lambda request: httpx.Response(429)) as client:
            with self.assertRaises(HLSRateLimitedError):
                await _fetch_hls_playlist(client, url)

        def timeout(request):
            raise httpx.ReadTimeout("", request=request)

        async with client_for(timeout) as client:
            with self.assertRaises(HLSManifestFetchError) as caught:
                await _fetch_hls_playlist(client, url)
        self.assertIn("ReadTimeout", str(caught.exception))


class TaskManagerRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/tasks.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.clock = FakeClock()
        self.throttle = DownloadThrottle(clock=self.clock)
        # 单元测试不触网：链路探测固定为可用，普通网络错误按原有计次策略处理。
        self.manager = TaskManager(session_factory=self.session, notifier_client=AsyncMock(),
                                   link_health=LinkHealth(probe=AsyncMock(return_value=True)),
                                   download_throttle=self.throttle)
        self.manager._request_pump = lambda: None
        self.manager._create_upload_task = AsyncMock()
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)
        self.resolver = AsyncMock(return_value={
            "m3u8_url": "https://cdn.example/media.m3u8", "hls_plan": {"quality": 1080}})
        self.manager._media_resolver = self.resolver

    async def asyncTearDown(self):
        self.throttle.close()
        for task in self.manager._running.values():
            task.cancel()
        await asyncio.gather(*self.manager._running.values(), return_exceptions=True)
        await self.engine.dispose()
        self.temp.cleanup()

    async def seed(self, viewkey="example", **fields):
        async with self.session() as session:
            task = Task(viewkey=viewkey, model_name="Example", title="Example",
                        url="https://example.invalid/video", task_type="download",
                        local_path=str(Path(self.temp.name) / f"{viewkey}.mp4"), **fields)
            session.add(task)
            await session.commit()
            return task.id

    async def row(self, task_id):
        async with self.session() as session:
            return await session.get(Task, task_id)

    async def test_fragment_429_requeues_without_spending_attempts(self):
        task_id = await self.seed(status="queued")
        download = AsyncMock(side_effect=RateLimitedDownloadError(DETAIL_429))
        self.manager._download_client = SimpleNamespace(download=download)
        with patch("backend.task_manager.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.manager._run_download(task_id)
        self.assertEqual(download.await_count, 1, "429 不能在同一轮里连续重试")
        sleep.assert_not_awaited()
        task = await self.row(task_id)
        self.assertEqual((task.status, task.error), ("queued", WAITING_TEXT))
        self.assertIsNone(task.next_retry_at)
        self.assertTrue(self.throttle.blocked)
        self.assertEqual(self.manager.download_blocked_reason()["code"], "rate_limited")

    async def test_manifest_429_shares_the_same_cooldown(self):
        task_id = await self.seed(status="queued")
        self.resolver.side_effect = HLSRateLimitedError("HLS 清单请求返回 HTTP 429")
        await self.manager._run_download(task_id)
        self.assertEqual((await self.row(task_id)).status, "queued")
        self.assertTrue(self.throttle.blocked)

    async def test_retry_that_lands_in_cooldown_goes_back_to_queue(self):
        task_id = await self.seed(status="queued")
        download = AsyncMock(side_effect=NetworkDownloadError(
            "[download] Got error: HTTP Error 403: Forbidden"))
        self.manager._download_client = SimpleNamespace(download=download)

        async def other_task_hits_429(_seconds):
            self.throttle.report()

        with patch("backend.task_manager.asyncio.sleep", new=other_task_hits_429):
            await self.manager._run_download(task_id)
        self.assertEqual(download.await_count, 1)
        self.assertEqual((await self.row(task_id)).status, "queued")

    async def test_pump_waits_out_cooldown_then_ramps_up(self):
        for index in range(5):
            await self.seed(viewkey=f"v{index}", status="queued")
        self.manager._download_limit = 5
        self.manager._run_download = AsyncMock()
        self.throttle.report()
        with patch("backend.task_manager._select_output_path_for_task",
                   side_effect=lambda task, _cache: task.local_path):
            await self.manager._pump()
            self.assertEqual(self.manager._run_download.await_count, 0)
            self.assertEqual(self.manager.download_blocked_reason()["code"], "rate_limited")

            self.clock.now += 301                 # 冷却结束：第一档只放一半并发
            await self.manager._pump()
            await asyncio.sleep(0)
        self.assertEqual(len(self.manager._running), 2)
        self.assertIsNone(self.manager.download_blocked_reason())

    async def test_success_relaxes_the_throttle(self):
        task_id = await self.seed(status="queued")
        output = Path(self.temp.name) / "example.mp4"
        output.write_bytes(b"media")
        self.throttle.report()
        self.clock.now += 301
        self.manager._download_client = SimpleNamespace(download=AsyncMock(return_value=str(output)))
        await self.manager._run_download(task_id)
        self.assertEqual((await self.row(task_id)).status, "completed")
        self.assertEqual(self.throttle.level, 0)


if __name__ == "__main__":
    unittest.main()
