# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义（onedir）。

打包要点：
  - p115client 依赖几十个小包且部分有数据文件，用 collect_all 完整收集。
  - yt-dlp 是唯一网络下载引擎，显式收集其子模块和数据文件。
  - uvicorn 的 loop/protocol 实现是运行时动态选择，必须显式 hiddenimport，
    否则 /ws 起不来、HTTP 也可能挂。
  - 前端 dist 整个塞进 frontend_dist（main.py frozen 时从 sys._MEIPASS 读）。
  - console=False：分发版隐藏窗口；排错靠 data/app.log（见 app_main.py）。
构建用 build.ps1（建干净 venv 后 pyinstaller ph115.spec）。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# p115client 全家桶 + 有 C 扩展/数据的关键库
_collect_pkgs = [
    "p115cipher", "p115oss", "p115pickcode",
    "iterdir", "id2dirnode", "orjson", "m3u8", "qrcode",
    "yarl", "multidict", "filelock",
]
for pkg in _collect_pkgs:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        # 个别纯 Python 小包 collect_all 偶发找不到 metadata，不致命，跳过
        pass

# p115client 是登录/上传硬依赖，收集失败必须中止构建，不能产出“能启动但不能上传”的包。
d, b, h = collect_all("p115client")
datas += d
binaries += b
hiddenimports += h

# yt-dlp 是下载必需组件：收集失败时直接让构建失败，禁止产出缺少下载引擎的安装包。
d, b, h = collect_all("yt_dlp")
datas += d
binaries += b
hiddenimports += h

# 探索页图像塔是本地视觉功能的硬依赖，ONNX Runtime 的 DLL 与 provider
# 必须显式收集；缺少时禁止产出只能打开、却无法生成结果的发行包。
d, b, h = collect_all("onnxruntime")
datas += d
binaries += b
hiddenimports += h

visual_model_dir = Path(SPECPATH) / "models" / "visual"
visual_model_files = [
    visual_model_dir / "siglip2-int8.json",
    visual_model_dir / "siglip2-vision-int8.onnx",
    visual_model_dir / "README.md",
]
missing_visual_files = [str(path) for path in visual_model_files if not path.is_file()]
if missing_visual_files:
    raise FileNotFoundError(
        "探索页本地视觉资产不完整，无法打包：" + ", ".join(missing_visual_files)
    )
datas += [(str(path), "models/visual") for path in visual_model_files]

# p115client 的众多 python-* 小依赖：完整收集子模块（导入名各异，全量扫描最稳）。
hiddenimports += collect_submodules("p115client")

# uvicorn 运行时动态导入的实现，必须显式声明
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "websockets.legacy", "websockets.legacy.server",
    "aiosqlite",
    "anyio._backends._asyncio",
]

# 前端构建产物
datas += [("frontend/dist", "frontend_dist")]

a = Analysis(
    ["app_main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PH115",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 隐藏窗口（用户选择）；排错靠 data/app.log
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PH115",
)
