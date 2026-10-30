"""统一轮转日志；请求关联标识不包含用户内容。"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .diagnostics import request_id


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


def log_handlers(data_dir, *, console=True, max_bytes=5 * 1024 * 1024, backup_count=3):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(Path(data_dir) / "app.log", maxBytes=max_bytes,
                                   backupCount=backup_count, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s request=%(request_id)s: %(message)s"))
    return handlers


def setup_logging(data_dir, *, console=True):
    logging.basicConfig(level=logging.INFO, handlers=log_handlers(data_dir, console=console))
