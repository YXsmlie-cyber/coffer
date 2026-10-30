param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("start", "stop", "status", "url")]
  [string]$Action
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $Root "data\dev-processes.json"

function New-State {
  return @{ backend = $null; frontend = $null }
}

function Read-State {
  if (-not (Test-Path -LiteralPath $StatePath)) { return (New-State) }
  try {
    $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    return @{ backend = $raw.backend; frontend = $raw.frontend }
  } catch {
    return (New-State)
  }
}

function Write-State([hashtable]$State) {
  $directory = Split-Path -Parent $StatePath
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  $tmp = "$StatePath.tmp"
  $State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $StatePath -Force
}

function Get-OwnedProcess($Entry) {
  if ($null -eq $Entry -or -not $Entry.pid -or -not $Entry.start_time) { return $null }
  $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
  if ($null -eq $process) { return $null }
  try { $actual = $process.StartTime.ToUniversalTime().ToString("o") }
  catch { return $null }
  $expected = $Entry.start_time
  # 新版 PowerShell 会把 JSON 时间戳解码为 DateTime；归一后仍精确核对启动时间。
  if ($expected -is [DateTime]) { $expected = $expected.ToUniversalTime().ToString("o") }
  if ($actual -ne [string]$expected) { return $null }
  return $process
}

function New-Entry($Process, [int]$Port = 0) {
  $Process.Refresh()
  $entry = @{
    pid = $Process.Id
    start_time = $Process.StartTime.ToUniversalTime().ToString("o")
  }
  if ($Port -gt 0) { $entry.port = $Port }
  return $entry
}

function Test-LocalPort([int]$Port) {
  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $pending = $client.ConnectAsync("127.0.0.1", $Port)
    if (-not $pending.Wait(400)) { return $false }
    return $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

function Get-ProcessTreeIds([int]$RootId) {
  $all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
  $ids = [System.Collections.Generic.List[int]]::new()
  $ids.Add($RootId)
  for ($index = 0; $index -lt $ids.Count; $index++) {
    $parent = $ids[$index]
    foreach ($child in $all | Where-Object ParentProcessId -eq $parent) {
      if (-not $ids.Contains([int]$child.ProcessId)) { $ids.Add([int]$child.ProcessId) }
    }
  }
  return @($ids)
}

function Get-FrontendPort($Entry) {
  if ($null -eq $Entry) { return 0 }
  $rootProcess = Get-OwnedProcess $Entry
  if ($null -eq $rootProcess) { return 0 }
  $ids = @(Get-ProcessTreeIds $rootProcess.Id)
  try { $storedPort = [int]$Entry.port } catch { $storedPort = 0 }
  if ($storedPort -gt 0) {
    $storedListener = Get-NetTCPConnection -State Listen -LocalPort $storedPort -ErrorAction SilentlyContinue |
      Where-Object { $ids -contains [int]$_.OwningProcess } |
      Select-Object -First 1
    if ($storedListener) { return $storedPort }
  }

  $listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $ids -contains [int]$_.OwningProcess -and $_.LocalPort -ge 5173 -and $_.LocalPort -le 5192 } |
    Sort-Object LocalPort |
    Select-Object -First 1
  return $(if ($listener) { [int]$listener.LocalPort } else { 0 })
}

function Find-AvailableFrontendPort {
  foreach ($port in 5173..5192) {
    if (-not (Test-LocalPort $port)) { return $port }
  }
  throw "No available frontend port in 5173-5192."
}

function Stop-OwnedTree($Entry) {
  $rootProcess = Get-OwnedProcess $Entry
  if ($null -eq $rootProcess) { return }
  $ids = @(Get-ProcessTreeIds $rootProcess.Id)
  for ($index = $ids.Count - 1; $index -ge 0; $index--) {
    Stop-Process -Id $ids[$index] -Force -ErrorAction SilentlyContinue
  }
}

$state = Read-State
$backend = Get-OwnedProcess $state.backend
$frontend = Get-OwnedProcess $state.frontend
$frontendPort = Get-FrontendPort $state.frontend
if ($null -eq $backend) { $state.backend = $null }
if ($null -eq $frontend) { $state.frontend = $null }
elseif ($frontendPort -gt 0) { $state.frontend = New-Entry $frontend $frontendPort }

switch ($Action) {
  "status" {
    Write-State $state
    $backendText = if ($backend) { "running (PID $($backend.Id))" } else { "not running" }
    $frontendText = if ($frontend) {
      $urlText = if ($frontendPort -gt 0) { " http://127.0.0.1:$frontendPort/" } else { "" }
      "running (PID $($frontend.Id))$urlText"
    } else { "not running" }
    Write-Host "  Backend: $backendText    Frontend: $frontendText"
  }
  "start" {
    if (-not $backend) {
      if (Test-LocalPort 8000) {
        throw "Port 8000 is owned by another process; nothing was terminated."
      }
      $python = Join-Path $Root ".venv\Scripts\python.exe"
      if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python).Source }
      # stderr 只收未处理异常与 spawn 下载子进程的崩溃栈（子进程继承该句柄）；常规
      # 日志仍写轮转的 app.log。保留上一次运行的文件，重启后仍能查看之前的崩溃。
      $dataDir = Join-Path $Root "data"
      New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
      $stderrLog = Join-Path $dataDir "backend-stderr.log"
      if (Test-Path -LiteralPath $stderrLog) {
        try {
          Move-Item -LiteralPath $stderrLog -Destination (Join-Path $dataDir "backend-stderr.prev.log") -Force
        } catch {
          # 旧文件仍被残留子进程占用时不阻断启动，改用带时间戳的新文件。
          $stderrLog = Join-Path $dataDir ("backend-stderr-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
        }
      }
      # 由 cmd.exe 做重定向：Start-Process 自带的重定向会让后端继承本控制台的
      # stdout 句柄。stdout 与原先一样丢弃；根 PID 为 cmd.exe，整棵树仍由
      # Stop-OwnedTree 回收。环境变量随进程继承，只关闭写往 stderr 的日志副本。
      $env:PH115_CONSOLE_LOG = "0"
      $backendCommand = "`"$python`" run.py 1>NUL 2>`"$stderrLog`""
      $backend = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /s /c `"$backendCommand`"" `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru
      $state.backend = New-Entry $backend
      Write-State $state
    }
    if (-not $frontend) {
      $frontendPort = Find-AvailableFrontendPort
      $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
      # .cmd 走 ShellExecute 时拿不到进程句柄，-PassThru 会报“找不到所需的所有信息”；
      # 显式交给 cmd.exe 启动，根 PID 即 cmd.exe，整棵进程树仍由 Stop-OwnedTree 回收。
      $npmCommand = "`"$npm`" run dev -- --host 127.0.0.1 --port $frontendPort --strictPort"
      $frontend = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /s /c `"$npmCommand`"" `
        -WorkingDirectory (Join-Path $Root "frontend") -WindowStyle Hidden -PassThru
      $state.frontend = New-Entry $frontend $frontendPort
      Write-State $state
    }
    Write-Host "Project processes started; only owned PIDs will be managed."
    if ($frontendPort -gt 0) { Write-Host "Frontend URL: http://127.0.0.1:$frontendPort/" }
  }
  "stop" {
    Stop-OwnedTree $state.frontend
    Stop-OwnedTree $state.backend
    $state = New-State
    Write-State $state
    Write-Host "Owned project processes stopped; unrelated processes were untouched."
  }
  "url" {
    if ($frontend -and $frontendPort -gt 0) {
      Write-Output "http://127.0.0.1:$frontendPort/"
    } elseif ($backend) {
      Write-Output "http://127.0.0.1:8000/"
    } else {
      Write-Output "http://127.0.0.1:8000/"
    }
  }
}
