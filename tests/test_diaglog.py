import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import diaglog  # noqa: E402


def _state(nodes, pods=None, alerts=None, split=None):
    return {
        "state": {"nodes": nodes, "pods": pods or [], "ai": {"split": split or {}},
                  "cluster": {"name": "testcluster"}},
        "alerts": alerts or [], "totals": {}, "updated": 0,
    }


def _node(name, ready, temp=None, restarts=None):
    return {"name": name, "ready": ready, "ready_reason": "" if ready else "NodeStatusUnknown",
            "ready_message": "" if ready else "Kubelet stopped posting node status.",
            "cpu_used": 0.5, "mem_used": 1000, "hw": {"temp_c": temp, "load": [0.2, 0.1, 0.1]}}


class FakeStore:
    def __init__(self):
        self.snap = _state([])

    def snapshot(self):
        return self.snap


def _ctx():
    return SimpleNamespace(store=FakeStore(), selfupdate=None, mode="test", nd_version="9.9")


class DiaglogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "diag", "log.ndjson")
        self.ctx = _ctx()
        self.rec = diaglog.Recorder(self.ctx, self.path)

    def _set(self, snap):
        self.ctx.store.snap = snap

    def _events(self, kind=None):
        return [e for e in self.rec.entries if e.get("type") == "event" and (kind is None or e.get("kind") == kind)]

    def test_notready_and_recovery_are_logged(self):
        self._set(_state([_node("debian-1", True)]))
        self.rec.record(now=1000)               # baseline, first sample
        self._set(_state([_node("debian-1", False)]))
        self.rec.record(now=1015)               # ready -> NotReady
        self._set(_state([_node("debian-1", True)]))
        self.rec.record(now=1030)               # back to Ready
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("node-notready", kinds)
        self.assertIn("node-ready", kinds)
        nr = self._events("node-notready")[0]
        self.assertEqual(nr["node"], "debian-1")
        self.assertIn("Kubelet stopped posting", nr["detail"])

    def test_sample_cadence(self):
        self._set(_state([_node("n1", True, temp=40)]))
        self.rec.record(now=2000)               # sample
        self.rec.record(now=2030)               # within 60s -> no new sample
        self.rec.record(now=2065)               # >60s -> sample
        samples = [e for e in self.rec.entries if e.get("type") == "sample"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["nodes"]["n1"]["temp"], 40)
        self.assertTrue(samples[0]["nodes"]["n1"]["ready"])

    def test_alert_on_and_off(self):
        a = {"level": "critical", "kind": "node", "ref": "debian-1", "title": "debian-1 is not ready", "detail": "x"}
        self._set(_state([_node("debian-1", False)], alerts=[a]))
        self.rec.record(now=3000)
        self._set(_state([_node("debian-1", True)], alerts=[]))
        self.rec.record(now=3015)
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("alert-on", kinds)
        self.assertIn("alert-off", kinds)

    def test_pod_restart_jump(self):
        p0 = {"namespace": "ns", "name": "pod", "status": "Running", "restarts": 3}
        self._set(_state([_node("n1", True)], pods=[p0]))
        self.rec.record(now=4000)
        p1 = dict(p0, restarts=5)
        self._set(_state([_node("n1", True)], pods=[p1]))
        self.rec.record(now=4015)
        pr = self._events("pod-restart")
        self.assertEqual(len(pr), 1)
        self.assertEqual(pr[0]["ref"], "ns/pod")

    def test_prune_drops_old_entries(self):
        self._set(_state([_node("n1", True)]))
        self.rec.record(now=1000)               # old sample
        later = 1000 + diaglog.RETAIN_SECONDS + 100
        self._set(_state([_node("n1", True)]))
        self.rec.record(now=later)              # prunes the old one
        self.assertTrue(all(e["t"] >= later - diaglog.RETAIN_SECONDS for e in self.rec.entries))

    def test_bundle_summary_counts_flaps(self):
        self._set(_state([_node("debian-1", True)]))
        self.rec.record(now=5000)
        for i in range(3):                      # flap NotReady<->Ready three times
            self._set(_state([_node("debian-1", False)]))
            self.rec.record(now=5000 + i * 100 + 10)
            self._set(_state([_node("debian-1", True)]))
            self.rec.record(now=5000 + i * 100 + 20)
        b = self.rec.bundle(now=5400)
        self.assertEqual(b["summary"]["node_notready_counts"].get("debian-1"), 3)
        self.assertEqual(b["kind"], "nodeyard-2day-log")
        self.assertIn("events", b)
        self.assertIn("samples", b)
        self.assertEqual(b["cluster"], "testcluster")

    def test_persists_across_restart(self):
        # near-now timestamps so the real-time load cutoff (now - 2 days) keeps them
        import time
        base = time.time()
        self._set(_state([_node("n1", True)]))
        self.rec.record(now=base)
        self._set(_state([_node("n1", False)]))
        self.rec.record(now=base + 15)
        # a fresh recorder on the same file (an update restart) keeps the history
        rec2 = diaglog.Recorder(self.ctx, self.path)
        kinds = [e.get("kind") for e in rec2.entries if e.get("type") == "event"]
        self.assertIn("node-notready", kinds)

    def test_file_is_private(self):
        self.rec.note("x", "y")
        self.assertEqual(oct(os.stat(self.path).st_mode)[-3:], "600")


if __name__ == "__main__":
    unittest.main()
