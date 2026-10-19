"""PwnForge, hosted from the dashboard if you want it: the self-hosted cybersecurity training platform
(github.com/Codemanhtmlpythoncss/pwnforge), run as its own systemd service on the machine the dashboard runs on.

The dashboard never needs PwnForge. Off or not installed, this page just says so. When it's on:
  - Install / Update gets PwnForge's code, Python packages and website and installs PwnForge's own unit file, which
    runs it as the unprivileged user "pwnforge" (pwnforge-host.sh, through systemd-run: the only step that runs as root);
  - it starts, stops and restarts the service, and turns starting at boot on or off;
  - it sets the address: this machine only (the default), your tailnet, or every network this machine is on (only
    after PwnForge's administrator account exists, and only when asked for in so many words). Never the internet;
  - it shows the service's health (/api/health), its log with anything that looks like a secret hidden, and the
    training environments through `pwnforge env` (PwnForge's own services, run as the service user), so they are
    started, stopped and removed exactly as the website would. Nothing here edits PwnForge's database.

Training targets stay on PwnForge's isolated lab networks: this page shows each one's network and whether it has
outside access, and changes neither.

  GET  /api/pwnforge          status, settings, health, environments
  GET  /api/pwnforge/logs     the service's recent log (and the last install's), secrets hidden
  POST /api/pwnforge/action   {action: start|stop|restart|enable|disable|hosting-on|hosting-off|install|doctor|lab-access-on|lab-access-off|lab-access-status}
  POST /api/pwnforge/config   {bind: local|tailnet|lan, port, confirm_lan}
  POST /api/pwnforge/env      {action: start|stop|delete, kind: vm|lab, id} or {action: cleanup, apply}
"""
import json
import ipaddress
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(HERE, "pwnforge-host.sh")
REPO = "https://github.com/Codemanhtmlpythoncss/pwnforge.git"
UNIT = "pwnforge.service"
DROPIN = "/etc/systemd/system/pwnforge.service.d/nodeyard.conf"   # written by pwnforge-host.sh
JOB = "nodeyard-pwnforge-job"
# systemd's own housekeeping for the short-lived --collect job unit: harmless once the unit is gone, and it floods
# the log so a successful install looks failed. Genuine failure lines ("Main process exited", "Failed with result",
# "status=1/FAILURE") are NOT matched here and stay visible.
JOB_NOISE_RE = re.compile(
    r"(?m)^.*%s(?:\.service)?: (?:Failed to open /run/systemd/transient/|Consumed [\d.]+\w* CPU time|"
    r"Deactivated successfully|Scheduled restart job|Unit .* has finished).*\n?" % re.escape(JOB))
SVC_USER = "pwnforge"
DEFAULTS = {"enabled": False, "path": "/opt/pwnforge", "data_dir": "/var/lib/pwnforge", "bind": "local", "port": 8080, "autostart": True}
BINDS = {"local": "This machine only", "tailnet": "Your tailnet", "https": "Your tailnet, over HTTPS",
         "lan": "Every network this machine is on"}
SERVE_KEPT = (443, 8443, 10000)   # the dashboard's own https:// and its public access (Tailscale Funnel)
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENV_TTL = 15
# what a log line must never show: passwords, tokens, keys, cookies, bearer headers, captured flags
SECRET_RE = re.compile(r"(?i)\b(password|passwd|pass|secret|token|api[_-]?key|authorization|cookie|session(?:_id)?|secret_key)(\"?\s*[=:]\s*\"?)([^\s\"',;&]+)")
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
FLAG_RE = re.compile(r"(?i)\b(?:flag|pf|pwnforge|htb|thm|ctf)\{[^}\s]{4,}\}")


class PFError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def redact(text):
    text = BEARER_RE.sub(lambda m: m.group(1) + " [hidden]", text or "")
    text = SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", text)
    return FLAG_RE.sub("[flag hidden]", text)


def tailscale_ip():
    ts = shutil.which("tailscale") or ("/usr/bin/tailscale" if os.path.exists("/usr/bin/tailscale") else "")
    if not ts:
        return ""
    try:
        out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=4, check=False).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out[0] if out and re.match(r"^\d+\.\d+\.\d+\.\d+$", out[0]) else ""


def tailscale_name():
    """This machine's MagicDNS name (e.g. pi.example.ts.net), or "" without Tailscale or MagicDNS."""
    ts = shutil.which("tailscale") or ("/usr/bin/tailscale" if os.path.exists("/usr/bin/tailscale") else "")
    if not ts:
        return ""
    try:
        out = subprocess.run([ts, "status", "--json", "--peers=false"], capture_output=True, text=True, timeout=4, check=False).stdout
        name = str(((json.loads(out or "{}") or {}).get("Self") or {}).get("DNSName") or "").rstrip(".")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return name if re.match(r"^[A-Za-z0-9.-]+$", name) else ""


def read_dropin(path=None):
    """The folder, data folder, address and port pwnforge-host.sh last set up, or None."""
    try:
        with open(path or DROPIN, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^ExecStart=(/\S+)/\.venv/bin/pwnforge serve --host (\S+) --port (\d+)\s*$", text, re.M)
    d = re.search(r"^Environment=PWNFORGE_DATA_DIR=(/\S+)\s*$", text, re.M)
    if not m or not d:
        return None
    return {"path": m.group(1), "data_dir": d.group(1), "host": m.group(2), "port": int(m.group(3))}


def bind_of(host):
    if host.startswith("127.") or host == "::1":
        return "local"
    return "lan" if host in ("0.0.0.0", "::") else "tailnet"


def answers_as_pwnforge(host, port):
    """Whether PwnForge itself is what answers on host:port (its /api/health)."""
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        url = "http://%s:%d/api/health" % (("[%s]" % host) if ":" in host else host, port)
        with urllib.request.urlopen(url, timeout=2) as r:
            body = json.loads(r.read(4096) or b"{}")
        return isinstance(body, dict) and body.get("status") == "ok" and "version" in body
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_in_use(port):
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


class PwnForge:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        self.path = os.path.join(self.state_dir, "pwnforge.json")
        self.dash_port = int(getattr(args, "port", 0) or 0)
        self.lock = threading.Lock()
        self.cfg = self._load()
        self.env_cache = {"at": 0.0, "data": None, "error": "", "busy": False}
        self.cpu_prev = {}
        self.job = None   # {"what", "started", "unit"}
        if self.demo:
            if not os.path.exists(self.path):
                self.cfg["enabled"] = True
            self.fake = {"installed": True, "active": "active", "enabled": True, "initialised": True, "since": time.time() - 5400,
                         "vms": [
                             {"kind": "vm", "id": "vm1", "name": "web-basics-target", "status": "running", "driver": "simulation", "domain": "pf-vm1",
                              "vcpu": 1, "memory_mb": 1024, "ip_address": "10.77.1.10", "network": {"mode": "isolated", "external_access": False, "cidr": "10.77.1.0/24"},
                              "lab_instance_id": None, "persistent": False, "disposable": True, "owner": "learner", "disk_bytes": 412 << 20,
                              "started_at": "2026-10-18T09:12:00+00:00", "last_heartbeat": None, "last_error": None},
                             {"kind": "vm", "id": "vm2", "name": "linux-privesc-01", "status": "stopped", "driver": "simulation", "domain": "pf-vm2",
                              "vcpu": 2, "memory_mb": 2048, "ip_address": None, "network": {"mode": "isolated", "external_access": False, "cidr": "10.77.2.0/24"},
                              "lab_instance_id": None, "persistent": True, "disposable": False, "owner": "learner", "disk_bytes": 1310 << 20,
                              "started_at": None, "last_heartbeat": None, "last_error": None}],
                         "labs": [{"kind": "lab", "id": "lab1", "lab_id": "ad-intro", "title": "Active Directory intro", "status": "stopped", "last_error": None,
                                   "started_at": None, "machines": []}]}

    # ---- settings ----------------------------------------------------------------------------------------------
    def _load(self):
        cfg = dict(DEFAULTS)
        saved = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            pass
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in DEFAULTS if k in saved})
        elif not self.demo:
            # installed before this page had settings (by hand, or by an older dashboard): use what's there, not the defaults
            found = read_dropin()
            if found:
                cfg.update(path=found["path"], data_dir=found["data_dir"], port=found["port"], bind=bind_of(found["host"]), enabled=True,
                           autostart=os.path.exists("/etc/systemd/system/multi-user.target.wants/" + UNIT))
        if cfg["bind"] not in BINDS or not PATH_RE.match(str(cfg["path"])) or not PATH_RE.match(str(cfg["data_dir"])):
            cfg.update(bind=DEFAULTS["bind"], path=DEFAULTS["path"], data_dir=DEFAULTS["data_dir"])
        try:
            cfg["port"] = int(cfg["port"])
        except (TypeError, ValueError):
            cfg["port"] = DEFAULTS["port"]
        return cfg

    def _save(self):
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
            os.replace(tmp, self.path)
        except OSError as e:
            raise PFError("Couldn't save the PwnForge settings: %s" % (e.strerror or e), 500)

    # ---- running things ----------------------------------------------------------------------------------------
    @staticmethod
    def _run(argv, timeout=30, stdin=None):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin, check=False)
        except FileNotFoundError:
            raise PFError("%s isn't installed on this machine." % argv[0], 501)
        except subprocess.TimeoutExpired:
            raise PFError("%s took longer than %d s." % (" ".join(argv[:3]), timeout), 504)
        return r.returncode, r.stdout, r.stderr

    def _root(self, argv, timeout=60):
        """argv as root outside the dashboard's sandbox (a short-lived systemd unit), waiting for it."""
        return self._run(["systemd-run", "--quiet", "--collect", "--pipe", "--wait", "--service-type=exec",
                          "--description=nodeyard dashboard: PwnForge", "-p", "WorkingDirectory=/", "--"] + argv, timeout)

    def _cli(self, args, timeout=120):
        """PwnForge's own command line, as the service user with the service's data folder."""
        c = self.cfg
        return self._root(["runuser", "-u", SVC_USER, "--", "env", "PWNFORGE_DATA_DIR=" + c["data_dir"],
                           os.path.join(c["path"], ".venv", "bin", "pwnforge")] + args, timeout)

    def platform_problem(self):
        if self.demo:
            return ""
        if platform.system() != "Linux":
            return "PwnForge hosting needs the dashboard to run on Linux with systemd."
        if not shutil.which("systemctl") or not shutil.which("systemd-run"):
            return "PwnForge hosting needs systemd (systemctl and systemd-run) on this machine."
        return ""

    def installed(self):
        if self.demo:
            return self.fake["installed"]
        c = self.cfg
        return os.path.isfile(os.path.join(c["path"], "backend", "app", "main.py")) and os.access(os.path.join(c["path"], ".venv", "bin", "pwnforge"), os.X_OK)

    def unit(self):
        if self.demo:
            f = self.fake
            return {"loaded": f["installed"], "active": f["active"], "sub": "running" if f["active"] == "active" else "dead", "enabled": f["enabled"],
                    "pid": 4242 if f["active"] == "active" else 0, "since": f["since"] if f["active"] == "active" else 0, "restarts": 0, "result": "success"}
        rc, out, _ = self._run(["systemctl", "show", UNIT, "-p", "LoadState,ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestampMonotonic,NRestarts,Result"], 10)
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        since = 0.0
        try:
            mono = int(kv.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
            if mono:
                since = time.time() - (time.monotonic() - mono)
        except ValueError:
            pass
        return {"loaded": kv.get("LoadState") == "loaded", "active": kv.get("ActiveState", "unknown"), "sub": kv.get("SubState", ""),
                "enabled": kv.get("UnitFileState") in ("enabled", "enabled-runtime"), "pid": int(kv.get("MainPID") or 0),
                "since": since if kv.get("ActiveState") == "active" else 0, "restarts": int(kv.get("NRestarts") or 0), "result": kv.get("Result", "")}

    def bind_host(self, bind):
        if bind in ("local", "https"):     # (over HTTPS, Tailscale Serve is the only way in from elsewhere)
            return "127.0.0.1"
        if bind == "lan":
            return "0.0.0.0"
        ip = "100.64.0.1" if self.demo else tailscale_ip()
        if not ip:
            raise PFError("This machine has no Tailscale address (is Tailscale up?).", 409)
        return ip

    def urls(self):
        c, port = self.cfg, self.cfg["port"]
        if c["bind"] == "local":
            return [{"url": "http://localhost:%d/" % port, "note": "on the dashboard's machine only (from elsewhere: ssh -L %d:localhost:%d)" % (port, port)}]
        if c["bind"] == "tailnet":
            ip = "100.64.0.1" if self.demo else tailscale_ip()
            return [{"url": "http://%s:%d/" % (ip, port), "note": "devices on your tailnet"}] if ip else []
        if c["bind"] == "https":
            name = "pi.example.ts.net" if self.demo else tailscale_name()
            return [{"url": "https://%s:%d/" % (name, port), "note": "devices on your tailnet, over HTTPS"}] if name else []
        host = socket.gethostname()
        out = [{"url": "http://%s:%d/" % (host, port), "note": "every network this machine is on"}]
        ip = "" if self.demo else tailscale_ip()
        if ip:
            out.append({"url": "http://%s:%d/" % (ip, port), "note": "over Tailscale"})
        return out

    def health(self):
        if self.demo:
            ok = self.fake["active"] == "active"
            return {"ok": ok, "version": "1.2.0", "ms": 12, "initialised": self.fake["initialised"]} if ok else {"ok": False, "error": "not running"}
        try:
            host = self.bind_host(self.cfg["bind"])
        except PFError as e:
            return {"ok": False, "error": str(e)}
        host = "127.0.0.1" if host == "0.0.0.0" else host
        base = "http://%s:%d" % (("[%s]" % host) if ":" in host else host, self.cfg["port"])
        t0 = time.time()
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                body = json.loads(r.read(4096) or b"{}")
            ms = int((time.time() - t0) * 1000)
            initialised = None
            try:
                with urllib.request.urlopen(base + "/api/auth/status", timeout=3) as r:
                    initialised = bool(json.loads(r.read(4096) or b"{}").get("initialised"))
            except (OSError, ValueError, urllib.error.URLError):
                pass
            return {"ok": body.get("status") == "ok", "version": str(body.get("version") or ""), "ms": ms, "initialised": initialised}
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": "HTTP %d from /api/health" % e.code}
        except ValueError:
            return {"ok": False, "error": "Something answers on port %d, but it isn't PwnForge (another program uses that port?): "
                                          "pick another port under Address." % self.cfg["port"]}
        except (OSError, urllib.error.URLError) as e:
            return {"ok": False, "error": str(getattr(e, "reason", "") or e)[:200]}

    # ---- training environments ---------------------------------------------------------------------------------
    def environments(self, wait=False):
        """`pwnforge env list` (cached; refreshed in the background so the page never waits for it)."""
        if self.demo:
            return {"vms": self.fake["vms"], "labs": self.fake["labs"]}, ""
        ec = self.env_cache
        stale = time.time() - ec["at"] > ENV_TTL
        if stale and not ec["busy"]:
            ec["busy"] = True
            t = threading.Thread(target=self._refresh_env, daemon=True)
            t.start()
            if wait or ec["data"] is None:
                t.join(30)
        return ec["data"], ec["error"]

    def _refresh_env(self):
        ec = self.env_cache
        try:
            rc, out, err = self._cli(["env", "list", "--json"], 60)
            if rc == 0:
                lines = [l for l in out.splitlines() if l.startswith("{")]
                ec["data"] = json.loads(lines[-1]) if lines else {"vms": [], "labs": []}
                ec["error"] = ""
                ec["storage"] = self.storage()
            elif "No such command 'env'" in (err + out):
                ec["data"], ec["error"] = None, "This PwnForge is older than the `pwnforge env` command: press Update to manage environments here."
            else:
                last = (err or out).strip().splitlines()
                ec["data"], ec["error"] = None, "pwnforge env list failed: %s" % redact(last[-1] if last else "exit %d" % rc)
        except (PFError, ValueError) as e:
            ec["data"], ec["error"] = None, str(e)
        finally:
            ec["at"] = time.time()
            ec["busy"] = False

    def usage(self, vms):
        """CPU and memory of each running VM's QEMU process (real VMs only; simulated ones have none)."""
        if self.demo:
            return {v["id"]: {"cpu": 3.5, "rss": 640 << 20} for v in vms if v["status"] == "running"}
        doms = {v.get("domain"): v["id"] for v in vms if v.get("domain") and v.get("driver") != "simulation"}
        if not doms:
            return {}
        out, now, hz = {}, time.time(), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().split(b"\0")
                if not cmd or b"qemu" not in os.path.basename(cmd[0]):
                    continue
                name = next((a.decode(errors="replace") for i, a in enumerate(cmd) if i and cmd[i - 1] == b"-name"), "")
                dom = name.split("guest=", 1)[1].split(",", 1)[0] if "guest=" in name else name.split(",", 1)[0]
                if dom not in doms:
                    continue
                with open("/proc/%s/stat" % pid, "r", encoding="utf-8") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                ticks = int(fields[11]) + int(fields[12])
                rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError):
                continue
            prev = self.cpu_prev.get(pid)
            self.cpu_prev[pid] = (ticks, now)
            cpu = round(100.0 * (ticks - prev[0]) / hz / (now - prev[1]), 1) if prev and now > prev[1] else None
            out[doms[dom]] = {"cpu": cpu, "rss": rss, "pid": int(pid)}
        return out

    def storage(self):
        if self.demo:
            return {"physical": {"data_dir": "/var/lib/pwnforge", "total_gb": 228.0, "used_gb": 61.2, "free_gb": 166.8, "low_space_warning": False},
                    "images": {"count": 4, "present": 1, "actual_gb": 2.1}, "overlays": {"actual_gb": 1.6}}
        try:
            rc, out, _ = self._cli(["storage", "--json"], 60)
            lines = [l for l in out.splitlines() if l.startswith("{")]
            return json.loads(lines[-1]) if rc == 0 and lines else None
        except (PFError, ValueError):
            return None

    # ---- the page ----------------------------------------------------------------------------------------------
    def virt_status(self):
        """Whether PwnForge can boot real machines, accelerated by KVM when it is available."""
        if self.demo:
            return {"linux": True, "kvm": True, "libvirt": True, "binding": True,
                    "qemu": True, "qemu_img": True, "ready": True}
        linux = platform.system() == "Linux"
        kvm = os.path.exists("/dev/kvm")
        libvirt = bool(shutil.which("virsh")) or os.path.exists("/usr/sbin/libvirtd")
        arch = platform.machine().lower()
        qemu_name = "qemu-system-aarch64" if arch in ("aarch64", "arm64") else "qemu-system-x86_64"
        qemu = bool(shutil.which(qemu_name))
        qemu_img = bool(shutil.which("qemu-img"))
        venv = os.path.join(self.cfg["path"], ".venv", "lib")
        binding = False
        try:
            for d in os.listdir(venv):
                if os.path.exists(os.path.join(venv, d, "site-packages", "libvirt.py")):
                    binding = True
        except OSError:
            pass
        return {"linux": linux, "kvm": kvm, "libvirt": libvirt, "binding": binding,
                "qemu": qemu, "qemu_img": qemu_img,
                "ready": linux and libvirt and binding and qemu and qemu_img}

    def view(self):
        problem = self.platform_problem()
        out = {"config": dict(self.cfg), "binds": BINDS, "problem": problem, "demo": self.demo, "repo": REPO, "job": self.job_view()}
        if problem:
            return out
        inst = self.installed()
        u = self.unit()
        out.update(installed=inst, unit=u, urls=self.urls(), virt=self.virt_status())
        if inst and u["active"] == "active":
            out["health"] = self.health()
        if inst:
            data, err = self.environments()
            if data is not None:
                use = self.usage(data.get("vms") or [])
                for v in data.get("vms") or []:
                    v["usage"] = use.get(v["id"])
            out.update(environments=data, environments_error=err, storage=self.storage() if self.demo else self.env_cache.get("storage"))
        return out

    def logs(self):
        if self.demo:
            return {"service": "Oct 18 09:00:01 pwnforge[4242]: Pwnforge serving at http://localhost:8080\nOct 18 09:00:02 pwnforge[4242]: INFO: Application startup complete.",
                    "job": self.job_log()}
        rc, out, err = self._run(["journalctl", "-u", UNIT, "-n", "200", "-o", "short-iso", "--no-pager", "-q"], 15)
        return {"service": redact(out if rc == 0 else err)[-60000:], "job": self.job_log()}

    # ---- install / doctor (a background job that outlives a dashboard restart) ---------------------------------
    def job_view(self):
        j = self.job
        if not j:
            return None
        if self.demo:
            done = time.time() - j["started"] > 6
            return {"what": j["what"], "running": not done, "result": "ok" if done else "", "started": j["started"]}
        rc, out, _ = self._run(["systemctl", "show", JOB, "-p", "ActiveState,Result"], 10)
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        running = kv.get("ActiveState") in ("active", "activating")
        log = self.job_log()
        result = "" if running else ("ok" if "pwnforge-host: OK" in log else "failed")
        return {"what": j["what"], "running": running, "result": result, "started": j["started"]}

    def job_log(self):
        j = self.job
        if not j:
            return ""
        if self.demo:
            steps = ["step 1/7: checking what this machine has", "step 2/7: service user", "step 3/7: code (%s)" % REPO, "step 4/7: Python packages",
                     "step 5/7: data folder and database", "step 6/7: website", "step 7/7: service", "OK"]
            n = min(len(steps), int((time.time() - j["started"]) / 0.8) + 1)
            return "\n".join("pwnforge-host: " + s for s in steps[:n])
        try:
            rc, out, _ = self._run(["journalctl", "-u", JOB, "--since", "@%d" % int(j["started"] - 2), "-o", "cat", "--no-pager", "-q"], 15)
        except PFError:
            return ""
        return redact(JOB_NOISE_RE.sub("", out))[-20000:]

    def start_job(self, what, argv):
        v = self.job_view()
        if v and v["running"]:
            raise PFError("PwnForge is busy with %s already." % v["what"], 409)
        self.job = {"what": what, "started": time.time()}
        if self.demo:
            if what == "install":
                self.fake["installed"] = True
            return
        rc, out, err = self._run(["systemd-run", "--quiet", "--collect", "--unit=" + JOB, "--description=nodeyard dashboard: PwnForge " + what,
                                  "-p", "WorkingDirectory=/", "--"] + argv, 20)
        if rc != 0:
            self.job = None
            raise PFError("Couldn't start the %s: %s" % (what, (err or out).strip()[:300]), 500)

    # ---- actions -----------------------------------------------------------------------------------------------
    def _systemctl(self, *args):
        if self.demo:
            f = self.fake
            if args[0] in ("start", "restart"):
                f["active"], f["since"] = "active", time.time()
            elif args[0] == "stop":
                f["active"] = "inactive"
            elif args[0] in ("enable", "disable"):
                f["enabled"] = args[0] == "enable"
            return
        rc, out, err = self._run(["systemctl"] + list(args) + [UNIT], 90)
        if rc != 0:
            raise PFError("systemctl %s failed: %s" % (args[0], redact((err or out).strip())[:400]), 500)

    def _configure(self):
        c = self.cfg
        if self.demo:
            return "pwnforge-host: OK"
        rc, out, err = self._root(["/bin/bash", HOST_SCRIPT, "configure", c["path"], c["data_dir"], self.bind_host(c["bind"]), str(c["port"])], 120)
        if rc != 0:
            raise PFError("Couldn't set PwnForge's address: %s" % redact((out + err).strip().splitlines()[-1] if (out + err).strip() else "exit %d" % rc), 500)
        return out

    def _https(self, action, port):
        if self.demo:
            return
        rc, out, err = self._root(["/bin/bash", HOST_SCRIPT, "https", action, str(port)], 90)
        if rc != 0:
            last = (out + err).strip().splitlines()
            raise PFError(redact(last[-1] if last else "exit %d" % rc).replace("pwnforge-host: FAILED: ", ""), 500)

    def update_step(self):
        """PwnForge's part of Settings → Update everything: a shell step for that root script (it runs after nodeyard
        itself is updated, so the newest pwnforge-host.sh does the work), or "" when PwnForge isn't installed here."""
        if self.demo or self.platform_problem() or not self.installed():
            return ""
        c = self.cfg
        try:
            host = self.bind_host(c["bind"])
        except PFError as e:
            return "echo '== PwnForge =='; echo %s; " % shlex.quote("skipped: " + str(e))
        argv = ["/bin/bash", HOST_SCRIPT, "install", c["path"], c["data_dir"], REPO, host, str(c["port"])]
        # restarted only if it was running, so the new code is what answers
        return "echo '== PwnForge =='; %s && systemctl try-restart %s; " % (" ".join(shlex.quote(a) for a in argv), UNIT)

    def action(self, body):
        a = str(body.get("action") or "")
        problem = self.platform_problem()
        if problem:
            raise PFError(problem, 501)
        if a == "install":
            c = self.cfg
            self.start_job("install", ["/bin/bash", HOST_SCRIPT, "install", c["path"], c["data_dir"], REPO, self.bind_host(c["bind"]), str(c["port"])])
            return {"started": True}
        if not self.installed():
            raise PFError("PwnForge isn't installed yet: press Install first.", 409)
        if a in ("lab-access-on", "lab-access-off", "lab-access-status"):
            try:
                network = ipaddress.ip_network(str(body.get("cidr") or "10.200.0.0/16"), strict=True)
            except ValueError:
                raise PFError("Enter a canonical private IPv4 network, such as 10.200.0.0/16.")
            if network.version != 4 or not network.is_private or not 8 <= network.prefixlen <= 30:
                raise PFError("Lab access only accepts private IPv4 networks with a prefix from /8 through /30.")
            cidr = str(network)
            if a == "lab-access-status":
                if self.demo:
                    return {"output": "cidr=%s\nipv4_forwarding=no\nfirewall=no\nboot_persistence=no\ntailnet_route_advertised=no\nlan_interface=demo\nlan_address=demo" % cidr, "exit": 0}
                rc, out, err = self._root(["/bin/bash", HOST_SCRIPT, "tailnet-access", "status", cidr], 20)
                return {"output": redact((out + ("\n" + err if err.strip() else "")).strip()), "exit": rc}
            if self.demo:
                return {}
            operation = "on" if a == "lab-access-on" else "off"
            self.start_job("lab IP access " + operation,
                           ["/bin/bash", HOST_SCRIPT, "tailnet-access", operation, cidr])
            return {"started": True}
        if a in ("start", "stop", "restart"):
            self._systemctl(a)
        elif a in ("enable", "disable"):
            self._systemctl(a)
            with self.lock:
                self.cfg["autostart"] = a == "enable"
                self._save()
        elif a == "hosting-on":
            self._configure()
            with self.lock:
                self.cfg["enabled"] = True
                self._save()
            if self.cfg["autostart"]:
                self._systemctl("enable")
            self._systemctl("start")
        elif a == "hosting-off":
            with self.lock:
                self.cfg["enabled"] = False
                self._save()
            self._systemctl("stop")
            self._systemctl("disable")
        elif a == "virt":
            c = self.cfg
            self.start_job("real machines setup", ["/bin/bash", HOST_SCRIPT, "virt", c["path"], c["data_dir"], self.bind_host(c["bind"]), str(c["port"])])
            return {"started": True}
        elif a == "doctor":
            if self.demo:
                return {"output": "Host\n  driver: simulation\nCatalogue courses=89 profiles=1440\ndoctor: OK", "exit": 0}
            rc, out, err = self._cli(["doctor"], 120)
            return {"output": redact((out + ("\n" + err if err.strip() else "")).strip())[-20000:], "exit": rc}
        else:
            raise PFError("Unknown action.")
        return {}

    def set_config(self, body):
        bind = str(body.get("bind") or self.cfg["bind"])
        if bind not in BINDS:
            raise PFError("Pick this machine only, your tailnet (with or without HTTPS) or every network.")
        try:
            port = int(body.get("port", self.cfg["port"]))
        except (TypeError, ValueError):
            raise PFError("The port must be a number.")
        if not 1024 <= port <= 65535:
            raise PFError("Pick a port from 1024 to 65535 (PwnForge doesn't run as root, so it can't use lower ones).")
        if port == self.dash_port:
            raise PFError("That's the dashboard's own port.")
        u = self.unit() if not self.platform_problem() else {"active": "inactive"}
        if port != self.cfg["port"] or u["active"] != "active":
            found = None if self.demo else read_dropin()
            ours = found is not None and found["port"] == port and answers_as_pwnforge(found["host"], port)
            if not self.demo and not ours and port_in_use(port):
                raise PFError("Something on this machine already listens on port %d. Pick another." % port, 409)
        if bind == "lan":
            if body.get("confirm_lan") is not True:
                raise PFError("Opening PwnForge to every network needs your explicit OK.", 428)
            h = self.health() if u["active"] == "active" else {"ok": False}
            if not h.get("ok") or h.get("initialised") is not True:
                raise PFError("Start PwnForge and create its administrator account first (on this machine or over the tailnet); "
                              "until then anyone on the network could claim it.", 409)
        if bind == "https":
            if port in SERVE_KEPT:
                raise PFError("Port %d is kept for the dashboard's own https:// address and public access: pick another." % port)
            if not self.demo and not tailscale_name():
                raise PFError("HTTPS needs Tailscale with MagicDNS on this machine (https://login.tailscale.com/admin/dns).", 409)
        self.bind_host(bind)   # (the tailnet needs a Tailscale address)
        old = dict(self.cfg)
        with self.lock:
            self.cfg.update(bind=bind, port=port)
            self._save()
        if self.installed() and not self.platform_problem():
            try:
                out = self._configure()
                if bind == "https":
                    self._https("on", port)
            except PFError:
                with self.lock:
                    self.cfg = old
                    self._save()
                try:
                    self._configure()          # back to the address it had
                except PFError:
                    pass
                raise
            if old["bind"] == "https" and (bind != "https" or old["port"] != port):
                try:
                    self._https("off", old["port"])
                except PFError:
                    pass
            return {"applied": True, "output": out[-2000:]}
        return {"applied": False}

    def env_action(self, body):
        a = str(body.get("action") or "")
        if not self.installed():
            raise PFError("PwnForge isn't installed.", 409)
        if a == "cleanup":
            apply = body.get("apply") is True
            if self.demo:
                return {"result": {"dry_run": not apply, "candidates": [] if apply else [{"path": "/var/lib/pwnforge/overlays/old-1.qcow2", "reason": "no live instance references this overlay", "size_bytes": 220 << 20}],
                                   "removed": ["/var/lib/pwnforge/overlays/old-1.qcow2"] if apply else [], "freed_bytes": (220 << 20) if apply else 0}}
            rc, out, err = self._cli(["env", "cleanup"] + (["--apply"] if apply else []), 120)
            lines = [l for l in out.splitlines() if l.startswith("{")]
            if rc != 0 or not lines:
                raise PFError("Cleanup failed: %s" % redact((err or out).strip()[-300:]), 500)
            self.env_cache["at"] = 0
            return {"result": json.loads(lines[-1])}
        kind, ident = str(body.get("kind") or ""), str(body.get("id") or "")
        if a not in ("start", "stop", "delete") or kind not in ("vm", "lab") or not ID_RE.match(ident):
            raise PFError("Say start, stop or delete, vm or lab, and which one.")
        if a == "delete" and body.get("confirm") is not True:
            raise PFError("Deleting removes its disk; confirm first.", 428)
        if self.demo:
            for v in self.fake["vms" if kind == "vm" else "labs"]:
                if v["id"] == ident:
                    if a == "delete":
                        self.fake["vms" if kind == "vm" else "labs"].remove(v)
                    else:
                        v["status"] = "running" if a == "start" else "stopped"
                    return {}
            raise PFError("No such %s." % kind, 404)
        if a == "start":   # (waits for the machine to boot: a job, not a page request)
            self.start_job("start %s" % kind, ["runuser", "-u", SVC_USER, "--", "env", "PWNFORGE_DATA_DIR=" + self.cfg["data_dir"],
                                               os.path.join(self.cfg["path"], ".venv", "bin", "pwnforge"), "env", "start", kind, ident])
            self.env_cache["at"] = 0
            return {"started": True}
        rc, out, err = self._cli(["env", a, kind, ident], 180)
        self.env_cache["at"] = 0
        if rc != 0:
            raise PFError("pwnforge env %s failed: %s" % (a, redact((err or out).strip()[-300:])), 500)
        return {}


def register(ctx, args):
    pf = PwnForge(ctx, args)
    ctx.pwnforge = pf

    def wrap(fn, privileged=False):
        def route(h, arg):
            if privileged and h._forwarded() is not None:
                return h._json({"ok": False, "error": "PwnForge is managed over Tailscale or your own network only, not through public access."}, 403)
            try:
                r = fn(h, arg)
            except PFError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            except Exception as e:  # noqa: BLE001 -- this page is optional: it must never take the dashboard down
                sys.stderr.write("pwnforge: %r\n" % e)
                return h._json({"ok": False, "error": "PwnForge page error: %s" % e}, 500)
            return h._json(dict(r or {}, ok=True))
        return route

    ctx.get_routes["/api/pwnforge"] = wrap(lambda h, q: pf.view())
    ctx.get_routes["/api/pwnforge/logs"] = wrap(lambda h, q: pf.logs(), True)
    ctx.post_routes.update({
        "/api/pwnforge/action": wrap(lambda h, b: pf.action(b), True),
        "/api/pwnforge/config": wrap(lambda h, b: pf.set_config(b), True),
        "/api/pwnforge/env": wrap(lambda h, b: pf.env_action(b), True),
    })
