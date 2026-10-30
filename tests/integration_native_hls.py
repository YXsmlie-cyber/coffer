"""本地原生 HLS 实链路验收；不访问外网、不写入项目下载目录。"""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings
from backend.downloader import (
    DownloadError,
    M3U8Downloader,
    NetworkDownloadError,
    _validate_with_ffprobe,
    downloader,
    ffmpeg_path,
    ffprobe_path,
)
from backend.scraper import HLSPlanError, resolve_hls_plan


class SlowFixtureHandler(SimpleHTTPRequestHandler):
    metric_lock = threading.Lock()
    active_segments = 0
    max_active_segments = 0
    active_request_paths = {}
    max_request_paths = []
    request_counts = {}
    checkpoint_failures_remaining = 1

    def log_message(self, *_args):
        pass

    def do_GET(self):
        request_path = self.path.split("?", 1)[0]
        with self.metric_lock:
            type(self).request_counts[request_path] = (
                type(self).request_counts.get(request_path, 0) + 1
            )
        if request_path.endswith("/checkpoint-fail.ts"):
            with self.metric_lock:
                should_fail = type(self).checkpoint_failures_remaining > 0
                if should_fail:
                    type(self).checkpoint_failures_remaining -= 1
            if should_fail:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if request_path.endswith("/stall.ts"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(1024 * 1024))
            self.end_headers()
            time.sleep(10)
            return
        super().do_GET()

    def copyfile(self, source, outputfile):
        is_segment = self.path.split("?", 1)[0].endswith(".ts")
        if is_segment:
            with self.metric_lock:
                type(self).active_segments += 1
                type(self).active_request_paths[id(self)] = self.path
                type(self).max_active_segments = max(
                    type(self).max_active_segments,
                    type(self).active_segments,
                )
                if type(self).active_segments == type(self).max_active_segments:
                    type(self).max_request_paths = list(
                        type(self).active_request_paths.values())
        try:
            while chunk := source.read(4096):
                outputfile.write(chunk)
                outputfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            if is_segment:
                with self.metric_lock:
                    type(self).active_segments -= 1
                    type(self).active_request_paths.pop(id(self), None)


def build_fixture(directory: Path) -> None:
    subprocess.run([
        ffmpeg_path(), "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "24", "-keyint_min", "24", "-sc_threshold", "0",
        "-c:a", "aac", "-f", "hls", "-hls_time", "1",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(directory / "segment-%03d.ts"),
        str(directory / "fixture.m3u8"),
    ], check=True, capture_output=True)
    (directory / "stall.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.0,\nstall.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    (directory / "quality-master.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180,"
        'CODECS="avc1.42e01e,mp4a.40.2"\n'
        "fixture.m3u8\n",
        encoding="utf-8",
    )
    (directory / "master.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=160x90,"
        'CODECS="avc1.42e01e,mp4a.40.2"\n'
        "fixture.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180,"
        'CODECS="avc1.42e01e,mp4a.40.2"\n'
        "quality-master.m3u8\n",
        encoding="utf-8",
    )
    fixture_text = (directory / "fixture.m3u8").read_text(encoding="utf-8")
    media_names = [
        line.strip() for line in fixture_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(media_names) < 3:
        raise AssertionError("fixture 至少需要三个 HLS 分片")
    shutil.copyfile(directory / media_names[2], directory / "checkpoint-fail.ts")
    (directory / "checkpoint.m3u8").write_text(
        fixture_text.replace(media_names[2], "checkpoint-fail.ts", 1),
        encoding="utf-8",
    )


async def run_checks(base_url: str, directory: Path, full_stall: bool = False) -> dict:
    plan = await resolve_hls_plan(f"{base_url}/master.m3u8")
    stall_plan = await resolve_hls_plan(f"{base_url}/stall.m3u8")
    checkpoint_plan = await resolve_hls_plan(f"{base_url}/checkpoint.m3u8")
    assert plan["m3u8_url"].endswith("/fixture.m3u8"), plan["m3u8_url"]
    assert plan["quality"] == 180, plan["quality"]
    assert plan["segment_count"] >= 2, plan["segment_count"]
    assert plan["media_duration"] < 20, plan["media_duration"]
    with SlowFixtureHandler.metric_lock:
        frozen_leaf_requests = SlowFixtureHandler.request_counts.get(
            "/fixture.m3u8", 0)
    normal_events = []
    normal_started = time.monotonic()

    async def on_normal(event):
        normal_events.append((time.monotonic() - normal_started, dict(event)))

    output = directory / "normal.mp4"
    await downloader.download(
        plan, str(output),
        on_progress=on_normal, task_key="integration-normal")
    probe = subprocess.run([
        ffprobe_path(), "-v", "error", "-show_entries", "stream=codec_type",
        "-of", "json", str(output),
    ], check=True, capture_output=True, text=True, encoding="utf-8")
    streams = json.loads(probe.stdout).get("streams") or []
    assert any(item.get("codec_type") == "video" for item in streams)
    first_nonzero = min(
        elapsed for elapsed, item in normal_events
        if float(item.get("speed_bps") or 0) > 0
    )
    assert first_nonzero <= 3.0, first_nonzero
    assert normal_events[-1][1]["phase"] == "completed", normal_events[-1]
    with SlowFixtureHandler.metric_lock:
        assert SlowFixtureHandler.request_counts.get(
            "/fixture.m3u8", 0) == frozen_leaf_requests
    wrong_timeline = dict(plan, media_duration=999.0)
    try:
        _validate_with_ffprobe(str(output), wrong_timeline)
    except DownloadError as exc:
        assert "时间轴" in str(exc), exc
    else:
        raise AssertionError("截断/时长不符成品未被拒绝")
    _validate_with_ffprobe(str(output), plan)

    # 非 HLS 响应必须在计划解析阶段被拒绝，不能退化到 HttpFD。
    try:
        await resolve_hls_plan(f"{base_url}/normal.mp4")
    except HLSPlanError:
        pass
    else:
        raise AssertionError("非 HLS 直链未被拒绝")

    # 模拟暂停后应用重启：取消会杀完整 worker 树但保留断点；新下载器实例
    # 必须只走同一原生 HLS 引擎继续并产出可校验文件。
    resume_output = directory / "resume.mp4"
    partial_ready = asyncio.Event()

    async def on_first_attempt(event):
        if int(event.get("downloaded_bytes") or 0) > 0:
            partial_ready.set()

    interrupted = asyncio.create_task(downloader.download(
        plan, str(resume_output),
        on_progress=on_first_attempt, task_key="integration-resume-first"))
    await asyncio.wait_for(partial_ready.wait(), timeout=10)
    interrupted.cancel()
    try:
        await interrupted
    except asyncio.CancelledError:
        pass
    partials = list(directory.glob("resume.native*"))
    assert partials, "暂停后没有保留原生 HLS 断点文件"
    sidecar = json.loads((
        directory / "resume.native-v2.mp4.hls-plan.json"
    ).read_text(encoding="utf-8"))
    assert sidecar.get("resume_safe") is True, sidecar
    assert not downloader._procs, downloader._procs

    restarted_downloader = M3U8Downloader()
    try:
        await restarted_downloader.download(
            plan, str(resume_output),
            task_key="integration-resume-after-restart")
    finally:
        restarted_downloader.shutdown()
    _validate_with_ffprobe(str(resume_output), plan)
    assert not restarted_downloader._procs

    # 固定版 HlsFD 在网络错误自然退栈后，其 .part + .ytdl 位于完整 TS 分片边界。
    # 首次让第3段快速503，第二次恢复后前两段不得再次请求，证明不是伪续传或整条重下。
    checkpoint_output = directory / "network-checkpoint.mp4"
    checkpoint_media = [
        line.strip() for line in checkpoint_plan["manifest_text"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    first_checkpoint_path = "/" + checkpoint_media[0].split("?", 1)[0]
    with SlowFixtureHandler.metric_lock:
        SlowFixtureHandler.checkpoint_failures_remaining = 1
        first_before = SlowFixtureHandler.request_counts.get(first_checkpoint_path, 0)
    old_fragment_retries = settings.download_fragment_retries
    old_checkpoint_budget = settings.download_max_connections
    old_checkpoint_tasks = settings.max_concurrent_downloads
    old_checkpoint_cap = settings.download_max_fragment_workers_per_task
    settings.download_fragment_retries = 0
    settings.download_max_connections = 2
    settings.max_concurrent_downloads = 1
    settings.download_max_fragment_workers_per_task = 1
    checkpoint_downloader = M3U8Downloader()
    try:
        try:
            await checkpoint_downloader.download(
                checkpoint_plan, str(checkpoint_output),
                task_key="integration-network-checkpoint-first",
            )
        except NetworkDownloadError:
            pass
        else:
            raise AssertionError("分片503未被分类为自然网络失败")
    finally:
        checkpoint_downloader.shutdown()
    checkpoint_sidecar = json.loads((
        directory / "network-checkpoint.native-v2.mp4.hls-plan.json"
    ).read_text(encoding="utf-8"))
    assert checkpoint_sidecar.get("resume_safe") is True, checkpoint_sidecar
    with SlowFixtureHandler.metric_lock:
        first_after_failure = SlowFixtureHandler.request_counts.get(
            first_checkpoint_path, 0)
    assert first_after_failure == first_before + 1, (
        first_before, first_after_failure, first_checkpoint_path)

    checkpoint_restarted = M3U8Downloader()
    try:
        await checkpoint_restarted.download(
            checkpoint_plan, str(checkpoint_output),
            task_key="integration-network-checkpoint-resume",
        )
    finally:
        checkpoint_restarted.shutdown()
        settings.download_fragment_retries = old_fragment_retries
        settings.download_max_connections = old_checkpoint_budget
        settings.max_concurrent_downloads = old_checkpoint_tasks
        settings.download_max_fragment_workers_per_task = old_checkpoint_cap
    _validate_with_ffprobe(str(checkpoint_output), checkpoint_plan)
    with SlowFixtureHandler.metric_lock:
        first_after_resume = SlowFixtureHandler.request_counts.get(
            first_checkpoint_path, 0)
    assert first_after_resume == first_after_failure, (
        first_after_failure, first_after_resume, first_checkpoint_path)

    # 提高到总预算60后，五任务首轮应使用更多原生 HLS 分片线程；服务端实测
    # 峰值必须高于旧预算15，同时仍不得突破新总预算。
    drain_deadline = time.monotonic() + 5
    while time.monotonic() < drain_deadline:
        with SlowFixtureHandler.metric_lock:
            if SlowFixtureHandler.active_segments == 0:
                break
        await asyncio.sleep(0.05)
    with SlowFixtureHandler.metric_lock:
        assert SlowFixtureHandler.active_segments == 0
    with SlowFixtureHandler.metric_lock:
        SlowFixtureHandler.active_segments = 0
        SlowFixtureHandler.max_active_segments = 0
        SlowFixtureHandler.active_request_paths = {}
        SlowFixtureHandler.max_request_paths = []
    old_budget = settings.download_max_connections
    old_tasks = settings.max_concurrent_downloads
    old_worker_cap = settings.download_max_fragment_workers_per_task
    settings.download_max_connections = 60
    settings.max_concurrent_downloads = 5
    settings.download_max_fragment_workers_per_task = 12
    parallel_downloader = M3U8Downloader()
    try:
        assert parallel_downloader._config(1)["connections"] == 11
        await asyncio.gather(*[
            parallel_downloader.download(
                plan,
                str(directory / f"parallel-{index}.mp4"),
                task_key=f"integration-parallel-{index}",
            )
            for index in range(5)
        ])
    finally:
        parallel_downloader.shutdown()
        settings.download_max_connections = old_budget
        settings.max_concurrent_downloads = old_tasks
        settings.download_max_fragment_workers_per_task = old_worker_cap
    with SlowFixtureHandler.metric_lock:
        max_hls_connections = SlowFixtureHandler.max_active_segments
        max_request_paths = list(SlowFixtureHandler.max_request_paths)
    assert 15 < max_hls_connections <= 60, (
        max_hls_connections, max_request_paths)
    assert not parallel_downloader._procs

    old_waiting = settings.download_waiting_timeout
    old_stall = settings.download_stall_timeout
    settings.download_waiting_timeout = 3.0 if full_stall else 0.5
    settings.download_stall_timeout = 60.0 if full_stall else 1.5
    stalled_events = []
    stall_error = ""
    started = time.monotonic()
    try:
        async def on_stalled(event):
            stalled_events.append((time.monotonic() - started, dict(event)))

        try:
            await downloader.download(
                stall_plan, str(directory / "stall.mp4"),
                on_progress=on_stalled, task_key="integration-stall")
        except DownloadError as exc:
            stall_error = str(exc)
            if not full_stall:
                assert "无字节进展" in stall_error, exc
        else:
            raise AssertionError("永久断流没有失败")
    finally:
        settings.download_waiting_timeout = old_waiting
        settings.download_stall_timeout = old_stall
        downloader.cleanup_partials(str(directory / "stall.mp4"))
    elapsed = time.monotonic() - started
    waiting_at = min(
        event_at for event_at, item in stalled_events
        if item.get("phase") == "waiting"
    )
    if full_stall:
        assert 2.5 <= waiting_at <= 5.0, waiting_at
        assert 55.0 <= elapsed <= 65.0, elapsed
    else:
        assert waiting_at < 1.5, waiting_at
        assert elapsed < 4.0, elapsed
    output_locks = list(directory.glob("*.download-v2.lock"))
    assert not output_locks, output_locks
    return {
        "normal_events": len(normal_events),
        "first_nonzero_seconds": round(first_nonzero, 2),
        "max_speed_bps": max(
            int(item.get("speed_bps") or 0) for _, item in normal_events),
        "stall_elapsed": round(elapsed, 2),
        "waiting_at_seconds": round(waiting_at, 2),
        "waiting_seen": True,
        "stall_exit": (
            "watchdog" if "无字节进展" in stall_error
            else "fragment_retries_exhausted"),
        "non_hls_rejected": True,
        "manifest_timeline_checked": True,
        "nested_master_resolved": True,
        "frozen_manifest_reused": True,
        "cooperative_resume_safe": True,
        "natural_network_resume_safe": True,
        "natural_network_resume_skipped_completed_fragments": True,
        "output_locks_released": True,
        "resume_after_restart": True,
        "partials_preserved_on_pause": len(partials),
        "max_hls_connections": max_hls_connections,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ph115-hls-") as tmp:
        directory = Path(tmp)
        build_fixture(directory)
        handler = partial(SlowFixtureHandler, directory=str(directory))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        old_ph_base_url = settings.ph_base_url
        old_proxy = settings.proxy
        old_download_proxy = settings.download_proxy
        settings.ph_base_url = base_url
        settings.proxy = ""
        settings.download_proxy = ""
        try:
            result = asyncio.run(run_checks(
                base_url, directory,
                full_stall="--full-stall" in sys.argv))
            print(json.dumps(result, ensure_ascii=False))
        finally:
            settings.ph_base_url = old_ph_base_url
            settings.proxy = old_proxy
            settings.download_proxy = old_download_proxy
            server.shutdown()
            server.server_close()
            downloader.shutdown()


if __name__ == "__main__":
    main()
