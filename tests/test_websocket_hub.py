"""WebSocket 广播提取前后的终态、合并和慢客户端边界。"""
import asyncio
import unittest

from backend.main import WSHub


class WebSocketHubContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_survives_coalescing_and_retry_opens_next_lifecycle(self):
        hub = WSHub()
        received = []
        delivered = asyncio.Event()

        class Client:
            async def accept(self):
                pass

            async def send_json(self, message):
                received.append(message)
                delivered.set()

        client = Client()
        try:
            await hub.connect(client)
            for status, progress in (
                ("running", 10), ("running", 80), ("completed", 100), ("running", 90),
            ):
                await hub.broadcast({"type": "task_update", "task": {
                    "id": 1, "status": status, "progress": progress}})
            await asyncio.wait_for(delivered.wait(), timeout=1)
            self.assertEqual([item["task"]["status"] for item in received], ["completed"])
            delivered.clear()
            await hub.broadcast({"type": "task_update", "task": {
                "id": 1, "status": "queued", "progress": 0}})
            await hub.broadcast({"type": "task_update", "task": {
                "id": 1, "status": "running", "progress": 5}})
            await asyncio.wait_for(delivered.wait(), timeout=1)
            self.assertEqual([item["task"]["status"] for item in received],
                             ["completed", "running"])
            self.assertEqual(received[-1]["task"]["progress"], 5)
        finally:
            await hub.close()

    async def test_reliable_overflow_disconnects_client_without_waiting_for_network(self):
        hub = WSHub()
        hub._MAX_RELIABLE = 2
        closed = asyncio.Event()
        close_codes = []

        class Client:
            async def accept(self):
                pass

            async def send_json(self, _message):
                await asyncio.Event().wait()

            async def close(self, *, code, reason):
                close_codes.append(code)
                closed.set()

        client = Client()
        try:
            await hub.connect(client)
            for index in range(3):
                await hub.broadcast({"type": "reliable_event", "value": index})
            self.assertNotIn(client, hub.clients)
            # 关闭任务在完成前必须被 hub 强引用，不能依赖事件循环的弱引用。
            self.assertEqual(len(hub._closing), 1)
            await asyncio.wait_for(closed.wait(), timeout=1)
            self.assertEqual(close_codes, [1013])
            await asyncio.sleep(0.01)
            self.assertEqual(hub._closing, set())
        finally:
            await hub.close()

    async def test_shutdown_does_not_wait_for_hung_slow_client_close(self):
        hub = WSHub()
        hub._MAX_RELIABLE = 1

        class Client:
            async def accept(self):
                pass

            async def send_json(self, _message):
                await asyncio.Event().wait()

            async def close(self, *, code, reason):
                await asyncio.Event().wait()

        await hub.connect(Client())
        for index in range(2):
            await hub.broadcast({"type": "reliable_event", "value": index})
        self.assertEqual(len(hub._closing), 1)
        await asyncio.wait_for(hub.close(), timeout=1)
        self.assertEqual(hub._closing, set())


if __name__ == "__main__":
    unittest.main()
