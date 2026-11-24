import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))

import aiapi  # noqa: E402


class PublicPortAction(unittest.TestCase):
    def test_builds_public_port_command(self):
        title, argv = aiapi.build_command("public-port", {"api": "443", "dashboard": "8443"}, [], "")
        self.assertIn("443", title)
        self.assertEqual(argv, ["public", "port", "--api", "443", "--dashboard", "8443", "--yes"])

    def test_rejects_bad_or_shared_ports(self):
        for p in ({"api": "80", "dashboard": "8443"}, {"api": "443", "dashboard": "443"}, {"api": "443; rm", "dashboard": "8443"}):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command("public-port", p, [], "")


if __name__ == "__main__":
    unittest.main()
