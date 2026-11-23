import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import server  # noqa: E402


class _Handler(server.Handler):
    """Just what _public_api touches, without a socket."""

    def __init__(self, path, auth=True):
        self.path = path
        self.server = SimpleNamespace(ctx=SimpleNamespace(auth=object() if auth else None))
        self.sent = None
        self.drained = False

    def _json(self, obj, code=200, extra=None):
        self.sent = (obj, code)

    def _drain_body(self):
        self.drained = True


class PublicControlApiTests(unittest.TestCase):
    def test_other_paths_are_untouched(self):
        for p in ("/api/v1/models", "/api/settings", "/", "/public-api/v10/models"):
            h = _Handler(p)
            self.assertTrue(h._public_api("GET"))
            self.assertEqual(h.path, p)
            self.assertIsNone(h.sent)

    def test_allowed_calls_map_onto_api_v1(self):
        cases = [("GET", "/public-api/v1/models", "/api/v1/models"),
                 ("GET", "/public-api/v1/targets", "/api/v1/targets"),
                 ("GET", "/public-api/v1/status", "/api/v1/status"),
                 ("GET", "/public-api/v1/jobs?id=7&since=3", "/api/v1/jobs?id=7&since=3"),
                 ("POST", "/public-api/v1/models/load", "/api/v1/models/load"),
                 ("POST", "/public-api/v1/models/unload", "/api/v1/models/unload"),
                 ("POST", "/public-api/v1/chat/completions", "/api/v1/chat/completions")]
        for method, path, want in cases:
            h = _Handler(path)
            self.assertTrue(h._public_api(method), path)
            self.assertEqual(h.path, want)
            self.assertFalse(h.drained)

    def test_proxy_that_keeps_its_mount_point(self):
        h = _Handler("/public-api/v1/api/v1/models")
        self.assertTrue(h._public_api("GET"))
        self.assertEqual(h.path, "/api/v1/models")

    def test_everything_else_is_refused(self):
        cases = [("GET", "/public-api/v1/chats"), ("GET", "/public-api/v1/cluster"),
                 ("GET", "/public-api/v1/yardcode/bundle"), ("GET", "/public-api/v1/search?q=x"),
                 ("POST", "/public-api/v1/models/download"), ("POST", "/public-api/v1/chats/delete"),
                 ("GET", "/public-api/v1/models/load"), ("POST", "/public-api/v1/models"),
                 ("GET", "/public-api/v1/../settings"), ("GET", "/public-api/v1")]
        for method, path in cases:
            h = _Handler(path)
            self.assertFalse(h._public_api(method), path)
            self.assertEqual(h.sent[1], 404, path)
            self.assertEqual(h.drained, method == "POST")

    def test_password_less_dashboard_refuses_the_internet(self):
        h = _Handler("/public-api/v1/models", auth=False)
        self.assertFalse(h._public_api("GET"))
        self.assertEqual(h.sent[1], 403)


if __name__ == "__main__":
    unittest.main()
