"""HTTP 请求体模型；类名即 OpenAPI 组件名，改名会破坏契约基线。"""
from typing import Annotated, Literal

from pydantic import BaseModel, Field, FiniteFloat


__all__ = [
    "CookieLogin",
    "QRCodePoll",
    "PluginDirectoryRequest",
    "PluginMagnetSubmitRequest",
    "RankingRequest",
    "DiscoveryRequest",
    "ModelLookupRequest",
    "LibraryBindingRequest",
    "ModelSyncJobRequest",
    "TasteComparisonRequest",
    "TasteResultFeedbackRequest",
    "DownloadRequest",
    "PatrolToggle",
    "PatrolConfig",
    "NotifyConfig",
    "RemoteLoginRequest",
    "BatchDeleteRequest",
]


class CookieLogin(BaseModel):
    cookie: str = Field(min_length=1, max_length=16_384)


class QRCodePoll(BaseModel):
    uid: str = Field(min_length=1, max_length=256)
    time: int
    sign: str = Field(min_length=1, max_length=512)


class PluginDirectoryRequest(BaseModel):
    directory_id: str = Field(min_length=1, max_length=20, pattern=r"^[1-9][0-9]{0,19}$")


class PluginMagnetSubmitRequest(PluginDirectoryRequest):
    magnet: str = Field(min_length=20, max_length=4096)


class RankingRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    max_models: int = Field(default=100, ge=1, le=500)


class DiscoveryRequest(BaseModel):
    source: str = Field(min_length=1, max_length=40)
    max_creators: int = Field(default=50, ge=1, le=500)


class ModelLookupRequest(BaseModel):
    query: str = Field(min_length=1, max_length=80)


class LibraryBindingRequest(BaseModel):
    model_id: int = Field(gt=0)


class ModelSyncJobRequest(BaseModel):
    model_ids: list[Annotated[int, Field(gt=0)]] = Field(
        min_length=1, max_length=5000)


class TasteComparisonRequest(BaseModel):
    left_model_id: int = Field(gt=0)
    right_model_id: int = Field(gt=0)
    choice: Literal["left", "right", "both", "neither", "skip"]


class TasteResultFeedbackRequest(BaseModel):
    model_id: int = Field(gt=0)
    action: Literal["like", "dislike"]


class TasteResultFeedbackBatchRequest(BaseModel):
    """一次评价当前批次中的多位（如“其余都不符合”）。"""
    model_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=24)
    action: Literal["like", "dislike"]


class DownloadRequest(BaseModel):
    # 选择某模特的某些视频下载；mode: all=全部缺失 / missing=仅缺失(不含maybe) / selected=指定viewkeys
    model_id: int
    mode: Literal["all", "missing", "selected"] = "missing"
    viewkeys: list[str] = Field(default_factory=list, max_length=1000)


class PatrolToggle(BaseModel):
    enabled: bool


class PatrolConfig(BaseModel):
    interval_hours: Annotated[FiniteFloat, Field(ge=0.1, le=720)]


class NotifyConfig(BaseModel):
    enabled: bool = False
    # None 表示不修改已存地址（前端留空提交）；显式字符串才覆盖。
    bark_url: str | None = Field(default=None, max_length=2048)
    events: dict[str, bool] = Field(default_factory=dict)


class DownloadSettingsRequest(BaseModel):
    """下载运行参数；范围细节由 download_settings 统一校验并给出中文说明。"""
    max_concurrent_downloads: int = Field(ge=1, le=64)
    max_connections: int = Field(ge=2, le=256)
    rate_limit_mbps: float = Field(default=0.0, ge=0, le=1000)
    window_start: str = Field(default="", max_length=5)
    window_end: str = Field(default="", max_length=5)
    keep_awake: bool = True


class RemoteLoginRequest(BaseModel):
    remember: bool = True


class BatchDeleteRequest(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=1000)
