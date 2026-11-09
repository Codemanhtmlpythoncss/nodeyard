import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import auth  # noqa: E402

IP = "10.50.0.9"
MAC = "64:00:6a:fe:99:d4"


class RememberTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "remember.json")   # sub dir must be created on save

    def _store(self):
        return auth.Remember(self.path)

    def test_cookie_recognised_within_the_month(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        self.assertTrue(tok)
        self.assertTrue(r.recognised(tok, IP, MAC))
        # a wrong/empty cookie from a machine with no matching IP+MAC is not recognised
        self.assertFalse(r.recognised("", "203.0.113.4", ""))

    def test_expires_after_a_month(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        past = time.time() + auth.REMEMBER_SECONDS + 10
        self.assertFalse(r.recognised(tok, IP, MAC, now=past))
        self.assertEqual(r.count(now=past), 0)   # pruned

    def test_public_request_never_skips_the_password(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        self.assertFalse(r.recognised(tok, IP, MAC, public=True))

    def test_lan_ip_plus_mac_recognised_without_a_cookie(self):
        r = self._store()
        r.remember(IP, MAC)
        # a browser that lost its cookie, same machine (same IP + MAC), is still recognised
        self.assertTrue(r.recognised(None, IP, MAC))
        # same IP but a different machine (different MAC) is NOT recognised
        self.assertFalse(r.recognised(None, IP, "aa:bb:cc:dd:ee:ff"))
        # a MAC with no IP match is not recognised
        self.assertFalse(r.recognised(None, "10.50.0.250", MAC))

    def test_no_ip_mac_match_needs_the_cookie(self):
        r = self._store()
        r.remember(IP, MAC)
        # no cookie and no MAC (e.g. Tailscale/remote) -> must sign in
        self.assertFalse(r.recognised(None, "100.64.0.3", ""))

    def test_disabled_never_recognises_and_forgets(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        self.assertTrue(r.recognised(tok, IP, MAC))
        r.set_enabled(False)
        self.assertFalse(r.recognised(tok, IP, MAC))
        self.assertEqual(r.count(), 0)
        self.assertEqual(r.remember(IP, MAC), "")   # off: no token handed out

    def test_persists_across_restart(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        r2 = self._store()      # a fresh process reading the same file (an update restart)
        self.assertTrue(r2.recognised(tok, IP, MAC))

    def test_store_file_is_private_and_holds_no_cookie(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        self.assertEqual(oct(os.stat(self.path).st_mode)[-3:], "600")
        with open(self.path, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn(tok, body)   # only a hash is stored, never the cookie value

    def test_logs_a_new_address(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        mac2 = "aa:bb:cc:00:11:22"
        self.assertTrue(r.recognised(tok, "192.168.1.5", mac2))
        dev = r.view()["devices"][0]
        addrs = {(e["ip"], e["mac"]) for e in dev["history"]}
        self.assertIn((IP, MAC), addrs)
        self.assertIn(("192.168.1.5", mac2), addrs)

    def test_forget_and_forget_all(self):
        r = self._store()
        tok = r.remember(IP, MAC)
        did = r.view()["devices"][0]["id"]
        self.assertTrue(r.forget(did))
        self.assertFalse(r.recognised(tok, IP, MAC))
        r.remember(IP, MAC)
        r.remember("10.50.0.10", "aa:aa:aa:aa:aa:aa")
        self.assertEqual(r.forget_all(), 2)

    def test_mac_for_ignores_ipv6_and_loopback(self):
        r = self._store()
        self.assertEqual(r.mac_for("::1"), "")
        self.assertEqual(r.mac_for(""), "")


if __name__ == "__main__":
    unittest.main()
