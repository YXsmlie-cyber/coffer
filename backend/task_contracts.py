"""任务传输与排序契约；不依赖数据库、配置或运行实例。"""
from datetime import datetime
from typing import Literal, NotRequired, TypedDict


TaskType = Literal["download", "upload"]
TaskStatus = Literal["running", "queued", "paused", "completed", "failed", "skipped"]
TASK_STATUS_RANK = {
    "running": 0, "queued": 1, "paused": 2,
    "completed": 3, "failed": 4, "skipped": 5,
}
TASK_STATUS_SQL = "CASE status " + " ".join(
    f"WHEN '{status}' THEN {rank}" for status, rank in TASK_STATUS_RANK.items()
) + " ELSE 6 END"


class TaskPatch(TypedDict):
    id: int
    task_type: NotRequired[TaskType]
    status: NotRequired[TaskStatus]
    phase: NotRequired[str]
    progress: NotRequired[float]
    speed_bps: NotRequired[float]
    speed: NotRequired[str]
    downloaded_bytes: NotRequired[int]
    total_bytes: NotRequired[int]
    attempt: NotRequired[int]
    last_progress_at: NotRequired[str | None]
    auto_retry_count: NotRequired[int]
    next_retry_at: NotRequired[str | None]
    remote_missing_count: NotRequired[int]
    patrol_recovery_count: NotRequired[int]
    error: NotRequired[str]


def wire_value(value):
    """保持现有时间字段的 JSON 表示；不会补默认值或过滤字段。"""
    return value.isoformat() if isinstance(value, datetime) else value
