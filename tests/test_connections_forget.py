import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import connections  # noqa: E402


def store_with(nodes):
    return SimpleNamespace(snapshot=lambda: {"state": {"nodes": nodes}})


class ForgetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ny-conn-test-")
        # Two real nodes; "worker" also has a stale duplicate record (a second machine
        # identity under the same name) -- the phantom-row case from the dashboard.
        nodes = [
            {"name": "keep", "machine_id": "id-keep", "ready": True, "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]},
            {"name": "worker", "machine_id": "id-worker", "ready": False, "addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]},
        ]
        self.c = connections.Connections(store_with(nodes), self.dir, demo=True)
        self.c.tick()
        # inject a stale duplicate record for "worker" under a different identity (the
        # phantom-row case): a real one has the full path shape, so clone and re-id it.
        dup = json.loads(json.dumps(self.c.records["id-worker"]))
        dup["id"] = "id-worker-old"
        self.c.records["id-worker-old"] = dup
        self.c._save()

    def ids_named(self, name):
        return [k for k, r in self.c.records.items() if r.get("name") == name]

    def test_forget_removes_one_record_by_id_without_touching_others(self):
        self.assertEqual(len(self.ids_named("worker")), 2)
        name = self.c.forget("id-worker-old")
        self.assertEqual(name, "worker")
        self.assertEqual(len(self.ids_named("worker")), 1)  # only the duplicate is gone
        self.assertIn("id-keep", self.c.records)            # unrelated device untouched

    def test_forget_unknown_id_raises_404(self):
        with self.assertRaises(connections.ConnectionError_) as ctx:
            self.c.forget("nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_forget_persists_to_disk(self):
        self.c.forget("id-worker-old")
        with open(os.path.join(self.dir, "devices.json"), encoding="utf-8") as f:
            saved = json.load(f)["devices"]
        self.assertNotIn("id-worker-old", saved)
        self.assertIn("id-worker", saved)

    def test_forget_name_clears_every_record_for_a_removed_node(self):
        n = self.c.forget_name("worker")
        self.assertEqual(n, 2)                      # both the live and the stale record
        self.assertEqual(self.ids_named("worker"), [])
        self.assertIn("id-keep", self.c.records)

    def test_forget_name_of_unknown_node_is_a_noop(self):
        self.assertEqual(self.c.forget_name("ghost"), 0)

    def test_a_forgotten_live_node_record_is_rediscovered_on_tick(self):
        # Forgetting the record of a machine that is still a cluster node doesn't stick:
        # tick re-creates it. (Removing it for good means removing the node.)
        self.c.forget("id-worker")
        self.assertNotIn("id-worker", self.c.records)
        self.c.tick()
        self.assertIn("id-worker", self.c.records)


if __name__ == "__main__":
    unittest.main()
