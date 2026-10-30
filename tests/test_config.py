import unittest

from pydantic import ValidationError

from backend.config import Settings


class SettingsValidationTests(unittest.TestCase):
    def test_concurrency_cannot_silently_disable_queue(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, max_concurrent_uploads=0)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, max_concurrent_downloads=-1)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, model_sync_concurrency=0)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, patrol_sync_concurrency=0)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, max_concurrent_115_reads=0)

    def test_waiting_phase_must_finish_before_stall_timeout(self):
        with self.assertRaisesRegex(
            ValidationError,
            "DOWNLOAD_WAITING_TIMEOUT 必须小于 DOWNLOAD_STALL_TIMEOUT",
        ):
            Settings(
                _env_file=None,
                download_waiting_timeout=60,
                download_stall_timeout=60,
            )

    def test_documented_defaults_are_valid(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.max_concurrent_downloads, 5)
        self.assertEqual(settings.media_processing_concurrency, 2)
        self.assertEqual(settings.model_sync_concurrency, 4)
        self.assertEqual(settings.patrol_sync_concurrency, 2)
        self.assertEqual(settings.max_concurrent_115_reads, 6)
        self.assertEqual(settings.model_sync_timeout_seconds, 180.0)
        self.assertLess(
            settings.download_waiting_timeout,
            settings.download_stall_timeout,
        )

    def test_download_paths_keep_priority_and_remove_duplicates(self):
        settings = Settings(
            _env_file=None,
            download_dir="E:/downloads",
            download_fallback_dirs="F:/downloads; E:/downloads\nD:/downloads",
        )
        self.assertEqual(
            settings.download_paths(),
            ("E:/downloads", "F:/downloads", "D:/downloads"),
        )


if __name__ == "__main__":
    unittest.main()
