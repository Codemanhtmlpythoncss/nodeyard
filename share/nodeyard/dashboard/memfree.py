"""Free up memory: the processes over 5 MB on each node that a cluster node doesn't need, moved to swap or stopped.

The list comes from the node agents (agents.py). Each process is sorted into "not needed" (desktop, printing,
Bluetooth, update checkers... on a cluster node), "your call" (anything else) or "needed" (Kubernetes, the AI model,
networking, SSH, nodeyard: never offered). Nothing changes until the page has shown exactly what will run and you've
pressed Run; then it runs as root on that node through a short-lived helper pod (nodeexec.py), and the audit log has it.

  Move to swap  the C node agent's `pageout` (process_madvise MADV_PAGEOUT: only that process; it comes back into RAM
                by itself when used), else the kernel's reclaim for its cgroup. Needs swap: "Set up swap" adds zram
                (compressed RAM, best for SD cards) or a swap file.
  Stop          its systemd service (optionally kept off after reboots, and "Start again" undoes it), or ends the
                process when no service owns it. Kubernetes pods aren't stopped here (Kubernetes would start them again).

  GET  /api/memory                       {nodes: [{node, swap, procs: [...]}], stopped: [...]}
  POST /api/memory/action  {node, pid, start, action: swap|stop, keep_off, confirm}
  POST /api/memory/swap    {node, kind: zram|file, gib, confirm}
  POST /api/memory/undo    {node, unit, confirm}
"""
import json
import os
import re
import threading
import time

import nodeexec

MIN_RSS = 5 * 1024 * 1024
AGENT_NS = "nodeyard-system"
BIN_CM = ("nodeyard-agent-bin", "/agent-bin")
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,120}\.service$")

# Never offered, by process name (exact, or a prefix ending in *; the kernel cuts names to 15 characters) -- what keeps the node, the cluster and the model up.
NEEDED = {
    "systemd*": "part of systemd (the system's core)", "init": "the system's init", "dbus-daemon": "system messages", "dbus-broker*": "system messages",
    "k3s*": "Kubernetes", "containerd*": "Kubernetes containers", "kubelet": "Kubernetes", "runc": "Kubernetes containers",
    "sshd*": "SSH", "ssh": "SSH", "tailscaled": "Tailscale", "NetworkManager": "networking", "wpa_supplicant": "Wi-Fi", "iwd": "Wi-Fi",
    "dhcpcd": "networking", "dhclient": "networking", "chronyd": "the clock", "ntpd": "the clock", "llama-server": "the AI model",
    "rpc-server": "the AI model", "nodeyard-agent*": "nodeyard", "agetty": "the console", "login": "the console",
    "cron": "scheduled jobs", "crond": "scheduled jobs", "polkitd": "admin permissions", "thermald": "overheating protection",
    "irqbalance": "spreading interrupts across cores", "flanneld": "the cluster network", "coredns": "cluster DNS",
    "kube-*": "Kubernetes", "traefik": "Kubernetes ingress", "etcd": "Kubernetes datastore", "rsyslogd": "the system log",
    "sudo": "an admin command", "su": "an admin command", "nsenter": "a nodeyard helper", "pause": "Kubernetes",
}
# Not needed on a cluster node, by name: what it is.
NOT_NEEDED = {
    "Xorg": "the graphical desktop's display server", "Xwayland": "the graphical desktop", "gnome-shell": "the GNOME desktop",
    "gnome-session*": "the GNOME desktop", "gdm*": "the desktop login screen", "lightdm": "the desktop login screen",
    "sddm": "the desktop login screen", "plasmashell": "the KDE desktop", "kwin_*": "the KDE desktop", "ksmserver": "the KDE desktop",
    "xfce4-*": "the Xfce desktop", "xfwm4": "the Xfce desktop", "mutter*": "the desktop", "gsd-*": "GNOME desktop settings",
    "gnome-software": "the desktop app store", "nautilus": "the desktop file manager", "evolution-*": "the desktop's mail and calendar",
    "evolution": "the desktop's mail and calendar", "tracker-miner*": "desktop file indexing", "tracker-extract*": "desktop file indexing",
    "localsearch*": "desktop file indexing", "goa-*": "desktop online accounts", "at-spi*": "desktop accessibility",
    "ibus-*": "desktop keyboard input", "ibus-daemon": "desktop keyboard input", "gvfs*": "desktop file sharing", "xdg-*": "desktop portals",
    "dconf-service": "desktop settings", "update-notifier": "the desktop update checker", "gnome-keyring-d*": "the desktop keyring",
    "gnome-terminal-*": "the desktop terminal", "pulseaudio": "desktop sound", "pipewire*": "desktop sound", "wireplumber": "desktop sound",
    "colord": "printer and screen colour profiles", "cupsd": "printing", "cups-browsed": "printing", "avahi-daemon": "network device discovery (Bonjour)",
    "ModemManager": "mobile-broadband modems", "bluetoothd": "Bluetooth", "snapd": "Snap packages", "packagekitd": "the desktop package updater",
    "fwupd": "firmware updates", "unattended-upgr*": "automatic package upgrades (run them yourself instead)", "whoopsie": "crash reporting",
    "kerneloops": "crash reporting", "apport*": "crash reporting", "accounts-daemon": "desktop user accounts", "rtkit-daemon": "desktop sound priority",
    "udisksd": "desktop disk mounting", "upowerd": "desktop battery reporting", "power-profiles-*": "desktop power profiles",
    "switcheroo-cont*": "desktop GPU switching", "geoclue": "desktop location", "firefox*": "a web browser", "chrome": "a web browser",
    "chromium*": "a web browser", "code": "VS Code", "thunderbird*": "a mail app", "multipathd": "SAN disk paths (unused without a SAN)",
    "iscsid": "network disks (unused without iSCSI)", "speech-dispatch*": "desktop text-to-speech", "cups-pk-helper": "printing",
    "gnome-calendar": "the desktop calendar", "gnome-calculator": "the desktop calculator", "snap-store": "the desktop app store",
    "sd_espeak-ng*": "desktop text-to-speech", "mpd": "a music player daemon", "minidlnad": "a media server you may not use",
}
# Namespaces whose pods are part of the cluster or nodeyard itself.
SYSTEM_NS = ("kube-system", "ai-split", "nodeyard-system", "nodeyard-shell", "kube-public", "kube-node-lease")


def _match(table, name):
    if name in table:
        return table[name]
    for k, v in table.items():
        if k.endswith("*") and name.startswith(k[:-1]):
            return v
    return None


def classify(p, me_dashboard=False):
    """{"verdict": needed|not-needed|maybe, "what", "can_swap", "can_stop", "unit"} for one agent process row."""
    g = p.get("group") or {}
    name, cmd = str(p.get("name") or ""), str(p.get("cmd") or "")
    unit = g.get("name") if g.get("kind") == "service" and UNIT_RE.match(str(g.get("name") or "")) else ""
    if g.get("kind") == "kernel" or p.get("pid") in (1, 2):
        return {"verdict": "needed", "what": "the kernel" if g.get("kind") == "kernel" else "the system's init", "can_swap": False, "can_stop": False, "unit": ""}
    if g.get("kind") == "pod":
        pod = str(g.get("name") or "")
        if pod.split("/")[0] in SYSTEM_NS or pod.startswith("pod "):
            return {"verdict": "needed", "what": "part of Kubernetes or nodeyard (%s)" % pod, "can_swap": False, "can_stop": False, "unit": ""}
        return {"verdict": "maybe", "what": "a Kubernetes app (%s): scale it down on the Workloads page if you don't need it" % pod,
                "can_swap": False, "can_stop": False, "unit": ""}
    if "nodeyard" in cmd and ("server.py" in cmd or "dashboard" in cmd):
        return {"verdict": "needed", "what": "this dashboard", "can_swap": False, "can_stop": False, "unit": ""}
    why = _match(NEEDED, name)
    if why or (unit and _match(NEEDED, unit[:-8])):
        return {"verdict": "needed", "what": why or _match(NEEDED, unit[:-8]), "can_swap": False, "can_stop": False, "unit": ""}
    why = _match(NOT_NEEDED, name) or (unit and _match(NOT_NEEDED, unit[:-8].split("@")[0]))
    if why:
        return {"verdict": "not-needed", "what": why, "can_swap": True, "can_stop": True, "unit": unit if not unit.startswith("user@") else ""}
    return {"verdict": "maybe", "what": ("the %s service" % unit) if unit else "not sure: check what it is before stopping it",
            "can_swap": True, "can_stop": True, "unit": unit if not unit.startswith("user@") else ""}


# -- the scripts ---------------------------------------------------------------------------------------------------

CHECK = r'''pid=%(pid)d; want=%(start)s
[ -r /proc/$pid/stat ] || { echo "error=that process has already ended"; exit 0; }
s=$(cat /proc/$pid/stat); s=${s##*) }; set -- $s
if [ -n "$want" ] && [ "${20}" != "$want" ]; then echo "error=that process has ended (another one has its number now)"; exit 0; fi
mem() { awk '/^(VmRSS|VmSwap):/ {printf "%%s=%%d\n", $1, $2 * 1024}' /proc/$pid/status 2>/dev/null | tr -d ':'; }
'''

# with the C agent's pageout (helper pod with hostPID and the agent programs mounted), else the cgroup's reclaim
SWAP = CHECK + r'''case "$(uname -m)" in x86_64) a=amd64 ;; aarch64) a=arm64 ;; arm*) a=arm ;; *) a=none ;; esac
if [ -x /agent-bin/$a ]; then out=$(/agent-bin/$a pageout $pid $want); rc=$?; [ $rc = 3 ] || { echo "$out"; exit 0; }; fi
# (no C agent here, or an old kernel: ask the kernel to reclaim that much from the process's own cgroup)
mem | sed 's/^/before_/'
anon=$(awk '/^RssAnon:/ {print $2 * 1024}' /proc/$pid/status)
nsenter -t 1 -m -- sh -c 'pid=$0; anon=$1
  d=$(find /sys/fs/cgroup -name cgroup.procs -exec grep -lx "$pid" {} + 2>/dev/null | head -1); d=${d%%/cgroup.procs}
  [ -n "$d" ] && [ -w "$d/memory.reclaim" ] || { echo "error=this node can'"'"'t move one program'"'"'s memory to swap (needs Linux 5.19 or the C node agent)"; exit 0; }
  echo "$anon swappiness=200" > "$d/memory.reclaim" 2>/dev/null || echo "$anon" > "$d/memory.reclaim" 2>/dev/null ||
    echo "$((anon / 2))" > "$d/memory.reclaim" 2>/dev/null; echo "cgroup=$d"' "$pid" "$anon"
sleep 0.3; mem | sed 's/^/after_/'; echo ok=1
'''

STOP_UNIT = r'''u='%(unit)s'
nsenter -t 1 -m -u -i -n -p -- sh -c 'systemctl stop "$0" && echo stopped=1; [ "$1" = 1 ] && systemctl disable "$0" >/dev/null 2>&1 && echo disabled=1;
  systemctl is-active "$0" >/dev/null 2>&1 && echo "error=it is still running (something started it again)"; true' "$u" %(off)d
'''

STOP_PID = CHECK + r'''kill -TERM $pid 2>/dev/null || { echo "error=couldn't end it"; exit 0; }
i=0; while [ -e /proc/$pid ] && [ $i -lt 10 ]; do sleep 0.5; i=$((i + 1)); done
if [ -e /proc/$pid ]; then kill -KILL $pid 2>/dev/null; sleep 0.5; fi
[ -e /proc/$pid ] && echo "error=it didn't end" || echo stopped=1
'''

START_UNIT = r'''nsenter -t 1 -m -u -i -n -p -- sh -c 'systemctl enable "$0" >/dev/null 2>&1; systemctl start "$0" && echo started=1' '%(unit)s'
'''

ZRAM = r'''modprobe zram 2>/dev/null; d=/sys/block/zram0
[ -e $d ] || { echo "error=this kernel has no zram"; exit 0; }
grep -q '^/dev/zram' /proc/swaps && { echo "error=zram swap is already on"; exit 0; }
cat > /etc/nodeyard-zram.sh <<'NYZ'
#!/bin/sh
# compressed swap in RAM, set up from the nodeyard dashboard (Processes > Free up memory)
modprobe zram 2>/dev/null; d=/sys/block/zram0
swapoff /dev/zram0 2>/dev/null; echo 1 > $d/reset 2>/dev/null
for alg in zstd lz4 lzo-rle lzo; do grep -qw "$alg" $d/comp_algorithm && { echo $alg > $d/comp_algorithm; break; }; done
echo %(mib)dM > $d/disksize && mkswap /dev/zram0 >/dev/null && swapon -p 100 /dev/zram0
NYZ
chmod 755 /etc/nodeyard-zram.sh
cat > /etc/systemd/system/nodeyard-zram.service <<'NYZ'
[Unit]
Description=nodeyard compressed swap (zram)
After=local-fs.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /etc/nodeyard-zram.sh
ExecStop=/sbin/swapoff /dev/zram0
[Install]
WantedBy=multi-user.target
NYZ
systemctl daemon-reload; systemctl enable --now nodeyard-zram.service >/dev/null 2>&1
grep -q '^/dev/zram0' /proc/swaps && echo ok=1 || echo "error=zram didn't turn on"
'''

SWAPFILE = r'''f=/swap.nodeyard
[ -e $f ] && { echo "error=$f already exists"; exit 0; }
free=$(df -Pk / | awk 'NR == 2 {print $4}')
[ "$free" -gt $(( (%(mib)d + 2048) * 1024 )) ] || { echo "error=not enough free disk on / (needs %(gib)d GB plus 2 GB to spare)"; exit 0; }
: > $f; chmod 600 $f; chattr +C $f 2>/dev/null
fallocate -l %(mib)dM $f 2>/dev/null || dd if=/dev/zero of=$f bs=1M count=%(mib)d 2>/dev/null
mkswap $f >/dev/null && swapon -p 10 $f || { rm -f $f; echo "error=the swap file didn't turn on"; exit 0; }
grep -q "^$f " /etc/fstab || echo "$f none swap sw,pri=10 0 0" >> /etc/fstab
echo ok=1
'''


def _kv(text):
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and re.match(r"^[A-Za-z_]+$", k):
            out[k] = v.strip()
    return out


def _mb(n):
    return "%d MB" % round((n or 0) / 1048576)


class MemFree:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.lock = threading.Lock()
        state = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        self.path = os.path.join(state, "memory-stopped.json")
        self.demo_gone = set()

    # -- what's there ------------------------------------------------------------------------------------------
    def _payloads(self):
        snap = self.ctx.store.snapshot()
        st = snap.get("state")
        agents = getattr(self.ctx.store, "agents", None)
        if not st or agents is None:
            return {}, {}
        return agents.payload(st), {n["name"]: n for n in st.get("nodes") or []}

    def stopped(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def _save_stopped(self, rows):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-100:], f)
        os.replace(tmp, self.path)

    def view(self):
        payloads, nodes = self._payloads()
        out = []
        for name in sorted(payloads):
            d = payloads[name]
            mem = d.get("memory") or {}
            native = any("start_ticks" in p for p in d.get("processes") or [])
            procs = []
            for p in d.get("processes") or []:
                if (p.get("rss") or 0) < MIN_RSS or (self.demo and (name, p.get("pid")) in self.demo_gone):
                    continue
                c = classify(p)
                procs.append({"pid": p.get("pid"), "start": p.get("start_ticks"), "name": p.get("name"), "cmd": p.get("cmd", ""),
                              "user": p.get("user", ""), "rss": p.get("rss") or 0, "swap": p.get("swap") or 0, "anon": p.get("anon"),
                              "cpu": p.get("cpu") or 0, "group": p.get("group") or {}, **c})
            procs.sort(key=lambda x: -x["rss"])
            out.append({"node": name, "ready": bool((nodes.get(name) or {}).get("ready")), "native": native,
                        "mem_total": mem.get("MemTotal"), "mem_available": mem.get("MemAvailable"),
                        "swap_total": mem.get("SwapTotal") or 0, "swap_free": mem.get("SwapFree") or 0,
                        "disk_kind": next((x.get("kind", "") for x in ((d.get("hardware") or {}).get("disks") or [])), ""),
                        "procs": procs})
        return {"nodes": out, "stopped": self.stopped(), "demo": self.demo}

    def _find(self, node, pid):
        payloads, _ = self._payloads()
        d = payloads.get(node)
        if d is None:
            raise MemError("No agent data for %s (is the node agent running there?)." % node, 404)
        p = next((x for x in d.get("processes") or [] if x.get("pid") == pid), None)
        if p is None:
            raise MemError("That process isn't in %s's list any more (it may have ended)." % node, 404)
        return p, d

    # -- doing it ----------------------------------------------------------------------------------------------
    def plan(self, node, body):
        """(sentences, script, how): how = "bin" (helper with the agent programs) or "host"."""
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            raise MemError("Choose a process.")
        p, d = self._find(node, pid)
        c = classify(p)
        start = str(p.get("start_ticks") or "")
        if body.get("start") not in (None, "") and start and str(body.get("start")) != start:
            raise MemError("That process has ended (another one has its number now). Refresh the list.", 409)
        action = body.get("action")
        name = str(p.get("name") or "")
        if action == "swap":
            if not c["can_swap"]:
                raise MemError("%s is needed (%s): moving it to swap would slow the node down." % (name, c["what"]))
            mem = d.get("memory") or {}
            if not mem.get("SwapTotal"):
                raise MemError("%s has no swap yet: set some up first (the button above the list)." % node, 409)
            says = ["Move %s's memory (%s, PID %d) on %s to swap. It comes back into RAM by itself when the program needs it, a little "
                    "slower the first time." % (name, _mb(p.get("anon") or p.get("rss")), pid, node)]
            return says, SWAP % {"pid": pid, "start": start}, "bin"
        if action == "stop":
            if not c["can_stop"]:
                raise MemError("%s is needed (%s), so it can't be stopped here." % (name, c["what"]))
            if c["unit"]:
                off = 1 if body.get("keep_off") else 0
                says = ["Stop %s on %s (%s)." % (c["unit"], node, c["what"])]
                says.append("Keep it off after reboots (systemctl disable). \"Start again\" here undoes it." if off else
                            "It starts again at the next reboot (tick \"keep it off\" to stop that).")
                return says, STOP_UNIT % {"unit": c["unit"], "off": off}, "host"
            says = ["End %s (PID %d) on %s: %s." % (name, pid, node, c["what"]),
                    "It's asked to stop, then forced after 5 s. If something starts it again, it comes back."]
            return says, STOP_PID % {"pid": pid, "start": start}, "host"
        raise MemError("Unknown action.")

    def _exec(self, node, script, how):
        if self.demo:
            return self._demo_exec(node, script)
        try:
            if how == "bin":
                return nodeexec.run(self.kubeconfig, node, script, host=True, enter=False, ns=AGENT_NS, configmap=BIN_CM, deadline=90, kind="memory")
            return nodeexec.run(self.kubeconfig, node, script, host=True, deadline=120, kind="memory")
        except nodeexec.NodeExecError as e:
            raise MemError(str(e), 502)

    def action(self, node, body, confirm):
        says, script, how = self.plan(node, body)
        if not confirm:
            return {"plan": says, "script": script}
        r = _kv(self._exec(node, script, how))
        if r.get("error"):
            raise MemError(r["error"], 409)
        if body.get("action") == "swap":
            before = int(r.get("rss_before") or r.get("before_VmRSS") or 0)
            after = int(r.get("rss_after") or r.get("after_VmRSS") or 0)
            swapped = int(r.get("swap_after") or r.get("after_VmSwap") or 0) - int(r.get("swap_before") or r.get("before_VmSwap") or 0)
            if before - after < 1048576:
                raise MemError("Nothing could be moved: the kernel kept it in RAM (it may already be mostly in swap, or its group "
                               "isn't allowed to swap).", 409)
            return {"done": "Moved %s out of RAM (%s to %s)%s." % (_mb(max(0, before - after)), _mb(before), _mb(after),
                                                                 ", %s into swap" % _mb(swapped) if swapped > 0 else "")}
        p, _ = self._find(node, int(body.get("pid")))
        c = classify(p)
        if c["unit"] and r.get("stopped"):
            with self.lock:
                rows = [x for x in self.stopped() if not (x.get("node") == node and x.get("unit") == c["unit"])]
                rows.append({"node": node, "unit": c["unit"], "what": c["what"], "rss": p.get("rss") or 0, "kept_off": bool(r.get("disabled")), "at": int(time.time())})
                self._save_stopped(rows)
        if self.demo:
            self.demo_gone.add((node, p.get("pid")))
        return {"done": "Stopped %s on %s, freeing about %s." % (c["unit"] or p.get("name"), node, _mb(p.get("rss")))}

    def undo(self, node, body, confirm):
        unit = str(body.get("unit") or "")
        row = next((x for x in self.stopped() if x.get("node") == node and x.get("unit") == unit), None)
        if row is None or not UNIT_RE.match(unit):
            raise MemError("That wasn't stopped from here.", 404)
        says = ["Start %s on %s again%s." % (unit, node, " and turn it back on at boot" if row.get("kept_off") else "")]
        script = START_UNIT % {"unit": unit}
        if not confirm:
            return {"plan": says, "script": script}
        r = _kv(self._exec(node, script, "host"))
        if not r.get("started"):
            raise MemError(r.get("error") or "It didn't start.", 409)
        with self.lock:
            self._save_stopped([x for x in self.stopped() if x is not row and not (x.get("node") == node and x.get("unit") == unit)])
        return {"done": "Started %s on %s again." % (unit, node)}

    def swap_setup(self, node, body, confirm):
        payloads, nodes = self._payloads()
        if node not in nodes:
            raise MemError("No such node.", 404)
        kind = body.get("kind")
        try:
            gib = int(body.get("gib"))
        except (TypeError, ValueError):
            raise MemError("Choose a size.")
        total = ((payloads.get(node) or {}).get("memory") or {}).get("MemTotal") or 0
        if kind == "zram":
            cap = max(1, int(total / 1073741824)) if total else 4
            if not 1 <= gib <= min(16, cap):
                raise MemError("zram can be 1 to %d GB on this node (up to its RAM)." % min(16, cap))
            says = ["Add %d GB of compressed swap in RAM (zram) on %s. Idle memory is squeezed to about a third of its size instead of "
                    "being written to disk: fast, and no wear on an SD card." % (gib, node),
                    "It stays on after reboots (a small nodeyard-zram service)."]
            script = ZRAM % {"mib": gib * 1024}
        elif kind == "file":
            if not 1 <= gib <= 32:
                raise MemError("A swap file can be 1 to 32 GB.")
            says = ["Create a %d GB swap file (/swap.nodeyard) on %s and turn it on; it's added to /etc/fstab so it stays after reboots." % (gib, node),
                    "Idle programs can then be moved out of RAM onto the disk."]
            script = SWAPFILE % {"mib": gib * 1024, "gib": gib}
        else:
            raise MemError("Choose zram or a swap file.")
        if not confirm:
            return {"plan": says, "script": script}
        r = _kv(self._exec(node, script, "host"))
        if not r.get("ok"):
            raise MemError(r.get("error") or "It didn't work.", 409)
        return {"done": "%s now has %d GB of %s." % (node, gib, "compressed swap" if kind == "zram" else "swap")}

    # -- the demo ------------------------------------------------------------------------------------------------
    def _demo_exec(self, node, script):
        if "pageout" in script:
            return "ok=1\nrss_before=88000000\nswap_before=0\nrss_after=6000000\nswap_after=80000000\n"
        if "systemctl stop" in script or "kill -TERM" in script:
            return "stopped=1\n" + ("disabled=1\n" if '"$u" 1' in script else "")
        if "systemctl start" in script:
            return "started=1\n"
        return "ok=1\n"


class MemError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def register(ctx, args):
    m = MemFree(ctx, args)
    ctx.memfree = m

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(h, arg)
            except MemError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    ctx.get_routes["/api/memory"] = wrap(lambda h, q: m.view())
    ctx.post_routes.update({
        "/api/memory/action": wrap(lambda h, b: m.action(str(b.get("node") or ""), b, b.get("confirm") is True)),
        "/api/memory/swap": wrap(lambda h, b: m.swap_setup(str(b.get("node") or ""), b, b.get("confirm") is True)),
        "/api/memory/undo": wrap(lambda h, b: m.undo(str(b.get("node") or ""), b, b.get("confirm") is True)),
    })
