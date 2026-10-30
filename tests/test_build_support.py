"""隔离验证构建失败即停止；不安装依赖，不操作发行目录。"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import unittest


SHELLS = [path for name in ("pwsh", "powershell") if (path := shutil.which(name))]
SUPPORT = Path(__file__).resolve().parents[1] / "scripts" / "build_support.ps1"


@unittest.skipUnless(SHELLS, "需要 PowerShell 验证 Windows 构建边界")
class BuildCommandTests(unittest.TestCase):
    def test_build_cleanup_rejects_workspace_root_and_parent_escape(self):
        script = self.prelude() + """
            $base = Join-Path ([System.IO.Path]::GetTempPath()) 'ph115-safe-build-test'
            foreach ($target in @($base, (Join-Path $base '..\\outside'))) {
                try { Assert-BuildDirectory $target $base; exit 3 }
                catch { if ($_.Exception.Message -notmatch '构建目录越界') { exit 4 } }
            }
            $child = Assert-BuildDirectory (Join-Path $base 'build') $base
            if ($child -ne (Join-Path $base 'build')) { exit 5 }
        """
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_script(shell, script)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_script(self, shell, script):
        environment = dict(os.environ, PH115_BUILD_TEST_PYTHON=sys.executable)
        args = [shell, "-NoProfile", "-NonInteractive"]
        if Path(shell).name.lower().startswith("powershell"):
            # Windows PowerShell 受执行策略约束，默认 Restricted 的机器会拒绝加载
            # 仓库内的 .ps1。这里只放行本次子进程，不改机器全局设置。
            args += ["-ExecutionPolicy", "Bypass"]
        args += ["-Command", script]
        return subprocess.run(
            args,
            env=environment, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )

    def prelude(self):
        path = str(SUPPORT).replace("'", "''")
        return f"$ErrorActionPreference='Stop'; . '{path}'; "

    def test_failed_dependency_step_prevents_later_success(self):
        script = self.prelude() + """
            try {
                Invoke-BuildCommand 'requirements' $env:PH115_BUILD_TEST_PYTHON @('-c', 'raise SystemExit(9)')
                Invoke-BuildCommand 'later' $env:PH115_BUILD_TEST_PYTHON @('-c', 'print(12345)')
                exit 3
            } catch {
                if ($_.Exception.Message -notmatch 'requirements.*9') { exit 4 }
                Write-Output 'stopped-at-requirements'
            }
        """
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_script(shell, script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("stopped-at-requirements", result.stdout)
                self.assertNotIn("12345", result.stdout)

    def test_success_preserves_argument_boundaries_and_continues(self):
        script = self.prelude() + """
            Invoke-BuildCommand 'success' $env:PH115_BUILD_TEST_PYTHON @('-c', 'import sys; assert sys.argv[1] == ''two words''', 'two words')
            Write-Output 'continued'
        """
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_script(shell, script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("continued", result.stdout)
