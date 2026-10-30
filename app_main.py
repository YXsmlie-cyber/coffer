"""打包专用入口（PyInstaller 从这里起）。

与 run.py 的区别：
  - 传 app 对象给 uvicorn（不用 import-string，打包后字符串导入会找不到模块）。
  - 因为分发版选了隐藏窗口（console=False），把日志同时写到 data/app.log，
    对方启动失败时把这个文件发回来即可定位问题。
  - 端口自动选择：优先 8000，被占则顺延找空闲端口，实际端口写入 data/port.txt
    供启动脚本读取后打开浏览器。这样对方机器即使 8000 被占也能正常用。
"""
import logging
import multiprocessing
from pathlib import Path
import hashlib
import os
import socket
import sys

import uvicorn

_PREFERRED_PORT = 8000


class _AlreadyRunning(RuntimeError):
    pass


class _SingleInstanceGuard:
    """按应用目录加进程锁，避免两个实例同时读写同一 SQLite/下载目录。"""

    def __init__(self, app_dir: Path):
        identity = hashlib.sha256(
            str(app_dir.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:20]
        self._handle = None
        self._file = None
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                               ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateMutexW(None, False, f"Local\\PH115-{identity}")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                raise _AlreadyRunning("PH115 已在运行")
            self._handle = handle
            self._close_handle = kernel32.CloseHandle
        else:
            import fcntl

            lock_path = app_dir / "data" / ".instance.lock"
            self._file = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._file.close()
                self._file = None
                raise _AlreadyRunning("PH115 已在运行") from exc

    def close(self):
        if self._handle is not None:
            self._close_handle(self._handle)
            self._handle = None
        if self._file is not None:
            self._file.close()
            self._file = None


def _pick_port() -> int:
    """优先 8000；被占则在 8000-8019 内顺延找一个能绑定的端口。"""
    for port in range(_PREFERRED_PORT, _PREFERRED_PORT + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return _PREFERRED_PORT   # 都被占就回退 8000，让 uvicorn 报错进 app.log


def _setup_logging(data_dir):
    from backend.logging_setup import setup_logging
    setup_logging(data_dir, console=not getattr(sys, "frozen", False))


def _verify_runtime_dependencies():
    """冻结包启动即验证硬依赖，禁止把缺包错误拖到用户扫码或下载时暴露。"""
    import numpy
    import onnxruntime
    import p115client
    import p115client.tool.upload
    from concurrenttools import Return
    import yt_dlp

    del numpy, onnxruntime, p115client, Return
    version = getattr(getattr(yt_dlp, "version", None), "__version__", "")
    if version not in {"2026.06.09", "2026.6.9"}:
        raise RuntimeError(
            f"yt-dlp 版本不一致：需要 2026.6.9，当前为 {version or '未知'}")


def main():
    instance_guard = None
    pid_file = None
    try:
        # 日志必须早于 backend 导入建立；即使冻结包缺模块，也能在 app.log 留下根因。
        app_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                   else Path(__file__).resolve().parent)
        early_data_dir = app_dir / "data"
        early_data_dir.mkdir(parents=True, exist_ok=True)
        _setup_logging(early_data_dir)
        try:
            instance_guard = _SingleInstanceGuard(app_dir)
        except _AlreadyRunning:
            logging.getLogger("ph115").warning(
                "PH115 is already running for app dir %s", app_dir)
            return
        _verify_runtime_dependencies()

        # 延迟导入应用：PyInstaller spawn 子进程先在 __main__ 调用 freeze_support，
        # 下载 worker 不会无谓初始化 FastAPI、数据库和任务管理器。
        from backend.config import DATA_DIR
        from backend.main import app

        port = _pick_port()
        # 把实际端口写出来，供启动器读取后打开正确的浏览器地址
        # （发行版的 启动.bat 与开发版的 scripts/dev_launcher.ps1 都读这个文件）。
        (DATA_DIR / "port.txt").write_text(str(port), encoding="utf-8")
        pid_file = DATA_DIR / "pid.txt"
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        logging.getLogger("ph115").info(
            "PH115 starting, data dir=%s, port=%s", DATA_DIR, port)
        # 应用只监听回环；关闭代理头改写，确保 request.client 保留真实 TCP 对端，
        # 供外网登录验证 Nginx/SSH 隧道确实来自本机。
        uvicorn.run(
            app, host="127.0.0.1", port=port,
            log_level="info", log_config=None, proxy_headers=False,
        )
    except Exception:
        # 正常情况下这里会把导入期和启动期异常写入 data/app.log。
        logging.getLogger("ph115").exception("PH115 crashed during import or startup")
        raise
    finally:
        if pid_file is not None:
            try:
                if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                    pid_file.unlink()
            except OSError:
                pass
        if instance_guard is not None:
            instance_guard.close()


if __name__ == "__main__":
    # Windows / PyInstaller 的下载工作进程会重新进入 exe；必须先让
    # multiprocessing 识别工作进程参数，且 uvicorn 只能由主进程启动。
    multiprocessing.freeze_support()
    main()
