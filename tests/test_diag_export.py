import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import diaglog  # noqa: E402


class RedactTests(unittest.TestCase):
    def test_key_value_secrets_removed(self):
        for s, leak in [
            ("api_key=ABCDEF123456", "ABCDEF123456"),
            ("password: hunter2000", "hunter2000"),
            ("token = sk-live-abcdef123456", "sk-live-abcdef123456"),
            ("Authorization: Bearer sk-abc123secrettoken", "sk-abc123secrettoken"),
        ]:
            out = diaglog.redact(s)
            self.assertNotIn(leak, out, out)
            self.assertIn("REDACTED", out)

    def test_private_key_block_removed(self):
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAsecret\n-----END OPENSSH PRIVATE KEY-----"
        self.assertNotIn("AAAAsecret", diaglog.redact(pem))

    def test_ordinary_text_untouched(self):
        s = "debian-1 went NotReady: kubelet stopped posting"
        self.assertEqual(diaglog.redact(s), s)

    def test_none_and_nonstr_safe(self):
        self.assertIsNone(diaglog.redact(None))
        self.assertEqual(diaglog.redact(123), 123)


class _Store:
    def __init__(self):
        self.snap = {"state": {"nodes": [], "pods": [], "cluster": {"name": "c"}}, "alerts": [], "totals": {}, "updated": 0}

    def snapshot(self):
        return self.snap


def _rec():
    d = tempfile.mkdtemp()
    ctx = SimpleNamespace(store=_Store(), selfupdate=None, mode="test", nd_version="9")
    return diaglog.Recorder(ctx, os.path.join(d, "log.ndjson"))


class ExportTests(unittest.TestCase):
    def test_events_view_redacts_and_lists_devices(self):
        r = _rec()
        r.note("node-notready", "debian-1 went NotReady", detail="token=SECRETVALUE123", level="critical", node="debian-1")
        v = r.events_view()
        self.assertTrue(v["ok"])
        self.assertIn("debian-1", v["devices"])
        self.assertNotIn("SECRETVALUE123", v["events"][0].get("detail", ""))

    def test_text_report_has_sections(self):
        r = _rec()
        r.note("node-ready", "debian-1 became Ready", node="debian-1")
        txt = diaglog.text_report(r.bundle())
        self.assertIn("NODEYARD DIAGNOSTIC REPORT", txt)
        self.assertIn("SUMMARY", txt)
        self.assertIn("EVENTS", txt)
        self.assertIn("debian-1 became Ready", txt)


if __name__ == "__main__":
    unittest.main()
