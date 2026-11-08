import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import aiapi  # noqa: E402


def _store(split):
    return SimpleNamespace(snapshot=lambda: {"state": {"ai": {"split": split}}})


def _fake_backend(split, cache=None, refresh_calls=None):
    """A stand-in with just the attributes Live.disk_models touches, so we can exercise
    the load gate without building a whole Live backend."""
    refresh_calls = refresh_calls if refresh_calls is not None else []
    fake = SimpleNamespace(
        store=_store(split),
        models_lock=threading.Lock(),
        models_cache=cache,
        models_dirty=True,                 # stale -> a scan would run but for the gate
        MODEL_CACHE_TTL=120,
        models_refreshing=False,
        models_generation=0,
        models_error="",
        models_deleted={},
        _downloads=lambda: [],
        _refresh_models=lambda generation: refresh_calls.append(generation),
    )
    fake._split_loading = lambda: aiapi.Live._split_loading(fake)
    return fake


class SplitLoadingTests(unittest.TestCase):
    def test_true_only_while_loaded_but_not_ready(self):
        self.assertTrue(aiapi.Live._split_loading(SimpleNamespace(store=_store({"loaded": True, "ready": False}))))
        self.assertFalse(aiapi.Live._split_loading(SimpleNamespace(store=_store({"loaded": True, "ready": True}))))
        self.assertFalse(aiapi.Live._split_loading(SimpleNamespace(store=_store({"loaded": False, "ready": False}))))
        self.assertFalse(aiapi.Live._split_loading(SimpleNamespace(store=_store(None))))

    def test_survives_a_broken_snapshot(self):
        bad = SimpleNamespace(store=SimpleNamespace(snapshot=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertFalse(aiapi.Live._split_loading(bad))


class DiskScanGateTests(unittest.TestCase):
    def test_no_scan_while_the_model_is_loading(self):
        calls = []
        fake = _fake_backend({"loaded": True, "ready": False},
                             cache=(time.time(), {"nodes": [{"node": "debian-1"}], "in_use": ""}),
                             refresh_calls=calls)
        data = aiapi.Live.disk_models(fake, force=True)   # force would normally scan
        self.assertTrue(data["scan_paused"])
        self.assertFalse(fake.models_refreshing)          # the background scan never started
        self.assertEqual(calls, [])                        # _refresh_models was not launched
        self.assertEqual(data["nodes"], [{"node": "debian-1"}])  # served the last inventory

    def test_scan_runs_when_not_loading(self):
        calls = []
        fake = _fake_backend({"loaded": True, "ready": True},   # ready -> not loading
                             cache=(time.time() - 1000, {"nodes": [], "in_use": ""}),
                             refresh_calls=calls)
        data = aiapi.Live.disk_models(fake, force=True)
        self.assertFalse(data["scan_paused"])
        self.assertTrue(fake.models_refreshing)            # a scan was started

    def test_loading_with_no_inventory_yet_does_not_error(self):
        fake = _fake_backend({"loaded": True, "ready": False}, cache=None)
        data = aiapi.Live.disk_models(fake, force=True)    # no cache + loading must not raise
        self.assertTrue(data["scan_paused"])
        self.assertEqual(data["nodes"], [])


if __name__ == "__main__":
    unittest.main()
