import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))

import clusterapi  # noqa: E402


def handler():
    h = SimpleNamespace(calls=[])
    h._json = lambda payload, code=200, *a, **k: h.calls.append((payload, code))
    return h


GPU = {"index": 0, "name": "RTX 3060", "use": 41.0, "mem_used": 2 << 30, "mem_total": 12 << 30, "temp_c": 55.0,
       "power_w": 90.5, "power_limit_w": 170.0, "clock_mhz": 1800.0, "clock_max_mhz": 2100.0, "fan": 30.0}
NODES = [
    {"name": "pi", "ready": True, "status": "Ready", "roles": ["control-plane"], "arch": "arm64", "cpu_cores": 4, "cpu_used": 0.5,
     "mem_total": 8 << 30, "mem_used": 2 << 30, "disk_total": 100, "disk_used": 40, "net_rx_rate": 10.0, "net_tx_rate": 5.0,
     "pods_running": 3, "pods_total": 4, "hw": {"cpu_use": 12.5, "temp_c": 48.2, "load": [0.4, 0.3, 0.2]}},
    {"name": "pc", "ready": False, "status": "NotReady", "roles": [], "arch": "amd64", "cpu_cores": 16, "cpu_used": 4.0,
     "mem_total": 32 << 30, "mem_used": 8 << 30, "hw": {"gpu_live": [GPU], "hardware": {"gpus": [{"vendor": "0x10de"}]}}},
]


class FakeStore:
    def __init__(self, state=True):
        self.samples = [{"t": float(i), "nodes": {"pi": [0.5, 1, 2, 3, 40, 0, 0, None, None, 9], "pc": [4.0, 2, 1, 1, 50, 0, 0, 40.0 + i, 5, 9]}}
                        for i in range(5)]
        self.state = {"nodes": NODES} if state else None

    def snapshot(self):
        return {"state": self.state, "totals": {"nodes": 2, "nodes_ready": 1, "cpu_total": 20, "gpus": 1, "secret": "x"},
                "alerts": [{"level": "warning"}, {"level": "critical"}, {"level": "warning"}], "error": None, "updated": 123.0}

    def history_slice(self, n):
        return self.samples[-n:]


class ClusterV1Tests(unittest.TestCase):
    def route(self, store, q=None):
        ctx = SimpleNamespace(get_routes={}, post_routes={}, store=store)
        clusterapi.register(ctx, SimpleNamespace())
        self.assertEqual(list(ctx.get_routes), ["/api/v1/cluster"])
        self.assertEqual(ctx.post_routes, {})   # read-only
        h = handler()
        ctx.get_routes["/api/v1/cluster"](h, q or {})
        return h.calls[-1]

    def test_nodes_gpus_and_totals(self):
        body, code = self.route(FakeStore())
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([n["name"] for n in body["nodes"]], ["pi", "pc"])
        pi, pc = body["nodes"]
        self.assertEqual((pi["cpu_use"], pi["temp_c"], pi["load"], pi["gpus"]), (12.5, 48.2, 0.4, []))
        self.assertFalse(pc["ready"])
        self.assertEqual(pc["gpus"][0]["name"], "RTX 3060")
        self.assertEqual(pc["gpus"][0]["use"], 41.0)
        self.assertEqual(pc["gpu_cards"], 1)
        self.assertNotIn("secret", body["totals"])
        self.assertEqual(body["alerts"], {"critical": 1, "warning": 2, "info": 0})

    def test_history_per_node_and_cluster(self):
        body, _ = self.route(FakeStore(), {"points": ["3"]})
        hist = body["history"]
        self.assertEqual(hist["t"], [2.0, 3.0, 4.0])
        self.assertEqual(hist["nodes"]["pc"]["gpu"], [42.0, 43.0, 44.0])
        self.assertEqual(hist["nodes"]["pi"]["gpu"], [None, None, None])
        self.assertEqual(hist["nodes"]["pi"]["disk"], [9, 9, 9])
        self.assertEqual(hist["cluster"]["cpu"], [4.5, 4.5, 4.5])
        self.assertEqual(hist["cluster"]["gpu"], [42.0, 43.0, 44.0])

    def test_bad_points_and_no_data(self):
        body, code = self.route(FakeStore(state=False), {"points": ["lots"]})
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["nodes"], [])
        self.assertEqual(len(body["history"]["t"]), 5)

    def test_no_store(self):
        body, code = self.route(None)
        self.assertEqual(code, 503)


if __name__ == "__main__":
    unittest.main()
