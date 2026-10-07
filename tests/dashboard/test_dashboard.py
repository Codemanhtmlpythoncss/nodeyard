"""Unit tests for the dashboard's Python: quantity parsing, pod status, alerts, demo data."""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import analysis  # noqa: E402
import auth  # noqa: E402
import demo  # noqa: E402
import kube  # noqa: E402


class Quantities(unittest.TestCase):
    def test_binary_and_decimal_suffixes(self):
        self.assertEqual(kube.parse_quantity("16244588Ki"), 16244588 * 1024)
        self.assertEqual(kube.parse_quantity("2Gi"), 2 * 1024 ** 3)
        self.assertAlmostEqual(kube.parse_quantity("250m"), 0.25)
        self.assertAlmostEqual(kube.parse_quantity("123456789n"), 0.123456789)
        self.assertEqual(kube.parse_quantity("4"), 4.0)
        self.assertEqual(kube.parse_quantity("1.5"), 1.5)

    def test_garbage_is_zero(self):
        for bad in (None, "", "abc", "12x3"):
            self.assertEqual(kube.parse_quantity(bad), 0.0)

    def test_timestamps(self):
        self.assertEqual(kube.parse_time("2024-01-02T03:04:05Z"), 1704164645.0)
        self.assertEqual(kube.parse_time("2024-01-02T03:04:05.123456Z"), 1704164645.0)
        self.assertEqual(kube.parse_time(None), 0.0)
        self.assertEqual(kube.parse_time("nonsense"), 0.0)


def pod(phase="Running", waiting=None, terminated=None, deleted=False, init=None):
    st = {"phase": phase, "containerStatuses": [{"name": "c", "ready": phase == "Running", "restartCount": 0, "state": {}}]}
    if waiting:
        st["containerStatuses"][0]["state"] = {"waiting": {"reason": waiting}}
    if terminated:
        st["containerStatuses"][0]["state"] = {"terminated": {"reason": terminated}}
    if init:
        st["initContainerStatuses"] = [init]
    meta = {"name": "p", "namespace": "ns"}
    if deleted:
        meta["deletionTimestamp"] = "2024-01-01T00:00:00Z"
    return {"metadata": meta, "spec": {"containers": [{"name": "c", "image": "i"}]}, "status": st}


class PodStatus(unittest.TestCase):
    def test_plain_states(self):
        self.assertEqual(kube.pod_status(pod("Running")), "Running")
        self.assertEqual(kube.pod_status(pod("Pending")), "Pending")
        self.assertEqual(kube.pod_status(pod("Running", waiting="CrashLoopBackOff")), "CrashLoopBackOff")
        self.assertEqual(kube.pod_status(pod("Failed", terminated="OOMKilled")), "OOMKilled")
        self.assertEqual(kube.pod_status(pod("Succeeded", terminated="Completed")), "Succeeded")

    def test_terminating_and_init(self):
        self.assertEqual(kube.pod_status(pod("Running", deleted=True)), "Terminating")
        init = {"name": "i", "state": {"waiting": {"reason": "ImagePullBackOff"}}}
        self.assertEqual(kube.pod_status(pod("Pending", init=init)), "Init:ImagePullBackOff")

    def test_build_pod_ready_and_usage(self):
        p = kube.build_pod(pod("Running"), {("ns", "p"): (0.5, 1024.0)})
        self.assertEqual(p["ready"], "1/1")
        self.assertEqual((p["cpu"], p["mem"]), (0.5, 1024.0))
        self.assertIsNone(kube.build_pod(pod("Running"), {})["cpu"])


class KubeconfigParsing(unittest.TestCase):
    def test_reads_inline_credentials(self):
        import tempfile
        text = ("apiVersion: v1\nclusters:\n- cluster:\n    certificate-authority-data: QUJD\n    server: https://127.0.0.1:6443\n  name: default\n"
                "users:\n- name: default\n  user:\n    client-certificate-data: REVG\n    client-key-data: R0hJ\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(text)
        try:
            cfg = kube.load_kubeconfig(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["server"], "https://127.0.0.1:6443")
        self.assertEqual((cfg["ca"], cfg["cert"], cfg["key"]), ("QUJD", "REVG", "R0hJ"))

    def test_missing_file_and_missing_credentials(self):
        with self.assertRaises(kube.KubeError):
            kube.load_kubeconfig("/nonexistent/kubeconfig")
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("server: https://x:6443\n")
        try:
            with self.assertRaises(kube.KubeError):
                kube.load_kubeconfig(f.name)
        finally:
            os.unlink(f.name)


class SignIn(unittest.TestCase):
    KEY = "ABCD-EF01-2345-6789-ABCD-EF01"

    def test_generated_password_matching_ignores_case_dashes_and_spaces(self):
        a = auth.Auth(self.KEY)
        for typed in (self.KEY, self.KEY.lower(), self.KEY.replace("-", ""), " abcd ef01 2345 6789 abcd ef01 "):
            self.assertIsNotNone(a.login("1.2.3.4", typed), typed)
        self.assertIsNone(a.login("1.2.3.4", "ABCD-EF01-2345-6789-ABCD-EF02"))
        self.assertIsNone(a.login("1.2.3.4", ""))

    def test_a_stored_password_is_always_usable_but_changing_it_needs_a_minimum_length(self):
        # the service must start whatever is stored (dashboard.weak-password may allow short ones)...
        self.assertIsNotNone(auth.Auth("short").login("1.2.3.4", "short"))
        with self.assertRaises(ValueError):
            auth.Auth("   ")
        # ...but setting a new one from the page enforces the minimum unless it is relaxed
        a = auth.Auth("sixsix")
        with self.assertRaises(ValueError):
            a.set_password("abc")
        a.set_password("abc", min_length=1)
        self.assertIsNotNone(a.login("1.2.3.4", "abc"))

    def test_a_chosen_password_is_matched_exactly(self):
        a = auth.Auth("huskiboi\n")
        self.assertIsNotNone(a.login("1.2.3.4", "huskiboi"))
        self.assertIsNotNone(a.login("1.2.3.4", "  huskiboi "))   # stray spaces from a phone keyboard
        self.assertIsNone(a.login("1.2.3.4", "Huskiboi"))
        self.assertIsNone(a.login("1.2.3.4", "huski-boi"))
        self.assertIsNone(a.login("1.2.3.4", "HUSKIBOI"))

    def test_sessions_expire_and_log_out(self):
        a = auth.Auth(self.KEY)
        tok = a.login("1.2.3.4", self.KEY, now=1000.0)
        self.assertTrue(a.valid(tok, now=1000.0 + 60))
        self.assertFalse(a.valid(tok, now=1000.0 + auth.SESSION_SECONDS + 1))
        tok2 = a.login("1.2.3.4", self.KEY, now=5000.0)
        a.logout(tok2)
        self.assertFalse(a.valid(tok2, now=5001.0))
        self.assertFalse(a.valid(None))
        self.assertFalse(a.valid("made-up"))

    def test_lockout_per_address_and_it_wears_off(self):
        a = auth.Auth(self.KEY)
        for i in range(auth.PER_IP_LIMIT):
            self.assertEqual(a.retry_after("9.9.9.9", now=100.0 + i), 0)
            self.assertIsNone(a.login("9.9.9.9", "wrong", now=100.0 + i))
        self.assertGreater(a.retry_after("9.9.9.9", now=110.0), 0)
        self.assertEqual(a.retry_after("8.8.8.8", now=110.0), 0)  # someone else isn't locked out
        self.assertEqual(a.retry_after("9.9.9.9", now=100.0 + auth.WINDOW + 10), 0)

    def test_a_good_login_clears_the_failures(self):
        a = auth.Auth(self.KEY)
        for i in range(auth.PER_IP_LIMIT - 1):
            a.login("7.7.7.7", "wrong", now=10.0 + i)
        self.assertIsNotNone(a.login("7.7.7.7", self.KEY, now=20.0))
        self.assertEqual(a.retry_after("7.7.7.7", now=21.0), 0)

    def test_many_internet_addresses_guessing_lock_the_internet_but_not_your_own_network(self):
        a = auth.Auth(self.KEY)
        for i in range(auth.GLOBAL_LIMIT):
            a.login("internet:10.0.0.%d" % (i % 250), "wrong", now=50.0 + i * 0.01, public=True)
        self.assertGreater(a.retry_after("internet:10.1.1.1", now=60.0, public=True), 0)
        self.assertEqual(a.retry_after("192.168.1.9", now=60.0), 0)  # strangers can't lock you out at home


class ClusterReading(unittest.TestCase):
    def source(self):
        src = object.__new__(kube.KubeSource)
        src.summary_cache, src.net_prev = {}, {}
        return src

    @staticmethod
    def k8s_node():
        return {"metadata": {"name": "n1", "labels": {}, "creationTimestamp": "2024-01-01T00:00:00Z"}, "spec": {"podCIDR": "10.42.0.0/24"},
                "status": {"nodeInfo": {}, "conditions": [{"type": "Ready", "status": "True"}], "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}],
                           "capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"}, "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"}}}

    def test_pod_range_is_the_cluster_range(self):
        pc = kube.KubeSource._pod_cidr
        self.assertEqual(pc([{"pod_cidr": "10.42.0.0/24"}, {"pod_cidr": "10.42.3.0/24"}]), "10.42.0.0/16")
        self.assertEqual(pc([{"pod_cidr": "10.42.0.0/24"}, {"pod_cidr": "10.99.1.0/24"}]), "10.42.0.0/24")
        self.assertEqual(pc([{}]), "")

    def test_network_rate_needs_two_distinct_samples(self):
        src, n = self.source(), self.k8s_node()
        net = lambda rx, tx: {"node": {"network": {"rxBytes": rx, "txBytes": tx}}}  # noqa: E731
        src.summary_cache["n1"] = (100.0, net(1000, 500))
        first = src._build_node(n, {}, {"n1": src.summary_cache["n1"][1]}, [])
        self.assertIsNone(first["net_rx_rate"])
        src.summary_cache["n1"] = (110.0, net(2000, 1500))
        second = src._build_node(n, {}, {"n1": src.summary_cache["n1"][1]}, [])
        self.assertEqual((second["net_rx_rate"], second["net_tx_rate"]), (100.0, 100.0))
        # polled again before the kubelet was asked again: same figures, not zero
        third = src._build_node(n, {}, {"n1": src.summary_cache["n1"][1]}, [])
        self.assertEqual((third["net_rx_rate"], third["net_tx_rate"]), (100.0, 100.0))

    def test_node_usage_falls_back_to_the_kubelet(self):
        src, n = self.source(), self.k8s_node()
        summ = {"node": {"cpu": {"usageNanoCores": 500000000}, "memory": {"workingSetBytes": 1024}, "fs": {"capacityBytes": 100, "usedBytes": 40}}}
        node = src._build_node(n, {}, {"n1": summ}, [])
        self.assertAlmostEqual(node["cpu_used"], 0.5)
        self.assertEqual((node["mem_used"], node["disk_total"], node["disk_used"]), (1024, 100, 40))
        self.assertEqual(node["roles"], ["worker"])
        self.assertEqual(node["internal_ip"], "10.0.0.1")


class DemoAndAnalysis(unittest.TestCase):
    def setUp(self):
        self.state = demo.DemoSource().collect()

    def test_shape(self):
        for key in ("cluster", "nodes", "pods", "workloads", "services", "ingresses", "volumes", "events", "namespaces", "ai", "errors"):
            self.assertIn(key, self.state)
        self.assertEqual(len(self.state["nodes"]), 4)

    def test_totals_add_up(self):
        t = analysis.totals(self.state)
        self.assertEqual(t["nodes"], 4)
        self.assertEqual(t["cpu_total"], sum(n["cpu_cores"] for n in self.state["nodes"]))
        self.assertEqual(t["pods"], len(self.state["pods"]))
        self.assertEqual(sum(t["pod_states"].values()), t["pods"])

    def test_node_usage_never_exceeds_capacity(self):
        for n in self.state["nodes"]:
            self.assertLessEqual(n["cpu_used"], n["cpu_cores"])
            self.assertLessEqual(n["mem_used"], n["mem_total"])

    def test_alerts_flag_the_planted_problems(self):
        titles = " | ".join(a["title"] for a in analysis.alerts(self.state))
        self.assertIn("CrashLoopBackOff", titles)
        self.assertIn("Pending", titles)
        levels = [a["level"] for a in analysis.alerts(self.state)]
        self.assertEqual(levels, sorted(levels, key=["critical", "warning", "info"].index))

    def test_healthy_cluster_has_no_alerts(self):
        s = self.state
        s["pods"] = [p for p in s["pods"] if p["status"] == "Running"]
        s["workloads"] = [w for w in s["workloads"] if w["ready"] >= w["desired"]]
        for n in s["nodes"]:
            n["disk_used"] = n["disk_total"] * 0.3
        self.assertEqual(analysis.alerts(s), [])

    def test_not_ready_node_is_critical(self):
        self.state["nodes"][0]["ready"] = False
        a = analysis.alerts(self.state)
        self.assertEqual(a[0]["level"], "critical")
        self.assertEqual(a[0]["kind"], "node")

    def test_history_seed_and_sample(self):
        h = demo.DemoSource().seed_history(30, 5)
        self.assertEqual(len(h), 30)
        self.assertLess(h[0]["t"], h[-1]["t"])
        self.assertEqual(sorted(h[0]["nodes"]), ["yard-1", "yard-2", "yard-3", "yard-4"])
        self.assertEqual(len(h[0]["nodes"]["yard-1"]), 9)  # cpu, memory, rx, tx, temperature, clock, load, gpu use, gpu memory

    def test_demo_is_deterministic_for_a_given_time(self):
        t = time.time()
        a = demo.DemoSource().collect(t)["nodes"][2]["cpu_used"]
        b = demo.DemoSource().collect(t)["nodes"][2]["cpu_used"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
