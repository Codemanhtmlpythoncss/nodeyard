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
import trainapi  # noqa: E402

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

    def test_weights_endpoint_gives_chosen_rows_exactly(self):
        # The network diagram fetches just the rows its lines need (e.g. the output rows of the likeliest tokens).
        with tempfile.TemporaryDirectory() as d:
            t = trainapi.Trainer(d)
            os.makedirs(os.path.join(t.models_dir, "m"))
            model = T.GPT(50, 8, 16, 2, 1)
            sd = {k: v.detach() for k, v in model.state_dict().items()}
            torch.save(sd, os.path.join(t.models_dir, "m", "model.pt"))
            r = t.weights({"model": ["m"], "name": ["head.weight"], "rows": ["7,0,49,7"]})
            self.assertEqual(r["index"], [7, 0, 49, 7])
            self.assertEqual(r["rows"], [sd["head.weight"][i].tolist() for i in (7, 0, 49, 7)])
            self.assertEqual(t.weights({"model": ["m"], "name": ["head.bias"], "rows": ["0"]})["rows"], [sd["head.bias"].tolist()])
            for bad in ("50", "-1", "a,b"):
                with self.assertRaises(trainapi.TrainError):
                    t.weights({"model": ["m"], "name": ["head.weight"], "rows": [bad]})
            many = ",".join(["1"] * 500)
            self.assertEqual(len(t.weights({"model": ["m"], "name": ["head.weight"], "rows": [many]})["rows"]), trainapi.WEIGHT_ROWS_MAX)

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
        g = t["graph"]                           # the whole-network picture's lines: every feed-forward matrix and the head
        self.assertIn("head.weight", g)
        self.assertIn("blocks.layers.0.linear1.weight", g)
        self.assertIn("blocks.layers.0.linear2.weight", g)

    def test_graph_edges_keep_the_strongest_weights_into_each_neuron(self):
        import base64
        import numpy as np
        torch.manual_seed(0)
        w = torch.randn(6, 40)

        def decode(e):
            idx = np.frombuffer(base64.b64decode(e["idx"]), dtype="<u2" if e["ib"] == 2 else "<u4").reshape(e["shape"][0], e["k"])
            q = np.frombuffer(base64.b64decode(e["w"]), dtype=np.int8).reshape(e["shape"][0], e["k"])
            return idx, q

        e = T.graph_edges(w, 6 * 5)              # 30 lines for 6 neurons: the 5 biggest |w| into each
        self.assertEqual((e["shape"], e["k"], e["ib"]), ([6, 40], 5, 2))
        idx, q = decode(e)
        for r in range(6):
            self.assertEqual(set(idx[r].tolist()), set(w[r].abs().topk(5).indices.tolist()))
            for i, c in zip(idx[r], q[r]):       # int8 of w / scale: within half a step
                self.assertLessEqual(abs(c / 127 * e["scale"] - float(w[r, i])), e["scale"] / 254 + 1e-6)
        self.assertAlmostEqual(e["scale"], float(w.abs().max()), places=5)
        full = T.graph_edges(w, 10 ** 6)          # budget for every weight: all 40 into each, in order
        idx, q = decode(full)
        self.assertEqual(full["k"], 40)
        self.assertEqual(idx.tolist(), [list(range(40))] * 6)
        self.assertEqual(T.graph_edges(torch.randn(3, 70000), 3)["ib"], 4)   # more than 65535 inputs: 32-bit indices


if __name__ == "__main__":
    unittest.main()
