import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import controlapi  # noqa: E402


class _Handler:
    """Just the bits KeyGate.__call__ touches."""
    def __init__(self, authed, session_valid=False, authorization=""):
        self.ctx = SimpleNamespace(auth=SimpleNamespace(valid=lambda tok: session_valid))
        self.headers = {"Authorization": authorization} if authorization else {}
        self.client_address = ("10.50.0.9",)
        self._authed_val = authed
        self.sent = None

    def _authed(self):
        return self._authed_val

    def _token(self):
        return "sess-token"

    def _forwarded(self):
        return None

    def _json(self, obj, code=200, extra=None):
        self.sent = (obj, code)


class V1GateRememberTests(unittest.TestCase):
    def setUp(self):
        self.gate = controlapi.KeyGate(SimpleNamespace(api_key=lambda: "SERVERKEY1234567890"))

    def test_remembered_browser_passes_without_session_or_key(self):
        # session cookie no longer valid (expired), but the browser is remembered -> _authed() is True.
        # Before the fix the gate only checked ctx.auth.valid(token) and 401'd here (the AI-button bounce).
        h = _Handler(authed=True, session_valid=False)
        self.assertTrue(self.gate(h))
        self.assertIsNone(h.sent)   # no 401 was sent

    def test_not_authed_and_no_key_is_rejected(self):
        h = _Handler(authed=False, session_valid=False)
        self.assertFalse(self.gate(h))
        self.assertEqual(h.sent[1], 401)

    def test_valid_session_still_passes(self):
        h = _Handler(authed=True, session_valid=True)
        self.assertTrue(self.gate(h))

    def test_correct_bearer_key_still_passes(self):
        h = _Handler(authed=False, authorization="Bearer SERVERKEY1234567890")
        self.assertTrue(self.gate(h))
        self.assertTrue(getattr(h, "v1_bearer", False))


if __name__ == "__main__":
    unittest.main()
