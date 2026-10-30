from pathlib import Path
import tempfile
import unittest

from app_main import _AlreadyRunning, _SingleInstanceGuard


class SingleInstanceTests(unittest.TestCase):
    def test_same_app_directory_cannot_acquire_two_instance_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory)
            (app_dir / "data").mkdir()
            first = _SingleInstanceGuard(app_dir)
            try:
                with self.assertRaises(_AlreadyRunning):
                    _SingleInstanceGuard(app_dir)
            finally:
                first.close()
            second = _SingleInstanceGuard(app_dir)
            second.close()


if __name__ == "__main__":
    unittest.main()
