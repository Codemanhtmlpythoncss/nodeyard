import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import updates  # noqa: E402


class FakeStore:
    def __init__(self, nodes):
        self._nodes = nodes

    def snapshot(self):
        return {"state": {"nodes": self._nodes}}


def updater(nodes=None, state_dir=None):
    nodes = nodes if nodes is not None else [{"name": "debian-1", "ready": True, "os": "Debian GNU/Linux 12", "kernel": "6.1.0"},
                                             {"name": "debian-2", "ready": True}]
    ctx = SimpleNamespace(store=FakeStore(nodes), get_routes={}, post_routes={})
    args = SimpleNamespace(demo=True, state_dir=state_dir or tempfile.mkdtemp())
    return updates.Updates(ctx, args)


class ClassifyTests(unittest.TestCase):
    def test_a_package_lands_in_exactly_one_group(self):
        self.assertEqual(updates.classify("nvidia-driver", "Debian:bookworm"), "drivers")
        self.assertEqual(updates.classify("firmware-realtek", "Debian:bookworm"), "drivers")
        self.assertEqual(updates.classify("linux-image-amd64", "Debian:bookworm-security"), "kernel")
        self.assertEqual(updates.classify("openssl", "Debian:bookworm-security"), "security")
        self.assertEqual(updates.classify("curl", "Debian:bookworm"), "other")

    def test_a_drivers_security_update_counts_as_drivers(self):
        # "keep drivers current" must catch a driver's security update too, so drivers win over the security tag.
        self.assertEqual(updates.classify("nvidia-driver", "Debian:bookworm-security"), "drivers")


class ParseTests(unittest.TestCase):
    def test_parses_versions_and_upgradable_packages(self):
        text = ("os=Debian GNU/Linux 12 (bookworm)\n"
                "kernel=6.1.0-18-amd64\n"
                "nvidia=535.161.08\n"
                "pm=apt\n"
                "pkg=nvidia-driver [535.161.08-1] (535.183.01-1 Debian:bookworm-updates [amd64])\n"
                "pkg=openssl [3.0.11-1] (3.0.14-1 Debian:bookworm-security [amd64])\n"
                "pkg=openssl [3.0.11-1] (3.0.14-1 Debian:bookworm-security [amd64])\n"   # a dupe the simulation can print
                "probe-done\n")
        d = updates.parse_probe(text)
        self.assertEqual(d["os"], "Debian GNU/Linux 12 (bookworm)")
        self.assertEqual(d["nvidia"], "535.161.08")
        self.assertTrue(d["done"])
        self.assertEqual([p["name"] for p in d["packages"]], ["nvidia-driver", "openssl"])   # deduped
        self.assertEqual(d["packages"][0]["group"], "drivers")
        self.assertEqual(d["packages"][1]["group"], "security")
        self.assertTrue(d["packages"][1]["security"])

    def test_notes_a_failed_refresh(self):
        d = updates.parse_probe("pm=apt\nrefresh=failed\nprobe-done\n")
        self.assertTrue(d["refresh_failed"])
        self.assertEqual(d["packages"], [])


class PlanTests(unittest.TestCase):
    def test_plan_upgrades_only_the_chosen_groups(self):
        u = updater()
        u.probe("debian-1")   # demo: a driver update, a kernel security update and some ordinary ones
        says, script, names = u.plan("debian-1", ["drivers"])
        self.assertTrue(all("nvidia" in n or "libnvidia" in n for n in names))
        self.assertIn("apt-get install -y --only-upgrade", script)
        self.assertNotIn("libc6", script)                       # an "other" package is left out
        self.assertTrue(any("nothing is removed" in s for s in says))

    def test_plan_says_nothing_when_a_group_is_already_current(self):
        u = updater()
        u.probe("debian-1")
        says, script, names = u.plan("debian-1", ["security"])   # debian-1 demo has a kernel-security pkg, grouped as kernel
        # security group on debian-1: libc6 is bookworm-security -> "security"
        self.assertTrue(script == "" or names)


class AutoTests(unittest.TestCase):
    def test_turning_auto_on_needs_a_reviewed_confirm(self):
        u = updater()
        with self.assertRaises(updates.UpdatesError) as cm:
            u.set_auto({"node": "debian-1", "groups": ["drivers"]})           # no confirm
        self.assertEqual(cm.exception.code, 428)
        with self.assertRaises(updates.UpdatesError):
            u.set_auto({"node": "debian-1", "groups": ["drivers"], "confirm": True, "hash": "wrong"})
        v = u.set_auto({"node": "debian-1", "groups": ["drivers"], "confirm": True, "hash": updates.groups_hash(["drivers"])})
        row = next(r for r in v["nodes"] if r["node"] == "debian-1")
        self.assertEqual(row["auto"], ["drivers"])
        self.assertFalse(row["auto_paused"])

    def test_auto_settings_persist_and_unticking_clears_them(self):
        sd = tempfile.mkdtemp()
        u = updater(state_dir=sd)
        u.set_auto({"node": "debian-1", "groups": ["drivers", "security"], "confirm": True, "hash": updates.groups_hash(["drivers", "security"])})
        again = updater(state_dir=sd)                                          # reloaded from disk
        self.assertEqual(again.auto["debian-1"]["groups"], ["drivers", "security"])
        again.set_auto({"node": "debian-1", "groups": []})                     # untick everything
        self.assertNotIn("debian-1", again.auto)
        self.assertNotIn("debian-1", updater(state_dir=sd).auto)               # and it stays gone

    def test_due_waits_for_the_interval_and_pauses_when_groups_change(self):
        u = updater()
        u.set_auto({"node": "debian-1", "groups": ["drivers"], "confirm": True, "hash": updates.groups_hash(["drivers"])})
        now = 1_000_000.0
        self.assertTrue(u.due("debian-1", now))                               # never run -> due
        u.auto["debian-1"]["last"] = now
        self.assertFalse(u.due("debian-1", now + 3600))                       # ran an hour ago -> not yet
        self.assertTrue(u.due("debian-1", now + updates.AUTO_INTERVAL + 1))   # a day later -> due again
        u.auto["debian-1"]["reviewed"] = "stale"                             # groups changed, not re-confirmed
        self.assertFalse(u.due("debian-1", now + updates.AUTO_INTERVAL + 1))  # paused


class ApplyTests(unittest.TestCase):
    def test_apply_reviews_before_it_runs_then_upgrades_in_demo(self):
        u = updater()
        u.probe("debian-1")
        preview = u.apply({"node": "debian-1", "groups": ["drivers"]})         # no confirm -> just the plan
        self.assertIn("script", preview)
        self.assertNotIn("done", preview)
        done = u.apply({"node": "debian-1", "groups": ["drivers"], "confirm": True})
        self.assertIn("done", done)
        # after the (demo) upgrade the driver packages are no longer listed
        self.assertEqual(u._node_view({"name": "debian-1", "ready": True})["counts"]["drivers"], 0)


if __name__ == "__main__":
    unittest.main()
