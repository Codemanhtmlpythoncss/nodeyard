"""Maintenance: outdated software and drivers on every machine, and what to keep updated automatically.

For each machine the dashboard runs a small read-only script on the host (through nodeexec, the same way
Hardware care does). It refreshes the package list and asks the package manager what it could upgrade, reads
the running NVIDIA driver, and sorts what comes back into drivers / kernel / security / other. Nothing is
installed by looking.

You can turn on automatic updates per machine for the groups you choose. Turning it on needs you to confirm
after seeing exactly which packages would be upgraded and the command that runs; the dashboard then upgrades
just those packages (`apt-get install --only-upgrade <names>`, never a blanket dist-upgrade, so nothing is
removed) at most once a day. Unticking a group stops it, and changing the groups pauses it until you confirm
again. A machine never updates on its own until you turn it on.

Routes
  GET  /api/updates                     every machine: versions it knows, outdated groups, auto settings, last run
  POST /api/updates/check   {node|"all"}  refresh one (or every ready) machine now, in the background
  POST /api/updates/plan    {node, groups}            the exact command and the packages it would upgrade
  POST /api/updates/apply   {node, groups, confirm}   upgrade those groups now (confirm required)
  POST /api/updates/auto    {node, groups, confirm}   keep these groups updated automatically (enabling needs confirm)
"""
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time

import nodeexec

GROUPS = ("drivers", "kernel", "security", "other")   # the order they are offered in, and the priority a package falls into
AUTO_INTERVAL = 24 * 3600        # an automatic machine is upgraded at most this often
CHECK_EVERY = 3600               # the background loop wakes this often to see whether an automatic machine is due
PROBE_FRESH = 15 * 60            # a probe older than this is refreshed before an automatic upgrade

# A driver / firmware / microcode package, by name. Kept broad on purpose: the user asked to keep "drivers and stuff" current.
DRIVER_RE = re.compile(r"^(nvidia-|libnvidia|xserver-xorg-video-|.*-dkms$|firmware-|linux-firmware|amdgpu|libdrm|mesa|"
                       r"intel-microcode|amd64-microcode|bolt|fwupd|.*-firmware$)", re.I)
KERNEL_RE = re.compile(r"^(linux-image|linux-headers|linux-kbuild|raspberrypi-kernel|rpi-eeprom)", re.I)

# `apt-get --just-print full-upgrade` prints one "Inst" line per upgrade; LANG=C keeps it parseable. The probe turns
# each one into `pkg=<name> [<current>] (<candidate> <origin...> [<arch>])`, which this matches.
PKG_RE = re.compile(r"^(\S+) \[([^\]]*)\] \(([^) ]+) ([^)]*)\)")

PROBE = r"""
set +e
echo "os=$(. /etc/os-release 2>/dev/null; printf '%s' "$PRETTY_NAME")"
echo "kernel=$(uname -r)"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
fi
if command -v apt-get >/dev/null 2>&1; then
  echo "pm=apt"
  apt-get update -qq >/dev/null 2>&1 || echo "refresh=failed"
  LANG=C apt-get --just-print full-upgrade 2>/dev/null | sed -n 's/^Inst /pkg=/p'
else
  echo "pm=none"
fi
echo "probe-done"
"""


class UpdatesError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def classify(name, origin):
    """Which single group a package falls into. Drivers win over kernel over security over everything else, so that
    "keep drivers current" catches a driver's security update too."""
    if DRIVER_RE.match(name):
        return "drivers"
    if KERNEL_RE.match(name):
        return "kernel"
    if "-security" in (origin or "").lower():
        return "security"
    return "other"


def parse_probe(text):
    """Turn the probe's output into {os, kernel, nvidia, pm, refresh_failed, packages:[...]}.
    Each package is {name, current, candidate, origin, group, security}."""
    out = {"os": "", "kernel": "", "nvidia": "", "pm": "", "refresh_failed": False, "packages": [], "done": False}
    for line in (text or "").splitlines():
        line = line.rstrip()
        if line == "probe-done":
            out["done"] = True
        elif line == "refresh=failed":
            out["refresh_failed"] = True
        elif line.startswith(("os=", "kernel=", "nvidia=", "pm=")):
            k, v = line.split("=", 1)
            out[k] = v.strip()
        elif line.startswith("pkg="):
            m = PKG_RE.match(line[4:])
            if not m:
                continue
            name, current, candidate, origin = m.group(1), m.group(2), m.group(3), m.group(4)
            name = name.split(":", 1)[0]   # drop an :arch suffix like libc6:amd64
            out["packages"].append({"name": name, "current": current, "candidate": candidate, "origin": origin,
                                    "group": classify(name, origin), "security": "-security" in origin.lower()})
    # one row per package name (the simulation can mention a name twice); keep the first
    seen, packages = set(), []
    for p in out["packages"]:
        if p["name"] not in seen:
            seen.add(p["name"])
            packages.append(p)
    out["packages"] = packages
    return out


def groups_hash(groups):
    return hashlib.sha256(",".join(sorted(groups)).encode()).hexdigest()[:16]


def clean_groups(groups):
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple)):
        raise UpdatesError("Choose which groups to update (drivers, kernel, security, other).")
    picked = ["all"] if "all" in groups else [g for g in GROUPS if g in groups]
    if "all" in groups:
        return list(GROUPS)
    return picked


class Updates:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.probes = {}
        sd = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        self.path = os.path.join(sd, "updates.json")
        self.auto = {}          # node -> {"groups": [...], "reviewed": hash, "last": ts|None, "last_result": str}
        self._read()

    # ---- state ----------------------------------------------------------------------------------------------
    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            auto = raw.get("auto") if isinstance(raw, dict) else None
            if isinstance(auto, dict):
                for node, rec in auto.items():
                    if isinstance(rec, dict) and rec.get("groups"):
                        self.auto[node] = {"groups": clean_groups(rec.get("groups")), "reviewed": rec.get("reviewed", ""),
                                           "last": rec.get("last"), "last_result": rec.get("last_result", "")}
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"auto": self.auto}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            raise UpdatesError("Couldn't save the update settings: %s" % (e.strerror or e), 500)

    # ---- the cluster ----------------------------------------------------------------------------------------
    def _nodes(self):
        try:
            return (self.ctx.store.snapshot().get("state") or {}).get("nodes") or []
        except Exception:   # noqa: BLE001 -- the page still works from the cache and the saved auto settings
            return []

    def _node(self, name):
        n = next((x for x in self._nodes() if x.get("name") == name), None)
        if n is None and name in self.auto:
            n = {"name": name}
        if n is None:
            raise UpdatesError("No such machine.", 404)
        return n

    def _exec(self, node, script, deadline=120):
        if self.demo:
            return demo_exec(node, script)
        try:
            return nodeexec.run(self.kubeconfig, node, script, host=True, deadline=deadline, kind="updates")
        except nodeexec.NodeExecError as e:
            raise UpdatesError(str(e), 502)

    def probe(self, node):
        self._node(node)
        with self.lock:
            slot = self.probes.setdefault(node, {})
            if slot.get("busy"):
                return
            slot["busy"] = True
        try:
            data = parse_probe(self._exec(node, PROBE))
            with self.lock:
                self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        except UpdatesError as e:
            with self.lock:
                self.probes[node] = dict(self.probes.get(node) or {}, error=str(e), busy=False)

    def _probe_of(self, node, max_age=None):
        with self.lock:
            pr = copy.deepcopy(self.probes.get(node) or {})
        if not pr.get("data"):
            raise UpdatesError("Check this machine first.", 409)
        if max_age and time.time() - pr["at"] > max_age:
            self.probe(node)
            return self._probe_of(node)
        return pr["data"]

    # ---- reading --------------------------------------------------------------------------------------------
    @staticmethod
    def _grouped(packages):
        out = {g: [] for g in GROUPS}
        for p in packages:
            out[p["group"]].append({"name": p["name"], "current": p["current"], "candidate": p["candidate"], "security": p["security"]})
        return out

    def _node_view(self, n):
        name = n.get("name")
        with self.lock:
            pr = copy.deepcopy(self.probes.get(name) or {})
        data = pr.get("data") or {}
        auto = self.auto.get(name)
        grouped = self._grouped(data.get("packages") or [])
        row = {
            "node": name, "ready": bool(n.get("ready")),
            "os": data.get("os") or n.get("os") or "", "kernel": data.get("kernel") or n.get("kernel") or "",
            "kubelet": n.get("kubelet") or "", "runtime": n.get("runtime") or "",
            "driver": data.get("nvidia") or "",
            "pm": data.get("pm", ""), "refresh_failed": bool(data.get("refresh_failed")),
            "probe_at": pr.get("at"), "probe_error": pr.get("error", ""), "probing": bool(pr.get("busy")),
            "checked": bool(data),
            "groups": grouped, "counts": {g: len(grouped[g]) for g in GROUPS}, "total": sum(len(v) for v in grouped.values()),
        }
        if auto:
            row["auto"] = auto["groups"]
            row["auto_paused"] = auto.get("reviewed", "") != groups_hash(auto["groups"])
            row["auto_last"] = auto.get("last")
            row["auto_last_result"] = auto.get("last_result", "")
        else:
            row["auto"] = []
            row["auto_paused"] = False
        return row

    def view(self):
        nodes = self._nodes()
        names = [n.get("name") for n in nodes]
        rows = [self._node_view(n) for n in nodes]
        for name in self.auto:   # a machine that has automatic updates set but is missing from the snapshot right now
            if name not in names:
                rows.append(self._node_view({"name": name, "ready": False}))
        return {"nodes": rows, "groups": list(GROUPS), "demo": self.demo, "auto_interval_h": AUTO_INTERVAL // 3600}

    # ---- planning and applying ------------------------------------------------------------------------------
    def _packages_for(self, data, groups):
        groups = set(clean_groups(groups))
        return [p["name"] for p in (data.get("packages") or []) if p["group"] in groups]

    def plan(self, node, groups, data=None):
        """(says, script, names): what upgrading these groups on this machine would do. No change is made."""
        self._node(node)
        groups = clean_groups(groups)
        if not groups:
            raise UpdatesError("Choose at least one group to update.")
        if data is None:
            data = self._probe_of(node, max_age=PROBE_FRESH)
        names = self._packages_for(data, groups)
        label = ", ".join(groups)
        if not names:
            return (["Nothing in %s is out of date on %s right now." % (label, node)], "", [])
        script = "apt-get install -y --only-upgrade " + " ".join(names)
        says = ["Upgrade %d package%s on %s (%s): %s." % (len(names), "" if len(names) == 1 else "s", node, label, ", ".join(names)),
                "Only these named packages change — nothing is removed and no new kernel metapackage is pulled in.",
                "Runs: %s" % script]
        return (says, script, names)

    def apply(self, body, auto=False):
        node = str(body.get("node") or "")
        groups = clean_groups(body.get("groups"))
        says, script, names = self.plan(node, groups)
        if not script:
            return {"done": says}
        if not auto and body.get("confirm") is not True:
            return {"plan": says, "script": script, "names": names, "hash": groups_hash(groups)}
        if not self.run_lock.acquire(blocking=False):
            raise UpdatesError("An update is already running.", 409)
        try:
            out = self._exec(node, script + "\n" + PROBE, deadline=900)
        finally:
            self.run_lock.release()
        data = parse_probe(out)
        with self.lock:
            self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
            if node in self.auto:
                self.auto[node]["last"] = time.time()
                self.auto[node]["last_result"] = "upgraded %d" % len(names)
                self._save()
        left = self._packages_for(data, groups)
        done = says[:1] + (["%d still listed as upgradable (a held package, or it needs a reboot)." % len(left)] if left else ["Done — those packages are up to date now."])
        return {"done": done}

    def set_auto(self, body):
        node = str(body.get("node") or "")
        self._node(node)
        groups = clean_groups(body.get("groups"))
        want = groups_hash(groups)
        if groups and (body.get("confirm") is not True or body.get("hash") != want):
            raise UpdatesError("Review which packages would be upgraded, then confirm to keep %s updated automatically on %s." % (", ".join(groups), node), 428)
        with self.lock:   # view() takes this lock too, so release it before calling view() below (the lock isn't reentrant)
            if not groups:
                self.auto.pop(node, None)
            else:
                prev = self.auto.get(node) or {}
                self.auto[node] = {"groups": groups, "reviewed": want, "last": prev.get("last"), "last_result": prev.get("last_result", "")}
            self._save()
        return self.view()

    # ---- the automatic loop ---------------------------------------------------------------------------------
    def due(self, node, now=None):
        """Whether an automatic machine is ready for another upgrade (groups set, confirmed, and enough time passed)."""
        now = time.time() if now is None else now
        rec = self.auto.get(node)
        if not rec or not rec.get("groups"):
            return False
        if rec.get("reviewed", "") != groups_hash(rec["groups"]):   # paused: the groups changed and weren't re-confirmed
            return False
        last = rec.get("last")
        return last is None or now - last >= AUTO_INTERVAL

    def run_auto(self, now=None):
        """Upgrade every automatic machine that is due. Each one is probed fresh first so we act on current data."""
        done = []
        for node in list(self.auto):
            if not self.due(node, now):
                continue
            try:
                self.probe(node)
                r = self.apply({"node": node, "groups": self.auto[node]["groups"]}, auto=True)
                done.append((node, r))
            except UpdatesError as e:
                with self.lock:
                    if node in self.auto:
                        self.auto[node]["last"] = time.time()
                        self.auto[node]["last_result"] = "failed: %s" % e
                        self._save()
        return done

    def loop(self):
        while True:
            try:
                self.run_auto()
            except Exception as e:   # noqa: BLE001 -- a background thread must never die
                sys.stderr.write("updates: automatic run failed (%s)\n" % e)
            time.sleep(CHECK_EVERY)


def demo_exec(node, script):
    """A couple of machines with something to update, so the page (and the tests) work without a cluster."""
    if "apt-get" not in script and "nvidia-smi" not in script and "os-release" not in script:
        return "probe-done\n"
    i = int(re.sub(r"\D", "", str(node)) or 0)
    upgraded = "install -y --only-upgrade" in script
    lines = ["os=Debian GNU/Linux 12 (bookworm)", "kernel=6.1.0-18-%s" % ("arm64" if i in (1, 2) else "amd64"), "pm=apt"]
    if i == 1:   # a GPU box: a driver update and a couple of ordinary ones
        lines.insert(2, "nvidia=535.161.08")
        if not upgraded:
            lines += ["pkg=nvidia-driver [535.161.08-1] (535.183.01-1 Debian:bookworm-updates [amd64])",
                      "pkg=libnvidia-compute-535 [535.161.08-1] (535.183.01-1 Debian:bookworm-updates [amd64])",
                      "pkg=linux-image-amd64 [6.1.90-1] (6.1.99-1 Debian:bookworm-security [amd64])",
                      "pkg=libc6 [2.36-9] (2.36-9+deb12u7 Debian:bookworm-security [amd64])",
                      "pkg=curl [7.88.1-10] (7.88.1-10+deb12u8 Debian:bookworm [amd64])"]
    elif i == 2:   # only a security update outstanding
        if not upgraded:
            lines += ["pkg=openssl [3.0.11-1] (3.0.14-1~deb12u2 Debian:bookworm-security [arm64])"]
    return "\n".join(lines) + "\nprobe-done\n"


def register(ctx, args):
    u = Updates(ctx, args)
    ctx.updates = u

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(h, arg)
            except UpdatesError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    def check(h, b):
        node = str(b.get("node") or "")
        names = [n.get("name") for n in u._nodes() if n.get("ready")] if node == "all" else [u._node(node)["name"]]
        for name in names:
            threading.Thread(target=u.probe, args=(name,), daemon=True).start()
        time.sleep(0.05)
        return u.view()

    def plan(h, b):
        says, script, names = u.plan(str(b.get("node") or ""), clean_groups(b.get("groups")))
        return {"plan": says, "script": script, "names": names, "hash": groups_hash(clean_groups(b.get("groups")))}

    ctx.get_routes["/api/updates"] = wrap(lambda h, q: u.view())
    ctx.post_routes.update({
        "/api/updates/check": wrap(check),
        "/api/updates/plan": wrap(plan),
        "/api/updates/apply": wrap(lambda h, b: u.apply(b)),
        "/api/updates/auto": wrap(lambda h, b: u.set_auto(b)),
    })

    if not u.demo:
        threading.Thread(target=u.loop, daemon=True).start()
    return u
