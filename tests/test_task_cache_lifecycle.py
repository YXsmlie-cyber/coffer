"""仅使用内存目录夹具验证 TTL 与按键锁，不访问 115 或本地媒体。"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import backend.task_manager as task_manager_module
from backend.cloud115 import Cloud115Client
from backend.task_manager import TaskManager


class TaskCacheLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_legacy_index_preserves_order_and_duplicate_file_id(self):
        manager = TaskManager()
        index = task_manager_module.dedup.build_cloud_index([
            {"n": "First.mp4", "play_long": 60},
            {"n": "No duration.mp4"},
            {"n": "Second.mp4", "play_long": 60},
        ])
        index.pop("next_duration_idx")  # 旧索引没有追加序号。
        manager._dedup_cache["fixture"] = (1.0, index)
        new_file = {"fid": "new-file", "n": "Third.mp4", "play_long": 60}
        await manager._merge_confirmed_cloud_file("fixture", new_file)
        await manager._merge_confirmed_cloud_file("fixture", new_file)
        await manager._merge_confirmed_cloud_file("fixture", {
            "fid": "last-file", "n": "Last.mp4", "play_long": 60,
        })
        self.assertEqual([item["idx"] for item in index["duration_items"]], [0, 2, 3, 4])
        self.assertEqual(index["next_duration_idx"], 5)
        self.assertEqual(manager._dedup_cache["fixture"][0], 1.0)
        self.assertEqual(task_manager_module.dedup.match_video("", 60, index),
                         ("maybe", "First.mp4"))

    async def test_expired_directory_values_are_pruned_but_fresh_entries_stay(self):
        manager = TaskManager()
        clock = SimpleNamespace(now=0.0)
        listing = AsyncMock(return_value=[{"fid": "fixture", "n": "fixture.mp4"}])
        with patch.object(task_manager_module, "time", SimpleNamespace(
                monotonic=lambda: clock.now)), \
                patch.object(task_manager_module.cloud, "list_all_files", new=listing):
            for index in range(100):
                await manager._get_dedup_index(f"expired-{index}")
            clock.now = 59.0
            fresh = await manager._get_dedup_index("fresh")
            clock.now = 61.0
            await manager._get_dedup_index("new")
            self.assertIs(await manager._get_dedup_index("fresh"), fresh)
        self.assertEqual(set(manager._dedup_cache), {"fresh", "new"})
        self.assertEqual(manager._dedup_cache["fresh"][0], 59.0)
        self.assertEqual(listing.await_count, 102)
        self.assertEqual(len(manager._dedup_cache_locks), 0)

    async def test_cancelled_holder_does_not_split_waiters_onto_a_new_lock(self):
        manager = TaskManager()
        first_started = asyncio.Event()
        next_started = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def listing(cid, *, only_video):
            nonlocal calls
            calls += 1
            (first_started if calls == 1 else next_started).set()
            await finish.wait()
            return [{"fid": "fixture", "n": "fixture.mp4"}]

        with patch.object(task_manager_module.cloud, "list_all_files", new=listing):
            first = asyncio.create_task(manager._get_dedup_index("same"))
            await asyncio.wait_for(first_started.wait(), 1)
            original_lock = manager._dedup_cache_locks["same"]
            second = asyncio.create_task(manager._get_dedup_index("same"))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await asyncio.wait_for(next_started.wait(), 1)
            self.assertIs(manager._dedup_cache_locks["same"], original_lock)
            third = asyncio.create_task(manager._get_dedup_index("same"))
            await asyncio.sleep(0)
            self.assertEqual(calls, 2)
            finish.set()
            second_index, third_index = await asyncio.gather(second, third)
        self.assertIs(second_index, third_index)
        self.assertEqual(calls, 2)

    async def test_unused_cloud_directory_locks_are_released(self):
        with patch.object(Cloud115Client, "_load_cookie"):
            client = Cloud115Client()
        with patch.object(task_manager_module.settings, "video_hub_cid", "fixture"), \
                patch.object(client, "find_subdir", new=AsyncMock(return_value="cid")):
            for index in range(100):
                await client.ensure_model_dir(f"Fixture {index}")
        self.assertEqual(len(client._model_dir_locks), 0)
