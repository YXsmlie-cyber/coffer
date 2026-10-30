"""115 上传闸门：整体拒绝本机上传时退回队列、不计次、暂停派发，恢复后自动继续。"""
import asyncio
import io
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import cloud115
from backend.cloud115 import UploadTransferError
from backend.database import Base, Task
from backend.downloader import MediaToolsStatus
import backend.task_manager as task_manager_module
from backend.task_manager import TaskManager
from backend.upload_gate import UploadGate, WAITING_TEXT


def _oss_error(code: int, body: bytes) -> HTTPError:
    class _Body(io.BytesIO):
        data = body

    return HTTPError("https://fhnfile.oss-cn-shenzhen.aliyuncs.com/key?uploads=1",
                     code, "Forbidden", Message(), _Body())


_DENIED = (b"<Error><Code>AccessDenied</Code>"
           b"<Message>Access denied by bucket policy.</Message></Error>")


class _Sleeper:
    """可控的复查等待：记录每次等待时长，由测试逐次放行。"""

    def __init__(self):
        self.delays: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, delay):
        self.delays.append(delay)
        await self.release.wait()
        self.release.clear()


class UploadGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_confirmed_denial_closes_gate(self):
        for verdict in ("allowed", "unknown"):
            gate = UploadGate(probe=AsyncMock(return_value=verdict))
            self.assertFalse(await gate.confirm_denied())
            self.assertFalse(gate.blocked)

        events = []
        probe = AsyncMock(return_value="denied")
        gate = UploadGate(probe=probe, sleep=_Sleeper())
        gate.add_listener(lambda event, seconds: events.append(event))
        results = await asyncio.gather(gate.confirm_denied(), gate.confirm_denied())
        self.assertEqual(results, [True, True])
        self.assertTrue(gate.blocked)
        self.assertEqual(probe.await_count, 1, "并发失败只探测一次")
        self.assertEqual(events, ["blocked"])
        await gate.close()

    async def test_monitor_reopens_only_on_allowed_probe(self):
        verdicts = ["denied", "unknown", "allowed"]
        probe = AsyncMock(side_effect=lambda: verdicts.pop(0))
        sleeper = _Sleeper()
        events = []
        gate = UploadGate(probe=probe, sleep=sleeper, recheck_delays=(300.0, 600.0))
        gate.add_listener(lambda event, seconds: events.append(event))
        self.assertTrue(await gate.confirm_denied())

        async def finish_wait(expected_waits):
            for _ in range(100):
                if len(sleeper.delays) >= expected_waits:
                    break
                await asyncio.sleep(0)
            sleeper.release.set()
            for _ in range(5):
                await asyncio.sleep(0)

        await finish_wait(1)
        self.assertTrue(gate.blocked, "第一次复查网络不通，仍保持暂停")
        await finish_wait(2)
        self.assertEqual(sleeper.delays, [300.0, 600.0])
        self.assertFalse(gate.blocked, "网络不通不能开闸，确认放行后才开")
        self.assertEqual(events, ["blocked", "restored"])
        await gate.close()

    async def test_successful_upload_reopens_gate_and_stops_monitor(self):
        gate = UploadGate(probe=AsyncMock(return_value="denied"), sleep=_Sleeper())
        events = []
        gate.add_listener(lambda event, seconds: events.append(event))
        await gate.confirm_denied()
        monitor = gate._monitor
        gate.record_success()
        await asyncio.gather(monitor, return_exceptions=True)
        self.assertFalse(gate.blocked)
        self.assertTrue(monitor.cancelled())
        self.assertEqual(events, ["blocked", "restored"])


class UploadProbeTests(unittest.TestCase):
    def test_bucket_policy_denial_is_recognised_through_the_error_chain(self):
        try:
            try:
                raise _oss_error(403, _DENIED)
            except HTTPError as cause:
                raise RuntimeError("MultipartUploadAbort") from cause
        except RuntimeError as error:
            self.assertTrue(cloud115.is_bucket_policy_denial(error))
        other = _oss_error(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
        self.assertFalse(cloud115.is_bucket_policy_denial(other))

    def test_probe_verdicts_and_cleanup(self):
        token = {"StatusCode": "200", "AccessKeyId": "a", "AccessKeySecret": "b",
                 "SecurityToken": "c", "Expiration": "2099-01-01T00:00:00Z"}
        cancel = Mock()
        with patch("p115oss.api.upload_token", return_value=token), \
                patch("p115oss.oss.oss_multipart_upload_init",
                      side_effect=_oss_error(403, _DENIED)), \
                patch("p115oss.oss.oss_multipart_upload_cancel", cancel):
            self.assertEqual(cloud115._probe_upload_access_sync(), "denied")
        cancel.assert_not_called()

        with patch("p115oss.api.upload_token", return_value=token), \
                patch("p115oss.oss.oss_multipart_upload_init", return_value="upload-1"), \
                patch("p115oss.oss.oss_multipart_upload_cancel", cancel):
            self.assertEqual(cloud115._probe_upload_access_sync(), "allowed")
        self.assertEqual(cancel.call_args.args[1], "upload-1", "放行时必须取消探测用的分块上传")

        with patch("p115oss.api.upload_token", side_effect=OSError("reset")):
            self.assertEqual(cloud115._probe_upload_access_sync(), "unknown")
        with patch("p115oss.api.upload_token", return_value=token), \
                patch("p115oss.oss.oss_multipart_upload_init", side_effect=ConnectionResetError()):
            self.assertEqual(cloud115._probe_upload_access_sync(), "unknown")


class UploadGateTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, verdict):
        gate = UploadGate(probe=AsyncMock(return_value=verdict), sleep=_Sleeper())
        manager = TaskManager(upload_gate=gate)
        task = SimpleNamespace(
            id=7, model_id=1, model_name="Model", model_cloud_cid="cid-1",
            viewkey="abcdef1234567", title="Title", duration=10, local_path="video.mp4")
        statuses = []

        async def set_status(_task_id, **fields):
            statuses.append(fields)
            return task if fields.get("_return_task") else None

        manager._set_status = set_status
        manager._get_dedup_index = AsyncMock(return_value={})
        manager._schedule_auto_requeue = Mock()

        def discard(coro):
            coro.close()

        manager._spawn_bg = discard
        return manager, gate, statuses

    async def _run_failed_upload(self, manager):
        with patch.object(task_manager_module.dedup, "match_video",
                          return_value=("missing", None)), \
                patch.object(task_manager_module.cloud, "upload_file",
                             new=AsyncMock(side_effect=UploadTransferError("上传失败: 403"))), \
                patch.object(task_manager_module.os.path, "getsize", return_value=123):
            await manager._run_upload(7)

    async def test_denied_upload_returns_to_queue_without_spending_retries(self):
        manager, gate, statuses = self._manager("denied")
        await self._run_failed_upload(manager)
        self.assertTrue(gate.blocked)
        final = statuses[-1]
        self.assertEqual(final["status"], "queued")
        self.assertEqual(final["error"], WAITING_TEXT)
        self.assertNotIn("auto_retry_count", final)
        manager._schedule_auto_requeue.assert_not_called()
        self.assertEqual(manager.upload_blocked_reason()["code"], "upload_denied")
        await manager.shutdown()

    async def test_other_upload_failures_keep_existing_retry_policy(self):
        manager, gate, statuses = self._manager("allowed")
        await self._run_failed_upload(manager)
        self.assertFalse(gate.blocked)
        self.assertEqual(statuses[-1]["status"], "failed")
        manager._schedule_auto_requeue.assert_called_once()
        self.assertIsNone(manager.upload_blocked_reason())
        await manager.shutdown()


class UploadGateDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = (Path(self.temp.name) / "tasks.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(task_manager_module, "async_session", self.session)
        self.session_patch.start()
        self.gate = UploadGate(probe=AsyncMock(return_value="denied"), sleep=_Sleeper())
        self.manager = TaskManager(upload_gate=self.gate)
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)

        def discard(coro):
            coro.close()

        self.manager._spawn_bg = discard
        async with self.session() as session:
            session.add(Task(viewkey="abcdef1234567", model_name="Model", title="T",
                             url="https://example/v", task_type="upload", status="queued",
                             local_path="video.mp4"))
            await session.commit()

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_closed_gate_holds_queued_uploads_until_reopened(self):
        started = []

        async def fake_upload(task_id):
            started.append(task_id)
            self.manager._running.pop(task_id, None)

        self.manager._run_upload = fake_upload
        await self.gate.confirm_denied()
        await self.manager._pump()
        await asyncio.sleep(0)
        self.assertEqual(started, [])

        self.gate.record_success()
        await self.manager._pump()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
