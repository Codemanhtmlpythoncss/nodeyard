import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import setupapi  # noqa: E402


class SetupAPITests(unittest.TestCase):
    def test_target_host_must_be_a_private_or_tailscale_ipv4(self):
        self.assertEqual(setupapi.private_ipv4("192.168.1.50"), "192.168.1.50")
        self.assertEqual(setupapi.private_ipv4("100.77.4.12"), "100.77.4.12")
        for host in ("8.8.8.8", "127.0.0.1", "169.254.2.3", "host;reboot"):
            with self.subTest(host=host), self.assertRaises(setupapi.SetupError):
                setupapi.private_ipv4(host)

    def test_keyscan_fingerprints_are_parsed_and_ed25519_is_preferred(self):
        output = """256 SHA256:abcDEF0123+/= host (RSA)
256 SHA256:ed25519XYZ+/= host (ED25519)
"""
        self.assertEqual(setupapi.parse_key_fingerprints(output), [
            {"algorithm": "ED25519", "fingerprint": "SHA256:ed25519XYZ+/="},
            {"algorithm": "RSA", "fingerprint": "SHA256:abcDEF0123+/="},
        ])

    def test_probe_route_refuses_public_funnel_requests_before_scanning(self):
        ctx = SimpleNamespace(post_routes={})
        setupapi.register(ctx, SimpleNamespace(demo=False))

        class Handler:
            @staticmethod
            def _forwarded():
                return "public-funnel"

            def _json(self, payload, status=200):
                self.payload = payload
                self.status = status

        handler = Handler()
        with patch.object(setupapi, "probe_host") as probe:
            ctx.post_routes["/api/setup/node/probe"](handler, {"host": "192.168.1.10", "port": 22})
        self.assertEqual(handler.status, 403)
        self.assertFalse(probe.called)

    def test_probe_returns_only_fingerprints_and_uses_argument_vector(self):
        scan = SimpleNamespace(stdout="192.168.1.10 ssh-ed25519 AAAATEST\n", returncode=0)
        fingerprints = SimpleNamespace(stdout="256 SHA256:abcDEF0123+/= [192.168.1.10]:22 (ED25519)\n", returncode=0)
        with patch.object(setupapi.subprocess, "run", side_effect=[scan, fingerprints]) as run:
            result = setupapi.probe_host("192.168.1.10", "22")
        self.assertEqual(result["suggested"], "SHA256:abcDEF0123+/=")
        self.assertEqual(result["fingerprints"], [{"algorithm": "ED25519", "fingerprint": "SHA256:abcDEF0123+/="}])
        self.assertEqual(run.call_args_list[0].args[0], ["ssh-keyscan", "-T", "6", "-p", "22", "192.168.1.10"])
        self.assertEqual(run.call_args_list[1].args[0], ["ssh-keygen", "-lf", "-"])


if __name__ == "__main__":
    unittest.main()
