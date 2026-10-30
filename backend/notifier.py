"""Bark 推送通知。

无人值守巡查的核心补全：把巡查结果、任务批次完成、错误异常主动推送到用户手机，
避免"人不在页面就完全无感知"。设计原则：

  - 复用 httpx，不新增依赖；配置走既有 RuntimeSetting 表，不改数据库 schema。
  - 推送失败绝不影响主流程——push 只 logger.warning，永不向上抛出。
  - 错误类通知按场景节流，避免磁盘不足/掉登录等状况每次巡查或 pump 都轰炸手机。

Bark URL 已含 device_key（形如 https://api.day.app/<device_key>），是敏感信息：
status() 只回显是否已配置与末尾数位，完整 URL 不进日志、不回前端。
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .config import settings
from .database import async_session, RuntimeSetting

logger = logging.getLogger(__name__)

# RuntimeSetting 持久化键
_SETTING_ENABLED = "notify.bark.enabled"
_SETTING_URL = "notify.bark.url"
_SETTING_EVENT_PREFIX = "notify.event."

# 事件标识；顺序即前端展示顺序。
EVENT_PATROL_SUMMARY = "patrol_summary"
EVENT_NEW_MISSING = "new_missing"
EVENT_BATCH_DONE = "batch_done"
EVENT_DOWNLOAD_DONE = "download_done"
EVENT_UPLOAD_DONE = "upload_done"
EVENT_ERROR = "error"
EVENT_DAILY_DIGEST = "daily_digest"
_EVENT_KEYS = (
    EVENT_PATROL_SUMMARY,
    EVENT_NEW_MISSING,
    EVENT_BATCH_DONE,
    EVENT_DOWNLOAD_DONE,
    EVENT_UPLOAD_DONE,
    EVENT_ERROR,
    EVENT_DAILY_DIGEST,
)
_EVENT_DEFAULTS = {
    EVENT_PATROL_SUMMARY: True,
    EVENT_NEW_MISSING: True,
    EVENT_BATCH_DONE: True,
    # 单条完成通知容易在大批量同步时轰炸手机；新配置默认只发批次汇总。
    EVENT_DOWNLOAD_DONE: False,
    EVENT_UPLOAD_DONE: False,
    EVENT_ERROR: True,
    # 每天一条，比逐条完成推送安静，默认开启。
    EVENT_DAILY_DIGEST: True,
}

_PUSH_TIMEOUT = 8.0
_PUSH_MIN_INTERVAL = 0.35
# 连接阶段失败（请求未送达）时补发一次；等待略长于代理常见的断流窗口。
_PUSH_CONNECT_RETRY_DELAY = 20.0
_COMPLETION_DEBOUNCE_SECONDS = 5.0
_COMPLETION_SAMPLE_LIMIT = 3
# 同一错误场景的最小推送间隔（秒），防止重复轰炸。
_ERROR_THROTTLE_SECONDS = 300.0
_TRUTHY = {"1", "true", "yes", "on"}


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in _TRUTHY


class Notifier:
    """Bark 推送单例。事件方法内部自行判断开关，调用方无需关心是否启用。"""

    def __init__(self):
        self.enabled: bool = bool(getattr(settings, "notify_bark_enabled", False))
        self.bark_url: str = str(getattr(settings, "notify_bark_url", "") or "")
        self.event_flags: dict[str, bool] = dict(_EVENT_DEFAULTS)
        # 错误场景 -> 上次推送的单调时钟时间，用于节流。
        self._error_last_sent: dict[str, float] = {}
        # 所有 Bark 请求串行复用一个连接，并保留最小间隔，避免任务完成时并发轰炸。
        self._client: httpx.AsyncClient | None = None
        self._push_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._last_push_at: float | None = None
        self._push_min_interval = _PUSH_MIN_INTERVAL
        # 单条下载/上传完成事件会在短窗口内合并成一条摘要。
        self._completion_debounce_seconds = _COMPLETION_DEBOUNCE_SECONDS
        self._completion_counts = {
            EVENT_DOWNLOAD_DONE: 0,
            EVENT_UPLOAD_DONE: 0,
        }
        self._completion_samples: dict[str, list[str]] = {
            EVENT_DOWNLOAD_DONE: [],
            EVENT_UPLOAD_DONE: [],
        }
        self._completion_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()

    # ---------- 配置持久化 ----------
    async def restore_config(self):
        """从 RuntimeSetting 读取用户设置；缺省时沿用 .env 默认。"""
        self.enabled = bool(getattr(settings, "notify_bark_enabled", False))
        self.bark_url = str(getattr(settings, "notify_bark_url", "") or "")
        self.event_flags = dict(_EVENT_DEFAULTS)
        async with async_session() as session:
            rows = (await session.execute(select(RuntimeSetting))).scalars().all()
        values = {row.key: row.value for row in rows}
        if _SETTING_ENABLED in values:
            self.enabled = _as_bool(values[_SETTING_ENABLED])
        if _SETTING_URL in values:
            self.bark_url = str(values[_SETTING_URL] or "").strip()
        if self.bark_url:
            try:
                self.bark_url = self._validated_bark_url(self.bark_url)
            except ValueError:
                # 历史脏配置失效时关闭发送，但不把含 device_key 的原值写入日志。
                logger.warning("已保存的 Bark 地址无效，通知发送已暂停")
                self.bark_url = ""
        for key in _EVENT_KEYS:
            setting_key = f"{_SETTING_EVENT_PREFIX}{key}"
            if setting_key in values:
                self.event_flags[key] = _as_bool(values[setting_key])

    @staticmethod
    def _validated_bark_url(value: str) -> str:
        """接受官方或自建 Bark 的 HTTP(S) 地址，拒绝凭据和畸形 URL。"""
        url = str(value or "").strip()
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Bark 地址格式无效") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None and not 1 <= port <= 65535
        ):
            raise ValueError("Bark 地址必须是有效的 HTTP(S) URL，且不能包含账号密码")
        return url

    async def _persist_settings(self, values: dict[str, str]):
        """在一个事务中持久化整次页面修改，避免配置只保存一半。"""
        async with async_session() as session:
            for key, value in values.items():
                row = await session.get(RuntimeSetting, key)
                if row is None:
                    session.add(RuntimeSetting(key=key, value=value))
                else:
                    row.value = value
            await session.commit()

    async def set_config(
        self,
        *,
        enabled: bool,
        bark_url: str | None = None,
        events: dict[str, bool] | None = None,
    ):
        """更新并持久化配置。

        bark_url 为 None 表示保留已存地址（前端留空不修改）；events 缺省的键保持原值。
        """
        async with self._config_lock:
            next_enabled = bool(enabled)
            next_url = self.bark_url
            next_events = dict(self.event_flags)
            values = {
                _SETTING_ENABLED: "true" if next_enabled else "false",
            }
            if bark_url is not None:
                next_url = self._validated_bark_url(bark_url)
                values[_SETTING_URL] = next_url
            if events is not None:
                for key in _EVENT_KEYS:
                    if key in events:
                        next_events[key] = bool(events[key])
                        values[f"{_SETTING_EVENT_PREFIX}{key}"] = (
                            "true" if next_events[key] else "false")
            await self._persist_settings(values)
            self.enabled = next_enabled
            self.bark_url = next_url
            self.event_flags = next_events

    def status(self) -> dict:
        """给前端的状态快照；完整 URL 脱敏，只回显是否已配置与末尾数位。"""
        url = self.bark_url.strip()
        tail = url[-4:] if len(url) >= 4 else ""
        return {
            "enabled": self.enabled,
            "configured": bool(url),
            "url_hint": f"…{tail}" if tail else "",
            "events": dict(self.event_flags),
        }

    # ---------- 推送底层 ----------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=_PUSH_TIMEOUT,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=2,
                    max_keepalive_connections=1,
                ),
            )
        return self._client

    async def push(
        self,
        title: str,
        body: str,
        *,
        group: str = "PH115",
        level: str = "active",
    ) -> bool:
        """向 Bark 推送一条消息。任何失败只记录日志，返回 False，绝不抛出。"""
        url = self.bark_url.strip()
        if not url:
            return False
        # Bark 服务端路径长度有限；正文过长先行截断。
        payload = {
            "title": str(title)[:120],
            "body": str(body)[:900],
            "group": group,
            "level": level,
        }
        async with self._push_lock:
            if self._last_push_at is not None:
                elapsed = time.monotonic() - self._last_push_at
                delay = self._push_min_interval - elapsed
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                client = await self._get_client()
                try:
                    resp = await client.post(url, json=payload)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # 连接都没建立，Bark 不可能收到过这条消息，补发不会重复推送。
                    await asyncio.sleep(_PUSH_CONNECT_RETRY_DELAY)
                    client = await self._get_client()
                    resp = await client.post(url, json=payload)
                self._last_push_at = time.monotonic()
            except Exception as error:
                self._last_push_at = time.monotonic()
                # 不记录异常正文，避免某些 URL 异常把完整 device_key 带入日志。
                logger.warning(
                    "Bark 推送失败（%s）：%s", type(error).__name__, title)
                return False
            if resp.status_code >= 400:
                logger.warning("Bark 推送返回 HTTP %s：%s", resp.status_code, title)
                return False
            try:
                result = resp.json()
                code = int(result.get("code")) if isinstance(result, dict) else 200
            except (TypeError, ValueError):
                code = 200
            if not 200 <= code < 300:
                logger.warning("Bark 推送业务返回 %s：%s", code, title)
                return False
            return True

    async def push_test(self) -> bool:
        """前端"发送测试"按钮：无视事件开关，但仍需 URL 已配置。"""
        return await self.push("PH-115 测试通知", "如果你收到这条消息，Bark 通知已配置成功。")

    def _spawn_background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _schedule_completion(self, event: str, model_name: str, title: str):
        self._completion_counts[event] += 1
        samples = self._completion_samples[event]
        if len(samples) < _COMPLETION_SAMPLE_LIMIT:
            samples.append(f"{model_name}：{title}"[:180])
        task = self._completion_tasks.get(event)
        if task is not None and not task.done():
            return
        task = self._spawn_background(self._flush_completion(event))
        self._completion_tasks[event] = task

    async def _flush_completion(self, event: str):
        """把短时间内密集的单条完成事件合并成一条摘要。"""
        await asyncio.sleep(self._completion_debounce_seconds)
        current = asyncio.current_task()
        if self._completion_tasks.get(event) is current:
            self._completion_tasks.pop(event, None)
        count = self._completion_counts[event]
        samples = list(self._completion_samples[event])
        self._completion_counts[event] = 0
        self._completion_samples[event].clear()
        if count <= 0:
            return
        label = "下载" if event == EVENT_DOWNLOAD_DONE else "上传"
        parts = [f"本轮完成 {count} 个视频"]
        parts.extend(samples)
        if count > len(samples):
            parts.append(f"另有 {count - len(samples)} 个")
        await self.push(
            f"{label}完成 × {count}",
            "\n".join(parts),
            group=f"PH115-{label}",
            level="passive",
        )

    async def close(self):
        """关闭通知后台任务与连接池；应用退出时调用。"""
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._completion_tasks.clear()
        for event in self._completion_counts:
            self._completion_counts[event] = 0
            self._completion_samples[event].clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _should_send(self, event: str) -> bool:
        return self.enabled and self.event_flags.get(event, False) and bool(
            self.bark_url.strip())

    # ---------- 语义化事件 ----------
    async def notify_patrol_summary(self, result: dict):
        """每轮巡查结束的结果摘要。"""
        if not self._should_send(EVENT_PATROL_SUMMARY):
            return
        if result.get("skipped_reason"):
            # 跳过原因（如 115 未登录）由 notify_error 负责，避免重复。
            return
        parts = [
            f"范围 {result.get('bound_models', 0)}/{result.get('total_models', 0)}",
            f"扫描 {result.get('scanned', 0)}",
            f"缺失 {result.get('missing', 0)}",
        ]
        if result.get("newly_missing"):
            parts.append(f"新发现 {result['newly_missing']}")
        if result.get("queued_downloads"):
            parts.append(f"新建下载 {result['queued_downloads']}")
        if result.get("repaired"):
            parts.append(f"自动修复 {result['repaired']}")
        if result.get("errors"):
            parts.append(f"出错 {result['errors']}")
        await self.push("巡查完成", " · ".join(parts), group="PH115-巡查")

    async def notify_new_missing(
        self, model_name: str, count: int, total_missing: int
    ):
        """某模特发现新缺失视频。"""
        if not self._should_send(EVENT_NEW_MISSING) or count <= 0:
            return
        await self.push(
            f"{model_name} 有新视频",
            f"本轮新发现 {count} 个缺失视频，当前累计缺失 {total_missing} 个。",
            group="PH115-巡查",
        )

    async def notify_new_missing_summary(self, details: list[dict]):
        """把一轮巡查的新缺失合并为一条，避免按模特连续轰炸 Bark。"""
        if not self._should_send(EVENT_NEW_MISSING):
            return
        grouped: dict[str, dict[str, int]] = {}
        for detail in details or []:
            name = " ".join(str(
                detail.get("model_name") or detail.get("name") or "未知模特"
            ).strip().split()) or "未知模特"
            count = max(0, int(
                detail.get("count") or detail.get("newly_missing") or 0))
            if count <= 0:
                continue
            current = grouped.setdefault(name, {"count": 0, "total_missing": 0})
            current["count"] += count
            current["total_missing"] = max(
                current["total_missing"],
                max(0, int(detail.get("total_missing") or 0)),
            )
        if not grouped:
            return
        total = sum(item["count"] for item in grouped.values())
        top = sorted(
            grouped.items(), key=lambda item: (-item[1]["count"], item[0].casefold())
        )[:5]
        samples = []
        for name, item in top:
            suffix = (f"（累计 {item['total_missing']}）"
                      if item["total_missing"] else "")
            samples.append(f"{name} +{item['count']}{suffix}")
        remainder = len(grouped) - len(top)
        if remainder:
            samples.append(f"另有 {remainder} 位")
        await self.push(
            f"巡查新发现 {total} 个视频",
            f"影响 {len(grouped)} 位模特：" + "；".join(samples),
            group="PH115-巡查",
        )

    async def notify_batch_done(self, task_type: str, stats: dict):
        """某类型任务队列排空（一批任务全部结束）。"""
        if not self._should_send(EVENT_BATCH_DONE):
            return
        label = "下载" if task_type == "download" else "上传"
        completed = int(stats.get("completed", 0))
        failed = int(stats.get("failed", 0))
        skipped = int(stats.get("skipped", 0))
        parts = [f"完成 {completed}"]
        if skipped:
            parts.append(f"跳过 {skipped}")
        if failed:
            parts.append(f"失败 {failed}")
        await self.push(
            f"{label}队列已清空",
            " · ".join(parts),
            group="PH115-任务",
            level="active" if failed else "passive",
        )

    async def notify_download_done(self, model_name: str, title: str):
        """单个视频下载完成。"""
        if not self._should_send(EVENT_DOWNLOAD_DONE):
            return
        self._schedule_completion(EVENT_DOWNLOAD_DONE, model_name, title)

    async def notify_upload_done(self, model_name: str, title: str):
        """单个视频上传到 115 完成。"""
        if not self._should_send(EVENT_UPLOAD_DONE):
            return
        self._schedule_completion(EVENT_UPLOAD_DONE, model_name, title)

    async def notify_error(self, scene: str, detail: str):
        """错误与异常通知，按场景节流。"""
        if not self._should_send(EVENT_ERROR):
            return
        now = time.monotonic()
        last = self._error_last_sent.get(scene)
        if last is not None and now - last < _ERROR_THROTTLE_SECONDS:
            return
        self._error_last_sent[scene] = now
        await self.push(f"⚠️ {scene}", str(detail)[:400], group="PH115-异常")

    async def notify_daily_digest(self, title: str, body: str):
        """每日摘要：过去 24 小时的完成、失败与断流情况。"""
        if not self._should_send(EVENT_DAILY_DIGEST):
            return
        await self.push(title, body, group="PH115-摘要", level="passive")

    async def notify_network_restored(self, seconds: float):
        """较长断流（已发过提醒）恢复后的跟进通知，与错误通知共用开关。"""
        if not self._should_send(EVENT_ERROR):
            return
        minutes = max(1, round(float(seconds or 0) / 60))
        await self.push("✅ 网络已恢复", f"断流约 {minutes} 分钟，下载已自动继续。",
                        group="PH115-异常", level="passive")

    async def notify_upload_restored(self, seconds: float):
        """115 重新接受本机上传后的跟进通知，与错误通知共用开关。"""
        if not self._should_send(EVENT_ERROR):
            return
        minutes = max(1, round(float(seconds or 0) / 60))
        await self.push("✅ 115 上传已恢复", f"上传暂停约 {minutes} 分钟，已自动继续。",
                        group="PH115-异常", level="passive")


notifier = Notifier()
