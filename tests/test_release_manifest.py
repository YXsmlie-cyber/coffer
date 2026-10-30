"""发行依赖闭包和二进制指纹门槛的隔离测试。"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packaging.requirements import Requirement
from scripts.build_manifest import digest, validate_tools
from scripts.lock_dependencies import installed_closure


class ReleaseManifestTests(unittest.TestCase):
    def test_dependency_extras_expand_transitively_and_conflicts_fail(self):
        packages = {
            "root": SimpleNamespace(version="1.0", requires=["base==2", "speed==3; extra == 'fast'"]),
            "base": SimpleNamespace(version="2", requires=[]),
            "speed": SimpleNamespace(version="3", requires=["base==2"]),
        }
        with patch("scripts.lock_dependencies.metadata.distribution", side_effect=packages.__getitem__):
            self.assertEqual(installed_closure([Requirement("root[fast]==1.0")]),
                             {"base": "2", "root": "1.0", "speed": "3"})
            with self.assertRaises(RuntimeError):
                installed_closure([Requirement("root==9")])

    def test_changed_tool_binary_is_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "ffmpeg.exe"
            tool.write_bytes(b"verified fixture binary")
            expected = {"ffmpeg.exe": {"sha256": digest(tool)}}
            validate_tools({"ffmpeg.exe": tool}, expected)
            tool.write_bytes(b"different fixture binary")
            with self.assertRaises(RuntimeError):
                validate_tools({"ffmpeg.exe": tool}, expected)
