import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import aiapi  # noqa: E402


class RemoveNodeCommandTests(unittest.TestCase):
    def test_valid_node_maps_to_remove_node_yes(self):
        label, argv = aiapi.build_command("remove-node", {"node": "debian-worker"}, [], "")
        self.assertEqual(argv, ["remove-node", "debian-worker", "--yes"])
        self.assertIn("debian-worker", label)

    def test_runs_outside_the_sandbox(self):
        self.assertIn("remove-node", aiapi.OUTSIDE_ACTIONS)

    def test_empty_node_rejected(self):
        with self.assertRaises(aiapi.AIError):
            aiapi.build_command("remove-node", {"node": ""}, [], "")

    def test_bad_node_name_rejected(self):
        for bad in ("bad name", "a;rm -rf", "../x", "-flag", "UPPER"):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command("remove-node", {"node": bad}, [], "")


if __name__ == "__main__":
    unittest.main()
