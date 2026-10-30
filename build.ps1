<#
  PH-115 一键打包脚本（全自包含 onedir）。
  在仓库根 <repo> 下用 PowerShell 运行： .\build.ps1

  步骤：建干净 venv → 装依赖 → 构建前端 → PyInstaller 打包 →
        复制内置 ffmpeg/ffprobe 后处理工具 → 生成脱敏 .env / 启动.bat / 使用说明.txt →
        整包复制到桌面。

  隐私：不打包 .env / data.db / 115_cookie.txt；随包 .env 为脱敏版（cid 留空）。
#>
param(
  # CI/本机验收只保留 dist；显式发布时才复制到桌面。
  [switch]$PublishToDesktop,
  # 在隔离副本中验证冻结包；不会使用真实 Cookie、数据库或媒体。
  [switch]$VerifyPackage
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root
. (Join-Path $Root "scripts\build_support.ps1")

# 发行锁定清单固定 Windows / Python 3.14。
$PyLauncher = "python"
Invoke-BuildCommand "检查发行 Python" $PyLauncher @("-c", "import sys; assert sys.platform == 'win32' and sys.version_info[:2] == (3, 14), '需要 Windows / Python 3.14'")
Invoke-BuildCommand "检查发行工具指纹" $PyLauncher @("-X", "utf8", "scripts/build_manifest.py", "--check-tools")

Write-Host "==> [1/7] 建干净虚拟环境 .venv_build" -ForegroundColor Cyan
Remove-BuildDirectory "$Root\.venv_build" $Root
Invoke-BuildCommand "创建构建环境" $PyLauncher @("-m", "venv", ".venv_build")
$Py = "$Root\.venv_build\Scripts\python.exe"

Write-Host "==> [2/7] 安装后端依赖 + PyInstaller" -ForegroundColor Cyan
Invoke-BuildCommand "安装固定 pip" $Py @("-m", "pip", "install", "pip==26.1.2")
Invoke-BuildCommand "安装完整构建依赖" $Py @("-m", "pip", "install", "--no-deps", "-r", "requirements-build.lock")
Invoke-BuildCommand "检查依赖一致性" $Py @("-m", "pip", "check")
Invoke-BuildCommand "验证运行依赖闭包" $Py @("-X", "utf8", "scripts/lock_dependencies.py")
Invoke-BuildCommand "验证构建依赖闭包" $Py @("-X", "utf8", "scripts/lock_dependencies.py", "--input", "requirements-build.in", "--output", "requirements-build.lock")
# 构建前硬校验登录/上传依赖。不能把 PyInstaller 的缺模块告警留到用户扫码时才暴露。
Invoke-BuildCommand "验证核心运行依赖" $Py @("-c", "import p115client; import p115client.tool.upload; from concurrenttools import Return; import yt_dlp; assert yt_dlp.version.__version__ in {'2026.06.09','2026.6.9'}; print('核心运行依赖导入 OK')")

Write-Host "==> [3/7] 构建前端并检查包体预算" -ForegroundColor Cyan
$FrontendBuild = "$Root\.frontend_build"
Remove-BuildDirectory $FrontendBuild $Root
New-Item -ItemType Directory -Force -Path $FrontendBuild | Out-Null
# 始终在隔离目录安装和构建。Windows 上运行中的 Vite 会占用项目
# node_modules 里的 esbuild.exe，直接 npm ci 会破坏开发环境或报 EPERM。
Copy-Item "$Root\frontend\package.json" $FrontendBuild
Copy-Item "$Root\frontend\package-lock.json" $FrontendBuild
Copy-Item "$Root\frontend\index.html" $FrontendBuild
Copy-Item "$Root\frontend\vite.config.js" $FrontendBuild
Copy-Item "$Root\frontend\src" $FrontendBuild -Recurse
Copy-Item "$Root\frontend\scripts" $FrontendBuild -Recurse
Push-Location $FrontendBuild
try {
  Invoke-BuildCommand "安装前端锁定依赖" "npm.cmd" @("ci")
  Invoke-BuildCommand "前端构建及包体预算" "npm.cmd" @("run", "build:check")
} finally {
  Pop-Location
}
Remove-BuildDirectory "$Root\frontend\dist" $Root
Copy-Item "$FrontendBuild\dist" "$Root\frontend\dist" -Recurse
Remove-BuildDirectory $FrontendBuild $Root

Write-Host "==> [4/7] PyInstaller 打包" -ForegroundColor Cyan
Remove-BuildDirectory "$Root\build" $Root
Remove-BuildDirectory "$Root\dist" $Root
Invoke-BuildCommand "PyInstaller 打包" $Py @("-m", "PyInstaller", "ph115.spec", "--noconfirm")
$OutDir = "$Root\dist\PH115"
if (-not (Test-Path "$OutDir\PH115.exe")) { throw "未生成 PH115.exe" }

Write-Host "==> [5/7] 复制内置 ffmpeg/ffprobe 后处理工具" -ForegroundColor Cyan
# 优先使用项目 tools\ffmpeg 中的固定版工具（源码运行也从这里读取）；没有时回退 PATH。
# 上一步已删除 dist，不能再依赖旧打包目录里的 bin。
$ffmpeg = Join-Path $Root "tools\ffmpeg\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) { $ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source }
if (-not $ffmpeg) { throw "tools\ffmpeg 与 PATH 上都找不到 ffmpeg，无法内置。请先放入固定版 ffmpeg/ffprobe。" }
New-Item -ItemType Directory -Force -Path "$OutDir\bin" | Out-Null
Copy-Item $ffmpeg "$OutDir\bin\ffmpeg.exe" -Force
# 顺带把同目录的 ffprobe 也带上（部分场景需要）
$ffprobe = Join-Path (Split-Path $ffmpeg) "ffprobe.exe"
if (-not (Test-Path $ffprobe)) { throw "ffmpeg 同目录找不到 ffprobe，无法执行下载成品校验。" }
Copy-Item $ffprobe "$OutDir\bin\ffprobe.exe" -Force

Write-Host "==> [6/7] 生成脱敏 .env / 启动.bat / 使用说明.txt" -ForegroundColor Cyan

# 脱敏 .env —— 注意：故意不写 DOWNLOAD_DIR 行（空串会覆盖默认值）。
# 用 UTF-8 无 BOM 写出，避免 pydantic 读取首行键名带 BOM。
$envText = @"
# ===== PH-115 配置 =====
# 代理（爬 PornHub 用）。留空=直连；如需代理填 http://127.0.0.1:7890
PROXY=

# 下载代理（仅用于 yt-dlp 原生 HLS；留空=直连，不会自动切换）
DOWNLOAD_PROXY=

# 单一下载引擎连接预算与超时（一般不用改）
DOWNLOAD_MAX_CONNECTIONS=15
DOWNLOAD_MAX_FRAGMENT_WORKERS_PER_TASK=12
DOWNLOAD_MIN_FREE_SPACE_GB=20
DOWNLOAD_SOCKET_TIMEOUT=10
DOWNLOAD_STALL_TIMEOUT=60
DOWNLOAD_WAITING_TIMEOUT=3
DOWNLOAD_FRAGMENT_RETRIES=3
DOWNLOAD_RESOLVE_TIMEOUT=90

# 【必填】你的 115 网盘 "VIDEO HUB" 文件夹的 cid（见 使用说明.txt 第3步）
VIDEO_HUB_CID=

# 并发与匹配（一般不用改）
MAX_CONCURRENT_DOWNLOADS=5
MAX_CONCURRENT_UPLOADS=2
DURATION_TOLERANCE=3
PH_BASE_URL=https://cn.pornhub.com

# 自动巡查
PATROL_ENABLED=true
PATROL_INTERVAL_HOURS=6
PATROL_AUTO_DOWNLOAD=true

# 115模特独立页面
LIBRARY_PAGE_V2=true

# 外网应用内登录会话（账号密码仍由服务器 .htpasswd 管理）
REMOTE_SESSION_HOURS=12
REMOTE_REMEMBER_DAYS=30
"@
[System.IO.File]::WriteAllText("$OutDir\.env", $envText, (New-Object System.Text.UTF8Encoding($false)))

# 启动.bat —— 控制台菜单版（参考开发版 scripts\dev_launcher.ps1 的菜单，适配打包版单 exe + 自动端口）。
# PID 由 exe 写入 data\pid.txt；关闭前必须核对进程路径属于当前发行目录。
# 端口由 exe 自动选择并写入 data\port.txt（8000 被占则顺延）。
$batText = @'
@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title PH-115 控制台
cd /d "%~dp0"

:menu
cls
echo ============================================
echo              PH-115 同步工具  控制台
echo ============================================
echo.
call :status_line
echo.
echo   [1] 启动程序 ^(后台运行,自动打开浏览器^)
echo   [2] 关闭程序
echo   [3] 打开浏览器
echo   [4] 刷新状态
echo   [0] 退出控制台 ^(不影响后台运行^)
echo.
echo ============================================
set /p "choice=请输入数字后回车: "

if "%choice%"=="1" goto start_app
if "%choice%"=="2" goto stop_app
if "%choice%"=="3" goto open_browser
if "%choice%"=="4" goto menu
if "%choice%"=="0" exit /b
goto menu

:status_line
set "ST=未运行"
if exist "%~dp0data\pid.txt" for /f "delims=" %%S in ('powershell -NoProfile -Command "$f='%~dp0data\pid.txt'; $expected=[IO.Path]::GetFullPath('%~dp0PH115.exe'); $status='未运行'; try { $processId=[int](Get-Content -LiteralPath $f); $process=Get-Process -Id $processId -ErrorAction Stop; if ($process.Path -eq $expected) { $status='运行中' } } catch {}; Write-Output $status"') do set "ST=%%S"
set "CURPORT=-"
if exist "%~dp0data\port.txt" set /p CURPORT=<"%~dp0data\port.txt"
echo   状态: !ST!        端口: !CURPORT!
goto :eof

:start_app
echo.
call :status_line
if "!ST!"=="运行中" (
  echo 程序已在运行,直接打开浏览器...
  goto open_browser
)
if exist "%~dp0data\port.txt" del /q "%~dp0data\port.txt" >nul 2>&1
echo 正在启动,请稍候...
start "" "%~dp0PH115.exe"
set /a tries=0
:waitloop
timeout /t 1 /nobreak >nul
if exist "%~dp0data\port.txt" goto started
set /a tries+=1
if !tries! geq 30 goto start_timeout
goto waitloop
:started
set /p PORT=<"%~dp0data\port.txt"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:!PORT!/"
echo 已启动并打开浏览器（端口 !PORT!）。
timeout /t 2 /nobreak >nul
goto menu
:start_timeout
echo 启动超时。请把 data\app.log 发给开发者排查。
pause
goto menu

:stop_app
echo.
echo 正在关闭...
if exist "%~dp0data\pid.txt" for /f "delims=" %%P in ('powershell -NoProfile -Command "$f='%~dp0data\pid.txt'; $expected=[IO.Path]::GetFullPath('%~dp0PH115.exe'); try { $processId=[int](Get-Content -LiteralPath $f); $process=Get-Process -Id $processId -ErrorAction Stop; if ($process.Path -eq $expected) { Write-Output $processId } } catch {}"') do taskkill /F /T /PID %%P >nul 2>&1
if exist "%~dp0data\pid.txt" del /q "%~dp0data\pid.txt" >nul 2>&1
if exist "%~dp0data\port.txt" del /q "%~dp0data\port.txt" >nul 2>&1
echo 已关闭。
timeout /t 2 /nobreak >nul
goto menu

:open_browser
if not exist "%~dp0data\port.txt" (
  echo 程序尚未运行,请先选 [1] 启动。
  timeout /t 2 /nobreak >nul
  goto menu
)
set /p PORT=<"%~dp0data\port.txt"
start "" "http://127.0.0.1:!PORT!/"
goto menu
'@
# 写出 .bat：必须 CRLF 换行（cmd 解析 LF 会错乱导致提前退出）+ GBK 编码（配合 chcp 936，中文不乱码）
$batCRLF = ($batText -replace "`r`n", "`n") -replace "`n", "`r`n"
$gbk = [System.Text.Encoding]::GetEncoding(936)
[System.IO.File]::WriteAllBytes("$OutDir\启动.bat", $gbk.GetBytes($batCRLF))

# 使用说明.txt
$readmeText = @"
PH-115 同步工具 使用说明
================================

【怎么用】
1) 双击 “启动.bat”。第一次可能弹出 Windows 安全提示（见下方“放行”），
   放行后会自动打开浏览器进入操作界面。
2) 在网页里按提示用 115 手机 App 扫码登录（登录信息只存在你本机 data 文件夹）。
3) 填你自己的“VIDEO HUB”文件夹 cid：
   - 在 115 网盘里打开你存视频的那个总文件夹，浏览器地址栏里 cid=后面那一串数字
     就是它的 cid；
   - 用记事本打开本目录的 “.env” 文件，把数字填到 VIDEO_HUB_CID= 后面，保存；
   - 关掉程序重新双击“启动.bat”即可生效。
4) 页面分工：
   - “115模特”：只管理115已有模特，包含绑定、巡查、自动下载和通知；
   - “模特”：获取PH模特榜单、同步完整作品，并在服务端排除115已有模特；
   - “视频”：从视频入口发现上传者，获取后完整同步，主列表排除115已有模特。

【Windows 拦截怎么放行】
- 若出现蓝色“Windows 已保护你的电脑”：点“更多信息”→“仍要运行”。
- 若被杀毒软件拦截/隔离：在杀软里把本文件夹加入信任/白名单。
- 本程序未做数字签名，被提示“未知发布者”属正常现象。

【出问题怎么办】
- 双击没反应或网页打不开：把本目录 data\app.log 文件发给开发者，即可定位问题。
- 程序在后台运行，关掉黑窗口不影响；要彻底退出可在任务管理器结束 PH115.exe。

【说明】
- 下载固定使用内置 Python 依赖 yt-dlp 的原生 HLS 引擎，不探测系统 yt-dlp 或 aria2。
- ffmpeg 仅用于下载后的封装，ffprobe 用于成品校验；二者不参与网络下载。
- 内置 ffmpeg/ffprobe 来源：https://www.gyan.dev/ffmpeg/builds/ ，遵循 GPL 许可。
- 下载的视频在 downloads 文件夹，运行数据在 data 文件夹。
"@
[System.IO.File]::WriteAllText("$OutDir\使用说明.txt", $readmeText, (New-Object System.Text.UTF8Encoding($false)))

Invoke-BuildCommand "生成发行构建清单" $Py @("-X", "utf8", "scripts/build_manifest.py", "--output", $OutDir)

if ($VerifyPackage -or $PublishToDesktop) {
  Write-Host "==> 冻结包本地 HLS 验收" -ForegroundColor Cyan
  Invoke-BuildCommand "冻结包 HLS 验收" $Py @("tests\integration_packaged_native_hls.py")
}

if ($PublishToDesktop) {
  Write-Host "==> [7/7] 复制到桌面" -ForegroundColor Cyan
  $Desktop = [Environment]::GetFolderPath("Desktop")
  $DeskOut = Join-Path $Desktop "PH115"
  if (Test-Path $DeskOut) {
    Write-Host "桌面已存在 PH115，备份为 PH115_old" -ForegroundColor Yellow
    Remove-BuildDirectory "$DeskOut`_old" $Desktop
    $checkedDesktop = Assert-BuildDirectory $DeskOut $Desktop
    Rename-Item -LiteralPath $checkedDesktop -NewName "PH115_old"
  }
  Copy-Item $OutDir $DeskOut -Recurse -Force
  Write-Host "完成！桌面已生成 PH115 文件夹。" -ForegroundColor Green
} else {
  Write-Host "==> [7/7] 跳过桌面发布（需要时使用 -PublishToDesktop）" -ForegroundColor Cyan
  Write-Host "完成！发行目录：$OutDir" -ForegroundColor Green
}
Write-Host ""
Write-Host "下一步：在干净环境验证（见 plan 验证清单），确认扫码登录+下载+上传全流程。" -ForegroundColor Green
