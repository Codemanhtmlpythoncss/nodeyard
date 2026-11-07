import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import lifecycle  # noqa: E402


class Backend:
    def __init__(self, split=None, ollama=None):
        self.store = SimpleNamespace(snapshot=lambda: {"state": {"ai": {"split": split}}})
        self.ollama = ollama or []
        self.run_calls = []
        self.keep_alive_calls = []
        self.job_status = "running"

    def jobs_running(self):
        return False

    def split_slots(self):
        return {"processing": False, "task": None}

    def ollama_overview(self):
        return self.ollama

    def ollama_keep_alive(self, pod, model, value):
        self.keep_alive_calls.append((pod, model, value))

    def run(self, action, params):
        self.run_calls.append((action, params))
        return "job-%d" % len(self.run_calls)

    def job(self, _job_id, _wait=0):
        return {"status": self.job_status}


class ModelWarmthTests(unittest.TestCase):
    def make_lifecycle(self, backend, raw_settings=None):
        prefs = {"model_lifecycle": raw_settings or {}}
        return lifecycle.Lifecycle(backend, lambda: dict(prefs), lambda value: prefs.update(value))

    def test_keep_loaded_is_enabled_by_default(self):
        self.assertTrue(lifecycle.stored_settings({})["keep_loaded"])

    def test_ollama_chat_is_kept_loaded_when_keep_loaded_is_on(self):
        backend = Backend()
        lc = self.make_lifecycle(backend)

        lc._after_ollama_use("ollama:ollama-0:gpt-oss:20b")

        self.assertEqual(backend.keep_alive_calls, [("ollama-0", "gpt-oss:20b", -1)])

    def test_manual_ollama_load_uses_indefinite_keep_alive_when_keep_loaded_is_on(self):
        lc = self.make_lifecycle(Backend(), {"enabled": True, "idle_seconds": 300, "keep_loaded": True})

        self.assertEqual(lc.load_keep_alive(), -1)

    def test_keep_loaded_preloads_unloaded_split_once(self):
        backend = Backend(split={"model": "sample.gguf", "loaded": False, "ready": False, "stale": None})
        lc = self.make_lifecycle(backend)

        lc.tick(now=1000)
        lc.tick(now=1001)

        self.assertEqual([action for action, _ in backend.run_calls], ["split-load"])

    def test_keep_loaded_does_not_hourly_reload_idle_split(self):
        backend = Backend(split={"model": "sample.gguf", "loaded": True, "ready": True, "stale": None, "shares": []})
        lc = self.make_lifecycle(backend)

        lc.tick(now=1000)
        lc.tick(now=4601)

        self.assertEqual(backend.run_calls, [])

    def test_startup_policy_keeps_loaded_ollama_indefinitely(self):
        backend = Backend(ollama=[{"pod": "ollama-0", "node": "node-1", "models": [{"name": "qwen", "loaded": True}]}])
        lc = self.make_lifecycle(backend)

        lc.tick(now=1000)

        self.assertEqual(backend.keep_alive_calls, [("ollama-0", "qwen", -1)])


if __name__ == "__main__":
    unittest.main()
