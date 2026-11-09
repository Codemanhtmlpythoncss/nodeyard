import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import analysis  # noqa: E402


def _node(name, ready, ready_status, since_ago):
    return {
        "name": name, "ready": ready, "ready_status": ready_status,
        "ready_reason": "" if ready else ("NodeStatusUnknown" if ready_status == "Unknown" else "KubeletNotReady"),
        "ready_message": "" if ready else "Kubelet stopped posting node status.",
        "ready_since": time.time() - since_ago,
        "conditions": {}, "mem_used": None, "mem_total": None, "cpu_used": None, "cpu_cores": None,
        "disk_used": None, "disk_total": None, "hw": None,
    }


def _alerts(nodes):
    state = {"nodes": nodes, "pods": [], "workloads": [], "services": [],
             "volumes": [], "namespaces": [], "errors": []}
    return analysis.alerts(state)


def _find(alerts, name):
    return [a for a in alerts if a.get("ref") == name]


class ReadinessAlertTests(unittest.TestCase):
    def test_unknown_is_a_warning_not_a_critical(self):
        al = _find(_alerts([_node("debian-1", False, "Unknown", 500)]), "debian-1")
        self.assertTrue(al)
        self.assertEqual(al[0]["level"], "warning")
        self.assertIn("status is unknown", al[0]["title"])
        self.assertNotIn("is not ready", al[0]["title"])

    def test_false_ready_is_critical_not_ready(self):
        al = _find(_alerts([_node("worker", False, "False", 500)]), "worker")
        crit = [a for a in al if a["level"] == "critical" and "is not ready" in a["title"]]
        self.assertTrue(crit, "a node actively reporting NotReady must stay critical")

    def test_recent_change_is_info_regardless(self):
        al = _find(_alerts([_node("debian-1", False, "Unknown", 10)]), "debian-1")
        info = [a for a in al if a["level"] == "info" and "briefly stopped" in a["title"]]
        self.assertTrue(info, "the first 60s of any change is a soft info, not an alarm")

    def test_ready_node_has_no_readiness_alert(self):
        al = _find(_alerts([_node("ok", True, "True", 500)]), "ok")
        self.assertFalse([a for a in al if "not ready" in a["title"] or "status is unknown" in a["title"]])


if __name__ == "__main__":
    unittest.main()
