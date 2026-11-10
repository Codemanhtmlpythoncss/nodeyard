import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import trainapi  # noqa: E402
import trainpool  # noqa: E402
import trainworker  # noqa: E402
import trainsets  # noqa: E402
import starter_dataset  # noqa: E402
import lr_governor  # noqa: E402
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

    def test_small_data_gets_a_model_big_enough_to_learn_it(self):
        # The complete works of Shakespeare as chat pairs (~8M characters): the old rule (tokens ÷ 5) gave ~400K weights
        # and the loss went flat early; nanoGPT's Shakespeare model has ~10 weights per character.
        r = trainapi.recommend(self.HW[1], 7_948_437, 8)
        self.assertGreater(r["parameters"], 2_000_000)
        self.assertEqual(r["settings"]["dropout"], 0.2)
        self.assertEqual(trainapi.recommend(self.HW[1], 2_000_000_000, 1)["settings"]["dropout"], 0.0)

    def test_a_fast_machine_with_hours_gets_a_nanogpt_sized_model(self):
        prof = {"unique_fraction": 0.5, "example_chars": 190, "chat": True}
        r = trainapi.recommend(self.HW[1], 7_948_437, 8, prof)          # the GTX 1050 Ti, overnight
        self.assertGreater(r["parameters"], 8_000_000)
        quick = trainapi.recommend(self.HW[1], 7_948_437, 0.25, prof)
        self.assertEqual(quick["limit"], "compute")                     # 15 minutes: time is the limit, said so
        self.assertLess(quick["parameters"], r["parameters"])

    def test_overview_says_how_to_get_a_bigger_model(self):
        ctx = SimpleNamespace(get_routes={}, post_routes={}, post_limits={}, store=None)
        with tempfile.TemporaryDirectory() as d:
            trainapi.register(ctx, SimpleNamespace(demo=True, state_dir=d))
            ctx.trainer.pool.ensure = lambda *a, **k: None   # don't start a worker in a unit test
            got = {}
            ctx.get_routes["/api/train"](SimpleNamespace(_json=lambda obj, code=200: got.update(obj)), {"hours": ["0.25"]})
        times = got["recommend"]["bigger"]["times"]
        self.assertEqual([t["hours"] for t in times], [0.25, 1, 3, 8, 24, 72])
        self.assertTrue(all(b["parameters"] >= a["parameters"] for a, b in zip(times, times[1:])))

    def test_settings_follow_the_dataset(self):
        hw = self.HW[1]
        plain = trainapi.recommend(hw, 8_000_000, 8)
        repeats = trainapi.recommend(hw, 8_000_000, 8, {"unique_fraction": 0.5, "example_chars": 190, "chat": True})
        self.assertLess(repeats["tokens"], plain["tokens"] * 0.55)            # sized for the unique half
        self.assertTrue(repeats["settings"]["assistant_only"])
        self.assertFalse(plain["settings"]["assistant_only"])
        long_examples = trainapi.recommend(hw, 8_000_000, 8, {"unique_fraction": 1.0, "example_chars": 1500, "chat": False})
        self.assertEqual(long_examples["settings"]["context"], 512)            # 1500 chars ≈ 417 tokens → room for a whole one
        self.assertTrue(any(w["name"] == "Learn only the replies" for w in repeats["why"]))

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

    def test_every_step_reports_its_gradient(self):
        log = os.path.join(self.t.logs_dir, "s.log")
        with open(log, "w") as f:
            for i in range(1, 4):
                f.write("Step %d/3 | train loss 5.0 | 1.00 steps/sec | 100 tokens/sec | total training step %d | learning rate 0.001 | gradient norm %.1f\n" % (i, i, i / 2))
            f.write("Gradient norm at step 3: 9.9 (clipped to 1.0 when larger)\n")   # the per-check line no longer doubles up
        self.t.job = {"model": "m", "log": log}
        st = self.t.status(0)
        self.assertEqual([p["step"] for p in st["train"]], [1, 2, 3])
        self.assertEqual([(g["step"], g["norm"]) for g in st["grad"]], [(1, 0.5), (2, 1.0), (3, 1.5)])

    def test_status_keeps_memory_bounded_on_a_long_fast_run(self):
        # A fast run (e.g. 30k tokens/sec) logs one step line per step for a long time.
        # The parsed loss points must stay bounded in memory, or the 768 MB dashboard is
        # OOM-killed and systemd restarts it mid-run (the "nodeyard reloaded" that stopped training).
        n = 60000
        log = os.path.join(self.t.logs_dir, "long.log")
        with open(log, "w") as f:
            for i in range(1, n + 1):
                f.write("Step %d/%d | train loss 5.0 | 7.00 steps/sec | 30,000 tokens/sec | "
                        "total training step %d | learning rate 0.001 | gradient norm 0.5\n" % (i, n, i))
        self.t.job = {"model": "m", "log": log, "pid": None}
        # Drain the log the way the live UI polls it (each read is capped at 4 MB).
        st, guard = self.t.status(0), 0
        while st["next"] and guard < 50:
            nxt = self.t.status(st["next"])
            if nxt["next"] == st["next"] and not nxt["lines"]:
                break
            st, guard = nxt, guard + 1
        cap = trainapi.PARSED_POINTS_MAX
        self.assertLessEqual(len(self.t.parsed["train"]), 2 * cap)   # bounded, not growing with n
        self.assertLessEqual(len(self.t.parsed["grad"]), 2 * cap)
        self.assertLess(len(self.t.parsed["train"]), n)
        self.assertLessEqual(len(st["train"]), 1501)                 # chart still thinned for display
        self.assertEqual(self.t.parsed["last"]["step"], n)           # the newest point is never dropped
        self.assertEqual(st["train"][-1]["step"], n)

    def test_weights_need_a_saved_model(self):
        self.assertEqual(post(self.t, self.t.weights, {"model": ["nope"]})["code"], 404)
        self.assertIn("error", post(self.t, self.t.weights, {"model": ["../x"]}))

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

    def test_gpu_detected_from_the_durable_label_when_inventory_is_missing(self):
        # debian-1's agent inventory drops out often; the nodeyard.io/gpu label (set after nvidia-smi worked there)
        # must keep it a GPU node so it isn't shown as a plain CPU box.
        labelled = {"name": "debian-1", "cpu_cores": 8, "arch": "x86_64",
                    "labels": {"nodeyard.io/gpu": "nvidia", "nodeyard.io/gpu-vram-mib": "4096"}}
        self.assertEqual(trainapi.node_gpu(labelled)["vram"], 4096 * 1024 ** 2)
        device, speed, _ = trainapi.node_speed(labelled)
        self.assertEqual(device, "cuda")
        self.assertAlmostEqual(speed, 0.8e12)                 # the ~800 GFLOP/s GTX 1050 Ti estimate
        inv = {"hw": {"hardware": {"gpus": [{"vendor": "0x10DE", "name_full": "GTX 1050 Ti", "vram": 4e9}]}}}
        self.assertEqual(trainapi.node_gpu(inv)["name"], "GTX 1050 Ti")   # live inventory wins when present
        self.assertIsNone(trainapi.node_gpu({"arch": "x86_64", "cpu_cores": 8}))   # no GPU, no label
        self.assertEqual(trainapi.node_speed({"arch": "x86_64", "cpu_cores": 8})[0], "cpu")

    def test_a_gpu_node_keeps_its_gpu_when_the_worker_falls_back_to_cpu(self):
        node = {"name": "debian-1", "ready": True, "arch": "x86_64", "cpu_cores": 8,
                "mem_total": 24 * 1024 ** 3, "mem_used": 1024 ** 3,
                "labels": {"nodeyard.io/gpu": "nvidia", "nodeyard.io/gpu-vram-mib": "4096"}}
        self.t.pool.cluster_nodes = lambda: [node]
        self.t.speeds = {"debian-1": {"flops": 51.8e9, "device": "cpu"}}   # a stale CPU-fallback measurement
        e = self.t.pool.entry("debian-1")
        e.update(state="ready", status={"device": "cpu", "ram_free": 20 * 1024 ** 3,
                 "probe": {"torch": "2.4.0", "cuda": False, "cuda_error": "no kernel image",
                           "gpu": {"name": "NVIDIA GeForce GTX 1050 Ti", "vram": 4e9}}})
        m = next(x for x in self.t.machines() if x["name"] == "debian-1")
        self.assertEqual(m["device"], "cuda")                 # not relabelled a CPU box
        self.assertEqual(m["gpu"]["name"], "NVIDIA GeForce GTX 1050 Ti")
        self.assertAlmostEqual(m["speed"], 0.8e12)            # the GPU estimate, not the 51.8 GFLOP/s CPU measurement
        self.assertFalse(m.get("measured"))
        self.assertEqual(m["worker"]["cuda_error"], "no kernel image")   # the fall-back is still surfaced

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


class DatasetKitTests(unittest.TestCase):
    def test_starter_set_is_chat_and_every_sum_is_right(self):
        text = starter_dataset.generate(300_000)
        self.assertEqual(text, starter_dataset.generate(300_000))   # same every time
        _, rep = training_data.normalize(text)
        self.assertEqual((rep["format"], rep["chat"], rep["skipped"]), ("chat", True, 0))
        checked = 0
        for a, op, b, ans in re.findall(r"User: .*?(\d+) (plus|minus|times|\+|-|×) (\d+)\?\nAssistant: (.*)", text):
            want = {"plus": int(a) + int(b), "+": int(a) + int(b), "minus": int(a) - int(b), "-": int(a) - int(b),
                    "times": int(a) * int(b), "×": int(a) * int(b)}[op]
            self.assertEqual(int(re.findall(r"-?\d+", ans)[-1]), want, (a, op, b, ans))
            checked += 1
        self.assertGreater(checked, 100)

    def test_books_starting_with_a_bracket_are_text_and_dolly_context_is_kept(self):
        self.assertEqual(training_data.detect_format("[Illustration: a ship]\nCHAPTER I."), "text")
        self.assertEqual(training_data.detect_format('[{"text": "a"}]'), "json")
        self.assertEqual(training_data.record_to_text({"instruction": "Summarise.", "context": "The cat sat.", "response": "A cat sat."}),
                         "User: Summarise.\nThe cat sat.\nAssistant: A cat sat.")

    def test_gutenberg_header_and_footer_go(self):
        book = "The Project Gutenberg eBook\nlicence\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n\n\nIt was.\n\nThe end.\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\nmore licence"
        self.assertEqual(trainsets.strip_gutenberg(book), "It was.\n\nThe end.\n")

    def test_tinystories_markers_become_blank_lines_across_any_chunking(self):
        src = "Once a cat.\nIt slept.\n<|endoftext|>\nA dog ran.\n<|endoftext|>\n\nThe end.\n"
        for size in (1, 3, 7, 1000):
            out = io.StringIO()
            w = trainsets.TinyStoriesWriter(out)
            for i in range(0, len(src), size):
                w.feed(src[i:i + size])
            w.close()
            self.assertEqual(out.getvalue(), "Once a cat.\nIt slept.\n\nA dog ran.\n\nThe end.\n", size)

    def test_catalog_shows_licences_but_not_urls(self):
        with tempfile.TemporaryDirectory() as d:
            f = trainsets.Fetcher(d, lambda: 10 ** 12)
            for c in f.catalog():
                self.assertTrue(c["licence"] and c["source"] and c["what"])
                self.assertNotIn("url", c)
                self.assertNotIn("urls", c)
            with self.assertRaises(ValueError):
                f.start("../../etc")
            with self.assertRaises(OverflowError):
                trainsets.Fetcher(d, lambda: 10).start("tinystories")


class DataToolsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = trainapi.Trainer(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_starter_set_is_added_without_a_download(self):
        self.t.fetch({"id": "starter-chat"})
        for _ in range(100):
            if self.t.fetcher.state["starter-chat"]["state"] != "downloading":
                break
            time.sleep(0.1)
        self.assertEqual(self.t.fetcher.state["starter-chat"]["state"], "done")
        self.assertIn("starter-chat.txt", [d["name"] for d in self.t.datasets()])
        self.assertEqual(post(self.t, self.t.fetch, {"id": "nope"})["code"], 404)

    def test_convert_writes_exactly_what_the_model_reads(self):
        lines = [{"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]}, {"oops": 1}, {"text": "Plain."}]
        post(self.t, self.t.paste, {"name": "c.jsonl", "text": "\n".join(json.dumps(x) for x in lines)})
        check = self.t.check("c.jsonl")
        self.assertEqual(len(check["examples"]), 3)
        self.assertIsNone(check["examples"][1]["text"])
        r = self.t.convert({"name": "c.jsonl"})
        self.assertEqual((r["name"], r["records"], r["skipped"]), ("c.train.txt", 2, 1))
        with open(os.path.join(self.t.data_dir, "c.train.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "User: Hi\nAssistant: Hello\n\nPlain.\n")
        self.assertIn("already", post(self.t, self.t.convert, {"name": "c.train.txt"})["error"])

    def test_big_files_are_checked_from_a_sample(self):
        old = trainapi.CHECK_SAMPLE
        trainapi.CHECK_SAMPLE = 4096
        try:
            post(self.t, self.t.paste, {"name": "big.txt", "text": "User: hi\nAssistant: hello there\n\n" * 2000})
            c = self.t.check("big.txt")
        finally:
            trainapi.CHECK_SAMPLE = old
        self.assertTrue(c["sampled"])
        self.assertAlmostEqual(c["records"], 2000, delta=40)
        self.assertTrue(any("estimated" in a[1] for a in c["advice"]))


class GovernorTests(unittest.TestCase):
    def test_small_steady_gains_never_cut_the_rate(self):
        g, rate, said = lr_governor.LearningRateGovernor(1e-3), 1e-3, []
        loss = 4.0
        for step in range(200):
            loss *= 0.9995          # 0.05% better per check: the late-training case that used to flatline
            new = g.observe(loss, rate, step, said.append)
            rate = new or rate
        self.assertEqual(rate, 1e-3)

    def test_a_real_stall_cuts_slowly_and_never_below_a_tenth(self):
        g, rate, said = lr_governor.LearningRateGovernor(1e-3), 1e-3, []
        cuts = []
        for step in range(400):
            new = g.observe(3.0, rate, step, said.append)
            if new:
                cuts.append(step)
                rate = new
        self.assertEqual(cuts[0], 4)                 # four checks without progress, not two
        self.assertAlmostEqual(rate, 1e-4)           # floor: 10% of the start, not 2%


class QualityAndMultipartyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = trainapi.Trainer(self.dir.name)
        post(self.t, self.t.paste, {"name": "d.txt", "text": "User: hi\nAssistant: hello\n\n" * 100})

    def tearDown(self):
        self.dir.cleanup()

    def test_good_enough_needs_something_to_check(self):
        settings = {"dim": 64, "heads": 2, "steps": 10, "batch_size": 1, "context": 8, "layers": 1, "vocab_size": 300, "learning_rate": 0.001}
        bad = post(self.t, self.t.start, {"data": ["d.txt"], "settings": settings, "quality": {"on": True, "prompts": [{"prompt": "Hi", "expect": []}]}})
        self.assertIn("expected word", bad["error"])
        self.assertFalse(self.t.running())

    def test_status_reads_test_replies_and_the_verdict(self):
        log = os.path.join(self.t.logs_dir, "q.log")
        res = [{"prompt": "Hi", "reply": "hello", "expect": ["hello"], "ok": True}]
        with open(log, "w") as f:
            f.write("QUALITY " + json.dumps({"step": 500, "score": 0.5, "loss": 2.1, "results": res}) + "\n"
                    "QUALITY " + json.dumps({"step": 1000, "score": 1.0, "loss": 1.9, "results": res}) + "\n"
                    "Good enough at total training step 1,000: 1 of 1 test prompts pass.\n")
        self.t.job = {"model": "m", "log": log, "quality": "{}"}
        st = self.t.status(0)
        self.assertEqual([q["score"] for q in st["quality"]], [0.5, 1.0])
        self.assertEqual(st["quality_last"]["results"][0]["reply"], "hello")
        self.assertTrue(st["verdict"].startswith("Good enough"))
        self.assertTrue(st["job"]["until_good"])

    def test_saved_settings(self):
        r = self.t.save_preset({"name": "Shakespeare on the GPU", "settings": {"dim": 256, "heads": 8, "dropout": 0.2, "assistant_only": True, "junk": 1}})
        self.assertEqual(r["presets"][0]["settings"], {"dim": 256, "heads": 8, "dropout": 0.2, "assistant_only": True})
        self.t.save_preset({"name": "Shakespeare on the GPU", "settings": {"dim": 320}})   # same name: replaced
        self.assertEqual([p["settings"] for p in self.t.presets()], [{"dim": 320}])
        self.assertIn("Name it", post(self.t, self.t.save_preset, {"name": "../x", "settings": {}})["error"])
        self.assertIn("number", post(self.t, self.t.save_preset, {"name": "x", "settings": {"dim": "wide"}})["error"])
        self.assertEqual(self.t.delete_preset({"name": "Shakespeare on the GPU"})["presets"], [])

    def test_check_reports_repeats_and_example_length(self):
        pairs = [{"messages": [{"role": "user", "content": "line %d" % i}, {"role": "assistant", "content": "line %d" % (i + 1)}]} for i in range(200)]
        post(self.t, self.t.paste, {"name": "pairs.jsonl", "text": "\n".join(json.dumps(x) for x in pairs)})
        c = self.t.check("pairs.jsonl")
        self.assertLess(c["unique_fraction"], 0.55)
        self.assertTrue(any("repeats" in a[1] for a in c["advice"]))
        self.assertGreater(c["example_chars"], 20)

    def test_samples_are_real_exchanges_from_the_data(self):
        pairs = [{"messages": [{"role": "user", "content": "q%d" % i}, {"role": "assistant", "content": "a%d" % i}]} for i in range(400)]
        post(self.t, self.t.paste, {"name": "qa.jsonl", "text": "\n".join(json.dumps(x) for x in pairs)})
        r = self.t.samples(["qa.jsonl"], 20)
        self.assertEqual(len(r["samples"]), 20)
        self.assertTrue(r["chat"])
        for x in r["samples"]:
            self.assertEqual(x["answer"], "a" + x["prompt"][1:])
            self.assertLess(int(x["prompt"][1:]), 380)              # from the training part
        held = self.t.samples(["qa.jsonl"], 20, held_out=True)
        self.assertTrue(all(int(x["prompt"][1:]) >= 380 for x in held["samples"]))   # the last 5%, never trained on
        self.assertEqual(post(self.t, self.t.samples, ["nope.txt"])["code"], 404)

    def test_answers_count_as_something_to_check(self):
        settings = {"dim": 64, "heads": 2, "steps": 10, "batch_size": 1, "context": 8, "layers": 1, "vocab_size": 300, "learning_rate": 0.001}
        q = {"on": True, "prompts": [{"prompt": "Hi", "answer": "Hello."}], "match": "close"}
        r = post(self.t, self.t.start, {"data": ["d.txt"], "settings": settings, "quality": q})
        self.assertNotIn("error", r)
        spec = json.loads(self.t.job["quality"])
        self.assertEqual((spec["prompts"][0]["answer"], spec["match"]), ("Hello.", "close"))
        self.t.job["stop"] = True

    def test_group_chat_becomes_pairs_for_one_speaker(self):
        chat = ("12/03/2024, 21:15 - Sam: anyone up?\n12/03/2024, 21:16 - Alex: yep\n[12/03/2024, 21:17:03] Jo: me too\n"
                "what's up\n12/03/2024, 21:18 - Sam: game tonight?\n12/03/2024, 21:18 - Sam: 8pm\n")
        post(self.t, self.t.paste, {"name": "group.txt", "text": chat})
        self.assertEqual([x["name"] for x in self.t.speakers("group.txt")["speakers"]], ["Sam", "Alex", "Jo"])
        r = self.t.multiparty({"name": "group.txt", "speaker": "Sam", "context": 2})
        self.assertEqual((r["name"], r["examples"]), ("group-as-Sam.jsonl", 1))
        with open(os.path.join(self.t.data_dir, r["name"]), encoding="utf-8") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["messages"][0]["content"], "Alex: yep\nJo: me too what's up")
        self.assertEqual(rec["messages"][1]["content"], "game tonight? 8pm")
        self.assertIn("speakers", post(self.t, self.t.multiparty, {"name": "group.txt", "speaker": "Nobody"})["error"])


if __name__ == "__main__":
    unittest.main()
