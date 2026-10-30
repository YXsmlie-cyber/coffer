"""冻结包原生 HLS 实链路验收；不访问外网，不使用系统 yt-dlp/aria2。"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import urlopen
from hls_fixtures import build_composite_fixture


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "PH115"


class PackageFixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/video", "/composite-video"}:
            port = self.server.server_address[1]
            manifest = "composite.m3u8" if path == "/composite-video" else "master.m3u8"
            flashvars = {
                "mediaDefinitions": [{
                    "format": "hls",
                    "quality": "1080",
                    "videoUrl": f"http://127.0.0.1:{port}/{manifest}",
                }],
                "video_title": "Package Native HLS",
                # 故意与真实 10 秒 VOD 不一致，证明页面元数据不再参与完整性判定。
                "video_duration": "50",
            }
            html = (
                "<html><script>var flashvars_123 = "
                + json.dumps(flashvars)
                + ";</script>"
                + ("x" * 60_000)
                + "</html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        super().do_GET()

    def copyfile(self, source, outputfile):
        if not urlparse(self.path).path.endswith(".ts"):
            return super().copyfile(source, outputfile)
        try:
            while chunk := source.read(4096):
                outputfile.write(chunk)
                outputfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _safe_remove_tree(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    if resolved.parent != parent_resolved or not resolved.name.startswith(
            ".package-native-hls-"):
        raise RuntimeError(f"拒绝清理越界测试目录：{resolved}")
    for attempt in range(4):
        try:
            shutil.rmtree(resolved)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))


def _request_json(url: str, timeout: float = 2.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_package(
    app_dir: Path, environment: dict[str, str], *, clear_port: bool = True
) -> subprocess.Popen:
    port_file = app_dir / "data" / "port.txt"
    if clear_port:
        try:
            port_file.unlink()
        except FileNotFoundError:
            pass
    return subprocess.Popen(
        [str(app_dir / "PH115.exe")],
        cwd=app_dir,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_ready(process: subprocess.Popen, app_dir: Path, timeout: float = 30) -> int:
    port_file = app_dir / "data" / "port.txt"
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            port = int(port_file.read_text(encoding="utf-8").strip())
            payload = _request_json(
                f"http://127.0.0.1:{port}/api/tasks?type=download")
            if isinstance(payload.get("tasks"), list):
                return port
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)
    log_path = app_dir / "data" / "app.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise AssertionError(
        f"冻结包未就绪，exit={process.poll()} error={last_error}\n{log[-4000:]}")


def _stop_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _package_process_ids(executable: Path) -> list[int]:
    if os.name != "nt":
        return []
    escaped = str(executable.resolve()).replace("'", "''")
    script = (
        "@(Get-CimInstance Win32_Process | Where-Object "
        f"{{ $_.ExecutablePath -eq '{escaped}' }} | "
        "Select-Object -ExpandProperty ProcessId) -join ','"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return [
        int(item) for item in result.stdout.strip().split(",")
        if item.strip().isdigit()
    ]


def _build_fixture(directory: Path, ffmpeg: Path) -> None:
    subprocess.run([
        str(ffmpeg), "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "24", "-keyint_min", "24", "-sc_threshold", "0",
        "-c:a", "aac", "-f", "hls", "-hls_time", "1",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(directory / "segment-%03d.ts"),
        str(directory / "fixture.m3u8"),
    ], check=True, capture_output=True)
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


def _insert_download(db_path: Path, video_url: str, viewkey="abcdef1234567") -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    values = (
        viewkey, "PackageModel", "", "", "Package Native HLS",
        video_url, 50, "download", "queued", 0.0, "", "", 0.0,
        0, 0, 0, None, 0, None, 0, 0, "", "", "", "fixture", None,
        now, now,
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO tasks (
                viewkey, model_name, model_cloud_cid, artifact_scope, title, url, duration,
                task_type, status, progress, speed, phase, speed_bps,
                downloaded_bytes, total_bytes, attempt, last_progress_at,
                auto_retry_count, next_retry_at, remote_missing_count, patrol_recovery_count,
                local_path, m3u8_url, error, origin, origin_candidate_id,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    if not (DIST / "PH115.exe").is_file():
        raise SystemExit("缺少 dist/PH115/PH115.exe，请先运行 build.ps1")
    temp_root = Path(tempfile.mkdtemp(prefix=".package-native-hls-", dir=ROOT))
    app_dir = temp_root / "PH115"
    fixture_dir = temp_root / "fixture"
    process = None
    server = None
    try:
        shutil.copytree(DIST, app_dir, copy_function=_link_or_copy)
        for generated in (app_dir / "data", app_dir / "downloads"):
            if generated.exists():
                shutil.rmtree(generated)
        env_file = app_dir / ".env"
        try:
            env_file.unlink()
        except FileNotFoundError:
            pass
        env_file.write_text(
            "PROXY=\n"
            "DOWNLOAD_PROXY=\n"
            "PATROL_ENABLED=false\n"
            "PATROL_AUTO_DOWNLOAD=false\n"
            "MAX_CONCURRENT_DOWNLOADS=5\n"
            # 并发配置禁止为0；本夹具没有115 CID，上传阶段不会成功，但不能靠
            # 非法配置静默关闭调度器。
            "MAX_CONCURRENT_UPLOADS=1\n"
            "DOWNLOAD_MAX_CONNECTIONS=15\n"
            "DOWNLOAD_MAX_FRAGMENT_WORKERS_PER_TASK=12\n"
            "DOWNLOAD_MIN_FREE_SPACE_GB=0\n"
            "DOWNLOAD_SOCKET_TIMEOUT=10\n"
            "DOWNLOAD_WAITING_TIMEOUT=3\n"
            "DOWNLOAD_STALL_TIMEOUT=60\n"
            "DOWNLOAD_FRAGMENT_RETRIES=3\n"
            "DOWNLOAD_RESOLVE_TIMEOUT=90\n",
            encoding="utf-8",
        )

        ffmpeg = app_dir / "bin" / "ffmpeg.exe"
        ffprobe = app_dir / "bin" / "ffprobe.exe"
        if not ffmpeg.is_file() or not ffprobe.is_file():
            raise AssertionError("发行包未内置 ffmpeg/ffprobe")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        restricted_path = os.pathsep.join([
            str(app_dir / "bin"),
            str(system_root / "System32"),
            str(system_root),
        ])
        if shutil.which("yt-dlp", path=restricted_path):
            raise AssertionError("测试 PATH 意外包含系统 yt-dlp")
        if shutil.which("aria2c", path=restricted_path):
            raise AssertionError("测试 PATH 意外包含 aria2c")
        environment = dict(os.environ)
        environment["PATH"] = restricted_path

        # 第一次启动只用于让冻结包执行硬依赖检查并建立完整数据库结构。
        process = _start_package(app_dir, environment)
        first_port = _wait_ready(process, app_dir)
        auth_status = _request_json(
            f"http://127.0.0.1:{first_port}/api/remote-auth/status")
        if auth_status.get("required") or not auth_status.get("authenticated"):
            raise AssertionError(f"冻结包本机会话端点异常：{auth_status}")
        duplicate = _start_package(app_dir, environment, clear_port=False)
        try:
            duplicate.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _stop_tree(duplicate)
            raise AssertionError("同目录第二个冻结包实例未被单实例锁拒绝")
        if duplicate.returncode != 0:
            raise AssertionError(f"第二实例应安全退出，实际 exit={duplicate.returncode}")
        if process.poll() is not None:
            raise AssertionError("第二实例启动影响了原有冻结包进程")
        _stop_tree(process)
        process = None
        db_path = app_dir / "data" / "data.db"
        connection = sqlite3.connect(db_path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()

        fixture_dir.mkdir()
        _build_fixture(fixture_dir, ffmpeg)
        build_composite_fixture(fixture_dir, ffmpeg)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(PackageFixtureHandler, directory=str(fixture_dir)),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        fixture_port = server.server_address[1]
        # SSRF 防护只允许显式配置的页面源；冻结包第二次启动读取本地夹具作为 PH 源。
        with env_file.open("a", encoding="utf-8") as stream:
            stream.write(f"PH_BASE_URL=http://127.0.0.1:{fixture_port}\n")
        _insert_download(db_path, f"http://127.0.0.1:{fixture_port}/video")
        _insert_download(db_path, f"http://127.0.0.1:{fixture_port}/composite-video", "abcdef7654321")

        process = _start_package(app_dir, environment)
        app_port = _wait_ready(process, app_dir)
        deadline = time.monotonic() + 90
        phases: list[str] = []
        first_downloading_at = None
        first_speed_at = None
        max_speed = 0
        task = None
        while time.monotonic() < deadline:
            payload = _request_json(
                f"http://127.0.0.1:{app_port}/api/tasks?type=download")
            tasks = payload.get("tasks") or []
            task = next(
                (item for item in tasks if item.get("viewkey") == "abcdef1234567"),
                None,
            )
            if task:
                now = time.monotonic()
                phase = str(task.get("phase") or "")
                if phase and (not phases or phases[-1] != phase):
                    phases.append(phase)
                if phase == "downloading" and first_downloading_at is None:
                    first_downloading_at = now
                speed = int(float(task.get("speed_bps") or 0))
                max_speed = max(max_speed, speed)
                if speed > 0 and first_speed_at is None:
                    first_speed_at = now
                if task.get("status") in {"completed", "failed"}:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.1)

        if not task or task.get("status") != "completed":
            log = (app_dir / "data" / "app.log").read_text(
                encoding="utf-8", errors="replace")
            raise AssertionError(f"冻结包下载失败：{task}\n{log[-4000:]}")
        if first_downloading_at is None or first_speed_at is None:
            raise AssertionError(f"冻结包未上报非零速度：{phases}")
        speed_delay = first_speed_at - first_downloading_at
        if speed_delay > 3.0:
            raise AssertionError(f"冻结包首个非零速度过晚：{speed_delay:.2f}s")

        output = Path(task["local_path"])
        probe = subprocess.run([
            str(ffprobe), "-v", "error",
            "-show_entries", "stream=codec_type:format=duration,format_name",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True, encoding="utf-8")
        media = json.loads(probe.stdout)
        if not any(
                stream.get("codec_type") == "video"
                for stream in media.get("streams") or []):
            raise AssertionError("冻结包成品缺少视频流")

        composite_task = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            payload = _request_json(f"http://127.0.0.1:{app_port}/api/tasks?type=download")
            composite_task = next((t for t in payload.get("tasks", [])
                                   if t.get("viewkey") == "abcdef7654321"), None)
            if composite_task and composite_task["status"] in {"completed", "failed"}:
                break
            time.sleep(0.1)
        if not composite_task or composite_task["status"] != "completed":
            raise AssertionError(f"冻结包分轨/时间段合并失败：{composite_task}")
        composite_probe = subprocess.run([
            str(ffprobe), "-v", "error", "-count_packets", "-show_entries",
            "stream=codec_type,nb_read_packets,duration", "-of", "json", composite_task["local_path"]
        ], check=True, capture_output=True, text=True, encoding="utf-8")
        composite_streams = json.loads(composite_probe.stdout)["streams"]
        assert any(s["codec_type"] == "video" and int(s["nb_read_packets"]) == 144
                   and abs(float(s["duration"]) - 6) < 0.1 for s in composite_streams)
        assert any(s["codec_type"] == "audio" and int(s["nb_read_packets"]) > 250 for s in composite_streams)
        time.sleep(0.8)
        package_pids = _package_process_ids(app_dir / "PH115.exe")
        if package_pids and package_pids != [process.pid]:
            raise AssertionError(
                f"下载完成后仍有冻结包 worker：主进程={process.pid}, 全部={package_pids}")

        _stop_tree(process)
        process = None
        time.sleep(0.5)
        orphans = _package_process_ids(app_dir / "PH115.exe")
        if orphans:
            raise AssertionError(f"关闭后存在孤儿进程：{orphans}")

        print(json.dumps({
            "app_port": app_port,
            "phases": phases,
            "first_speed_seconds": round(speed_delay, 2),
            "max_speed_bps": max_speed,
            "final_bytes": output.stat().st_size,
            "system_ytdlp": False,
            "system_aria2c": False,
            "orphan_processes": 0,
            "db_integrity": "ok",
            "remote_auth": "ok",
            "composite_tracks_and_periods": "ok",
        }, ensure_ascii=False))
    finally:
        _stop_tree(process)
        if server is not None:
            server.shutdown()
            server.server_close()
        _safe_remove_tree(temp_root, ROOT)


if __name__ == "__main__":
    main()
