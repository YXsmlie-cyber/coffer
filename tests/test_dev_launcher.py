"""隔离验证启动器的 PID/时间归属判断；不执行启动器或操作项目进程。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


SHELLS = [path for name in ("pwsh", "powershell") if (path := shutil.which(name))]
LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "dev_launcher.ps1"


@unittest.skipUnless(SHELLS, "需要 PowerShell 验证启动器进程归属")
class DevLauncherOwnershipTests(unittest.TestCase):
    def run_check(self, shell, check):
        path = str(LAUNCHER).replace("'", "''")
        script = f"""
            $ErrorActionPreference = 'Stop'
            $tokens = $null
            $parseErrors = $null
            $tree = [System.Management.Automation.Language.Parser]::ParseFile(
                '{path}', [ref]$tokens, [ref]$parseErrors)
            if ($parseErrors.Count -gt 0) {{ throw 'Launcher syntax error' }}
            $function = $tree.Find({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    $node.Name -eq 'Get-OwnedProcess'
            }}, $true)
            if ($null -eq $function) {{ throw 'Ownership function missing' }}
            Invoke-Expression $function.Extent.Text
            $selfProcess = Get-Process -Id $PID
            $startedAt = $selfProcess.StartTime.ToUniversalTime()
        """ + check
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_json_roundtrip_recognizes_current_process(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_check(shell, """
                    $entry = @{ pid = $PID; start_time = $startedAt.ToString('o') } |
                        ConvertTo-Json | ConvertFrom-Json
                    @{
                        owned = ($null -ne (Get-OwnedProcess $entry))
                        decoded_type = $entry.start_time.GetType().FullName
                    } | ConvertTo-Json -Compress
                """)
                self.assertTrue(result["owned"], result)

    def test_json_roundtrip_rejects_different_start_time(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_check(shell, """
                    $entry = @{ pid = $PID; start_time = $startedAt.AddTicks(1).ToString('o') } |
                        ConvertTo-Json | ConvertFrom-Json
                    @{ owned = ($null -ne (Get-OwnedProcess $entry)) } |
                        ConvertTo-Json -Compress
                """)
                self.assertFalse(result["owned"])

    def test_string_start_time_keeps_exact_comparison(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_check(shell, """
                    $entry = @{ pid = $PID; start_time = $startedAt.ToString('o') }
                    $exact = $null -ne (Get-OwnedProcess $entry)
                    $entry.start_time = $entry.start_time.Replace('Z', '+00:00')
                    @{
                        exact_owned = $exact
                        alternate_owned = ($null -ne (Get-OwnedProcess $entry))
                    } | ConvertTo-Json -Compress
                """)
                self.assertTrue(result["exact_owned"])
                self.assertFalse(result["alternate_owned"])
