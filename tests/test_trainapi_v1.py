import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import trainapi  # noqa: E402


def handler():
    h = SimpleNamespace(calls=[])
    h._json = lambda payload, code=200, *a, **k: h.calls.append((payload, code))
    return h


class TrainV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = SimpleNamespace(get_routes={}, post_routes={}, post_limits={})
        trainapi.register(self.ctx, SimpleNamespace(state_dir=self.tmp.name))
        self.t = self.ctx.trainer

    def tearDown(self):
        self.tmp.cleanup()

    def test_routes_live_under_the_key_gated_prefix(self):
        for path in ("/api/v1/train/status", "/api/v1/train/models"):
            self.assertIn(path, self.ctx.get_routes)
        for path in ("/api/v1/train/chat", "/api/v1/train/stop"):
            self.assertIn(path, self.ctx.post_routes)
        self.assertNotIn("/api/v1/train/start", self.ctx.post_routes)   # starting a run stays on the page

    def test_status_with_nothing_trained(self):
        h = handler()
        self.ctx.get_routes["/api/v1/train/status"](h, {})
        payload, code = h.calls[-1]
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["running"])
        self.assertIsNone(payload["model"])
        self.assertEqual(payload["curve"], [])

    def test_status_summarises_the_log(self):
        log = os.path.join(self.tmp.name, "run.log")
        lines = []
        for i in range(1, 401):
            lines.append("Step %d/1000 | train loss %.3f | 2.0 steps/sec | 1,000 tokens/sec | total training step %d | learning rate 3e-4"
                         % (i, 5 - i / 100, i))
        lines += ["Validation at step 200: loss 3.900 | total training step 200",
                  "Validation at step 400: loss 3.100 | total training step 400", "ERROR: worker went away"]
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.t.job = {"model": "tiny", "log": log, "started": 1.0, "phase": "training", "machines": ["mac"], "mode": "single"}
        s = trainapi.widget_status(self.t, self.t.status(0))
        self.assertEqual(s["model"], "tiny")
        self.assertEqual(s["step"], 400)
        self.assertEqual(s["of"], 1000)
        self.assertAlmostEqual(s["progress"], 0.4)
        self.assertAlmostEqual(s["loss"], 1.0)
        self.assertAlmostEqual(s["val_loss"], 3.1)
        self.assertAlmostEqual(s["best_val_loss"], 3.1)
        self.assertEqual(s["tokens_per_sec"], 1000)
        self.assertEqual(s["error"], "worker went away")
        self.assertLessEqual(len(s["curve"]), trainapi.WIDGET_POINTS + 1)
        self.assertEqual(s["curve"][-1], [400, 1.0])
        self.assertIsNone(s["eta_seconds"])        # not running, so no time left
        self.assertNotIn("lines", s)
        json.dumps(s)

    def test_models_are_trimmed(self):
        mdir = os.path.join(self.t.models_dir, "m1")
        os.makedirs(mdir)
        with open(os.path.join(mdir, "model.json"), "w", encoding="utf-8") as f:
            json.dump({"parameters": 1000, "trained_steps": 5, "validation_loss": 2.5, "vocabulary": 300}, f)
        h = handler()
        self.ctx.get_routes["/api/v1/train/models"](h, {})
        (m,) = h.calls[-1][0]["models"]
        self.assertEqual(m["name"], "m1")
        self.assertEqual(m["parameters"], 1000)
        self.assertNotIn("resume_lr", m)

    def test_stop_with_nothing_running_is_a_409(self):
        h = handler()
        self.ctx.post_routes["/api/v1/train/stop"](h, {})
        self.assertEqual(h.calls[-1][1], 409)


if __name__ == "__main__":
    unittest.main()
