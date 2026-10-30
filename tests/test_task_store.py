import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import OperationalError

from backend.task_store import TaskStore, is_sqlite_locked


def locked_error():
    return OperationalError("UPDATE tasks", {}, Exception("database is locked"))


class TaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_write_retries_with_a_fresh_transaction(self):
        store = TaskStore(lambda: None, max_lock_retries=2, retry_delay=0.01)
        expected = object()
        store._update_once = AsyncMock(side_effect=[locked_error(), expected])

        with patch("backend.task_store.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await store.update(1, {"status": "running"})

        self.assertIs(result, expected)
        self.assertEqual(store._update_once.await_count, 2)
        sleep.assert_awaited_once_with(0.01)

    async def test_non_lock_database_error_is_not_hidden(self):
        store = TaskStore(lambda: None, max_lock_retries=2)
        error = OperationalError("UPDATE tasks", {}, Exception("disk I/O error"))
        store._update_once = AsyncMock(side_effect=error)

        with self.assertRaises(OperationalError):
            await store.update(1, {"status": "running"})

        self.assertEqual(store._update_once.await_count, 1)

    def test_lock_detection_is_specific(self):
        self.assertTrue(is_sqlite_locked(locked_error()))
        self.assertFalse(is_sqlite_locked(RuntimeError("disk I/O error")))


if __name__ == "__main__":
    unittest.main()
