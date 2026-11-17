from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class AtomicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(Path(self.directory.name).resolve() / "state")

    def test_concurrent_feature_updates_preserve_each_others_keys(self):
        self.store.save_settings({"schema_version": 1, "ignore_paths": ["/chosen"]})
        def update(key):
            store = StateStore(self.store.root)
            for _ in range(8):
                def increment(settings):
                    value = settings.get(key, 0)
                    time.sleep(0.002)
                    settings[key] = value + 1
                store.update_settings(increment)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, ["favorites", "scopes", "alerts", "schedules"]))
        result = self.store.get_settings()
        self.assertEqual(result["ignore_paths"], ["/chosen"])
        for key in ("favorites", "scopes", "alerts", "schedules"):
            self.assertEqual(result[key], 8)

    def test_callback_exception_or_invalid_return_preserves_settings(self):
        original = self.store.save_settings({"schema_version": 1, "ignore_paths": ["/keep"]})
        def failing(settings):
            settings["ignore_paths"] = []
            raise RuntimeError("fixture")
        with self.assertRaises(RuntimeError):
            self.store.update_settings(failing)
        with self.assertRaises(CleanMacError):
            self.store.update_settings(lambda settings: ["invalid"])
        self.assertEqual(self.store.get_settings(), original)

    def test_explicit_replacement_is_serialized_and_returned(self):
        result = self.store.update_settings(lambda settings: {**settings, "value": 3})
        self.assertEqual(result, self.store.get_settings())


if __name__ == "__main__":
    unittest.main()
