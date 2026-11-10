import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))

import connections  # noqa: E402


def store_with(nodes):
    return SimpleNamespace(snapshot=lambda: {"state": {"nodes": nodes}})


def always_fail(address, port, timeout):
    return (False, None, "no route to host")


class ManualOverrideImmediateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ny-conn-ovr-")
        nodes = [{"name": "box", "machine_id": "id-box", "ready": True,
                  "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]}]
        self.c = connections.Connections(store_with(nodes), self.dir, probe=always_fail)
        self.c.tick()
        self.rid = next(iter(self.c.records))

    def test_typed_address_is_used_at_once_even_when_the_machine_is_off(self):
        # The machine is unplugged (probe fails), but a manual address is authoritative:
        # it becomes the active address straight away instead of waiting to be verified.
        self.c.update(self.rid, {"lan_override": "10.50.0.5"})
        lan = self.c.records[self.rid]["lan"]
        self.assertEqual(lan["address"], "10.50.0.5")       # used right away
        self.assertEqual(lan["candidate"], "10.50.0.5")
        self.assertEqual(lan["state"], "unreachable")       # reachability still reported honestly
        self.assertEqual(self.c.records[self.rid]["settings"]["lan_override"], "10.50.0.5")

    def test_a_discovered_address_still_waits_to_verify(self):
        # Without an override, an address that doesn't answer stays a candidate and does
        # NOT silently become the active address -- only typed addresses are immediate.
        self.c.tick(force_ids={self.rid})
        lan = self.c.records[self.rid]["lan"]
        self.assertEqual(lan["candidate"], "10.0.0.1")
        self.assertEqual(lan["address"], "")                # not promoted while unverified
        self.assertEqual(lan["state"], "unreachable")


if __name__ == "__main__":
    unittest.main()
