"""The whole-network view: model.pt read without PyTorch, and the trace of one forward pass (needs PyTorch)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))
import ptread  # noqa: E402

try:
    import torch
    import train_bpe_gpt_torch as T
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "PyTorch isn't installed here")
class TraceTests(unittest.TestCase):
    def test_model_pt_is_read_exactly_without_torch(self):
        with tempfile.TemporaryDirectory() as d:
            model = T.GPT(50, 8, 16, 2, 1)
            torch.save({k: v.detach() for k, v in model.state_dict().items()}, os.path.join(d, "model.pt"))
            m = ptread.Model(os.path.join(d, "model.pt"))
            for name, v in model.state_dict().items():
                flat = [x for row in m.rows(name) for x in row]
                self.assertEqual(flat, v.reshape(-1).tolist(), name)
            self.assertEqual(m.rows("blocks.layers.0.linear1.weight", 3, 2), model.state_dict()["blocks.layers.0.linear1.weight"][3:5].tolist())
            m.close()

    def test_trace_has_every_step_and_matches_pytorch(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.json")
            subprocess.run([sys.executable, str(ROOT / "share/nodeyard/jobs/train_bpe_gpt_torch.py"), "trace", "--prompt", "Hi there", "--out", out],
                           check=True, capture_output=True, cwd=str(ROOT / "share/nodeyard/jobs"))
            t = json.loads(Path(out).read_text())
        kinds = {o["how"]["kind"] for o in t["ops"]}
        self.assertEqual(kinds, {"lookup", "add", "layernorm", "linear", "dot", "softmax", "mix", "concat", "gelu"})
        self.assertLess(t["check_max_difference"], 1e-4)
        scores = next(o for o in t["ops"] if o["how"]["kind"] == "dot")["values"]
        self.assertIsNone(scores[0][1])          # a later token is masked: −∞ is written as null
        q = next(o for o in t["ops"] if o["title"].startswith("Queries"))
        h = q["how"]
        x = t["ops"][h["x"]]["values"][1]
        w = t["weights"][h["weight"]][h["offset"] + 2]
        b = t["vectors"][h["bias"]][h["offset"] + 2]
        self.assertAlmostEqual(sum(a * c for a, c in zip(x, w)) + b, q["values"][1][2], places=5)   # the page's sum of products


if __name__ == "__main__":
    unittest.main()
