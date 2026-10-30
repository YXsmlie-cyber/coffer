"""ffmpeg/ffprobe 发现与可用性检查：源码运行不再只依赖 PATH。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.downloader as downloader
from backend.config import settings
from backend.download_errors import classify_stored_download_failure


def _touch_tool(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + (".exe" if sys.platform == "win32" else ""))
    path.write_bytes(b"")
    return str(path)


class MediaToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_dir = settings.ffmpeg_dir
        downloader._media_tools_cache = None

    def tearDown(self):
        settings.ffmpeg_dir = self.old_dir
        downloader._media_tools_cache = None
        self.temp.cleanup()

    def test_configured_dir_wins_over_project_tools_and_path(self):
        configured = _touch_tool(self.root / "configured", "ffprobe")
        _touch_tool(self.root / "app" / "tools" / "ffmpeg", "ffprobe")
        settings.ffmpeg_dir = str(self.root / "configured")
        with patch.object(downloader, "_BIN_DIR", None), \
                patch.object(downloader, "APP_DIR", self.root / "app"), \
                patch.object(downloader.shutil, "which", return_value="/usr/bin/ffprobe"):
            self.assertEqual(os.path.normcase(downloader.ffprobe_path()),
                             os.path.normcase(configured))

    def test_project_tools_dir_is_used_when_path_has_no_ffmpeg(self):
        bundled = _touch_tool(self.root / "app" / "tools" / "ffmpeg", "ffmpeg")
        settings.ffmpeg_dir = ""
        with patch.object(downloader, "_BIN_DIR", None), \
                patch.object(downloader, "APP_DIR", self.root / "app"), \
                patch.object(downloader.shutil, "which", return_value=None):
            self.assertEqual(os.path.normcase(downloader.ffmpeg_path()),
                             os.path.normcase(bundled))
            # 找不到的工具保留原来的裸命令名，由调用方报环境错误。
            self.assertEqual(downloader.ffprobe_path(), "ffprobe")
            self.assertIsNone(downloader.resolve_media_tool("ffprobe"))

    def test_status_reports_missing_and_broken_tools_without_paths(self):
        with patch.object(downloader, "resolve_media_tool",
                          side_effect=lambda name: None if name == "ffprobe" else "/secret/dir/ffmpeg"), \
                patch.object(downloader, "_probe_media_tool", return_value=None):
            status = downloader.check_media_tools(force=True)
        self.assertFalse(status.ok)
        self.assertEqual(status.missing, ("ffprobe",))
        self.assertEqual(status.broken, ("ffmpeg",))
        self.assertIn("ffprobe", status.message)
        self.assertNotIn("secret", str(status.public()) + status.message)

    def test_status_is_cached_until_forced(self):
        calls = []

        def probe(path):
            calls.append(path)
            return "ffmpeg version 8.1.1"

        with patch.object(downloader, "resolve_media_tool", side_effect=lambda name: f"/tools/{name}"), \
                patch.object(downloader, "_probe_media_tool", side_effect=probe):
            self.assertTrue(downloader.check_media_tools(force=True).ok)
            self.assertTrue(downloader.check_media_tools().ok)
            self.assertEqual(len(calls), 2)
            downloader.check_media_tools(force=True)
            self.assertEqual(len(calls), 4)


class StoredEnvironmentFailureTests(unittest.TestCase):
    def test_missing_media_tool_failure_is_bulk_recoverable(self):
        for error in (
            "DownloadEnvironmentError: 未找到 ffprobe，无法校验下载成品 | ffmpeg-location ffmpeg does not exist!",
            "DownloadEnvironmentError: 无法启动 ffmpeg: [WinError 5] 拒绝访问。",
        ):
            disposition = classify_stored_download_failure(error)
            self.assertEqual(disposition.kind, "missing_media_tool")
            self.assertEqual(disposition.recovery_action, "retry_network")
            self.assertTrue(disposition.bulk_recoverable)

    def test_other_environment_failures_stay_manual(self):
        disposition = classify_stored_download_failure(
            "DownloadEnvironmentError: [WinError 112] 磁盘空间不足。")
        self.assertFalse(disposition.bulk_recoverable)


if __name__ == "__main__":
    unittest.main()
