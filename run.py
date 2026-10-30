"""启动入口: python run.py"""
import os

import uvicorn

if __name__ == "__main__":
    from backend.config import DATA_DIR
    from backend.logging_setup import setup_logging
    # 后台启动器把 stderr 写入 data/backend-stderr.log 以捕获未处理异常和下载子进程
    # 崩溃栈；此时关闭控制台日志副本，避免把轮转的 app.log 再无界地写一份。
    setup_logging(DATA_DIR, console=os.environ.get("PH115_CONSOLE_LOG", "1") != "0")
    # 只监听回环，且必须保留真实 TCP 对端供远程登录信任边界判断；若让 Uvicorn
    # 处理 X-Forwarded-For，request.client 会被改写成手机公网 IP。
    uvicorn.run(
        "backend.main:app", host="127.0.0.1", port=8000,
        reload=False, proxy_headers=False,
    )
