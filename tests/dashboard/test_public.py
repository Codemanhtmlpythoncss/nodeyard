"""Public access (Tailscale Funnel): the gate's public port and the dashboard's sign-in limits."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "gate"))

import aiapi  # noqa: E402
import auth  # noqa: E402
import gate  # noqa: E402

NETS = gate.parse_networks(gate.DEFAULT_TRUSTED)


class GatePublicPort(unittest.TestCase):
    def test_loopback_is_trusted_on_the_normal_port_only(self):
        g = gate.Gate("http://x:8080", "s3cret-key-123456", NETS)
        self.assertEqual(g.decide("127.0.0.1", {}, "/v1/models")[0], "ok")
        self.assertEqual(g.decide("127.0.0.1", {}, "/v1/models", public=True)[0], "unauthorized")
        self.assertEqual(g.decide("127.0.0.1", {"Authorization": "Bearer s3cret-key-123456"}, "/v1/models", public=True)[0], "ok")

    def test_public_port_without_a_key_refuses_everything(self):
        g = gate.Gate("http://x:8080", "", NETS)
        self.assertEqual(g.decide("127.0.0.1", {}, "/v1/models", public=True)[0], "forbidden")

    def test_public_failures_count_per_visitor_and_all_together(self):
        g = gate.Gate("http://x:8080", "s3cret-key-123456", NETS)
        for _ in range(gate.LIMIT):
            g.decide("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, "/v1/x", public=True)
        self.assertEqual(g.decide("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, "/v1/x", public=True)[0], "wait")
        # another visitor is not punished for the first one
        self.assertEqual(g.decide("127.0.0.1", {"X-Forwarded-For": "198.51.100.4", "Authorization": "Bearer s3cret-key-123456"}, "/v1/x", public=True)[0], "ok")
        # your own network is never affected
        self.assertEqual(g.decide("192.168.1.5", {}, "/v1/x")[0], "ok")


class DashboardSignIn(unittest.TestCase):
    def test_internet_failures_cant_lock_out_your_network(self):
        a = auth.Auth("correct-horse-battery")
        for i in range(auth.GLOBAL_LIMIT + 2):
            a.login("internet:203.0.113.%d" % i, "wrong", public=True)
        self.assertGreater(a.retry_after("internet:198.51.100.1", public=True), 0)  # everyone from the internet waits
        self.assertEqual(a.retry_after("100.101.102.103"), 0)                     # Tailscale doesn't
        self.assertIsNotNone(a.login("100.101.102.103", "correct-horse-battery"))


class Allowlist(unittest.TestCase):
    def test_public_actions(self):
        self.assertEqual(aiapi.build_command("public-on", {"what": "api"}, set(), "/k")[1], ["public", "on", "--yes", "--api"])
        self.assertEqual(aiapi.build_command("public-off", {}, set(), "/k")[1], ["public", "off", "--yes"])
        with self.assertRaises(aiapi.AIError):
            aiapi.build_command("public-on", {"what": "everything; rm -rf /"}, set(), "/k")


if __name__ == "__main__":
    unittest.main()


class SlowLink(unittest.TestCase):
    def test_a_100_mbit_cluster_link_is_flagged(self):
        import analysis
        node = {"name": "archlinux-2", "ready": True, "status": "Ready", "conditions": {}, "cpu_used": 0, "cpu_cores": 4, "mem_used": 0, "mem_total": 8 * 2 ** 30,
                "disk_used": 1, "disk_total": 10, "hw": {"hardware": {"nics": [{"name": "eno1", "cluster": True, "wireless": False, "speed_mbps": 100},
                                                                               {"name": "wlo1", "cluster": False, "wireless": True, "speed_mbps": None}]}}}
        state = {"nodes": [node], "pods": [], "workloads": [], "volumes": [], "errors": []}
        al = analysis.alerts(state)
        self.assertTrue(any("100 Mbit/s" in a["title"] for a in al))


class SlowLinkHardware(unittest.TestCase):
    def test_a_100_mbit_card_at_100_is_fine(self):
        import analysis
        node = {"name": "Dead_channel-1", "ready": True, "status": "Ready", "conditions": {}, "cpu_used": 0, "cpu_cores": 4, "mem_used": 0, "mem_total": 2 ** 30,
                "disk_used": 1, "disk_total": 10, "hw": {"hardware": {"nics": [{"name": "eth0", "cluster": True, "wireless": False, "speed_mbps": 100, "max_mbps": 100}]}}}
        al = analysis.alerts({"nodes": [node], "pods": [], "workloads": [], "volumes": [], "errors": []})
        self.assertFalse(any("Mbit/s" in a["title"] for a in al))
