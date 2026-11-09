import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import tune  # noqa: E402

PROBE = {"governors": ["powersave", "performance"], "governor": "powersave",
         "epps": [], "profiles": [], "gpus": [], "amdgpus": [], "pi": {}, "persist": False}


def tuner_with_probe(probe):
    t = tune.Tuning(SimpleNamespace(), SimpleNamespace(demo=True))
    t.probes["box"] = {"data": dict(probe)}
    return t


class PersistDefaultTests(unittest.TestCase):
    def test_a_change_is_kept_after_reboot_by_default(self):
        t = tuner_with_probe(PROBE)
        says, script, info = t._plan("box", {"governor": "performance"})
        self.assertTrue(info["persist"])                               # restart-resistant by default
        self.assertIn("/etc/nodeyard-tune.sh", script)                 # the boot script is written
        self.assertTrue(any("after a reboot" in s for s in says))

    def test_explicit_opt_out_applies_only_until_reboot(self):
        t = tuner_with_probe(PROBE)
        says, script, info = t._plan("box", {"governor": "performance", "persist": False})
        self.assertFalse(info["persist"])
        self.assertNotIn("nodeyard-tune.service", script)              # no boot service installed
        self.assertFalse(any("Keep these after a reboot" in s for s in says))

    def test_opt_out_drops_an_earlier_kept_value_for_the_same_setting(self):
        # If the governor was previously kept, opting out now must rewrite the boot
        # script without it, so a reboot doesn't bring the old value back.
        probe = dict(PROBE)
        probe["persist_script"] = tune.persist_script({"governor": ["echo performance > gov"]})
        t = tuner_with_probe(probe)
        says, script, info = t._plan("box", {"governor": "performance", "persist": False})
        self.assertFalse(info["persist"])
        self.assertIn("/etc/nodeyard-tune.sh", script)                 # rewritten (to drop the old entry)
        self.assertTrue(any("until the next reboot" in s for s in says))


if __name__ == "__main__":
    unittest.main()
