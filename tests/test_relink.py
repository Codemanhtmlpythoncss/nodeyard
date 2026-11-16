"""nodeyard relink: bringing back a node that dropped off after its cable was
unplugged and plugged in again (lib/modules/relink.sh, share/nodeyard/net/arpwho.py)."""
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import aiapi  # noqa: E402

_spec = importlib.util.spec_from_file_location("arpwho", ROOT / "share" / "nodeyard" / "net" / "arpwho.py")
arpwho = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arpwho)


def _bash4():
    """A bash new enough for nodeyard (macOS ships 3.2), or None."""
    for cand in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if not cand or not os.path.exists(cand):
            continue
        try:
            out = subprocess.run([cand, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out.isdigit() and int(out) >= 4:
            return cand
    return None


BASH = _bash4()


class RelinkActionTests(unittest.TestCase):
    def test_every_missing_node(self):
        label, argv = aiapi.build_command("relink", {}, [], "")
        self.assertEqual(argv, ["relink", "--yes"])
        self.assertIn("Reconnect", label)

    def test_one_node(self):
        label, argv = aiapi.build_command("relink", {"node": "debian-worker"}, [], "")
        self.assertEqual(argv, ["relink", "--yes", "debian-worker"])
        self.assertIn("debian-worker", label)

    def test_runs_outside_the_sandbox(self):
        self.assertIn("relink", aiapi.OUTSIDE_ACTIONS)

    def test_bad_node_name_rejected(self):
        for bad in ("bad name", "a;rm -rf", "../x", "-flag", "UPPER", "--auto"):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command("relink", {"node": bad}, [], "")


def _reply(sender_mac, sender_ip, op=2, ethertype=0x0806):
    eth = b"\x02" * 6 + sender_mac + struct.pack("!H", ethertype)
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, sender_mac, socket.inet_aton(sender_ip), b"\x02" * 6, socket.inet_aton("10.50.0.1"))
    return eth + arp


class ArpWhoTests(unittest.TestCase):
    MAC = bytes.fromhex("c8d9d2a572ea")

    def test_request_is_a_broadcast_arp_request(self):
        src = bytes.fromhex("d83addaabbcc")
        f = arpwho.request(src, socket.inet_aton("10.50.0.1"), socket.inet_aton("10.50.0.5"))
        self.assertEqual(len(f), 42)
        self.assertEqual(f[:6], b"\xff" * 6)
        self.assertEqual(f[6:12], src)
        self.assertEqual(f[12:14], b"\x08\x06")
        _h, ptype, hlen, plen, op, sha, spa, tha, tpa = struct.unpack("!HHBBH6s4s6s4s", f[14:42])
        self.assertEqual((ptype, hlen, plen, op), (0x0800, 6, 4, 1))
        self.assertEqual((sha, spa, tha, tpa), (src, socket.inet_aton("10.50.0.1"), b"\x00" * 6, socket.inet_aton("10.50.0.5")))

    def test_reply_from_the_address_gives_its_mac(self):
        self.assertEqual(arpwho.reply_mac(_reply(self.MAC, "10.50.0.5"), socket.inet_aton("10.50.0.5")), "c8:d9:d2:a5:72:ea")

    def test_other_frames_are_ignored(self):
        want = socket.inet_aton("10.50.0.5")
        self.assertIsNone(arpwho.reply_mac(_reply(self.MAC, "10.50.0.4"), want))       # someone else
        self.assertIsNone(arpwho.reply_mac(_reply(self.MAC, "10.50.0.5", op=1), want))  # a request, not a reply
        self.assertIsNone(arpwho.reply_mac(_reply(self.MAC, "10.50.0.5", ethertype=0x0800), want))
        self.assertIsNone(arpwho.reply_mac(_reply(self.MAC, "10.50.0.5")[:30], want))

    def test_cant_ask_without_arguments(self):
        self.assertEqual(arpwho.main(["arpwho.py"]), 1)


NODES = ('k3s kubectl get nodes -o jsonpath=*backend-data*\t0\t'
         'yard-1|True|2026-10-06T08:00:00Z|192.168.1.10|{"VtepMAC":"5a:1e:7c:30:9b:01"}|192.168.1.10|10.42.0.0/24\\n'
         'yard-4|False|2026-11-16T08:00:00Z|192.168.1.13|{"VtepMAC":"5a:1e:7c:30:9b:04"}|192.168.1.13|10.42.3.0/24\n'
         'k3s kubectl get nodes --no-headers\t0\t'
         'yard-1   Ready    control-plane,etcd,master   41d   v1.33.4+k3s1\\nyard-4   NotReady    <none>   12d   v1.33.4+k3s1\n'
         'ip neigh show to 192.168.1.13 dev eth0\t0\t192.168.1.13 lladdr c8:d9:d2:a5:72:ea PERMANENT\n')


@unittest.skipUnless(BASH, "needs bash 4 or newer")
class RelinkDemoTests(unittest.TestCase):
    """The command itself, in demo mode (nothing on this machine is touched)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def nodeyard(self, *args, rules=NODES):
        env = dict(os.environ, NODEYARD_DEMO_DIR=os.path.join(self.tmp.name, "demo"), NODEYARD_COLOR="never")
        env.pop("NODEYARD_SHIM_RULES_EXTRA", None)
        if rules:
            path = os.path.join(self.tmp.name, "rules")
            with open(path, "w") as f:
                f.write(rules)
            env["NODEYARD_SHIM_RULES_EXTRA"] = path
        return subprocess.run([BASH, str(ROOT / "bin" / "nodeyard"), "--demo", *args], capture_output=True, text=True, env=env, timeout=120)

    def test_pinned_entry_cleared_and_server_restarted(self):
        r = self.nodeyard("--json", "relink", "--yes", "--wait", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual([n["name"] for n in d["nodes"]], ["yard-4"])
        node = d["nodes"][0]
        self.assertEqual((node["ip"], node["interface"]), ("192.168.1.13", "eth0"))
        self.assertTrue(node["pinned"])
        self.assertTrue(node["answers"])
        self.assertTrue(node["ready"])
        self.assertTrue(d["restarted_server"])
        cmds = [" ".join(p.get("command") or []) for p in d["plan"]]
        self.assertIn("ip neigh del 192.168.1.13 dev eth0", cmds)
        self.assertIn("systemctl restart k3s", cmds)
        self.assertTrue(any("pinned" in line for line in node["done"]))

    def test_no_restart_leaves_advice(self):
        r = self.nodeyard("--json", "relink", "yard-4", "--yes", "--no-restart", "--wait", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertFalse(d["restarted_server"])
        self.assertFalse(d["nodes"][0]["ready"])
        self.assertIn("k3s-agent", d["nodes"][0]["advice"])
        self.assertNotIn("systemctl restart k3s", [" ".join(p.get("command") or []) for p in d["plan"]])

    def test_no_server_restart_when_the_agent_is_stopped(self):
        # It answers ping, but nothing listens on its kubelet port: k3s-agent
        # is stopped there, and restarting k3s on the server wouldn't help.
        rules = "timeout 2 bash -c exec 3<>/dev/tcp/192.168.1.13/10250\t1\t\n" + NODES
        r = self.nodeyard("--json", "relink", "--yes", "--wait", "1", rules=rules)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        node = d["nodes"][0]
        self.assertTrue(node["answers"])
        self.assertFalse(node["kubelet"])
        self.assertFalse(d["restarted_server"])
        self.assertIn("10250", node["advice"])

    def test_nothing_to_do_when_every_node_is_ready(self):
        r = self.nodeyard("--json", "relink", "--yes", rules=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["nodes"], [])

    def test_unknown_and_self_rejected(self):
        self.assertNotEqual(self.nodeyard("relink", "nosuch", "--yes").returncode, 0)
        self.assertNotEqual(self.nodeyard("relink", "yard-1", "--yes").returncode, 0)

    def test_doctor_offers_the_fix(self):
        r = self.nodeyard("--json", "doctor", "--only", "cluster-nodes,relink-watch")
        checks = {c["id"]: c for c in json.loads(r.stdout.strip().splitlines()[-1])["checks"]}
        self.assertEqual(checks["cluster-nodes"]["status"], "issue")
        self.assertIn("reconnect", checks["cluster-nodes"]["fix"])
        self.assertEqual(checks["relink-watch"]["status"], "issue")

    def test_watch_installs_a_timer(self):
        r = self.nodeyard("--dry-run", "relink-watch", "install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nodeyard-relink.timer", r.stderr + r.stdout)
        self.assertIn("ExecStart=/usr/local/bin/nodeyard relink --auto", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
