"""基于 yt-dlp 原生 HLS 的下载器。

下载网络路径只有一条：固定版本的 ``yt_dlp`` Python API + ``m3u8_native``。
每个任务运行在独立的 spawn 子进程中，子进程通过队列上报结构化字节进度；
父协程负责滚动速度、等待/停滞判断和进程树回收。ffmpeg 只允许用于下载完成后的
容器修复/封装，ffprobe 用于最终文件校验，二者都不参与网络下载。
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import glob
import inspect
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from .config import APP_DIR, settings
from .download_errors import (
    DownloadArtifactError,
    DownloadEnvironmentError,
    DownloadError,
    DownloadIntegrityError,
    DownloadProcessingError,
    NetworkDownloadError,
    RateLimitedDownloadError,
    SourceUnavailableDownloadError,
    classify_ytdlp_download_error as _classify_ytdlp_download_error,
)
from .scraper import HEADERS, InvalidScrapeURL, validate_media_url
from .hls_composite import compose_plan, MAX_PERIODS


EXPECTED_YTDLP_VERSIONS = {"2026.06.09", "2026.6.9"}
SPEED_WINDOW_SECONDS = 5.0
PROGRESS_EMIT_INTERVAL = 1.0
PROCESSING_TIMEOUT_SECONDS = 600.0
PACKET_SCAN_TIMEOUT_SECONDS = 120.0
MAX_PROCESSING_TIMEOUT_SECONDS = 21_600.0
MAX_PACKET_SCAN_TIMEOUT_SECONDS = 7_200.0
MEDIA_VALIDATION_VERSION = 2

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class _PauseRequested(KeyboardInterrupt):
    """worker 在分片边界协作退出；区别于父进程强杀。"""


def download_concurrency_limit() -> int:
    """按总连接预算限制任务数；原生 HLS 每任务至少含1工作连接+1探测连接。"""
    total_budget = max(
        2, int(getattr(settings, "download_max_connections", 15)))
    requested = max(
        1, int(getattr(settings, "max_concurrent_downloads", 5)))
    return min(requested, max(1, total_budget // 2))


def fragment_workers_for_attempt(attempt: int = 1) -> int:
    """按总预算计算当次 HLS 分片线程数；完整重试逐次降档以换取稳定性。"""
    total_budget = max(
        2, int(getattr(settings, "download_max_connections", 15)))
    task_limit = download_concurrency_limit()
    per_task_budget = max(2, total_budget // task_limit)
    worker_cap = max(
        1,
        int(getattr(
            settings, "download_max_fragment_workers_per_task", 12)),
    )
    base_workers = min(worker_cap, max(1, per_task_budget - 1))

    normalized_attempt = max(1, int(attempt))
    if normalized_attempt == 1:
        return base_workers
    if normalized_attempt == 2:
        return max(1, (base_workers + 1) // 2)
    return min(base_workers, 2)


# yt-dlp 的 ratelimit 作用于每个分片线程；总限速按并发任务与线程数平均分配，
# 结果是近似值。每线程设下限，避免极端设置把分片拖到超时重试。
MIN_FRAGMENT_RATE_LIMIT = 64 * 1024


def fragment_rate_limit(fragment_workers: int) -> int:
    """页面设置的总限速（MB/s）折算为单个分片线程的字节速率；0 表示不限。"""
    total = float(getattr(settings, "download_rate_limit_mbps", 0.0) or 0.0)
    if total <= 0:
        return 0
    per_task = total * 1024 ** 2 / max(1, download_concurrency_limit())
    return max(MIN_FRAGMENT_RATE_LIMIT, int(per_task / max(1, int(fragment_workers))))


def _media_tool_dirs() -> list[str]:
    """ffmpeg/ffprobe 的查找顺序：FFMPEG_DIR → 项目 tools/ffmpeg → PATH。

    本机未把 ffmpeg 加入 PATH 时，每个任务都会在下载完整个视频后才因找不到
    ffprobe 失败；显式目录让查找发生在派发之前，PATH 仍作为兼容兜底。
    """
    candidates: list[str] = []
    configured = str(getattr(settings, "ffmpeg_dir", "") or "").strip()
    if configured:
        candidates.append(os.path.abspath(os.path.expanduser(configured)))
    candidates.append(str(APP_DIR / "tools" / "ffmpeg"))
    seen: set[str] = set()
    directories = []
    for directory in candidates:
        key = os.path.normcase(directory)
        if key not in seen and os.path.isdir(directory):
            seen.add(key)
            directories.append(directory)
    return directories


def resolve_media_tool(name: str) -> str | None:
    """返回工具的绝对路径；找不到时返回 None，由调用方决定如何失败。"""
    suffix = ".exe" if sys.platform == "win32" else ""
    for directory in _media_tool_dirs():
        candidate = os.path.join(directory, name + suffix)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(name)


def ffmpeg_path() -> str:
    return resolve_media_tool("ffmpeg") or "ffmpeg"


def ffprobe_path() -> str:
    return resolve_media_tool("ffprobe") or "ffprobe"


@dataclass(frozen=True)
class MediaToolsStatus:
    """本机后处理工具是否可用；只含工具名与版本行，不含本地路径。"""

    ok: bool
    missing: tuple[str, ...] = ()
    broken: tuple[str, ...] = ()
    versions: tuple[tuple[str, str], ...] = ()

    @property
    def message(self) -> str:
        if self.ok:
            return "ffmpeg/ffprobe 可用"
        parts = []
        if self.missing:
            parts.append(f"找不到 {'、'.join(self.missing)}")
        if self.broken:
            parts.append(f"无法运行 {'、'.join(self.broken)}")
        return "；".join(parts) + "（设置 FFMPEG_DIR 或放入 tools/ffmpeg）"

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "missing": list(self.missing),
            "broken": list(self.broken),
            "versions": dict(self.versions),
        }


# 可用结果缓存较久；缺失时缩短，补上工具后下一次调度很快就能恢复。
MEDIA_TOOLS_OK_TTL = 300.0
MEDIA_TOOLS_MISSING_TTL = 20.0
_media_tools_lock = threading.Lock()
_media_tools_cache: tuple[float, MediaToolsStatus] | None = None


def _probe_media_tool(path: str) -> str | None:
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32" else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first_line = (result.stdout or "").splitlines()[:1]
    return first_line[0][:160] if first_line else ""


def check_media_tools(*, force: bool = False) -> MediaToolsStatus:
    """阻塞检查 ffmpeg/ffprobe；调度器经 asyncio.to_thread 调用并共享缓存。"""
    global _media_tools_cache
    with _media_tools_lock:
        now = time.monotonic()
        if not force and _media_tools_cache and now < _media_tools_cache[0]:
            return _media_tools_cache[1]
        missing, broken, versions = [], [], []
        for name in ("ffmpeg", "ffprobe"):
            path = resolve_media_tool(name)
            if not path:
                missing.append(name)
                continue
            version = _probe_media_tool(path)
            if version is None:
                broken.append(name)
            else:
                versions.append((name, version))
        status = MediaToolsStatus(
            ok=not missing and not broken,
            missing=tuple(missing), broken=tuple(broken), versions=tuple(versions))
        ttl = MEDIA_TOOLS_OK_TTL if status.ok else MEDIA_TOOLS_MISSING_TTL
        _media_tools_cache = (now + ttl, status)
        return status


def safe_filename(name: str, max_len: int = 120) -> str:
    """生成 Windows 和 115 都能稳定接受的文件名主体。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # Windows 不接受尾随空格/点；首点名称在不同 115 客户端里也容易被当作
    # 隐藏式文件名显示。截断后再清理一次，避免边界字符重新变成空格或点。
    name = name.strip(" .")[:max_len].rstrip(" .")
    return name or "video"


def _ascii_safe(name: str) -> str:
    """本地中转目录只使用 ASCII，规避 Windows 工具链编码问题。"""
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("_") or "x"
    return name[:80]


def build_output_path(
    model_name: str,
    title: str,
    viewkey: str,
    artifact_scope: str = "",
    download_dir: str | None = None,
) -> str:
    """本地路径；新任务用模特作用域隔离跨模特同 viewkey 文件。

    ``artifact_scope`` 为空时保持旧路径，确保历史暂停/失败任务的原生 HLS
    断点仍可续传；新建任务传入 model_id，落到独立目录。
    """
    del title  # 标题只用于远端文件名，故意不进入本地路径。
    folder_name = _ascii_safe(model_name)
    if artifact_scope:
        folder_name = f"{folder_name}-{_ascii_safe(str(artifact_scope))}"
    folder = os.path.join(download_dir or settings.download_dir, folder_name)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{viewkey}.mp4")


def build_remote_filename(title: str, viewkey: str) -> str:
    """115 文件名：保留标题并追加 viewkey，供后续强匹配去重。"""
    return f"{safe_filename(title)} [{viewkey}].mp4"


def _staging_path(output_path: str) -> str:
    """v2 独立中间文件；回滚后旧版仍可继续识别原来的 ``.native`` 断点。"""
    root, _ = os.path.splitext(output_path)
    return root + ".native-v2.mp4"


def _legacy_staging_path(output_path: str) -> str:
    root, _ = os.path.splitext(output_path)
    return root + ".native.mp4"


def _processing_path(output_path: str) -> str:
    """ffmpeg 封装输出；与最终文件分离，只有校验通过后才原子提交。"""
    root, _ = os.path.splitext(output_path)
    return root + ".processing-v2.mp4"


def _plan_sidecar_path(output_path: str) -> str:
    return _staging_path(output_path) + ".hls-plan.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stream_closed_by_fragment_failure(exc: BaseException, detail: str) -> bool:
    """固定版 HlsFD 并发分片的竞态：某分片重试耗尽时工作线程先关闭输出流，主线程
    正在追加的前一个分片随即报 ``write to closed file``。真正原因在日志尾部的分片错误。"""
    return (isinstance(exc, ValueError)
            and "closed file" in str(exc).casefold()
            and re.search(r"\[download\]\s+got error:", detail, re.I) is not None)


# 代理/隧道常有 4–16 秒的短暂断流（2026-10 实测），且断流往往在链路探测前就已恢复，
# 探测不到便按计次重试，三次很快用完。分片级退避累计约 23 秒（默认 5 次），
# 让单个分片自己撑过短断流，而不是让整次尝试失败。
_RETRY_DELAY_CAP_SECONDS = 8


def _retry_delay(n: int = 0, **_kwargs) -> float:
    """yt-dlp 从 0 开始传入重试序号，对应 1/2/4/8/8… 秒。"""
    return float(min(2 ** max(0, int(n)), _RETRY_DELAY_CAP_SECONDS))


def _safe_queue_put(events: Any, event: dict[str, Any]) -> None:
    try:
        events.put(event)
    except (BrokenPipeError, EOFError, OSError):
        # 父进程已退出时，子进程不应因为上报失败掩盖原始下载结果。
        pass


def _validated_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """校验下载器输入边界，禁止重新退化为裸 URL + 页面时长。"""
    if not isinstance(plan, dict):
        raise DownloadIntegrityError("下载器缺少结构化 HLS 计划")
    if plan.get("schema_version") == 3:
        try:
            tracks = plan["tracks"]
            if ([t["kind"] for t in tracks] not in (["video"], ["video", "audio"])
                    or any(not 0 < len(t["parts"]) <= MAX_PERIODS for t in tracks)):
                raise ValueError("轨道类型或时间段数量无效")
            for track in tracks:
                for part in track["parts"]:
                    if part.get("schema_version") != 2:
                        raise ValueError("不允许嵌套组合计划")
                    _validated_plan(part)
            expected = compose_plan(tracks[0]["parts"], tracks[1]["parts"] if len(tracks) == 2 else None)
            if any(plan.get(k) != v for k, v in expected.items() if k != "tracks"):
                raise ValueError("组合摘要与各轨道计划不一致")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DownloadIntegrityError(f"组合 HLS 计划无效：{exc}") from exc
        return plan
    try:
        schema = int(plan.get("schema_version") or 0)
        segment_count = int(plan.get("segment_count") or 0)
        unit_count = int(plan.get("unit_count") or 0)
        media_duration = float(plan.get("media_duration") or 0)
        target_duration = float(plan.get("target_duration") or 0)
    except (TypeError, ValueError) as exc:
        raise DownloadIntegrityError("HLS 计划字段类型无效") from exc
    fingerprint = str(plan.get("fingerprint") or "")
    manifest_text = str(plan.get("manifest_text") or "")
    m3u8_url = str(plan.get("m3u8_url") or "")
    if schema != 2:
        raise DownloadIntegrityError(f"不支持的 HLS 计划版本: {schema or '空'}")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DownloadIntegrityError("HLS 计划指纹无效")
    if not m3u8_url.startswith(("http://", "https://")):
        raise DownloadIntegrityError("HLS 计划媒体 URL 无效")
    if not manifest_text.lstrip().startswith("#EXTM3U"):
        raise DownloadIntegrityError("HLS 计划未携带有效媒体清单")
    if "#EXT-X-ENDLIST" not in manifest_text:
        raise DownloadIntegrityError("HLS 计划缺少 ENDLIST")
    media_uris = [
        line.strip() for line in manifest_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if segment_count < 1 or len(media_uris) != segment_count:
        raise DownloadIntegrityError(
            f"HLS 计划分片数不一致: plan={segment_count}, manifest={len(media_uris)}")
    if unit_count < segment_count or unit_count > segment_count + 1:
        raise DownloadIntegrityError("HLS 计划下载单元数无效")
    if (not math.isfinite(media_duration) or not math.isfinite(target_duration)
            or media_duration <= 0 or target_duration <= 0):
        raise DownloadIntegrityError("HLS 计划时间轴无效")
    if "#EXT-X-TARGETDURATION:" not in manifest_text:
        raise DownloadIntegrityError("HLS 计划缺少 TARGETDURATION")
    map_count = 0
    media_seen = False
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-MAP:"):
            map_count += 1
            if media_seen or map_count > 1:
                raise DownloadIntegrityError(
                    "冻结 HLS 清单包含重复或后置 EXT-X-MAP")
        elif not line.startswith("#"):
            media_seen = True
    unsupported_key = re.search(
        r"(?im)^#EXT-X-KEY:.*\bMETHOD=(?!NONE(?:,|$)|AES-128(?:,|$))([^,\s]+)",
        manifest_text,
    )
    if unsupported_key:
        raise DownloadIntegrityError(
            f"原生 HLS 不支持加密方法 {unsupported_key.group(1)}")
    manifest_encrypted = bool(re.search(
        r"(?im)^#EXT-X-KEY:.*\bMETHOD=AES-128(?:,|$)", manifest_text))
    if bool(plan.get("encrypted")) != manifest_encrypted:
        raise DownloadIntegrityError("HLS 计划加密标记与冻结清单不一致")
    try:
        import m3u8 as m3u8lib

        parsed = m3u8lib.loads(manifest_text)
        parsed_durations = [
            float(segment.duration or 0) for segment in parsed.segments
        ]
        parsed_target = float(parsed.target_duration or 0)
    except Exception as exc:
        raise DownloadIntegrityError(f"冻结 HLS 清单无法复核：{exc}") from exc
    if parsed.playlists or not parsed.is_endlist:
        raise DownloadIntegrityError("冻结 HLS 清单不是最终有限媒体清单")
    if any(seg.discontinuity or seg.gap_tag for seg in parsed.segments):
        raise DownloadIntegrityError("冻结叶子清单含未处理的时间轴边界或缺失分片")
    if (len(parsed_durations) != segment_count
            or any(not math.isfinite(value) or value <= 0
                   for value in parsed_durations)):
        raise DownloadIntegrityError("冻结 HLS 清单分片时间轴无效")
    if abs(sum(parsed_durations) - media_duration) > 0.001:
        raise DownloadIntegrityError("HLS 计划媒体时间轴与冻结清单不一致")
    if (not math.isfinite(parsed_target)
            or abs(parsed_target - target_duration) > 0.001):
        raise DownloadIntegrityError("HLS 计划 TARGETDURATION 与冻结清单不一致")
    has_init = any(
        bool(getattr(segment, "init_section", None)) for segment in parsed.segments
    )
    if unit_count != segment_count + int(has_init):
        raise DownloadIntegrityError("HLS 计划下载单元数与冻结清单不一致")
    return plan


def _plan_summary(
    plan: dict[str, Any],
    *,
    resume_safe: bool = False,
    artifact_state: str = "active",
    media_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """断点 sidecar 只保存稳定摘要，不落盘签名 URL 或完整清单。"""
    plan = _validated_plan(plan)
    summary = {
        "schema_version": int(plan["schema_version"]),
        "validation_version": MEDIA_VALIDATION_VERSION,
        "fingerprint": str(plan["fingerprint"]),
        "segment_count": int(plan["segment_count"]),
        "unit_count": int(plan["unit_count"]),
        "media_duration": float(plan["media_duration"]),
        "target_duration": float(plan["target_duration"]),
        "quality": int(plan.get("quality") or 0),
        "expect_audio": bool(plan.get("expect_audio")),
        "encrypted": bool(plan.get("encrypted")),
        # 运行前先置 false；只有 worker 在分片边界协作退出后才可置 true。
        "resume_safe": bool(resume_safe),
        "artifact_state": str(artifact_state),
    }
    if media_proof:
        summary["media_proof"] = media_proof
    if plan["schema_version"] == 3:
        summary["component_count"] = sum(len(t["parts"]) for t in plan["tracks"])
    return summary


def _write_plan_sidecar(output_path: str, summary: dict[str, Any]) -> None:
    sidecar = _plan_sidecar_path(output_path)
    temp_path = sidecar + f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
    os.makedirs(os.path.dirname(os.path.abspath(sidecar)), exist_ok=True)
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=True, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, sidecar)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _read_plan_sidecar(output_path: str) -> dict[str, Any] | None:
    try:
        with open(_plan_sidecar_path(output_path), encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _v2_resume_artifacts(output_path: str) -> list[str]:
    staging = _staging_path(output_path)
    candidates = [
        staging,
        staging + ".part",
        staging + ".ytdl",
        staging + ".part.ytdl",
        _processing_path(output_path),
    ]
    candidates.extend(glob.glob(staging + ".part-Frag*"))
    candidates.extend(glob.glob(os.path.splitext(output_path)[0] + ".native-v3-*"))
    return list(dict.fromkeys(candidates))


def _remove_files_strict(paths: list[str], *, purpose: str) -> None:
    failures = []
    for candidate in list(dict.fromkeys(paths)):
        try:
            path = Path(candidate)
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError as exc:
            failures.append(f"{os.path.basename(candidate)}: {exc}")
    if failures:
        raise DownloadIntegrityError(
            f"{purpose}失败，已拒绝把旧断点绑定到新计划：{' | '.join(failures)[:400]}")


def _discard_incomplete_fragment_parts(staging: str) -> None:
    """丢弃并发 HLS 留下的半截单分片，保留已完成分片和主断点。"""
    _remove_files_strict(
        glob.glob(staging + ".part-Frag*.part"),
        purpose="清理未完成的并发分片",
    )


def _discard_v2_resume(output_path: str) -> None:
    """只清除 v2 活跃断点；任何删除失败都保持旧 sidecar 并硬失败。"""
    _remove_files_strict(
        _v2_resume_artifacts(output_path), purpose="清除 v2 断点")
    _remove_files_strict(
        [_plan_sidecar_path(output_path)], purpose="清除 v2 计划摘要")


def _ytdl_index(path: str, unit_count: int) -> int | None:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        index = int(state["downloader"]["current_fragment"]["index"])
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError,
            json.JSONDecodeError):
        return None
    return index if 0 <= index <= unit_count else None


def _plan_summary_matches(stored: dict[str, Any] | None, summary: dict[str, Any]) -> bool:
    if not stored:
        return False
    try:
        stored_unit_count = int(stored.get("unit_count"))
    except (TypeError, ValueError):
        return False
    return bool(
        stored.get("schema_version") == summary["schema_version"]
        and stored.get("fingerprint") == summary["fingerprint"]
        and stored_unit_count == summary["unit_count"]
    )


def _mark_resume_safe(output_path: str, plan: dict[str, Any]) -> bool:
    """仅在协作暂停后，把可证明位于分片边界的 TS 断点标为可复用。"""
    summary = _plan_summary(plan, resume_safe=True)
    stored = _read_plan_sidecar(output_path)
    if not _plan_summary_matches(stored, summary):
        return False
    if plan["schema_version"] == 3:
        for kind, index, part in _composite_parts(plan):
            _mark_resume_safe(_component_path(output_path, kind, index), part)
        # 子段各自执行旧断点门禁；fMP4 未完成段仍重下，已完成段保持可复用。
        _write_plan_sidecar(output_path, summary)
        return True
    # 固定版 HlsFD 续传会重复 EXT-X-MAP；fMP4 只允许完整 staging 复用。
    if (summary["unit_count"] != summary["segment_count"]
            or summary["encrypted"]):
        return False
    staging = _staging_path(output_path)
    # HlsFD 的并发分片失败时，已完成分片为 ``part-FragN``，半截分片为
    # ``part-FragN.part``。后者若跨签名刷新继续 Range，CDN 会稳定返回 416；
    # 它不属于安全边界，必须单独重下，不能因此丢弃其它已完成分片。
    _discard_incomplete_fragment_parts(staging)
    part = staging + ".part"
    frag_files = glob.glob(staging + ".part-Frag*")
    ytdl_candidates = [staging + ".ytdl", staging + ".part.ytdl"]
    existing_ytdl = [path for path in ytdl_candidates if os.path.isfile(path)]
    if not (os.path.isfile(part) or frag_files) or len(existing_ytdl) != 1:
        return False
    index = _ytdl_index(existing_ytdl[0], summary["unit_count"])
    if index is None:
        return False
    try:
        part_size = os.path.getsize(part) if os.path.isfile(part) else 0
    except OSError:
        return False
    if part_size > 0 and index == 0:
        return False
    _write_plan_sidecar(output_path, summary)
    return True


def _mark_plan_artifact_invalid(output_path: str, plan: dict[str, Any]) -> None:
    """保留坏 staging 供诊断，但保证下一次人工重试先清除后重下。"""
    try:
        _write_plan_sidecar(
            output_path,
            _plan_summary(plan, resume_safe=False, artifact_state="invalid"),
        )
    except OSError:
        # 原始完整性错误仍是主错误；sidecar 写失败时下次也因 resume_safe=false 拒绝断点。
        pass


def _prepare_plan_state(output_path: str, plan: dict[str, Any]) -> bool:
    """建立 v2 断点门禁；返回本次是否安全复用了已有活跃文件。"""
    summary = _plan_summary(plan)
    stored = _read_plan_sidecar(output_path)
    active = [path for path in _v2_resume_artifacts(output_path) if os.path.exists(path)]
    reusable = bool(
        active
        and _plan_summary_matches(stored, summary)
        and stored.get("artifact_state") != "invalid"
    )
    if active and not reusable:
        _discard_v2_resume(output_path)
        active = []
    elif reusable:
        if plan["schema_version"] == 3:
            _write_plan_sidecar(output_path, _plan_summary(
                plan, artifact_state=str(stored.get("artifact_state") or "active"),
                media_proof=stored.get("media_proof")))
            return True
        staging = _staging_path(output_path)
        # 兼容修复上线前已被标为 resume_safe 的断点；进入 worker 前先移除
        # 半截单分片，避免旧状态在第一次重试仍触发 HTTP 416。
        _discard_incomplete_fragment_parts(staging)
        part = staging + ".part"
        ytdl_candidates = [staging + ".ytdl", staging + ".part.ytdl"]
        existing_ytdl = [path for path in ytdl_candidates if os.path.isfile(path)]
        frag_files = glob.glob(staging + ".part-Frag*")
        if os.path.isfile(staging):
            # 已完成 staging 优先；遗留的临时续传状态不允许与它混用。
            _remove_files_strict(
                [part, *existing_ytdl, *frag_files],
                purpose="清理完整 staging 的临时状态",
            )
        elif (os.path.isfile(part) or frag_files):
            index = (
                _ytdl_index(existing_ytdl[0], summary["unit_count"])
                if len(existing_ytdl) == 1 else None
            )
            try:
                part_size = os.path.getsize(part) if os.path.isfile(part) else 0
            except OSError:
                part_size = -1
            if (not bool(stored.get("resume_safe"))
                    or summary["unit_count"] != summary["segment_count"]
                    or summary["encrypted"]
                    or index is None or part_size < 0
                    or (part_size > 0 and index == 0)):
                _discard_v2_resume(output_path)
                active = []
                reusable = False
        elif existing_ytdl:
            _discard_v2_resume(output_path)
            active = []
            reusable = False
    _write_plan_sidecar(output_path, summary)
    return reusable and bool(active)


def _output_lock_path(output_path: str) -> str:
    root, _ = os.path.splitext(output_path)
    return root + ".download-v2.lock"


def _partial_artifact_patterns(output_path: str) -> list[str]:
    """返回新旧下载引擎可能留下的续传/处理中间文件模式。"""
    output_path = os.path.abspath(output_path)
    root, _ = os.path.splitext(output_path)
    return [
        root + ".native*",
        root + ".processing*",
        output_path + ".part",
        output_path + ".part.*",
        output_path + ".part-Frag*",
    ]


def has_partial_artifacts(output_path: str) -> bool:
    """判断目标路径旁是否有真正需要留在原盘续传的媒体数据。"""
    for pattern in _partial_artifact_patterns(output_path):
        for candidate in glob.glob(pattern):
            # 计划摘要和 yt-dlp 元数据本身不含媒体字节，不能因此把任务钉死在满盘。
            lowered = candidate.casefold()
            if lowered.endswith((".hls-plan.json", ".ytdl")):
                continue
            try:
                if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                    return True
            except OSError:
                continue
    return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # Access denied still means the PID exists; fail closed.
            return ctypes.windll.kernel32.GetLastError() == 5
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_identity(pid: int) -> str:
    """返回可区分 PID 复用的进程创建身份；无法读取时返回空并 fail closed。"""
    if pid <= 0:
        return ""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return ""
            creation = FILETIME()
            exit_time = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            try:
                ok = ctypes.windll.kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            if not ok:
                return ""
            value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return f"win:{value}"
        except Exception:
            return ""
    try:
        # Linux /proc stat 的第 22 字段是自系统启动以来的 starttime tick。
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        tail = stat.rsplit(")", 1)[1].strip().split()
        return f"proc:{tail[19]}"
    except (OSError, IndexError, ValueError):
        return ""


def _lock_owner_active(record: dict[str, Any]) -> bool:
    """判断锁记录是否仍属于存活的同一进程实例；不完整记录保守视为活跃。"""
    try:
        owner_pids = {
            int(record.get("parent_pid") or 0),
            int(record.get("worker_pid") or 0),
            int(record.get("pid") or 0),
        } - {0}
    except (TypeError, ValueError):
        return True
    if not owner_pids:
        return True
    identities = record.get("pid_identities")
    identities = identities if isinstance(identities, dict) else {}
    for pid in owner_pids:
        if not _pid_alive(pid):
            continue
        expected = str(identities.get(str(pid)) or "")
        if expected:
            current = _pid_identity(pid)
            if current and current != expected:
                # PID 已复用，不再是创建锁的进程实例。
                continue
        return True
    return False


def _read_output_lock(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = handle.read(16 * 1024 + 1)
        if len(payload) > 16 * 1024:
            return None
        value = json.loads(payload)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def output_lock_allows_recovery(output_path: str, task_key: str) -> bool:
    """只读核对恢复资格，不清理锁；实际下载仍须原子取得输出锁。"""
    path = _output_lock_path(os.path.abspath(output_path))
    record = _read_output_lock(path)
    if record is None:
        return not os.path.lexists(path)
    if not task_key or str(record.get("task_key") or "") != str(task_key):
        return False
    return not _lock_owner_active(record)


def _write_output_lock(path: str, record: dict[str, Any], *, exclusive: bool) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if exclusive:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(path, flags, 0o600)
        try:
            payload = json.dumps(record, ensure_ascii=True, sort_keys=True).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    temp_path = path + f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=True, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _acquire_output_lock(output_path: str, task_key: str) -> str:
    path = _output_lock_path(output_path)
    token = secrets.token_hex(16)
    parent_pid = os.getpid()
    parent_identity = _pid_identity(parent_pid)
    record = {
        "pid": parent_pid,
        "parent_pid": parent_pid,
        "worker_pid": 0,
        "task_key": task_key,
        "token": token,
        "pid_identities": (
            {str(parent_pid): parent_identity} if parent_identity else {}
        ),
    }
    for _ in range(3):
        try:
            _write_output_lock(path, record, exclusive=True)
            return token
        except FileExistsError:
            existing = _read_output_lock(path)
            if not existing:
                raise DownloadIntegrityError(
                    f"输出锁损坏，拒绝并发写入：{os.path.basename(path)}")
            if _lock_owner_active(existing):
                raise DownloadError(
                    f"同一输出文件正由任务 {existing.get('task_key') or '未知'} 写入")
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DownloadIntegrityError(f"无法清理失效输出锁：{exc}") from exc
    raise DownloadIntegrityError("无法原子获取输出文件锁")


def _assign_output_lock_to_worker(
    output_path: str, token: str, task_key: str, worker_pid: int,
) -> None:
    path = _output_lock_path(output_path)
    existing = _read_output_lock(path)
    if not existing or existing.get("token") != token:
        raise DownloadIntegrityError("输出锁所有权在 worker 启动时发生变化")
    parent_pid = os.getpid()
    identities = existing.get("pid_identities")
    identities = dict(identities) if isinstance(identities, dict) else {}
    parent_identity = _pid_identity(parent_pid)
    worker_identity = _pid_identity(int(worker_pid))
    if parent_identity:
        identities[str(parent_pid)] = parent_identity
    if worker_identity:
        identities[str(int(worker_pid))] = worker_identity
    _write_output_lock(path, {
        "pid": int(worker_pid),
        "parent_pid": parent_pid,
        "worker_pid": int(worker_pid),
        "task_key": task_key,
        "token": token,
        "pid_identities": identities,
    }, exclusive=False)


def _release_output_lock(output_path: str, token: str) -> None:
    path = _output_lock_path(output_path)
    existing = _read_output_lock(path)
    if not existing or existing.get("token") != token:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        # 遗留锁含已结束 PID；下次启动会原子识别并清理，不得误删别人的锁。
        pass


def cleanup_stale_output_lock(output_path: str, task_key: str = "") -> bool:
    """确认输出已解锁；必要时清理身份匹配且属主已死亡的单个锁。"""
    path = _output_lock_path(os.path.abspath(output_path))
    existing = _read_output_lock(path)
    if not existing:
        return not os.path.exists(path)
    existing_key = str(existing.get("task_key") or "")
    if task_key and existing_key and existing_key != str(task_key):
        return False
    if _lock_owner_active(existing):
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise DownloadProcessingError(f"无法清理失效输出锁: {exc}") from exc
    return True


class _YTDLPLogger:
    """抑制控制台文本进度，只保留最终错误上下文。"""

    def __init__(self) -> None:
        self.tail: deque[str] = deque(maxlen=8)

    def debug(self, message: str) -> None:
        # yt-dlp 会把普通信息也走 debug；只保留明确的错误/警告。
        if message.startswith("[debug]"):
            return

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        value = str(message)
        # 旧链路把 master 当媒体清单时，Generic extractor 会把非零媒体序号误报成 live。
        # v2 固定输入最终 ENDLIST 清单且不再使用 Generic；该文本不应污染真实错误。
        if "Live HLS streams are not supported by the native downloader" not in value:
            self.tail.append(value)

    def error(self, message: str) -> None:
        self.tail.append(str(message))


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = urlparse(left), urlparse(right)
        a_port = a.port or (443 if a.scheme.lower() == "https" else 80)
        b_port = b.port or (443 if b.scheme.lower() == "https" else 80)
        return (
            a.scheme.lower(), (a.hostname or "").lower(), a_port
        ) == (
            b.scheme.lower(), (b.hostname or "").lower(), b_port
        )
    except ValueError:
        return False


def _validate_worker_media_url(url: str, plan_url: str) -> str:
    """worker 重定向守卫；本机仅放行父进程已验证的测试夹具同源。"""
    try:
        return validate_media_url(url)
    except InvalidScrapeURL:
        if _same_origin(url, plan_url):
            return url
        raise


def _restrict_request_director_to_urllib(ydl: Any) -> None:
    """移除网络后端回退，确保重定向守卫异常不能改走 Requests/CurlCFFI。"""
    director = ydl._request_director
    if "Urllib" not in director.handlers:
        raise DownloadEnvironmentError("yt-dlp 缺少可安装重定向守卫的 Urllib 后端")
    for key, handler in list(director.handlers.items()):
        if key == "Urllib":
            continue
        try:
            handler.close()
        finally:
            director.handlers.pop(key, None)
    if set(director.handlers) != {"Urllib"}:
        raise DownloadEnvironmentError("无法锁定 yt-dlp 单一受守卫网络后端")


def _is_local_file_busy(detail: str) -> bool:
    lowered = str(detail or "").casefold()
    return any(marker in lowered for marker in (
        "permission denied",
        "being used by another process",
        "resource temporarily unavailable",
        "sharing violation",
    ))


def _safe_file_size(path: str) -> int:
    try:
        return max(0, int(os.path.getsize(path)))
    except OSError:
        return 0


def _operation_timeout(
    path: str,
    duration: float,
    *,
    base: float,
    maximum: float,
    duration_ratio: float,
    bytes_per_second: float,
) -> float:
    """按媒体时长和文件体积放大本地处理时限，同时保留有限上界。"""
    safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
    size_seconds = _safe_file_size(path) / max(1.0, bytes_per_second)
    return min(
        maximum,
        max(base, 60.0 + safe_duration * duration_ratio, 60.0 + size_seconds),
    )


def _probe_duration(probe: dict[str, Any]) -> float:
    values: list[float] = []
    raw_values = [(probe.get("format") or {}).get("duration")]
    raw_values.extend(
        stream.get("duration")
        for stream in (probe.get("streams") or [])
        if isinstance(stream, dict)
    )
    for raw in raw_values:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            values.append(value)
    return max(values, default=0.0)


def _packet_scan_timeout(path: str, probe: dict[str, Any]) -> float:
    # 逐包输出量随时长和文件体积增长；8 MiB/s 是刻意保守的本地扫描基线。
    return _operation_timeout(
        path,
        _probe_duration(probe),
        base=PACKET_SCAN_TIMEOUT_SECONDS,
        maximum=MAX_PACKET_SCAN_TIMEOUT_SECONDS,
        duration_ratio=0.10,
        bytes_per_second=8 * 1024 * 1024,
    )


def _remux_timeout(path: str, plan: dict[str, Any]) -> float:
    return _operation_timeout(
        path,
        float(plan.get("media_duration") or 0),
        base=PROCESSING_TIMEOUT_SECONDS,
        maximum=MAX_PROCESSING_TIMEOUT_SECONDS,
        duration_ratio=0.25,
        bytes_per_second=4 * 1024 * 1024,
    )


def _processing_watchdog_timeout(path: str, plan: dict[str, Any]) -> float:
    """覆盖 staging 扫描、封装和成品复核三段，而不是固定十分钟误杀。"""
    duration = float(plan.get("media_duration") or 0)
    packet = _operation_timeout(
        path,
        duration,
        base=PACKET_SCAN_TIMEOUT_SECONDS,
        maximum=MAX_PACKET_SCAN_TIMEOUT_SECONDS,
        duration_ratio=0.10,
        bytes_per_second=8 * 1024 * 1024,
    )
    remux = _operation_timeout(
        path,
        duration,
        base=PROCESSING_TIMEOUT_SECONDS,
        maximum=MAX_PROCESSING_TIMEOUT_SECONDS,
        duration_ratio=0.25,
        bytes_per_second=4 * 1024 * 1024,
    )
    return min(MAX_PROCESSING_TIMEOUT_SECONDS, 120.0 + packet * 2 + remux)


def _processing_watchdog_for_artifacts(
    staging_path: str,
    output_path: str,
    plan: dict[str, Any],
) -> float:
    """按处理阶段此刻已有的最大 staging/processing 文件计算父看门狗。"""
    return max(
        _processing_watchdog_timeout(staging_path, plan),
        _processing_watchdog_timeout(_processing_path(output_path), plan),
    )


def _ffprobe_json(path: str) -> dict[str, Any]:
    """读取容器/流元数据；网络下载和页面时长均不参与。"""
    cmd = [
        ffprobe_path(),
        "-v", "error",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,start_time,duration,width,height,time_base,sample_rate,channels:"
            "format=start_time,duration,format_name"
        ),
        "-of", "json",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32" else 0
            ),
        )
    except FileNotFoundError as exc:
        raise DownloadEnvironmentError("未找到 ffprobe，无法校验下载成品") from exc
    except OSError as exc:
        raise DownloadEnvironmentError(f"无法启动 ffprobe: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadProcessingError("ffprobe 校验超时") from exc

    if result.returncode != 0:
        detail = result.stderr.strip()[:400]
        if _is_local_file_busy(detail):
            raise DownloadProcessingError(
                f"ffprobe 读取成品时文件暂时被占用: {detail or '未知错误'}")
        raise DownloadIntegrityError(
            f"ffprobe 校验失败: {detail or '无法读取媒体文件'}")
    try:
        probe = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DownloadProcessingError("ffprobe 返回了无效的 JSON 数据") from exc
    if not isinstance(probe, dict):
        raise DownloadProcessingError("ffprobe 返回了无效的媒体结构")
    return probe


def _scan_packet_timelines(path: str, probe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """逐包复核 staging 顺序，并生成封装前后可比较的保真证据。"""
    stream_types: dict[int, str] = {}
    for stream in probe.get("streams") or []:
        codec_type = str(stream.get("codec_type") or "")
        if codec_type not in {"video", "audio"}:
            continue
        try:
            stream_types[int(stream["index"])] = codec_type
        except (KeyError, TypeError, ValueError):
            raise DownloadProcessingError("ffprobe 流索引无效")

    cmd = [
        ffprobe_path(),
        "-v", "error",
        "-show_entries", "packet=stream_index,dts_time,duration_time",
        "-of", "csv=p=0",
        path,
    ]
    per_stream: dict[int, dict[str, Any]] = {
        index: {
            "codec_type": codec_type,
            "packet_count": 0,
            "start": None,
            "end": None,
            "previous_dts": None,
        }
        for index, codec_type in stream_types.items()
    }

    # stdout 逐行消费，内存与媒体包数量无关。stderr 写入临时文件并只读取末尾，
    # 避免 capture_output 在超长视频上同时保留整份 CSV 和 splitlines 对象。
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    process: subprocess.Popen | None = None
    timed_out = threading.Event()
    returncode: int | None = None
    stderr_tail = ""

    def _expire() -> None:
        timed_out.set()
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    timer: threading.Timer | None = None
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32" else 0
            ),
        )
        timer = threading.Timer(_packet_scan_timeout(path, probe), _expire)
        timer.daemon = True
        timer.start()
        if process.stdout is None:
            raise DownloadProcessingError("ffprobe 未提供媒体包输出管道")
        for raw_line in process.stdout:
            parts = raw_line.rstrip("\r\n,").split(",")
            if len(parts) < 2:
                continue
            try:
                stream_index = int(parts[0])
                dts = float(parts[1])
                packet_duration = (
                    float(parts[2]) if len(parts) > 2 and parts[2] else 0.0)
            except (TypeError, ValueError):
                continue
            state = per_stream.get(stream_index)
            if state is None:
                continue
            if not math.isfinite(dts) or not math.isfinite(packet_duration):
                raise DownloadIntegrityError("下载 staging 包含非有限时间戳")
            previous = state["previous_dts"]
            # DTS 理应按媒体包顺序单调递增。大幅倒退意味着并发分片或旧断点
            # 被错误拼接；即使总时长碰巧正确也必须重下。
            if previous is not None and dts < float(previous) - 0.5:
                label = "视频" if state["codec_type"] == "video" else "音频"
                raise DownloadArtifactError(
                    f"下载 staging 的{label}时间戳倒退 "
                    f"{float(previous) - dts:.1f}s，分片顺序无法证明完整")
            state["previous_dts"] = dts
            state["packet_count"] += 1
            state["start"] = (
                dts if state["start"] is None else min(state["start"], dts))
            packet_end = dts + max(0.0, packet_duration)
            state["end"] = (
                packet_end if state["end"] is None
                else max(state["end"], packet_end))
        returncode = process.wait()
    except FileNotFoundError as exc:
        raise DownloadEnvironmentError("未找到 ffprobe，无法复核媒体分片顺序") from exc
    except OSError as exc:
        if timed_out.is_set():
            raise DownloadProcessingError("ffprobe 分片顺序校验超时") from exc
        raise DownloadEnvironmentError(f"无法启动 ffprobe: {exc}") from exc
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=0.2)
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.stdout is not None:
                process.stdout.close()
        try:
            stderr_file.seek(0, os.SEEK_END)
            length = stderr_file.tell()
            stderr_file.seek(max(0, length - 4096))
            stderr_tail = stderr_file.read().decode("utf-8", errors="replace")
        finally:
            stderr_file.close()

    if timed_out.is_set():
        raise DownloadProcessingError("ffprobe 分片顺序校验超时")
    if returncode != 0:
        detail = stderr_tail.strip()[-400:]
        if _is_local_file_busy(detail):
            raise DownloadProcessingError(
                f"ffprobe 读取 staging 时文件暂时被占用: {detail or '未知错误'}")
        raise DownloadIntegrityError(
            f"ffprobe 分片顺序校验失败: {detail or '无法读取媒体包'}")

    aggregate: dict[str, dict[str, Any]] = {}
    for codec_type in ("video", "audio"):
        states = [
            state for state in per_stream.values()
            if state["codec_type"] == codec_type
        ]
        populated = [state for state in states if state["packet_count"] > 0]
        if states and len(populated) != len(states):
            label = "视频" if codec_type == "video" else "音频"
            raise DownloadIntegrityError(f"下载 staging 的{label}流没有可验证的媒体包")
        start = min((state["start"] for state in populated), default=0.0)
        end = max((state["end"] for state in populated), default=0.0)
        aggregate[codec_type] = {
            "stream_count": len(states),
            "packet_count": sum(state["packet_count"] for state in populated),
            "duration": max(0.0, float(end) - float(start)),
        }
    return aggregate


def _timeline_tolerance(plan: dict[str, Any]) -> float:
    # MPEG-TS/MP4 的 time_base 舍入在长视频末端可接近 1 秒；仍严格小于一个
    # 常规 HLS 分片，既吸收封装舍入，也不会放过整段缺失或重复。
    return max(0.35, min(1.0, float(plan["target_duration"]) * 0.2))


def _probe_basics(
    probe: dict[str, Any],
    plan: dict[str, Any],
    *,
    require_mp4: bool,
    allow_unknown_audio_duration: bool,
) -> dict[str, Any]:
    try:
        format_info = probe.get("format", {})
        duration = float(format_info.get("duration") or 0)
        format_names = {
            item.strip().lower()
            for item in str(format_info.get("format_name") or "").split(",")
            if item.strip()
        }
        streams = probe.get("streams") or []
    except (TypeError, ValueError) as exc:
        raise DownloadProcessingError("ffprobe 返回了无效的媒体数据") from exc

    video_streams = [
        stream for stream in streams if stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    if not video_streams:
        raise DownloadIntegrityError("下载成品中不存在视频流")
    if plan.get("expect_audio") and not audio_streams:
        raise DownloadIntegrityError("HLS 计划声明含音频，但下载成品缺少音频流")
    if not math.isfinite(duration) or duration <= 0:
        raise DownloadIntegrityError("下载成品时长无效")
    if (require_mp4
            and not format_names.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})):
        raise DownloadIntegrityError(
            f"封装格式不是 MP4：{','.join(sorted(format_names)) or '未知'}")
    try:
        video_timelines = [
            (
                float(stream.get("start_time") or 0),
                float(stream.get("duration") or 0),
            )
            for stream in video_streams
        ]
        audio_timelines = [
            (
                float(stream.get("start_time") or 0),
                float(stream.get("duration") or 0),
            )
            for stream in audio_streams
        ]
    except (TypeError, ValueError) as exc:
        raise DownloadIntegrityError("下载成品音视频时间轴无效") from exc
    timeline_values = [
        value for timeline in [*video_timelines, *audio_timelines]
        for value in timeline
    ]
    if any(not math.isfinite(value) for value in timeline_values):
        raise DownloadIntegrityError("下载成品音视频时间轴包含非有限值")
    video_durations = [duration for _, duration in video_timelines]
    audio_durations = [duration for _, duration in audio_timelines]
    video_duration = max(video_durations, default=0.0)
    if video_duration <= 0:
        raise DownloadIntegrityError("下载成品视频时间轴无效")
    expected = float(plan["media_duration"])
    # HlsFD 已被要求下载全部冻结分片；容器时间轴只允许编码器/PTS 的亚秒级舍入，
    # 绝不允许用一个 TARGETDURATION 的宽容差放过整段缺失或重复。
    tolerance = _timeline_tolerance(plan)
    if abs(video_duration - expected) > tolerance:
        raise DownloadIntegrityError(
            f"成品视频时间轴 {video_duration:.1f}s 与 HLS 清单 {expected:.1f}s "
            f"相差超过协议边界 {tolerance:g}s")
    audio_duration = max(audio_durations, default=0.0)
    if audio_streams:
        if audio_duration <= 0 and not allow_unknown_audio_duration:
            raise DownloadIntegrityError("下载成品音频时间轴无效")
    elif plan.get("expect_audio"):
        raise DownloadIntegrityError("HLS 计划声明含音频，但下载成品缺少音频流")
    return {
        "duration": duration,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "streams": streams,
    }


def _media_proof(packet_timelines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    proof: dict[str, Any] = {}
    for codec_type in ("video", "audio"):
        state = packet_timelines.get(codec_type) or {}
        proof[f"{codec_type}_stream_count"] = int(state.get("stream_count") or 0)
        proof[f"{codec_type}_packet_count"] = int(state.get("packet_count") or 0)
        proof[f"{codec_type}_duration"] = round(float(state.get("duration") or 0), 6)
    return proof


def _validate_media_proof(
    actual: dict[str, dict[str, Any]],
    expected: dict[str, Any],
    tolerance: float,
) -> None:
    for codec_type, label in (("video", "视频"), ("audio", "音频")):
        state = actual.get(codec_type) or {}
        stream_count = int(state.get("stream_count") or 0)
        packet_count = int(state.get("packet_count") or 0)
        duration = float(state.get("duration") or 0)
        try:
            expected_streams = int(expected.get(f"{codec_type}_stream_count") or 0)
            expected_packets = int(expected.get(f"{codec_type}_packet_count") or 0)
            expected_duration = float(expected.get(f"{codec_type}_duration") or 0)
        except (TypeError, ValueError) as exc:
            raise DownloadIntegrityError("媒体保真证据字段无效") from exc
        if stream_count != expected_streams or packet_count != expected_packets:
            raise DownloadIntegrityError(
                f"MP4 封装前后{label}流/媒体包数量不一致："
                f"{expected_streams}/{expected_packets} -> {stream_count}/{packet_count}")
        if expected_packets and abs(duration - expected_duration) > tolerance:
            raise DownloadIntegrityError(
                f"MP4 封装前后{label}时间轴变化 {abs(duration - expected_duration):.1f}s，"
                f"超过协议边界 {tolerance:g}s")


def _validate_staging_with_ffprobe(path: str, plan: dict[str, Any]) -> dict[str, Any]:
    """确认完整 staging：视频覆盖冻结清单，且媒体包顺序没有倒退。"""
    plan = _validated_plan(plan)
    probe = _ffprobe_json(path)
    basics = _probe_basics(
        probe,
        plan,
        require_mp4=False,
        allow_unknown_audio_duration=True,
    )
    packets = _scan_packet_timelines(path, probe)
    if packets["video"]["packet_count"] <= 0:
        raise DownloadIntegrityError("下载 staging 中不存在可验证的视频包")
    if plan.get("expect_audio") and packets["audio"]["packet_count"] <= 0:
        raise DownloadIntegrityError("HLS 计划声明含音频，但 staging 中没有音频包")
    return {**basics, "packets": packets, "media_proof": _media_proof(packets)}


def _validate_with_ffprobe(
    path: str,
    plan: dict[str, Any],
    *,
    source_evidence: dict[str, Any] | None = None,
    media_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """确认 MP4；有 staging 时证明无损封装，无 staging 时核对已保存的证据。"""
    plan = _validated_plan(plan)
    probe = _ffprobe_json(path)
    basics = _probe_basics(
        probe,
        plan,
        require_mp4=True,
        allow_unknown_audio_duration=False,
    )
    expected_proof = (
        source_evidence.get("media_proof") if source_evidence else media_proof
    )
    packets = None
    if expected_proof:
        packets = _scan_packet_timelines(path, probe)
        _validate_media_proof(packets, expected_proof, _timeline_tolerance(plan))
    elif basics["audio_duration"] > 0:
        # 兼容旧版已经完成但尚无保真证据的文件；旧版成功条件本来就要求音轨
        # 与清单等长，所以这里继续按旧标准复核，不给历史文件放宽边界。
        expected = float(plan["media_duration"])
        tolerance = _timeline_tolerance(plan)
        if abs(basics["audio_duration"] - expected) > tolerance:
            raise DownloadIntegrityError(
                f"成品音频时间轴 {basics['audio_duration']:.1f}s 与 HLS 清单 "
                f"{expected:.1f}s 相差超过协议边界 {tolerance:g}s")
    return {
        **basics,
        "packets": packets,
        "media_proof": _media_proof(packets) if packets else media_proof,
    }


def validate_completed_artifact(output_path: str) -> dict[str, Any]:
    """离线复核已提交 MP4，供崩溃后的任务状态对账使用。

    sidecar 不保存签名 URL 或完整清单，因此这里只接受当前校验版本写出的
    ``completed`` 摘要，并重新核对最终文件的流、时长、包数量和时间轴证据。
    """
    output_path = os.path.abspath(output_path)
    if not os.path.isfile(output_path):
        raise DownloadIntegrityError("待对账的最终 MP4 不存在")
    sidecar = _read_plan_sidecar(output_path)
    if not sidecar:
        raise DownloadIntegrityError("已有最终 MP4 缺少 HLS 完成摘要")
    if sidecar.get("artifact_state") != "completed":
        raise DownloadIntegrityError("已有最终 MP4 的 HLS 摘要未标记 completed")
    if int(sidecar.get("validation_version") or 0) != MEDIA_VALIDATION_VERSION:
        raise DownloadIntegrityError("已有最终 MP4 的校验版本不受信任")
    if not re.fullmatch(r"[0-9a-f]{64}", str(sidecar.get("fingerprint") or "")):
        raise DownloadIntegrityError("已有最终 MP4 的 HLS 指纹无效")
    proof = sidecar.get("media_proof")
    if not isinstance(proof, dict):
        raise DownloadIntegrityError("已有最终 MP4 缺少封装保真证据")
    try:
        media_duration = float(sidecar["media_duration"])
        target_duration = float(sidecar["target_duration"])
        segment_count = int(sidecar["segment_count"])
        unit_count = int(sidecar["unit_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DownloadIntegrityError("已有最终 MP4 的 HLS 摘要字段无效") from exc
    component_count = int(sidecar.get("component_count") or 1)
    max_inits = component_count if sidecar.get("schema_version") == 3 else 1
    if (not 1 <= component_count <= MAX_PERIODS * 2
            or not math.isfinite(media_duration) or media_duration <= 0
            or not math.isfinite(target_duration) or target_duration <= 0
            or segment_count <= 0
            or not segment_count <= unit_count <= segment_count + max_inits):
        raise DownloadIntegrityError("已有最终 MP4 的 HLS 摘要边界无效")
    plan_summary = {
        "media_duration": media_duration,
        "target_duration": target_duration,
        "expect_audio": bool(sidecar.get("expect_audio")),
    }
    probe = _ffprobe_json(output_path)
    basics = _probe_basics(
        probe,
        plan_summary,
        require_mp4=True,
        allow_unknown_audio_duration=False,
    )
    packets = _scan_packet_timelines(output_path, probe)
    _validate_media_proof(
        packets,
        proof,
        _timeline_tolerance(plan_summary),
    )
    return {
        **basics,
        "packets": packets,
        "media_proof": _media_proof(packets),
        "size": os.path.getsize(output_path),
        "fingerprint": str(sidecar["fingerprint"]),
    }


def _validate_existing_output(
    output_path: str,
    plan: dict[str, Any],
) -> int | None:
    """在线程中复核已有成品；文件不存在时返回 None。"""
    if not os.path.isfile(output_path):
        return None
    sidecar = _read_plan_sidecar(output_path)
    if not sidecar or sidecar.get("fingerprint") != plan["fingerprint"]:
        raise DownloadIntegrityError("现有成品缺少匹配的 HLS 计划指纹")
    _validate_with_ffprobe(
        output_path,
        plan,
        media_proof=sidecar.get("media_proof"),
    )
    return os.path.getsize(output_path)


def _prepare_download_files(
    output_path: str,
    plan: dict[str, Any],
    clean_start: bool,
) -> None:
    """在线程中准备目录、断点和 sidecar，避免慢盘阻塞事件循环。"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if clean_start:
        _discard_v2_resume(output_path)
    _prepare_plan_state(output_path, plan)


def _ffmpeg_failure(detail: str) -> DownloadError:
    """固定识别本地资源类失败；未知 ffmpeg 失败视为 staging 完整性错误。"""
    lowered = detail.casefold()
    if _is_local_file_busy(detail):
        return DownloadProcessingError(
            f"ffmpeg 输出文件暂时被占用: {detail or '未知错误'}")
    if any(marker in lowered for marker in (
        "no space left on device",
        "not enough space on the disk",
        "磁盘空间不足",
    )):
        return DownloadEnvironmentError(
            f"ffmpeg 写入失败，磁盘空间不足: {detail or '未知错误'}")
    return DownloadIntegrityError(
        f"完整 staging 无法封装，已标记为损坏: {detail or '未知错误'}")


def _remux_and_validate(
    source_path: str,
    output_path: str,
    plan: dict[str, Any],
) -> int:
    """用 ffmpeg 流复制封装为 MP4，经 staging→成品保真校验后原子提交。"""
    source_evidence = _validate_staging_with_ffprobe(source_path, plan)
    if plan["schema_version"] == 3:
        original_proof = (_read_plan_sidecar(output_path) or {}).get("media_proof")
        if not original_proof:
            raise DownloadIntegrityError("组合 staging 缺少各轨道合并证据")
        _validate_media_proof(source_evidence["packets"], original_proof, _timeline_tolerance(plan))
    processing_path = _processing_path(output_path)
    try:
        os.remove(processing_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DownloadProcessingError(
            f"旧 processing 文件暂时无法清理: {exc}") from exc
    cmd = [
        ffmpeg_path(), "-y", "-v", "error",
        # MPEG-TS 某条流可能有站点原生的静音间隙。默认 timestamp correction 会把
        # 这类间隙错误地加到其它流上，造成 MP4 时长膨胀；copyts + start_at_zero
        # 保留源时间轴并只做统一归零。
        "-copyts", "-start_at_zero",
        "-i", source_path,
        "-map", "0:v?",
        "-map", "0:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        processing_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_remux_timeout(source_path, plan),
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32" else 0
            ),
        )
    except FileNotFoundError as exc:
        raise DownloadEnvironmentError("未找到 ffmpeg，无法封装下载成品") from exc
    except OSError as exc:
        raise DownloadEnvironmentError(f"无法启动 ffmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadProcessingError("ffmpeg 封装超时") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[:400]
        raise _ffmpeg_failure(detail)

    output_evidence = _validate_with_ffprobe(
        processing_path, plan, source_evidence=source_evidence)
    try:
        _write_plan_sidecar(
            output_path,
            _plan_summary(
                plan,
                artifact_state="completed",
                media_proof=output_evidence["media_proof"],
            ),
        )
    except OSError as exc:
        raise DownloadProcessingError(f"无法保存成品保真证据: {exc}") from exc
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        os.replace(processing_path, output_path)
    except OSError as exc:
        raise DownloadProcessingError(
            f"成品原子提交暂时失败: {exc}") from exc
    if os.path.abspath(source_path) != os.path.abspath(output_path):
        try:
            os.remove(source_path)
        except OSError:
            # 成品已原子提交且校验通过；旧 staging 留给后续显式清理，不能翻转成功状态。
            pass
    return os.path.getsize(output_path)


def _worker_output(staging_path: str, info: dict[str, Any]) -> str | None:
    """定位 yt-dlp 后处理后的确定性输出，不接受分片/断点文件。"""
    candidates: list[str] = [staging_path]
    filepath = info.get("filepath")
    if filepath:
        candidates.append(str(filepath))
    for item in info.get("requested_downloads") or []:
        filepath = item.get("filepath")
        if filepath:
            candidates.append(str(filepath))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.abspath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        basename = os.path.basename(normalized)
        if basename.endswith((".part", ".ytdl")) or "-Frag" in basename:
            continue
        if os.path.isfile(normalized):
            return normalized
    return None


def _acquire_processing_slot(
    processing_ready: Any,
    stop_event: Any,
    events: Any,
) -> bool:
    """向父进程申请本地媒体处理租约，并可在排队时安全暂停。

    处理并发令牌只由父进程持有。worker 被 taskkill/看门狗强杀时，父协程仍能
    在 ``finally`` 归还租约；不能把 ``multiprocessing.Semaphore`` 直接交给
    worker 持有，否则 Windows 强杀不会自动恢复计数，后续任务会永久饿死。
    """
    if processing_ready is None:
        return False
    processing_ready.clear()
    _safe_queue_put(events, {"kind": "processing_waiting"})
    while not processing_ready.wait(timeout=0.25):
        if stop_event.is_set():
            raise _PauseRequested
    if stop_event.is_set():
        raise _PauseRequested
    return True


def _release_processing_slot(events: Any) -> None:
    """通知父进程归还当前 worker 的处理租约。"""
    _safe_queue_put(events, {"kind": "processing_released"})


def _composite_parts(plan):
    for track in plan["tracks"]:
        for index, part in enumerate(track["parts"]):
            yield track["kind"], index, part


def _component_path(output_path, kind, index):
    # kind/index 由组合计划结构生成；文件仍位于已锁定任务的同一目录。
    return os.path.splitext(output_path)[0] + f".native-v3-{kind}-{index:04d}.mp4"


class _ComponentEvents:
    def __init__(self, events, byte_offset, unit_offset, unit_count):
        self.events = events
        self.byte_offset = byte_offset
        self.unit_offset = unit_offset
        self.unit_count = unit_count

    def put(self, event):
        if event.get("kind") == "progress":
            event = {**event,
                     "downloaded_bytes": self.byte_offset + int(event.get("downloaded_bytes") or 0),
                     "total_bytes": 0,
                     "progress_hint": 100 * (self.unit_offset + int(event.get("fragment_index") or 0)) / self.unit_count}
        _safe_queue_put(self.events, event)


def _local_ffmpeg(args, path, plan):
    """仅处理已落盘的文件；组合/解码校验禁止 ffmpeg 打开网络协议。"""
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-y", "-v", "error", *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=_remux_timeout(path, plan),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    except FileNotFoundError as exc:
        raise DownloadEnvironmentError("未找到 ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadProcessingError("组合 HLS 本地处理超时") from exc
    except OSError as exc:
        raise DownloadProcessingError(f"组合 HLS 本地处理暂时失败：{exc}") from exc
    if result.returncode:
        raise _ffmpeg_failure(result.stderr.strip()[-400:])


def _component_evidence(path, part, kind, separated):
    probe = _ffprobe_json(path)
    packets = _scan_packet_timelines(path, probe)
    expected_kinds = [kind]
    if kind == "video" and not separated and packets["audio"]["stream_count"]:
        expected_kinds.append("audio")
    for stream_kind in ("video", "audio"):
        state = packets[stream_kind]
        if stream_kind in expected_kinds:
            if state["stream_count"] != 1 or state["packet_count"] <= 0:
                raise DownloadIntegrityError(f"HLS 时间段的 {stream_kind} 轨道数量或媒体包无效")
            if abs(state["duration"] - part["media_duration"]) > _timeline_tolerance(part):
                raise DownloadIntegrityError(f"HLS 时间段的 {stream_kind} 时间轴与冻结清单不符")
        elif state["stream_count"]:
            raise DownloadIntegrityError("HLS 分离轨道包含未声明的其它流")
    if part.get("expect_audio") and not packets["audio"]["packet_count"]:
        raise DownloadIntegrityError("HLS 时间段缺少清单声明的音频")
    return {"probe": probe, "media_proof": _media_proof(packets)}


def _build_composite_staging(plan, staging_path, output_path, config,
                             allow_network, stop_event, events, processing_ready, logger):
    """各段仍用同一个 HlsFD；只在声明的时间段边界做本地无损拼接。"""
    byte_offset = unit_offset = 0
    paths = {}
    for kind, index, part in _composite_parts(plan):
        if stop_event.is_set():
            raise _PauseRequested
        component = _component_path(output_path, kind, index)
        _prepare_plan_state(component, part)
        staging = _staging_path(component)
        if not os.path.isfile(staging):
            if not allow_network:
                raise DownloadProcessingError("本地组合重试缺少完整时间段，已拒绝网络重下")
            _download_native_hls(part, staging, config, stop_event,
                                 _ComponentEvents(events, byte_offset, unit_offset, plan["unit_count"]), logger)
        paths[kind, index] = staging
        byte_offset += os.path.getsize(staging)
        unit_offset += part["unit_count"]

    _safe_queue_put(events, {"kind": "phase", "phase": "processing"})
    acquired = _acquire_processing_slot(processing_ready, stop_event, events)
    try:
        separated = len(plan["tracks"]) == 2
        evidence = {}
        stream_layout = {}
        for kind, index, part in _composite_parts(plan):
            if stop_event.is_set():
                raise _PauseRequested
            item = _component_evidence(paths[kind, index], part, kind, separated)
            # H.264 的 MP4→Annex B 转换携带各段 SPS，允许源明确声明的分辨率变化。
            # 其它编码参数/音轨格式变化仍拒绝，不能用 stream copy 猜测兼容性。
            layout = []
            for stream in item["probe"].get("streams", []):
                if stream.get("codec_type") not in {"video", "audio"}:
                    continue
                keys = ["codec_type", "codec_name", "time_base", "sample_rate", "channels"]
                if stream.get("codec_name") != "h264":
                    keys += ["width", "height"]
                layout.append(tuple(stream.get(k) for k in keys))
            if kind in stream_layout and layout != stream_layout[kind]:
                raise DownloadIntegrityError("HLS 时间段编码或音轨格式变化，无法安全无损合并")
            stream_layout[kind] = layout
            evidence[kind, index] = item["media_proof"]

        period_paths = []
        total_proof = {f"{kind}_{field}": 0 for kind in ("video", "audio")
                       for field in ("stream_count", "packet_count", "duration")}
        for index, video_part in enumerate(plan["tracks"][0]["parts"]):
            if stop_event.is_set():
                raise _PauseRequested
            period = _component_path(output_path, "period", index)
            inputs = ["-copyts", "-start_at_zero", "-protocol_whitelist", "file",
                      "-i", paths["video", index]]
            proof = dict(evidence["video", index])
            if separated:
                inputs += ["-protocol_whitelist", "file", "-i", paths["audio", index],
                           "-map", "0:v:0", "-map", "1:a:0"]
                proof.update({k: v for k, v in evidence["audio", index].items() if k.startswith("audio_")})
            else:
                inputs += ["-map", "0:v:0", "-map", "0:a?"]
            _local_ffmpeg([*inputs, "-c", "copy", "-movflags", "+faststart", period],
                          paths["video", index], video_part)
            period_plan = {**video_part, "expect_audio": bool(proof["audio_stream_count"])}
            _validate_with_ffprobe(period, period_plan, media_proof=proof)
            period_paths.append(period)
            for key, value in proof.items():
                if key.endswith("stream_count"):
                    total_proof[key] = max(total_proof[key], value)
                else:
                    total_proof[key] += value

        concat_path = os.path.splitext(output_path)[0] + ".native-v3-concat.txt"
        joined = _component_path(output_path, "joined", 0)
        lines = ["ffconcat version 1.0"]
        for period, part in zip(period_paths, plan["tracks"][0]["parts"]):
            name = os.path.basename(period).replace("'", "'\\''")
            if "\n" in name or "\r" in name:
                raise DownloadIntegrityError("组合文件名包含换行")
            lines += [f"file '{name}'", f"duration {part['media_duration']:.6f}"]
        Path(concat_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        _local_ffmpeg(["-protocol_whitelist", "file", "-f", "concat", "-safe", "0",
                       "-auto_convert", "1", "-i", concat_path, "-map", "0:v:0", "-map", "0:a?",
                       "-c", "copy", "-movflags", "+faststart", joined], period_paths[0], plan)
        result = _validate_with_ffprobe(joined, plan, media_proof=total_proof)
        # 分辨率切换还要通过完整解码，防止仅包数正确但初始化参数不能衔接。
        _local_ffmpeg(["-xerror", "-err_detect", "explode", "-threads", "2",
                       "-protocol_whitelist", "file", "-i", joined,
                       "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], joined, plan)
        _write_plan_sidecar(output_path, _plan_summary(
            plan, artifact_state="assembled", media_proof=result["media_proof"]))
        os.replace(joined, staging_path)
    finally:
        if acquired:
            _release_processing_slot(events)


def _download_native_hls(plan, staging_path, worker_config, stop_event, events, logger):
    """唯一的网络下载实现，连续清单和组合计划共用固定版 HlsFD。"""
    import yt_dlp
    def progress_hook(data: dict[str, Any]) -> None:
        if stop_event.is_set():
            raise _PauseRequested
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
            )
            fragment_index = int(data.get("fragment_index") or 0)
            fragment_count = int(data.get("fragment_count") or 0)
            progress_hint = (
                fragment_index / fragment_count * 100.0
                if fragment_count > 0 else 0.0
            )
            _safe_queue_put(events, {
                "kind": "progress",
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "progress_hint": progress_hint,
                # 仅供诊断/UI；完整性由冻结清单 + HlsFD 全分片成功返回证明。
                "fragment_index": fragment_index,
                "fragment_count": fragment_count,
            })
        elif status == "finished":
            _safe_queue_put(events, {"kind": "phase", "phase": "processing"})

    def postprocessor_hook(data: dict[str, Any]) -> None:
        if stop_event.is_set():
            raise _PauseRequested
        if data.get("status") in {"started", "processing"}:
            _safe_queue_put(events, {"kind": "phase", "phase": "processing"})

    headers = dict(worker_config.get("headers") or {})
    ydl = None
    native_hls_confirmed = False
    native_hls_rejection = ""

    def require_native_hls(info: dict[str, Any], **_kwargs) -> str | None:
        """在任何网络下载发生前拒绝非 HLS 协议或非 HlsFD 选择结果。"""
        nonlocal native_hls_confirmed, native_hls_rejection
        # 首次预筛时 yt-dlp 尚未解析 protocol；只在完整格式信息阶段做硬判断。
        if _kwargs.get("incomplete"):
            return None
        protocol = str(info.get("protocol") or "").lower()
        if protocol not in {"m3u8", "m3u8_native"}:
            native_hls_rejection = (
                f"拒绝非原生 HLS 协议：{protocol or '未知'}")
            return native_hls_rejection
        try:
            from yt_dlp.downloader import get_suitable_downloader
            from yt_dlp.downloader.hls import HlsFD

            selected = get_suitable_downloader(info, ydl.params)
        except Exception as exc:
            native_hls_rejection = f"无法确认原生 HLS 下载器：{exc}"
            return native_hls_rejection
        if selected is not HlsFD and not (
                isinstance(selected, type) and issubclass(selected, HlsFD)):
            name = getattr(selected, "__name__", str(selected))
            native_hls_rejection = f"拒绝非原生 HLS 下载器：{name}"
            return native_hls_rejection
        native_hls_confirmed = True
        return None

    ydl_options: dict[str, Any] = {
        "outtmpl": {"default": staging_path},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "progress_delta": 0.25,
        "continuedl": bool(worker_config.get("continuedl", True)),
        "nopart": False,
        "overwrites": False,
        "skip_unavailable_fragments": False,
        "updatetime": False,
        "http_headers": headers,
        "socket_timeout": float(worker_config["socket_timeout"]),
        "fragment_retries": int(worker_config["fragment_retries"]),
        "retries": int(worker_config["fragment_retries"]),
        "retry_sleep_functions": {
            "fragment": _retry_delay,
            "http": _retry_delay,
        },
        "concurrent_fragment_downloads": int(worker_config["connections"]),
        **({"ratelimit": int(worker_config["ratelimit"])}
           if int(worker_config.get("ratelimit") or 0) > 0 else {}),
        "match_filter": require_native_hls,
        # 显式指定 native，禁止 yt-dlp 根据环境切换为 ffmpeg/aria2。
        "external_downloader": {"m3u8": "native"},
        "hls_prefer_native": True,
        "ffmpeg_location": worker_config["ffmpeg_location"],
        # 网络完成后统一由本模块执行一次可审计的 ffmpeg 封装与 ffprobe 校验。
        "fixup": "never",
        "postprocessors": [],
        # 固定 urllib 后端，确保所有重定向都经过上面的同步守卫。
        "compat_opts": {"prefer-legacy-http-handler"},
    }
    proxy = str(worker_config.get("proxy") or "").strip()
    if proxy:
        ydl_options["proxy"] = proxy

    _safe_queue_put(events, {"kind": "phase", "phase": "downloading"})
    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        _restrict_request_director_to_urllib(ydl)
        info = ydl.process_ie_result({
            "_type": "video",
            "id": str(plan["fingerprint"])[:16],
            "title": str(plan["fingerprint"])[:16],
            "url": str(plan["m3u8_url"]),
            "ext": "mp4",
            "protocol": "m3u8_native",
            "format_id": f"hls-{int(plan.get('quality') or 0)}p",
            "format": "hls-native",
            "is_live": False,
            "duration": float(plan["media_duration"]),
            "hls_media_playlist_data": str(plan["manifest_text"]),
            "http_headers": headers,
        }, download=True)
    if not isinstance(info, dict):
        raise DownloadIntegrityError("yt-dlp 未返回有效的媒体信息")
    if not native_hls_confirmed:
        raise DownloadIntegrityError(
            native_hls_rejection or "yt-dlp 未确认使用原生 HLS 下载器，已拒绝")

    _safe_queue_put(events, {"kind": "phase", "phase": "processing"})
    completed_path = _worker_output(staging_path, info)
    if not completed_path:
        raise DownloadIntegrityError("下载结束但未生成完整媒体文件")
    return completed_path


def _yt_dlp_worker(
    plan: dict[str, Any],
    staging_path: str,
    output_path: str,
    worker_config: dict[str, Any],
    allow_network: bool,
    ownership_ready: Any,
    stop_event: Any,
    events: Any,
    processing_ready: Any = None,
) -> None:
    """spawn 子进程入口；不得引用父进程事件循环或不可序列化对象。"""
    # 在父进程把 worker PID 原子写入输出锁前，子进程不得碰文件或网络。
    # 父进程若在 start→锁转移之间被强杀，等待超时后子进程无副作用退出。
    if not ownership_ready.wait(timeout=30.0):
        return
    if sys.platform != "win32":
        # 让 yt-dlp 后处理产生的 ffmpeg 与本进程处于独立进程组，便于整树回收。
        try:
            os.setsid()
        except OSError:
            pass

    logger = _YTDLPLogger()
    exit_code = 0
    ytdlp_error_type: tuple[type[BaseException], ...] = ()
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError as YTDLPDownloadError
        from yt_dlp.downloader.hls import HlsFD
        from yt_dlp.dependencies import Cryptodome
        from yt_dlp.networking import _urllib as ytdlp_urllib

        ytdlp_error_type = (YTDLPDownloadError,)
        plan = _validated_plan(plan)

        # HlsFD.real_download 会在 AES-128 且缺少 Cryptodome 时内部委派 FFmpegFD；
        # 这里在任何网络请求前硬失败，兑现“网络只走原生 HLS”约束。
        if bool(plan.get("encrypted")) and not getattr(Cryptodome, "AES", None):
            raise DownloadEnvironmentError(
                "AES-128 HLS 需要 Cryptodome.AES；已拒绝委派 ffmpeg 网络下载")

        # 固定清单必须由 HlsFD 本身支持，禁止 real_download 内部委派 FFmpegFD。
        leaves = ([part for _, _, part in _composite_parts(plan)]
                  if plan["schema_version"] == 3 else [plan])
        if any(not HlsFD.can_download(str(part["manifest_text"]), {"is_live": False}, False)
               for part in leaves):
            raise DownloadIntegrityError("冻结 HLS 清单包含原生下载器不支持的特性")

        original_redirect_handler = ytdlp_urllib.RedirectHandler

        class GuardedRedirectHandler(original_redirect_handler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                resolved = urljoin(req.full_url, newurl)
                _validate_worker_media_url(resolved, str(plan["m3u8_url"]))
                return super().redirect_request(
                    req, fp, code, msg, headers, resolved)

        # worker 进程隔离，锁定 urllib 后可在真正跟随重定向前执行私网边界检查。
        ytdlp_urllib.RedirectHandler = GuardedRedirectHandler

        version = getattr(getattr(yt_dlp, "version", None), "__version__", "")
        if version not in EXPECTED_YTDLP_VERSIONS:
            raise DownloadEnvironmentError(
                f"yt-dlp 版本不一致：需要 2026.6.9，当前为 {version or '未知'}"
            )

        if plan["schema_version"] == 3 and not os.path.isfile(staging_path):
            _build_composite_staging(plan, staging_path, output_path, worker_config,
                                     allow_network, stop_event, events, processing_ready, logger)

        # 崩溃若发生在“封装完成→原子提交”之间，优先校验并提交 processing 文件。
        processing_path = _processing_path(output_path)
        if os.path.isfile(processing_path):
            if stop_event.is_set():
                raise _PauseRequested
            _safe_queue_put(events, {"kind": "phase", "phase": "processing"})
            slot_acquired = _acquire_processing_slot(
                processing_ready, stop_event, events)
            try:
                try:
                    sidecar = _read_plan_sidecar(output_path) or {}
                    source_evidence = (
                        _validate_staging_with_ffprobe(staging_path, plan)
                        if os.path.isfile(staging_path) else None
                    )
                    output_evidence = _validate_with_ffprobe(
                        processing_path,
                        plan,
                        source_evidence=source_evidence,
                        media_proof=sidecar.get("media_proof"),
                    )
                except DownloadError:
                    if not os.path.isfile(staging_path):
                        raise
                    # 固定文件名重做本地封装，不再制造 .invalid-PID 副本。
                    try:
                        os.remove(processing_path)
                    except FileNotFoundError:
                        pass
                else:
                    os.makedirs(
                        os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                    try:
                        _write_plan_sidecar(
                            output_path,
                            _plan_summary(
                                plan,
                                artifact_state="completed",
                                media_proof=output_evidence["media_proof"],
                            ),
                        )
                        os.replace(processing_path, output_path)
                    except OSError as exc:
                        raise DownloadProcessingError(
                            f"已校验 processing 成品暂时无法提交: {exc}") from exc
                    if os.path.isfile(staging_path):
                        try:
                            os.remove(staging_path)
                        except OSError:
                            pass
                    _safe_queue_put(events, {
                        "kind": "completed",
                        "downloaded_bytes": os.path.getsize(output_path),
                    })
                    return
            finally:
                if slot_acquired:
                    _release_processing_slot(events)

        # staging 是网络下载完成、尚未可信封装的输入。无论扩展名为何都必须重新
        # 走 ffmpeg + ffprobe，禁止把 MPEG-TS 内容仅改成 .mp4 后直接上传。
        if os.path.isfile(staging_path):
            if stop_event.is_set():
                raise _PauseRequested
            _safe_queue_put(events, {"kind": "phase", "phase": "processing"})
            slot_acquired = _acquire_processing_slot(
                processing_ready, stop_event, events)
            try:
                size = _remux_and_validate(staging_path, output_path, plan)
            finally:
                if slot_acquired:
                    _release_processing_slot(events)
            _safe_queue_put(events, {
                "kind": "completed",
                "downloaded_bytes": size,
            })
            return

        if not allow_network:
            raise DownloadProcessingError(
                "本地处理重试缺少完整 staging，已拒绝退化为网络重下")

        completed_path = _download_native_hls(
            plan, staging_path, worker_config, stop_event, events, logger)
        slot_acquired = _acquire_processing_slot(
            processing_ready, stop_event, events)
        try:
            final_size = _remux_and_validate(completed_path, output_path, plan)
        finally:
            if slot_acquired:
                _release_processing_slot(events)
        _safe_queue_put(events, {
            "kind": "completed",
            "downloaded_bytes": final_size,
        })
    except BaseException as exc:
        if isinstance(exc, _PauseRequested):
            _safe_queue_put(events, {"kind": "paused_safe"})
            exc = None
        if exc is None:
            detail = ""
        else:
            exit_code = 1
            detail = str(exc).strip() or exc.__class__.__name__
        if exc is not None and logger.tail:
            logger_text = " | ".join(logger.tail)
            if logger_text not in detail:
                detail = f"{detail} | {logger_text}"
        stream_closed = False
        if exc is None:
            action = ""
            code = ""
        elif isinstance(exc, DownloadError):
            action = exc.action
            code = exc.code
        elif ytdlp_error_type and isinstance(exc, ytdlp_error_type):
            classified = _classify_ytdlp_download_error(exc, detail)
            action = classified.action
            code = classified.code
        elif _stream_closed_by_fragment_failure(exc, detail):
            # 按日志里的分片错误分类以便重试；但 HlsFD 追加分片的 finally 已把这个
            # 没写进去的分片记入 .ytdl，断点会缺片，所以下面不标记续传安全，
            # 下一次尝试会丢弃本次 staging 从头下载。
            classified = _classify_ytdlp_download_error(exc, detail)
            action = classified.action
            code = classified.code
            stream_closed = True
        else:
            action = DownloadError.action
            code = "unexpected_worker_error"
        resume_checkpoint = False
        if (exc is not None and allow_network and not stream_closed
                and action == NetworkDownloadError.action):
            # 能运行到外层 catch，说明固定版 HlsFD 已退出分片线程池并关闭写句柄。
            # 只有 sidecar/ytdl/part 全部证明位于安全 TS 分片边界时才允许续传。
            try:
                resume_checkpoint = _mark_resume_safe(output_path, plan)
            except (DownloadError, OSError, ValueError):
                resume_checkpoint = False
        lower_detail = detail.lower()
        no_data_stall = (action == "retry_network" and code != SourceUnavailableDownloadError.code
                        and any(marker in lower_detail for marker in (
            "incompleteread",
            "timed out",
            "timeout",
            "connection reset",
            "remote end closed",
        )))
        if exc is not None:
            _safe_queue_put(events, {
                "kind": "error",
                "error": detail[:1000],
                "no_data_stall": no_data_stall,
                "resume_checkpoint": resume_checkpoint,
                "action": action,
                "code": code,
            })
    finally:
        # 正常结束时等待队列 feeder 刷完最终事件；被父进程硬停时由父端关闭。
        try:
            events.close()
            events.join_thread()
        except (AttributeError, OSError, ValueError):
            pass
    if exit_code:
        raise SystemExit(exit_code)


def _rolling_speed(
    samples: deque[tuple[float, int]],
    now: float,
    current_bytes: int,
) -> int:
    """按最近五秒真实字节增量计算速度，空窗会自然衰减。"""
    cutoff = now - SPEED_WINDOW_SECONDS
    while len(samples) > 1 and samples[1][0] <= cutoff:
        samples.popleft()
    if not samples:
        return 0
    started_at, started_bytes = samples[0]
    # 窗口边界落在两个采样点之间时，线性插值得到边界字节数。
    if started_at < cutoff and len(samples) > 1:
        next_at, next_bytes = samples[1]
        if next_at > started_at:
            ratio = min(1.0, max(0.0, (cutoff - started_at) / (next_at - started_at)))
            started_bytes = round(started_bytes + (next_bytes - started_bytes) * ratio)
            started_at = cutoff
    elapsed = now - started_at
    if elapsed <= 0:
        return 0
    return max(0, round((current_bytes - started_bytes) / elapsed))


def _worker_failure(action: str, code: str, message: str) -> DownloadError:
    if code == SourceUnavailableDownloadError.code:
        return SourceUnavailableDownloadError(message)
    if code == RateLimitedDownloadError.code:
        return RateLimitedDownloadError(message)
    if action == NetworkDownloadError.action:
        return NetworkDownloadError(message)
    if action == DownloadArtifactError.action or code == DownloadArtifactError.code:
        return DownloadArtifactError(message)
    if action == DownloadProcessingError.action:
        return DownloadProcessingError(message)
    if code == DownloadEnvironmentError.code:
        return DownloadEnvironmentError(message)
    if code == DownloadIntegrityError.code:
        return DownloadIntegrityError(message)
    return DownloadError(message)


def _process_alive(process: multiprocessing.Process) -> bool:
    try:
        return process.is_alive()
    except (AssertionError, ValueError):
        return False


def _terminate_process_tree(process: multiprocessing.Process) -> None:
    """同步硬停 worker 及其 ffmpeg 后代，并等待 OS 完成回收。"""
    if not _process_alive(process):
        try:
            process.join(timeout=0.2)
        except (AssertionError, ValueError):
            pass
        return

    pid = process.pid
    if pid and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif pid:
        # worker 只有在 setsid 成功后才拥有 PID 同名的独立进程组。若 setsid
        # 失败，直接 killpg(pid) 可能误伤父进程所在组，必须退回单进程终止。
        try:
            isolated_group = os.getpgid(pid) == pid
        except (ProcessLookupError, PermissionError, OSError):
            isolated_group = False
        if isolated_group:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                isolated_group = False
        if not isolated_group:
            try:
                process.terminate()
            except (ProcessLookupError, AttributeError):
                pass
        process.join(timeout=2)
        if _process_alive(process):
            if isolated_group:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    isolated_group = False
            if not isolated_group:
                try:
                    process.kill()
                except (ProcessLookupError, AttributeError):
                    pass

    process.join(timeout=3)
    if _process_alive(process):
        try:
            process.kill()
        except (ProcessLookupError, AttributeError):
            pass
        process.join(timeout=2)
class M3U8Downloader:
    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._processing_limit = max(
            1,
            min(
                download_concurrency_limit(),
                int(getattr(settings, "media_processing_concurrency", 2)),
            ),
        )
        # 网络 worker 可并发下载，但 ffprobe 全包扫描 + ffmpeg remux 会同时争用
        # 磁盘和内存带宽。令牌由父事件循环持有，worker 只拿一次性租约；这样即使
        # Windows taskkill 强杀 worker，父协程的 finally 仍能确定归还令牌。
        self._processing_loop: asyncio.AbstractEventLoop | None = None
        self._processing_sem: asyncio.Semaphore | None = None
        self._procs: dict[str, multiprocessing.Process] = {}
        self._queues: dict[str, Any] = {}
        self._controls: dict[str, Any] = {}
        self._path_owners: dict[str, str] = {}
        self._lock_tokens: dict[str, tuple[str, str]] = {}

    def _processing_limiter(self) -> asyncio.Semaphore:
        """返回当前事件循环的父进程处理门限。

        生产环境只有一个 FastAPI 事件循环；测试可能顺序创建多个循环。只要没有
        活跃 worker，就允许在新循环上重建 semaphore，避免 asyncio 原语跨循环。
        """
        loop = asyncio.get_running_loop()
        if self._processing_loop is not loop:
            if self._procs:
                raise DownloadEnvironmentError(
                    "下载器不能在另一个事件循环中接管仍活跃的 worker")
            self._processing_loop = loop
            self._processing_sem = asyncio.Semaphore(self._processing_limit)
        assert self._processing_sem is not None
        return self._processing_sem

    @staticmethod
    def _config(
        attempt: int = 1,
        *,
        conservative: bool = False,
    ) -> dict[str, Any]:
        # 每任务为清单/探测保留1条连接，剩余预算交给原生 HLS 分片线程。
        # 首轮追求吞吐，保守模式固定单连接；是否丢弃旧断点由 clean_start
        # 单独决定，因此保守模式本身必须继续允许续传。
        fragment_workers = 1 if conservative else fragment_workers_for_attempt(attempt)
        return {
            "proxy": str(getattr(settings, "download_proxy", "") or ""),
            "connections": fragment_workers,
            "ratelimit": fragment_rate_limit(fragment_workers),
            "socket_timeout": max(
                1.0, float(getattr(settings, "download_socket_timeout", 10.0))
            ),
            "fragment_retries": max(
                0, int(getattr(settings, "download_fragment_retries", 5))
            ),
            "ffmpeg_location": ffmpeg_path(),
            "headers": dict(HEADERS),
            "continuedl": True,
        }

    async def _notify(
        self,
        callback: ProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        result = callback(payload)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _resume_size(staging_path: str) -> int:
        try:
            return os.path.getsize(staging_path + ".part")
        except OSError:
            return 0

    async def download(
        self,
        plan: dict[str, Any],
        output_path: str,
        on_progress: ProgressCallback | None = None,
        task_key: str = "",
        attempt: int = 1,
        allow_network: bool = True,
        clean_start: bool = False,
        conservative: bool = False,
    ) -> str:
        """下载 HLS 并返回最终路径。

        ``on_progress`` 接收单个字典，包含 phase/progress/speed_bps/
        downloaded_bytes/total_bytes/last_progress_at。取消和失败不会清除断点数据。
        """
        plan = _validated_plan(plan)
        output_path = os.path.abspath(output_path)
        processing_sem = self._processing_limiter()
        # 不存在成品时无需占用本地处理槽；网络 worker 可以继续并发下载，只有
        # 真正的 ffprobe/ffmpeg 阶段受门限约束。
        output_exists = await asyncio.to_thread(os.path.isfile, output_path)
        if output_exists:
            async with processing_sem:
                size = await asyncio.to_thread(
                    _validate_existing_output,
                    output_path,
                    plan,
                )
        else:
            size = None
        if size is not None:
            await self._notify(on_progress, {
                "phase": "completed",
                "progress": 100.0,
                "speed_bps": 0,
                "downloaded_bytes": size,
                "total_bytes": size,
                "last_progress_at": None,
            })
            return output_path

        key = task_key or f"anonymous-{id(asyncio.current_task())}"
        current = self._procs.get(key)
        if current and _process_alive(current):
            raise DownloadError(f"下载任务 {key} 已在运行")
        path_owner = self._path_owners.get(output_path)
        if path_owner and path_owner != key:
            raise DownloadError(f"同一输出文件正由任务 {path_owner} 写入")

        lock_token = await asyncio.to_thread(
            _acquire_output_lock, output_path, key)
        self._path_owners[output_path] = key
        self._lock_tokens[key] = (output_path, lock_token)
        events = None
        stop_event = None
        ownership_ready = None
        process = None
        try:
            staging_path = _staging_path(output_path)
            # 只删除这个输出路径的 v2 staging/断点/sidecar；最终 MP4 和其它
            # 任务目录不在删除范围内。随后单连接从第一个分片重新开始。
            await asyncio.to_thread(
                _prepare_download_files,
                output_path,
                plan,
                clean_start,
            )
            config = self._config(
                attempt,
                conservative=(clean_start or conservative),
            )
            waiting_timeout = max(
                0.1, float(getattr(settings, "download_waiting_timeout", 3.0))
            )
            stall_timeout = max(
                waiting_timeout + 0.1,
                float(getattr(settings, "download_stall_timeout", 60.0)),
            )

            events = self._context.Queue()
            stop_event = self._context.Event()
            ownership_ready = self._context.Event()
            processing_ready = self._context.Event()
            process = self._context.Process(
                target=_yt_dlp_worker,
                args=(
                    plan, staging_path, output_path, config, bool(allow_network),
                    ownership_ready, stop_event, events, processing_ready,
                ),
                name=f"hls-{key}",
                daemon=False,
            )
            process.start()
            await asyncio.to_thread(
                _assign_output_lock_to_worker,
                output_path,
                lock_token,
                key,
                int(process.pid or 0),
            )
            self._procs[key] = process
            self._queues[key] = events
            self._controls[key] = stop_event
            ownership_ready.set()
        except BaseException:
            if process is not None and _process_alive(process):
                await asyncio.to_thread(_terminate_process_tree, process)
            try:
                if events is not None:
                    events.cancel_join_thread()
                    events.close()
            except (AttributeError, OSError, ValueError):
                pass
            try:
                if process is not None:
                    process.close()
            except (AttributeError, ValueError):
                pass
            if self._path_owners.get(output_path) == key:
                self._path_owners.pop(output_path, None)
            if self._procs.get(key) is process:
                self._procs.pop(key, None)
                self._queues.pop(key, None)
                self._controls.pop(key, None)
            self._lock_tokens.pop(key, None)
            await asyncio.to_thread(
                _release_output_lock, output_path, lock_token)
            raise
        started_at = time.monotonic()
        resume_bytes = await asyncio.to_thread(self._resume_size, staging_path)
        processing_timeout: float | None = None
        downloaded_bytes = resume_bytes
        total_bytes = 0
        last_byte_at = started_at
        last_progress_at: str | None = None
        samples: deque[tuple[float, int]] = deque([(started_at, resume_bytes)])
        # spawn 返回只代表子进程已创建；子进程仍需加载运行时。真正收到 worker 的
        # downloading 事件前继续显示“解析中”，避免把进程冷启动误报成下载中零速。
        phase = "resolving"
        waiting_sent = False
        completed = False
        worker_error = ""
        worker_no_data_stall = False
        worker_action = DownloadError.action
        worker_code = DownloadError.code
        worker_paused_safe = False
        last_emit_at = 0.0
        last_emitted_phase = ""
        progress_hint = 0.0
        exit_seen_at: float | None = None
        processing_started_at: float | None = None
        processing_acquire_task: asyncio.Task | None = None
        processing_slot_held = False

        def release_processing_slot() -> None:
            """父进程幂等归还已授予 worker 的处理租约。"""
            nonlocal processing_slot_held, processing_started_at
            if not processing_slot_held:
                return
            processing_slot_held = False
            processing_started_at = None
            processing_ready.clear()
            processing_sem.release()

        def cleanup_processing_lease() -> None:
            """同步登记回收，避免重复取消穿透 finally 后泄漏父进程租约。"""
            nonlocal processing_acquire_task
            acquire_task = processing_acquire_task
            processing_acquire_task = None
            if acquire_task is not None:
                def release_if_acquired(done: asyncio.Task) -> None:
                    try:
                        acquired = done.result()
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        return
                    if acquired is True:
                        processing_sem.release()

                if acquire_task.done():
                    release_if_acquired(acquire_task)
                else:
                    # acquire() 与 cancel 的胜者由 Task 结果决定；若前者已成功，
                    # done callback 归还令牌；若后者胜出，Semaphore 自己恢复计数。
                    acquire_task.add_done_callback(release_if_acquired)
                    acquire_task.cancel()
            release_processing_slot()

        async def emit(now: float, *, force: bool = False) -> None:
            nonlocal last_emit_at, last_emitted_phase
            phase_changed = phase != last_emitted_phase
            if not force and not phase_changed and now - last_emit_at < PROGRESS_EMIT_INTERVAL:
                return
            if total_bytes > 0:
                progress = downloaded_bytes / total_bytes * 100.0
            else:
                progress = progress_hint
            progress = round(max(0.0, min(99.0, progress)), 1)
            speed = 0 if phase != "downloading" else _rolling_speed(
                samples, now, downloaded_bytes
            )
            await self._notify(on_progress, {
                "phase": phase,
                "progress": progress,
                "speed_bps": speed,
                "downloaded_bytes": downloaded_bytes,
                "total_bytes": total_bytes,
                "last_progress_at": last_progress_at,
            })
            last_emit_at = now
            last_emitted_phase = phase

        try:
            await emit(started_at, force=True)
            while True:
                now = time.monotonic()
                received = False
                while True:
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        break
                    except (EOFError, OSError, ValueError):
                        break
                    received = True
                    kind = event.get("kind")
                    if kind == "progress":
                        reported = max(0, int(event.get("downloaded_bytes") or 0))
                        if reported > downloaded_bytes:
                            first_new_bytes = downloaded_bytes <= resume_bytes
                            downloaded_bytes = reported
                            samples.append((now, downloaded_bytes))
                            last_byte_at = now
                            last_progress_at = _utc_now_iso()
                            waiting_sent = False
                            phase = "downloading"
                            # 小型/本地高速HLS可能在同一轮队列中紧接着报告 processing；
                            # 第一批真实字节必须先发一次，不能让页面整段下载始终显示0速。
                            if first_new_bytes:
                                await emit(now, force=True)
                        total_bytes = max(
                            total_bytes, int(event.get("total_bytes") or 0)
                        )
                        progress_hint = max(
                            progress_hint, float(event.get("progress_hint") or 0.0)
                        )
                    elif kind == "phase":
                        new_phase = str(event.get("phase") or "")
                        if new_phase == "processing":
                            # 这里只更新 UI。看门狗必须等父进程真正授予处理租约后
                            # 才开始，排队等槽不能被误算成本地处理停滞。
                            phase = new_phase
                        elif new_phase == "downloading":
                            # worker 就绪不等于已有网络字节；首批真实字节会切换阶段，
                            # 使 WebSocket 与节流后的REST快照都得到一次非零速度。
                            if phase == "processing":
                                last_byte_at = now
                                waiting_sent = False
                            if downloaded_bytes > resume_bytes:
                                phase = new_phase
                    elif kind == "processing_waiting":
                        # 同一 worker 若开始第二段处理，按队列顺序上一条 released
                        # 应已先到；这里防御性归还，避免单条通知异常造成自锁。
                        release_processing_slot()
                        if (processing_acquire_task is None
                                or processing_acquire_task.done()):
                            processing_acquire_task = asyncio.create_task(
                                processing_sem.acquire())
                    elif kind == "processing_released":
                        release_processing_slot()
                    elif kind == "completed":
                        release_processing_slot()
                        completed = True
                        downloaded_bytes = max(
                            downloaded_bytes,
                            int(event.get("downloaded_bytes") or 0),
                        )
                    elif kind == "error":
                        # worker 协议会先发 released；异常路径再兜底，租约只在父进程
                        # 归还一次，不依赖子进程 finally 能否跑完。
                        release_processing_slot()
                        worker_error = str(event.get("error") or "yt-dlp 下载失败")
                        worker_no_data_stall = bool(event.get("no_data_stall"))
                        worker_action = str(event.get("action") or DownloadError.action)
                        worker_code = str(event.get("code") or DownloadError.code)
                    elif kind == "paused_safe":
                        worker_paused_safe = True

                if (processing_acquire_task is not None
                        and processing_acquire_task.done()):
                    acquire_task = processing_acquire_task
                    processing_acquire_task = None
                    try:
                        acquired = acquire_task.result()
                    except asyncio.CancelledError:
                        acquired = False
                    if acquired:
                        if stop_event.is_set() or not _process_alive(process):
                            processing_sem.release()
                        else:
                            processing_slot_held = True
                            processing_started_at = now
                            # worker 刚下载完时 staging 才达到最终体积；在真正取得
                            # 租约的这一刻重算超时，且重复处理只会放宽不会缩短。
                            current_timeout = await asyncio.to_thread(
                                _processing_watchdog_for_artifacts,
                                staging_path,
                                output_path,
                                plan,
                            )
                            processing_timeout = max(
                                float(processing_timeout or 0),
                                current_timeout,
                            )
                            processing_ready.set()

                if completed:
                    final_size = await asyncio.to_thread(
                        _safe_file_size, output_path)
                    if final_size <= 0:
                        raise DownloadIntegrityError("子进程报告完成但最终文件不存在")
                    await self._notify(on_progress, {
                        "phase": "completed",
                        "progress": 100.0,
                        "speed_bps": 0,
                        "downloaded_bytes": final_size,
                        # 估算总量只用于下载中；完成后以磁盘成品为唯一真值。
                        "total_bytes": final_size,
                        "last_progress_at": last_progress_at,
                    })
                    return output_path
                defer_worker_error = (
                    worker_error
                    and worker_no_data_stall
                    and worker_action == NetworkDownloadError.action
                    and downloaded_bytes <= resume_bytes
                )
                if worker_error and not defer_worker_error:
                    failure = _worker_failure(worker_action, worker_code, worker_error)
                    if isinstance(failure, DownloadIntegrityError):
                        await asyncio.to_thread(
                            _mark_plan_artifact_invalid, output_path, plan)
                    raise failure

                alive = _process_alive(process)
                if not alive:
                    if worker_paused_safe:
                        await asyncio.to_thread(
                            _mark_resume_safe, output_path, plan)
                        raise asyncio.CancelledError
                    if defer_worker_error:
                        # worker 的固定分片重试可能早于60秒耗尽；零字节断流仍由
                        # 父看门狗统一在 stall_timeout 结算，保证槽位释放时点确定。
                        exit_seen_at = None
                    else:
                        # 正常退出时 multiprocessing.Queue 的 feeder 可能稍晚一拍可见。
                        exit_seen_at = exit_seen_at or now
                        if now - exit_seen_at >= 0.5:
                            code = process.exitcode
                            raise DownloadError(
                                f"yt-dlp 子进程异常退出(code={code})，未返回完成事件"
                            )
                else:
                    exit_seen_at = None

                if phase in {"resolving", "downloading", "waiting"}:
                    idle = now - last_byte_at
                    if idle >= stall_timeout:
                        await asyncio.to_thread(_terminate_process_tree, process)
                        raise NetworkDownloadError(
                            f"下载连续 {stall_timeout:g} 秒无字节进展，已终止停滞进程"
                        )
                    if idle >= waiting_timeout and not waiting_sent:
                        phase = "waiting"
                        waiting_sent = True
                        await emit(now, force=True)
                elif (
                    phase == "processing"
                    and processing_started_at is not None
                    and processing_timeout is not None
                    and now - processing_started_at >= processing_timeout
                ):
                    await asyncio.to_thread(_terminate_process_tree, process)
                    raise DownloadProcessingError(
                        f"本地封装或校验超过 {processing_timeout:g} 秒，已终止进程"
                    )

                if received:
                    await emit(now)
                elif now - last_emit_at >= PROGRESS_EMIT_INTERVAL:
                    # 即使没有 hook 事件也定时刷新，使滚动速度不会停留在旧值。
                    await emit(now, force=True)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # 先请求 HlsFD 在网络进度/分片边界协作退出；只有收到确认且 worker
            # 已自然结束，才把 journal 标为可续传。超时强杀的断点保持 unsafe。
            stop_event.set()
            safe_pause = worker_paused_safe
            deadline = time.monotonic() + min(
                15.0, max(3.0, float(config["socket_timeout"]) + 2.0))
            while _process_alive(process) and time.monotonic() < deadline:
                while True:
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        break
                    except (EOFError, OSError, ValueError):
                        break
                    if event.get("kind") == "paused_safe":
                        safe_pause = True
                await asyncio.sleep(0.05)
            if _process_alive(process):
                await asyncio.to_thread(_terminate_process_tree, process)
                safe_pause = False
            else:
                while True:
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        break
                    except (EOFError, OSError, ValueError):
                        break
                    if event.get("kind") == "paused_safe":
                        safe_pause = True
            if safe_pause:
                await asyncio.to_thread(_mark_resume_safe, output_path, plan)
            raise
        finally:
            try:
                if _process_alive(process):
                    await asyncio.to_thread(_terminate_process_tree, process)
            finally:
                # worker 可能在持有租约时被 taskkill，或在 acquire() 成功与主循环
                # 登记 held 的窄边界被反复取消；同步回收登记不可被再次 cancel 穿透。
                cleanup_processing_lease()

            def _close_process() -> None:
                try:
                    process.join(timeout=0.5)
                    process.close()
                except (AssertionError, AttributeError, ValueError):
                    pass

            await asyncio.to_thread(_close_process)
            if self._procs.get(key) is process:
                self._procs.pop(key, None)
                self._queues.pop(key, None)
                self._controls.pop(key, None)
            try:
                events.cancel_join_thread()
                events.close()
            except (AttributeError, OSError, ValueError):
                pass
            if self._path_owners.get(output_path) == key:
                self._path_owners.pop(output_path, None)
            self._lock_tokens.pop(key, None)
            await asyncio.to_thread(
                _release_output_lock, output_path, lock_token)

    def cancel(self, task_key: str) -> None:
        """请求 worker 在安全分片边界退出；调用方随后取消父协程并等待结算。"""
        control = self._controls.get(task_key)
        if control is not None:
            control.set()

    def shutdown(self) -> None:
        """应用退出时同步回收全部 worker，不留下 yt-dlp/ffmpeg 孤儿进程。"""
        for control in list(self._controls.values()):
            control.set()
        for process in list(self._procs.values()):
            _terminate_process_tree(process)
            try:
                process.close()
            except (AttributeError, ValueError):
                pass
        self._procs.clear()
        self._controls.clear()
        for events in self._queues.values():
            try:
                events.cancel_join_thread()
                events.close()
            except (AttributeError, OSError, ValueError):
                pass
        self._queues.clear()
        for output_path, token in list(self._lock_tokens.values()):
            _release_output_lock(output_path, token)
        self._lock_tokens.clear()
        self._path_owners.clear()

    def cleanup_partials(self, output_path: str) -> None:
        """显式删除一个任务的新旧续传数据；仅应在用户删除任务时调用。"""
        output_path = os.path.abspath(output_path)
        for pattern in _partial_artifact_patterns(output_path):
            for candidate in glob.glob(pattern):
                try:
                    path = Path(candidate)
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                except OSError:
                    pass


downloader = M3U8Downloader()
