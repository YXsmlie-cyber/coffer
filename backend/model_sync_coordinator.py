"""上传者完整同步协调器。

职责：
  - 把榜单/发现页的浏览器端 fan-out 改为后端批量任务；
  - 每批只读取一次 VIDEO HUB 根目录，批次结束即丢弃；
  - 对同一 model_id 串行化，对全部同步和数据库快照写入做全局限流；
  - 保存短期内存任务状态并通过 WebSocket 广播进度。

这里不缓存历史同步结果。每个任务都会重新抓取 PH，并重新读取本次涉及的
115 上传者目录。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import select

from .cloud115 import Cloud115Client, _norm_name, cloud
from .config import settings
from .database import Model, async_session
from .model_sync import sync_model_core


_NO_CID_HINT = object()
_JOB_RETENTION = timedelta(minutes=60)
_MAX_RETAINED_JOBS = 20
_MAX_JOB_ERRORS = 100
_MAX_WIRE_NOT_FOUND_IDS = 100


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class _SyncModel:
    """批次只持有调度需要的字段，不保留整批 ORM/头像元数据。"""

    id: int
    name: str
    cloud_cid: str


@dataclass
class ModelSyncJob:
    id: str
    requested_ids: list[int]
    model_ids: list[int]
    not_found_ids: list[int]
    state: str = "queued"
    done: int = 0
    succeeded: int = 0
    failed: int = 0
    active_model_ids: set[int] = field(default_factory=set)
    errors: list[dict] = field(default_factory=list)
    error_groups: list[dict] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str | None = None
    timings: dict[str, float] = field(default_factory=lambda: {
        "root_scan_ms": 0.0,
        "ph_ms": 0.0,
        "cloud_ms": 0.0,
        "dedup_ms": 0.0,
        "db_ms": 0.0,
        "wall_ms": 0.0,
    })
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def total(self) -> int:
        return len(self.requested_ids)

    def wire(self) -> dict:
        return {
            "job_id": self.id,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "active_model_ids": sorted(self.active_model_ids),
            "not_found_ids": list(self.not_found_ids[:_MAX_WIRE_NOT_FOUND_IDS]),
            "not_found_truncated": max(
                0, len(self.not_found_ids) - _MAX_WIRE_NOT_FOUND_IDS),
            "errors": list(self.errors),
            "error_groups": list(self.error_groups),
            "errors_truncated": max(0, self.failed - len(self.errors)),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "failure_reason": self.failure_reason,
            "timings": {key: round(value, 3) for key, value in self.timings.items()},
        }


class ModelSyncCoordinator:
    def __init__(
        self,
        *,
        session_factory=async_session,
        cloud_client: Cloud115Client = cloud,
        sync_func=sync_model_core,
    ):
        self._session_factory = session_factory
        self._cloud = cloud_client
        self._sync_func = sync_func
        self._sync_sem = asyncio.Semaphore(settings.model_sync_concurrency)
        self._write_sem = asyncio.Semaphore(1)
        self._model_locks: dict[int, asyncio.Lock] = {}
        self._inflight: dict[int, asyncio.Task] = {}
        # 每个共享同步当前还有几个等待方；归零时停止同步，避免无人等待的孤儿任务。
        self._waiters: dict[asyncio.Task, int] = {}
        self._inflight_lock = asyncio.Lock()
        self._job_lock = asyncio.Lock()
        self._jobs: OrderedDict[str, ModelSyncJob] = OrderedDict()
        self._broadcast: Callable[[dict], Awaitable[None]] | None = None

    def set_broadcast(self, callback: Callable[[dict], Awaitable[None]] | None):
        self._broadcast = callback

    async def _emit(self, payload: dict):
        if self._broadcast is None:
            return
        try:
            await self._broadcast(payload)
        except Exception:
            # 页面连接失败不能影响同步和数据库快照。
            pass

    def _prune_jobs(self):
        cutoff = datetime.now() - _JOB_RETENTION
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is not None and job.finished_at < cutoff:
                self._jobs.pop(job_id, None)
        completed_ids = [
            job_id for job_id, job in self._jobs.items()
            if job.finished_at is not None
        ]
        for job_id in completed_ids[:-_MAX_RETAINED_JOBS]:
            self._jobs.pop(job_id, None)

    def _find_active_job(
        self, signature: tuple[int, ...],
    ) -> ModelSyncJob | None:
        return next((
            item for item in reversed(self._jobs.values())
            if item.state in {"queued", "running"}
            and tuple(item.requested_ids) == signature
        ), None)

    async def _partition_model_ids(
        self, model_ids: list[int],
    ) -> tuple[list[int], list[int]]:
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(Model.id).where(Model.id.in_(model_ids))
            )).scalars().all()
        existing = set(rows)
        return (
            [model_id for model_id in model_ids if model_id in existing],
            [model_id for model_id in model_ids if model_id not in existing],
        )

    async def create_job(self, model_ids: list[int]) -> ModelSyncJob:
        """创建并启动批量同步；ID 已由 API 做长度校验，这里负责去重和查库。"""
        unique_ids = list(dict.fromkeys(int(model_id) for model_id in model_ids))
        signature = tuple(unique_ids)

        async with self._job_lock:
            # 双击、多个页面或 WS 补刷新提交同一批 ID 时复用正在运行的 Job；
            # 调用方得到同一 job_id，不再重复查库、根目录扫描和 worker 队列。
            self._prune_jobs()
            existing_job = self._find_active_job(signature)
            if existing_job is not None:
                return existing_job

        accepted, not_found = await self._partition_model_ids(unique_ids)

        job = ModelSyncJob(
            id=uuid.uuid4().hex,
            requested_ids=unique_ids,
            model_ids=accepted,
            not_found_ids=not_found,
            done=len(not_found),
            failed=len(not_found),
            errors=[{
                "model_id": model_id,
                "error": "上传者不存在",
            } for model_id in not_found[:_MAX_JOB_ERRORS]],
        )
        async with self._job_lock:
            # 查库期间另一个同签名请求可能已入场，再检查一次关闭竞态窗口。
            existing_job = self._find_active_job(signature)
            if existing_job is not None:
                return existing_job
            self._prune_jobs()
            self._jobs[job.id] = job
            job.task = asyncio.create_task(self._run_job(job))
        return job

    def get_job(self, job_id: str) -> ModelSyncJob | None:
        self._prune_jobs()
        return self._jobs.get(str(job_id or "").strip())

    async def sync_model(self, model_id: int) -> dict | None:
        """兼容单卡片/旧 REST 路由：等待并返回这一轮全量同步结果。"""
        return await self._join_or_start(model_id)

    async def _join_or_start(
        self, model_id: int, *,
        resolved_cloud_cid: str | None | object = _NO_CID_HINT,
    ) -> dict | None:
        async with self._inflight_lock:
            task = self._inflight.get(model_id)
            # 正在被取消的同步不能再接新等待方，否则新等待方会意外收到 CancelledError；
            # 新任务会在模特锁上等旧任务退出后再开始。
            if task is None or task.done() or task.cancelling():
                task = asyncio.create_task(self._sync_one(
                    model_id, resolved_cloud_cid=resolved_cloud_cid))
                self._inflight[model_id] = task

                def finished(done: asyncio.Task, *, expected=task, key=model_id):
                    if self._inflight.get(key) is expected:
                        self._inflight.pop(key, None)

                task.add_done_callback(finished)
            self._waiters[task] = self._waiters.get(task, 0) + 1
        # 一个等待方取消不能取消其他 Job/单卡片正在共享的真实同步；但最后一个等待方
        # 也离开时（例如巡查的单模特总预算到期），继续跑只会占着并发名额，到自身超时后
        # 还会以“exception in shielded future”刷 ERROR 日志，所以此时一并停止。
        abandoned = False
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned = True
            raise
        finally:
            remaining = self._waiters.get(task, 1) - 1
            if remaining > 0:
                self._waiters[task] = remaining
            else:
                self._waiters.pop(task, None)
                if abandoned and not task.done():
                    task.cancel()

    async def _sync_one(
        self,
        model_id: int,
        *,
        resolved_cloud_cid: str | None | object = _NO_CID_HINT,
    ) -> dict | None:
        lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        try:
            async with lock:
                async with self._sync_sem:
                    kwargs = {
                        "session_factory": self._session_factory,
                        "cloud_client": self._cloud,
                        "write_semaphore": self._write_sem,
                    }
                    if resolved_cloud_cid is not _NO_CID_HINT:
                        kwargs["resolved_cloud_cid"] = resolved_cloud_cid
                    # 排队等待 semaphore 不计入预算；真正开始单模特网络/去重/
                    # 短事务后才启动 timeout，所有 joiner 共享同一结果或 TimeoutError。
                    return await asyncio.wait_for(
                        self._sync_func(model_id, **kwargs),
                        timeout=float(settings.model_sync_timeout_seconds),
                    )
        finally:
            if not lock.locked() and self._model_locks.get(model_id) is lock:
                self._model_locks.pop(model_id, None)

    async def _load_models(
        self, model_ids: list[int],
    ) -> tuple[list[_SyncModel], list[int]]:
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(Model.id, Model.name, Model.cloud_cid)
                .where(Model.id.in_(model_ids))
            )).all()
        by_id = {row.id: _SyncModel(*row) for row in rows}
        return (
            [by_id[model_id] for model_id in model_ids if model_id in by_id],
            [model_id for model_id in model_ids if model_id not in by_id],
        )

    @staticmethod
    def _record_disappeared_models(
        job: ModelSyncJob, model_ids: list[int],
    ) -> None:
        """补齐任务创建后、worker 启动前被删除的上传者计数。"""
        if not model_ids:
            return
        job.not_found_ids.extend(model_ids)
        job.done += len(model_ids)
        job.failed += len(model_ids)
        remaining = max(0, _MAX_JOB_ERRORS - len(job.errors))
        job.errors.extend({
            "model_id": model_id,
            "error": "上传者在同步开始前被删除",
        } for model_id in model_ids[:remaining])

    async def _finish_model(
        self,
        job: ModelSyncJob,
        model_id: int,
        *,
        result: dict | None = None,
        error: str | None = None,
        record_error: bool = True,
    ):
        job.active_model_ids.discard(model_id)
        job.done += 1
        if error:
            job.failed += 1
            if record_error and len(job.errors) < _MAX_JOB_ERRORS:
                job.errors.append({"model_id": model_id, "error": error[:500]})
        else:
            job.succeeded += 1
            for key, value in (result or {}).get("timings", {}).items():
                if key in job.timings:
                    try:
                        job.timings[key] += float(value or 0.0)
                    except (TypeError, ValueError):
                        pass
        await self._emit({
            "type": "model_sync_progress",
            "job_id": job.id,
            "model_id": model_id,
            "phase": "finished",
            "done": job.done,
            "total": job.total,
            "succeeded": job.succeeded,
            "failed": job.failed,
            "error": error,
        })

    async def _run_job(self, job: ModelSyncJob):
        wall_started = time.perf_counter()
        per_model_timeout = float(settings.model_sync_timeout_seconds)
        job.state = "running"
        job.started_at = datetime.now()
        try:
            models, disappeared_ids = await self._load_models(job.model_ids)
            self._record_disappeared_models(job, disappeared_ids)
            unbound = [model for model in models if not str(model.cloud_cid or "").strip()]
            directory_map: dict[str, str] = {}
            root_error: str | None = None
            if unbound:
                root_started = time.perf_counter()
                try:
                    directories = await asyncio.wait_for(
                        self._cloud.list_model_dirs(), timeout=per_model_timeout)
                    for item in directories:
                        name = _norm_name(str(item.get("name") or ""))
                        cid = str(item.get("cid") or "").strip()
                        if name and cid:
                            # 与原 find_subdir 一致：同一规范化名称取列表中的首项。
                            directory_map.setdefault(name, cid)
                except Exception as error:
                    root_error = str(error).strip() or type(error).__name__
                finally:
                    job.timings["root_scan_ms"] = round(
                        (time.perf_counter() - root_started) * 1000.0, 3)

            queue: asyncio.Queue[_SyncModel] = asyncio.Queue()
            for model in models:
                queue.put_nowait(model)

            if root_error:
                affected = sum(
                    not str(model.cloud_cid or "").strip() for model in models)
                if affected:
                    grouped_error = f"读取 115 根目录失败：{root_error}"[:500]
                    job.error_groups.append({
                        "scope": "root_scan",
                        "error": grouped_error,
                        "affected": affected,
                    })
                    if len(job.errors) < _MAX_JOB_ERRORS:
                        job.errors.append({"model_id": None, "error": grouped_error})
            timeout_group = {
                "scope": "model_timeout", "error": "单模特同步超过时间预算", "affected": 0,
            }

            async def worker():
                while True:
                    try:
                        model = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    job.active_model_ids.add(model.id)
                    await self._emit({
                        "type": "model_sync_progress",
                        "job_id": job.id,
                        "model_id": model.id,
                        "phase": "started",
                        "done": job.done,
                        "total": job.total,
                        "succeeded": job.succeeded,
                        "failed": job.failed,
                        "error": None,
                    })
                    if not str(model.cloud_cid or "").strip() and root_error:
                        await self._finish_model(
                            job, model.id,
                            error=f"读取 115 根目录失败：{root_error}",
                            record_error=False,
                        )
                        continue
                    cid_hint: str | None = str(model.cloud_cid or "").strip() or (
                        directory_map.get(_norm_name(model.name))
                    )
                    try:
                        result = await self._join_or_start(
                            model.id, resolved_cloud_cid=cid_hint)
                        if result is None:
                            await self._finish_model(
                                job, model.id, error="上传者在同步期间被删除")
                        elif result.get("status") == "source_unavailable":
                            await self._finish_model(
                                job,
                                model.id,
                                error=result.get("error") or "源站主页不可用或已删除",
                            )
                        else:
                            await self._finish_model(job, model.id, result=result)
                    except asyncio.TimeoutError:
                        timeout_group["affected"] += 1
                        await self._finish_model(
                            job, model.id, error=timeout_group["error"],
                            record_error=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        message = str(error).strip() or type(error).__name__
                        await self._finish_model(job, model.id, error=message)

            worker_count = min(
                max(1, settings.model_sync_concurrency),
                max(1, len(models)),
            )
            await asyncio.gather(*(worker() for _ in range(worker_count)))
            if timeout_group["affected"]:
                job.error_groups.append(timeout_group)
            job.state = "completed"
        except asyncio.CancelledError:
            job.state = "failed"
            job.failure_reason = "同步因后端关闭而中断"
            raise
        except Exception as error:
            job.state = "failed"
            job.failure_reason = str(error).strip() or type(error).__name__
        finally:
            job.active_model_ids.clear()
            job.finished_at = datetime.now()
            job.timings["wall_ms"] = round(
                (time.perf_counter() - wall_started) * 1000.0, 3)
            await self._emit({
                "type": "model_sync_progress",
                "job_id": job.id,
                "model_id": None,
                "phase": "job_finished",
                "done": job.done,
                "total": job.total,
                "succeeded": job.succeeded,
                "failed": job.failed,
                "error": job.failure_reason,
                "state": job.state,
            })

    async def shutdown(self):
        tasks = [
            job.task for job in self._jobs.values()
            if job.task is not None and not job.task.done()
        ]
        tasks.extend(
            task for task in self._inflight.values()
            if task not in tasks and not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


model_sync_coordinator = ModelSyncCoordinator()
