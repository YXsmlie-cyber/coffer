"""来源失效冷却使用持久队列，不占工作槽；暂停、重启和额度有明确边界。"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.downloader import MediaToolsStatus
from backend.database import Base, Task
from backend.download_errors import (classify_stored_download_failure,
                                     classify_ytdlp_download_error, SourceUnavailableDownloadError)
from backend.scraper import HLSSourceUnavailableError, _resolve_vod_plan
from backend.task_manager import TaskManager


class SourceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name).as_posix()}/tasks.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.manager = TaskManager(session_factory=self.session,
                                   notifier_client=AsyncMock())
        self.manager._request_pump = lambda: None
        self.manager._create_upload_task = AsyncMock()
        # 与本机是否安装 ffmpeg 无关；缺工具门禁见 test_media_tools 与调度用例。
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)

    async def asyncTearDown(self):
        if self.manager._source_wakeup:
            self.manager._source_wakeup.cancel()
        for task in self.manager._running.values():
            task.cancel()
        await asyncio.gather(*self.manager._running.values(), return_exceptions=True)
        await self.engine.dispose()
        self.temp.cleanup()

    async def seed(self, **fields):
        async with self.session() as session:
            task = Task(viewkey=str(fields.pop('viewkey', 'example')), model_name='Example',
                        title='Example', url='https://example.invalid/video',
                        task_type='download', local_path=str(Path(self.temp.name) / 'out.mp4'),
                        **fields)
            session.add(task)
            await session.commit()
            return task.id

    async def row(self, task_id):
        async with self.session() as session:
            return await session.get(Task, task_id)

    async def test_three_immediate_source_failures_schedule_first_cooldown(self):
        task_id = await self.seed(status='queued')
        resolver = AsyncMock(side_effect=HLSSourceUnavailableError('HLS 清单请求返回 HTTP 410'))
        self.manager._media_resolver = resolver
        with patch('backend.task_manager.asyncio.sleep', new=AsyncMock()):
            await self.manager._run_download(task_id)
        task = await self.row(task_id)
        self.assertEqual(resolver.await_count, 3)
        self.assertEqual((task.status, task.phase, task.auto_retry_count),
                         ('queued', 'source_cooldown', 1))
        self.assertGreater(task.next_retry_at, datetime.now() + timedelta(minutes=14))
        self.assertLess(task.next_retry_at, datetime.now() + timedelta(minutes=16))
        self.manager._create_upload_task.assert_not_awaited()

    async def test_each_cooldown_checks_once_and_exhaustion_stops(self):
        for count, expected in ((1, 'queued'), (2, 'queued'), (3, 'failed')):
            task_id = await self.seed(viewkey=f'round-{count}', status='queued',
                                      phase='source_cooldown', auto_retry_count=count,
                                      next_retry_at=datetime.now()-timedelta(seconds=1))
            resolver = AsyncMock(side_effect=HLSSourceUnavailableError('HLS 清单请求返回 HTTP 410'))
            self.manager._media_resolver = resolver
            await self.manager._run_download(task_id)
            task = await self.row(task_id)
            self.assertEqual(resolver.await_count, 1)
            self.assertEqual(task.status, expected)
            if count == 3:
                self.assertEqual(task.phase, 'failed_source_exhausted')
                self.assertIsNone(task.next_retry_at)
                self.assertFalse(classify_stored_download_failure(
                    task.error, task.phase).bulk_recoverable)

    async def test_future_source_wait_does_not_block_ready_download(self):
        waiting = await self.seed(status='queued', phase='source_cooldown',
                                  next_retry_at=datetime.now()+timedelta(hours=1))
        ready = await self.seed(viewkey='ready', status='queued')
        self.manager._run_download = AsyncMock()
        with patch('backend.task_manager._select_output_path_for_task',
                   return_value=str(Path(self.temp.name)/'out.mp4')), \
                patch('backend.task_manager.download_space_available', return_value=True):
            await self.manager._pump()
        await asyncio.sleep(0)
        self.manager._run_download.assert_awaited_once_with(ready)
        self.assertNotIn(waiting, self.manager._running)
        self.assertIsNotNone(self.manager._source_wakeup)

    async def test_pause_resume_and_restore_keep_deadline_and_budget(self):
        deadline = datetime.now()+timedelta(hours=1)
        task_id = await self.seed(status='queued', phase='source_cooldown',
                                  auto_retry_count=2, next_retry_at=deadline)
        await self.manager.pause_all('download')
        task = await self.row(task_id)
        self.assertEqual((task.status, task.phase), ('paused', 'paused_source_cooldown'))
        self.manager._pump = AsyncMock()
        self.manager.reconcile_failed_downloads = AsyncMock(return_value={'reconciled': 0})
        await self.manager.restore()
        self.assertEqual((await self.row(task_id)).status, 'paused')
        await self.manager.resume_all('download')
        task = await self.row(task_id)
        self.assertEqual((task.status, task.phase, task.auto_retry_count, task.next_retry_at),
                         ('queued', 'source_cooldown', 2, deadline))

    async def test_source_error_is_typed_without_marking_video_removed(self):
        from types import SimpleNamespace
        client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=410)))
        with self.assertRaises(HLSSourceUnavailableError):
            await _resolve_vod_plan(client, 'https://cdn.example/master.m3u8')
        for error in ('HLSManifestFetchError: HLS 清单请求返回 HTTP 410',
                      'HLSSourceUnavailableError: HLS 清单请求返回 HTTP 410'):
            policy = classify_stored_download_failure(error)
            self.assertEqual(policy.kind, 'source_unavailable')
            self.assertTrue(policy.bulk_recoverable)

    async def test_valid_cooldown_plan_is_downloaded_without_second_resolution(self):
        task_id = await self.seed(status='queued', phase='source_cooldown', auto_retry_count=1)
        plan = {'quality': 1080}
        resolver = AsyncMock(return_value={'m3u8_url': 'https://cdn.example/media.m3u8', 'hls_plan': plan})
        self.manager._media_resolver = resolver
        async def finish(received, output, **kwargs):
            self.assertIs(received, plan)
            Path(output).write_bytes(b'fixture')
        from types import SimpleNamespace
        self.manager._download_client = SimpleNamespace(download=AsyncMock(side_effect=finish))
        await self.manager._run_download(task_id)
        task = await self.row(task_id)
        resolver.assert_awaited_once()
        self.assertEqual(task.status, 'completed')
        self.assertIsNone(task.next_retry_at)
        self.manager._create_upload_task.assert_awaited_once_with(task_id)

    async def test_cooldown_preserves_existing_quality_recovery_intent(self):
        task_id = await self.seed(status='queued', phase='queued_quality_retry:1079')
        resolver = AsyncMock(side_effect=HLSSourceUnavailableError('HLS 清单请求返回 HTTP 410'))
        self.manager._media_resolver = resolver
        with patch('backend.task_manager.asyncio.sleep', new=AsyncMock()):
            await self.manager._run_download(task_id)
        self.assertEqual((await self.row(task_id)).phase, 'source_cooldown:queued_quality_retry:1079')
        await self.manager.pause_all('download')
        self.manager._pump = AsyncMock()
        await self.manager.resume_all('download')
        resolver.reset_mock()
        await self.manager._run_download(task_id)
        resolver.assert_awaited_once_with('https://example.invalid/video', max_quality=1079)

    async def test_exhausted_budget_requires_individual_retry_to_reset(self):
        task_id = await self.seed(status='failed', phase='failed_source_exhausted', auto_retry_count=3,
                                  error='HLSSourceUnavailableError: HLS 清单请求返回 HTTP 410')
        result = await self.manager.retry_failed('download')
        self.assertEqual(result['retried'], 0)
        self.assertEqual((await self.row(task_id)).status, 'failed')
        self.assertTrue(await self.manager.retry_task(task_id))
        task = await self.row(task_id)
        self.assertEqual((task.status, task.auto_retry_count, task.next_retry_at), ('queued', 0, None))

    async def test_single_timer_wakes_at_deadline_and_is_cleared_after_pause(self):
        await self.seed(status='queued', phase='source_cooldown',
                        next_retry_at=datetime.now()+timedelta(milliseconds=250))
        wake = asyncio.Event()
        self.manager._request_pump = wake.set
        await self.manager._schedule_source_wakeup()
        old_timer = self.manager._source_wakeup
        await self.manager._schedule_source_wakeup()
        self.assertTrue(old_timer.cancelled())
        await asyncio.wait_for(wake.wait(), timeout=2)
        await self.manager.pause_all('download')
        self.assertIsNone(self.manager._source_wakeup)

    async def test_deadline_crossing_queue_read_and_timer_query_is_not_lost(self):
        cutoff = datetime.now()-timedelta(seconds=2)
        await self.seed(status='queued', phase='source_cooldown',
                        next_retry_at=cutoff+timedelta(seconds=1))
        wake = asyncio.Event()
        self.manager._request_pump = wake.set
        await self.manager._schedule_source_wakeup(cutoff)
        await asyncio.wait_for(wake.wait(), timeout=1)

    async def test_fragment_410_survives_worker_boundary_and_uses_source_budget(self):
        from backend.downloader import _worker_failure
        from yt_dlp.networking.exceptions import HTTPError
        from types import SimpleNamespace
        detail = 'ERROR: [download] Got error: HTTP Error 410: Gone. Giving up after 3 retries'
        classified = classify_ytdlp_download_error(Exception(detail), detail)
        self.assertIsInstance(classified, SourceUnavailableDownloadError)
        typed = HTTPError(SimpleNamespace(status=410, reason='Gone', close=lambda: None))
        self.assertIsInstance(classify_ytdlp_download_error(typed, '传输失败'),
                              SourceUnavailableDownloadError)
        failure = _worker_failure(classified.action, classified.code, detail)
        self.assertIsInstance(failure, SourceUnavailableDownloadError)
        task_id = await self.seed(status='queued', phase='source_cooldown', auto_retry_count=3)
        self.manager._media_resolver = AsyncMock(return_value={
            'm3u8_url': 'https://cdn.example/media.m3u8', 'hls_plan': {'quality': 1080}})
        self.manager._download_client = SimpleNamespace(download=AsyncMock(side_effect=failure))
        await self.manager._run_download(task_id)
        self.assertEqual(self.manager._download_client.download.await_count, 1)
        task = await self.row(task_id)
        self.assertEqual((task.status, task.phase), ('failed', 'failed_source_exhausted'))
        self.assertEqual(classify_stored_download_failure('NetworkDownloadError: '+detail).kind,
                         'source_unavailable')

    async def test_source_error_is_not_delayed_by_an_earlier_timeout_message(self):
        from backend.downloader import _yt_dlp_worker, _staging_path
        from types import SimpleNamespace
        from test_task_manager_download import make_plan
        from yt_dlp.networking import _urllib as ytdlp_urllib
        plan = make_plan('https://cdn.example/media.m3u8', duration=10)
        output = str(Path(self.temp.name)/'worker.mp4')
        events = []
        channel = SimpleNamespace(put=events.append, close=lambda: None, join_thread=lambda: None)
        gate = SimpleNamespace(wait=lambda **kwargs: True)
        stop = SimpleNamespace(is_set=lambda: False)
        error = SourceUnavailableDownloadError('HTTP Error 410: Gone | previous connection timed out')
        with patch.object(ytdlp_urllib, 'RedirectHandler', ytdlp_urllib.RedirectHandler), \
                patch('backend.downloader._download_native_hls', side_effect=error):
            with self.assertRaises(SystemExit):
                _yt_dlp_worker(plan, _staging_path(output), output, {}, True, gate, stop, channel)
        self.assertEqual(events[-1]['code'], 'source_unavailable')
        self.assertFalse(events[-1]['no_data_stall'])
