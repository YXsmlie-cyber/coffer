import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import scraper
from backend.cloud115 import Cloud115Client, Cloud115Error
from backend.database import Base, Model, Task, Video
from backend.downloader import MediaToolsStatus
import backend.task_manager as task_manager_module
from backend.task_manager import TaskManager, UploadConfirmationError
import backend.main as main_module
from backend.main import QRCodePoll, WSHub
from backend.patrol import Patrol


class ScrapeBoundaryTests(unittest.TestCase):
    def test_ranking_url_rejects_loopback_and_accepts_ph_subdomain(self):
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"):
            with self.assertRaises(scraper.InvalidScrapeURL):
                scraper.validate_ph_url("http://127.0.0.1:8765/private")
            self.assertEqual(
                scraper.validate_ph_url("https://www.pornhub.com/pornstars"),
                "https://www.pornhub.com/pornstars",
            )

    def test_media_url_rejects_private_literal_address(self):
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"):
            with self.assertRaises(scraper.InvalidScrapeURL):
                scraper.validate_media_url("http://192.168.1.10/master.m3u8")


class WebSocketHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_uses_snapshot_when_clients_disconnect_during_send(self):
        hub = WSHub()

        class Client:
            def __init__(self):
                self.sent = 0

            async def accept(self):
                return None

            async def send_json(self, _message):
                self.sent += 1
                hub.disconnect(self)

            async def close(self, **_kwargs):
                return None

        clients = [Client(), Client(), Client()]
        senders = []
        try:
            for client in clients:
                await hub.connect(client)
                senders.append(hub._states[client].sender)
            await hub.broadcast({"ok": True})

            async with asyncio.timeout(1):
                while ([client.sent for client in clients] != [1, 1, 1]
                       or not all(sender.done() for sender in senders)):
                    await asyncio.sleep(0)

            self.assertEqual([client.sent for client in clients], [1, 1, 1])
            self.assertFalse(hub.clients)
        finally:
            await hub.close()
            for sender in senders:
                if not sender.done():
                    sender.cancel()
            await asyncio.gather(*senders, return_exceptions=True)

    async def test_qrcode_api_never_projects_cookie_to_browser(self):
        with patch.object(main_module.cloud, "qrcode_poll", new=AsyncMock(
                return_value={"status": 2, "cookie": "UID=secret"})):
            payload = await main_module.cloud_qrcode_poll(
                QRCodePoll(uid="uid", time=1, sign="sign"), Response())
        self.assertEqual(payload, {"status": 2})


class Plugin115HttpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(*, client_host: str, remote: bool = False) -> Request:
        headers = [(b"host", b"127.0.0.1:8000")]
        if remote:
            headers.append((b"x-ph115-remote", b"1"))
        return Request({
            "type": "http", "method": "POST", "path": "/api/115/plugin/directory-ready",
            "headers": headers, "client": (client_host, 45678),
            "scheme": "http", "server": ("127.0.0.1", 8000),
        })

    async def test_local_plugin_directory_route_projects_only_safe_status(self):
        body = main_module.PluginDirectoryRequest(directory_id="12345")
        response = Response()
        with patch.object(main_module.cloud, "plugin_directory_ready", new=AsyncMock(
                return_value={"status": "READY", "cookie": "DO_NOT_ECHO"})):
            result = await main_module.plugin_directory_ready(
                body, self._request(client_host="127.0.0.1"), response)

        self.assertEqual(result, {"status": "READY"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("DO_NOT_ECHO", repr(result))

    async def test_plugin_routes_reject_remote_proxy_requests_before_cloud_access(self):
        body = main_module.PluginDirectoryRequest(directory_id="12345")
        with patch.object(main_module.cloud, "plugin_directory_ready", new=AsyncMock()) as checked:
            with self.assertRaises(HTTPException) as raised:
                await main_module.plugin_directory_ready(
                    body, self._request(client_host="127.0.0.1", remote=True), Response())

        self.assertEqual(raised.exception.status_code, 404)
        checked.assert_not_awaited()

    async def test_local_plugin_submit_route_projects_only_confirmation_status(self):
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        body = main_module.PluginMagnetSubmitRequest(directory_id="12345", magnet=magnet)
        response = Response()
        with patch.object(main_module.cloud, "plugin_submit_magnet", new=AsyncMock(
                return_value={"status": "CONFIRMED_SUCCESS", "magnet": magnet})):
            result = await main_module.plugin_magnet_submit(
                body, self._request(client_host="127.0.0.1"), response)

        self.assertEqual(result, {"status": "CONFIRMED_SUCCESS"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(magnet, repr(result))


class DownloadSelectionTests(unittest.TestCase):
    def test_manual_selection_cannot_requeue_video_already_in_115(self):
        videos = [
            SimpleNamespace(viewkey="missing", dedup_status="missing"),
            SimpleNamespace(viewkey="maybe", dedup_status="maybe"),
            SimpleNamespace(viewkey="exists", dedup_status="exists"),
        ]
        body = main_module.DownloadRequest(
            model_id=1,
            mode="selected",
            viewkeys=["missing", "maybe", "exists"],
        )

        selected = main_module._select_download_videos(videos, body)

        self.assertEqual(
            [video.viewkey for video in selected], ["missing", "maybe"])


class CloudDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_root_cid_fails_closed(self):
        client = Cloud115Client()
        with patch.object(task_manager_module.settings, "video_hub_cid", ""):
            with self.assertRaises(Cloud115Error):
                await client.ensure_model_dir("Model")

    async def test_concurrent_ensure_creates_only_one_directory(self):
        client = Cloud115Client()
        created = False
        mkdir_calls = 0

        async def find(_parent, _name):
            return "new-cid" if created else None

        async def mkdir(_parent, _name):
            nonlocal created, mkdir_calls
            mkdir_calls += 1
            await asyncio.sleep(0)
            created = True
            return "new-cid"

        with patch.object(task_manager_module.settings, "video_hub_cid", "root"), \
                patch.object(client, "find_subdir", new=find), \
                patch.object(client, "mkdir", new=mkdir):
            results = await asyncio.gather(
                client.ensure_model_dir(" Model "),
                client.ensure_model_dir("model"),
            )
        self.assertEqual(results, ["new-cid", "new-cid"])
        self.assertEqual(mkdir_calls, 1)

    async def test_cancelling_upload_waits_for_sync_worker_and_returns_real_result(self):
        client = Cloud115Client()
        started = threading.Event()
        release = threading.Event()

        class FakeP115:
            def upload_file(self, *_args, **_kwargs):
                started.set()
                release.wait(timeout=2)
                return {
                    "state": True,
                    "reuse": False,
                    "data": {"file_id": "remote-file-1"},
                }

        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "video.mp4"
            local.write_bytes(b"video")
            with patch("backend.cloud115._get_p115", return_value=FakeP115()):
                upload = asyncio.create_task(client.upload_file(str(local), "cid"))
                await asyncio.to_thread(started.wait, 1)
                upload.cancel()
                await asyncio.sleep(0)
                self.assertFalse(upload.done())
                # 关机和用户操作可能先后发出取消；第二次也不能穿透 shield，
                # 否则同步上传线程仍在写115而任务行已被误标暂停。
                upload.cancel()
                await asyncio.sleep(0)
                self.assertFalse(upload.done())
                release.set()
                result = await asyncio.wait_for(upload, timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "oss")
        self.assertEqual(result["file_id"], "remote-file-1")

    async def test_upload_without_callback_registers_no_reporthook(self):
        client = Cloud115Client()

        class FakeP115:
            has_reporthook = True

            def upload_file(self, *_args, **kwargs):
                self.has_reporthook = "reporthook" in kwargs
                return {
                    "state": True,
                    "reuse": True,
                    "data": {"file_id": "rapid-file"},
                }

        fake = FakeP115()
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "video.mp4"
            local.write_bytes(b"video")
            with patch("backend.cloud115._get_p115", return_value=fake):
                await client.upload_file(str(local), "cid")
        self.assertFalse(fake.has_reporthook)

    async def test_upload_progress_coalesces_chunk_callbacks_and_keeps_final_value(self):
        client = Cloud115Client()
        progress = []

        class FakeP115:
            def upload_file(self, *_args, **kwargs):
                reporthook = kwargs["reporthook"]
                for _ in range(10_000):
                    reporthook(1)
                return {
                    "state": True,
                    "reuse": False,
                    "data": {"file_id": "oss-file"},
                }

        async def on_progress(sent, total):
            progress.append((sent, total))

        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "video.mp4"
            local.write_bytes(b"x" * 10_000)
            with patch("backend.cloud115._get_p115", return_value=FakeP115()):
                await client.upload_file(
                    str(local), "cid", on_progress=on_progress)

        self.assertLessEqual(len(progress), 3)
        self.assertEqual(progress[-1], (10_000, 10_000))

    async def test_file_info_uses_authoritative_file_id_endpoint(self):
        client = Cloud115Client()

        class FakeP115:
            async def fs_file(self, fid, *, async_):
                self.called = (fid, async_)
                return {
                    "state": True,
                    "data": [{"fid": fid, "cid": "cid-1", "n": "video.mp4"}],
                }

        fake = FakeP115()
        with patch("backend.cloud115._get_p115", return_value=fake):
            info = await client.get_file_info("fid-1")

        self.assertEqual(fake.called, ("fid-1", True))
        self.assertEqual(info["cid"], "cid-1")


class TaskIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = (Path(self.temp.name) / "tasks.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(task_manager_module, "async_session", self.session)
        self.session_patch.start()
        self.manager = TaskManager()
        # 调度用例与本机是否安装 ffmpeg 无关；缺工具门禁由专门用例覆盖。
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)

        def discard(coro):
            coro.close()

        self.manager._spawn_bg = discard
        async with self.session() as session:
            first = Model(ph_id="first", name="First", url="https://example/first")
            second = Model(ph_id="second", name="Second", url="https://example/second")
            session.add_all([first, second])
            await session.commit()
            self.first_id = first.id
            self.second_id = second.id

    async def asyncTearDown(self):
        # 部分调度用例会让 _pump 创建短生命周期任务；先完整关闭测试 manager，
        # 再释放 SQLAlchemy 连接，避免 Windows 删除临时 SQLite 时仍有句柄占用。
        await self.manager.shutdown()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_collaboration_viewkey_creates_one_task_per_model(self):
        base = {
            "viewkey": "shared1234567",
            "title": "Shared",
            "url": "https://example/video",
            "duration": 60,
        }
        ids = await self.manager.add_download_tasks([
            {**base, "model_id": self.first_id, "model_name": "First"},
            {**base, "model_id": self.second_id, "model_name": "Second"},
        ])
        duplicate = await self.manager.add_download_tasks([
            {**base, "model_id": self.first_id, "model_name": "First"},
        ])

        async with self.session() as session:
            rows = (await session.execute(
                select(Task).order_by(Task.model_id)
            )).scalars().all()
        self.assertEqual(len(ids), 2)
        self.assertEqual(duplicate, [])
        self.assertEqual({row.model_id for row in rows}, {self.first_id, self.second_id})
        self.assertEqual({row.artifact_scope for row in rows}, {
            str(self.first_id), str(self.second_id),
        })

    async def test_concurrent_upload_cid_resolution_rechecks_database_once_bound(self):
        first_task = SimpleNamespace(
            model_id=self.first_id, model_name="First", model_cloud_cid="")
        second_task = SimpleNamespace(
            model_id=self.first_id, model_name="First", model_cloud_cid="")
        ensure = AsyncMock(return_value="cid-first")
        with patch.object(task_manager_module.cloud, "ensure_model_dir", new=ensure):
            resolved = await asyncio.gather(
                self.manager._resolve_upload_cid(first_task),
                self.manager._resolve_upload_cid(second_task),
            )

        self.assertEqual(resolved, ["cid-first", "cid-first"])
        ensure.assert_awaited_once_with("First")
        self.assertEqual(len(self.manager._model_cid_locks), 0)
        async with self.session() as session:
            model = await session.get(Model, self.first_id)
        self.assertEqual(model.cloud_cid, "cid-first")

    async def test_manual_requeue_reuses_failed_download_row(self):
        async with self.session() as session:
            failed = Task(
                model_id=self.first_id, viewkey="reuse-failed", model_name="First",
                title="Old", url="https://example/old", task_type="download",
                status="failed", error="network",
            )
            session.add(failed)
            await session.commit()
            original_id = failed.id

        ids = await self.manager.add_download_tasks([{
            "model_id": self.first_id,
            "model_name": "First",
            "viewkey": "reuse-failed",
            "title": "New",
            "url": "https://example/new",
            "duration": 30,
        }])

        async with self.session() as session:
            rows = (await session.execute(select(Task).where(
                Task.viewkey == "reuse-failed",
            ))).scalars().all()
        self.assertEqual(ids, [original_id])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, original_id)
        self.assertEqual(rows[0].status, "queued")
        self.assertEqual(rows[0].title, "New")

    async def test_delete_all_uses_multiple_bounded_batches(self):
        async with self.session() as session:
            session.add_all([
                Task(
                    model_id=self.first_id,
                    viewkey=f"bulk-{index}",
                    model_name="First",
                    title="Bulk",
                    url="x",
                    task_type="upload",
                    status="completed",
                )
                for index in range(1_601)
            ])
            await session.commit()

        result = await self.manager.delete_all_tasks("upload")

        async with self.session() as session:
            remaining = (await session.execute(select(Task.id))).scalars().all()
        self.assertEqual(result, {
            "deleted": 1_601,
            "skipped_running_uploads": 0,
        })
        self.assertEqual(remaining, [])

    async def test_delete_task_serializes_with_pump_before_upload_can_start(self):
        async with self.session() as session:
            row = Task(
                model_id=self.first_id,
                viewkey="delete-race-one",
                model_name="First",
                title="Queued upload",
                url="x",
                task_type="upload",
                status="queued",
            )
            session.add(row)
            await session.commit()
            task_id = row.id

        entered_cancel = asyncio.Event()
        allow_cancel = asyncio.Event()
        upload_started = asyncio.Event()
        original_cancel = self.manager._cancel_deletable_tasks

        async def gated_cancel(rows):
            entered_cancel.set()
            await allow_cancel.wait()
            await original_cancel(rows)

        async def fake_upload(_task_id):
            upload_started.set()

        self.manager._cancel_deletable_tasks = gated_cancel
        self.manager._run_upload = fake_upload
        deletion = asyncio.create_task(self.manager.delete_task(task_id))
        await asyncio.wait_for(entered_cancel.wait(), timeout=1)
        pumping = asyncio.create_task(self.manager._pump())
        await asyncio.sleep(0.05)
        self.assertFalse(upload_started.is_set())

        allow_cancel.set()
        self.assertTrue(await deletion)
        await pumping
        self.assertFalse(upload_started.is_set())
        async with self.session() as session:
            self.assertIsNone(await session.get(Task, task_id))

    async def test_delete_tasks_serializes_with_pump_before_upload_can_start(self):
        async with self.session() as session:
            row = Task(
                model_id=self.first_id,
                viewkey="delete-race-many",
                model_name="First",
                title="Queued upload",
                url="x",
                task_type="upload",
                status="queued",
            )
            session.add(row)
            await session.commit()
            task_id = row.id

        entered_cancel = asyncio.Event()
        allow_cancel = asyncio.Event()
        upload_started = asyncio.Event()
        original_cancel = self.manager._cancel_deletable_tasks

        async def gated_cancel(rows):
            entered_cancel.set()
            await allow_cancel.wait()
            await original_cancel(rows)

        async def fake_upload(_task_id):
            upload_started.set()

        self.manager._cancel_deletable_tasks = gated_cancel
        self.manager._run_upload = fake_upload
        deletion = asyncio.create_task(self.manager.delete_tasks([task_id]))
        await asyncio.wait_for(entered_cancel.wait(), timeout=1)
        pumping = asyncio.create_task(self.manager._pump())
        await asyncio.sleep(0.05)
        self.assertFalse(upload_started.is_set())

        allow_cancel.set()
        self.assertEqual(await deletion, {
            "deleted": 1,
            "skipped_running_uploads": 0,
        })
        await pumping
        self.assertFalse(upload_started.is_set())

    async def test_delete_all_uses_fixed_request_highwater(self):
        async with self.session() as session:
            session.add_all([
                Task(
                    model_id=self.first_id,
                    viewkey=f"highwater-old-{index}",
                    model_name="First",
                    title="Old",
                    url="x",
                    task_type="upload",
                    status="completed",
                )
                for index in range(2)
            ])
            await session.commit()

        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        original_cleanup = self.manager._cleanup_task_rows

        def gated_cleanup(rows):
            cleanup_entered.set()
            if not allow_cleanup.wait(2):
                raise AssertionError("测试未放行批量清理")
            original_cleanup(rows)

        self.manager._cleanup_task_rows = gated_cleanup
        deletion = asyncio.create_task(self.manager.delete_all_tasks("upload"))
        self.assertTrue(await asyncio.to_thread(cleanup_entered.wait, 1))
        async with self.session() as session:
            fresh = Task(
                model_id=self.first_id,
                viewkey="highwater-new",
                model_name="First",
                title="New after request",
                url="x",
                task_type="upload",
                status="queued",
            )
            session.add(fresh)
            await session.commit()
            fresh_id = fresh.id
        allow_cleanup.set()

        self.assertEqual(await deletion, {
            "deleted": 2,
            "skipped_running_uploads": 0,
        })
        async with self.session() as session:
            remaining = (await session.execute(
                select(Task.id, Task.viewkey)
            )).all()
        self.assertEqual(remaining, [(fresh_id, "highwater-new")])

    async def test_mark_video_exists_recomputes_grouped_counts(self):
        async with self.session() as session:
            model = await session.get(Model, self.first_id)
            model.total_videos = 3
            model.snapshot_hash = "stale-snapshot"
            session.add_all([
                Video(
                    model_id=self.first_id,
                    viewkey="mark-missing",
                    title="Missing",
                    url="x",
                    dedup_status="missing",
                ),
                Video(
                    model_id=self.first_id,
                    viewkey="mark-maybe",
                    title="Maybe",
                    url="x",
                    dedup_status="maybe",
                ),
                Video(
                    model_id=self.first_id,
                    viewkey="mark-exists",
                    title="Exists",
                    url="x",
                    dedup_status="exists",
                ),
            ])
            await session.commit()

        broadcast = AsyncMock()
        self.manager.set_broadcast(broadcast)
        await self.manager._mark_video_exists(
            "mark-missing", self.first_id, "First")

        async with self.session() as session:
            model = await session.get(Model, self.first_id)
            video = (await session.execute(select(Video).where(
                Video.viewkey == "mark-missing"
            ))).scalar_one()
        self.assertEqual(video.dedup_status, "exists")
        self.assertEqual(
            (model.cloud_videos, model.maybe_videos, model.missing_videos),
            (2, 1, 0),
        )
        self.assertEqual(model.snapshot_hash, "")
        broadcast.assert_awaited_once()

    async def test_pump_starts_only_available_upload_slots(self):
        async with self.session() as session:
            session.add_all([
                Task(
                    model_id=self.first_id,
                    viewkey=f"queued-upload-{index}",
                    model_name="First",
                    title="Queued",
                    url="x",
                    task_type="upload",
                    status="queued",
                )
                for index in range(100)
            ])
            await session.commit()

        release = asyncio.Event()

        async def run_upload(_task_id):
            await release.wait()

        self.manager._run_upload = run_upload
        await self.manager._pump()
        runners = list(self.manager._running.values())
        self.assertEqual(len(runners), self.manager._upload_sem._value)
        release.set()
        await asyncio.gather(*runners)
        self.manager._running.clear()

    async def test_pump_defers_downloads_while_media_tools_missing(self):
        async with self.session() as session:
            task = Task(
                model_id=self.first_id, viewkey="needs-tools", model_name="First",
                title="Queued", url="x", task_type="download", status="queued")
            session.add(task)
            await session.commit()
            task_id = task.id

        started = []
        release = asyncio.Event()

        async def run_download(started_id):
            started.append(started_id)
            await release.wait()

        alerts = []
        self.manager._run_download = run_download
        self.manager._alert_media_tools_unavailable = lambda: alerts.append(True)
        self.manager._media_tools_check = lambda force=False: MediaToolsStatus(
            ok=False, missing=("ffprobe",))
        with patch.object(
                type(task_manager_module.settings), "download_paths",
                return_value=[str(Path(self.temp.name) / "downloads")]):
            await self.manager._pump()
            self.assertEqual(started, [])
            self.assertEqual(alerts, [True])
            blocked = self.manager.download_blocked_reason()
            self.assertEqual(blocked["missing"], ["ffprobe"])
            self.assertNotIn(self.temp.name, str(blocked))
            async with self.session() as session:
                self.assertEqual((await session.get(Task, task_id)).status, "queued")

            # 工具补齐后的下一次调度直接派发，并撤销复查计时器。
            self.manager._media_tools_check = lambda force=False: MediaToolsStatus(ok=True)
            await self.manager._pump()
        self.assertEqual(started, [task_id])
        self.assertIsNone(self.manager.download_blocked_reason())
        self.assertIsNone(self.manager._media_tools_wakeup)
        release.set()
        await asyncio.gather(*self.manager._running.values())
        self.manager._running.clear()

    async def test_pump_keyset_skips_more_than_one_page_of_blocked_downloads(self):
        blocked_dir = Path(self.temp.name) / "blocked"
        valid_dir = Path(self.temp.name) / "valid"
        async with self.session() as session:
            blocked = [
                Task(
                    model_id=self.first_id,
                    viewkey=f"blocked-{index}",
                    model_name="First",
                    title="Blocked",
                    url="x",
                    task_type="download",
                    status="queued",
                    local_path=str(blocked_dir / f"{index}.mp4"),
                    downloaded_bytes=1,
                )
                for index in range(35)
            ]
            runnable = [
                Task(
                    model_id=self.first_id,
                    viewkey=f"runnable-{index}",
                    model_name="First",
                    title="Runnable",
                    url="x",
                    task_type="download",
                    status="queued",
                )
                for index in range(self.manager._download_limit)
            ]
            session.add_all([*blocked, *runnable])
            await session.commit()
            runnable_ids = {task.id for task in runnable}

        release = asyncio.Event()

        async def run_download(_task_id):
            await release.wait()

        def available(path, _cache=None):
            return "blocked" not in str(path)

        self.manager._run_download = run_download
        with patch.object(
                type(task_manager_module.settings),
                "download_paths",
                return_value=[str(valid_dir)]), patch.object(
                task_manager_module,
                "_download_dir_available",
                side_effect=available):
            await self.manager._pump()

        self.assertEqual(set(self.manager._running), runnable_ids)
        runners = list(self.manager._running.values())
        release.set()
        await asyncio.gather(*runners)
        self.manager._running.clear()

    async def test_bulk_retry_skips_deterministic_download_failure(self):
        ssl_error = (
            "DownloadIntegrityError: ERROR: [download] Got error: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )
        timeline_error = "DownloadIntegrityError: 成品视频时间轴不完整"
        async with self.session() as session:
            session.add_all([
                Task(
                    model_id=self.first_id, viewkey="ssl-retry", model_name="First",
                    title="SSL", url="x", task_type="download", status="failed",
                    error=ssl_error,
                ),
                Task(
                    model_id=self.first_id, viewkey="timeline-stays", model_name="First",
                    title="Timeline", url="x", task_type="download", status="failed",
                    error=timeline_error,
                ),
                Task(
                    model_id=self.first_id, viewkey="artifact-clean", model_name="First",
                    title="Artifact", url="x", task_type="download", status="failed",
                    error=("DownloadIntegrityError: 下载 staging 的视频时间戳倒退 "
                           "83.1s，分片顺序无法证明完整"),
                ),
                Task(
                    model_id=self.first_id, viewkey="upload-retry", model_name="First",
                    title="Upload", url="x", task_type="upload", status="failed",
                    error="Cloud115Error: temporary", progress=62.5,
                ),
            ])
            await session.commit()

        result = await self.manager.retry_failed()

        async with self.session() as session:
            rows = (await session.execute(select(Task).where(
                Task.viewkey.in_([
                    "ssl-retry", "timeline-stays", "artifact-clean", "upload-retry",
                ]),
            ))).scalars().all()
        statuses = {task.viewkey: task.status for task in rows}
        phases = {task.viewkey: task.phase for task in rows}
        progress = {task.viewkey: task.progress for task in rows}
        self.assertEqual(result, {
            "retried": 3, "reconciled": 0, "skipped_non_retryable": 1,
        })
        self.assertEqual(progress["upload-retry"], 0.0, "上传从头开始，不保留旧进度")
        self.assertEqual(statuses["ssl-retry"], "queued")
        self.assertEqual(statuses["artifact-clean"], "queued")
        self.assertEqual(phases["artifact-clean"], "queued_clean_retry")
        self.assertEqual(statuses["upload-retry"], "queued")
        self.assertEqual(statuses["timeline-stays"], "failed")

    async def test_pause_and_resume_preserve_download_recovery_intent(self):
        async with self.session() as session:
            session.add_all([
                Task(
                    model_id=self.first_id, viewkey="clean-resume", model_name="First",
                    title="Clean", url="x", task_type="download", status="queued",
                    phase="queued_clean_retry", progress=47.0,
                    downloaded_bytes=1234, total_bytes=4321,
                ),
                Task(
                    model_id=self.first_id, viewkey="source-resume", model_name="First",
                    title="Source", url="x", task_type="download", status="queued",
                    phase="queued_source_retry",
                ),
                Task(
                    model_id=self.first_id, viewkey="normal-resume", model_name="First",
                    title="Normal", url="x", task_type="download", status="queued",
                    phase="",
                ),
                Task(
                    model_id=self.first_id, viewkey="quality-resume", model_name="First",
                    title="Quality", url="x", task_type="download", status="queued",
                    phase="queued_quality_retry:1079",
                ),
                Task(
                    model_id=self.first_id, viewkey="clean-active", model_name="First",
                    title="Clean Active", url="x", task_type="download", status="queued",
                    phase="queued_clean_resume",
                ),
                Task(
                    model_id=self.first_id, viewkey="quality-active", model_name="First",
                    title="Quality Active", url="x", task_type="download", status="queued",
                    phase="queued_quality_resume:719",
                ),
            ])
            await session.commit()

        self.manager._pump = AsyncMock()
        await self.manager.pause_all("download")

        async with self.session() as session:
            paused = (await session.execute(select(Task).where(
                Task.viewkey.in_([
                    "clean-resume", "source-resume", "normal-resume",
                    "quality-resume", "clean-active", "quality-active",
                ]),
            ))).scalars().all()
        paused_by_key = {task.viewkey: task for task in paused}
        self.assertEqual(paused_by_key["clean-resume"].status, "paused")
        self.assertEqual(paused_by_key["clean-resume"].phase, "paused_clean_retry")
        self.assertEqual(paused_by_key["source-resume"].phase, "paused_source_retry")
        self.assertEqual(paused_by_key["normal-resume"].phase, "")
        self.assertEqual(
            paused_by_key["quality-resume"].phase,
            "paused_quality_retry:1079",
        )
        self.assertEqual(
            paused_by_key["clean-active"].phase,
            "paused_clean_resume",
        )
        self.assertEqual(
            paused_by_key["quality-active"].phase,
            "paused_quality_resume:719",
        )
        self.assertEqual(paused_by_key["clean-resume"].progress, 47.0)
        self.assertEqual(paused_by_key["clean-resume"].downloaded_bytes, 1234)

        await self.manager.start_all("download")

        async with self.session() as session:
            resumed = (await session.execute(select(Task).where(
                Task.viewkey.in_([
                    "clean-resume", "source-resume", "normal-resume",
                    "quality-resume", "clean-active", "quality-active",
                ]),
            ))).scalars().all()
        resumed_by_key = {task.viewkey: task for task in resumed}
        self.assertEqual(resumed_by_key["clean-resume"].status, "queued")
        self.assertEqual(resumed_by_key["clean-resume"].phase, "queued_clean_retry")
        self.assertEqual(resumed_by_key["source-resume"].phase, "queued_source_retry")
        self.assertEqual(resumed_by_key["normal-resume"].phase, "")
        self.assertEqual(
            resumed_by_key["quality-resume"].phase,
            "queued_quality_retry:1079",
        )
        self.assertEqual(
            resumed_by_key["clean-active"].phase,
            "queued_clean_resume",
        )
        self.assertEqual(
            resumed_by_key["quality-active"].phase,
            "queued_quality_resume:719",
        )
        self.manager._pump.assert_awaited_once()

    async def test_failed_lock_with_valid_final_is_reconciled_before_retry(self):
        output = Path(self.temp.name) / "final.mp4"
        output.write_bytes(b"validated-media")
        async with self.session() as session:
            task = Task(
                model_id=self.first_id,
                viewkey="reconcile-final",
                model_name="First",
                title="Final",
                url="x",
                task_type="download",
                status="failed",
                local_path=str(output),
                error="DownloadError: 同一输出文件正由任务 77 写入",
            )
            session.add(task)
            await session.commit()
            task_id = task.id

        self.manager._create_upload_task = AsyncMock()
        with patch(
                "backend.task_manager.cleanup_stale_output_lock",
                return_value=True), patch(
                "backend.task_manager.validate_completed_artifact",
                return_value={"size": output.stat().st_size}):
            dry_run = await self.manager.reconcile_failed_downloads(apply=False)
            applied = await self.manager.reconcile_failed_downloads(apply=True)

        self.assertEqual(dry_run["validated"], 1)
        self.assertEqual(dry_run["reconciled"], 0)
        self.assertEqual(applied["reconciled"], 1)
        async with self.session() as session:
            restored = await session.get(Task, task_id)
        self.assertEqual(restored.status, "completed")
        self.assertEqual(restored.phase, "completed")
        self.assertEqual(restored.downloaded_bytes, output.stat().st_size)
        self.manager._create_upload_task.assert_awaited_once_with(task_id)

    async def test_bulk_retry_only_recovers_missing_or_dead_same_task_output_locks(self):
        from backend.downloader import _output_lock_path, _staging_path

        cases = ("dead", "absent", "active", "different_task", "malformed", "oversized")
        ids = {}
        originals = {}
        async with self.session() as session:
            for case in cases:
                output = Path(self.temp.name) / f"{case}.mp4"
                partial = Path(_staging_path(str(output)) + ".part")
                partial.write_bytes(b"retain-partial")
                task = Task(model_id=self.first_id, viewkey=case, model_name="First",
                            title="Recovery", url="x", task_type="download", status="failed",
                            phase="failed", local_path=str(output), downloaded_bytes=14,
                            error="DownloadError: 同一输出文件正由任务 77 写入")
                session.add(task)
                await session.flush()
                ids[case] = task.id
                lock = Path(_output_lock_path(str(output)))
                if case == "absent":
                    continue
                if case == "malformed":
                    payload = b"not-json"
                elif case == "oversized":
                    payload = b" " * 20000 + b"{}"
                else:
                    payload = json.dumps({"pid": 222 if case == "active" else 111,
                                          "task_key": str(task.id + 1000 if case == "different_task" else task.id)}).encode()
                lock.write_bytes(payload)
                originals[lock] = payload
            await session.commit()

        with patch("backend.downloader._pid_alive", side_effect=lambda pid: pid == 222):
            counts = await self.manager.task_counts("download")
            listed = {row["id"]: row for row in await self.manager.list_tasks("download")}
            self.assertEqual(counts["failed_recoverable"], 2)
            self.assertEqual(counts["failed_manual"], 4)
            for case, task_id in ids.items():
                self.assertEqual(listed[task_id]["bulk_recoverable"], case in {"dead", "absent"})
            result = await self.manager.retry_failed("download")

        self.assertEqual(result, {"retried": 2, "reconciled": 0, "skipped_non_retryable": 4})
        async with self.session() as session:
            for case, task_id in ids.items():
                task = await session.get(Task, task_id)
                self.assertEqual(task.status, "queued" if case in {"dead", "absent"} else "failed")
                self.assertEqual(task.downloaded_bytes, 14)
                self.assertEqual(Path(_staging_path(task.local_path) + ".part").read_bytes(), b"retain-partial")
                self.assertFalse(Path(task.local_path).exists())
        for lock, payload in originals.items():
            self.assertEqual(lock.read_bytes(), payload)

    async def test_historical_task_errors_are_scrubbed_only_when_applied(self):
        async with self.session() as session:
            task = Task(
                model_id=self.first_id,
                viewkey="secret-error",
                model_name="First",
                title="Secret",
                url="x",
                task_type="upload",
                status="failed",
                error="MultipartUploadAbort: {'user_key': 'secret-value'}",
            )
            session.add(task)
            await session.commit()
            task_id = task.id

        dry_run = await self.manager.scrub_stored_task_errors(apply=False)
        async with self.session() as session:
            unchanged = await session.get(Task, task_id)
            self.assertIn("secret-value", unchanged.error)
        applied = await self.manager.scrub_stored_task_errors(apply=True)
        async with self.session() as session:
            scrubbed = await session.get(Task, task_id)

        self.assertEqual(dry_run["candidates"], 1)
        self.assertEqual(dry_run["scrubbed"], 0)
        self.assertEqual(applied["scrubbed"], 1)
        self.assertNotIn("secret-value", scrubbed.error)
        self.assertIn("<redacted>", scrubbed.error)

    async def test_delete_all_downloads_does_not_cancel_running_upload(self):
        async with self.session() as session:
            download = Task(
                model_id=self.first_id, viewkey="download-key", model_name="First",
                title="D", url="https://example/d", task_type="download", status="queued",
            )
            upload = Task(
                model_id=self.first_id, viewkey="upload-key", model_name="First",
                title="U", url="https://example/u", task_type="upload", status="running",
            )
            session.add_all([download, upload])
            await session.commit()
            download_id, upload_id = download.id, upload.id

        release = asyncio.Event()

        async def running_upload():
            await release.wait()

        runner = asyncio.create_task(running_upload())
        self.manager._running[upload_id] = runner
        try:
            result = await self.manager.delete_all_tasks("download")
            async with self.session() as session:
                remaining = (await session.execute(
                    select(Task).order_by(Task.id)
                )).scalars().all()
            self.assertEqual(result, {"deleted": 1, "skipped_running_uploads": 0})
            self.assertEqual([row.id for row in remaining], [upload_id])
            self.assertIn(upload_id, self.manager._running)
            self.assertFalse(runner.cancelled())
            self.assertNotEqual(download_id, upload_id)
            self.assertFalse(await self.manager.delete_task(upload_id))
        finally:
            release.set()
            await runner
            self.manager._running.pop(upload_id, None)

    async def test_task_keyset_cursor_continues_status_order_without_duplicates(self):
        async with self.session() as session:
            session.add_all([
                Task(model_id=self.first_id, viewkey="run", model_name="First",
                     title="run", url="x", task_type="download", status="running"),
                Task(model_id=self.first_id, viewkey="queue", model_name="First",
                     title="queue", url="x", task_type="download", status="queued"),
                Task(model_id=self.first_id, viewkey="done", model_name="First",
                     title="done", url="x", task_type="download", status="completed"),
            ])
            await session.commit()
        first = await self.manager.list_tasks("download", limit=2)
        cursor = (1, first[-1]["id"])
        second = await self.manager.list_tasks("download", limit=2, cursor=cursor)
        self.assertEqual([item["status"] for item in first], ["running", "queued"])
        self.assertEqual([item["status"] for item in second], ["completed"])
        self.assertFalse({item["id"] for item in first} & {item["id"] for item in second})

    async def test_patrol_recreates_missing_upload_from_local_artifact(self):
        local = Path(self.temp.name) / "finished.mp4"
        local.write_bytes(b"video")
        async with self.session() as session:
            session.add(Video(
                model_id=self.first_id, viewkey="repair-upload", title="Repair",
                url="https://example/repair", duration=12, dedup_status="missing",
            ))
            session.add(Task(
                model_id=self.first_id, viewkey="repair-upload", model_name="First",
                title="Repair", url="https://example/repair", duration=12,
                task_type="download", status="completed", local_path=str(local),
            ))
            await session.commit()

        result = await self.manager.reconcile_missing_pipeline(self.first_id)

        async with self.session() as session:
            rows = (await session.execute(
                select(Task).where(Task.viewkey == "repair-upload")
                .order_by(Task.task_type)
            )).scalars().all()
        self.assertEqual(result["repaired_uploads"], 1)
        self.assertEqual([(row.task_type, row.status) for row in rows], [
            ("download", "completed"), ("upload", "queued"),
        ])
        self.assertEqual(rows[1].local_path, str(local))

    async def test_patrol_confirms_remote_drift_twice_before_redownload(self):
        async with self.session() as session:
            session.add(Video(
                model_id=self.first_id, viewkey="remote-drift", title="Drift",
                url="https://example/drift", duration=20, dedup_status="missing",
            ))
            session.add_all([
                Task(
                    model_id=self.first_id, viewkey="remote-drift", model_name="First",
                    title="Drift", url="https://example/drift", duration=20,
                    task_type="download", status="completed",
                    local_path=str(Path(self.temp.name) / "missing.mp4"),
                ),
                Task(
                    model_id=self.first_id, viewkey="remote-drift", model_name="First",
                    title="Drift", url="https://example/drift", duration=20,
                    task_type="upload", status="completed", progress=100.0,
                ),
            ])
            await session.commit()

        first = await self.manager.reconcile_missing_pipeline(self.first_id)
        async with self.session() as session:
            upload = (await session.execute(select(Task).where(
                Task.viewkey == "remote-drift", Task.task_type == "upload",
            ))).scalar_one()
            upload_id = upload.id
            self.assertEqual(upload.remote_missing_count, 1)
        self.assertEqual(first["confirmation_pending"], 1)
        self.assertEqual(first["queued_downloads"], 0)

        second = await self.manager.reconcile_missing_pipeline(self.first_id)
        async with self.session() as session:
            rows = (await session.execute(select(Task).where(
                Task.viewkey == "remote-drift",
            ))).scalars().all()
        self.assertEqual(second["repaired_downloads"], 1)
        self.assertEqual(second["queued_downloads"], 1)
        self.assertEqual(len(rows), 2)
        by_type = {row.task_type: row for row in rows}
        self.assertEqual(by_type["download"].status, "queued")
        self.assertEqual(by_type["download"].patrol_recovery_count, 1)
        self.assertEqual(by_type["upload"].id, upload_id)
        self.assertEqual(by_type["upload"].status, "paused")
        self.assertEqual(by_type["upload"].phase, "waiting_download")

        # “全部继续上传”不能越过等待下载门禁；新文件完成后复用同一上传任务 ID。
        await self.manager.start_all("upload")
        recovered = Path(self.temp.name) / "recovered.mp4"
        recovered.write_bytes(b"video")
        async with self.session() as session:
            download = (await session.execute(select(Task).where(
                Task.viewkey == "remote-drift", Task.task_type == "download",
            ))).scalar_one()
            upload = (await session.execute(select(Task).where(
                Task.viewkey == "remote-drift", Task.task_type == "upload",
            ))).scalar_one()
            self.assertEqual(upload.status, "paused")
            download.status = "completed"
            download.local_path = str(recovered)
            await session.commit()
            download_id = download.id

        await self.manager._create_upload_task(download_id)
        async with self.session() as session:
            upload = (await session.execute(select(Task).where(
                Task.viewkey == "remote-drift", Task.task_type == "upload",
            ))).scalar_one()
        self.assertEqual(upload.id, upload_id)
        self.assertEqual(upload.status, "queued")
        self.assertEqual(upload.phase, "")
        self.assertEqual(upload.local_path, str(recovered))

    async def test_patrol_never_silently_requeues_final_download_failure(self):
        async with self.session() as session:
            session.add_all([
                Video(
                    model_id=self.first_id, viewkey="fresh-missing", title="Fresh",
                    url="https://example/fresh", duration=10, dedup_status="missing",
                ),
                Video(
                    model_id=self.first_id, viewkey="failed-download", title="Failed",
                    url="https://example/failed", duration=10, dedup_status="missing",
                ),
                Task(
                    model_id=self.first_id, viewkey="failed-download", model_name="First",
                    title="Failed", url="https://example/failed",
                    task_type="download", status="failed", error="network",
                ),
            ])
            await session.commit()

        result = await self.manager.reconcile_missing_pipeline(self.first_id)

        async with self.session() as session:
            failed = (await session.execute(select(Task).where(
                Task.viewkey == "failed-download",
            ))).scalar_one()
            fresh = (await session.execute(select(Task).where(
                Task.viewkey == "fresh-missing",
            ))).scalar_one()
        self.assertEqual(result["queued_downloads"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(fresh.status, "queued")

    async def test_patrol_report_only_mode_creates_no_tasks(self):
        async with self.session() as session:
            session.add(Video(
                model_id=self.first_id, viewkey="report-only", title="Report",
                url="https://example/report", duration=10, dedup_status="missing",
            ))
            await session.commit()

        result = await self.manager.reconcile_missing_pipeline(
            self.first_id, auto_repair=False)

        async with self.session() as session:
            rows = (await session.execute(select(Task).where(
                Task.viewkey == "report-only",
            ))).scalars().all()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["unmanaged"], 1)
        self.assertEqual(result["queued_downloads"], 0)
        self.assertEqual(rows, [])

    async def test_upload_retry_deadline_survives_manager_recreation(self):
        async with self.session() as session:
            task = Task(
                model_id=self.first_id, viewkey="persisted-retry", model_name="First",
                title="Retry", url="https://example/retry", task_type="upload",
                status="failed", auto_retry_count=1,
                next_retry_at=datetime.now() - timedelta(seconds=1),
            )
            session.add(task)
            await session.commit()
            task_id = task.id

        restored_manager = TaskManager()
        restored_manager._pump = AsyncMock()
        await restored_manager._schedule_pending_upload_retries()
        async with asyncio.timeout(1):
            while True:
                async with self.session() as session:
                    restored = (await session.execute(select(Task).where(
                        Task.id == task_id,
                    ))).scalar_one()
                    if restored.status == "queued":
                        break
                await asyncio.sleep(0)

        self.assertEqual(restored.auto_retry_count, 1)
        self.assertIsNone(restored.next_retry_at)
        self.assertIn("自动重试 1/2", restored.error)
        await asyncio.gather(
            *restored_manager._upload_retry_waiters.values(),
            return_exceptions=True,
        )


class UploadRetryAndPatrolTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_pump_requests_are_coalesced(self):
        manager = TaskManager()
        manager._pump = AsyncMock()

        for _ in range(100):
            manager._request_pump()
        runner = manager._pump_runner
        self.assertIsNotNone(runner)
        await runner

        manager._pump.assert_awaited_once()

    async def test_pump_runner_retries_transient_failure_without_losing_wakeup(self):
        manager = TaskManager()
        manager._pump = AsyncMock(side_effect=[RuntimeError("locked"), None])

        with patch.object(task_manager_module.logger, "exception"):
            manager._request_pump()
            runner = manager._pump_runner
            self.assertIsNotNone(runner)
            await asyncio.wait_for(runner, timeout=1)

        self.assertEqual(manager._pump.await_count, 2)
        self.assertIsNone(manager._pump_runner)

    async def test_upload_confirmation_uses_authoritative_file_id(self):
        manager = TaskManager()
        manager._upload_verify_delays = (0, 0)
        infos = [
            None,
            {
                "fid": "fid-1",
                "cid": "cid-1",
                "n": "Title [abcdef1234567].mp4",
                "s": 123,
            },
        ]
        with patch.object(task_manager_module.cloud, "get_file_info",
                          new=AsyncMock(side_effect=infos)) as lookup:
            info = await manager._confirm_remote_upload(
                "cid-1",
                "abcdef1234567",
                "Title [abcdef1234567].mp4",
                123,
                "fid-1",
            )

        self.assertEqual(lookup.await_count, 2)
        lookup.assert_awaited_with("fid-1")
        self.assertEqual(info["fid"], "fid-1")

    async def test_confirmed_upload_is_merged_into_complete_dedup_cache(self):
        manager = TaskManager()
        original = task_manager_module.dedup.build_cloud_index([{
            "fid": "old-file",
            "n": "Old [1111111111111].mp4",
            "play_long": 60,
        }])
        built_at = time.monotonic()
        manager._dedup_cache["cid-1"] = (built_at, original)

        await manager._merge_confirmed_cloud_file("cid-1", {
            "fid": "new-file",
            "n": "New [abcdef1234567].mp4",
            "play_long": 90,
        })

        cached_at, index = manager._dedup_cache["cid-1"]
        self.assertEqual(cached_at, built_at)
        self.assertEqual(
            task_manager_module.dedup.match_video(
                "abcdef1234567", 90, index)[0],
            "exists",
        )
        self.assertIn(90, index["duration_buckets"])

    async def test_upload_dedup_includes_files_waiting_for_media_index(self):
        manager = TaskManager()
        pending = [{
            "fid": "fid-1",
            "n": "Title [abcdef1234567].mp4",
            "s": 123,
        }]
        with patch.object(task_manager_module.cloud, "list_all_files",
                          new=AsyncMock(return_value=pending)) as listing:
            index = await manager._get_dedup_index("cid-1")

        listing.assert_awaited_once_with("cid-1", only_video=False)
        self.assertIn("abcdef1234567", index["viewkeys"])

    async def test_concurrent_upload_dedup_scan_is_single_flight_per_cid(self):
        manager = TaskManager()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def listing(_cid, *, only_video):
            nonlocal calls
            calls += 1
            self.assertFalse(only_video)
            started.set()
            await release.wait()
            return [{"fid": "fid-1", "n": "Title [abcdef1234567].mp4"}]

        with patch.object(task_manager_module.cloud, "list_all_files", new=listing):
            first = asyncio.create_task(manager._get_dedup_index("cid-1"))
            await started.wait()
            second = asyncio.create_task(manager._get_dedup_index("cid-1"))
            await asyncio.sleep(0)
            release.set()
            indexes = await asyncio.gather(first, second)

        self.assertEqual(calls, 1)
        self.assertIs(indexes[0], indexes[1])

    async def test_unconfirmed_upload_keeps_local_file_and_marks_failed(self):
        manager = TaskManager()
        task = SimpleNamespace(
            id=10, model_id=1, model_name="Model", model_cloud_cid="cid-1",
            viewkey="abcdef1234567", title="Title", duration=10,
            local_path="keep-me.mp4",
        )
        statuses = []

        async def set_status(_task_id, **fields):
            statuses.append(fields)
            return task if fields.get("_return_task") else None

        manager._set_status = set_status
        manager._get_dedup_index = AsyncMock(return_value={})
        manager._confirm_remote_upload = AsyncMock(
            side_effect=UploadConfirmationError("未确认"))
        manager._schedule_auto_requeue = Mock()
        manager._cleanup_local = Mock()

        def discard(coro):
            coro.close()

        manager._spawn_bg = discard
        with patch.object(task_manager_module.dedup, "match_video",
                          return_value=("missing", None)), patch.object(
                task_manager_module.cloud, "upload_file",
                new=AsyncMock(return_value={"file_id": "fid-1"})), patch.object(
                task_manager_module.os.path, "getsize", return_value=123):
            await manager._run_upload(10)

        failed = next(item for item in statuses if item.get("status") == "failed")
        self.assertEqual(failed["auto_retry_count"], manager._auto_requeue_max)
        self.assertIsNone(failed["next_retry_at"])
        manager._cleanup_local.assert_not_called()
        manager._schedule_auto_requeue.assert_not_called()

    async def test_upload_failure_invalidates_dedup_cache(self):
        manager = TaskManager()
        manager._dedup_cache["cid-1"] = (time.monotonic(), {})
        task = SimpleNamespace(
            id=9, model_id=1, model_name="Model", model_cloud_cid="cid-1",
            viewkey="abcdef1234567", title="Title", duration=10,
            local_path="missing.mp4",
        )
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
        with patch.object(task_manager_module.dedup, "match_video", return_value=("missing", None)), \
                patch.object(task_manager_module.cloud, "upload_file",
                             new=AsyncMock(side_effect=RuntimeError("network"))):
            await manager._run_upload(9)

        self.assertNotIn("cid-1", manager._dedup_cache)
        self.assertTrue(any(item.get("status") == "failed" for item in statuses))
        manager._schedule_auto_requeue.assert_called_once()

    async def test_manual_patrol_runs_when_scheduler_is_disabled(self):
        patrol = Patrol()
        patrol.enabled = False
        patrol._task = None
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_once():
            started.set()
            await release.wait()
            return {"scanned": 0}

        patrol.run_once = run_once
        await patrol.trigger_now()
        await asyncio.wait_for(started.wait(), timeout=1)
        manual = patrol._manual_task
        self.assertIsNotNone(manual)
        release.set()
        await asyncio.wait_for(manual, timeout=1)
        self.assertIsNone(patrol._manual_task)


if __name__ == "__main__":
    unittest.main()
