import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "share" / "nodeyard" / "agent"
sys.path.insert(0, str(AGENT))

import agent  # noqa: E402


class AgentNetworkTests(unittest.TestCase):
    def _write(self, root, iface, name, value):
        path = Path(root) / "class" / "net" / iface / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def test_link_state_reports_duplex_only_when_carrier_is_up(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "enxusb0", "operstate", "up\n")
            self._write(root, "enxusb0", "carrier", "1\n")
            self._write(root, "enxusb0", "duplex", "full\n")
            self._write(root, "enxusb0", "speed", "2500\n")
            self.assertEqual(agent.iface_link_state(root, "enxusb0"), {
                "carrier": True, "duplex": "full", "speed_mbps": 2500,
            })

            self._write(root, "enxusb0", "carrier", "0\n")
            self.assertEqual(agent.iface_link_state(root, "enxusb0"), {
                "carrier": False, "duplex": "down", "speed_mbps": None,
            })

    def test_link_state_marks_missing_carrier_and_unrecognized_duplex_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(root, "eth0", "operstate", "up\n")
            self._write(root, "eth0", "carrier", "1\n")
            self._write(root, "eth0", "duplex", "mystery\n")
            self.assertEqual(agent.iface_link_state(root, "eth0"), {
                "carrier": True, "duplex": "unknown", "speed_mbps": None,
            })
            self.assertEqual(agent.iface_link_state(root, "tailscale0"), {
                "carrier": None, "duplex": "unknown", "speed_mbps": None,
            })


if __name__ == "__main__":
    unittest.main()
