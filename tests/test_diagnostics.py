"""运行诊断的有界资源、内容隔离和生命周期回归。"""
import asyncio
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from backend.diagnostics import DiagnosticsMiddleware, RuntimeDiagnostics, request_id
from backend.logging_setup import log_handlers


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_export_uses_existing_remote_session_guard(self):
        from backend.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            denied = await client.get("/api/diagnostics", headers={"X-PH115-Remote": "1"})
            local = await client.get("/api/diagnostics")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(local.status_code, 200)
        self.assertEqual(local.headers["cache-control"], "no-store")
        self.assertIn("attachment", local.headers["content-disposition"])
        self.assertEqual(local.json()["schema_version"], 1)
        for key in ("local_path", "viewkey", "cookie", "model_name"):
            self.assertNotIn(f'"{key}":', local.text)

    async def test_sql_observer_never_records_parameters_or_breaks_queries(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from backend.diagnostics import install_sql_observer
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        recorder = RuntimeDiagnostics()
        install_sql_observer(engine, recorder)
        install_sql_observer(engine, recorder)
        try:
            async with engine.connect() as connection:
                self.assertEqual(await connection.scalar(text("SELECT :secret"), {"secret": "PRIVATE"}), "PRIVATE")
            self.assertEqual(recorder.snapshot()["timings"]["sql.SELECT"]["count"], 1)
            self.assertNotIn("PRIVATE", str(recorder.snapshot()))
        finally:
            await engine.dispose()

    async def test_request_contents_never_enter_diagnostic_labels(self):
        recorder = RuntimeDiagnostics()
        app = FastAPI()
        app.add_middleware(DiagnosticsMiddleware, recorder=recorder)

        @app.get("/api/example/{identity}")
        async def example(identity: str):
            return {"ok": True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/example/PRIVATE?cookie=SECRET", headers={"Cookie": "TOKEN=secret"})
        self.assertEqual(response.json(), {"ok": True})
        self.assertRegex(response.headers["x-ph115-request-id"], r"^[a-f0-9]{16}$")
        snapshot = str(recorder.snapshot())
        self.assertIn("/api/example/{identity}", snapshot)
        for secret in ("PRIVATE", "SECRET", "TOKEN", "secret"):
            self.assertNotIn(secret, snapshot)
        self.assertEqual(request_id.get(), "-")

    async def test_observer_failure_does_not_break_http_response(self):
        app = FastAPI()
        app.add_middleware(DiagnosticsMiddleware, recorder=Mock(observe=Mock(side_effect=RuntimeError())))

        @app.get("/ok")
        async def ok():
            return {"ok": True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ok")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_id.get(), "-")

    async def test_series_samples_and_monitor_are_bounded(self):
        recorder = RuntimeDiagnostics(max_series=3, sample_size=8)
        for index in range(1000):
            recorder.observe(f"series-{index}", .01)
        snapshot = recorder.snapshot()
        self.assertLessEqual(len(snapshot["timings"]), 4)
        self.assertTrue(all(item["window_samples"] <= 8 for item in snapshot["timings"].values()))
        recorder.start()
        original = recorder._monitor
        recorder.start()
        self.assertIs(recorder._monitor, original)
        await recorder.stop()
        self.assertTrue(original.done())

    def test_log_rotation_keeps_bounded_files_and_request_context(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = log_handlers(directory, console=False, max_bytes=200, backup_count=2)[0]
            logger = logging.getLogger("ph115-test-rotation")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            token = request_id.set("test-request")
            try:
                for _ in range(30):
                    logger.info("example line for bounded rotation")
            finally:
                request_id.reset(token)
                logger.removeHandler(handler)
                handler.close()
            files = list(Path(directory).glob("app.log*"))
            self.assertEqual(len(files), 3)
            self.assertIn("request=test-request", (Path(directory) / "app.log").read_text(encoding="utf-8"))
