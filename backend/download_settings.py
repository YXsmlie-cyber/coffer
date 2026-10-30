"""下载运行参数：页面可调，写入 runtime_settings，重启后优先于 .env 默认值。

只包含不影响完整性门禁的调度参数：并发数、总连接预算、近似总限速、允许开始
新下载的时段，以及忙碌时是否阻止系统睡眠。纯数据与时间计算，不做 I/O。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta
import re


STORAGE_KEYS = {
    "max_concurrent_downloads": "download.max_concurrent",
    "max_connections": "download.max_connections",
    "rate_limit_mbps": "download.rate_limit_mbps",
    "window_start": "download.window_start",
    "window_end": "download.window_end",
    "keep_awake": "power.keep_awake",
}
CONCURRENCY_RANGE = (1, 16)
CONNECTIONS_RANGE = (2, 128)
RATE_LIMIT_MAX_MBPS = 1000.0
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str) -> time | None:
    match = _HHMM.match(str(text or "").strip())
    return time(int(match.group(1)), int(match.group(2))) if match else None


@dataclass(frozen=True)
class DownloadSettings:
    max_concurrent_downloads: int
    max_connections: int
    rate_limit_mbps: float = 0.0
    window_start: str = ""
    window_end: str = ""
    keep_awake: bool = True

    @classmethod
    def from_config(cls, config) -> "DownloadSettings":
        return cls(
            max_concurrent_downloads=int(getattr(config, "max_concurrent_downloads", 5)),
            max_connections=int(getattr(config, "download_max_connections", 15)),
            rate_limit_mbps=float(getattr(config, "download_rate_limit_mbps", 0.0) or 0.0),
            window_start=str(getattr(config, "download_window_start", "") or ""),
            window_end=str(getattr(config, "download_window_end", "") or ""),
            keep_awake=bool(getattr(config, "keep_awake_while_busy", True)),
        )

    def validated(self) -> "DownloadSettings":
        """校验并规范化；不合法时抛出带中文说明的 ValueError。"""
        low, high = CONCURRENCY_RANGE
        if not low <= int(self.max_concurrent_downloads) <= high:
            raise ValueError(f"同时下载数需在 {low}–{high} 之间")
        low, high = CONNECTIONS_RANGE
        if not low <= int(self.max_connections) <= high:
            raise ValueError(f"总连接数需在 {low}–{high} 之间")
        rate = float(self.rate_limit_mbps or 0)
        if not 0 <= rate <= RATE_LIMIT_MAX_MBPS:
            raise ValueError(f"限速需在 0–{RATE_LIMIT_MAX_MBPS:g} MB/s 之间，0 表示不限")
        start, end = str(self.window_start or "").strip(), str(self.window_end or "").strip()
        if bool(start) != bool(end):
            raise ValueError("下载时段需要同时填写开始和结束时间，或都留空表示全天")
        if start and (parse_hhmm(start) is None or parse_hhmm(end) is None):
            raise ValueError("下载时段格式应为 HH:MM，例如 22:00")
        if start and start == end:
            raise ValueError("下载时段的开始与结束不能相同；全天请都留空")
        return replace(
            self, max_concurrent_downloads=int(self.max_concurrent_downloads),
            max_connections=int(self.max_connections), rate_limit_mbps=round(rate, 2),
            window_start=start, window_end=end, keep_awake=bool(self.keep_awake))

    def effective_concurrency(self) -> int:
        """与 downloader.download_concurrency_limit 一致：每任务至少需要 2 条连接。"""
        return min(self.max_concurrent_downloads, max(1, self.max_connections // 2))

    def to_storage(self) -> dict[str, str]:
        values = asdict(self)
        return {STORAGE_KEYS[name]: (
            ("1" if value else "0") if isinstance(value, bool) else str(value))
            for name, value in values.items()}

    @classmethod
    def from_storage(cls, base: "DownloadSettings", stored: dict[str, str]) -> "DownloadSettings":
        """用已保存的值覆盖默认；单项损坏时保留默认值，不让整组设置失效。"""
        result = base
        for name, key in STORAGE_KEYS.items():
            if key not in stored:
                continue
            raw = stored[key]
            try:
                if name == "keep_awake":
                    value = str(raw).strip().lower() in {"1", "true", "yes", "on"}
                elif name in {"window_start", "window_end"}:
                    value = str(raw or "").strip()
                elif name == "rate_limit_mbps":
                    value = float(raw)
                else:
                    value = int(raw)
                candidate = replace(result, **{name: value})
                if name not in {"window_start", "window_end"}:
                    candidate.validated()
                result = candidate
            except (TypeError, ValueError):
                continue
        try:
            return result.validated()
        except ValueError:
            # .env 里超出页面范围的历史值（如并发 32）照常生效，只是不经页面校验。
            return result

    # ---------- 下载时段 ----------
    @property
    def has_window(self) -> bool:
        return (parse_hhmm(self.window_start) is not None
                and parse_hhmm(self.window_end) is not None
                and self.window_start != self.window_end)

    def window_text(self) -> str:
        return f"{self.window_start}–{self.window_end}" if self.has_window else ""

    def window_open(self, now: datetime) -> bool:
        if not self.has_window:
            return True
        start, end = parse_hhmm(self.window_start), parse_hhmm(self.window_end)
        current = now.time().replace(second=0, microsecond=0)
        if start < end:
            return start <= current < end
        return current >= start or current < end          # 跨午夜，如 22:00–08:00

    def next_window_open(self, now: datetime) -> datetime | None:
        """时段外时返回下一次开始时刻；时段内或未设置时返回 None。"""
        if self.window_open(now):
            return None
        start = parse_hhmm(self.window_start)
        candidate = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def public(self) -> dict:
        return {**asdict(self), "effective_concurrency": self.effective_concurrency(),
                "window_text": self.window_text()}
