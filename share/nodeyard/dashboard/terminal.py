"""Shells in the dashboard (the Terminal page): on this server, on every cluster node and on your Tailscale devices.

Where a terminal can go:
  - this server: a login shell as the terminal user (--terminal-user, set by `nodeyard dashboard start` to whoever
    ran it with sudo), never root, started outside the dashboard's own sandbox through systemd;
  - a cluster node, through Kubernetes: a short-lived helper pod on that node (busybox, privileged, host PID and
    network) enters the machine's own namespaces, then logs in as the same user, or as root if you pick "Root
    shell" or "Checks". It works without SSH keys, and the pod is deleted when the terminal closes (and after
    12 hours whatever happens);
  - any cluster node or Tailscale device that accepts SSH: `ssh` as the terminal user (its own keys, or the
    device's password typed into the terminal; Tailscale SSH works too). Only devices the dashboard found itself
    (cluster nodes and `tailscale status`) can be picked: the page can't send the server to an arbitrary address.

Safety rules, all enforced here (not in the page):
  - only over Tailscale or your own network: requests that came in through Tailscale Funnel (the public internet)
    are always refused;
  - the dashboard password is asked again to open every terminal (a stolen session cookie alone isn't enough), and
    wrong ones count towards the sign-in lockout;
  - a terminal belongs to the sign-in session that opened it; idle ones close after 30 minutes, at most 8 are open
    at once and at most 4 of them on cluster nodes.

Output goes to the page as server-sent events; keystrokes come back as small POSTs.
"""
import base64
import fcntl
import ipaddress
import json
import os
import pty
import pwd
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import termios
import threading
import time

import connections
import kube

MAX_SESSIONS = 8
MAX_NODE_SESSIONS = 4
IDLE_SECONDS = 30 * 60
KEEP_BYTES = 256 * 1024
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.I)
NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
SHELL_NS = "nodeyard-shell"
SHELL_IMAGE = "busybox:1.36"
SHELL_LIFETIME = 12 * 3600
TAILNET_TTL = 20.0
NO_SSH_OS = ("ios", "android", "tvos")
METHODS = {
    "local": "Shell on this server",
    "kube": "Shell through Kubernetes",
    "root": "Root shell through Kubernetes",
    "diagnose": "Checks, then a root shell",
    "ssh": "SSH",
}

# On the node, as root, after entering the machine's namespaces: read-only checks for a node that drops out
# (Kubernetes agent, kernel messages about memory/links/GPU, pressure, disk, network, clock, temperature).
DIAGNOSE = r"""
h() { printf '\n\033[1;36m== %s\033[0m\n' "$1"; }
printf '\033[1mChecks on %s at %s\033[0m (they only read)\n' "$(hostname)" "$(date)"
uptime
h "Recent boots (newest last)"
journalctl --list-boots --no-pager 2>/dev/null | tail -n 5
for u in k3s-agent k3s; do
  if systemctl cat "$u" >/dev/null 2>&1; then
    h "Kubernetes agent ($u)"
    systemctl status "$u" --no-pager -n 0 2>/dev/null | head -n 6
    echo "-- warnings and errors in the last 2 hours (newest last):"
    journalctl -u "$u" --since "-2h" --no-pager -p warning -o short-iso 2>/dev/null | tail -n 30
  fi
done
h "Kernel: memory, network links, disks, GPU, power (last 2 hours)"
journalctl -k --since "-2h" --no-pager -o short-iso 2>/dev/null |
  grep -iE 'oom|out of memory|killed process|link is (up|down)|carrier|nvidia|xid|hung task|watchdog|i/o error|throttl|under-voltage|nfs' |
  tail -n 30
h "Memory and pressure"
free -h 2>/dev/null
for f in memory cpu io; do [ -r /proc/pressure/$f ] && printf '%s pressure: %s\n' "$f" "$(head -n1 /proc/pressure/$f)"; done
h "Disk"
df -h / /var/lib/rancher 2>/dev/null
h "Network"
ip -br addr 2>/dev/null | grep -v '^lo '
ip route show default 2>/dev/null
command -v tailscale >/dev/null 2>&1 && tailscale status --peers=false 2>/dev/null | head -n 3
h "Clock"
timedatectl 2>/dev/null | grep -iE 'local time|synchronized|ntp service'
h "Temperature"
for z in /sys/class/thermal/thermal_zone*; do
  [ -r "$z/temp" ] && printf '%s: %s C\n' "$(cat "$z/type" 2>/dev/null)" "$(( $(cat "$z/temp") / 1000 ))"
done 2>/dev/null
command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled 2>/dev/null
command -v nvidia-smi >/dev/null 2>&1 &&
  nvidia-smi --query-gpu=name,temperature.gpu,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null
printf '\n\033[2mThat was everything. This is now a root shell on %s: type exit to close it.\033[0m\n' "$(hostname)"
"""
ROOT_SHELL = 'cd /root 2>/dev/null || cd /; if command -v bash >/dev/null 2>&1; then exec bash -l; fi; exec sh -l'
USER_SHELL = ('id "$1" >/dev/null 2>&1 || { echo "There is no account $1 on $(hostname). Open a Root shell instead."; exit 1; }; '
              'exec su -l "$1"')

# Runs the helper pod and attaches to it. Everything comes in as arguments (no values are pasted into the script).
NODE_SHELL = r"""
KBIN=$1 KSUB=$2 KC=$3 NS=$4 POD=$5 NODE=$6 MANIFEST=$7
shift 7
k() { if [ -n "$KSUB" ]; then "$KBIN" "$KSUB" --kubeconfig "$KC" "$@"; else "$KBIN" --kubeconfig "$KC" "$@"; fi; }
gone() { k -n "$NS" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1; }
trap 'gone; exit 129' HUP TERM INT
printf '\033[2mStarting a helper pod on %s (Kubernetes)...\033[0m\n' "$NODE"
k get namespace "$NS" >/dev/null 2>&1 || k create namespace "$NS" >/dev/null 2>&1
k label namespace "$NS" pod-security.kubernetes.io/enforce=privileged --overwrite >/dev/null 2>&1
if ! printf '%s' "$MANIFEST" | k apply -f - >/dev/null; then
  echo "Kubernetes refused the helper pod. Try SSH instead."
  exit 1
fi
if ! k -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=90s >/dev/null 2>&1; then
  echo "The helper pod didn't start on $NODE within 90 seconds:"
  k -n "$NS" get pod "$POD" -o wide 2>&1 | sed 's/^/  /'
  k -n "$NS" get events --field-selector "involvedObject.name=$POD" 2>/dev/null | tail -n 5 | sed 's/^/  /'
  echo "If $NODE isn't Ready its kubelet may be down: try SSH (over Tailscale) instead."
  gone
  exit 1
fi
k -n "$NS" exec -it "$POD" -- "$@"
s=$?
gone
exit $s
"""


class TermError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Session:
    def __init__(self, sid, owner, argv, cols, rows, target=None, cleanup=None):
        self.id, self.owner = sid, owner
        self.target = target or {}
        self.cleanup = cleanup
        self.started = time.time()
        self.buf = bytearray()
        self.base = 0          # offset of buf[0] in the whole output
        self.cond = threading.Condition()
        self.alive = True
        self.last = time.time()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: become the shell
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        self.resize(cols, rows)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    continue
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.cond:
                self.buf += data
                if len(self.buf) > KEEP_BYTES:
                    cut = len(self.buf) - KEEP_BYTES
                    del self.buf[:cut]
                    self.base += cut
                self.cond.notify_all()
        with self.cond:
            self.alive = False
            self.cond.notify_all()
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._cleanup()

    def _cleanup(self):
        fn, self.cleanup = self.cleanup, None
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001 -- best effort (the pod also expires by itself)
                pass

    def write(self, data):
        self.last = time.time()
        if not self.alive:
            raise TermError("That terminal has closed.", 410)
        try:
            os.write(self.fd, data)
        except OSError:
            raise TermError("That terminal has closed.", 410)

    def resize(self, cols, rows):
        cols, rows = max(10, min(500, int(cols))), max(4, min(200, int(rows)))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def read_from(self, offset):
        """(bytes since offset, new offset, alive)"""
        with self.cond:
            start = max(offset, self.base)
            return bytes(self.buf[start - self.base:]), self.base + len(self.buf), self.alive

    def close(self):
        self.alive = False
        for kill in (os.killpg, os.kill):  # the whole group (ssh, kubectl), else just the shell
            try:
                kill(self.pid, signal.SIGHUP)
                break
            except OSError:
                continue
        if self.cleanup is not None:
            threading.Thread(target=self._cleanup, daemon=True).start()

    def describe(self):
        with self.cond:
            end = self.base + len(self.buf)
        return {"id": self.id, "alive": self.alive, "started": self.started, "target": self.target, "end": end}


def _which(*names):
    for n in names:
        if n.startswith("/"):
            if os.access(n, os.X_OK):
                return n
        else:
            p = shutil.which(n)
            if p:
                return p
    return ""


def read_tailnet(binary, timeout=4.0):
    """(this machine, [peers]) from `tailscale status --json`; each {'name','dns','ips','os','online','last_seen'}."""
    out = subprocess.run([binary, "status", "--json"], capture_output=True, text=True, timeout=timeout, check=False)
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError((out.stderr or "tailscale status failed").strip().splitlines()[-1][:200])
    return parse_tailnet(json.loads(out.stdout))


def parse_tailnet(data):
    def one(p):
        ips = []
        for ip in p.get("TailscaleIPs") or []:
            try:
                ips.append(str(ipaddress.ip_address(ip)))
            except ValueError:
                continue
        ips.sort(key=lambda a: ":" in a)  # IPv4 first
        dns = (p.get("DNSName") or "").rstrip(".")
        seen = kube.parse_time(p.get("LastSeen"))
        return {"name": p.get("HostName") or dns.split(".")[0], "dns": dns, "ips": ips, "os": p.get("OS", "") or "",
                "online": bool(p.get("Online")), "last_seen": seen if seen > 0 else 0.0}
    me = one(data.get("Self") or {})
    me["online"] = True
    peers = [one(p) for p in (data.get("Peer") or {}).values()]
    return me, [p for p in peers if p["ips"]]


def demo_tailnet(nodes):
    """A made-up tailnet for --demo: every node, plus a laptop, a phone and an old machine that's offline."""
    peers = [{"name": n["name"], "dns": n["name"] + ".tail0000.ts.net", "ips": ["100.64.0." + n["internal_ip"].rsplit(".", 1)[1]],
              "os": "linux", "online": True, "last_seen": 0.0} for n in nodes[1:] if n.get("internal_ip")]
    peers += [{"name": "laptop", "dns": "laptop.tail0000.ts.net", "ips": ["100.64.1.20"], "os": "macOS", "online": True, "last_seen": 0.0},
              {"name": "phone", "dns": "phone.tail0000.ts.net", "ips": ["100.64.1.21"], "os": "iOS", "online": True, "last_seen": 0.0},
              {"name": "old-desktop", "dns": "old-desktop.tail0000.ts.net", "ips": ["100.64.1.22"], "os": "windows", "online": False,
               "last_seen": time.time() - 9 * 86400}]
    first = nodes[0] if nodes else {"name": "server", "internal_ip": "192.168.1.10"}
    me = {"name": first["name"], "dns": "", "ips": ["100.64.0." + first.get("internal_ip", "0.0.0.10").rsplit(".", 1)[1]], "os": "linux",
          "online": True, "last_seen": 0.0}
    return me, peers


def by_name(peers, node):
    """The tailnet device for a node found only by name: Tailscale adds -1, -2... when a name is taken, so an online
    "debian-1-1" beats an offline "debian-1" left behind by an earlier install."""
    node = node.lower()
    same = [p for p in peers if p["name"].lower() == node]
    suffixed = [p for p in peers if re.match(re.escape(node) + r"-\d+$", p["name"].lower())]
    for group in ([p for p in same if p["online"]], [p for p in suffixed if p["online"]], same):
        if group:
            return group[0]
    return None


def pod_name(node):
    base = re.sub(r"[^a-z0-9-]+", "-", node.lower()).strip("-")[:40] or "node"
    return "shell-%s-%s" % (base, secrets.token_hex(3))


def pod_manifest(pod, node, sid):
    return json.dumps({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": SHELL_NS, "labels": {"app": "nodeyard-shell", "nodeyard/session": sid[:12]}},
        "spec": {
            "nodeName": node, "hostPID": True, "hostNetwork": True, "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0, "activeDeadlineSeconds": SHELL_LIFETIME, "automountServiceAccountToken": False,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{"name": "shell", "image": SHELL_IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["sleep", str(SHELL_LIFETIME)],
                            # no limits: what you run in the shell lives in this pod's cgroup
                            "securityContext": {"privileged": True}, "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}],
        },
    }, separators=(",", ":"))


def enter_host(mode, user):
    """What `kubectl exec` runs in the helper pod: the machine's own namespaces, then a shell."""
    base = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "env", "TERM=xterm-256color"]
    if mode == "kube":
        return base + ["sh", "-c", USER_SHELL, "sh", user]
    if mode == "diagnose":
        return base + ["HOME=/root", "sh", "-c", DIAGNOSE + ROOT_SHELL]
    return base + ["HOME=/root", "sh", "-c", ROOT_SHELL]


class Terminals:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.user = getattr(args, "terminal_user", "") or ""
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.lock = threading.Lock()
        self.sessions = {}
        self.tailnet = (0.0, None, [], "")  # (time, me, peers, error)
        self.tailnet_lock = threading.Lock()
        k3s = _which("/usr/local/bin/k3s", "k3s")
        self.kubectl = (k3s, "kubectl") if k3s else ((_which("kubectl"), "") if _which("kubectl") else None)
        self.tailscale = _which("tailscale", "/usr/bin/tailscale", "/usr/sbin/tailscale", "/usr/local/bin/tailscale")
        self.ssh = _which("/usr/bin/ssh", "ssh")
        self.systemd_run = _which("systemd-run")
        threading.Thread(target=self._reaper, daemon=True).start()
        if not self.demo and self.kubectl and os.path.exists(self.kubeconfig):
            threading.Thread(target=self._sweep_pods, daemon=True).start()

    # -- what can be opened --------------------------------------------------

    def available(self):
        if self.demo:
            return True, ""
        if not self.user or not USER_RE.match(self.user):
            return False, "No terminal user is set. Restart the dashboard with sudo from your own account: sudo nodeyard dashboard start"
        try:
            pwd.getpwnam(self.user)
        except KeyError:
            return False, "The terminal user %s doesn't exist on this machine." % self.user
        return True, ""

    def _tailnet(self):
        with self.tailnet_lock:
            at, me, peers, err = self.tailnet
            if time.time() - at < TAILNET_TTL:
                return me, peers, err
            if self.demo:
                nodes = ((self.ctx.store.snapshot().get("state") or {}).get("nodes")) or []
                me, peers = demo_tailnet(nodes)
                err = ""
            elif not self.tailscale:
                me, peers, err = None, [], "Tailscale isn't installed on this server."
            else:
                try:
                    me, peers = read_tailnet(self.tailscale)
                    err = ""
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                    me, peers, err = None, [], "Couldn't read the tailnet: %s" % (str(e)[:160] or e.__class__.__name__)
            self.tailnet = (time.time(), me, peers, err)
            return me, peers, err

    def targets(self):
        snap = self.ctx.store.snapshot()
        nodes = ((snap.get("state") or {}).get("nodes")) or []
        me, peers, ts_error = self._tailnet()
        ok, _ = self.available()
        host = socket.gethostname()
        can_ssh = ok and bool(self.ssh and (self.systemd_run or self.demo))
        can_kube = self.demo or bool(self.kubectl and os.path.exists(self.kubeconfig))
        out = [{"id": "local", "name": host, "kind": "server", "online": True, "status": "this server", "address": "",
                "methods": ["local"] if ok else [], "note": "" if ok else "No terminal user is set."}]
        used = set()
        my_ips = set((me or {}).get("ips") or [])
        for n in nodes:
            name = n.get("name", "")
            if not NODE_RE.match(name):
                continue
            found = connections.discover(n)
            ts = [a["address"] for a in found["tailscale"]]
            lan = [a["address"] for a in found["lan"]]
            peer = next((p for p in peers if set(p["ips"]) & set(ts + lan)), None) or by_name(peers, name)
            if peer is not None:
                used.add(id(peer))
                ts = peer["ips"][:1] + [a for a in ts if a not in peer["ips"][:1]]
            this = name == host or bool(my_ips & set(ts + lan))
            address = (ts or lan or [""])[0]
            methods = ((["kube"] if ok else []) + ["root", "diagnose"]) if can_kube else []
            if can_ssh and address and not this:
                methods.append("ssh")
            out.append({"id": "node:" + name, "name": name, "kind": "node", "ready": bool(n.get("ready")),
                        "online": bool(n.get("ready")) or bool(peer and peer["online"]),
                        "status": n.get("status", ""), "reason": n.get("ready_reason", "") if not n.get("ready") else "",
                        "address": address, "addresses": ts + lan, "tailnet": (peer or {}).get("name", ""),
                        "tailnet_online": (peer or {}).get("online"), "os": n.get("os", ""), "this_server": this, "methods": methods,
                        "note": "" if can_kube else "Node shells need kubectl on this server."})
        for p in sorted(peers, key=lambda p: (not p["online"], p["name"].lower())):
            if id(p) in used or my_ips & set(p["ips"]):
                continue
            phone = p["os"].lower() in NO_SSH_OS
            methods = ["ssh"] if can_ssh and not phone else []
            out.append({"id": "ts:" + p["ips"][0], "name": p["name"], "kind": "tailnet", "online": p["online"],
                        "status": "online" if p["online"] else "offline", "last_seen": p["last_seen"], "address": p["ips"][0],
                        "addresses": p["ips"], "dns": p["dns"], "os": p["os"], "methods": methods,
                        "note": "Phones and tablets don't accept SSH." if phone else ("" if can_ssh else "SSH isn't available on this server.")})
        return {"targets": out, "tailnet_error": ts_error, "user": self.user,
                "tools": {"kubectl": bool(self.kubectl), "tailscale": bool(self.tailscale), "ssh": bool(self.ssh)}}

    # -- starting one ----------------------------------------------------------

    def _shell_argv(self):
        u = pwd.getpwnam(self.user)
        shell = u.pw_shell if u.pw_shell and os.path.exists(u.pw_shell) and not u.pw_shell.endswith(("nologin", "false")) else "/bin/bash"
        return self._as_user([shell, "-l"], "nodeyard dashboard terminal")

    def _as_user(self, argv, what):
        # systemd starts it outside the dashboard's sandbox, as that user, in their home
        return ["systemd-run", "--quiet", "--collect", "--pty", "--uid=" + self.user, "--setenv=TERM=xterm-256color",
                "-p", "WorkingDirectory=~", "--description=" + what] + argv

    def _ssh_argv(self, target, login):
        argv = [self.ssh, "-t", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-o", "StrictHostKeyChecking=accept-new", "-l", login, "--", target["address"]]
        return self._as_user(argv, "nodeyard dashboard ssh to %s" % target["name"])

    def _node_argv(self, target, mode, sid):
        node = target["name"]
        pod = pod_name(node)
        kbin, ksub = self.kubectl
        inner = enter_host(mode, self.user)
        argv = ["/bin/sh", "-c", NODE_SHELL, "nodeyard-shell", kbin, ksub, self.kubeconfig, SHELL_NS, pod, node,
                pod_manifest(pod, node, sid)] + inner
        if self.systemd_run:
            # its own small sandbox and memory limit, outside the dashboard's (a stuck kubectl can't starve the page)
            argv = ["systemd-run", "--quiet", "--collect", "--pty", "--setenv=TERM=xterm-256color", "--setenv=HOME=/tmp",
                    "-p", "PrivateTmp=true", "-p", "ProtectSystem=strict", "-p", "ProtectHome=true", "-p", "NoNewPrivileges=true",
                    "-p", "MemoryMax=256M", "--description=nodeyard dashboard shell on %s" % node] + argv
        return argv, pod

    def _delete_pod(self, pod):
        kbin, ksub = self.kubectl
        cmd = [kbin] + ([ksub] if ksub else []) + ["--kubeconfig", self.kubeconfig, "-n", SHELL_NS, "delete", "pod", pod,
                                                   "--ignore-not-found", "--wait=false"]
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)

    def _sweep_pods(self):
        """Helper pods from before a restart belong to no one: remove them."""
        time.sleep(5)
        kbin, ksub = self.kubectl
        try:
            subprocess.run([kbin] + ([ksub] if ksub else []) + ["--kubeconfig", self.kubeconfig, "-n", SHELL_NS, "delete", "pod",
                                                                "-l", "app=nodeyard-shell", "--ignore-not-found", "--wait=false"],
                           capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    def _demo_argv(self, target, mode):
        how = {"local": "a shell on this server", "kube": "a shell on %s through Kubernetes" % target["name"],
               "root": "a root shell on %s through Kubernetes" % target["name"],
               "diagnose": "the read-only checks on %s, then a root shell" % target["name"],
               "ssh": "ssh to %s (%s)" % (target["name"], target.get("address", ""))}.get(mode, mode)
        banner = "\\033[1;36mDemo terminal\\033[0m: this would be %s. Nothing you type is run.\\r\\n" % how
        return ["/bin/sh", "-c", 'printf "%b" "$1"; while IFS= read -r l; do printf "\\033[33m(demo)\\033[0m would run: %s\\r\\n" "$l"; done',
                "demo", banner]

    def _reaper(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                for sid, s in list(self.sessions.items()):
                    if not s.alive or now - s.last > IDLE_SECONDS:
                        s.close()
                        if not s.alive:
                            self.sessions.pop(sid, None)

    def open(self, handler, body):
        if handler._forwarded() is not None:
            raise TermError("Terminals only work over Tailscale or your own network, never through public access.", 403)
        target_id = str(body.get("target") or "local")[:300]
        mode = str(body.get("method") or ("local" if target_id == "local" else "kube"))
        if mode not in METHODS:
            raise TermError("Unknown way to connect.")
        if mode in ("local", "kube", "ssh"):
            ok, why = self.available()
            if not ok:
                raise TermError(why, 503)
        target = next((t for t in self.targets()["targets"] if t["id"] == target_id), None)
        if target is None:
            raise TermError("That device isn't in the list any more. Refresh the page.", 404)
        if mode not in target["methods"]:
            raise TermError(target.get("note") or "%s can't be opened that way." % target["name"], 409)
        login = str(body.get("login") or self.user or "")[:32]
        if mode == "ssh" and not USER_RE.match(login):
            raise TermError("That isn't a valid user name.")
        auth = self.ctx.auth
        if auth is not None:
            ip = handler.client_address[0]
            wait = auth.retry_after(ip)
            if wait:
                raise TermError("Too many wrong passwords. Try again in %d seconds." % wait, 429)
            if auth.login(ip, str(body.get("password", ""))[:200]) is None:
                time.sleep(0.4)
                raise TermError("That isn't the right password.", 401)
        with self.lock:
            mine = [s for s in self.sessions.values() if s.alive]
            if len(mine) >= MAX_SESSIONS:
                raise TermError("%d terminals are open already. Close one first." % MAX_SESSIONS, 429)
            if mode in ("kube", "root", "diagnose") and sum(1 for s in mine if s.target.get("method") in ("kube", "root", "diagnose")) >= MAX_NODE_SESSIONS:
                raise TermError("%d node shells are open already. Close one first." % MAX_NODE_SESSIONS, 429)
            sid = secrets.token_hex(12)
            cleanup, pod = None, ""
            if self.demo:
                argv = self._demo_argv(target, mode)
            elif mode == "local":
                argv = self._shell_argv()
            elif mode == "ssh":
                argv = self._ssh_argv(target, login)
            else:
                argv, pod = self._node_argv(target, mode, sid)
                cleanup = lambda: self._delete_pod(pod)  # noqa: E731
            who = "root" if mode in ("root", "diagnose") else (login if mode == "ssh" else (self.user or os.environ.get("USER", "")))
            info = {"id": target["id"], "name": target["name"], "kind": target["kind"], "method": mode, "user": who,
                    "address": target.get("address", ""), "how": METHODS[mode]}
            s = Session(sid, handler._token() or "local", argv, body.get("cols", 100), body.get("rows", 30), info, cleanup)
            self.sessions[sid] = s
        return {"id": sid, "user": who, "target": info}

    # -- an open one -----------------------------------------------------------

    def get(self, handler, sid):
        with self.lock:
            s = self.sessions.get(str(sid))
        if s is None or s.owner != (handler._token() or "local"):
            raise TermError("No such terminal.", 404)
        if handler._forwarded() is not None:
            raise TermError("Terminals only work over Tailscale or your own network.", 403)
        return s

    def stream(self, handler, q):
        try:
            s = self.get(handler, q.get("id", [""])[0])
            offset = int(q.get("from", ["0"])[0] or 0)
        except (TermError, ValueError) as e:
            return handler._json({"ok": False, "error": str(e)}, getattr(e, "code", 400))
        handler.send_response(200)
        for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                     ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
            handler.send_header(k, v)
        handler.end_headers()
        handler.close_connection = True
        try:
            handler.wfile.write(b"retry: 1000\n\n")
            while True:
                with s.cond:
                    s.cond.wait_for(lambda: s.base + len(s.buf) > offset or not s.alive, timeout=15)
                data, offset, alive = s.read_from(offset)
                if data:
                    msg = json.dumps({"d": base64.b64encode(data).decode(), "o": offset})
                    handler.wfile.write(("event: out\ndata: %s\n\n" % msg).encode())
                elif alive:
                    handler.wfile.write(b": ping\n\n")
                if not alive and not data:
                    handler.wfile.write(b"event: exit\ndata: {}\n\n")
                    handler.wfile.flush()
                    return None
                handler.wfile.flush()
                if self.ctx.auth is not None and not handler._authed():
                    return None
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None

    def input(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        data = body.get("data", "")
        if not isinstance(data, str) or len(data) > 65536:
            raise TermError("Bad input.")
        s.write(data.encode("utf-8", "surrogateescape"))
        return {}

    def resize(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        try:
            s.resize(body.get("cols", 80), body.get("rows", 24))
        except (TypeError, ValueError):
            raise TermError("Bad size.")
        return {}

    def close(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        s.close()
        return {}

    def info(self, handler):
        ok, why = self.available()
        owner = handler._token() or "local"
        with self.lock:
            mine = [s.describe() for s in self.sessions.values() if s.owner == owner]
        return {"available": ok, "why": why, "user": self.user, "public": handler._forwarded() is not None,
                "open": sum(1 for m in mine if m["alive"]), "sessions": mine}


def register(ctx, args):
    t = Terminals(ctx, args)
    ctx.terminals = t

    def wrap(fn):
        def route(h, arg):
            try:
                h._json(dict(fn(h, arg) or {}, ok=True))
            except TermError as e:
                h._json({"ok": False, "error": str(e)}, e.code)
        return route

    def targets(h, q):
        if h._forwarded() is not None:
            raise TermError("Terminals only work over Tailscale or your own network.", 403)
        return t.targets()

    ctx.get_routes.update({
        "/api/term/info": wrap(lambda h, q: t.info(h)),
        "/api/term/targets": wrap(targets),
        "/api/term/stream": lambda h, q: t.stream(h, q),
    })
    ctx.post_routes.update({
        "/api/term/open": wrap(t.open),
        "/api/term/input": wrap(t.input),
        "/api/term/resize": wrap(t.resize),
        "/api/term/close": wrap(t.close),
    })
