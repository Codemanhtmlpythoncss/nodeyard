"""Pause and Resume on the Train page: a one-machine run saves its exact state on that machine and stops; Resume
starts the same job there and carries on from that step. Runs without PyTorch: the worker runs a fake trainer that
follows the real one's pause protocol (SIGUSR1 -> paused.pt + paused.json, --resume-paused carries on)."""

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import trainapi  # noqa: E402
import trainpool  # noqa: E402
import trainworker  # noqa: E402
import lr_governor  # noqa: E402

FAKE_TRAINER = textwrap.dedent('''
    import argparse, json, os, signal, sys, time
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("--app-data")
    p.add_argument("--steps", type=int)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--resume-paused", action="store_true")
    args, _ = p.parse_known_args()
    d = args.app_data
    flag = {"pause": False, "stop": False}
    signal.signal(signal.SIGUSR1, lambda *a: flag.update(pause=True))
    signal.signal(signal.SIGINT, lambda *a: flag.update(stop=True))

    def drop():
        for f in ("paused.json", "paused.pt"):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def save(step):
        for f in ("model.pt", "optimizer.pt", "bpe.json"):
            with open(os.path.join(d, f), "w") as fh:
                fh.write("x")
        with open(os.path.join(d, "model.json"), "w") as fh:
            json.dump({"trained_steps": step, "validation_loss": 5.0}, fh)

    start = 0
    if args.resume_paused:
        start = json.load(open(os.path.join(d, "paused.json")))["step"]
        print("Resuming the paused run at step %d" % start, flush=True)
    else:
        drop()
    print("ARGS " + " ".join(sys.argv[2:]), flush=True)
    for step in range(start + 1, args.steps + 1):
        print("Step %d/%d | train loss 5.0 | 1.00 steps/sec | 100 tokens/sec | total training step %d | learning rate 0.001"
              % (step, args.steps, step), flush=True)
        time.sleep(0.04)
        if flag["pause"]:
            save(step)
            with open(os.path.join(d, "paused.pt"), "w") as fh:
                fh.write("state")
            json.dump({"step": step, "steps": args.steps, "total_step": step, "saved_at": time.time(),
                       "kept": {"loss": 5.0}, "last_validation": 5.1, "train_loss": 4.9}, open(os.path.join(d, "paused.json"), "w"))
            print("Paused at step %d" % step, flush=True)
            sys.exit(0)
        if flag["stop"]:
            break
    save(step)
    drop()
    print("Training finished.", flush=True)
''')

SETTINGS = {"steps": 120, "warmup": 0, "eval_every": 10, "save_every": 0, "dim": 32, "heads": 4, "layers": 1,
            "vocab_size": 300, "context": 16, "batch_size": 1, "learning_rate": 0.001, "weight_decay": 0.1, "dropout": 0.0, "grad_accum": 1}


def post(fn, body):
    try:
        return fn(body)
    except trainapi.TrainError as e:
        return {"error": str(e), "code": e.code}


class GovernorStateTests(unittest.TestCase):
    def test_a_restored_governor_judges_exactly_like_the_original(self):
        a, said = lr_governor.LearningRateGovernor(1e-3), []
        for x in (5.0, 4.6, 4.8, 4.2, 4.4, 4.3, 4.1, 4.15):
            a.note_train_loss(x)
        rate = 1e-3
        for i, v in enumerate((3.0, 3.1, 3.1, 3.1, 3.1, 3.2)):
            rate = a.observe(v, rate, i, said.append) or rate
        b = lr_governor.LearningRateGovernor(1e-3)
        b.load_state(json.loads(json.dumps(a.state())))   # through JSON, as paused.json carries it
        self.assertEqual(b.state(), a.state())
        for v in (3.2, 3.2, 3.3, 3.3, 2.9):
            self.assertEqual(a.observe(v, rate, 0, said.append), b.observe(v, rate, 0, said.append))


class WorkerPauseTests(unittest.TestCase):
    def test_pause_signals_only_a_running_trainer(self):
        with tempfile.TemporaryDirectory() as root:
            w = trainworker.Worker(root, root)
            self.assertEqual(w.pause(), {"pausing": False})


class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        code = os.path.join(self.dir.name, "code")
        os.makedirs(code)
        with open(os.path.join(code, trainworker.TRAINER), "w") as f:
            f.write(FAKE_TRAINER)
        self.worker = w = trainworker.Worker(os.path.join(self.dir.name, "worker"), code)
        w.deps["state"] = "ready"
        token = "t" * 32
        self.srv = trainworker.Server(("127.0.0.1", 0), trainworker.make_handler(w, token))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        remote = trainpool.Remote("box", "http://127.0.0.1:%d" % self.srv.server_address[1], token)
        t = self.t = trainapi.Trainer(os.path.join(self.dir.name, "dash"))
        box = {"name": "box", "speed": 5e11, "usable": True, "reason": "", "device": "cpu", "ram_free": 8e9,
               "gpu": None, "cores": 8, "arch": "x86_64"}
        t.machines = lambda both=False: [dict(box)]
        t.pool.ensure = lambda *a, **k: None
        t.measure_links = lambda machines: None
        t._remote = lambda name, say, wait=3600: remote
        t.paste({"name": "d.txt", "text": "User: hi\nAssistant: hello there\n" * 300})

    def tearDown(self):
        if self.worker.running():
            self.worker.proc.kill()
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def wait_for(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def log(self):
        with open(self.t.job["log"], encoding="utf-8") as f:
            return f.read()

    def pause_a_run(self):
        self.t.start({"data": ["d.txt"], "settings": SETTINGS, "model": "m"})
        self.wait_for(lambda: self.t.job.get("wjob") and "Step 2/" in self.log())
        self.assertEqual(self.t.pause({}), {"pausing": True})
        self.t.thread.join(20)
        self.assertFalse(self.t.running())
        return self.t.models()[0]["paused"]

    def test_pause_then_resume_carries_on_from_the_same_step(self):
        p = self.pause_a_run()
        self.assertIsNotNone(p)
        self.assertEqual(p["machine"], "box")
        self.assertEqual(p["steps"], 120)
        self.assertLess(p["step"], 120)
        self.assertTrue(self.t.job.get("paused"))
        self.assertIn("Paused on box", self.log())
        wdir = os.path.join(self.worker.models_dir, "m")
        self.assertTrue(os.path.isfile(os.path.join(wdir, "paused.pt")))
        self.assertFalse(os.path.exists(os.path.join(self.t.models_dir, "m", "paused.pt")))   # the big file stays on the machine

        r = self.t.start({"model": "m", "resume_paused": True})
        self.assertEqual((r["machines"], r["step"]), (["box"], p["step"]))
        self.t.thread.join(20)
        log = self.log()
        self.assertIn("Resuming the paused run at step %d" % p["step"], log)
        self.assertIn("--resume-paused", log)
        self.assertNotIn("--resume ", log.split("ARGS ", 1)[1].splitlines()[0] + " ")
        self.assertIn("Step %d/120" % (p["step"] + 1), log)
        self.assertIn("Training finished.", log)
        self.assertIsNone(self.t.models()[0]["paused"])               # finished: nothing left to resume
        self.assertFalse(os.path.exists(os.path.join(wdir, "paused.pt")))

    def test_continue_with_new_settings_drops_the_pause(self):
        self.pause_a_run()
        self.t.start({"data": ["d.txt"], "settings": SETTINGS, "model": "m", "resume": True})
        self.assertIsNone(self.t.models()[0]["paused"])
        self.t.thread.join(20)
        self.assertIn("--resume", self.log())
        self.assertNotIn("--resume-paused", self.log())

    def test_resume_needs_a_paused_run(self):
        self.assertEqual(post(self.t.start, {"model": "m", "resume_paused": True})["code"], 404)

    def test_only_one_machine_runs_can_pause(self):
        self.assertEqual(post(self.t.pause, {})["code"], 409)   # nothing running
        self.t.thread = threading.Thread(target=time.sleep, args=(1,))
        self.t.thread.start()
        self.t.job = {"mode": "shared", "stop": False, "wjob": {"id": "x"}, "machines": ["a", "b"]}
        err = post(self.t.pause, {})
        self.assertEqual(err["code"], 409)
        self.assertIn("one machine", err["error"])
        self.t.job = {"mode": "single", "stop": False, "wjob": None}
        self.assertIn("started training", post(self.t.pause, {})["error"])
        self.t.thread.join()


if __name__ == "__main__":
    unittest.main()
