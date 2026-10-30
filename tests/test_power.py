"""忙碌时阻止系统睡眠，空闲恢复；状态不变不重复调用系统接口。"""
import unittest
from unittest.mock import AsyncMock, Mock

from backend.network_health import LinkHealth
from backend.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
from backend.task_manager import TaskManager


class KeepAwakeTests(unittest.TestCase):
    def test_toggles_only_on_state_change(self):
        setter = Mock(return_value=1)
        keep = KeepAwake(setter)
        keep.update(True)
        keep.update(True)
        keep.update(False)
        keep.update(False)
        self.assertEqual([call.args[0] for call in setter.call_args_list],
                         [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS])
        self.assertFalse(keep.active)

    def test_disabled_or_unsupported_never_calls_system(self):
        setter = Mock(return_value=1)
        KeepAwake(setter, enabled=False).update(True)
        setter.assert_not_called()
        unsupported = KeepAwake(None)
        unsupported._setter = None
        unsupported.update(True)
        self.assertFalse(unsupported.active)
        self.assertFalse(unsupported.public()["supported"])

    def test_failed_system_call_keeps_previous_state(self):
        keep = KeepAwake(Mock(return_value=0))
        keep.update(True)
        self.assertFalse(keep.active)


class TaskManagerKeepAwakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_follows_running_and_retry_waiters_and_pause(self):
        setter = Mock(return_value=1)
        manager = TaskManager(link_health=LinkHealth(probe=AsyncMock(return_value=True)),
                              keep_awake=KeepAwake(setter))
        manager._update_keep_awake()
        self.assertFalse(manager._keep_awake.active)
        manager._running[1] = object()
        manager._update_keep_awake()
        self.assertTrue(manager._keep_awake.active)
        manager._running.clear()
        manager._download_retry_waiters[2] = object()
        manager._update_keep_awake()
        self.assertTrue(manager._keep_awake.active, "等待链路恢复/重试退避时仍算忙碌")
        manager._paused = True
        manager._update_keep_awake()
        self.assertFalse(manager._keep_awake.active)
        self.assertEqual(manager.runtime_statistics()["keep_awake"]["active"], False)
