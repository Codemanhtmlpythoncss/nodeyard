import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))

import server  # noqa: E402
import tsserve  # noqa: E402


class _H(server.Handler):
    def __init__(self, peer, headers, command="GET"):
        self.client_address = (peer, 1)
        self.headers = headers
        self.command = command
        self.server = SimpleNamespace(ctx=SimpleNamespace(port=None))
        self.sent = None

    def _json(self, obj, code=200, extra=None):
        self.sent = code

    def _drain_body(self):
        pass


class FunnelLeakTests(unittest.TestCase):
    def run_case(self, peer, headers):
        with mock.patch.object(tsserve, "proxy_source", return_value="100.64.0.5"):
            h = _H(peer, headers)
            return h._funnel_leak(), h.sent

    def test_tailnet_person_through_serve_is_let_in(self):
        self.assertEqual(self.run_case("100.64.0.5", {"Tailscale-User-Login": "me@x"}), (False, None))

    def test_internet_visitor_through_shared_port_is_refused(self):
        self.assertEqual(self.run_case("100.64.0.5", {}), (True, 404))
        self.assertEqual(self.run_case("100.64.0.5", {"Tailscale-Funnel-Request": "?1"}), (True, 404))

    def test_lan_and_public_routes_are_untouched(self):
        self.assertEqual(self.run_case("192.168.1.9", {}), (False, None))
        self.assertEqual(self.run_case("127.0.0.1", {"Tailscale-Funnel-Request": "?1"}), (False, None))


if __name__ == "__main__":
    unittest.main()
