"""有下载/上传在跑时阻止系统自动睡眠；空闲后恢复系统原有电源策略。

Windows 的 SetThreadExecutionState 只对调用线程生效、线程结束即失效，因此只在
事件循环所在的常驻线程里调用，不放进线程池。只请求“系统保持运行”，不强制
亮屏；其它平台为空操作。
"""
from __future__ import annotations

import logging
import sys


logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.SetThreadExecutionState
    except (AttributeError, OSError):
        return None


class KeepAwake:
    def __init__(self, setter=None, *, enabled: bool = True):
        self._setter = setter if setter is not None else _windows_setter()
        self.enabled = enabled
        self.active = False

    @property
    def supported(self) -> bool:
        return self._setter is not None

    def update(self, busy: bool) -> None:
        """按是否有任务在跑切换；状态不变时不重复调用系统接口。"""
        want = bool(busy and self.enabled and self.supported)
        if want == self.active:
            return
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if want else ES_CONTINUOUS
        try:
            if not self._setter(flags):
                logger.warning("设置系统睡眠策略失败（flags=%#x）", flags)
                return
        except Exception:
            logger.exception("设置系统睡眠策略异常")
            return
        self.active = want
        logger.info("任务%s，%s系统自动睡眠", "进行中" if want else "已空闲",
                    "阻止" if want else "恢复")

    def release(self) -> None:
        self.update(False)

    def public(self) -> dict:
        return {"supported": self.supported, "enabled": self.enabled, "active": self.active}
