import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.notifier as notifier_module
from backend.database import Base, RuntimeSetting
from backend.notifier import (
    EVENT_DOWNLOAD_DONE,
    EVENT_ERROR,
    EVENT_NEW_MISSING,
    EVENT_UPLOAD_DONE,
    Notifier,
)


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {"code": 200}


class _SerialClient:
    def __init__(self):
        self.is_closed = False
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def post(self, _url, json):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return _Response()

    async def aclose(self):
        self.is_closed = True


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        db_path = (Path(self.directory.name) / "notifier.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(
            notifier_module, "async_session", self.session)
        self.session_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.engine.dispose()
        self.directory.cleanup()

    async def test_new_config_defaults_to_batch_instead_of_per_item_pushes(self):
        notifier = Notifier()

        self.assertTrue(notifier.event_flags[EVENT_ERROR])
        self.assertFalse(notifier.event_flags[EVENT_DOWNLOAD_DONE])
        self.assertFalse(notifier.event_flags[EVENT_UPLOAD_DONE])

        await notifier.close()

    async def test_runtime_configuration_is_persisted_and_restored(self):
        notifier = Notifier()
        await notifier.set_config(
            enabled=True,
            bark_url="https://api.day.app/example-key",
            events={
                EVENT_DOWNLOAD_DONE: True,
                EVENT_ERROR: False,
            },
        )

        restored = Notifier()
        await restored.restore_config()

        self.assertTrue(restored.enabled)
        self.assertEqual(
            restored.bark_url, "https://api.day.app/example-key")
        self.assertTrue(restored.event_flags[EVENT_DOWNLOAD_DONE])
        self.assertFalse(restored.event_flags[EVENT_ERROR])
        await notifier.close()
        await restored.close()

    async def test_invalid_url_is_rejected_without_persisting_partial_config(self):
        notifier = Notifier()

        with self.assertRaisesRegex(ValueError, "HTTP"):
            await notifier.set_config(
                enabled=True,
                bark_url="file:///tmp/device-key",
                events={EVENT_ERROR: False},
            )

        async with self.session() as session:
            count = (await session.execute(
                select(func.count(RuntimeSetting.key))
            )).scalar_one()
        self.assertEqual(count, 0)
        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier.bark_url, "")
        self.assertTrue(notifier.event_flags[EVENT_ERROR])
        await notifier.close()

    async def test_memory_state_changes_only_after_database_commit(self):
        notifier = Notifier()
        with patch.object(
            notifier,
            "_persist_settings",
            new=AsyncMock(side_effect=RuntimeError("database locked")),
        ):
            with self.assertRaisesRegex(RuntimeError, "database locked"):
                await notifier.set_config(
                    enabled=True,
                    bark_url="https://api.day.app/example-key",
                    events={EVENT_ERROR: False},
                )

        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier.bark_url, "")
        self.assertTrue(notifier.event_flags[EVENT_ERROR])
        await notifier.close()

    async def test_dense_completion_events_are_coalesced(self):
        notifier = Notifier()
        notifier.enabled = True
        notifier.bark_url = "https://api.day.app/example-key"
        notifier.event_flags[EVENT_DOWNLOAD_DONE] = True
        notifier._completion_debounce_seconds = 0.01
        notifier.push = AsyncMock(return_value=True)

        await asyncio.gather(
            notifier.notify_download_done("A", "one"),
            notifier.notify_download_done("B", "two"),
            notifier.notify_download_done("C", "three"),
            notifier.notify_download_done("D", "four"),
        )
        await asyncio.sleep(0.05)

        notifier.push.assert_awaited_once()
        title, body = notifier.push.await_args.args
        self.assertEqual(title, "下载完成 × 4")
        self.assertIn("本轮完成 4 个视频", body)
        self.assertIn("另有 1 个", body)
        await notifier.close()

    async def test_pushes_are_serialized_and_reuse_one_client(self):
        notifier = Notifier()
        notifier.bark_url = "https://api.day.app/example-key"
        notifier._push_min_interval = 0
        client = _SerialClient()
        notifier._client = client

        results = await asyncio.gather(
            notifier.push("one", "body"),
            notifier.push("two", "body"),
            notifier.push("three", "body"),
        )

        self.assertEqual(results, [True, True, True])
        self.assertEqual(client.calls, 3)
        self.assertEqual(client.max_active, 1)
        await notifier.close()
        self.assertTrue(client.is_closed)

    async def test_connect_failure_is_retried_once_but_read_failure_is_not(self):
        """连不上时请求没送达，补发一次；已发出后的超时可能已送达，不能重发。"""
        import httpx

        class _FlakyClient(_SerialClient):
            def __init__(self, errors):
                super().__init__()
                self.errors = list(errors)

            async def post(self, url, json):
                self.calls += 1
                if self.errors:
                    raise self.errors.pop(0)
                return _Response()

        with patch.object(notifier_module, "_PUSH_CONNECT_RETRY_DELAY", 0):
            notifier = Notifier()
            notifier.bark_url = "https://api.day.app/example-key"
            notifier._push_min_interval = 0
            flaky = _FlakyClient([httpx.ConnectError("proxy blip")])
            notifier._client = flaky
            self.assertTrue(await notifier.push("one", "body"))
            self.assertEqual(flaky.calls, 2)

            twice = _FlakyClient([httpx.ConnectError("a"), httpx.ConnectError("b")])
            notifier._client = twice
            self.assertFalse(await notifier.push("two", "body"))
            self.assertEqual(twice.calls, 2)

            sent = _FlakyClient([httpx.ReadTimeout("maybe delivered")])
            notifier._client = sent
            self.assertFalse(await notifier.push("three", "body"))
            self.assertEqual(sent.calls, 1)

    async def test_new_missing_patrol_details_are_sent_as_one_summary(self):
        notifier = Notifier()
        notifier.enabled = True
        notifier.bark_url = "https://api.day.app/example-key"
        notifier.event_flags[EVENT_NEW_MISSING] = True
        notifier.push = AsyncMock(return_value=True)

        await notifier.notify_new_missing_summary([
            {"model_name": "Alpha", "newly_missing": 2, "total_missing": 5},
            {"model_name": "Alpha", "count": 1, "total_missing": 6},
            {"name": "Beta", "count": 4},
        ])

        notifier.push.assert_awaited_once()
        title, body = notifier.push.await_args.args[:2]
        self.assertEqual(title, "巡查新发现 7 个视频")
        self.assertIn("影响 2 位模特", body)
        self.assertIn("Alpha +3（累计 6）", body)
        self.assertIn("Beta +4", body)
        await notifier.close()


if __name__ == "__main__":
    unittest.main()
