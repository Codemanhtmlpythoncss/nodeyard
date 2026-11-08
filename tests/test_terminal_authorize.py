import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import terminal  # noqa: E402

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY nodeyard@control"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOTHER second@control"


class PickPubkeyTests(unittest.TestCase):
    def test_prefers_ed25519_then_ecdsa_then_rsa(self):
        have = {".ssh/id_rsa.pub", ".ssh/id_ecdsa.pub", ".ssh/id_ed25519.pub"}
        exists = lambda p: os.path.relpath(p, "/home/x") in have
        self.assertTrue(terminal.pick_pubkey("/home/x", exists).endswith("id_ed25519.pub"))
        have.discard(".ssh/id_ed25519.pub")
        self.assertTrue(terminal.pick_pubkey("/home/x", exists).endswith("id_ecdsa.pub"))
        have.discard(".ssh/id_ecdsa.pub")
        self.assertTrue(terminal.pick_pubkey("/home/x", exists).endswith("id_rsa.pub"))

    def test_empty_when_none(self):
        self.assertEqual(terminal.pick_pubkey("/home/x", lambda p: False), "")


class AuthorizeScriptTests(unittest.TestCase):
    def _run(self, sshdir, key):
        return subprocess.run(["sh", "-c", terminal.AUTHORIZE_SCRIPT, "sh", key],
                              env={"NY_SSH_DIR": sshdir, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                              capture_output=True, text=True, timeout=20)

    def test_installs_once_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            sshdir = os.path.join(d, "root-ssh")
            r1 = self._run(sshdir, KEY)
            self.assertIn("NODEYARD_SSH_OK", r1.stdout)
            akeys = os.path.join(sshdir, "authorized_keys")
            self.assertEqual(Path(akeys).read_text().count(KEY), 1)
            # running again must not duplicate the key
            self._run(sshdir, KEY)
            self.assertEqual(Path(akeys).read_text().count(KEY), 1)
            # a different key is appended, both kept
            self._run(sshdir, KEY2)
            body = Path(akeys).read_text()
            self.assertEqual(body.count(KEY), 1)
            self.assertEqual(body.count(KEY2), 1)
            # the key file is private
            self.assertEqual(oct(os.stat(akeys).st_mode)[-3:], "600")


class ManifestTests(unittest.TestCase):
    def test_passes_key_as_argument_not_interpolated(self):
        m = json.loads(terminal.authorize_pod_manifest("nyauth-abc", "debian-1", KEY))
        self.assertEqual(m["spec"]["nodeName"], "debian-1")
        self.assertTrue(m["spec"]["hostPID"])
        c = m["spec"]["containers"][0]
        self.assertTrue(c["securityContext"]["privileged"])
        # the key must travel as its own argv element, never baked into the script body
        self.assertIn(KEY, c["command"])
        self.assertNotIn(KEY, terminal.AUTHORIZE_SCRIPT)
        self.assertEqual(c["command"][:4], ["nsenter", "-t", "1", "-m"])


if __name__ == "__main__":
    unittest.main()
