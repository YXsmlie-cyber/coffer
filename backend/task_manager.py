"""任务队列管理器。

统一调度下载/上传任务：
  - 下载并发上限 settings.max_concurrent_downloads (5)
  - 上传并发上限 settings.max_concurrent_uploads (2)
  - 支持 全部开始/全部暂停/全部继续/删除单个任务
  - 下载完成 -> 自动二次去重 -> 生成上传任务
  - 通过 WebSocket 广播任务状态变化

任务持久化在 DB(Task表)，重启后恢复未完成任务。
"""
import asyncio
from contextlib import AsyncExitStack
import logging
import math
import os
import shutil
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from weakref import WeakValueDictionary
from sqlalchemy import case, delete, select, update, func, and_, or_, tuple_

from .config import settings
from .database import async_session, RuntimeSetting, Task, Model, Video
from .downloader import (
    downloader,
    build_output_path,
    build_remote_filename,
    download_concurrency_limit,
    cleanup_stale_output_lock,
    output_lock_allows_recovery,
    has_partial_artifacts,
    validate_completed_artifact,
    check_media_tools,
    DownloadProcessingError,
    NetworkDownloadError,
)
from .download_errors import (
    DownloadArtifactError,
    DownloadEnvironmentError,
    RateLimitedDownloadError,
    SourceUnavailableDownloadError,
    classify_stored_download_failure,
)
from .scraper import (
    HLSRateLimitedError, HLSSourceUnavailableError, ScrapeFetchError, VideoUnavailableError,
    get_best_m3u8,
)
from .network_health import LinkHealth, ProbeTarget
from .download_throttle import DownloadThrottle, WAITING_TEXT as DOWNLOAD_THROTTLE_WAITING_TEXT
from .upload_gate import UploadGate, WAITING_TEXT as UPLOAD_GATE_WAITING_TEXT
from .power import KeepAwake
from .download_settings import STORAGE_KEYS as DOWNLOAD_SETTING_KEYS, DownloadSettings
from .resizable_limiter import ResizableLimiter
from .task_store import TaskStore, is_sqlite_locked
from .task_queries import TaskQueries
from .inventory_writer import InventoryWriter
from .task_contracts import TaskPatch, wire_value as _wire_value
from .cloud115 import cloud, Cloud115Error, UploadTransferError, redact_sensitive_error_text
from .notifier import notifier
from . import dedup


logger = logging.getLogger(__name__)
PROGRESS_PERSIST_INTERVAL = 5.0
SOURCE_RECHECK_DELAYS = (15 * 60, 60 * 60, 6 * 60 * 60)
# 网络类失败的整次重试退避。同一时刻多个任务一起断流多半是共享链路（代理/隧道/
# 运营商）短暂中断；1s/2s 的即时重试会撞上同一次中断，把三次机会在几秒内用完。
NETWORK_RETRY_DELAYS = (15, 60)
# ffmpeg/ffprobe 缺失时新下载暂缓；按该间隔自动复查，补上工具后无需手动恢复。
MEDIA_TOOLS_RECHECK_SECONDS = 60.0
MEDIA_TOOLS_WAITING_PREFIX = "等待本机 ffmpeg/ffprobe"
# 共享链路断流时任务不计次等待恢复；超过该时长仍未恢复，回到原有计次策略收口。
NETWORK_OUTAGE_MAX_WAIT_SECONDS = 6 * 60 * 60
# 断流持续超过该时长才发 Bark，避免每分钟一次的短暂断流刷屏。
NETWORK_OUTAGE_ALERT_SECONDS = 5 * 60
NETWORK_WAIT_PHASE = "network_wait"


class UploadConfirmationError(Cloud115Error):
    """115 已接受上传但权威文件记录未通过校验；禁止自动重复上传。"""


def _error_text(error: BaseException) -> str:
    """把异常转换为可诊断的任务错误文本，避免空字符串掩盖失败原因。"""
    message = redact_sensitive_error_text(str(error)).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _format_speed(speed_bps: float | int | None) -> str:
    """兼容旧前端的派生速度文本；权威值始终是 speed_bps。"""
    value = float(speed_bps or 0)
    if value <= 0:
        return ""
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB/s"
    if value >= 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value:.0f} B/s"


def _quality_retry_cap(phase: str | None) -> int | None:
    """从持久化 phase 读取降质重试上限；非法值按普通恢复处理。"""
    value = str(phase or "")
    for prefix in (
        "queued_quality_retry:",
        "paused_quality_retry:",
        "queued_quality_resume:",
        "paused_quality_resume:",
    ):
        if value.startswith(prefix):
            try:
                cap = int(value[len(prefix):])
            except (TypeError, ValueError):
                return None
            return cap if cap > 0 else None
    return None


def _download_dir_available(
    path: str,
    cache: dict[str, bool] | None = None,
) -> bool:
    """检查一个下载目录是否达到保留空间；同一轮调度可复用结果。"""
    reserve_gb = max(
        0.0, float(getattr(settings, "download_min_free_space_gb", 20.0)))
    path = os.path.abspath(path)
    cache_key = os.path.normcase(path)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    try:
        os.makedirs(path, exist_ok=True)
        free_bytes = shutil.disk_usage(path).free
    except OSError:
        # 无法确认空间时宁可保持排队，避免把数据库所在磁盘写满。
        available = False
    else:
        available = reserve_gb <= 0 or free_bytes >= reserve_gb * 1024 ** 3
    if cache is not None:
        cache[cache_key] = available
    return available


def select_download_dir(cache: dict[str, bool] | None = None) -> str | None:
    """按配置顺序返回第一个空间充足的下载目录。"""
    for path in settings.download_paths():
        absolute = os.path.abspath(path)
        if _download_dir_available(absolute, cache):
            return absolute
    return None


def download_space_available(cache: dict[str, bool] | None = None) -> bool:
    """任一下载目录达到保留空间即可启动没有既有断点的新任务。"""
    return select_download_dir(cache) is not None




def _task_identity(model_id: int | None, model_name: str):
    """返回同一模特任务的查询条件；NULL 仅用于无法回填的旧任务。"""
    if model_id is not None:
        return Task.model_id == model_id
    return and_(Task.model_id.is_(None), Task.model_name == model_name)


def _is_rate_limited(error: BaseException | None) -> bool:
    """CDN 返回 429：本机出口被限流，不是视频或链路本身的问题。"""
    return isinstance(error, (RateLimitedDownloadError, HLSRateLimitedError))


class _DownloadThrottled(Exception):
    """内部控制流：下载因 CDN 限流退回队列，不落库为失败。cause 为 None 表示冷却中未发起请求。"""

    def __init__(self, cause: BaseException | None = None):
        super().__init__(str(cause or ""))
        self.cause = cause


def _is_link_failure(error: BaseException) -> bool:
    """可能由共享链路断流引起的失败；来源 410 与限流 429 各有单独处理。"""
    if isinstance(error, (HLSSourceUnavailableError, SourceUnavailableDownloadError)):
        return False
    if _is_rate_limited(error):
        return False
    return isinstance(error, (NetworkDownloadError, ScrapeFetchError))


def _output_path_for_task(task: Task) -> str:
    assigned = str(getattr(task, "local_path", "") or "").strip()
    if assigned:
        return os.path.abspath(assigned)
    return build_output_path(
        task.model_name,
        task.title,
        task.viewkey,
        getattr(task, "artifact_scope", "") or "",
    )


def _failure_metadata(task: Task) -> dict[str, object]:
    """返回前端与批量恢复共用的失败语义。"""
    if task.task_type != "download":
        return {
            "failure_kind": "upload_failure",
            "recovery_action": "retry_upload",
            "bulk_recoverable": True,
        }
    disposition = classify_stored_download_failure(task.error, task.phase)
    action = disposition.recovery_action
    kind = disposition.kind
    recoverable = disposition.bulk_recoverable
    if action == "reconcile_final":
        output_path = _output_path_for_task(task)
        if not output_lock_allows_recovery(output_path, str(task.id)):
            action = "manual"
            kind = "output_lock_unavailable"
            recoverable = False
        elif not os.path.isfile(output_path):
            # 无成品时，已释放或确认属于本任务的失效锁可进入正常下载。
            # 查询不删锁/断点，worker 会再次原子取锁并验证断点，不能直接记完成。
            action = "retry_network"
            kind = "stale_output_lock"
    return {
        "failure_kind": kind,
        "recovery_action": action,
        "bulk_recoverable": recoverable,
    }


def _build_output_path_in(task: Task, download_dir: str) -> str:
    return os.path.abspath(build_output_path(
        task.model_name,
        task.title,
        task.viewkey,
        getattr(task, "artifact_scope", "") or "",
        download_dir=download_dir,
    ))


def _select_output_path_for_task(
    task: Task,
    space_cache: dict[str, bool] | None = None,
) -> str | None:
    """选择并固定单个任务的目标路径，不跨盘拆分已有断点。"""
    assigned = str(getattr(task, "local_path", "") or "").strip()
    if assigned:
        assigned = os.path.abspath(assigned)
        # 成品已经存在时，下载器只做验证并直接流转上传，不受保留空间门禁影响。
        if os.path.isfile(assigned):
            return assigned
        assigned_dir = os.path.dirname(assigned) or os.curdir
        if _download_dir_available(assigned_dir, space_cache):
            return assigned
        # 有媒体断点时禁止静默改盘，否则旧盘断点会被遗弃且无法续传。
        if (has_partial_artifacts(assigned)
                or int(getattr(task, "downloaded_bytes", 0) or 0) > 0):
            return None

    paths = settings.download_paths()
    if paths and not assigned:
        # 功能升级前的任务没有记录目标路径，先探测旧主目录中的成品/断点。
        legacy = _build_output_path_in(task, os.path.abspath(paths[0]))
        if os.path.isfile(legacy):
            return legacy
        if has_partial_artifacts(legacy):
            legacy_dir = os.path.dirname(legacy) or os.curdir
            return legacy if _download_dir_available(
                legacy_dir, space_cache) else None

    selected = select_download_dir(space_cache)
    if selected is None:
        return None
    return _build_output_path_in(task, selected)


class TaskManager:
    def __init__(self, *, session_factory=None, cloud_client=None, download_client=None,
                 notifier_client=None, resolve_media=None, inventory_writer=None,
                 link_health=None, keep_awake=None, upload_gate=None,
                 download_throttle=None):
        # 默认仍使用同一应用实例；显式注入使测试和维护不依赖模块内部对象。
        self._session_factory = session_factory or (lambda: async_session())
        self._cloud_client = cloud_client
        self._download_client = download_client
        self._notifier_client = notifier_client
        self._media_resolver = resolve_media
        self._download_limit = download_concurrency_limit()
        # 并发上限可在页面调整：已在跑的任务不受影响，新的尝试按新上限排队。
        self._download_sem = ResizableLimiter(self._download_limit)
        self._download_settings = DownloadSettings.from_config(settings)
        self._window_wakeup: asyncio.TimerHandle | None = None
        self._now = datetime.now
        self._upload_sem = asyncio.Semaphore(settings.max_concurrent_uploads)
        self._running: dict[int, asyncio.Task] = {}
        # 完整尝试之间的退避协程不占下载并发槽，但仍需可被暂停/删除/关闭取消。
        self._download_retry_waiters: dict[int, asyncio.Task] = {}
        # 全部来源冷却共用一个到期唤醒，不为每个任务建立等待协程或周期轮询。
        self._source_wakeup: asyncio.TimerHandle | None = None
        self._paused = False
        self._broadcast = None  # 由 main 注入 websocket 广播函数
        self._pump_lock = asyncio.Lock()       # 串行化调度，避免并发 _pump 超开任务
        self._pump_wakeup = asyncio.Event()
        self._pump_runner: asyncio.Task | None = None
        self._task_create_lock = asyncio.Lock()  # 原子化查重+建任务，防止双击竞态
        # 二次去重的 115 文件夹索引短期缓存： cid -> (建立时间, index)
        self._dedup_cache: dict[str, tuple[float, dict]] = {}
        # 持有者/等待者的局部变量保持锁存活；无使用者后自动释放注册项，
        # 不在解锁时 pop，避免等待旧锁的请求与新建锁请求同时入场。
        self._dedup_cache_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._dedup_cache_ttl = 60.0           # 秒；同模特连续上传时复用，避免重复全量扫描
        self._dedup_cache_next_prune_at = float("-inf")
        self._model_cid_locks: WeakValueDictionary[
            tuple[int | None, str], asyncio.Lock
        ] = WeakValueDictionary()
        self._upload_verify_delays = (0.0, 0.5, 2.0, 5.0, 15.0)
        self._max_download_retries = 2         # 仅网络失败自动重试（指数退避）
        self._max_processing_retries = 1       # 本地封装失败只重做本地处理
        # 上传彻底失败后的延迟自动重新入队。下载固定三次同引擎尝试，
        # 最终失败后保持 failed，避免后台静默重排掩盖真实结果。
        self._auto_requeue_max = 2
        self._auto_requeue_delay = 20.0        # 秒
        self._upload_retry_waiters: dict[int, asyncio.Task] = {}
        # pause_all 会先登记意图再终止 worker；即使 Windows 进程退出事件抢在
        # CancelledError 前返回，也必须按 paused 收口，不能污染失败统计。
        self._download_pause_requests: set[int] = set()
        self._max_patrol_recoveries = 2
        # fire-and-forget 调度任务需要强引用；事件循环只持弱引用，未保存的 task 在 await
        # 挂起期间可能被 GC 回收，导致 _pump/自动重排静默丢失、任务卡 queued。
        self._bg_tasks: set[asyncio.Task] = set()
        # 实时进度走 WebSocket；SQLite 只保留恢复所需的周期快照。
        self._task_store = TaskStore(lambda: self._session_factory())
        self._latest_progress: dict[int, dict] = {}
        self._progress_persisted_at: dict[int, float] = {}
        self._progress_persisted_phase: dict[int, str] = {}
        # 批次完成通知：记录各类型上一次 _pump 是否有活跃任务，做"有→无"边沿检测。
        # 队列排空后延迟去抖确认仍为空才推送，避免"下载完成瞬间建上传任务"的连续批次误报。
        self._batch_active: dict[str, bool] = {"download": False, "upload": False}
        self._batch_debounce: dict[str, asyncio.Task] = {}
        self._batch_baseline: dict[str, dict[str, int]] = {}
        self._batch_debounce_seconds = 4.0
        # 磁盘空间不足通知的节流时间戳（单调时钟）。
        self._disk_alert_at: float | None = None
        self._disk_alert_throttle = 60.0
        # 后处理工具门禁：缺工具是运行环境问题，不能逐个任务下载完再记失败。
        self._media_tools_check = check_media_tools
        self._media_tools_block = None
        self._media_tools_alert_at: float | None = None
        self._media_tools_alert_throttle = 30 * 60.0
        self._media_tools_wakeup: asyncio.TimerHandle | None = None
        # 共享网络链路：断流期间任务不计次、调度暂缓新下载，恢复后自动继续。
        self._link_health = link_health if link_health is not None else LinkHealth()
        self._link_health.add_listener(self._on_link_event)
        self._link_alert_task: asyncio.Task | None = None
        self._link_alerted = False
        # 115 整体拒绝本机上传时：任务退回队列不计次、暂停派发上传，恢复后自动继续。
        self._upload_gate = upload_gate if upload_gate is not None else UploadGate(
            probe=self._probe_upload_access)
        self._upload_gate.add_listener(self._on_upload_gate_event)
        # PH 视频 CDN 限流（429）：整体冷却、任务退回队列不计次，恢复后降并发逐步爬升。
        self._download_throttle = (
            download_throttle if download_throttle is not None else DownloadThrottle())
        self._download_throttle.add_listener(self._on_download_throttle_event)
        # 有任务在跑（含等待重试/等待链路恢复）时阻止系统自动睡眠。
        self._keep_awake = keep_awake if keep_awake is not None else KeepAwake(
            enabled=bool(getattr(settings, "keep_awake_while_busy", True)))
        self._queries = TaskQueries(lambda: self._session_factory(), lambda task: _failure_metadata(task))
        self._inventory = inventory_writer if inventory_writer is not None else InventoryWriter(
            lambda: self._session_factory(), lambda: self._broadcast, logger=logger)

    @property
    def _cloud(self):
        return self._cloud_client if self._cloud_client is not None else cloud

    @property
    def _downloader(self):
        return self._download_client if self._download_client is not None else downloader

    @property
    def _notifier(self):
        return self._notifier_client if self._notifier_client is not None else notifier

    @property
    def _resolve_media(self):
        return self._media_resolver if self._media_resolver is not None else get_best_m3u8

    def _spawn_bg(self, coro):
        """启动后台协程并持有强引用，结束后自动移除。"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    def set_broadcast(self, fn):
        self._broadcast = fn

    def runtime_statistics(self):
        """只暴露有界资源计数，不读取数据库或输出任务身份。"""
        return {
            "active": len(self._running),
            "download_retry_waiters": len(self._download_retry_waiters),
            "upload_retry_waiters": len(self._upload_retry_waiters),
            "background_tasks": len(self._bg_tasks),
            "dedup_cache_entries": len(self._dedup_cache),
            "download_limit": self._download_limit,
            "download_blocked": self.download_blocked_reason(),
            "link_health": self._link_health.public(),
            "upload_gate": self._upload_gate.public(),
            "download_throttle": self._download_throttle.public(),
            "keep_awake": self._keep_awake.public(),
            "download_window_open": self._download_settings.window_open(self._now()),
        }

    def link_outage_totals(self) -> tuple[int, float]:
        return self._link_health.totals()

    def link_health(self) -> dict:
        """近一小时断流次数/时长与当前是否断流；不含主机名。"""
        return self._link_health.public()

    def upload_blocked_reason(self) -> dict | None:
        """上传被 115 整体拒绝时的暂停原因；不含主机名或任务身份。"""
        if not self._upload_gate.blocked:
            return None
        seconds = int(self._upload_gate.blocked_seconds())
        return {
            "code": "upload_denied",
            "message": f"115 暂时拒绝本机上传（已持续 {max(1, round(seconds / 60))} 分钟）",
            "blocked_seconds": seconds,
        }

    async def _probe_upload_access(self) -> str:
        probe = getattr(self._cloud, "probe_upload_access", None)
        if probe is None:
            return "unknown"
        return await probe()

    def _on_upload_gate_event(self, event: str, seconds: float) -> None:
        """闸门关闭/重开：广播给上传页并推送一次；重开后立即恢复上传调度。"""
        if self._broadcast:
            self._spawn_bg(self._broadcast({
                "type": "upload_gate", "upload_blocked": self.upload_blocked_reason(),
            }))
        if event == "blocked":
            self._spawn_bg(self._notifier.notify_error(
                "115 拒绝上传",
                "115 拒绝了本机的上传请求（存储桶策略）。上传已暂停且不消耗重试次数，"
                "程序每 10 分钟自动复查，恢复后自动继续。"))
            return
        self._spawn_bg(self._notifier.notify_upload_restored(seconds))
        self._request_pump()

    def download_blocked_reason(self) -> dict | None:
        """新下载被环境门禁暂缓时的原因；只含工具名，不含本地路径或主机名。"""
        if self._link_health.outage_active:
            seconds = int(self._link_health.outage_seconds())
            return {
                "code": "network_outage",
                "message": f"网络或代理链路中断（已持续 {seconds} 秒）",
                "outage_seconds": seconds,
            }
        if self._download_throttle.blocked:
            seconds = int(math.ceil(self._download_throttle.remaining_seconds()))
            return {
                "code": "rate_limited",
                "message": (
                    f"PH 视频 CDN 限流（HTTP 429），约 {max(1, math.ceil(seconds / 60))} 分钟后自动继续"),
                "seconds_left": seconds,
            }
        block = self._media_tools_block
        if block is not None:
            return {"code": "media_tools_unavailable", "message": block.message, **block.public()}
        values = self._download_settings
        if not values.window_open(self._now()):
            return {
                "code": "download_window",
                "message": f"当前不在下载时段 {values.window_text()}",
                "window": values.window_text(),
            }
        return None

    # ---------- 运行参数 ----------
    def download_settings(self) -> dict:
        return self._download_settings.public()

    async def restore_download_settings(self) -> None:
        """启动时用页面保存过的值覆盖 .env 默认；读取失败保持默认。"""
        keys = list(DOWNLOAD_SETTING_KEYS.values())
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(
                    select(RuntimeSetting).where(RuntimeSetting.key.in_(keys)))).scalars().all()
        except Exception:
            logger.exception("读取下载运行参数失败，沿用 .env 默认值")
            return
        stored = {row.key: row.value for row in rows}
        self._apply_download_settings(
            DownloadSettings.from_storage(DownloadSettings.from_config(settings), stored))

    async def update_download_settings(self, values: DownloadSettings) -> dict:
        """校验、整组持久化后立即生效；不合法时抛 ValueError。"""
        values = values.validated()
        async with self._session_factory() as session:
            for key, value in values.to_storage().items():
                row = await session.get(RuntimeSetting, key)
                if row is None:
                    session.add(RuntimeSetting(key=key, value=value))
                else:
                    row.value = value
            await session.commit()
        self._apply_download_settings(values)
        return self.download_settings()

    def _apply_download_settings(self, values: DownloadSettings) -> None:
        self._download_settings = values
        # downloader 在每次尝试时读取这些全局值（连接预算、分片线程、限速）。
        settings.max_concurrent_downloads = values.max_concurrent_downloads
        settings.download_max_connections = values.max_connections
        settings.download_rate_limit_mbps = values.rate_limit_mbps
        settings.download_window_start = values.window_start
        settings.download_window_end = values.window_end
        settings.keep_awake_while_busy = values.keep_awake
        self._download_limit = download_concurrency_limit()
        self._download_sem.resize(self._download_limit)
        self._keep_awake.enabled = values.keep_awake
        self._update_keep_awake()
        self._schedule_window_wakeup()
        try:
            self._request_pump()
        except RuntimeError:
            pass                                    # 无事件循环（同步测试）时由下次调度生效

    def _schedule_window_wakeup(self) -> None:
        """时段外时在下一次开始时刻唤醒调度；时段内或未设置时取消。"""
        if self._window_wakeup is not None:
            self._window_wakeup.cancel()
            self._window_wakeup = None
        opens_at = self._download_settings.next_window_open(self._now())
        if opens_at is None or self._paused:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(1.0, (opens_at - self._now()).total_seconds() + 1.0)

        def wake():
            self._window_wakeup = None
            self._request_pump()

        self._window_wakeup = loop.call_later(delay, wake)

    def _on_link_event(self, event: str, seconds: float) -> None:
        """断流开始/结束：广播给页面，断流过久才提醒，恢复后立即恢复调度。"""
        if self._broadcast:
            self._spawn_bg(self._broadcast({
                "type": "network_health", "health": self._link_health.public(),
                "download_blocked": self.download_blocked_reason(),
            }))
        if event == "outage":
            self._link_alerted = False
            if self._link_alert_task is None or self._link_alert_task.done():
                self._link_alert_task = self._spawn_bg(self._alert_long_outage())
            return
        if self._link_alert_task is not None and not self._link_alert_task.done():
            self._link_alert_task.cancel()
        self._link_alert_task = None
        if self._link_alerted:
            self._link_alerted = False
            self._spawn_bg(self._notifier.notify_network_restored(seconds))
        self._request_pump()

    def _on_download_throttle_event(self, event: str, seconds: float) -> None:
        """限流冷却开始/结束：沿用 network_health 消息刷新下载页与托盘；结束后恢复调度。"""
        if self._broadcast:
            self._spawn_bg(self._broadcast({
                "type": "network_health", "health": self._link_health.public(),
                "download_blocked": self.download_blocked_reason(),
            }))
        if event == "resumed":
            self._request_pump()

    async def _alert_long_outage(self) -> None:
        await asyncio.sleep(NETWORK_OUTAGE_ALERT_SECONDS)
        if not self._link_health.outage_active:
            return
        self._link_alerted = True
        minutes = max(1, round(self._link_health.outage_seconds() / 60))
        await self._notifier.notify_error(
            "网络中断",
            f"代理或网络已中断约 {minutes} 分钟。下载已暂停计次，网络恢复后自动继续。")

    async def _media_tools_ready(self, *, force: bool = False) -> bool:
        status = await asyncio.to_thread(self._media_tools_check, force=force)
        self._media_tools_block = None if status.ok else status
        if status.ok and self._media_tools_wakeup:
            self._media_tools_wakeup.cancel()
            self._media_tools_wakeup = None
        return status.ok

    def _schedule_media_tools_recheck(self) -> None:
        if self._paused or self._media_tools_wakeup is not None:
            return

        def recheck():
            self._media_tools_wakeup = None
            self._request_pump()

        self._media_tools_wakeup = asyncio.get_running_loop().call_later(
            MEDIA_TOOLS_RECHECK_SECONDS, recheck)

    def _alert_media_tools_unavailable(self) -> None:
        now = time.monotonic()
        if (self._media_tools_alert_at is not None
                and now - self._media_tools_alert_at < self._media_tools_alert_throttle):
            return
        self._media_tools_alert_at = now
        block = self._media_tools_block
        message = block.message if block else "ffmpeg/ffprobe 不可用"
        logger.warning("新下载已暂缓：%s", message)
        self._spawn_bg(self._notifier.notify_error(
            "下载暂缓：缺少 ffmpeg/ffprobe",
            f"{message}。排队任务保持原状，工具可用后会自动继续。"))

    def _request_pump(self) -> None:
        """合并高频调度唤醒；同一时刻最多保留一个 pump runner。"""
        self._pump_wakeup.set()
        if self._pump_runner is None or self._pump_runner.done():
            self._pump_runner = self._spawn_bg(self._pump_loop())

    async def _pump_loop(self) -> None:
        current = asyncio.current_task()
        consecutive_errors = 0
        try:
            while True:
                await self._pump_wakeup.wait()
                self._pump_wakeup.clear()
                try:
                    await self._pump()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    consecutive_errors += 1
                    logger.exception(
                        "任务调度 pump 失败（%s/3），将有限重试",
                        consecutive_errors,
                    )
                    if self._paused or consecutive_errors >= 3:
                        return
                    await asyncio.sleep(0.25 * consecutive_errors)
                    self._pump_wakeup.set()
                else:
                    consecutive_errors = 0
                self._update_keep_awake()
                # 让本轮 task 完成/finally 中产生的其它唤醒先合并进 Event。
                await asyncio.sleep(0)
                if not self._pump_wakeup.is_set():
                    return
        finally:
            if self._pump_runner is current:
                self._pump_runner = None

    def _update_keep_awake(self) -> None:
        busy = not self._paused and bool(
            self._running or self._download_retry_waiters or self._upload_retry_waiters)
        self._keep_awake.update(busy)

    async def _notify(self, task: Task):
        if self._broadcast:
            await self._broadcast({
                "type": "task_update",
                "task": {
                    "id": task.id, "viewkey": task.viewkey, "title": task.title,
                    "model_id": task.model_id, "model_name": task.model_name,
                    "task_type": task.task_type,
                    "status": task.status, "progress": task.progress,
                    "speed": task.speed, "speed_bps": task.speed_bps,
                    "downloaded_bytes": task.downloaded_bytes,
                    "total_bytes": task.total_bytes, "phase": task.phase,
                    "attempt": task.attempt,
                    "last_progress_at": _wire_value(task.last_progress_at),
                    "auto_retry_count": task.auto_retry_count,
                    "next_retry_at": _wire_value(task.next_retry_at),
                    "remote_missing_count": task.remote_missing_count,
                    "patrol_recovery_count": task.patrol_recovery_count,
                    "origin": task.origin,
                    "origin_candidate_id": task.origin_candidate_id,
                    "error": task.error,
                },
            })

    # ---------- 创建任务 ----------
    async def add_download_tasks(self, videos: list[dict]) -> list[int]:
        """批量加入下载队列。videos 含 model_id/viewkey/title/url 等字段。

        去重策略按 ``model_id + viewkey + task_type`` 隔离；合作视频在不同模特
        目录可各自同步。failed/skipped 旧任务复用原行重新入队，保留稳定任务 ID。
        """
        ids: list[int] = []
        if not videos:
            return ids

        # 先把输入按任务身份去重。一次同步可能因上游分页漂移重复返回同一视频；
        # 这里保留首次出现的稳定顺序，避免同一事务制造两个候选行。
        unique_videos: list[dict] = []
        seen: set[tuple[int | None, str, str]] = set()
        for video in videos:
            model_id = video.get("model_id")
            model_name = str(video.get("model_name") or "")
            identity = (model_id, model_name if model_id is None else "",
                        str(video["viewkey"]))
            if identity in seen:
                continue
            seen.add(identity)
            unique_videos.append(video)

        async with self._task_create_lock:
            async with self._session_factory() as s:
                # 原实现为每个视频各做一次 SELECT 和 flush，批量同步时产生 N+1
                # 往返。按 SQLite 历史最低 999 参数上限安全的 400 条分块预取
                # （每个 tuple 占两个参数，另有 task_type），并只在
                # 全部新行加入后 flush 一次；已有失败行仍复用原 ID。
                existing_by_identity: dict[
                    tuple[int | None, str, str], Task
                ] = {}
                selected: list[int | Task] = []
                scoped = [
                    (v.get("model_id"), str(v["viewkey"]))
                    for v in unique_videos if v.get("model_id") is not None
                ]
                legacy = [
                    (str(v.get("model_name") or ""), str(v["viewkey"]))
                    for v in unique_videos if v.get("model_id") is None
                ]
                chunk_size = 400
                for start in range(0, len(scoped), chunk_size):
                    rows = (await s.execute(
                        select(Task).where(
                            Task.task_type == "download",
                            tuple_(Task.model_id, Task.viewkey).in_(
                                scoped[start:start + chunk_size]),
                        ).order_by(Task.id.desc())
                    )).scalars().all()
                    for row in rows:
                        existing_by_identity.setdefault(
                            (row.model_id, "", row.viewkey), row)
                for start in range(0, len(legacy), chunk_size):
                    rows = (await s.execute(
                        select(Task).where(
                            Task.task_type == "download",
                            Task.model_id.is_(None),
                            tuple_(Task.model_name, Task.viewkey).in_(
                                legacy[start:start + chunk_size]),
                        ).order_by(Task.id.desc())
                    )).scalars().all()
                    for row in rows:
                        existing_by_identity.setdefault(
                            (None, row.model_name, row.viewkey), row)

                new_tasks: list[Task] = []
                for v in unique_videos:
                    model_id = v.get("model_id")
                    model_name = str(v.get("model_name") or "")
                    identity = (
                        model_id,
                        model_name if model_id is None else "",
                        str(v["viewkey"]),
                    )
                    exists = existing_by_identity.get(identity)
                    if exists:
                        if exists.status in ("queued", "running", "paused", "completed"):
                            continue
                        exists.title = v["title"]
                        exists.url = v["url"]
                        exists.duration = v.get("duration", 0)
                        exists.model_name = model_name
                        exists.model_cloud_cid = v.get("model_cloud_cid", "")
                        exists.artifact_scope = (
                            str(model_id) if model_id is not None else "")
                        exists.origin = str(v.get("origin") or "")
                        exists.origin_candidate_id = v.get("origin_candidate_id")
                        exists.status = "queued"
                        exists.progress = 0.0
                        exists.speed = ""
                        exists.speed_bps = 0.0
                        exists.phase = ""
                        exists.attempt = 0
                        exists.last_progress_at = None
                        # 失败行可能已固定到 fallback 盘并留有断点；重新入队必须继续
                        # 使用原路径，不能把同一媒体拆到另一块磁盘。
                        exists.error = ""
                        exists.patrol_recovery_count = 0
                        selected.append(exists.id)
                        continue
                    t = Task(
                        model_id=model_id,
                        viewkey=v["viewkey"], title=v["title"], url=v["url"],
                        duration=v.get("duration", 0), model_name=model_name,
                        model_cloud_cid=v.get("model_cloud_cid", ""),
                        artifact_scope=str(model_id) if model_id is not None else "",
                        origin=str(v.get("origin") or ""),
                        origin_candidate_id=v.get("origin_candidate_id"),
                        task_type="download", status="queued",
                    )
                    s.add(t)
                    new_tasks.append(t)
                    selected.append(t)
                if new_tasks:
                    await s.flush()
                ids = [
                    item.id if isinstance(item, Task) else item
                    for item in selected
                ]
                await s.commit()
        self._request_pump()
        return ids

    async def reconcile_missing_pipeline(
        self, model_id: int, *, auto_repair: bool = True
    ) -> dict:
        """把一个模特的 missing 视频逐一归类，并有限修复断裂的任务链。

        该方法只复用或重置当前 ``model_id + viewkey`` 的任务，不会清空队列。
        上传已结束但云端重新判 missing 时需连续确认两轮；下载最终失败保持人工处理。
        """
        result = {
            "missing": 0,
            "queued_downloads": 0,
            "pipeline": 0,
            "repaired_uploads": 0,
            "repaired_downloads": 0,
            "confirmation_pending": 0,
            "blocked": 0,
            "unmanaged": 0,
            "issues": [],
        }
        changed_tasks: list[Task] = []
        retry_upload_ids: set[int] = set()
        should_pump = False

        def note(viewkey: str, reason: str):
            if len(result["issues"]) < 20:
                result["issues"].append({"viewkey": viewkey, "reason": reason})

        def reset_for_queue(task: Task, *, reason: str):
            task.status = "queued"
            task.progress = 0.0
            task.speed = ""
            task.speed_bps = 0.0
            task.phase = ""
            task.attempt = 0
            task.downloaded_bytes = 0
            task.total_bytes = 0
            task.last_progress_at = None
            task.error = reason[:500]

        async def artifact_path(download: Task | None, upload: Task | None) -> str:
            candidates: list[str] = []
            if upload and upload.local_path:
                candidates.append(upload.local_path)
            if download:
                if download.local_path:
                    candidates.append(download.local_path)
                candidates.append(_output_path_for_task(download))
            def first_existing() -> str:
                for candidate in dict.fromkeys(candidates):
                    try:
                        if candidate and os.path.isfile(candidate):
                            return candidate
                    except OSError:
                        continue
                return ""

            return await asyncio.to_thread(first_existing)

        async with self._task_create_lock:
            async with self._session_factory() as session:
                model = (await session.execute(
                    select(Model).where(Model.id == model_id)
                )).scalar_one_or_none()
                if model is None:
                    raise ValueError(f"模特不存在: {model_id}")
                videos = (await session.execute(
                    select(Video).where(
                        Video.model_id == model_id,
                        Video.dedup_status == "missing",
                    )
                )).scalars().all()
                result["missing"] = len(videos)
                if not videos:
                    return result

                tasks = (await session.execute(
                    select(Task).where(Task.model_id == model_id)
                )).scalars().all()
                task_map = {(task.viewkey, task.task_type): task for task in tasks}

                def create_download(video: Video, *, recovery_count: int = 0) -> Task:
                    nonlocal should_pump
                    task = Task(
                        model_id=model.id,
                        viewkey=video.viewkey,
                        title=video.title,
                        url=video.url,
                        duration=video.duration,
                        model_name=model.name,
                        model_cloud_cid=model.cloud_cid,
                        artifact_scope=str(model.id),
                        task_type="download",
                        status="queued",
                        patrol_recovery_count=recovery_count,
                    )
                    session.add(task)
                    task_map[(video.viewkey, "download")] = task
                    changed_tasks.append(task)
                    result["queued_downloads"] += 1
                    should_pump = True
                    return task

                def create_upload(download: Task, path: str) -> Task:
                    nonlocal should_pump
                    task = Task(
                        model_id=download.model_id,
                        viewkey=download.viewkey,
                        title=download.title,
                        url=download.url,
                        duration=download.duration,
                        model_name=download.model_name,
                        model_cloud_cid=download.model_cloud_cid,
                        artifact_scope=download.artifact_scope,
                        task_type="upload",
                        status="queued",
                        local_path=path,
                    )
                    session.add(task)
                    task_map[(download.viewkey, "upload")] = task
                    changed_tasks.append(task)
                    result["repaired_uploads"] += 1
                    result["pipeline"] += 1
                    should_pump = True
                    return task

                def park_upload_for_redownload(upload: Task, *, reason: str):
                    """保留上传任务行，等待同一下载任务产出新文件后原地恢复。"""
                    upload.status = "paused"
                    upload.progress = 0.0
                    upload.speed = ""
                    upload.speed_bps = 0.0
                    upload.phase = "waiting_download"
                    upload.attempt = 0
                    upload.last_progress_at = None
                    upload.local_path = ""
                    upload.error = reason[:500]
                    upload.auto_retry_count = 0
                    upload.next_retry_at = None
                    upload.remote_missing_count = 0
                    changed_tasks.append(upload)

                for video in videos:
                    download = task_map.get((video.viewkey, "download"))
                    upload = task_map.get((video.viewkey, "upload"))

                    if download and (
                        download.status in ("queued", "running", "paused")
                        or download.id in self._running
                    ):
                        result["pipeline"] += 1
                        continue

                    if download and download.status == "failed":
                        result["blocked"] += 1
                        note(video.viewkey, "下载最终失败，等待人工处理")
                        continue

                    local_path = await artifact_path(download, upload)

                    if upload and (
                        upload.status in ("queued", "running", "paused")
                        or upload.id in self._running
                    ):
                        result["pipeline"] += 1
                        continue

                    if upload and upload.status == "failed":
                        if not local_path:
                            recovery_count = (
                                download.patrol_recovery_count if download else 0) or 0
                            if auto_repair and recovery_count < self._max_patrol_recoveries:
                                park_upload_for_redownload(
                                    upload,
                                    reason="巡查修复：等待本地文件重新下载",
                                )
                                if download:
                                    reset_for_queue(
                                        download,
                                        reason="巡查修复：上传失败且本地文件丢失，重新下载",
                                    )
                                    download.local_path = ""
                                    download.patrol_recovery_count = recovery_count + 1
                                    changed_tasks.append(download)
                                    result["queued_downloads"] += 1
                                else:
                                    create_download(video, recovery_count=1)
                                result["repaired_downloads"] += 1
                                should_pump = True
                            else:
                                result["blocked"] += 1
                                note(video.viewkey, "上传失败且本地文件不存在")
                            continue

                        if (upload.auto_retry_count or 0) < self._auto_requeue_max:
                            result["pipeline"] += 1
                            if auto_repair and upload.id is not None:
                                retry_upload_ids.add(upload.id)
                        else:
                            result["blocked"] += 1
                            note(video.viewkey, "上传自动重试已达上限")
                        continue

                    if upload and upload.status in ("completed", "skipped"):
                        upload.remote_missing_count = (
                            upload.remote_missing_count or 0) + 1
                        changed_tasks.append(upload)
                        if upload.remote_missing_count < 2:
                            result["confirmation_pending"] += 1
                            note(video.viewkey, "上传已结束但云端缺失，等待下一轮确认")
                            continue
                        if not auto_repair:
                            result["unmanaged"] += 1
                            continue
                        if local_path:
                            reset_for_queue(
                                upload,
                                reason="巡查修复：连续两轮确认云端缺失，重新上传",
                            )
                            upload.local_path = local_path
                            upload.auto_retry_count = 0
                            upload.next_retry_at = None
                            upload.remote_missing_count = 0
                            result["repaired_uploads"] += 1
                            result["pipeline"] += 1
                            should_pump = True
                            continue
                        recovery_count = (
                            download.patrol_recovery_count if download else 0) or 0
                        if recovery_count >= self._max_patrol_recoveries:
                            result["blocked"] += 1
                            note(video.viewkey, "远端漂移修复已达上限")
                            continue
                        park_upload_for_redownload(
                            upload,
                            reason="巡查修复：连续两轮确认云端缺失，等待重新下载",
                        )
                        if download:
                            reset_for_queue(
                                download,
                                reason="巡查修复：连续两轮确认云端缺失，重新下载",
                            )
                            download.local_path = ""
                            download.patrol_recovery_count = recovery_count + 1
                            changed_tasks.append(download)
                            result["queued_downloads"] += 1
                        else:
                            create_download(video, recovery_count=1)
                        result["repaired_downloads"] += 1
                        should_pump = True
                        continue

                    if download and download.status == "completed":
                        if not auto_repair:
                            result["unmanaged"] += 1
                            continue
                        if local_path:
                            create_upload(download, local_path)
                            continue
                        recovery_count = download.patrol_recovery_count or 0
                        if recovery_count >= self._max_patrol_recoveries:
                            result["blocked"] += 1
                            note(video.viewkey, "下载产物丢失，自动恢复已达上限")
                            continue
                        reset_for_queue(
                            download,
                            reason="巡查修复：下载完成但上传任务/本地文件缺失，重新下载",
                        )
                        download.local_path = ""
                        download.patrol_recovery_count = recovery_count + 1
                        changed_tasks.append(download)
                        result["queued_downloads"] += 1
                        result["repaired_downloads"] += 1
                        should_pump = True
                        continue

                    if download is None:
                        if auto_repair:
                            create_download(video)
                        else:
                            result["unmanaged"] += 1
                        continue

                    # 未知/历史终态不静默吞掉；有限重置后仍异常则交人工处理。
                    if (auto_repair and (download.patrol_recovery_count or 0)
                            < self._max_patrol_recoveries):
                        reset_for_queue(download, reason="巡查修复：重置异常下载状态")
                        download.patrol_recovery_count = (
                            download.patrol_recovery_count or 0) + 1
                        changed_tasks.append(download)
                        result["queued_downloads"] += 1
                        result["repaired_downloads"] += 1
                        should_pump = True
                    else:
                        result["blocked"] += 1
                        note(video.viewkey, f"无法自动处理的下载状态: {download.status}")

                await session.commit()

        for task in changed_tasks:
            if task.id is None:
                continue
            try:
                await self._notify(task)
            except Exception:
                pass
        for task_id in retry_upload_ids:
            self._schedule_auto_requeue(task_id, "巡查恢复上传重试")
        if should_pump:
            self._request_pump()
        return result

    # ---------- 调度 ----------
    async def _schedule_source_wakeup(self, cutoff=None):
        if self._source_wakeup:
            self._source_wakeup.cancel()
            self._source_wakeup = None
        if self._paused:
            return
        # 与本轮入队查询共用截止时刻：若 deadline 恰好跨过两次查询间隙，
        # 仍安排一次立即唤醒，不能把该任务永久留在无人唤醒的队列。
        cutoff = cutoff or datetime.now()
        async with self._session_factory() as session:
            deadline = (await session.execute(select(func.min(Task.next_retry_at)).where(
                Task.task_type == "download", Task.status == "queued",
                Task.next_retry_at > cutoff,
            ))).scalar_one_or_none()
        if deadline is not None:
            delay = max(0.05, (deadline - datetime.now()).total_seconds())
            self._source_wakeup = asyncio.get_running_loop().call_later(
                delay, self._request_pump)

    async def _pump(self):
        """检查队列，启动可运行的任务（受并发与暂停约束）。

        用 _pump_lock 串行化，避免多个 finally 并发调用时读到同一批 queued 任务、
        在 self._running 判断之间都通过检查而超开。并在锁内按类型统计在跑数量，
        确保不超过各自的并发上限（信号量是第二道防线）。
        """
        if self._paused:
            return
        async with self._pump_lock:
            ready_cutoff = datetime.now()
            async with self._session_factory() as s:
                # 统计当前在跑任务的类型分布，用于并发上限判断
                running_rows = (await s.execute(
                    select(Task.task_type).where(Task.id.in_(list(self._running.keys())))
                )).scalars().all() if self._running else []
            n_download = sum(1 for x in running_rows if x == "download")
            n_upload = sum(1 for x in running_rows if x == "upload")
            # 限流恢复后按档位降低同时下载数；已在跑的任务不受影响。
            download_cap = self._download_throttle.concurrency_cap(self._download_limit)
            download_slots = max(0, download_cap - n_download)
            upload_slots = max(
                0, settings.max_concurrent_uploads - n_upload)
            if self._upload_gate.blocked:
                # 115 正在拒绝本机上传：派出去只会立刻失败；闸门重开会重新唤醒调度。
                upload_slots = 0

            # 上传没有本地磁盘门禁，直接按空槽 LIMIT；下载则在下面按
            # (created_at,id) keyset 分页探测，既保持内存有界，也不会让前32个
            # 固定在不可用磁盘的断点永久饿死后续可运行任务。
            queued_uploads: list[Task] = []
            async with self._session_factory() as s:
                not_running = (
                    ~Task.id.in_(list(self._running))
                    if self._running else True
                )
                if upload_slots:
                    queued_uploads = (await s.execute(
                        select(Task).where(
                            Task.status == "queued",
                            Task.task_type == "upload",
                            not_running,
                        ).order_by(Task.created_at, Task.id).limit(upload_slots)
                    )).scalars().all()
            space_cache: dict[str, bool] = {}
            queued_downloads_found = False
            cursor_created: datetime | None = None
            cursor_id = 0
            media_tools_blocked = False
            if download_slots and self._link_health.outage_active:
                # 共享链路断流：新任务派出去也只会立刻失败；恢复回调会重新唤醒调度。
                download_slots = 0
            if download_slots and self._download_throttle.blocked:
                # CDN 限流冷却中：继续请求只会延长限流；冷却结束回调会重新唤醒调度。
                download_slots = 0
            if download_slots and not self._download_settings.window_open(self._now()):
                # 时段外不开始新下载；已在跑的继续下完，到点由定时唤醒恢复派发。
                download_slots = 0
                if self._window_wakeup is None:
                    self._schedule_window_wakeup()
            if download_slots and not await self._media_tools_ready():
                media_tools_blocked = True
                download_slots = 0
            while n_download < download_cap and download_slots:
                async with self._session_factory() as s:
                    conditions = [
                        Task.status == "queued",
                        Task.task_type == "download",
                        or_(Task.next_retry_at.is_(None), Task.next_retry_at <= ready_cutoff),
                    ]
                    if self._running:
                        conditions.append(~Task.id.in_(list(self._running)))
                    if cursor_created is not None:
                        conditions.append(or_(
                            Task.created_at > cursor_created,
                            and_(
                                Task.created_at == cursor_created,
                                Task.id > cursor_id,
                            ),
                        ))
                    batch = (await s.execute(
                        select(Task).where(*conditions).order_by(
                            Task.created_at, Task.id
                        ).limit(32)
                    )).scalars().all()
                if not batch:
                    break
                queued_downloads_found = True

                for t in batch:
                    cursor_created = t.created_at
                    cursor_id = t.id
                    if t.id in self._running:
                        continue
                    output_path = await asyncio.to_thread(
                        _select_output_path_for_task,
                        t,
                        space_cache,
                    )
                    if output_path is None:
                        continue
                    if os.path.normcase(str(t.local_path or "")) != os.path.normcase(
                            output_path):
                        # 在 worker 启动前持久化绝对路径。进程重启、暂停续传、上传与
                        # 清理都只认这个路径，避免同一任务跨盘写出两份中间文件。
                        await self._set_status(
                            t.id,
                            local_path=output_path,
                            _task_type="download",
                        )
                        t.local_path = output_path
                    n_download += 1
                    self._running[t.id] = asyncio.create_task(
                        self._run_download(t.id))
                    if n_download >= download_cap:
                        break
                if len(batch) < 32:
                    break

            for t in queued_uploads:
                if t.id in self._running:
                    continue
                if n_upload >= settings.max_concurrent_uploads:
                    break
                n_upload += 1
                self._running[t.id] = asyncio.create_task(self._run_upload(t.id))

            # 磁盘不足且确有下载排队：告警（内部按场景节流，不会每次 pump 都推）。
            if queued_downloads_found and not await asyncio.to_thread(
                    download_space_available, space_cache):
                self._alert_disk_full()
            if media_tools_blocked:
                async with self._session_factory() as s:
                    waiting_download = (await s.execute(
                        select(Task.id).where(
                            Task.status == "queued", Task.task_type == "download",
                        ).limit(1)
                    )).first()
                if waiting_download is not None:
                    self._alert_media_tools_unavailable()
                    self._schedule_media_tools_recheck()

            # 批次完成边沿检测：某类型仍有活跃(queued/running)任务或重试等待即为活跃；
            # 从"有→无"时安排一次去抖确认后推送。查询与状态更新都在 _pump 锁内完成。
            async with self._session_factory() as s:
                active_rows = (await s.execute(
                    select(Task.task_type, func.count(Task.id))
                    .where(Task.status.in_(("queued", "running")))
                    .group_by(Task.task_type)
                )).all()
            active_counts = {t: int(c) for t, c in active_rows}
            await self._schedule_source_wakeup(ready_cutoff)
            for task_type in ("download", "upload"):
                waiters = (
                    self._download_retry_waiters if task_type == "download"
                    else self._upload_retry_waiters)
                waiting = any(not t.done() for t in waiters.values())
                active = active_counts.get(task_type, 0) > 0 or waiting
                was_active = self._batch_active.get(task_type, False)
                self._batch_active[task_type] = active
                if active and not was_active:
                    # 批次起点：记录终态累计基线，收尾时用差值得到"本批"数量。
                    self._batch_baseline[task_type] = (
                        await self._task_status_counts(task_type))
                if was_active and not active:
                    existing = self._batch_debounce.get(task_type)
                    if existing and not existing.done():
                        continue
                    self._batch_debounce[task_type] = self._spawn_bg(
                        self._confirm_batch_done(task_type))

    def _alert_disk_full(self):
        """全部下载目录空间不足告警，按时间戳节流，避免每次 pump 都推。"""
        now = time.monotonic()
        if (self._disk_alert_at is not None
                and now - self._disk_alert_at < self._disk_alert_throttle):
            return
        self._disk_alert_at = now
        reserve_gb = max(
            0.0, float(getattr(settings, "download_min_free_space_gb", 20.0)))
        self._spawn_bg(self._notifier.notify_error(
            "磁盘空间不足",
            f"所有下载目录的可用空间都低于保留阈值 {reserve_gb:.0f}GB，"
            "新下载已暂缓，仅继续上传并清理已完成文件。请及时清理磁盘。"))

    async def _confirm_batch_done(self, task_type: str):
        """去抖确认：延迟后复查该类型仍无活跃任务，才推送批次完成汇总。"""
        await asyncio.sleep(self._batch_debounce_seconds)
        if self._paused:
            return
        async with self._session_factory() as s:
            still_active = (await s.execute(
                select(func.count(Task.id)).where(
                    Task.task_type == task_type,
                    Task.status.in_(("queued", "running")),
                )
            )).scalar_one()
        waiters = (
            self._download_retry_waiters if task_type == "download"
            else self._upload_retry_waiters)
        if int(still_active or 0) > 0 or any(
                not t.done() for t in waiters.values()):
            return
        # 用当前累计减去批次起点基线，得到本批各终态的增量。
        current = await self._task_status_counts(task_type)
        baseline = self._batch_baseline.pop(task_type, {})
        delta = {
            key: max(0, int(current.get(key, 0)) - int(baseline.get(key, 0)))
            for key in ("completed", "failed", "skipped")
        }
        if not any(delta.values()):
            return
        await self._notifier.notify_batch_done(task_type, delta)

    async def _task_status_counts(self, task_type: str) -> dict[str, int]:
        """只取批次通知需要的状态计数，不扫描失败任务的慢盘成品。"""
        async with self._session_factory() as s:
            rows = (await s.execute(select(
                Task.status,
                func.count(Task.id),
            ).where(
                Task.task_type == task_type,
            ).group_by(Task.status))).all()
        return {status: int(count) for status, count in rows}

    async def _set_status(self, task_id: int, **fields):
        """更新任务字段并广播。

        进度类高频更新无需回读整行：直接用已知字段构造广播负载，省掉一次 SELECT。
        仅当调用方需要完整 Task 对象时（传 _return_task=True）才回读。
        可传 _task_type 让轻量广播带上类型，便于前端按页面过滤。
        """
        return_task = fields.pop("_return_task", False)
        task_type = fields.pop("_task_type", None)
        if "speed_bps" in fields and "speed" not in fields:
            fields["speed"] = _format_speed(fields["speed_bps"])
        persisted_fields = {**self._latest_progress.get(task_id, {}), **fields}
        t = await self._task_store.update(
            task_id,
            persisted_fields,
            return_task=return_task,
        )
        if return_task:
            if t is None:
                return None
            await self._notify(t)
            return t
        # 不回读：构造一条轻量广播（仅含本次变更可见的字段）
        await self._notify_fields(task_id, persisted_fields, task_type)
        if fields.get("status") in {"completed", "failed", "skipped", "paused"}:
            self._forget_progress(task_id)
        return None

    async def _set_progress(
        self,
        task_id: int,
        fields: dict,
        task_type: str,
    ) -> None:
        """实时广播进度，并按周期/阶段变化保存恢复快照。"""
        if "speed_bps" in fields and "speed" not in fields:
            fields["speed"] = _format_speed(fields["speed_bps"])
        self._latest_progress[task_id] = {
            **self._latest_progress.get(task_id, {}),
            **fields,
        }
        await self._notify_fields(task_id, fields, task_type)

        now = time.monotonic()
        phase = str(fields.get("phase") or "")
        phase_changed = (
            "phase" in fields
            and phase != self._progress_persisted_phase.get(task_id)
        )
        due = (
            now - self._progress_persisted_at.get(task_id, float("-inf"))
            >= PROGRESS_PERSIST_INTERVAL
        )
        if not phase_changed and not due:
            return
        try:
            await self._task_store.update(task_id, self._latest_progress[task_id])
        except Exception as error:
            # 长时间下载不能因一个可在下个快照补写的 SQLite 瞬时锁被终止。
            # 其他数据库错误仍向上抛出，避免掩盖磁盘损坏/模式错误。
            if not is_sqlite_locked(error):
                raise
            logger.warning("任务 %s 进度快照因 SQLite 写锁延后", task_id)
            return
        self._progress_persisted_at[task_id] = now
        if "phase" in fields:
            self._progress_persisted_phase[task_id] = phase

    def _forget_progress(self, task_id: int) -> None:
        self._latest_progress.pop(task_id, None)
        self._progress_persisted_at.pop(task_id, None)
        self._progress_persisted_phase.pop(task_id, None)

    async def _notify_fields(self, task_id: int, fields: dict, task_type: str | None = None):
        """基于本次更新的字段做轻量广播，避免回读整行。"""
        if not self._broadcast:
            return
        payload: TaskPatch = {"id": task_id}
        if task_type:
            payload["task_type"] = task_type
        for k in (
                "status", "progress", "speed", "speed_bps", "downloaded_bytes",
                "total_bytes", "phase", "attempt", "last_progress_at", "error",
                "auto_retry_count", "next_retry_at", "remote_missing_count",
                "patrol_recovery_count"):
            if k in fields:
                payload[k] = _wire_value(fields[k])
        await self._broadcast({"type": "task_update", "task": payload})

    # ---------- 下载执行 ----------
    async def _run_download(self, task_id: int):
        download_ok = False
        pending_clean_start = False
        clean_retry_used = False
        source_retry_used = False
        quality_cap: int | None = None
        quality_fallback_used = False
        clean_attempt_started = False
        source_round = 0
        current = asyncio.current_task()

        def paused_recovery_phase() -> str:
            if quality_fallback_used and quality_cap is not None:
                action = "resume" if clean_attempt_started else "retry"
                return f"paused_quality_{action}:{quality_cap}"
            if clean_retry_used or pending_clean_start:
                return (
                    "paused_clean_resume"
                    if clean_attempt_started else "paused_clean_retry"
                )
            if source_retry_used:
                return "paused_source_retry"
            if source_round:
                return "paused_source_cooldown"
            return ""

        try:
            if self._paused:
                return
            # queued_clean_retry / queued_source_retry 是落库的恢复意图。先读取再清空
            # 阶段，确保进程重启后仍能执行同一种有限恢复策略。
            t = await self._set_status(
                task_id, status="running", speed_bps=0.0,
                attempt=0, _return_task=True)
            if t is None:
                return
            queued_recovery = str(getattr(t, "phase", "") or "")
            if queued_recovery.startswith("source_cooldown:"):
                queued_recovery = queued_recovery.partition(":")[2]
            source_round = max(0, int(getattr(t, "auto_retry_count", 0) or 0))
            quality_cap = _quality_retry_cap(queued_recovery)
            quality_fallback_used = quality_cap is not None
            clean_resume = queued_recovery == "queued_clean_resume"
            quality_resume = queued_recovery.startswith(
                "queued_quality_resume:"
            ) and quality_fallback_used
            pending_clean_start = (
                queued_recovery == "queued_clean_retry"
                or (
                    queued_recovery.startswith("queued_quality_retry:")
                    and quality_fallback_used
                )
            )
            clean_retry_used = clean_resume or quality_resume
            clean_attempt_started = clean_resume or quality_resume
            source_retry_used = (
                queued_recovery == "queued_source_retry"
                or quality_fallback_used
            )
            recovery_progress = (
                {
                    "progress": 0.0,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "last_progress_at": None,
                }
                if pending_clean_start else {}
            )
            await self._set_status(
                task_id, phase="resolving", error="", next_retry_at=None, _task_type="download",
                **recovery_progress)
            out_path = _output_path_for_task(t)

            async def on_progress(telemetry: dict):
                fields = {
                    key: telemetry[key]
                    for key in (
                        "phase", "progress", "speed_bps", "downloaded_bytes",
                        "total_bytes", "last_progress_at")
                    if key in telemetry
                }
                stamp = fields.get("last_progress_at")
                if isinstance(stamp, str):
                    try:
                        fields["last_progress_at"] = datetime.fromisoformat(
                            stamp.replace("Z", "+00:00"))
                    except ValueError:
                        fields.pop("last_progress_at", None)
                if fields:
                    await self._set_progress(task_id, fields, "download")

            async def download_with_local_processing_retry(
                    plan: dict, attempt: int, *, clean_start: bool = False,
                    conservative: bool = False):
                """网络完成后若本地封装暂时失败，只重做 staging 处理。"""
                try:
                    return await self._downloader.download(
                        plan, out_path, on_progress=on_progress,
                        task_key=str(task_id), attempt=attempt,
                        clean_start=clean_start,
                        conservative=conservative)
                except DownloadProcessingError as first_error:
                    last_error = first_error
                for processing_attempt in range(1, self._max_processing_retries + 1):
                    await self._set_status(
                        task_id, phase="processing", speed_bps=0.0,
                        error=(
                            f"本地封装重试 {processing_attempt}/"
                            f"{self._max_processing_retries}："
                            f"{_error_text(last_error)[:180]}"
                        ),
                        _task_type="download")
                    await asyncio.sleep(1)
                    try:
                        return await self._downloader.download(
                            plan, out_path, on_progress=on_progress,
                            task_key=str(task_id), attempt=attempt,
                            allow_network=False,
                            conservative=conservative)
                    except DownloadProcessingError as error:
                        last_error = error
                raise last_error

            # 每次完整尝试只在真正解析/下载时持有信号量。停滞 worker 被杀后先释放
            # 槽位，让队列中的下一任务接管；本任务在 retrying 退避后再公平竞争。
            last_err = None
            selected_quality = 0
            # 共享链路断流时不推进 attempt：等链路恢复后重做同一次尝试，总等待有上限。
            attempt = 1
            outage_budget = NETWORK_OUTAGE_MAX_WAIT_SECONDS
            probe_target = ProbeTarget.for_url(settings.ph_base_url, settings.proxy)
            while attempt <= self._max_download_retries + 1:
                if self._download_throttle.blocked:
                    # 冷却期间到点的重试/断流恢复也不再发请求，直接退回队列等冷却结束。
                    raise _DownloadThrottled()
                clean_start = pending_clean_start
                pending_clean_start = False
                if clean_start:
                    clean_retry_used = True
                conservative_attempt = clean_retry_used or quality_fallback_used
                async with self._download_sem:
                    self._download_retry_waiters.pop(task_id, None)
                    if current is not None:
                        self._running[task_id] = current
                    if self._paused:
                        raise asyncio.CancelledError
                    try:
                        await self._set_status(
                            task_id, status="running", phase="resolving",
                            attempt=attempt, speed_bps=0.0, error="",
                            _task_type="download")
                        try:
                            async with asyncio.timeout(settings.download_resolve_timeout):
                                if quality_cap is None:
                                    info = await self._resolve_media(t.url)
                                else:
                                    info = await self._resolve_media(
                                        t.url, max_quality=quality_cap)
                        except TimeoutError as exc:
                            raise NetworkDownloadError(
                                "视频页或 HLS 清单解析超时") from exc
                        await self._set_status(
                            task_id, m3u8_url=info["m3u8_url"],
                            _task_type="download")
                        # 之后的失败发生在媒体链路上，用同一下载出口探测媒体主机。
                        probe_target = ProbeTarget.for_url(
                            info["m3u8_url"], settings.download_proxy) or probe_target
                        plan = info["hls_plan"]
                        selected_quality = int(
                            plan.get("quality") or info.get("quality") or 0)
                        if conservative_attempt:
                            # 从这里开始 clean_start 已交给 downloader，暂停后必须
                            # 续传这次新 staging，不能再次删除。
                            clean_attempt_started = True
                        await download_with_local_processing_retry(
                            plan,
                            attempt,
                            clean_start=clean_start,
                            conservative=conservative_attempt,
                        )
                        last_err = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        last_err = e

                if last_err is None:
                    break
                if _is_rate_limited(last_err):
                    # 先关闸再退回队列，finally 里唤醒的调度不会立刻把它派回去。
                    self._download_throttle.report()
                    raise _DownloadThrottled(last_err)
                if (outage_budget > 0 and _is_link_failure(last_err)
                        and not await self._link_health.check(probe_target)):
                    restored, waited = await self._wait_for_link(
                        task_id, current, attempt, last_err, outage_budget)
                    outage_budget -= waited
                    if restored:
                        continue
                retry_label = "完整重试"
                should_retry = False
                if (isinstance(last_err, DownloadArtifactError)
                        and not clean_retry_used
                        and attempt <= self._max_download_retries):
                    # staging 已证明发生分片重复/乱序；只允许一次从零、单连接重下。
                    pending_clean_start = True
                    clean_attempt_started = False
                    should_retry = True
                    retry_label = "清洁重试"
                elif (isinstance(last_err, DownloadArtifactError)
                        and clean_retry_used
                        and selected_quality > 1
                        and attempt <= self._max_download_retries):
                    # 单连接、从零下载仍在同一清晰度复现分片乱序，说明更可能是
                    # 该档 CDN 源本身损坏。每次只降一级并从零下载，且仍受总共
                    # 三次完整尝试约束；额度耗尽就停在人工处理，绝不生成时间戳
                    # 掩盖缺片/重复内容。
                    quality_cap = selected_quality - 1
                    quality_fallback_used = True
                    pending_clean_start = True
                    clean_attempt_started = False
                    source_retry_used = True
                    should_retry = True
                    retry_label = f"降质清洁重试（低于 {selected_quality}p）"
                elif (bool(getattr(last_err, "retryable", False))
                        and not isinstance(last_err, DownloadArtifactError)
                        and not (source_round and isinstance(last_err, (HLSSourceUnavailableError, SourceUnavailableDownloadError)))
                        and attempt <= self._max_download_retries):
                    should_retry = True
                if should_retry:
                    await self._set_status(
                        task_id, phase="retrying", speed_bps=0.0,
                        attempt=attempt + 1,
                        error=(
                            f"准备第 {attempt + 1}/3 次{retry_label}："
                            f"{_error_text(last_err)[:180]}"
                        ),
                        _task_type="download")
                    self._running.pop(task_id, None)
                    if current is not None:
                        self._download_retry_waiters[task_id] = current
                    self._request_pump()
                    if (retry_label == "完整重试"
                            and isinstance(last_err, NetworkDownloadError)):
                        await asyncio.sleep(NETWORK_RETRY_DELAYS[
                            min(attempt, len(NETWORK_RETRY_DELAYS)) - 1])
                    else:
                        await asyncio.sleep(2 ** (attempt - 1))   # 1s, 2s
                    attempt += 1
                    continue
                break

            if last_err is not None:
                raise last_err
            final_size = await asyncio.to_thread(os.path.getsize, out_path)
            await self._set_status(
                task_id, status="completed", phase="completed", progress=100.0,
                speed_bps=0.0, downloaded_bytes=final_size,
                total_bytes=final_size, local_path=out_path, error="",
                next_retry_at=None,
                _task_type="download")
            download_ok = True
            self._download_throttle.record_success()
            self._spawn_bg(self._notifier.notify_download_done(t.model_name, t.title))
        except asyncio.CancelledError:
            await self._set_status(
                task_id, status="paused", phase=paused_recovery_phase(),
                speed_bps=0.0,
                _task_type="download")
            raise
        except _DownloadThrottled:
            phase = paused_recovery_phase()
            if task_id in self._download_pause_requests:
                await self._set_status(task_id, status="paused", phase=phase,
                                       speed_bps=0.0, _task_type="download")
            else:
                # 不计次：保留断点与恢复意图回到队列，冷却结束后按原顺序重新派发。
                await self._set_status(
                    task_id, status="queued", phase=phase.replace("paused_", "queued_", 1),
                    speed_bps=0.0, next_retry_at=None,
                    error=DOWNLOAD_THROTTLE_WAITING_TEXT, _task_type="download")
        except (HLSSourceUnavailableError, SourceUnavailableDownloadError) as e:
            if task_id in self._download_pause_requests:
                await self._set_status(task_id, status="paused", phase=paused_recovery_phase(),
                                       speed_bps=0.0, _task_type="download")
            elif source_round < len(SOURCE_RECHECK_DELAYS):
                deadline = datetime.now() + timedelta(seconds=SOURCE_RECHECK_DELAYS[source_round])
                intent = paused_recovery_phase().replace("paused_", "queued_", 1)
                cooldown_phase = "source_cooldown"
                if intent and intent != "queued_source_cooldown":
                    cooldown_phase += ":" + intent
                await self._set_status(
                    task_id, status="queued", phase=cooldown_phase, speed_bps=0.0,
                    next_retry_at=deadline, auto_retry_count=source_round + 1,
                    error=_error_text(e)[:500], _task_type="download")
            else:
                await self._set_status(
                    task_id, status="failed", phase="failed_source_exhausted", speed_bps=0.0,
                    next_retry_at=None, error=_error_text(e)[:500], _task_type="download")
        except VideoUnavailableError as e:
            await self._set_status(
                task_id, status="skipped", phase="skipped", speed_bps=0.0,
                error=_error_text(e)[:500], _task_type="download")
        except Exception as e:
            if task_id in self._download_pause_requests:
                await self._set_status(
                    task_id, status="paused", phase=paused_recovery_phase(),
                    speed_bps=0.0, error="", _task_type="download")
            elif (isinstance(e, DownloadEnvironmentError)
                    and not await self._media_tools_ready(force=True)):
                # 工具缺失不是该视频的问题：保留已下载断点与恢复意图回到队列，
                # 调度门禁会在工具可用前暂缓派发，避免反复下载后再失败。
                await self._set_status(
                    task_id, status="queued",
                    phase=paused_recovery_phase().replace("paused_", "queued_", 1),
                    speed_bps=0.0, next_retry_at=None,
                    error=f"{MEDIA_TOOLS_WAITING_PREFIX}：{_error_text(e)}"[:500],
                    _task_type="download")
            else:
                error = _error_text(e)
                disposition = classify_stored_download_failure(error)
                if (clean_retry_used
                        and disposition.recovery_action == "retry_clean"):
                    failed_phase = "failed_clean_retry"
                elif (source_retry_used
                        and disposition.recovery_action == "retry_source"):
                    failed_phase = "failed_source_retry"
                else:
                    failed_phase = "failed"
                await self._set_status(
                    task_id, status="failed", phase=failed_phase, speed_bps=0.0,
                    error=error[:500], _task_type="download")
        finally:
            self._download_pause_requests.discard(task_id)
            if self._running.get(task_id) is current:
                self._running.pop(task_id, None)
            if self._download_retry_waiters.get(task_id) is current:
                self._download_retry_waiters.pop(task_id, None)
            self._request_pump()

        # 流转到上传（含二次去重）——必须在主 try 之外：下载已成功并已提交 completed，
        # 若建上传任务时 DB 抛异常，绝不能把这个已成功的下载翻转回 failed 并触发重复下载。
        if download_ok:
            try:
                await self._create_upload_task(task_id)
            except Exception as e:
                # 仅记录，不改动下载任务状态。上传任务缺失可由后续巡查/手动补建。
                print(f"[task_manager] 下载完成但建上传任务失败 task={task_id}: {e}")

    async def _wait_for_link(self, task_id: int, current, attempt: int,
                             error: BaseException, budget: float) -> tuple[bool, float]:
        """释放下载槽等待共享链路恢复；返回 (是否恢复, 实际等待秒数)。"""
        await self._set_status(
            task_id, phase=NETWORK_WAIT_PHASE, speed_bps=0.0, attempt=attempt,
            error=(
                f"网络或代理链路中断，恢复后继续第 {attempt}/3 次尝试（不计次）："
                f"{_error_text(error)[:160]}"
            ),
            _task_type="download")
        self._running.pop(task_id, None)
        if current is not None:
            self._download_retry_waiters[task_id] = current
        self._request_pump()
        started = time.monotonic()
        restored = await self._link_health.wait_restored(budget)
        return restored, time.monotonic() - started

    def _schedule_auto_requeue(self, task_id: int, err: str):
        """为失败上传安排持久化、限次数的延迟重试。"""
        existing = self._upload_retry_waiters.get(task_id)
        if existing and not existing.done():
            return
        task = self._spawn_bg(self._prepare_auto_requeue(task_id, err))
        if not isinstance(task, asyncio.Task):
            return
        self._upload_retry_waiters[task_id] = task

        def discard(done: asyncio.Task):
            if self._upload_retry_waiters.get(task_id) is done:
                self._upload_retry_waiters.pop(task_id, None)

        task.add_done_callback(discard)

    async def _prepare_auto_requeue(self, task_id: int, err: str):
        """恢复已有截止时间，或原子登记下一次上传重试后等待。"""
        async with self._session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id)
            )).scalar_one_or_none()
            if not task or task.task_type != "upload" or task.status != "failed":
                return
            retry_at = task.next_retry_at
            count = int(task.auto_retry_count or 0)
            if retry_at is None:
                if count >= self._auto_requeue_max:
                    return
                count += 1
                delay = self._auto_requeue_delay * (2 ** (count - 1))
                retry_at = datetime.now() + timedelta(seconds=delay)
                task.auto_retry_count = count
                task.next_retry_at = retry_at
                if err:
                    task.error = err[:500]
                await session.commit()

        delay = max(0.0, (retry_at - datetime.now()).total_seconds())
        if delay:
            await asyncio.sleep(delay)
        if self._paused:
            return

        async with self._session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id)
            )).scalar_one_or_none()
            if not task or task.status != "failed":
                return
            if task.next_retry_at and task.next_retry_at > datetime.now():
                return
            attempt = int(task.auto_retry_count or 0)
            task.status = "queued"
            task.error = f"自动重试 {attempt}/{self._auto_requeue_max}…"
            task.progress = 0.0
            task.speed = ""
            task.speed_bps = 0.0
            task.phase = ""
            task.attempt = 0
            task.next_retry_at = None
            await session.commit()
            await self._notify(task)
        self._request_pump()

    async def _schedule_pending_upload_retries(self):
        """进程恢复/解除全局暂停后续接尚未耗尽的上传重试。"""
        if self._paused:
            return
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(Task.id).where(
                    Task.task_type == "upload",
                    Task.status == "failed",
                    (Task.next_retry_at.is_not(None))
                    | (Task.auto_retry_count < self._auto_requeue_max),
                )
            )).scalars().all()
        for task_id in rows:
            self._schedule_auto_requeue(task_id, "恢复未完成的上传重试")

    async def _create_upload_task(self, download_task_id: int):
        """下载完成后生成上传任务。

        去重：同 viewkey 若已有普通未结束任务或已成功任务，则不重复创建。
        failed/skipped 以及巡查暂存的 waiting_download 任务复用原行，保留任务 ID。
        """
        async with self._task_create_lock:
            async with self._session_factory() as s:
                dt = (await s.execute(
                    select(Task).where(Task.id == download_task_id)
                )).scalar_one_or_none()
                if dt is None:
                    return
                existing = (await s.execute(
                    select(Task).where(
                        _task_identity(dt.model_id, dt.model_name),
                        Task.viewkey == dt.viewkey,
                        Task.task_type == "upload",
                    ).order_by(Task.id.desc())
                )).scalars().first()
                if existing:
                    waiting_download = existing.phase == "waiting_download"
                    if (existing.status in ("queued", "running", "paused", "completed")
                            and not waiting_download):
                        return
                    existing.model_id = dt.model_id
                    existing.title = dt.title
                    existing.url = dt.url
                    existing.duration = dt.duration
                    existing.model_name = dt.model_name
                    existing.model_cloud_cid = dt.model_cloud_cid
                    existing.artifact_scope = dt.artifact_scope
                    existing.origin = dt.origin
                    existing.origin_candidate_id = dt.origin_candidate_id
                    existing.status = "queued"
                    existing.progress = 0.0
                    existing.speed = ""
                    existing.speed_bps = 0.0
                    existing.phase = ""
                    existing.attempt = 0
                    existing.last_progress_at = None
                    existing.local_path = dt.local_path
                    existing.error = ""
                    existing.auto_retry_count = 0
                    existing.next_retry_at = None
                    existing.remote_missing_count = 0
                else:
                    s.add(Task(
                        model_id=dt.model_id,
                        viewkey=dt.viewkey, title=dt.title, url=dt.url,
                        duration=dt.duration, model_name=dt.model_name,
                        model_cloud_cid=dt.model_cloud_cid,
                        artifact_scope=dt.artifact_scope,
                        origin=dt.origin,
                        origin_candidate_id=dt.origin_candidate_id,
                        task_type="upload", status="queued", local_path=dt.local_path,
                    ))
                await s.commit()
        self._request_pump()

    # ---------- 上传执行 ----------
    def _prune_dedup_cache(self, now: float) -> None:
        """节流清除过期目录值；TTL 内的完整快照及其建立时间保持不变。"""
        if now < self._dedup_cache_next_prune_at:
            return
        self._dedup_cache_next_prune_at = now + self._dedup_cache_ttl
        for cid, (built_at, _index) in list(self._dedup_cache.items()):
            if now - built_at >= self._dedup_cache_ttl:
                self._dedup_cache.pop(cid, None)

    async def _get_dedup_index(self, cid: str) -> dict:
        """获取 115 文件夹的去重索引，带短期缓存。

        同一模特多个视频连续上传时，避免每个任务都全量扫描该文件夹。
        缓存 TTL 内复用；超时则重新扫描。按 CID single-flight 并在锁内二次检查，
        避免两个并发上传同时对同一目录各扫一遍。权威确认后增量合并，异常时失效。
        """
        self._prune_dedup_cache(time.monotonic())
        lock = self._dedup_cache_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._dedup_cache.get(cid)
            if cached and now - cached[0] < self._dedup_cache_ttl:
                return cached[1]
            # 115 新上传文件在媒体索引完成前不会出现在 type=4（视频）列表里。
            # 去重必须读取未过滤目录，否则自动重试会把“已上传、待索引”误判成不存在。
            entries = await self._cloud.list_all_files(cid, only_video=False)
            # 未过滤目录响应还包含子目录；只让真正的文件进入 viewkey 索引。
            cloud_files = [
                item for item in entries
                if item.get("fid") or item.get("file_id")
            ]
            index = dedup.build_cloud_index(cloud_files)
            self._dedup_cache[cid] = (time.monotonic(), index)
            return index

    async def _merge_confirmed_cloud_file(self, cid: str, info: dict) -> None:
        """把权威 file-ID 记录增量合入仍有效的 CID 去重缓存。

        只更新由完整目录扫描建立的缓存；若缓存已经因不确定结果失效，绝不以
        单个文件伪装成完整目录快照。保留原建立时间，使连续上传不会无限延长
        TTL、遮蔽其它 115 客户端写入。
        """
        lock = self._dedup_cache_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            cached = self._dedup_cache.get(cid)
            if not cached:
                return
            built_at, index = cached
            fid = str(info.get("fid") or info.get("file_id") or "").strip()
            confirmed_fids = index.setdefault("confirmed_fids", set())
            if fid and fid in confirmed_fids:
                return

            single = dedup.build_cloud_index([info])
            index.setdefault("viewkeys", set()).update(
                single.get("viewkeys") or set())
            duration_items = index.setdefault("duration_items", [])
            next_idx = index.get("next_duration_idx")
            if next_idx is None:
                # 旧缓存/测试替身首次合并时回填；之后追加不再扫描完整目录。
                next_idx = 1 + max(
                    (int(item.get("idx", -1)) for item in duration_items),
                    default=-1,
                )
            buckets = index.setdefault("duration_buckets", {})
            for raw_item in single.get("duration_items") or []:
                item = dict(raw_item)
                item["idx"] = next_idx
                next_idx += 1
                duration_items.append(item)
                duration = int(item["duration"])
                index.setdefault("durations", []).append(
                    (duration, str(item.get("name") or "")))
                buckets.setdefault(duration, []).append(item)
            index["next_duration_idx"] = next_idx
            if fid:
                confirmed_fids.add(fid)
            self._dedup_cache[cid] = (built_at, index)

    async def _resolve_upload_cid(self, task: Task) -> str:
        """在按模特串行的临界区内复查数据库，只在仍未绑定时查找/建目录。"""
        current = str(task.model_cloud_cid or "").strip()
        if current:
            return current
        normalized_name = " ".join(str(task.model_name or "").split()).casefold()
        key = (task.model_id, normalized_name)
        lock = self._model_cid_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cid = ""
            async with self._session_factory() as session:
                model = None
                if task.model_id is not None:
                    model = await session.get(Model, task.model_id)
                else:
                    matches = (await session.execute(
                        select(Model).where(Model.name == task.model_name)
                    )).scalars().all()
                    if len(matches) == 1:
                        model = matches[0]
                if model is not None:
                    cid = str(model.cloud_cid or "").strip()
            if not cid:
                cid = await self._cloud.ensure_model_dir(task.model_name)
                await self._bind_model_cid(task.model_id, task.model_name, cid)
            task.model_cloud_cid = cid
            return cid

    async def _confirm_remote_upload(
        self,
        cid: str,
        viewkey: str,
        remote_name: str,
        expected_size: int,
        file_id: str,
    ) -> dict:
        """用上传返回的文件 ID 复核 CID、文件名、大小和强 viewkey。"""
        last_error: Exception | None = None
        for delay in self._upload_verify_delays:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                info = await self._cloud.get_file_info(file_id)
            except Cloud115Error as e:
                last_error = e
                continue
            if info is None:
                continue
            actual_cid = str(info.get("cid") or "")
            actual_name = str(info.get("n") or info.get("file_name") or "")
            try:
                actual_size = int(info.get("s") or info.get("file_size") or 0)
            except (TypeError, ValueError):
                actual_size = 0
            index = dedup.build_cloud_index([info])
            mismatches = []
            if actual_cid != str(cid):
                mismatches.append(f"CID={actual_cid or '空'}")
            if actual_name != remote_name:
                mismatches.append(f"文件名={actual_name or '空'}")
            if actual_size != int(expected_size):
                mismatches.append(f"大小={actual_size}")
            if str(viewkey or "").casefold() not in index["viewkeys"]:
                mismatches.append("缺少强viewkey")
            if mismatches:
                raise UploadConfirmationError(
                    f"115 文件ID {file_id} 落点校验失败：{'、'.join(mismatches)}")
            return info
        detail = f"；最后错误：{last_error}" if last_error else ""
        raise UploadConfirmationError(
            f"115 已返回文件ID {file_id}，但权威文件记录仍不可读{detail}；"
            "任务已保留本地文件，且不会自动重复上传")

    async def _run_upload(self, task_id: int):
        async with self._upload_sem:
            if self._paused:
                self._running.pop(task_id, None)
                return
            cid = ""
            try:
                t = await self._set_status(task_id, status="running", progress=0.0,
                                           error="", _return_task=True)
                if t is None:
                    return
                # 二次去重：扫描115该模特文件夹（带短期缓存，避免同模特重复全量扫描）
                cid = await self._resolve_upload_cid(t)
                index = await self._get_dedup_index(cid)
                status, _ = dedup.match_video(t.viewkey, t.duration, index)
                if status == "exists":
                    await self._set_status(task_id, status="skipped",
                                           error="115已存在(二次去重)",
                                           auto_retry_count=0, next_retry_at=None,
                                           remote_missing_count=0,
                                           _task_type="upload")
                    # 二次去重命中：115 已有该视频，同样回写榜单
                    await self._mark_video_exists(t.viewkey, t.model_id, t.model_name)
                    # 已存在：本地文件可删，无需保留
                    await asyncio.to_thread(self._cleanup_local, t.local_path)
                    return
                # 上传：本地用 ASCII 文件名（避免 ffmpeg 编码问题），上传到 115
                # 时改成 "标题 [viewkey].mp4" 保留中文标题 + 强匹配去重锚
                remote_name = build_remote_filename(t.title, t.viewkey)

                async def on_up_progress(sent, total):
                    if total > 0:
                        pct = round(min(99.0, sent / total * 100), 1)
                        await self._set_progress(
                            task_id,
                            {"progress": pct},
                            "upload",
                        )

                local_size = await asyncio.to_thread(os.path.getsize, t.local_path)
                upload_result = await self._cloud.upload_file(
                    t.local_path, cid, remote_name, on_progress=on_up_progress)
                # 目录的视频筛选有媒体索引延迟，不能拿它确认上传。这里改用上传响应中的
                # 文件 ID 查询权威记录；CID、文件名、大小和 viewkey 全部匹配才算完成。
                confirmed_info = await self._confirm_remote_upload(
                    cid,
                    t.viewkey,
                    remote_name,
                    local_size,
                    upload_result["file_id"],
                )
                # file-ID 权威确认后，直接把新强 viewkey 合入完整目录缓存；正常
                # 连续上传不再为每个文件重扫同一 CID。确认不确定时仍由异常分支失效。
                try:
                    await self._merge_confirmed_cloud_file(cid, confirmed_info)
                except Exception:
                    # 缓存只是性能层；权威 file-ID 已确认，内存索引维护失败不能
                    # 把成功上传倒置为自动重传。失效后让下一任务完整重扫。
                    self._dedup_cache.pop(cid, None)
                    logger.exception("增量维护115去重缓存失败 cid=%s", cid)
                await self._set_status(task_id, status="completed", progress=100.0,
                                       error="", auto_retry_count=0,
                                       next_retry_at=None, remote_missing_count=0,
                                       _task_type="upload")
                self._upload_gate.record_success()
                self._spawn_bg(self._notifier.notify_upload_done(t.model_name, t.title))
                # 上传成功：该视频已进 115。回写榜单——把对应 Video 标 exists 并刷新
                # Model 计数，让前端卡片缺失数立即下降，无需手动重新同步。
                await self._mark_video_exists(t.viewkey, t.model_id, t.model_name)
                if t.model_id is not None:
                    try:
                        await self._inventory.record_binding(
                            t.model_id, cid, t.model_name,
                            session_factory=self._session_factory,
                        )
                    except Exception:
                        # 115 上传已经由文件 ID 权威确认成功。目录索引属于附属回写，
                        # 失败只能留下日志并等待下一次刷新修复，不能把上传倒置为失败。
                        logger.exception(
                            "上传成功后更新115模特索引失败 model_id=%s cid=%s",
                            t.model_id, cid,
                        )
                # 删除本地文件释放磁盘；去重缓存已由权威记录增量更新。
                await asyncio.to_thread(self._cleanup_local, t.local_path)
            except asyncio.CancelledError:
                if cid:
                    self._dedup_cache.pop(cid, None)
                await self._set_status(task_id, status="paused", _task_type="upload")
                raise
            except Exception as e:
                if cid:
                    # 上传可能已在服务端落盘后才返回错误；重试前必须重新扫描，
                    # 不能继续复用失败前建立的 60 秒缓存。
                    self._dedup_cache.pop(cid, None)
                if (isinstance(e, UploadTransferError)
                        and await self._upload_gate.confirm_denied()):
                    # 不是这个文件的问题：退回队列、不计自动重试次数，等闸门重开。
                    await self._set_status(
                        task_id, status="queued", progress=0.0,
                        error=UPLOAD_GATE_WAITING_TEXT, next_retry_at=None,
                        _task_type="upload")
                    return
                error = _error_text(e)
                confirmation_failed = isinstance(e, UploadConfirmationError)
                terminal_fields = (
                    {
                        "auto_retry_count": self._auto_requeue_max,
                        "next_retry_at": None,
                    }
                    if confirmation_failed else {}
                )
                await self._set_status(
                    task_id,
                    status="failed",
                    error=error[:500],
                    _task_type="upload",
                    **terminal_fields,
                )
                if not confirmation_failed:
                    self._schedule_auto_requeue(task_id, error[:200])
            finally:
                self._running.pop(task_id, None)
                self._request_pump()

    async def _bind_model_cid(self, model_id, model_name, cid):
        return await self._inventory.bind_model_cid(model_id, model_name, cid)

    async def _mark_video_exists(self, viewkey, model_id, model_name):
        return await self._inventory.mark_video_exists(viewkey, model_id, model_name)

    def _cleanup_local(self, local_path: str | None):
        """删除上传完成的本地文件，释放磁盘。失败静默（不影响任务状态）。"""
        if not local_path:
            return
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except OSError:
            pass
        self._downloader.cleanup_partials(local_path)

    def _cleanup_incomplete_download(self, task: Task):
        """删除任务时才清理原生 HLS 续传数据；暂停和失败均保留。"""
        if task.task_type != "download" or task.status == "completed":
            return
        output_path = task.local_path or _output_path_for_task(task)
        self._downloader.cleanup_partials(output_path)

    # ---------- 批量控制 ----------
    async def start_all(self, task_type: str | None = None):
        # 仅"全部继续"才清全局暂停标志。若用户在全局暂停后只继续 upload，下载仍应保持
        # 全局暂停，避免后续新入队下载任务立即开跑。
        if task_type is None:
            self._paused = False
        async with self._session_factory() as s:
            stmt = (update(Task).where(Task.status == "paused")
                    .where((Task.phase != "waiting_download") | Task.phase.is_(None))
                    .values(
                        status="queued",
                        phase=case(
                            (Task.phase.like("paused_source_cooldown:%"),
                             func.replace(Task.phase, "paused_source_cooldown:", "source_cooldown:")),
                            (
                                Task.phase.like("paused_quality_resume:%"),
                                func.replace(
                                    Task.phase,
                                    "paused_quality_resume:",
                                    "queued_quality_resume:",
                                ),
                            ),
                            (
                                Task.phase.like("paused_quality_retry:%"),
                                func.replace(
                                    Task.phase,
                                    "paused_quality_retry:",
                                    "queued_quality_retry:",
                                ),
                            ),
                            (Task.phase == "paused_clean_resume", "queued_clean_resume"),
                            (Task.phase == "paused_clean_retry", "queued_clean_retry"),
                            (Task.phase == "paused_source_retry", "queued_source_retry"),
                            (Task.phase == "paused_source_cooldown", "source_cooldown"),
                            else_="",
                        ),
                        speed="",
                        speed_bps=0.0,
                    ))
            if task_type:
                stmt = stmt.where(Task.task_type == task_type)
            await s.execute(stmt)
            await s.commit()
        await self._pump()
        if not self._paused and task_type in (None, "upload"):
            await self._schedule_pending_upload_retries()

    async def pause_all(self, task_type: str | None = None):
        # 取消运行中的下载子进程树并保留续传数据。上传是 p115 同步调用，无法中断：
        # 正在跑的上传不强行标 paused（否则状态与实际不符），让它自然跑完；
        # 只暂停 queued 的上传。
        active_tasks = dict(self._download_retry_waiters)
        active_tasks.update(self._running)
        running_ids = list(active_tasks.keys())
        types_by_id: dict[int, str] = {}
        if running_ids:
            async with self._session_factory() as s:
                rows = (await s.execute(
                    select(Task.id, Task.task_type).where(Task.id.in_(running_ids))
                )).all()
            types_by_id = {rid: rtype for rid, rtype in rows}

        # 先收集可中断下载；运行中的上传跳过（无法中断）。必须先把数据库队列
        # 标成 paused，再取消并等待协程收尾，否则 finally 触发的 _pump 可能在
        # 暂停接口返回前补开新任务，调用方紧接着退出应用时也可能留下孤儿 worker。
        running_uploads: list[int] = []
        downloads_to_cancel: list[tuple[int, asyncio.Task]] = []
        for tid, task in list(active_tasks.items()):
            ttype = types_by_id.get(tid)
            if task_type and ttype != task_type:
                continue
            if ttype == "upload":
                running_uploads.append(tid)
                continue   # 上传无法中断，保留 running
            downloads_to_cancel.append((tid, task))

        self._download_pause_requests.update(
            tid for tid, _task in downloads_to_cancel)

        if not task_type:
            self._paused = True

        async with self._session_factory() as s:
            stmt = (update(Task).where(Task.status.in_(["running", "queued"]))
                    .values(
                        status="paused",
                        phase=case(
                            (Task.phase.like("source_cooldown:%"),
                             func.replace(Task.phase, "source_cooldown:", "paused_source_cooldown:")),
                            (
                                Task.phase.like("queued_quality_resume:%"),
                                func.replace(
                                    Task.phase,
                                    "queued_quality_resume:",
                                    "paused_quality_resume:",
                                ),
                            ),
                            (
                                Task.phase.like("queued_quality_retry:%"),
                                func.replace(
                                    Task.phase,
                                    "queued_quality_retry:",
                                    "paused_quality_retry:",
                                ),
                            ),
                            (Task.phase == "queued_clean_resume", "paused_clean_resume"),
                            (Task.phase == "queued_clean_retry", "paused_clean_retry"),
                            (Task.phase == "queued_source_retry", "paused_source_retry"),
                            (Task.phase == "source_cooldown", "paused_source_cooldown"),
                            else_="",
                        ),
                        speed="",
                        speed_bps=0.0,
                    ))
            if task_type:
                stmt = stmt.where(Task.task_type == task_type)
            # 不把正在跑、无法中断的上传标成 paused
            if running_uploads:
                stmt = stmt.where(Task.id.notin_(running_uploads))
            await s.execute(stmt)
            await s.commit()

        cancelled_tasks: list[asyncio.Task] = []
        for tid, task in downloads_to_cancel:
            # 先把取消投递给协程，再同步回收 worker。Windows 进程终止码可能比
            # CancelledError 更早送达，_download_pause_requests 会兜底按 paused 收口。
            task.cancel()
            cancelled_tasks.append(task)
            try:
                self._downloader.cancel(str(tid))
            except Exception:
                # 协程取消后的 finally 仍会再次终止进程树，不能因单个 OS
                # 回收异常阻断其余任务和数据库状态收尾。
                pass
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)
        if task_type in (None, "download") and self._source_wakeup:
            self._source_wakeup.cancel()
            self._source_wakeup = None
        if task_type in (None, "download") and self._media_tools_wakeup:
            self._media_tools_wakeup.cancel()
            self._media_tools_wakeup = None
        # 处理“取消时 worker 刚完成注册”的极窄竞态；第二次同步清扫应为空操作，
        # 但能保证 API 返回时对应进程树已经不存在。
        for tid, _task in downloads_to_cancel:
            try:
                self._downloader.cancel(str(tid))
            except Exception:
                pass
            finally:
                self._download_pause_requests.discard(tid)
        self._update_keep_awake()

    async def resume_all(self, task_type: str | None = None):
        await self.start_all(task_type)

    async def retry_task(self, task_id: int) -> bool:
        """重试单个失败/跳过的任务：重置为 queued 并重新调度。"""
        waiter = self._upload_retry_waiters.pop(task_id, None)
        if waiter:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        async with self._session_factory() as s:
            t = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
            if not t or t.status not in ("failed", "skipped"):
                return False
            recovery = (
                await asyncio.to_thread(_failure_metadata, t)
                if t.status == "failed" else {})
            t.status = "queued"
            t.error = ""
            t.speed = ""
            t.speed_bps = 0.0
            action = str(recovery.get("recovery_action") or "")
            t.phase = (
                "queued_clean_retry" if action == "retry_clean"
                else "queued_source_retry" if action == "retry_source"
                else ""
            )
            if action == "retry_clean":
                t.progress = 0.0
                t.downloaded_bytes = 0
                t.total_bytes = 0
                t.last_progress_at = None
            t.attempt = 0
            t.auto_retry_count = 0
            t.next_retry_at = None
            t.remote_missing_count = 0
            await s.commit()
            await self._notify(t)
        self._request_pump()
        return True

    async def reconcile_failed_downloads(
        self,
        *,
        apply: bool = False,
        task_ids: list[int] | None = None,
    ) -> dict[str, object]:
        """离线校验“已有成品但状态失败”的下载，并可原子补记完成。"""
        async with self._session_factory() as s:
            stmt = select(Task).where(
                Task.task_type == "download",
                Task.status == "failed",
            )
            if task_ids:
                stmt = stmt.where(Task.id.in_(task_ids))
            rows = (await s.execute(stmt)).scalars().all()
        recovery_by_id = await asyncio.to_thread(lambda: {
            task.id: _failure_metadata(task) for task in rows
        })
        candidates = [
            task for task in rows
            if recovery_by_id[task.id]["recovery_action"] == "reconcile_final"
        ]
        report: dict[str, object] = {
            "candidates": len(candidates),
            "validated": 0,
            "reconciled": 0,
            "invalid": 0,
            "details": [],
        }
        details: list[dict[str, object]] = report["details"]  # type: ignore[assignment]
        for task in candidates:
            output_path = _output_path_for_task(task)
            try:
                if apply:
                    lock_ready = await asyncio.to_thread(
                        cleanup_stale_output_lock,
                        output_path,
                        str(task.id),
                    )
                    if not lock_ready:
                        raise DownloadProcessingError(
                            "输出锁仍有存活属主或任务身份不匹配")
                evidence = await asyncio.to_thread(
                    validate_completed_artifact, output_path)
            except Exception as error:
                report["invalid"] = int(report["invalid"]) + 1
                details.append({
                    "task_id": task.id,
                    "viewkey": task.viewkey,
                    "result": "invalid",
                    "error": _error_text(error)[:300],
                })
                continue

            report["validated"] = int(report["validated"]) + 1
            if not apply:
                details.append({
                    "task_id": task.id,
                    "viewkey": task.viewkey,
                    "result": "valid",
                    "size": int(evidence["size"]),
                })
                continue

            async with self._session_factory() as s:
                current = (await s.execute(select(Task).where(
                    Task.id == task.id,
                    Task.task_type == "download",
                    Task.status == "failed",
                ))).scalar_one_or_none()
                if current is None:
                    continue
                current.status = "completed"
                current.phase = "completed"
                current.progress = 100.0
                current.speed = ""
                current.speed_bps = 0.0
                current.downloaded_bytes = int(evidence["size"])
                current.total_bytes = int(evidence["size"])
                current.local_path = output_path
                current.error = ""
                await s.commit()
                await self._notify(current)
            report["reconciled"] = int(report["reconciled"]) + 1
            details.append({
                "task_id": task.id,
                "viewkey": task.viewkey,
                "result": "reconciled",
                "size": int(evidence["size"]),
            })
            await self._create_upload_task(task.id)
        return report

    async def scrub_stored_task_errors(self, *, apply: bool = False) -> dict[str, object]:
        """检查并可脱敏历史任务错误；响应绝不回传原始错误内容。"""
        async with self._session_factory() as s:
            rows = (await s.execute(select(Task).where(Task.error != ""))).scalars().all()
            changed = []
            for task in rows:
                redacted = redact_sensitive_error_text(task.error)
                if redacted == task.error:
                    continue
                changed.append(task.id)
                if apply:
                    task.error = redacted[:500]
            if apply and changed:
                await s.commit()
        return {
            "candidates": len(changed),
            "scrubbed": len(changed) if apply else 0,
            "task_ids": changed[:100],
        }

    async def retry_failed(self, task_type: str | None = None) -> dict[str, int]:
        """先对账已有成品，再按明确恢复动作批量重排其它失败。"""
        reconciled = 0
        if task_type in (None, "download"):
            reconcile_report = await self.reconcile_failed_downloads(apply=True)
            reconciled = int(reconcile_report["reconciled"])
        async with self._session_factory() as s:
            rows_stmt = select(Task).where(Task.status == "failed")
            if task_type:
                rows_stmt = rows_stmt.where(Task.task_type == task_type)
            rows = (await s.execute(rows_stmt)).scalars().all()
            recovery_by_id = await asyncio.to_thread(lambda: {
                task.id: _failure_metadata(task) for task in rows
            })
            retryable = []
            for task in rows:
                recovery = recovery_by_id[task.id]
                action = str(recovery["recovery_action"])
                if not recovery["bulk_recoverable"] or action == "reconcile_final":
                    continue
                task.status = "queued"
                task.error = ""
                task.speed = ""
                task.speed_bps = 0.0
                task.phase = (
                    "queued_clean_retry" if action == "retry_clean"
                    else "queued_source_retry" if action == "retry_source"
                    else ""
                )
                if action == "retry_clean":
                    task.progress = 0.0
                    task.downloaded_bytes = 0
                    task.total_bytes = 0
                    task.last_progress_at = None
                elif task.task_type == "upload":
                    # 上传不能续传，重新排队后从 0 开始；保留旧进度会让排队任务看起来在传。
                    task.progress = 0.0
                task.attempt = 0
                task.auto_retry_count = 0
                task.next_retry_at = None
                task.remote_missing_count = 0
                retryable.append(task)
            task_ids = [task.id for task in retryable]
            retried = len(task_ids)
            await s.commit()
        cancelled = []
        for task_id in task_ids:
            waiter = self._upload_retry_waiters.pop(task_id, None)
            if waiter:
                waiter.cancel()
                cancelled.append(waiter)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        if retried:
            self._request_pump()
        return {
            "retried": retried,
            "reconciled": reconciled,
            "skipped_non_retryable": len(rows) - len(retryable),
        }

    async def _cancel_deletable_tasks(self, rows: list[Task]) -> None:
        """取消待删下载协程并等待其状态收尾；调用方已排除运行中上传。"""
        cancelled: list[asyncio.Task] = []
        download_ids: list[int] = []
        for row in rows:
            tid = row.id
            running = self._running.pop(tid, None)
            waiter = self._download_retry_waiters.pop(tid, None)
            upload_waiter = self._upload_retry_waiters.pop(tid, None)
            if row.task_type == "download":
                download_ids.append(tid)
                try:
                    self._downloader.cancel(str(tid))
                except Exception:
                    pass
            for task in {running, waiter, upload_waiter} - {None}:
                task.cancel()
                cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        # 覆盖“取消时 worker 刚完成注册”的窄竞态。
        for tid in download_ids:
            try:
                self._downloader.cancel(str(tid))
            except Exception:
                pass

    def _partition_deletable(
        self, rows: list[Task]
    ) -> tuple[list[Task], list[Task]]:
        """运行中上传不可中断，必须保留任务行直到实际上传结束。"""
        deletable: list[Task] = []
        running_uploads: list[Task] = []
        for row in rows:
            if row.task_type == "upload" and (
                row.status == "running" or row.id in self._running
            ):
                running_uploads.append(row)
            else:
                deletable.append(row)
        return deletable, running_uploads

    async def delete_task(self, task_id: int) -> bool:
        # 与 pump 串行化“判定可删→取消→权威重读→删除”。否则 queued upload
        # 可能在第一次判定后被 pump 启动，随后任务行却被删除，而同步上传仍在115写入。
        async with self._pump_lock:
            async with self._session_factory() as s:
                row = (await s.execute(
                    select(Task).where(Task.id == task_id)
                )).scalar_one_or_none()
            if row is None:
                return True
            deletable, running_uploads = self._partition_deletable([row])
            if running_uploads:
                return False
            await self._cancel_deletable_tasks(deletable)
            async with self._session_factory() as s:
                current = (await s.execute(
                    select(Task).where(Task.id == task_id)
                )).scalar_one_or_none()
            if current is None:
                return True
            current_deletable, current_running = self._partition_deletable([current])
            if current_running:
                return False
            await asyncio.to_thread(
                self._cleanup_incomplete_download, current_deletable[0])
            async with self._session_factory() as s:
                result = await s.execute(delete(Task).where(
                    Task.id == task_id,
                    # 最后一道 SQL 边界：外部状态修改也不能删除 running upload。
                    ~and_(Task.task_type == "upload", Task.status == "running"),
                ))
                await s.commit()
            return int(result.rowcount or 0) > 0

    async def delete_tasks(self, task_ids: list[int]) -> dict[str, int]:
        """批量删除；运行中上传保留并在返回值中明确报告。"""
        unique_ids = list(dict.fromkeys(task_ids))
        if not unique_ids:
            return {"deleted": 0, "skipped_running_uploads": 0}
        async with self._pump_lock:
            async with self._session_factory() as s:
                rows = (await s.execute(
                    select(Task).where(Task.id.in_(unique_ids))
                )).scalars().all()
            deletable, running_uploads = self._partition_deletable(rows)
            protected_ids = {row.id for row in running_uploads}
            await self._cancel_deletable_tasks(deletable)
            delete_ids = [row.id for row in deletable]
            deleted = 0
            if delete_ids:
                # 取消协程会改变 DB 状态；删除前必须再次按 running-upload 边界分区。
                async with self._session_factory() as s:
                    current_rows = (await s.execute(
                        select(Task).where(Task.id.in_(delete_ids))
                    )).scalars().all()
                current_deletable, current_running = self._partition_deletable(
                    current_rows)
                protected_ids.update(row.id for row in current_running)
                await asyncio.to_thread(
                    self._cleanup_task_rows, current_deletable)
                current_ids = [row.id for row in current_deletable]
                if current_ids:
                    async with self._session_factory() as s:
                        result = await s.execute(delete(Task).where(
                            Task.id.in_(current_ids),
                            ~and_(
                                Task.task_type == "upload",
                                Task.status == "running",
                            ),
                        ))
                        await s.commit()
                        deleted = max(0, int(result.rowcount or 0))
        return {
            "deleted": deleted,
            "skipped_running_uploads": len(protected_ids),
        }

    async def cancel_candidate_promote_tasks(self, candidate_id: int) -> dict[str, int]:
        """只撤销该次追加产生的可逆任务；已上传或运行中上传绝不伪装成已取消。"""
        # 固定锁顺序：先任务创建锁、再 pump 锁。既阻止下载完成边沿补建上传，
        # 也阻止 queued upload 在可撤销性判定之后才被调度启动。
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._task_create_lock)
            await stack.enter_async_context(self._pump_lock)
            async with self._session_factory() as session:
                rows = (await session.execute(select(Task).where(
                    Task.origin == "candidate_promote",
                    Task.origin_candidate_id == candidate_id,
                ))).scalars().all()
            if not rows:
                return {
                    "cancelled": 0,
                    "preserved_running_uploads": 0,
                    "already_uploaded": 0,
                }

            protected_viewkeys = {
                row.viewkey for row in rows
                if row.task_type == "upload" and (
                    row.status in ("running", "completed", "skipped")
                    or row.id in self._running
                )
            }
            cancellable = [
                row for row in rows if row.viewkey not in protected_viewkeys
            ]
            protected = [row for row in rows if row.viewkey in protected_viewkeys]
            # 运行下载先被终止并等待收尾；锁同时阻止完成边沿新建上传任务。
            await self._cancel_deletable_tasks(cancellable)
            delete_ids = [row.id for row in cancellable]
            cancelled = 0
            if delete_ids:
                async with self._session_factory() as session:
                    current_rows = (await session.execute(select(Task).where(
                        Task.id.in_(delete_ids)
                    ))).scalars().all()
                    await asyncio.to_thread(
                        self._cleanup_candidate_task_rows,
                        current_rows,
                    )
                    for row in current_rows:
                        await session.delete(row)
                        cancelled += 1
                    await session.commit()
        self._request_pump()
        return {
            "cancelled": cancelled,
            "preserved_running_uploads": len({
                row.viewkey for row in protected
                if row.task_type == "upload" and row.status == "running"
            }),
            "already_uploaded": len({
                row.viewkey for row in protected
                if row.task_type == "upload" and row.status in ("completed", "skipped")
            }),
        }

    async def delete_all_tasks(self, task_type: str | None = None) -> dict[str, int]:
        """分批删除全部或指定类型；绝不波及过滤条件之外的协程。

        每批都重新读取并复核 running upload 边界。这样既不会把数万行 ORM
        一次载入内存，也不会构造超过 SQLite 变量上限的巨大 ``IN`` 子句。
        """
        deleted = 0
        skipped_running_uploads = 0
        last_id = 0
        batch_size = 750

        # 阻止调度器在“选中 queued upload → 删除”之间启动它；已经运行的上传
        # 不受该锁影响，并在每一批权威重读时继续被保留。
        async with self._pump_lock:
            # 固定本次请求的高水位。请求开始后新建的任务不属于这次快照，也不会
            # 让持续生产者把删除循环拖成无界操作。
            async with self._session_factory() as s:
                highwater_query = select(func.max(Task.id))
                if task_type:
                    highwater_query = highwater_query.where(
                        Task.task_type == task_type)
                highwater = int((await s.execute(
                    highwater_query
                )).scalar_one_or_none() or 0)
            while True:
                async with self._session_factory() as s:
                    query = select(
                        Task.id,
                        Task.task_type,
                        Task.status,
                        Task.viewkey,
                        Task.model_name,
                        Task.title,
                        Task.artifact_scope,
                        Task.local_path,
                    ).where(
                        Task.id > last_id,
                        Task.id <= highwater,
                    )
                    if task_type:
                        query = query.where(Task.task_type == task_type)
                    raw_rows = (await s.execute(
                        query.order_by(Task.id).limit(batch_size)
                    )).mappings().all()
                if not raw_rows:
                    break

                last_id = max(int(row["id"]) for row in raw_rows)
                snapshot = [SimpleNamespace(**dict(row)) for row in raw_rows]
                deletable, _ = self._partition_deletable(snapshot)
                await self._cancel_deletable_tasks(deletable)

                batch_ids = [int(row["id"]) for row in raw_rows]
                # 取消协程可能在收尾时改变状态；删除前必须重读，而不是沿用上面的
                # 快照。750 个参数始终远低于 SQLite 的最小变量上限。
                async with self._session_factory() as s:
                    current_raw = (await s.execute(select(
                        Task.id,
                        Task.task_type,
                        Task.status,
                        Task.viewkey,
                        Task.model_name,
                        Task.title,
                        Task.artifact_scope,
                        Task.local_path,
                    ).where(Task.id.in_(batch_ids)))).mappings().all()
                current = [SimpleNamespace(**dict(row)) for row in current_raw]
                current_deletable, current_running = self._partition_deletable(current)
                skipped_running_uploads += len(current_running)
                if not current_deletable:
                    continue

                # glob/stat/unlink 都可能命中慢盘，不能在 FastAPI 事件循环或数据库
                # 事务内执行。清理完成后才集合删除，保留断点清理的原有边界。
                await asyncio.to_thread(
                    self._cleanup_task_rows,
                    current_deletable,
                )
                delete_ids = [row.id for row in current_deletable]
                async with self._session_factory() as s:
                    result = await s.execute(
                        delete(Task).where(Task.id.in_(delete_ids))
                    )
                    await s.commit()
                    deleted += max(0, int(result.rowcount or 0))

        return {
            "deleted": deleted,
            "skipped_running_uploads": skipped_running_uploads,
        }

    def _cleanup_task_rows(self, rows: list[SimpleNamespace]) -> None:
        """在线程中清理一小批待删任务的本地断点。"""
        for row in rows:
            self._cleanup_incomplete_download(row)

    def _cleanup_candidate_task_rows(self, rows: list[Task]) -> None:
        """在线程中清理一次候选追加产生的本地成品或断点。"""
        for row in rows:
            if row.task_type == "download" and row.status == "completed":
                self._cleanup_local(row.local_path or _output_path_for_task(row))
            else:
                self._cleanup_incomplete_download(row)

    async def list_tasks(self, task_type=None, limit=None, offset=0, cursor=None, **filters):
        return await self._queries.list_tasks(task_type, limit, offset, cursor, **filters)

    async def count_matching_tasks(self, task_type=None, **filters):
        return await self._queries.count_matching(task_type, **filters)

    async def task_today(self, task_type):
        return await self._queries.today_completed(task_type)

    async def task_counts(self, task_type=None):
        return await self._queries.task_counts(task_type)

    async def restore(self):
        """重启后重排 stale running，清掉旧速度但保留原生 HLS 续传数据。"""
        async with self._session_factory() as s:
            legacy_rows = (await s.execute(
                select(Task).where(
                    Task.status == "running", Task.task_type == "download",
                    (Task.phase == "") | (Task.phase.is_(None))))
            ).scalars().all()
            # 仅迁移旧版 ffmpeg 主 .part；新版原生 HLS 使用独立 native 临时名。
            def _remove_legacy_parts() -> None:
                for task in legacy_rows:
                    legacy_part = _output_path_for_task(task) + ".part"
                    try:
                        if os.path.exists(legacy_part):
                            os.remove(legacy_part)
                    except OSError:
                        pass

            await asyncio.to_thread(_remove_legacy_parts)
            await s.execute(
                update(Task).where(Task.status == "running").values(
                    status="queued", phase="", speed="", speed_bps=0.0,
                    attempt=0))
            await s.commit()
        reconcile_report = await self.reconcile_failed_downloads(apply=True)
        if reconcile_report["reconciled"]:
            logger.info(
                "启动对账恢复 %s 个已校验下载成品",
                reconcile_report["reconciled"],
            )
        await self._pump()
        await self._schedule_pending_upload_retries()

    async def shutdown(self):
        """关闭时终止下载；不可中断的同步上传等待其真实完成。"""
        self._paused = True
        if self._source_wakeup:
            self._source_wakeup.cancel()
            self._source_wakeup = None
        if self._media_tools_wakeup:
            self._media_tools_wakeup.cancel()
            self._media_tools_wakeup = None
        if self._link_alert_task is not None:
            self._link_alert_task.cancel()
            self._link_alert_task = None
        await self._link_health.close()
        await self._upload_gate.close()
        self._download_throttle.close()
        self._keep_awake.release()
        if self._window_wakeup is not None:
            self._window_wakeup.cancel()
            self._window_wakeup = None
        retry_waiters = list(self._upload_retry_waiters.values())
        self._upload_retry_waiters.clear()
        for waiter in retry_waiters:
            waiter.cancel()
        if retry_waiters:
            await asyncio.gather(*retry_waiters, return_exceptions=True)
        active_tasks = dict(self._download_retry_waiters)
        active_tasks.update(self._running)
        tasks = list(active_tasks.items())
        types_by_id: dict[int, str] = {}
        if tasks:
            async with self._session_factory() as s:
                rows = (await s.execute(
                    select(Task.id, Task.task_type).where(
                        Task.id.in_([task_id for task_id, _ in tasks])
                    )
                )).all()
            types_by_id = {task_id: task_type for task_id, task_type in rows}
        for task_id, task in tasks:
            if types_by_id.get(task_id) == "upload":
                continue
            try:
                self._downloader.cancel(str(task_id))
            except Exception:
                pass
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        self._running.clear()
        self._download_retry_waiters.clear()
        await asyncio.to_thread(self._downloader.shutdown)
        background = [
            task for task in self._bg_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._bg_tasks.clear()
        self._batch_debounce.clear()


manager = TaskManager()
