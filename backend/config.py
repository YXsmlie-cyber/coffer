"""应用配置，从环境变量 / .env 读取。

路径策略（兼顾源码运行与 PyInstaller 打包）：
  - 源码运行：APP_DIR = 项目根（backend 的上级）。
  - 打包(frozen)：APP_DIR = exe 所在目录（不能用 __file__，它指向临时解压目录）。
运行数据（data.db / 115_cookie.txt / app.log）统一放 APP_DIR/data，
主下载目录默认放 APP_DIR/downloads，备用目录可在 .env 配置；.env 从 APP_DIR
读取。这样换台机器/改目录名都不会崩。
"""
import sys
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent      # 打包：exe 所在目录
    return Path(__file__).resolve().parent.parent          # 源码：项目根


APP_DIR = _app_dir()
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
_ENV_FILE = APP_DIR / ".env"


def _visual_model_dir() -> Path:
    """源码读项目资产；冻结包读 PyInstaller 的只读资源目录。"""
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
        return bundle_root / "models" / "visual"
    return APP_DIR / "models" / "visual"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8", extra="ignore")

    # 本地代理（爬取PH用）
    proxy: str = "http://127.0.0.1:7890"

    # 下载目录（默认 exe 同级 downloads）
    download_dir: str = str(APP_DIR / "downloads")
    # 备用下载目录，多个路径用英文分号分隔。主目录低于保留空间后，
    # 新任务会按顺序选择第一个空间充足的备用目录。
    download_fallback_dirs: str = ""

    # 模特头像统一由后端下载、校验并转换为本地 WebP，前端不再直连 PH CDN。
    avatar_dir: str = str(DATA_DIR / "avatars")
    avatar_fetch_concurrency: int = Field(default=3, ge=1, le=32)
    avatar_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)

    # 视频封面按 viewkey 缓存为本地 640x360 WebP。旧库里的 150x84 小图仍作为
    # 快速占位和网络失败回退，不覆盖历史 Video / DiscoveryItem 数据。
    preview_image_dir: str = str(DATA_DIR / "preview_images")
    preview_image_fetch_concurrency: int = Field(default=3, ge=1, le=8)
    preview_image_max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=64 * 1024, le=100 * 1024 * 1024)

    # 下载链路：yt-dlp 原生 HLS 是唯一网络下载引擎。
    # 下载代理与爬虫代理分离，空字符串表示下载直连，不做自动切换。
    download_proxy: str = ""
    # ffmpeg/ffprobe 所在目录（只用于下载后的封装与校验）。留空时依次查找冻结包 bin、
    # 项目 tools/ffmpeg 和 PATH。
    ffmpeg_dir: str = ""
    download_max_connections: int = Field(default=15, ge=2, le=256)
    download_max_fragment_workers_per_task: int = Field(default=12, ge=1, le=64)
    download_min_free_space_gb: float = Field(default=20.0, ge=0)
    download_socket_timeout: int = Field(default=10, ge=1, le=300)
    download_stall_timeout: int = Field(default=60, ge=5, le=3600)
    download_waiting_timeout: int = Field(default=3, ge=1, le=300)
    download_fragment_retries: int = Field(default=5, ge=0, le=20)
    download_resolve_timeout: int = Field(default=90, ge=5, le=1800)

    # 并发
    max_concurrent_downloads: int = Field(default=5, ge=1, le=64)
    # 近似总限速（MB/s，0=不限）与允许开始新下载的时段（HH:MM，均留空=全天）；
    # 页面修改写入 runtime_settings，重启后优先使用。
    download_rate_limit_mbps: float = Field(default=0.0, ge=0, le=1000)
    download_window_start: str = ""
    download_window_end: str = ""
    max_concurrent_uploads: int = Field(default=2, ge=1, le=32)
    # ffprobe / ffmpeg 会完整读取媒体并争用磁盘与内存，必须与网络下载槽位分开
    # 限流。默认只允许两条本地处理链并行，避免多个长视频同时扫包。
    media_processing_concurrency: int = Field(default=2, ge=1, le=8)
    # 完整上传者同步由后端统一调度：批量同步保持现有 4 路抓取强度，
    # 自动巡查使用更保守的 2 路；115 目录读取再由独立信号量做总限流。
    model_sync_concurrency: int = Field(default=4, ge=1, le=16)
    patrol_sync_concurrency: int = Field(default=2, ge=1, le=8)
    max_concurrent_115_reads: int = Field(default=6, ge=1, le=32)
    # 单模特完整同步的总墙钟预算。抓取器内部已经负责瞬时网络重试，外层必须
    # 有界，避免代理/上游故障时一个巡查 worker 被占用数十分钟。
    model_sync_timeout_seconds: float = Field(default=180.0, ge=30.0, le=3600.0)

    # 115 VIDEO HUB 根目录ID。默认置空：避免把个人网盘文件夹ID编进二进制，
    # 分发版强制使用者填自己的（在 .env 里设 VIDEO_HUB_CID）。
    video_hub_cid: str = ""

    # 时长模糊匹配容差（秒）
    duration_tolerance: int = Field(default=3, ge=0, le=60)

    # PH 站点
    ph_base_url: str = "https://cn.pornhub.com"

    # 自动巡查默认值；页面修改的启停/间隔写入 runtime_settings，重启后优先使用。
    patrol_enabled: bool = True            # 尚无运行时设置时随项目启动自动开启
    patrol_interval_hours: float = Field(default=6.0, gt=0, le=720)
    patrol_auto_download: bool = True      # 发现 missing 是否自动入下载队列（总闸）

    # 外网应用内登录会话。账号密码仍由服务器 Nginx 的 htpasswd 校验；这里只控制
    # 验证成功后本机签名 Cookie 的有效期。
    remote_session_hours: int = Field(default=12, ge=1, le=24 * 30)
    remote_remember_days: int = Field(default=30, ge=1, le=365)

    # Bark 推送通知默认值；页面修改的启停/URL/事件开关写入 runtime_settings，重启后优先使用。
    notify_bark_enabled: bool = False
    notify_bark_url: str = ""

    # 有下载/上传在跑时阻止 Windows 自动睡眠（不强制亮屏）；空闲后恢复。
    keep_awake_while_busy: bool = True

    # 115模特独立页面保留；旧推荐器、推荐来源和任务联动保持下线。
    library_page_v2: bool = True

    # “探索”页只恢复独立的本地视觉校准链路，不恢复旧推荐器、来源调度器或任务联动。
    # 模型文件必须通过同目录清单里的 SHA-256 与量化质量门禁。
    taste_visual_enabled: bool = True
    taste_visual_model_dir: str = str(_visual_model_dir())
    taste_visual_max_threads: int = Field(default=4, ge=1, le=8)
    taste_visual_batch_size: int = Field(default=12, ge=8, le=16)
    taste_visual_cover_timeout: float = Field(default=90.0, ge=20.0, le=300.0)

    # 数据库
    db_path: str = str(DATA_DIR / "data.db")

    # 115 cookie 存储文件
    cookie_file: str = str(DATA_DIR / "115_cookie.txt")

    @model_validator(mode="after")
    def validate_download_timeouts(self):
        if self.download_waiting_timeout >= self.download_stall_timeout:
            raise ValueError("DOWNLOAD_WAITING_TIMEOUT 必须小于 DOWNLOAD_STALL_TIMEOUT")
        return self

    def download_paths(self) -> tuple[str, ...]:
        """返回去重后的主下载目录和备用目录，顺序即调度优先级。"""
        candidates = [self.download_dir]
        candidates.extend(
            item.strip()
            for item in self.download_fallback_dirs.replace("\r", "\n")
            .replace("\n", ";").split(";")
            if item.strip()
        )
        paths: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            path = str(candidate or "").strip()
            if not path:
                continue
            key = str(Path(path).expanduser()).replace("\\", "/").rstrip("/").casefold()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
        return tuple(paths)


settings = Settings()
