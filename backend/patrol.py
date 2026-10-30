"""自动巡查更新。

定时扫描已绑定 115 文件夹（cloud_cid 非空）的模特，重新爬取其视频列表并与
115 去重，再把每个 missing 视频交给任务管理器做流水线对账与有限自愈。

调度器使用命令队列区分“立即执行”和“只重算间隔”，避免 Event 先 clear 后 wait
造成的丢唤醒竞态。运行中的重复立即请求会合并为最多一轮后续巡查。
"""
import asyncio
import random
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .config import settings
from .database import async_session, Model, RuntimeSetting, Video
from .cloud115 import Cloud115Error, cloud
from .model_sync_coordinator import model_sync_coordinator
from . import scraper
from .task_manager import manager
from .notifier import notifier

# 抓取器内部已经执行有界网络重试；巡查层只额外补一次完整同步，避免形成
# “页面重试 × 完整同步重试”的乘法等待。
_SYNC_MAX_RETRIES = 1
_SYNC_RETRY_DELAY = 4.0
_SYNC_COOLDOWN_FAILURES = 3
_SYNC_COOLDOWN_SECONDS = 30.0
_HEARTBEAT_SECONDS = 30.0
_RUN_NOW = "run_now"
_RESCHEDULE = "reschedule"

_SETTING_ENABLED = "patrol.enabled"
_SETTING_INTERVAL = "patrol.interval_hours"
_SETTING_AUTO_DOWNLOAD = "patrol.auto_download"


async def sync_model_core(model_id: int):
    """保留可测试的模块边界，实际走全局同步协调器。"""
    return await model_sync_coordinator.sync_model(model_id)


class Patrol:
    def __init__(self, *, session_factory=None, cloud_client=None, task_manager=None,
                 notifier_client=None, sync_func=None):
        self._session_factory = session_factory or (lambda: async_session())
        self._cloud_client = cloud_client
        self._task_manager = task_manager
        self._notifier_client = notifier_client
        self._sync_func = sync_func
        self.enabled: bool = settings.patrol_enabled
        self.interval_hours: float = settings.patrol_interval_hours
        self.auto_download: bool = settings.patrol_auto_download
        self.running: bool = False
        self.last_started_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_result: dict | None = None
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._commands: asyncio.Queue[str] = asyncio.Queue()
        self._run_requested = False
        self._reschedule_requested = False
        self._manual_run_requested = False
        self._run_lock = asyncio.Lock()
        self._sync_cooldown_lock = asyncio.Lock()
        self._sync_failure_streak = 0
        self._sync_cooldown_until = 0.0
        self._broadcast = None
        # fire-and-forget 推送任务的强引用集合，防止事件循环仅持弱引用时被 GC。
        self._push_tasks: set[asyncio.Task] = set()

    @property
    def _cloud(self):
        return self._cloud_client if self._cloud_client is not None else cloud

    @property
    def _manager(self):
        return self._task_manager if self._task_manager is not None else manager

    @property
    def _notifier(self):
        return self._notifier_client if self._notifier_client is not None else notifier

    @property
    def _sync(self):
        return self._sync_func if self._sync_func is not None else sync_model_core

    def set_broadcast(self, fn):
        self._broadcast = fn

    async def _notify(self):
        if self._broadcast:
            await self._broadcast({
                "type": "patrol_update",
                "patrol": self.status(),
            })

    async def _safe_notify(self):
        try:
            await self._notify()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 状态广播失败不能杀死后台巡查循环。
            pass

    def _spawn_push(self, coro):
        """fire-and-forget 推送；持强引用防 GC，推送的网络超时不拖住巡查循环。"""
        task = asyncio.create_task(coro)
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

    def _push_summary(self, result: dict):
        """一轮只创建一个后台推送任务；新缺失按模特聚合为单条摘要。"""
        self._spawn_push(self._push_summary_notifications(dict(result)))

    async def _push_summary_notifications(self, result: dict):
        await self._notifier.notify_patrol_summary(result)
        await self._notifier.notify_new_missing_summary([
            {
                "model_name": str(detail.get("model", "")),
                "count": int(detail.get("newly_missing", 0) or 0),
                "total_missing": int(detail.get("missing", 0) or 0),
            }
            for detail in result.get("details", [])
            if int(detail.get("newly_missing", 0) or 0) > 0
        ])

    def _push_error(self, scene: str, detail: str):
        self._spawn_push(self._notifier.notify_error(scene, detail))

    def status(self) -> dict:
        scheduler_alive = bool(self._task and not self._task.done())
        return {
            "enabled": self.enabled,
            "running": self.running,
            "scheduler_alive": scheduler_alive,
            "run_now_pending": self._run_requested or self._manual_run_requested,
            "interval_hours": self.interval_hours,
            "auto_download": self.auto_download,
            "last_started_at": (
                self.last_started_at.isoformat() if self.last_started_at else None),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None),
            "last_result": self.last_result,
        }

    # ---------- 运行时配置持久化 ----------
    async def restore_config(self):
        """读取 UI 修改过的巡查设置；没有持久化值时继续使用 .env。"""
        # lifespan 可能在同一解释器内重建；先恢复静态默认，不能沿用上次 stop()
        # 留下的内存态，再用数据库里的用户设置覆盖。
        self.enabled = settings.patrol_enabled
        self.interval_hours = settings.patrol_interval_hours
        self.auto_download = settings.patrol_auto_download
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(RuntimeSetting).where(RuntimeSetting.key.in_((
                    _SETTING_ENABLED,
                    _SETTING_INTERVAL,
                    _SETTING_AUTO_DOWNLOAD,
                )))
            )).scalars().all()
        values = {row.key: row.value for row in rows}
        if _SETTING_ENABLED in values:
            self.enabled = values[_SETTING_ENABLED].strip().lower() in {
                "1", "true", "yes", "on",
            }
        if _SETTING_INTERVAL in values:
            try:
                self.interval_hours = max(0.1, float(values[_SETTING_INTERVAL]))
            except (TypeError, ValueError):
                pass
        if _SETTING_AUTO_DOWNLOAD in values:
            self.auto_download = values[_SETTING_AUTO_DOWNLOAD].strip().lower() in {
                "1", "true", "yes", "on",
            }

    async def _persist_setting(self, key: str, value: str):
        async with self._session_factory() as session:
            row = await session.get(RuntimeSetting, key)
            if row is None:
                session.add(RuntimeSetting(key=key, value=value))
            else:
                row.value = value
            await session.commit()

    async def set_enabled(self, enabled: bool):
        await self._persist_setting(_SETTING_ENABLED, "true" if enabled else "false")
        if enabled:
            self.start()
            await self._safe_notify()
        else:
            await self.stop()

    async def set_interval(self, hours: float, *, persist: bool = True):
        """修改间隔并重算截止时间，不触发额外巡查。"""
        self.interval_hours = max(0.1, float(hours))
        if persist:
            await self._persist_setting(_SETTING_INTERVAL, str(self.interval_hours))
        if self._task and not self._task.done():
            if not self._reschedule_requested:
                self._reschedule_requested = True
                self._commands.put_nowait(_RESCHEDULE)
        else:
            self.next_run_at = None
        await self._safe_notify()

    # ---------- 启停与命令 ----------
    def _drain_commands(self):
        while True:
            try:
                self._commands.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._run_requested = False
        self._reschedule_requested = False

    def start(self):
        if self._task and not self._task.done():
            return
        self._drain_commands()
        self.enabled = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        """停止当前进程内的巡查；是否持久化由 set_enabled 决定。"""
        self.enabled = False
        self.next_run_at = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._manual_task and not self._manual_task.done():
            self._manual_task.cancel()
            try:
                await self._manual_task
            except asyncio.CancelledError:
                pass
        self._manual_task = None
        self._manual_run_requested = False
        self._drain_commands()
        push_tasks = list(self._push_tasks)
        for task in push_tasks:
            task.cancel()
        if push_tasks:
            await asyncio.gather(*push_tasks, return_exceptions=True)
        self._push_tasks.clear()
        await self._safe_notify()

    async def trigger_now(self):
        """立即执行一轮；运行中重复请求合并为最多一轮后续执行。"""
        if self._task and not self._task.done():
            if not self._run_requested:
                self._run_requested = True
                self._commands.put_nowait(_RUN_NOW)
            await asyncio.sleep(0)
            return

        # 定时开关关闭时，使用独立的一次性任务。请求在运行期间到达时只追加一轮。
        self._manual_run_requested = True
        if not self._manual_task or self._manual_task.done():
            self._manual_task = asyncio.create_task(self._run_manual_sequence())
        await asyncio.sleep(0)

    async def _run_manual_sequence(self):
        try:
            while self._manual_run_requested:
                self._manual_run_requested = False
                self.last_heartbeat_at = datetime.now()
                await self._run_once_guarded(manual=True)
        finally:
            if self._manual_task is asyncio.current_task():
                self._manual_task = None

    # ---------- 后台循环 ----------
    def _failure_result(self, reason: str) -> dict:
        previous = self.last_result or {}
        return {
            "scanned": 0,
            "bound_models": previous.get("bound_models", 0),
            "total_models": previous.get("total_models", 0),
            "missing": 0,
            "newly_missing": 0,
            "queued_downloads": 0,
            "pipeline": 0,
            "repaired": 0,
            "confirmation_pending": 0,
            "attention": 0,
            "unmanaged": 0,
            "source_unavailable": 0,
            "added": 0,
            "errors": 1,
            "skipped_reason": None,
            "failure_reason": reason,
            "details": [{"error": reason}],
        }

    async def _run_once_guarded(self, *, manual: bool = False):
        try:
            return await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            prefix = "手动巡查异常" if manual else "巡查轮异常"
            self.last_result = self._failure_result(f"{prefix}: {message[:120]}")
            self.last_run_at = datetime.now()
            self._push_error(prefix, message[:200])
            await self._safe_notify()
            return self.last_result

    async def _loop(self):
        """启动时立即跑一轮，之后等待截止时间或显式命令。"""
        should_run = True
        try:
            while True:
                self.last_heartbeat_at = datetime.now()
                if should_run:
                    await self._run_once_guarded()
                    should_run = False

                self.next_run_at = datetime.now() + timedelta(hours=self.interval_hours)
                await self._safe_notify()

                while True:
                    remaining = (self.next_run_at - datetime.now()).total_seconds()
                    if remaining <= 0:
                        should_run = True
                        break
                    try:
                        command = await asyncio.wait_for(
                            self._commands.get(),
                            timeout=min(remaining, _HEARTBEAT_SECONDS),
                        )
                    except asyncio.TimeoutError:
                        self.last_heartbeat_at = datetime.now()
                        continue

                    self.last_heartbeat_at = datetime.now()
                    if command == _RUN_NOW:
                        self._run_requested = False
                        should_run = True
                        break
                    if command == _RESCHEDULE:
                        self._reschedule_requested = False
                        self.next_run_at = (
                            datetime.now() + timedelta(hours=self.interval_hours))
                        await self._safe_notify()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            self.last_result = self._failure_result(
                f"巡查调度器异常: {message[:120]}")
            self.last_run_at = datetime.now()
            self.next_run_at = None
            self._push_error(
                "巡查调度器异常", f"{message[:200]}；定时巡查已停止，请重启后端。")
            await self._safe_notify()

    # ---------- 巡查一轮 ----------
    async def run_once(self) -> dict:
        """巡查全部已绑定 115 的模特，并对每个 missing 流水线做对账。"""
        if self._run_lock.locked():
            return self.last_result or {}
        async with self._run_lock:
            self.running = True
            self.last_started_at = datetime.now()
            self.last_heartbeat_at = self.last_started_at
            await self._safe_notify()
            result = {
                "scanned": 0,
                "bound_models": 0,
                "total_models": 0,
                "missing": 0,
                "newly_missing": 0,
                "queued_downloads": 0,
                "pipeline": 0,
                "repaired": 0,
                "confirmation_pending": 0,
                "attention": 0,
                "unmanaged": 0,
                "source_unavailable": 0,
                # 旧前端兼容字段；含义与 queued_downloads 一致。
                "added": 0,
                "errors": 0,
                "skipped_reason": None,
                "failure_reason": None,
                "details": [],
            }
            try:
                async with self._session_factory() as session:
                    result["total_models"] = int((await session.scalar(
                        select(func.count(Model.id)))) or 0)
                    model_ids = (await session.execute(
                        select(Model.id, Model.name).where(Model.cloud_cid != "")
                    )).all()
                result["bound_models"] = len(model_ids)

                if not await self._cloud.check_login():
                    result["skipped_reason"] = "115未登录"
                    self.last_result = result
                    self.last_run_at = datetime.now()
                    self._push_error("115 未登录", "巡查已跳过：115 网盘登录已失效，请重新登录。")
                    return result

                async def scan_model(index: int, model_id: int, model_name: str) -> dict:
                    try:
                        previous_missing = await self._missing_viewkeys(model_id)
                        sync_result = await self._sync_with_retry(model_id)
                        if (sync_result or {}).get("status") == "source_unavailable":
                            return {
                                "index": index,
                                "kind": "source_unavailable",
                                "model": model_name,
                                "detail": sync_result.get("error")
                                or "主页不可用或已删除",
                            }
                        current_missing = await self._missing_viewkeys(model_id)
                        reconciliation = await self._manager.reconcile_missing_pipeline(
                            model_id, auto_repair=self.auto_download)
                        return {
                            "index": index,
                            "kind": "scanned",
                            "model": model_name,
                            "newly_missing": len(current_missing - previous_missing),
                            "reconciliation": reconciliation,
                        }
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        message = str(error).strip() or type(error).__name__
                        return {
                            "index": index,
                            "kind": "error",
                            "model": model_name,
                            "detail": message[:120],
                        }
                    finally:
                        self.last_heartbeat_at = datetime.now()

                queue: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()
                for index, (model_id, model_name) in enumerate(model_ids):
                    queue.put_nowait((index, model_id, model_name))
                outcomes: list[dict] = []

                async def worker():
                    while True:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        outcomes.append(await scan_model(*item))

                worker_count = min(
                    max(1, settings.patrol_sync_concurrency),
                    max(1, len(model_ids)),
                )
                await asyncio.gather(*(worker() for _ in range(worker_count)))

                # 并发完成顺序不稳定；按原 Model 顺序汇总，保持通知和详情可读。
                for outcome in sorted(outcomes, key=lambda item: item["index"]):
                    if outcome["kind"] == "source_unavailable":
                        result["source_unavailable"] += 1
                        result["details"].append({
                            "model": outcome["model"],
                            "source_unavailable": outcome["detail"],
                        })
                        continue
                    if outcome["kind"] == "error":
                        result["errors"] += 1
                        result["details"].append({
                            "model": outcome["model"],
                            "error": outcome["detail"],
                        })
                        continue

                    reconciliation = outcome["reconciliation"]
                    newly_missing = outcome["newly_missing"]
                    result["scanned"] += 1
                    result["missing"] += reconciliation["missing"]
                    result["newly_missing"] += newly_missing
                    result["queued_downloads"] += reconciliation["queued_downloads"]
                    result["pipeline"] += reconciliation["pipeline"]
                    repaired = (
                        reconciliation["repaired_uploads"]
                        + reconciliation["repaired_downloads"])
                    result["repaired"] += repaired
                    result["confirmation_pending"] += reconciliation[
                        "confirmation_pending"]
                    result["attention"] += reconciliation["blocked"]
                    result["unmanaged"] += reconciliation["unmanaged"]
                    if (newly_missing or reconciliation["queued_downloads"]
                            or repaired or reconciliation["blocked"]
                            or reconciliation["confirmation_pending"]):
                        result["details"].append({
                            "model": outcome["model"],
                            "newly_missing": newly_missing,
                            **reconciliation,
                        })
                result["added"] = result["queued_downloads"]

                self.last_result = result
                self.last_run_at = datetime.now()
                self._push_summary(result)
                return result
            finally:
                self.running = False
                self.last_heartbeat_at = datetime.now()
                await self._safe_notify()

    async def _missing_viewkeys(self, model_id: int) -> set[str]:
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(Video.viewkey).where(
                    Video.model_id == model_id,
                    Video.dedup_status == "missing",
                )
            )).scalars().all()
        return set(rows)

    async def _sync_with_retry(self, model_id: int):
        """爬取单模特，失败后指数退避；连续上游失败触发共享冷却。"""
        deadline = asyncio.timeout(settings.model_sync_timeout_seconds)
        try:
            async with deadline:
                return await self._sync_with_retry_budget(model_id)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # ``self._sync`` 自身也可能把上游超时作为 TimeoutError 抛出；
            # 只有本层 timeout 确实到期时才改写错误并累计一次失败。否则保留
            # 内层错误及其已经完成的失败计数，避免错误分类和双重冷却计数。
            if not deadline.expired():
                raise
            await self._record_sync_failure()
            raise TimeoutError(
                f"单模特同步超过 {settings.model_sync_timeout_seconds:g} 秒总预算"
            ) from None

    async def _sync_with_retry_budget(self, model_id: int):
        """在调用方总体 deadline 内执行有限的完整同步重试。"""
        last_exc: Exception | None = None
        for attempt in range(_SYNC_MAX_RETRIES + 1):
            await self._wait_sync_cooldown()
            try:
                result = await self._sync(model_id)
                await self._record_sync_success()
                return result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_exc = error
                if attempt < _SYNC_MAX_RETRIES:
                    await asyncio.sleep(
                        _SYNC_RETRY_DELAY * (2 ** attempt)
                        + random.uniform(0.0, 0.5))
        if isinstance(last_exc, (scraper.ScrapeError, Cloud115Error, TimeoutError)):
            await self._record_sync_failure()
        raise last_exc

    async def _wait_sync_cooldown(self):
        while True:
            async with self._sync_cooldown_lock:
                remaining = self._sync_cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _record_sync_success(self):
        async with self._sync_cooldown_lock:
            self._sync_failure_streak = 0
            # 已在冷却开始前启动的另一个 worker 即使随后成功，也不能提前解除冷却；
            # 冷却结束后的成功会自然清零时间戳。
            if self._sync_cooldown_until <= time.monotonic():
                self._sync_cooldown_until = 0.0

    async def _record_sync_failure(self):
        async with self._sync_cooldown_lock:
            self._sync_failure_streak += 1
            if self._sync_failure_streak >= _SYNC_COOLDOWN_FAILURES:
                self._sync_cooldown_until = max(
                    self._sync_cooldown_until,
                    time.monotonic() + _SYNC_COOLDOWN_SECONDS,
                )
                self._sync_failure_streak = 0

    async def _queue_missing(self, model_id: int) -> int:
        """旧调用兼容：由新对账器创建缺失任务并返回新下载数。"""
        result = await self._manager.reconcile_missing_pipeline(
            model_id, auto_repair=self.auto_download)
        return result["queued_downloads"]


patrol = Patrol()
