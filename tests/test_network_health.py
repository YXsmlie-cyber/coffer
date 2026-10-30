"""共享链路断流判定：探测失败才算断流，单一复查循环恢复后唤醒等待者。"""
import asyncio
import unittest
from unittest.mock import AsyncMock

from backend.network_health import LinkHealth, ProbeTarget

_real_sleep = asyncio.sleep


async def tick(_seconds):
    # 复查间隔在测试里缩成一次让出，保证循环不会独占事件循环。
    await _real_sleep(0)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LinkHealthTests(unittest.IsolatedAsyncioTestCase):
    def test_probe_target_keeps_only_origin(self):
        target = ProbeTarget.for_url(
            "https://ev-h.phncdn.com/hls/v/1080.mp4/master.m3u8?validto=1&hash=x", "http://127.0.0.1:7891")
        self.assertEqual(target.url, "https://ev-h.phncdn.com/")
        self.assertEqual(target.proxy, "http://127.0.0.1:7891")
        self.assertEqual(target.host, "ev-h.phncdn.com")
        self.assertEqual(ProbeTarget.for_url("http://127.0.0.1:8000/x").url, "http://127.0.0.1:8000/")
        self.assertIsNone(ProbeTarget.for_url("file:///etc/passwd"))
        self.assertIsNone(ProbeTarget.for_url(""))

    async def test_healthy_link_is_probed_once_for_a_burst_of_failures(self):
        probe = AsyncMock(return_value=True)
        health = LinkHealth(probe=probe, sleep=tick)
        target = ProbeTarget.for_url("https://cdn.example/a.m3u8")
        results = await asyncio.gather(*(health.check(target) for _ in range(5)))
        self.assertEqual(results, [True] * 5)
        probe.assert_awaited_once()
        self.assertFalse(health.outage_active)
        self.assertTrue(await health.check(None))

    async def test_outage_waits_for_single_monitor_and_records_history(self):
        clock = FakeClock()
        answers = iter([False, False, False, True])

        async def probe(_target):
            clock.now += 5
            return next(answers)

        events = []
        health = LinkHealth(probe=probe, clock=clock, sleep=tick)
        health.add_listener(lambda event, seconds: events.append((event, seconds)))
        target = ProbeTarget.for_url("https://cdn.example/a.m3u8")

        self.assertFalse(await health.check(target))
        self.assertTrue(health.outage_active)
        # 断流期间其它任务的检查直接得到“不通”，不再各自探测。
        self.assertFalse(await health.check(target))
        self.assertTrue(await health.wait_restored(timeout=5))
        self.assertFalse(health.outage_active)
        self.assertEqual([event for event, _ in events], ["outage", "restored"])
        self.assertEqual(events[-1][1], 15.0)
        summary = health.public()
        self.assertEqual(summary["recent_outages"], 1)
        self.assertEqual(summary["recent_outage_seconds"], 15.0)
        self.assertFalse(summary["outage"])
        # 恢复后刚确认可用的目标在缓存期内不重复探测。
        self.assertTrue(await health.check(target))
        await health.close()

    async def test_wait_times_out_while_link_stays_down(self):
        health = LinkHealth(probe=AsyncMock(return_value=False), sleep=tick)
        target = ProbeTarget.for_url("https://cdn.example/a.m3u8")
        self.assertFalse(await health.check(target))
        self.assertFalse(await health.wait_restored(timeout=0.02))
        self.assertTrue(health.outage_active)
        self.assertEqual(health.public()["recent_outages"], 1)
        await health.close()

    async def test_history_only_counts_last_hour(self):
        clock = FakeClock()
        answers = iter([False, True])
        health = LinkHealth(probe=AsyncMock(side_effect=lambda _t: next(answers)),
                            clock=clock, sleep=tick)
        target = ProbeTarget.for_url("https://cdn.example/a.m3u8")
        await health.check(target)
        await health.wait_restored(timeout=5)
        clock.now += 3601
        self.assertEqual(health.public()["recent_outages"], 0)
        await health.close()
