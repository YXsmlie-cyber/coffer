"""下载失败类型与重试策略。

该模块只负责回答一个问题：某个 yt-dlp 失败是否可以刷新签名后重试。
下载进程、文件处理和任务调度不应各自维护一套错误文案规则。
"""
from __future__ import annotations

from dataclasses import dataclass
import re


class DownloadError(Exception):
    """下载链路错误；未知错误默认禁止盲目网络重试。"""

    action = "fail"
    code = "download_error"
    retryable = False


class NetworkDownloadError(DownloadError):
    """刷新签名后可以重新执行网络下载的错误。"""

    action = "retry_network"
    code = "network_download_error"
    retryable = True


class SourceUnavailableDownloadError(NetworkDownloadError):
    """媒体分片返回 410；与失效清单共用有上限的来源冷却。"""

    code = "source_unavailable"


class RateLimitedDownloadError(NetworkDownloadError):
    """媒体请求返回 429：CDN 按本机出口限流，由全局限流冷却处理，不消耗任务重试次数。"""

    code = "rate_limited"


class DownloadProcessingError(DownloadError):
    """只允许针对同一 staging 重新执行本地封装的错误。"""

    action = "retry_processing"
    code = "processing_error"


class DownloadIntegrityError(DownloadError):
    """清单、断点或成品不满足完整性约束。"""

    code = "integrity_error"


class DownloadArtifactError(DownloadIntegrityError):
    """本次 staging 已证明损坏；只允许丢弃该 staging 后保守重下一次。"""

    action = "retry_clean"
    code = "artifact_error"
    retryable = True


class DownloadEnvironmentError(DownloadError):
    """运行环境缺少硬依赖或版本不兼容。"""

    code = "environment_error"


# 固定版 HlsFD 有时只保留扁平错误文本，无法再从异常链识别网络异常。
# 这里只收录明确表示瞬时传输失败的文本；未知协议错误继续 fail closed。
_FLAT_NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "remote end closed",
    "network is unreachable",
    "temporary failure in name resolution",
    "name or service not known",
    "getaddrinfo failed",
    "proxy error",
    "tunnel connection failed",
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "winerror 10054",
    "winerror 10060",
    "winerror 10061",
    "did not get any data blocks",
)


@dataclass(frozen=True)
class DownloadFailureDisposition:
    """历史下载失败的恢复决策；不依赖易变的前端文案。"""

    kind: str
    recovery_action: str
    bulk_recoverable: bool


MANUAL_FAILURE = DownloadFailureDisposition("manual", "manual", False)


def classify_ytdlp_download_error(
    exc: BaseException,
    detail: str,
) -> DownloadError:
    """按异常链和固定版 HlsFD 文案分类下载失败。"""
    try:
        from yt_dlp.networking.exceptions import network_exceptions, HTTPError
        from yt_dlp.utils import ContentTooShortError
    except ImportError:
        network_exceptions = ()
        ContentTooShortError = ()
        HTTPError = ()

    current: BaseException = exc
    seen: set[int] = set()
    saw_network_error = False
    saw_source_unavailable = False
    saw_rate_limited = False
    for _ in range(8):
        if id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, HTTPError) and current.status == 410:
            saw_source_unavailable = True
        if isinstance(current, HTTPError) and current.status == 429:
            saw_rate_limited = True
        if network_exceptions and isinstance(current, network_exceptions):
            # TransportError 常继续包着 urllib 的 URLError/OSError；一旦链上已确认
            # 是网络异常，就不能因继续下钻而误判成本机环境错误。
            saw_network_error = True
        if isinstance(current, ContentTooShortError):
            # ContentTooShortError 不属于 networking.network_exceptions；确认
            # 实收字节严格少于声明值，才按传输中断处理，超长/无效值仍保守失败。
            downloaded = getattr(current, "downloaded", None)
            expected = getattr(current, "expected", None)
            if (isinstance(downloaded, int) and isinstance(expected, int)
                    and 0 <= downloaded < expected):
                saw_network_error = True
        exc_info = getattr(current, "exc_info", None)
        nested = (
            exc_info[1]
            if isinstance(exc_info, tuple)
            and len(exc_info) > 1
            and isinstance(exc_info[1], BaseException)
            else None
        )
        nested = nested or getattr(current, "__cause__", None)
        if not isinstance(nested, BaseException):
            break
        current = nested

    if saw_source_unavailable or re.search(
            r"\[download\]\s+got error:\s*HTTP Error 410\b", detail, re.I):
        return SourceUnavailableDownloadError(detail)
    if saw_rate_limited or re.search(
            r"\[download\]\s+got error:\s*HTTP Error 429\b", detail, re.I):
        return RateLimitedDownloadError(detail)
    if saw_network_error or (
        network_exceptions and isinstance(current, network_exceptions)
    ):
        return NetworkDownloadError(detail)

    # RetryManager 可能在异常栈离开后才构造最终错误，导致 exc_info 丢失。
    # 仅解析固定的 ``[download] Got error`` 区段，避免把任意文案误判为可重试。
    flat_hls_error = re.search(
        r"\[download\]\s+got error:\s*(.+)", detail, re.I | re.DOTALL
    )
    flat_network_detail = flat_hls_error.group(1) if flat_hls_error else ""
    folded_detail = flat_network_detail.casefold()
    short_response = re.match(
        r"Downloaded ([0-9]{1,20}) bytes, expected ([0-9]{1,20}) bytes\b",
        flat_network_detail, re.I,
    )
    truncated_response = bool(short_response and (
        int(short_response.group(1)) < int(short_response.group(2))))
    flat_network = bool(
        flat_hls_error
        and (
            re.search(r"\bhttp error [45]\d\d\b", flat_network_detail, re.I)
            or truncated_response
            or re.search(
                r"\b\d+ bytes? read,\s*\d+ more expected\b",
                flat_network_detail,
                re.I,
            )
            or any(marker in folded_detail for marker in _FLAT_NETWORK_MARKERS)
        )
    )
    if (
        re.search(r"fragment \d+ not found, unable to continue", detail, re.I)
        or flat_network
        or "The downloaded file is empty" in detail
        or "did not get any data blocks" in detail.casefold()
    ):
        return NetworkDownloadError(detail)
    if isinstance(current, OSError):
        return DownloadEnvironmentError(detail)
    return DownloadIntegrityError(detail)


def classify_stored_download_failure(
    error: str,
    phase: str = "",
) -> DownloadFailureDisposition:
    """把已落库的旧错误映射为有限、可审计的恢复动作。

    ``retry_clean`` 首次执行会删除当前任务自己的 v2 staging，再以单分片连接
    重下；若中途暂停，恢复时会保留新断点。``reconcile_final`` 只会校验已有
    最终 MP4，不会发起网络下载；其它未知错误一律 fail closed，留给逐项人工处理。
    """
    detail = str(error or "").strip()
    folded = detail.casefold()
    normalized_phase = str(phase or "").strip().casefold()

    if normalized_phase == "failed_source_exhausted":
        return DownloadFailureDisposition("source_exhausted", "manual", False)
    if (folded.startswith(("hlssourceunavailableerror:", "sourceunavailabledownloaderror:")) or re.fullmatch(
            r"hlsmanifestfetcherror:\s*hls 清单请求返回 http 410", folded)):
        return DownloadFailureDisposition("source_unavailable", "retry_network", True)

    # 同一种确定性产物错误已经清洁重试过，不能再被批量按钮无限重放。
    if normalized_phase in {"failed_clean_retry", "failed_source_retry"}:
        return DownloadFailureDisposition("repeated_failure", "manual", False)

    if "同一输出文件正由任务" in detail:
        return DownloadFailureDisposition(
            "stale_output_lock", "reconcile_final", True)

    artifact_markers = (
        "下载 staging 的视频时间戳倒退",
        "下载 staging 的音频时间戳倒退",
        "HLS 计划声明含音频，但下载成品缺少音频流",
        "HLS 计划声明含音频，但 staging 中没有音频包",
    )
    duration_mismatch = bool(re.search(
        r"成品视频时间轴\s+\d+(?:\.\d+)?s\s+与\s+hls\s+清单\s+"
        r"\d+(?:\.\d+)?s\s+相差超过协议边界",
        detail,
        re.I,
    ))
    if (folded.startswith("downloadartifacterror:")
            or any(marker.casefold() in folded for marker in artifact_markers)
            or duration_mismatch):
        return DownloadFailureDisposition(
            "invalid_artifact", "retry_clean", True)

    if "所选 hls 变体使用外置音轨" in folded:
        return DownloadFailureDisposition(
            "unsupported_source_variant", "retry_source", True)

    if re.search(r"\[download\]\s+got error:\s*HTTP Error 410\b", detail, re.I):
        return DownloadFailureDisposition("source_unavailable", "retry_network", True)
    # 本机缺 ffmpeg/ffprobe 是运行环境问题，断点仍完整；调度门禁会在工具可用前
    # 暂缓派发，所以允许批量重排，不必逐项人工处理。
    if folded.startswith("downloadenvironmenterror:") and re.search(
            r"(未找到|无法启动)\s*(ffmpeg|ffprobe)", detail, re.I):
        return DownloadFailureDisposition(
            "missing_media_tool", "retry_network", True)
    if folded.startswith((
        "networkdownloaderror:",
        "hlsmanifestfetcherror:",
        "scrapefetcherror:",
    )):
        return DownloadFailureDisposition(
            "transient_network", "retry_network", True)
    if "database is locked" in folded or "database table is locked" in folded:
        return DownloadFailureDisposition(
            "sqlite_lock", "retry_network", True)
    # 兼容修复上线前被误记成 DownloadIntegrityError 的扁平 HlsFD 网络文案。
    classified = classify_ytdlp_download_error(Exception(detail), detail)
    if classified.retryable:
        return DownloadFailureDisposition(
            "transient_network", "retry_network", True)
    return MANUAL_FAILURE


def is_retryable_stored_download_failure(error: str) -> bool:
    """兼容旧调用方：返回历史失败是否可由批量恢复处理。"""
    return classify_stored_download_failure(error).bulk_recoverable
