import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import trainapi  # noqa: E402
import trainpool  # noqa: E402
import trainworker  # noqa: E402
import training_data  # noqa: E402


class ParameterCountTests(unittest.TestCase):
    def test_formula_matches_pytorch(self):
        # Counted from the trainer's GPT with PyTorch: (vocab, context, width, layers) -> parameters.
        for args, expected in [((300, 32, 32, 2), 45_996), ((2048, 128, 192, 4), 2_592_896), ((5000, 256, 512, 10), 36_780_936)]:
            self.assertEqual(trainapi.count_parameters(*args), expected, args)


class RecommendTests(unittest.TestCase):
    HW = [
        {"device": "cpu", "cores": 4, "arch": "aarch64", "ram_free": 3e9},
        {"device": "cuda", "cores": 4, "arch": "x86_64", "ram_free": 8e9, "gpu": {"name": "GTX 1050 Ti", "vram": 4.2e9}},
        {"device": "mps", "cores": 10, "arch": "arm64", "ram_free": 10e9},
    ]

    def test_settings_are_valid_for_every_machine_and_data_size(self):
        for hw in self.HW:
            for chars in (None, 5_000, 200_000, 50_000_000, 2_000_000_000):
                for hours in (0.25, 1, 72):
                    r = trainapi.recommend(hw, chars, hours)
                    s = r["settings"]
                    self.assertEqual(s["dim"] % s["heads"], 0, (hw, chars))
                    self.assertGreaterEqual(s["vocab_size"], 512)
                    self.assertGreaterEqual(s["steps"], 200)
                    self.assertGreaterEqual(s["batch_size"], 1)
                    self.assertEqual(r["parameters"], trainapi.count_parameters(s["vocab_size"], s["context"], s["dim"], s["layers"]))
                    self.assertLessEqual(r["memory"], max(r["budget"], 16 * r["parameters"] + trainapi.activation_bytes(1, s["context"], s["dim"], s["layers"], s["heads"], s["vocab_size"])))
                    self.assertTrue(all(w["formula"] and w["numbers"] for w in r["why"]))

    def test_more_time_and_data_never_shrink_the_model(self):
        hw = self.HW[1]
        small = trainapi.recommend(hw, 1_000_000, 1)["parameters"]
        self.assertGreaterEqual(trainapi.recommend(hw, 100_000_000, 1)["parameters"], small)
        self.assertGreaterEqual(trainapi.recommend(hw, 100_000_000, 24)["parameters"], trainapi.recommend(hw, 100_000_000, 1)["parameters"])

    def test_embeddings_stay_under_half_the_model_when_the_vocab_can_shrink(self):
        r = trainapi.recommend(self.HW[0], 50_000_000, 1)
        s = r["settings"]
        if s["vocab_size"] > 512:
            self.assertLessEqual(2 * s["vocab_size"] * s["dim"], 0.5 * r["parameters"])


class TrainingDataTests(unittest.TestCase):
    def test_every_supported_shape_becomes_user_assistant_text(self):
        lines = [
            {"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]},
            {"conversations": [{"from": "human", "value": "Q1"}, {"from": "gpt", "value": "A1"}]},
            {"instruction": "Do it", "input": "now", "output": "Done"},
            {"prompt": "P", "completion": "C"},
            {"question": "Why?", "answer": "Because."},
            {"text": "Plain record."},
        ]
        text, report = training_data.normalize("\n".join(json.dumps(x) for x in lines) + "\nnot json\n{\"weird\": 1}\n")
        self.assertEqual(report["format"], "jsonl")
        self.assertEqual(report["records"], 6)
        self.assertEqual(report["skipped"], 2)
        self.assertTrue(report["chat"])
        for want in ("User: Hi\nAssistant: Hello", "User: Q1\nAssistant: A1", "User: Do it\nnow\nAssistant: Done",
                     "User: P\nAssistant: C", "User: Why?\nAssistant: Because.", "Plain record."):
            self.assertIn(want, text)
        self.assertNotIn("{", text)

    def test_json_list_text_and_chat_transcripts(self):
        _, rep = training_data.normalize(json.dumps([{"prompt": "a", "completion": "b"}]))
        self.assertEqual((rep["format"], rep["records"]), ("json", 1))
        _, rep = training_data.normalize("User: hi\nAssistant: yo\n\nUser: a\nAssistant: b\n")
        self.assertEqual((rep["format"], rep["chat"]), ("chat", True))
        text, rep = training_data.normalize("﻿Just a book.\r\nLine two.")
        self.assertEqual(rep["format"], "text")
        self.assertEqual(text, "Just a book.\nLine two.\n")


def post(t, fn, body):
    try:
        return fn(body)
    except trainapi.TrainError as e:
        return {"error": str(e), "code": e.code}


class TrainerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = trainapi.Trainer(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_names_cannot_escape_the_data_folder(self):
        for bad in ("../x.txt", "/etc/passwd", "a/b.txt", "x.exe", ".hidden.txt", ""):
            self.assertIn("error", post(self.t, self.t.paste, {"name": bad, "text": "hello"}), bad)
        self.assertEqual(post(self.t, self.t.paste, {"name": "ok file.txt", "text": "hello"})["size"], 5)
        self.assertEqual([d["name"] for d in self.t.datasets()], ["ok file.txt"])

    def test_upload_in_order_and_out_of_order(self):
        data = b"User: hi\nAssistant: hello\n" * 100
        a, b = data[:1000], data[1000:]
        up = lambda i, chunk, done: post(self.t, self.t.upload, {"id": "abcdef12", "name": "c.txt", "index": i,
                                                                   "data": base64.b64encode(chunk).decode(), "done": done})
        self.assertEqual(up(0, a, False)["received"], 1000)
        self.assertEqual(up(5, b, True)["code"], 409)
        self.assertTrue(up(1, b, True)["done"])
        with open(os.path.join(self.t.data_dir, "c.txt"), "rb") as f:
            self.assertEqual(f.read(), data)
        check = self.t.check("c.txt")
        self.assertEqual(check["format"], "chat")
        self.assertTrue(any(lvl == "bad" for lvl, _ in check["advice"]) or any(lvl == "warn" for lvl, _ in check["advice"]))

    def test_status_parses_the_trainer_log(self):
        log = os.path.join(self.t.logs_dir, "m.log")
        with open(log, "w") as f:
            f.write("Step 10/100 | train loss 5.5000 | 3.00 steps/sec | 1,234 tokens/sec | total training step 310 | learning rate 0.0005\n"
                    "Validation at step 10: loss 5.6000 | total training step 310\n"
                    "Gradient norm at step 10: 0.8000 (clipped to 1.0 when larger)\n")
        self.t.job = {"model": "m", "log": log, "pid": None}
        st = self.t.status(0)
        self.assertEqual(st["last"]["step"], 310)
        self.assertEqual(st["last"]["tps"], 1234)
        self.assertEqual(st["val"], [{"step": 310, "loss": 5.6}])
        self.assertEqual(st["grad"][0]["norm"], 0.8)
        self.assertEqual(self.t.status(st["next"])["lines"], [])

    def test_start_checks_settings_before_anything_runs(self):
        post(self.t, self.t.paste, {"name": "d.txt", "text": "x" * 2000})
        bad = post(self.t, self.t.start, {"data": ["d.txt"], "settings": {"dim": 63, "heads": 2, "steps": 10, "batch_size": 1, "context": 8,
                                                                       "layers": 1, "vocab_size": 300, "learning_rate": 0.001}})
        self.assertIn("divide", bad["error"])
        self.assertEqual(post(self.t, self.t.start, {"data": ["nope.txt"]})["code"], 404)
        self.assertFalse(self.t.running())

    def test_plan_picks_the_fastest_and_leaves_out_slow_machines(self):
        def m(name, speed, usable=True, reason=""):
            return {"name": name, "speed": speed, "usable": usable, "reason": reason, "device": "cpu", "ram_free": 4e9, "gpu": None}
        machines = [m("gpu", 8e11), m("mini", 3e11), m("pi", 3e10), m("dead", 9e12, False, "not ready")]
        machines.sort(key=lambda x: (not x["usable"], -x["speed"]))
        p = self.t.plan(machines)
        self.assertEqual(p["lead"], "gpu")
        self.assertEqual(p["share"], ["gpu", "mini"])
        self.assertTrue(any("pi left out" in n for n in p["notes"]))
        self.assertAlmostEqual(p["speed"], 8e11 + 0.8 * 3e11)
        self.assertEqual(self.t.plan(machines, ["pi", "mini"])["share"], ["mini", "pi"])   # chosen by hand: kept
        none = self.t.plan([m("dead", 1e9, False, "not ready")])
        self.assertIsNone(none["lead"])
        self.assertIn("not ready", none["notes"][0])

    def test_pod_for_a_gpu_node(self):
        pool = self.t.pool
        pod = pool.pod_manifest({"name": "gpu-box", "mem_total": 16 * 1024 ** 3, "labels": {"nodeyard.io/gpu": "nvidia"}})
        spec = pod["spec"]
        self.assertEqual(spec["nodeName"], "gpu-box")
        self.assertEqual(spec["runtimeClassName"], "nvidia")
        self.assertEqual(spec["volumes"][2]["hostPath"]["path"], trainpool.NODE_DIR)
        self.assertIn("limits", spec["containers"][0]["resources"])
        self.assertNotIn("runtimeClassName", pool.pod_manifest({"name": "pi", "labels": {}})["spec"])
        kinds = [x["kind"] for x in pool.base_manifest()]
        self.assertEqual(kinds, ["Namespace", "Secret", "ConfigMap"])
        self.assertEqual(set(pool.base_manifest()[2]["data"]), set(trainpool.CODE_FILES))

    def test_register_adds_routes(self):
        ctx = SimpleNamespace(get_routes={}, post_routes={}, post_limits={}, store=None)
        trainapi.register(ctx, SimpleNamespace(demo=True, state_dir=self.dir.name))
        self.assertIn("/api/train", ctx.get_routes)
        self.assertIn("/api/train/start", ctx.post_routes)
        self.assertIn("/api/train/upload", ctx.post_limits)
        self.assertNotIn("/api/train/setup", ctx.post_routes)   # PyTorch installs itself on each machine now


class WorkerTests(unittest.TestCase):
    """The training worker's file and job API, without PyTorch."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.token = "t" * 32
        self.workers = []
        self.remotes = []
        for i in range(2):
            w = trainworker.Worker(os.path.join(self.dir.name, "w%d" % i), str(ROOT / "share" / "nodeyard" / "jobs"))
            w.deps["state"] = "installing"
            srv = trainworker.Server(("127.0.0.1", 0), trainworker.make_handler(w, self.token))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.workers.append((w, srv))
            self.remotes.append(trainpool.Remote("w%d" % i, "http://127.0.0.1:%d" % srv.server_address[1], self.token))

    def tearDown(self):
        for _, srv in self.workers:
            srv.shutdown()
            srv.server_close()
        self.dir.cleanup()

    def test_token_is_required(self):
        bad = trainpool.Remote("w0", self.remotes[0].url, "x" * 32)
        with self.assertRaises(trainpool.PoolError):
            bad.status()
        self.assertTrue(bad.healthy())   # /healthz needs no token
        self.assertEqual(self.remotes[0].status()["deps"]["state"], "installing")

    def test_files_go_up_in_pieces_come_back_and_keep_their_version(self):
        r = self.remotes[0]
        src = os.path.join(self.dir.name, "big.txt")
        with open(src, "wb") as f:
            f.write(os.urandom(trainpool.CHUNK * 2 + 123))
        v = trainpool.file_version(src)
        r.put_file("data/big.txt", src, v)
        self.assertEqual(r.ls("data")["big.txt"]["version"], v)
        back = os.path.join(self.dir.name, "back.txt")
        r.get_file("data/big.txt", back)
        with open(src, "rb") as a, open(back, "rb") as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(self.workers[0][0].data_dir, "big.txt"), "ab") as f:   # changed on the worker: the old version no longer holds
            f.write(b"x")
        self.assertNotEqual(r.ls("data")["big.txt"]["version"], v)

    def test_paths_stay_inside_the_worker(self):
        r = self.remotes[0]
        for bad in ("../x", "data/../../etc/passwd", "etc/passwd", "models/a/b/c/d", "data/.hidden"):
            with self.assertRaises(trainpool.PoolError, msg=bad):
                r.call("POST", "/delete", {"path": bad})

    def test_machines_swap_files_directly(self):
        a, b = self.remotes
        src = os.path.join(self.dir.name, "model.pt")
        with open(src, "wb") as f:
            f.write(b"weights" * 1000)
        a.put_file("models/m/model.pt", src, "7000:1")
        b.call("POST", "/pull", {"url": a.url, "path": "models/m/model.pt", "dest": "models/m/peer.pt", "version": "7000:1"})
        self.assertEqual(b.ls("models/m")["peer.pt"]["size"], 7000)

    def test_jobs_wait_for_pytorch_and_options_cannot_redirect_files(self):
        r = self.remotes[0]
        with self.assertRaises(trainpool.PoolError) as e:
            r.call("POST", "/train", {"model": "m", "data": [], "options": []})
        self.assertIn("PyTorch", str(e.exception))
        self.workers[0][0].deps["state"] = "ready"
        for opt in ("--app-data", "--result-file=/etc/x", "--data-file"):
            with self.assertRaises(trainpool.PoolError):
                r.call("POST", "/train", {"model": "m", "data": [], "options": [opt, "/tmp/x"]})


if __name__ == "__main__":
    unittest.main()
