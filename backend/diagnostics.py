"""有界运行指标；仅记录固定类别、耗时和计数，不保存请求/SQL/媒体内容。"""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
import math
import json
import threading
import time
import uuid


request_id = ContextVar("request_id", default="-")


class RuntimeDiagnostics:
    def __init__(self, *, max_series=96, sample_size=256, clock=time.monotonic):
        self._max_series = max_series
        self._sample_size = sample_size
        self._clock = clock
        self._started = clock()
        self._series = {}
        self._lock = threading.Lock()
        self._monitor = None

    def observe(self, name: str, seconds: float):
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self._lock:
            # 额外一个汇总桶吸收未知类别，输入再多也不能扩大内存上限。
            if name not in self._series and len(self._series) >= self._max_series:
                name = "other"
            bucket = self._series.setdefault(name, {
                "count": 0, "sum": 0.0, "max": 0.0,
                "samples": deque(maxlen=self._sample_size),
            })
            bucket["count"] += 1
            bucket["sum"] += seconds
            bucket["max"] = max(bucket["max"], seconds)
            bucket["samples"].append(seconds)

    def snapshot(self):
        with self._lock:
            rows = [(name, dict(bucket, samples=list(bucket["samples"])))
                    for name, bucket in self._series.items()]
        timings = {}
        for name, bucket in rows:
            samples = sorted(bucket.pop("samples"))
            timings[name] = {
                "count": bucket["count"], "window_samples": len(samples),
                "mean_ms": round(bucket["sum"] / bucket["count"] * 1000, 3),
                "p95_ms": round(samples[max(0, math.ceil(len(samples) * .95) - 1)] * 1000, 3),
                "max_ms": round(bucket["max"] * 1000, 3),
            }
        return {"uptime_seconds": round(self._clock() - self._started, 1),
                "thread_count": threading.active_count(),
                "sample_limit_per_series": self._sample_size, "timings": timings}

    def start(self):
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._sample_loop(), name="diagnostics-loop")

    async def stop(self):
        task, self._monitor = self._monitor, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _sample_loop(self):
        while True:
            started = self._clock()
            await asyncio.sleep(.5)
            self.observe("event_loop.lag", max(0.0, self._clock() - started - .5))


diagnostics = RuntimeDiagnostics()


def read_build_identity(app_dir):
    """在线程中读取有大小上限的发行标识，不输出完整清单或任意字段。"""
    try:
        with (app_dir / "build-manifest.json").open("rb") as handle:
            content = handle.read(128 * 1024 + 1)
        if len(content) <= 128 * 1024:
            metadata = json.loads(content)
            if isinstance(metadata, dict):
                return {"mode": "packaged", "commit": metadata.get("commit"),
                        "python": metadata.get("python")}
    except (OSError, ValueError):
        pass
    return {"mode": "source"}


class DiagnosticsMiddleware:
    def __init__(self, app, *, recorder=diagnostics):
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = request_id.set(uuid.uuid4().hex[:16])
        started = time.monotonic()
        status = 500

        async def response(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []),
                    (b"x-ph115-request-id", request_id.get().encode("ascii"))]}
            await send(message)

        try:
            await self.app(scope, receive, response)
        finally:
            # 路由模板不含真实 model_id、viewkey、查询参数或 Cookie。
            route = getattr(scope.get("route"), "path", "unmatched")
            try:
                method = scope.get("method")
                if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
                    method = "OTHER"
                self.recorder.observe(f"http.{method}.{route}.{status}",
                                      time.monotonic() - started)
            except Exception:
                pass  # 诊断失败不能改写 HTTP 响应或原异常。
            request_id.reset(token)


def record_timing(name, seconds):
    try:
        diagnostics.observe(name, seconds)
    except Exception:
        pass


def install_sql_observer(engine, recorder=diagnostics):
    """只统计 SQL 类型与耗时，不记录语句、参数或绑定数据。"""
    from sqlalchemy import event

    if getattr(engine.sync_engine, "_ph115_diagnostics_installed", False):
        return
    engine.sync_engine._ph115_diagnostics_installed = True

    def before(connection, _cursor, statement, _parameters, _context, _executemany):
        kind = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
        if kind not in {"SELECT", "WITH", "UPDATE", "INSERT", "DELETE", "PRAGMA"}:
            kind = "OTHER"
        connection.info["ph115_sql_started"] = (kind, time.monotonic())

    def finish(connection):
        sample = connection.info.pop("ph115_sql_started", None)
        if sample:
            try:
                recorder.observe(f"sql.{sample[0]}", time.monotonic() - sample[1])
            except Exception:
                pass

    def after(connection, *_args):
        finish(connection)

    def failed(context):
        if context.connection is not None:
            finish(context.connection)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    event.listen(engine.sync_engine, "handle_error", failed)
