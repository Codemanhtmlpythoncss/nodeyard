"""The Tuning page: per-device settings for running AI well, and CPU/GPU power, clock and overclock controls.

AI settings (threads and memory kept free per node) are the dashboard's own: they are used the next time a split model
is run or switched from the dashboard. Power and clock settings change the machine itself: they run as root in the
node's own namespaces through a short-lived helper pod (nodeexec.py), only after the page has shown exactly what will
run and you've said yes, and every change is in the audit log. Values come from fixed lists and the hardware's own
limits, never free text. Settings last until a reboot unless "keep after reboot" installs a small systemd service;
a Raspberry Pi's overclock is written to its config.txt (a backup is kept) and needs a reboot. "Restore defaults"
undoes all of it.
"""
import base64
import binascii
import copy
import re
import threading
import time

import nodeexec

PREF_KEY = "ai_tuning"

PROBE = r'''c=/sys/devices/system/cpu
echo "governor=$(cat $c/cpu0/cpufreq/scaling_governor 2>/dev/null)"
echo "governors=$(cat $c/cpu0/cpufreq/scaling_available_governors 2>/dev/null)"
echo "hw_min=$(cat $c/cpu0/cpufreq/cpuinfo_min_freq 2>/dev/null)"
echo "hw_max=$(cat $c/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null)"
echo "max=$(cat $c/cpu0/cpufreq/scaling_max_freq 2>/dev/null)"
if [ -f $c/intel_pstate/no_turbo ]; then echo "turbo=$((1 - $(cat $c/intel_pstate/no_turbo)))"; echo "turbo_kind=intel"
elif [ -f $c/cpufreq/boost ]; then echo "turbo=$(cat $c/cpufreq/boost)"; echo "turbo_kind=boost"; fi
echo "swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null)"
echo "model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
for f in /boot/firmware/config.txt /boot/config.txt; do
  if [ -f "$f" ]; then echo "pi_config=$f"; grep -E '^(arm_freq|over_voltage|over_voltage_delta|gpu_freq)=' "$f" | tail -4 | sed 's/^/pi_/'
    grep -q '^# nodeyard overclock start' "$f" && echo "pi_ours=1"; break; fi
done
command -v vcgencmd >/dev/null 2>&1 && echo "pi_throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,name,power.limit,power.min_limit,power.max_limit,power.default_limit,persistence_mode,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | sed 's/^/gpu=/'
[ -f /etc/systemd/system/nodeyard-tune.service ] && echo "persist=1" || echo "persist=0"
echo "thp=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
echo "thp_defrags=$(cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null | tr -d '[]')"
echo "vfs_cache_pressure=$(cat /proc/sys/vm/vfs_cache_pressure 2>/dev/null)"
echo "busy_poll=$(cat /proc/sys/net/core/busy_poll 2>/dev/null)"
echo "rmem_max=$(cat /proc/sys/net/core/rmem_max 2>/dev/null)"
[ -f /proc/sys/kernel/numa_balancing ] && echo "numa_balancing=$(cat /proc/sys/kernel/numa_balancing)"
n=0; d=0; for st in /sys/devices/system/cpu/cpu0/cpuidle/state*; do [ -f "$st/latency" ] || continue
  if [ "$(cat "$st/latency")" -gt 20 ]; then n=$((n + 1)); [ "$(cat "$st/disable")" = 1 ] && d=$((d + 1)); fi; done
echo "cstates_deep=$n"; echo "cstates_deep_off=$d"
dev=$(df -P /var/lib/nodeyard/models / 2>/dev/null | awk 'NR == 2 {print $1}'); b=$(basename "$(readlink -f "$dev" 2>/dev/null)")
[ -e "/sys/class/block/$b/partition" ] && b=$(basename "$(readlink -f "/sys/class/block/$b/..")")
[ -f "/sys/class/block/$b/queue/read_ahead_kb" ] && echo "readahead_dev=$b" && echo "readahead=$(cat "/sys/class/block/$b/queue/read_ahead_kb")"
for svc in k3s k3s-agent; do systemctl is-active --quiet "$svc" 2>/dev/null && echo "k3s_service=$svc"; done
p=$(pidof k3s 2>/dev/null | awk '{print $NF}'); [ -n "$p" ] && echo "k3s_rss_kb=$(awk '/^VmRSS/ {print $2}' "/proc/$p/status")"
p=$(pidof containerd 2>/dev/null | awk '{print $1}'); [ -n "$p" ] && echo "containerd_rss_kb=$(awk '/^VmRSS/ {print $2}' "/proc/$p/status")"
[ -f /etc/rancher/k3s/config.yaml.d/90-nodeyard-lean.yaml ] && echo "lean_cfg=1"
r=/sys/class/powercap/intel-rapl:0
if [ -f $r/constraint_0_power_limit_uw ]; then echo "rapl_pl1=$(cat $r/constraint_0_power_limit_uw)"; echo "rapl_pl2=$(cat $r/constraint_1_power_limit_uw 2>/dev/null)"
  echo "rapl_max2=$(cat $r/constraint_1_max_power_uw 2>/dev/null)"; [ -d /sys/class/powercap/intel-rapl-mmio:0 ] && echo "rapl_mmio=1"; fi
grep -m1 -q GenuineIntel /proc/cpuinfo 2>/dev/null && echo "intel=1"
echo "epp=$(cat $c/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
echo "epps=$(cat $c/cpu0/cpufreq/energy_performance_available_preferences 2>/dev/null)"
echo "profile=$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)"
echo "profiles=$(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)"
for d in /sys/class/drm/card[0-9]*/device; do [ -f "$d/power_dpm_force_performance_level" ] || continue
  h=$(ls -d "$d"/hwmon/hwmon* 2>/dev/null | head -1); n=$(basename "$(dirname "$d")")
  echo "amdgpu=$n,$(cat "$d/power_dpm_force_performance_level"),$(cat "$h/power1_cap" 2>/dev/null),$(cat "$h/power1_cap_max" 2>/dev/null),$(cat "$h/power1_cap_default" 2>/dev/null),$(cat "$h/temp1_input" 2>/dev/null)"; done
for w in /sys/class/net/*/wireless; do [ -d "$w" ] || continue; i=$(basename "$(dirname "$w")")
  command -v iw >/dev/null 2>&1 && echo "wifi=$i,$(iw dev "$i" get power_save 2>/dev/null | awk '{print $NF}')"; done
command -v ethtool >/dev/null 2>&1 && echo "ethtool=1"
[ -f /var/lib/nodeyard-tune.orig ] && echo "oc_saved=1"
grep -qs '^v=2$' /var/lib/nodeyard-tune.orig && echo "orig_saved=1"
[ -f /etc/nodeyard-tune.sh ] && echo "persist_script=$(base64 < /etc/nodeyard-tune.sh | tr -d '\n')"
ls /etc/systemd/system/k3s*.service.d/90-nodeyard-lean.conf >/dev/null 2>&1 && echo "lean_env=1"
true'''

# Raspberry Pi overclocks, by model: (id, label, config lines, about how many °C warmer it runs under load). Moderate
# ones only; they need a good power supply and cooling. The advice picks the highest that stays clear of the throttle point.
_PI4 = [("stock", "Stock (1.5-1.8 GHz)", [], 0), ("mild", "1.9 GHz", ["arm_freq=1900", "over_voltage=2"], 3),
        ("medium", "2.0 GHz", ["arm_freq=2000", "over_voltage=4"], 6), ("high", "2.15 GHz (needs a fan)", ["arm_freq=2147", "over_voltage=6"], 10)]
PI_PRESETS = {
    "Raspberry Pi 5": [("stock", "Stock (2.4 GHz)", [], 0), ("mild", "2.6 GHz", ["arm_freq=2600"], 3),
                       ("medium", "2.8 GHz (a little more voltage)", ["arm_freq=2800", "over_voltage_delta=25000"], 7),
                       ("high", "3.0 GHz (more voltage; needs the active cooler)", ["arm_freq=3000", "over_voltage_delta=50000"], 12)],
    "Raspberry Pi 4": _PI4, "Raspberry Pi Compute Module 4": _PI4,
    "Raspberry Pi 3 Model B Plus": [("stock", "Stock (1.4 GHz)", [], 0), ("mild", "1.5 GHz", ["arm_freq=1500", "over_voltage=2"], 4),
                                    ("medium", "1.55 GHz (needs a heatsink)", ["arm_freq=1550", "over_voltage=4"], 7)],
    "Raspberry Pi 3": [("stock", "Stock (1.2 GHz)", [], 0), ("mild", "1.3 GHz", ["arm_freq=1300", "over_voltage=2"], 4),
                       ("medium", "1.35 GHz (needs a heatsink)", ["arm_freq=1350", "over_voltage=4"], 7)],
    "Raspberry Pi Zero 2": [("stock", "Stock (1.0 GHz)", [], 0), ("mild", "1.2 GHz", ["arm_freq=1200", "over_voltage=6"], 5)],
}
PI_CONFIGS = ("/boot/firmware/config.txt", "/boot/config.txt")
PROBE_MAX_AGE = 600     # older readings are read again before a change is planned
EPP_DEFAULT = "balance_performance"
PROFILE_DEFAULT = "balanced"


class TuneError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def parse_probe(text):
    out = {"gpus": [], "pi": {}}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        v = v.strip()
        if k == "gpu":
            parts = [x.strip() for x in v.split(",")]
            if len(parts) >= 7 and parts[0].isdigit():
                out["gpus"].append({"index": parts[0], "name": parts[1], "power": _num(parts[2]), "min": _num(parts[3]), "max": _num(parts[4]),
                                    "default": _num(parts[5]), "persistence": parts[6].lower() == "enabled", "temp": _num(parts[7]) if len(parts) > 7 else None})
        elif k == "amdgpu":
            parts = [x.strip() for x in v.split(",")]
            if len(parts) >= 6 and re.match(r"^card\d+$", parts[0]):
                uw = [int(x) if x.isdigit() else None for x in parts[2:5]]
                out.setdefault("amdgpus", []).append({"card": parts[0], "level": parts[1], "cap": uw[0] and uw[0] // 1000000, "cap_max": uw[1] and uw[1] // 1000000,
                                                      "cap_default": uw[2] and uw[2] // 1000000, "temp": int(parts[5]) / 1000 if parts[5].isdigit() else None})
        elif k == "wifi":
            name, _, ps = v.partition(",")
            if re.match(r"^[A-Za-z0-9_.-]{1,15}$", name):
                out.setdefault("wifi", []).append({"iface": name, "power_save": ps.strip() == "on"})
        elif k == "pi_config":
            if v in PI_CONFIGS:            # (the path goes into a script: only the two real places)
                out["pi"]["config"] = v
        elif k.startswith("pi_"):
            out["pi"][k[3:]] = v
        elif k == "persist_script":
            try:
                out[k] = base64.b64decode(v).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                out[k] = ""
        elif k in ("hw_min", "hw_max", "max", "swappiness", "turbo", "vfs_cache_pressure", "busy_poll", "rmem_max", "numa_balancing",
                   "cstates_deep", "cstates_deep_off", "readahead", "k3s_rss_kb", "containerd_rss_kb", "rapl_pl1", "rapl_pl2", "rapl_max2"):
            out[k] = int(v) if v.isdigit() else None
        elif k == "thp_defrags":
            out[k] = [x for x in v.split() if re.match(r"^[a-z+]+$", x)]
        elif k in ("governors", "epps", "profiles"):
            out[k] = [g for g in v.split() if re.match(r"^[a-z_-]+$", g)]
        else:
            out[k] = v
    out["persist"] = out.get("persist") == "1"
    out["orig_saved"] = out.get("orig_saved") == "1"
    out["oc_saved"] = out.get("oc_saved") == "1"
    out.setdefault("amdgpus", [])
    out.setdefault("wifi", [])
    out["lean"] = out.get("lean_cfg") == "1" or out.get("lean_env") == "1"
    return out


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pi_presets(model):
    for name, presets in PI_PRESETS.items():
        if (model or "").startswith(name):
            return presets
    return []


class Tuning:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.lock = threading.Lock()
        self.probes = {}          # node -> {"at", "data", "error", "busy"}
        self.demo_state = {}

    # -- the AI part: saved per node, used when the dashboard runs a split model ---------------------------------
    def ai_prefs(self):
        s = getattr(self.ctx, "settings", None)
        raw = (s.prefs() if s else {}).get(PREF_KEY) or {}
        out = {}
        for node, v in raw.items():
            if isinstance(v, dict):
                t, r = v.get("threads"), v.get("reserve_gib")
                out[node] = {"threads": t if isinstance(t, int) and 1 <= t <= 256 else None,
                             "reserve_gib": r if isinstance(r, (int, float)) and 0 <= r <= 64 else None}
        return out

    def set_ai(self, body):
        node = str(body.get("node") or "")
        if node not in {n["name"] for n in self._nodes()}:
            raise TuneError("No such node.")
        try:
            threads = None if body.get("threads") in (None, "", "auto") else int(body["threads"])
            reserve = None if body.get("reserve_gib") in (None, "", "auto") else round(float(body["reserve_gib"]), 1)
        except (TypeError, ValueError):
            raise TuneError("Threads and memory to keep free must be numbers.")
        if threads is not None and not 1 <= threads <= 256:
            raise TuneError("Threads must be between 1 and 256.")
        if reserve is not None and not 0 <= reserve <= 64:
            raise TuneError("Memory to keep free must be between 0 and 64 GiB.")
        s = self.ctx.settings
        with s.lock:
            prefs = s.prefs()
            cur = dict(prefs.get(PREF_KEY) or {})
            if threads is None and reserve is None:
                cur.pop(node, None)
            else:
                cur[node] = {"threads": threads, "reserve_gib": reserve}
            prefs[PREF_KEY] = cur
            s._save_prefs(prefs)
        return self.view()

    def run_args(self):
        """--threads/--reserve for `nodeyard ai split deploy|switch|plan`."""
        out = []
        for node, v in sorted(self.ai_prefs().items()):
            if v["threads"]:
                out += ["--threads", "%s=%d" % (node, v["threads"])]
            if v["reserve_gib"] is not None:
                out += ["--reserve", "%s=%g" % (node, v["reserve_gib"])]
        return out

    # -- reading and changing a machine ---------------------------------------------------------------------------
    def _nodes(self):
        return (self.ctx.store.snapshot().get("state") or {}).get("nodes") or []

    def _exec(self, node, script):
        if self.demo:
            return self._demo_exec(node, script)
        try:
            return nodeexec.run(self.kubeconfig, node, script, host=True, deadline=90, kind="tune")
        except nodeexec.NodeExecError as e:
            raise TuneError(str(e), 502)

    def probe(self, node):
        if node not in {n["name"] for n in self._nodes()}:
            raise TuneError("No such node.")
        with self.lock:
            slot = self.probes.setdefault(node, {})
            if slot.get("busy"):
                return
            slot["busy"] = True
        try:
            data = parse_probe(self._exec(node, PROBE))
            with self.lock:
                self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        except TuneError as e:
            with self.lock:
                self.probes[node] = dict(self.probes.get(node) or {}, error=str(e), busy=False)

    def plan(self, node, body):
        """(what will change, as sentences; the shell script that does it)."""
        says, script, _ = self._plan(node, body)
        return says, script

    def _plan(self, node, body):
        """(sentences, script, info). Everything is checked against the machine's settings as last read, and settings
        that are already what's asked for are left out (info["skipped"] says which); info lists what the script sets,
        so apply() can check afterwards that the machine really took it."""
        with self.lock:
            p = copy.deepcopy((self.probes.get(node) or {}).get("data"))
        if not p:
            raise TuneError("Read this machine's settings first.", 409)
        if body.get("restore"):
            says, script = self._restore_plan(p)
            return says, script, {"restore": True, "keys": [], "skipped": []}
        lines, says, skipped = [], [], []
        rt = []                       # (key, [shell lines]): the runtime changes, by what they set
        cpu = "/sys/devices/system/cpu"
        gov = body.get("governor")
        if gov:
            if gov not in (p.get("governors") or []):
                raise TuneError("This machine doesn't offer the %s governor." % gov)
            if gov == p.get("governor"):
                skipped.append("CPU governor is already %s" % gov)
            else:
                rt.append(("governor", ['for f in %s/cpu*/cpufreq/scaling_governor; do echo %s > "$f"; done' % (cpu, gov)]))
                says.append("CPU governor: %s" % gov)
        if body.get("max_mhz") not in (None, ""):
            try:
                khz = int(float(body["max_mhz"]) * 1000)
            except (TypeError, ValueError):
                raise TuneError("The speed limit must be a number of MHz.")
            if not (p.get("hw_min") and p.get("hw_max") and p["hw_min"] <= khz <= p["hw_max"]):
                raise TuneError("The speed limit must be between %d and %d MHz." % ((p.get("hw_min") or 0) // 1000, (p.get("hw_max") or 0) // 1000))
            if p.get("max") and abs(p["max"] - khz) < 1000:
                skipped.append("CPU speed limit is already %d MHz" % (khz // 1000))
            else:
                rt.append(("max_mhz", ['for f in %s/cpu*/cpufreq/scaling_max_freq; do echo %d > "$f"; done' % (cpu, khz)]))
                says.append("CPU speed limit: %d MHz" % (khz // 1000))
        if body.get("turbo") in (True, False):
            if p.get("turbo_kind") not in ("intel", "boost"):
                raise TuneError("This machine has no turbo boost switch.")
            if p.get("turbo") == (1 if body["turbo"] else 0):
                skipped.append("Turbo boost is already %s" % ("on" if body["turbo"] else "off"))
            else:
                rt.append(("turbo", ["echo %d > %s/intel_pstate/no_turbo" % (0 if body["turbo"] else 1, cpu) if p["turbo_kind"] == "intel"
                                     else "echo %d > %s/cpufreq/boost" % (1 if body["turbo"] else 0, cpu)]))
                says.append("Turbo boost %s" % ("on" if body["turbo"] else "off"))
        if body.get("swappiness") not in (None, ""):
            try:
                sw = int(body["swappiness"])
            except (TypeError, ValueError):
                raise TuneError("Swappiness must be a number.")
            if not 0 <= sw <= 100:
                raise TuneError("Swappiness must be between 0 and 100.")
            if p.get("swappiness") == sw:
                skipped.append("Swappiness is already %d" % sw)
            else:
                rt.append(("swappiness", ["echo %d > /proc/sys/vm/swappiness" % sw]))
                says.append("Swappiness: %d" % sw)
        for idx, watts in (body.get("gpu_power") or {}).items():
            g = next((x for x in p["gpus"] if x["index"] == str(idx)), None)
            try:
                w = int(watts)
            except (TypeError, ValueError):
                raise TuneError("A GPU power limit must be a number of watts.")
            if not g or g["min"] is None or g["max"] is None or not g["min"] <= w <= g["max"]:
                raise TuneError("GPU %s's power limit must be between %s and %s W." % (idx, g and g["min"], g and g["max"]))
            if g.get("power") is not None and abs(g["power"] - w) < 1:
                skipped.append("GPU %s's power limit is already %d W" % (g["index"], w))
                continue
            rt.append(("gpu_power:%s" % g["index"], ["nvidia-smi -pm 1 >/dev/null; nvidia-smi -i %s -pl %d" % (g["index"], w)]))
            says.append("GPU %s (%s) power limit: %d W" % (g["index"], g["name"], w))
        ak = body.get("ai_kernel") or {}
        if ak:
            if ak.get("memory"):
                mem = []
                if p.get("thp") is not None and p.get("thp") != "always":
                    defrag = next((d for d in ("defer+madvise", "madvise") if d in (p.get("thp_defrags") or [])), None)
                    mem.append("echo always > /sys/kernel/mm/transparent_hugepage/enabled" + ("; echo %s > /sys/kernel/mm/transparent_hugepage/defrag" % defrag if defrag else ""))
                if p.get("swappiness") != 10 or p.get("vfs_cache_pressure") != 50:
                    mem += ["echo 10 > /proc/sys/vm/swappiness", "echo 50 > /proc/sys/vm/vfs_cache_pressure"]
                if p.get("numa_balancing"):
                    mem.append("echo 0 > /proc/sys/kernel/numa_balancing")
                if mem:
                    rt.append(("ai_memory", mem))
                    says.append("Huge pages for the model's memory, and keep the model in RAM (swappiness 10, vfs_cache_pressure 50%s)" % (
                        ", no NUMA balancing" if p.get("numa_balancing") else ""))
                else:
                    skipped.append("The AI memory settings are already in place")
            if ak.get("network"):
                net = []
                if (p.get("busy_poll") or 0) != 50 or (p.get("rmem_max") or 0) != 16777216:
                    net += ["echo 50 > /proc/sys/net/core/busy_poll; echo 50 > /proc/sys/net/core/busy_read",
                            "echo 16777216 > /proc/sys/net/core/rmem_max; echo 16777216 > /proc/sys/net/core/wmem_max",
                            "echo '4096 131072 16777216' > /proc/sys/net/ipv4/tcp_rmem; echo '4096 65536 16777216' > /proc/sys/net/ipv4/tcp_wmem"]
                for w in p.get("wifi") or []:
                    if w["power_save"]:
                        net.append("iw dev %s set power_save off" % w["iface"])
                        says.append("Wi-Fi power saving off on %s (it holds packets back for up to ~100 ms to save power)" % w["iface"])
                if net and p.get("ethtool"):
                    net.append('for i in $(ls /sys/class/net); do [ -e "/sys/class/net/$i/device" ] && [ ! -d "/sys/class/net/$i/wireless" ] && '
                               'ethtool --set-eee "$i" eee off 2>/dev/null; done; true')
                if net:
                    rt.append(("ai_network", net))
                    says.append("Low-latency networking for the split model: busy polling and larger socket buffers%s" % (
                        ", Energy-Efficient Ethernet off on the wired links" if p.get("ethtool") else ""))
                else:
                    skipped.append("Low-latency networking is already on")
            if ak.get("cstates"):
                if not p.get("cstates_deep"):
                    raise TuneError("This machine has no deep CPU sleep states to turn off.")
                if p.get("cstates_deep_off") == p.get("cstates_deep"):
                    skipped.append("Deep CPU sleep states are already off")
                else:
                    rt.append(("cstates", ['for st in /sys/devices/system/cpu/cpu*/cpuidle/state*; do [ "$(cat "$st/latency" 2>/dev/null || echo 0)" -gt 20 ] && echo 1 > "$st/disable"; done; true']))
                    says.append("Keep CPU cores out of deep sleep (they wake up instantly for each token; uses more power and runs warmer)")
            if ak.get("readahead"):
                dev = p.get("readahead_dev") or ""
                if not re.match(r"^[a-z0-9]+$", dev):
                    raise TuneError("Couldn't find the disk the models are on.")
                if p.get("readahead") == 4096:
                    skipped.append("Read-ahead on %s is already 4 MB" % dev)
                else:
                    rt.append(("readahead", ["echo 4096 > /sys/class/block/%s/queue/read_ahead_kb" % dev]))
                    says.append("Read ahead 4 MB on %s (loads a model faster)" % dev)
        lean = body.get("lean_k3s") or {}
        if lean:
            svc = p.get("k3s_service")
            if svc not in ("k3s", "k3s-agent"):
                raise TuneError("k3s isn't running as a service on this machine.")
            cfg = []
            if lean.get("disable"):
                if svc != "k3s":
                    raise TuneError("Only the control node's k3s runs Traefik and the load balancer.")
                opts = self.lean_options()
                for comp in lean["disable"]:
                    if comp not in opts or not opts[comp]["unused"]:
                        raise TuneError("%s can't be turned off: %s" % (comp, (opts.get(comp) or {}).get("why", "unknown component")))
                cfg += ["disable+:"] + ["  - %s" % c for c in lean["disable"]]
                says.append("Turn off %s (nothing uses %s)" % (" and ".join(opts[c]["label"] for c in lean["disable"]), "them" if len(lean["disable"]) > 1 else "it"))
            if lean.get("image_gc"):
                cfg += ["kubelet-arg+:", "  - image-gc-high-threshold=75", "  - image-gc-low-threshold=60"]
                says.append("Remove unused container images sooner (at 75% disk instead of 85%)")
            if cfg:
                lines.append("mkdir -p /etc/rancher/k3s/config.yaml.d && cat > /etc/rancher/k3s/config.yaml.d/90-nodeyard-lean.yaml <<'NYLEAN'\n"
                             "# nodeyard Tuning page: lighter Kubernetes (\"Restore defaults\" there removes this file)\n%s\nNYLEAN" % "\n".join(cfg))
            if lean.get("gc"):
                rss = (p.get("k3s_rss_kb") or 0) // 1024
                # (a soft limit only: Go collects at its normal pace until k3s nears it, so nothing slows down; GOGC stays at its default)
                limit = min(2048, max(384, int(rss * 0.85))) if svc == "k3s" else min(1024, max(192, int(rss * 0.85)))
                lines.append(("mkdir -p /etc/systemd/system/%(svc)s.service.d && cat > /etc/systemd/system/%(svc)s.service.d/90-nodeyard-lean.conf <<'NYLEAN'\n"
                              "[Service]\nEnvironment=GOMEMLIMIT=%(limit)dMiB\nNYLEAN") % {"svc": svc, "limit": limit})
                says.append("k3s hands back memory it no longer needs once it passes %d MiB%s (a soft limit: Go's normal collection pace, so it isn't slowed)" % (
                    limit, ", it uses %d MiB now" % rss if rss else ""))
            if not cfg and not lean.get("gc"):
                raise TuneError("Pick at least one Kubernetes change.")
            lines.append("systemctl daemon-reload")
            if lean.get("restart"):
                lines.append(LATER_RESTART % {"svc": svc})
                says.append("Restart k3s in 5 s to use them (running pods keep running; the cluster API pauses for about 20 s)")
            else:
                says.append("They take effect the next time k3s restarts")
        oc = body.get("oc") or {}
        if oc:
            asked = False
            if oc.get("pl1_w") not in (None, ""):
                asked = True
                if not (p.get("intel") and p.get("rapl_pl1") and p.get("rapl_pl2")):
                    raise TuneError("This machine has no adjustable CPU power limits.")
                try:
                    w = int(oc["pl1_w"])
                except (TypeError, ValueError):
                    raise TuneError("The power limit must be a number of watts.")
                top = p["rapl_pl2"] // 1000000
                if not 5 <= w <= top:
                    raise TuneError("The sustained power limit can be 5 to %d W (its boost limit)." % top)
                if p["rapl_pl1"] // 1000000 == w:
                    skipped.append("The CPU's sustained power limit is already %d W" % w)
                else:
                    rt.append(("oc_pl1", ["for r in /sys/class/powercap/intel-rapl:0 /sys/class/powercap/intel-rapl-mmio:0; do "
                                          "[ -f $r/constraint_0_power_limit_uw ] && echo %d > $r/constraint_0_power_limit_uw; done; true" % (w * 1000000)]))
                    says.append("CPU sustained power limit: %d W (from %d W; the boost limit stays %d W)" % (w, p["rapl_pl1"] // 1000000, top))
            if oc.get("epp"):
                asked = True
                if oc["epp"] not in (p.get("epps") or []):
                    raise TuneError("This CPU doesn't offer the %s energy preference." % oc["epp"])
                if oc["epp"] == p.get("epp"):
                    skipped.append("The CPU energy preference is already %s" % oc["epp"])
                else:
                    rt.append(("oc_epp", ['for f in %s/cpu*/cpufreq/energy_performance_preference; do echo %s > "$f"; done 2>/dev/null; true' % (cpu, oc["epp"])]))
                    says.append("CPU energy preference: %s (how eagerly it clocks up)" % oc["epp"])
            if oc.get("profile"):
                asked = True
                if oc["profile"] not in (p.get("profiles") or []):
                    raise TuneError("This machine doesn't offer the %s platform profile." % oc["profile"])
                if oc["profile"] == p.get("profile"):
                    skipped.append("The platform profile is already %s" % oc["profile"])
                else:
                    rt.append(("oc_profile", ["echo %s > /sys/firmware/acpi/platform_profile" % oc["profile"]]))
                    says.append("Platform profile: %s (the firmware's own power and fan setting)" % oc["profile"])
            for card, watts in (oc.get("amd_cap") or {}).items():
                asked = True
                g = next((x for x in p.get("amdgpus") or [] if x["card"] == card), None)
                try:
                    w = int(watts)
                except (TypeError, ValueError):
                    raise TuneError("A GPU power cap must be a number of watts.")
                if not g or not g.get("cap_max") or not 10 <= w <= g["cap_max"]:
                    raise TuneError("%s's power cap must be between 10 and %s W." % (card, g and g.get("cap_max")))
                if g.get("cap") == w:
                    skipped.append("%s's power cap is already %d W" % (card, w))
                    continue
                rt.append(("amd_cap:%s" % card, ["for h in /sys/class/drm/%s/device/hwmon/hwmon*; do echo %d > $h/power1_cap; done" % (card, w * 1000000)]))
                says.append("AMD GPU %s power cap: %d W (from %s W)" % (card, w, g.get("cap")))
            if not asked:
                raise TuneError("Pick an overclock setting.")
        pi = body.get("pi_preset")
        if pi:
            presets = pi_presets(p.get("model"))
            pick = next((x for x in presets if x[0] == pi), None)
            if not pick or not p["pi"].get("config"):
                raise TuneError("There's no %s overclock for this machine." % pi)
            if (self._node(node).get("hw") or {}).get("undervoltage") and pi != "stock":
                raise TuneError("This Pi reports low supply voltage: overclocking it would make it crash. Fix the power supply first.")
            cur = p["pi"].get("arm_freq") or ""
            if (cur and "arm_freq=" + cur in pick[2]) or (not cur and not pick[2] and not p["pi"].get("ours")):
                skipped.append("The Raspberry Pi is already at %s" % pick[1])
            else:
                lines.append(PI_BLOCK_REMOVE % {"f": p["pi"]["config"]})
                if pick[2]:
                    lines.append(PI_BLOCK_ADD % {"f": p["pi"]["config"], "lines": "\\n".join(pick[2])})
                says.append("Raspberry Pi clock: %s (in %s; takes effect after a reboot; a backup is kept)" % (pick[1], p["pi"]["config"]))
        if not rt and not lines:
            raise TuneError("Nothing to change: " + ("; ".join(skipped) if skipped else "pick a setting") + ".", 409)
        script = lines[:]
        if rt:
            script.insert(0, SAVE_ORIG)       # what everything was before nodeyard first changed it, for Restore defaults
            script += [ln for _, block in rt for ln in block]
        # Keep after a reboot: one boot script holding every kept setting, merged by what each sets. This change replaces its
        # own earlier entry; a change that isn't kept removes its earlier kept entry, so a reboot never brings back an old value.
        kept = parse_persisted(p.get("persist_script"))
        changed = {k for k, _ in rt}
        if rt and (body.get("persist") or (changed & set(kept))):
            merged = {k: v for k, v in kept.items() if k not in changed}
            if body.get("persist"):
                merged.update({k: block for k, block in rt})
                says.append("Keep these after a reboot (the nodeyard-tune service)")
            else:
                says.append("Not kept after a reboot (and the earlier kept value for %s is dropped)" % ", ".join(sorted(changed & set(kept))))
            script.append(persist_script(merged))
        return says, "\n".join(script), {"keys": [k for k, _ in rt], "persist": bool(rt and body.get("persist")), "skipped": skipped,
                                         "pi": any("Raspberry Pi clock" in x for x in says), "lean": bool(lean), "restore": False}

    def _restore_plan(self, p):
        """Back to what the machine had before nodeyard changed it (its saved originals), or nothing when nodeyard hasn't."""
        says, lines = [], []
        touched = p.get("orig_saved") or p.get("oc_saved") or p.get("persist") or p.get("lean") or (p["pi"].get("config") and p["pi"].get("ours"))
        if not touched:
            raise TuneError("Nothing to restore: nodeyard hasn't changed this machine's settings.", 409)
        if p.get("orig_saved") or p.get("oc_saved"):
            lines.append(RESTORE_ORIG)
            says.append("Settings back to what they were before nodeyard first changed them (saved on the machine at the time)")
        if p.get("persist"):
            if not (p.get("orig_saved") or p.get("oc_saved")):
                # (changed by an older nodeyard that didn't save the originals: the kernel's usual defaults for what it kept)
                kept = parse_persisted(p.get("persist_script"))
                lines += legacy_defaults(p, kept)
                says.append("Kept settings back to the usual defaults (the originals weren't saved): %s" % ", ".join(sorted(kept)) if kept else
                            "Kept settings back to the usual defaults")
            lines.append("systemctl disable nodeyard-tune.service >/dev/null 2>&1; rm -f /etc/systemd/system/nodeyard-tune.service /etc/nodeyard-tune.sh; "
                         "systemctl daemon-reload 2>/dev/null; true")
            says.append("Remove the keep-after-reboot service")
        for g in p.get("amdgpus") or []:
            if g.get("level") not in (None, "auto"):
                lines.append("echo auto > /sys/class/drm/%s/device/power_dpm_force_performance_level 2>/dev/null" % g["card"])
        if p["pi"].get("config") and p["pi"].get("ours"):
            lines.append(PI_BLOCK_REMOVE % {"f": p["pi"]["config"]})
            says.append("Remove the Raspberry Pi overclock from %s (takes effect after a reboot)" % p["pi"]["config"])
        if p.get("lean"):
            lines.append(LEAN_REMOVE)
            if p.get("k3s_service") in ("k3s", "k3s-agent"):
                lines.append(LATER_RESTART % {"svc": p["k3s_service"]})
            says.append("Kubernetes back to its usual settings (k3s restarts in 5 s; running pods keep running)")
        return says, "\n".join(lines)

    def lean_options(self):
        """Bundled k3s parts that can go, and whether anything uses them."""
        st = self.ctx.store.snapshot().get("state") or {}
        ing = st.get("ingresses") or []
        lbs = [s for s in st.get("services") or [] if s.get("type") == "LoadBalancer" and not (s.get("namespace") == "kube-system" and s.get("name") == "traefik")]
        traefik = any(s.get("namespace") == "kube-system" and s.get("name") == "traefik" for s in st.get("services") or [])
        return {"traefik": {"label": "Traefik (the bundled web router)", "present": traefik, "unused": not ing,
                            "why": "%d Ingress%s use%s it" % (len(ing), "es" if len(ing) != 1 else "", "" if len(ing) == 1 else "") if ing else "nothing uses it"},
                "servicelb": {"label": "the built-in load balancer (servicelb)", "present": True, "unused": not lbs,
                              "why": ("LoadBalancer services use it: " + ", ".join(s["name"] for s in lbs[:4])) if lbs else "nothing uses it"}}

    def _node(self, name):
        return next((n for n in self._nodes() if n["name"] == name), {})

    def apply(self, node, body, confirm):
        with self.lock:
            at = (self.probes.get(node) or {}).get("at") or 0
        if time.time() - at > PROBE_MAX_AGE:      # (never plan against old readings: read it again first)
            self.probe(node)
        says, script, info = self._plan(node, body)
        if not confirm:
            return {"plan": says, "script": script, "skipped": info["skipped"]}
        out = self._exec(node, script + "\n" + PROBE)
        data = parse_probe(out)
        with self.lock:
            self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        results = verify(info, body, data)
        return {"done": says, "results": results, "failed": [r for r in results if not r["ok"]], "skipped": info["skipped"]}

    # -- overclocking: how far to push -----------------------------------------------------------------------------
    def oc_advice(self, n, p):
        """How far to push each overclock on this machine, judged from how hot it runs under load, whether it has slowed
        itself down since boot and its power supply: {"lines": [{"what", "advice", "level": go|careful|stop}], "fix": the
        recommended settings, or None}."""
        if not p:
            return {"lines": [], "fix": None}
        hw = n.get("hw") or {}
        temp, use = hw.get("temp_c"), hw.get("cpu_use")
        model = p.get("model") or ""
        pi = model.startswith("Raspberry Pi")
        plus = model.startswith("Raspberry Pi 3 Model B Plus")
        limit = 60 if plus else 80 if pi else 90          # where it starts slowing itself down
        est = None if temp is None else (temp if (use or 0) >= 60 else temp + (15 if pi else 20))   # under the model's load
        head = None if est is None else limit - est
        stop = []
        if hw.get("undervoltage"):
            stop.append("its power supply voltage is low (use the official supply first)")
        thr = (p.get("pi") or {}).get("throttled")
        if thr and thr not in ("0x0", "0"):
            stop.append("it has slowed itself down since it booted (%s): fix cooling or power first" % thr)
        if temp is not None and temp >= limit - 5:
            stop.append("it's already at %.0f °C" % temp)
        lines, fix = [], {}
        loadtxt = ("about %.0f °C under load" % est) if est is not None else "at an unknown temperature (the node agent reads it)"
        presets = pi_presets(model)
        if presets and p["pi"].get("config"):
            freq = p["pi"].get("arm_freq") or ""
            cur = next((x for x in presets if "arm_freq=" + freq in x[2]), presets[0]) if freq else presets[0]
            if stop:
                lines.append({"what": "Raspberry Pi clock (now %s)" % cur[1], "level": "stop", "advice": "Don't overclock: " + "; ".join(stop) + "."})
            else:
                ok = [x for x in presets if (est is None and x[3] <= 4) or (est is not None and est + x[3] <= limit - 5)]
                best = ok[-1] if ok else presets[0]
                i = presets.index(best)
                nxt = presets[i + 1] if i + 1 < len(presets) else None
                if est is None:
                    txt = "Go no further than %s until its temperature can be read." % best[1]
                else:
                    txt = "Push to %s. It runs %s, so that keeps it near %.0f °C, clear of the %d °C where it slows itself%s." % (
                        best[1], loadtxt, est + best[3], limit, " (a 3B+ slows at 60 °C unless temp_soft_limit is raised)" if plus else "")
                    if nxt:
                        txt += " %s would reach about %.0f °C: too hot without better cooling." % (nxt[1], est + nxt[3])
                    elif i:
                        txt += " That's as far as nodeyard goes on this model."
                lines.append({"what": "Raspberry Pi clock (now %s)" % cur[1], "level": "go" if i else "careful", "advice": txt})
                if presets.index(best) > presets.index(cur):
                    fix["pi_preset"] = best[0]
        if p.get("intel") and p.get("rapl_pl1") and p.get("rapl_pl2") and p["rapl_pl2"] > p["rapl_pl1"] * 1.05:
            pl1, pl2 = p["rapl_pl1"] // 1000000, p["rapl_pl2"] // 1000000
            what = "CPU power limit (%d W sustained, %d W boost)" % (pl1, pl2)
            target = None if stop else pl2 if head is not None and head >= 20 else (pl1 + pl2) // 2 if head is None or head >= 10 else None
            if target:
                lines.append({"what": what, "level": "go" if target == pl2 else "careful",
                              "advice": "Raise the sustained limit to %d W. It runs %s%s, so it can hold its boost clock through a long answer instead of "
                                        "dropping back after about 30 s. Past about %d °C it slows itself, so this can't harm it." % (
                                            target, loadtxt, "" if head is None else " (%.0f °C of headroom)" % head, limit + 10)})
                fix["oc"] = dict(fix.get("oc") or {}, pl1_w=target)
            else:
                lines.append({"what": what, "level": "stop", "advice": "Leave it: " + ("; ".join(stop) if stop else
                              "only %.0f °C of headroom under load. Clean the fan or improve the airflow first" % head) + "."})
        if p.get("epp") and "performance" in (p.get("epps") or []) and p["epp"] != "performance":
            lines.append({"what": "CPU energy preference (now %s)" % p["epp"], "level": "go",
                          "advice": "Set it to performance: the CPU clocks up the moment a token starts instead of easing into it. It idles the same."})
            fix["oc"] = dict(fix.get("oc") or {}, epp="performance")
        if "performance" in (p.get("profiles") or []) and p.get("profile") != "performance":
            warm = head is not None and head < 15
            lines.append({"what": "Platform profile (now %s)" % (p.get("profile") or "?"), "level": "careful" if warm or stop else "go",
                          "advice": "Set it to performance: the firmware allows more power and spins the fans up sooner%s." % (
                              " (it runs warm: watch that it stays under %d °C)" % limit if warm else "")})
            if not warm and not stop:
                fix["oc"] = dict(fix.get("oc") or {}, profile="performance")
        for g in p.get("gpus") or []:
            if g.get("power") and g.get("max") and g["power"] < g["max"] * 0.95:
                t, what = g.get("temp"), "GPU %s power limit (%d of %d W)" % (g["index"], g["power"], g["max"])
                if t is not None and t >= 80:
                    lines.append({"what": what, "level": "stop", "advice": "Leave it: the card is at %.0f °C." % t})
                    continue
                w = int(g["max"]) if t is None or t < 65 else int((g["power"] + g["max"]) / 2)
                lines.append({"what": what, "level": "go" if w == int(g["max"]) else "careful",
                              "advice": "Raise it to %d W%s: the card holds higher clocks on the model, within its own rated limit." % (
                                  w, " (it's %.0f °C now)" % t if t is not None else "")})
                fix["gpu_power"] = dict(fix.get("gpu_power") or {}, **{g["index"]: w})
        for g in p.get("amdgpus") or []:
            if g.get("cap") and g.get("cap_max") and g["cap"] < g["cap_max"] * 0.95:
                t = g.get("temp")
                w = g["cap_max"] if t is None or t < 65 else (g["cap"] + g["cap_max"]) // 2
                lines.append({"what": "AMD GPU %s power cap (%d of %d W)" % (g["card"], g["cap"], g["cap_max"]), "level": "go" if w == g["cap_max"] else "careful",
                              "advice": "Raise it to %d W, within its rated maximum%s." % (w, " (it's %.0f °C now)" % t if t is not None else "")})
                oc = dict(fix.get("oc") or {})
                oc["amd_cap"] = dict(oc.get("amd_cap") or {}, **{g["card"]: w})
                fix["oc"] = oc
        if not lines:
            lines.append({"what": "Overclocking", "level": "careful", "advice": "Nothing on this machine can be pushed further from Linux%s." % (
                "" if pi else ": it's already at its limits, or (x86) its clocks are set in the BIOS")})
        return {"lines": lines, "fix": fix or None}

    def reset_all(self, confirm):
        """Restore defaults on every machine that has been read, and the AI settings back to automatic."""
        ready = [n["name"] for n in self._nodes() if n.get("ready")]
        with self.lock:
            read = {k for k, v in self.probes.items() if v.get("data")}
        plans, untouched = [], []
        for node in ready:
            if node in read:
                try:
                    says, script = self.plan(node, {"restore": True})
                except TuneError:
                    untouched.append(node)      # (nodeyard hasn't changed it: nothing to put back)
                    continue
                plans.append({"node": node, "plan": says, "script": script})
        ai = sorted(self.ai_prefs())
        if not confirm:
            return {"plans": plans, "unread": [x for x in ready if x not in read], "untouched": untouched, "ai": ai}
        results = {}

        def one(node):
            try:
                r = self.apply(node, {"restore": True}, True)
                results[node] = "; ".join("%s (now %s)" % (x["what"], x["now"]) for x in r["failed"])
            except TuneError as e:
                results[node] = str(e)
        threads = [threading.Thread(target=one, args=(x["node"],), daemon=True) for x in plans]
        for t in threads:
            t.start()
        for t in threads:
            t.join(150)
        if ai:
            st = self.ctx.settings
            with st.lock:
                prefs = st.prefs()
                prefs[PREF_KEY] = {}
                st._save_prefs(prefs)
        return {"done": sorted(k for k, v in results.items() if not v), "failed": {k: v for k, v in results.items() if v}, "ai_cleared": ai}

    # -- what to suggest ------------------------------------------------------------------------------------------
    def recommendations(self, n, probe, ai, rpc_threads, split=None):
        """[{"what", "why", "fix": {...} or None, "kind": "ai"|"power"|"hardware"}] for one node."""
        hw = n.get("hw") or {}
        cpu = (hw.get("hardware") or {}).get("cpu") or {}
        out = []
        physical = cpu.get("cores") or 0
        cp = any(r in ("control-plane", "master") for r in n.get("roles") or [])
        if physical:
            want = max(1, physical - (1 if cp else 0))
            have = (ai.get("threads") or rpc_threads.get(n["name"]))
            if have and have != want:
                out.append({"kind": "ai", "what": "Use %d AI threads (now %d)" % (want, have),
                            "why": "llama.cpp runs best with one thread per physical core%s (this %s has %d physical cores, %d threads)." % (
                                ", leaving one for Kubernetes on the control plane" if cp else "", cpu.get("model") or "CPU", physical, cpu.get("threads") or physical),
                            "fix": {"ai": {"threads": want}}})
        if probe:
            if probe.get("governor") and probe["governor"] != "performance" and "performance" in (probe.get("governors") or []):
                out.append({"kind": "power", "what": "Switch the CPU governor to performance (now %s)" % probe["governor"],
                            "why": "The model's work comes in short bursts; %s lowers the clock between them, which slows each token." % probe["governor"],
                            "fix": {"governor": "performance"}})
            if probe.get("hw_max") and probe.get("max") and probe["max"] < probe["hw_max"] * 0.97:
                out.append({"kind": "power", "what": "Remove the CPU speed limit (%d of %d MHz)" % (probe["max"] // 1000, probe["hw_max"] // 1000),
                            "why": "The CPU is capped below its own maximum.", "fix": {"max_mhz": probe["hw_max"] // 1000}})
            if probe.get("turbo") == 0:
                out.append({"kind": "power", "what": "Turn turbo boost on", "why": "It's off, so the CPU never goes above its base clock.", "fix": {"turbo": True}})
            if (probe.get("swappiness") or 0) > 20 and (hw.get("swap_total") or 0) > 0:
                out.append({"kind": "power", "what": "Lower swappiness to 10 (now %d)" % probe["swappiness"],
                            "why": "Swapping model memory to disk stalls answers; 10 keeps it in RAM unless memory really runs out.", "fix": {"swappiness": 10}})
            for g in probe.get("gpus") or []:
                if g["power"] and g["max"] and g["power"] < g["max"] * 0.95:
                    out.append({"kind": "power", "what": "Raise GPU %s's power limit to %d W (now %d W)" % (g["index"], int(g["max"]), int(g["power"])),
                                "why": "The card is capped below what it's rated for, so it clocks lower under load.", "fix": {"gpu_power": {g["index"]: int(g["max"])}}})
            thr = probe.get("pi", {}).get("throttled")
            if thr and thr not in ("0x0", "0"):
                out.append({"kind": "hardware", "what": "This Pi has been throttled (%s)" % thr,
                            "why": "Low voltage or heat made it slow down. A better power supply or cooling fixes it; don't overclock until it's 0x0.", "fix": None})
        if probe:
            holds_model = bool(split) and (n["name"] in (rpc_threads or {}) or split.get("main_node") == n["name"])
            k = []
            if probe.get("thp") not in (None, "always"):
                k.append("huge pages")
            if (probe.get("busy_poll") or 0) == 0 and holds_model:
                k.append("low-latency networking")
            if holds_model and any(w["power_save"] for w in probe.get("wifi") or []):
                k.append("Wi-Fi power saving off")
            if k:
                out.append({"kind": "power", "what": "Use the AI kernel profile (%s)" % " and ".join(k),
                            "why": "Large-model inference reads gigabytes of weights per token: huge pages cut the address lookups, and busy polling answers "
                                   "the other nodes' small messages sooner. Undo it any time with Restore defaults.",
                            "fix": {"ai_kernel": {"memory": True, "network": holds_model, "readahead": bool(probe.get("readahead_dev"))}}})
            if probe.get("k3s_service") and not probe.get("lean") and (probe.get("k3s_rss_kb") or 0) > 150 * 1024:
                server = probe["k3s_service"] == "k3s"
                drop = [c for c, o in (self.lean_options().items() if server else []) if o["unused"] and o["present"]]
                out.append({"kind": "power", "what": "Make Kubernetes lighter (k3s uses %d MB here)" % (probe["k3s_rss_kb"] // 1024),
                            "why": "Its Go runtime holds on to memory it no longer needs; a soft memory limit makes it hand that back (often 10-20%%) without slowing it%s. "
                                   "That memory goes to the model." % ((", and " + " and ".join(self.lean_options()[c]["label"] for c in drop) + " can go: nothing uses them") if drop else ""),
                            "fix": {"lean_k3s": {"gc": True, "image_gc": True, "disable": drop, "restart": True}}})
            if split and split.get("main_node") == n["name"] and (split.get("kv") or "f16") == "f16" and (hw.get("mem_available") or 1 << 40) < 2 * 1073741824:
                out.append({"kind": "ai", "what": "Halve the conversation memory (KV cache q8_0)",
                            "why": "The model's memory of the conversation uses about 1.5 GB at 16k tokens in full precision; q8_0 halves it with practically the same "
                                   "answers. Run the model again from AI › Models and choose \"Half the memory\".", "fix": None})
        temp = hw.get("temp_c")
        if temp is not None and temp >= 80:
            out.append({"kind": "hardware", "what": "It runs hot (%.0f °C)" % temp, "why": "Above about 80 °C CPUs slow themselves down. Improve airflow or the cooler before overclocking.", "fix": None})
        for nic in (hw.get("hardware") or {}).get("nics") or []:
            if nic.get("cluster") and nic.get("wireless"):
                out.append({"kind": "hardware", "what": "Use a network cable", "why": "A split model sends data between nodes for every token; Wi-Fi adds delay.", "fix": None})
            elif nic.get("cluster") and nic.get("speed_mbps") and nic["speed_mbps"] < 1000 and (nic.get("max_mbps") or 1000) >= 1000:
                out.append({"kind": "hardware", "what": "The network link runs at %d Mbit/s" % nic["speed_mbps"],
                            "why": "The card can do gigabit: swap the cable or switch port.", "fix": None})
        if hw.get("undervoltage"):
            out.append({"kind": "hardware", "what": "Low power-supply voltage", "why": "Use the official power supply; it can crash or slow down.", "fix": None})
        return out

    def view(self):
        st = self.ctx.store.snapshot().get("state") or {}
        split = (st.get("ai") or {}).get("split") or {}
        rpc_threads = split.get("threads") or {}
        shares = {s["node"]: s for s in split.get("shares") or []}
        ai = self.ai_prefs()
        out = []
        with self.lock:
            probes = copy.deepcopy(self.probes)
        for n in st.get("nodes") or []:
            hw = n.get("hw") or {}
            cpu = (hw.get("hardware") or {}).get("cpu") or {}
            pr = probes.get(n["name"]) or {}
            data = pr.get("data")
            out.append({
                "node": n["name"], "ready": bool(n.get("ready")), "arch": n.get("arch"), "roles": n.get("roles") or [],
                "cpu": {"model": cpu.get("model") or "", "cores": cpu.get("cores"), "threads": cpu.get("threads"), "freq_mhz": hw.get("freq_mhz"),
                        "freq_max": hw.get("freq_max"), "governor": hw.get("governor"), "temp_c": hw.get("temp_c")},
                "mem_total": n.get("mem_total"), "gpus": [g.get("name") for g in (hw.get("gpu_live") or [])],
                "ai": {"saved": ai.get(n["name"]) or {"threads": None, "reserve_gib": None}, "rpc_threads": rpc_threads.get(n["name"]),
                       "share_mib": (shares.get(n["name"]) or {}).get("mib"), "main": split.get("main_node") == n["name"]},
                "probe": data, "probe_at": pr.get("at"), "probe_error": pr.get("error", ""), "probing": bool(pr.get("busy")),
                "pi_presets": [{"id": x[0], "label": x[1]} for x in pi_presets((data or {}).get("model"))] if data else [],
                "tips": self.recommendations(n, data, ai.get(n["name"]) or {}, rpc_threads, split),
                "lean_options": self.lean_options() if data and data.get("k3s_service") == "k3s" else None,
                "oc": self.oc_advice(n, data),
            })
        return {"nodes": out, "demo": self.demo, "agents": bool((st.get("agents") or {}).get("installed"))}

    # -- the demo: a believable machine that remembers what you set -----------------------------------------------
    def _demo_exec(self, node, script):
        cp = any(r in ("control-plane", "master") for r in self._node(node).get("roles") or [])
        st = self.demo_state.setdefault(node, {"governor": "powersave", "max": 3600000, "turbo": 1, "swappiness": 60, "gpu": 220, "persist": 0,
                                               "thp": "madvise", "busy": 0, "lean": 0, "rss": 610000 if cp else 230000,
                                               "pl1": 15000000, "epp": "balance_power", "profile": "balanced", "oc": 0})
        for v in re.findall(r"echo (\d+) > \$r/constraint_0_power_limit_uw", script):
            st["pl1"], st["oc"] = int(v), 1
        for v in re.findall(r"echo ([a-z_]+) > \"\$f\"; done 2>/dev/null", script):
            st["epp"], st["oc"] = v, 1
        for v in re.findall(r"echo ([a-z-]+) > /sys/firmware/acpi/platform_profile$", script, re.M):
            st["profile"], st["oc"] = v, 1
        if 'echo "v=2"' in script and not st.get("orig"):
            st["orig"] = {k: st[k] for k in ("governor", "max", "turbo", "swappiness", "gpu", "thp", "busy", "pl1", "epp", "profile")}
        if "rm -f $o" in script and st.get("orig"):
            st.update(st.pop("orig"), oc=0)
        elif "rm -f $o" in script and st["oc"]:
            st.update(pl1=15000000, epp="balance_power", profile="balanced", oc=0)
        if "echo always > /sys/kernel/mm/transparent_hugepage/enabled" in script:
            st["thp"] = "always"
        if "echo madvise > /sys/kernel/mm/transparent_hugepage/enabled" in script:
            st["thp"] = "madvise"
        for v in re.findall(r"echo (\d+) > /proc/sys/net/core/busy_poll", script):
            st["busy"] = int(v)
        if "cat > /etc/rancher/k3s/config.yaml.d/90-nodeyard-lean.yaml" in script or "90-nodeyard-lean.conf <<" in script:
            st["lean"] = 1
        if "rm -f /etc/rancher/k3s/config.yaml.d/90-nodeyard-lean.yaml" in script:
            st["lean"] = 0
        for gov in re.findall(r'scaling_governor; do echo ([a-z_]+) > "\$f"; done', script):
            st["governor"] = gov
        for khz in re.findall(r'scaling_max_freq; do echo (\d+) > "\$f"; done', script):
            st["max"] = int(khz)
        for v in re.findall(r"echo (\d+) > /proc/sys/vm/swappiness", script):
            st["swappiness"] = int(v)
        for v in re.findall(r"-pl (\d+)", script):
            st["gpu"] = int(v)
        m = re.search(r"cat > /etc/nodeyard-tune.sh <<'NYTUNE'\n(.*?)\nNYTUNE", script, re.S)
        if m:
            st["persist"], st["pscript"] = 1, m.group(1)
        elif "rm -f /etc/systemd/system/nodeyard-tune.service" in script:
            st["persist"], st["pscript"] = 0, ""
        gpu = "gpu=0, NVIDIA GeForce RTX 3060, %d.00, 100.00, 270.00, 170.00, Enabled\n" % st["gpu"] if node.endswith("3") else ""
        model = (((self._node(node).get("hw") or {}).get("hardware") or {}).get("cpu") or {}).get("model") or ""
        if model.startswith("Raspberry Pi"):
            st.setdefault("pi", "")
            for line in re.findall(r"arm_freq=\d+", script):
                st["pi"] = line
            st.setdefault("thr", "0x0")
            if "nodeyard overclock start" not in script and "sed -i" in script:
                st["pi"] = ""
            gpu += "pi_config=/boot/firmware/config.txt\npi_throttled=0x0\n" + ("pi_%s\npi_ours=1\n" % st["pi"] if st["pi"] else "")
        else:
            gpu += ("intel=1\nrapl_pl1=%(pl1)d\nrapl_pl2=28000000\nepp=%(epp)s\nepps=default performance balance_performance balance_power power\n"
                    "profile=%(profile)s\nprofiles=low-power balanced performance\n" % st) + ("oc_saved=1\n" if st["oc"] else "")
        gpu += ("thp=%(thp)s\nthp_defrags=always defer defer+madvise madvise never\nvfs_cache_pressure=100\nbusy_poll=%(busy)d\nrmem_max=212992\n"
                "cstates_deep=3\ncstates_deep_off=0\nreadahead_dev=nvme0n1\nreadahead=128\nk3s_service=%(svc)s\nk3s_rss_kb=%(rss)d\ncontainerd_rss_kb=95000\n" %
                dict(st, svc="k3s" if cp else "k3s-agent")) + ("lean_cfg=1\n" if st["lean"] else "")
        return ("governor=%(governor)s\ngovernors=performance powersave\nhw_min=800000\nhw_max=3600000\nmax=%(max)d\nturbo=%(turbo)d\nturbo_kind=intel\n"
                "swappiness=%(swappiness)d\n" % st) + "model=%s\n" % model + gpu + "persist=%d\n" % st["persist"] + (
                    "orig_saved=1\noc_saved=1\n" if st.get("orig") else "") + (
                    "persist_script=%s\n" % base64.b64encode(st["pscript"].encode()).decode() if st.get("pscript") else "")


LATER_RESTART = "systemd-run --quiet --collect --on-active=5 --unit nodeyard-restart-%(svc)s-$(date +%%s) systemctl restart %(svc)s"
LEAN_REMOVE = ("rm -f /etc/rancher/k3s/config.yaml.d/90-nodeyard-lean.yaml /etc/systemd/system/k3s.service.d/90-nodeyard-lean.conf "
               "/etc/systemd/system/k3s-agent.service.d/90-nodeyard-lean.conf; systemctl daemon-reload")
# What the machine's settings were before nodeyard first changed any of them (Restore defaults puts them back), kept on the
# node. Written once, before the first change; what an older nodeyard saved there (the overclock values) is kept.
SAVE_ORIG = r"""o=/var/lib/nodeyard-tune.orig
if ! grep -qs '^v=2$' $o; then {
  [ -f $o ] && grep -v '^v=' $o
  echo "v=2"; c=/sys/devices/system/cpu
  echo "gov=$(cat $c/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "maxf=$(cat $c/cpu0/cpufreq/scaling_max_freq 2>/dev/null)"
  [ -f $c/intel_pstate/no_turbo ] && echo "no_turbo=$(cat $c/intel_pstate/no_turbo)"
  [ -f $c/cpufreq/boost ] && echo "boost=$(cat $c/cpufreq/boost)"
  for k in vm/swappiness vm/vfs_cache_pressure net/core/busy_poll net/core/busy_read net/core/rmem_max net/core/wmem_max kernel/numa_balancing net/ipv4/tcp_rmem net/ipv4/tcp_wmem; do
    [ -f /proc/sys/$k ] && echo "sys $k $(tr '\t' ' ' < /proc/sys/$k)"; done
  echo "thp=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
  echo "thpd=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null)"
  for f in /sys/class/block/*/queue/read_ahead_kb; do d=${f#/sys/class/block/}; d=${d%%/*}; case "$d" in loop*|ram*|zram*|dm-*) continue;; esac; echo "ra $d $(cat $f)"; done
  if command -v iw >/dev/null 2>&1; then for w in /sys/class/net/*/wireless; do [ -d "$w" ] || continue; i=$(basename "$(dirname "$w")"); echo "wifi $i $(iw dev "$i" get power_save 2>/dev/null | awk '{print $NF}')"; done; fi
  if command -v ethtool >/dev/null 2>&1; then for i in $(ls /sys/class/net); do [ -e /sys/class/net/$i/device ] && [ ! -d /sys/class/net/$i/wireless ] && s=$(ethtool --show-eee "$i" 2>/dev/null | awk '/EEE status/ {print $3}') && [ -n "$s" ] && echo "eee $i $s"; done; fi
  for st in $c/cpu*/cpuidle/state*/disable; do [ -f "$st" ] && echo "cst $st $(cat "$st")"; done
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,power.limit --format=csv,noheader,nounits 2>/dev/null | awk -F', *' '{printf "nv %s %d\n", $1, $2}'
  if ! grep -qs '^pl1=' $o; then
    echo "pl1=$(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null)"
    echo "pl1mmio=$(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw 2>/dev/null)"
    echo "epp=$(cat $c/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
    echo "profile=$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)"
    for h in /sys/class/drm/card[0-9]*/device/hwmon/hwmon*; do [ -f "$h/power1_cap" ] && echo "amdcap $h $(cat "$h/power1_cap")"; done
  fi
  true   # (the group's status is its last command's: a loop that matched nothing must not stop the file being saved)
} > $o.tmp && mv $o.tmp $o; fi"""
RESTORE_ORIG = r"""o=/var/lib/nodeyard-tune.orig
if [ -f $o ]; then
  c=/sys/devices/system/cpu
  v=$(sed -n 's/^gov=//p' $o); [ -n "$v" ] && for f in $c/cpu*/cpufreq/scaling_governor; do echo "$v" > "$f"; done 2>/dev/null
  v=$(sed -n 's/^maxf=//p' $o); [ -n "$v" ] && for f in $c/cpu*/cpufreq/scaling_max_freq; do echo "$v" > "$f"; done 2>/dev/null
  v=$(sed -n 's/^no_turbo=//p' $o); [ -n "$v" ] && echo "$v" > $c/intel_pstate/no_turbo 2>/dev/null
  v=$(sed -n 's/^boost=//p' $o); [ -n "$v" ] && echo "$v" > $c/cpufreq/boost 2>/dev/null
  grep '^sys ' $o | while read -r _ k v; do echo "$v" > "/proc/sys/$k" 2>/dev/null; done
  v=$(sed -n 's/^thp=//p' $o); [ -n "$v" ] && echo "$v" > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null
  v=$(sed -n 's/^thpd=//p' $o); [ -n "$v" ] && echo "$v" > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null
  grep '^ra ' $o | while read -r _ d v; do echo "$v" > "/sys/class/block/$d/queue/read_ahead_kb" 2>/dev/null; done
  grep '^wifi ' $o | while read -r _ i v; do [ -n "$v" ] && iw dev "$i" set power_save "$v" 2>/dev/null; done
  grep '^eee ' $o | while read -r _ i v; do ethtool --set-eee "$i" eee "$([ "$v" = enabled ] && echo on || echo off)" 2>/dev/null; done
  grep '^cst ' $o | while read -r _ f v; do echo "$v" > "$f" 2>/dev/null; done
  grep '^nv ' $o | while read -r _ i w; do nvidia-smi -i "$i" -pl "$w" >/dev/null 2>&1; done
  v=$(sed -n 's/^pl1=//p' $o); [ -n "$v" ] && echo "$v" > /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null
  v=$(sed -n 's/^pl1mmio=//p' $o); [ -n "$v" ] && echo "$v" > /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw 2>/dev/null
  v=$(sed -n 's/^epp=//p' $o); [ -n "$v" ] && for f in $c/cpu*/cpufreq/energy_performance_preference; do echo "$v" > "$f"; done 2>/dev/null
  v=$(sed -n 's/^profile=//p' $o); [ -n "$v" ] && echo "$v" > /sys/firmware/acpi/platform_profile 2>/dev/null
  grep '^amdcap ' $o | while read -r _ h v; do echo "$v" > "$h/power1_cap" 2>/dev/null; done
  rm -f $o
fi; true"""
# A kept setting's block in /etc/nodeyard-tune.sh starts with "# nodeyard:KEY". Files from older versions had none:
# their lines are recognised by what they write.
LEGACY_KEYS = [("governor", r"scaling_governor"), ("max_mhz", r"scaling_max_freq"), ("turbo", r"no_turbo|cpufreq/boost"),
               ("ai_memory", r"transparent_hugepage|vfs_cache_pressure|numa_balancing"), ("swappiness", r"vm/swappiness"),
               ("ai_network", r"busy_poll|busy_read|rmem_max|wmem_max|tcp_rmem|tcp_wmem|power_save|set-eee"), ("cstates", r"cpuidle"),
               ("readahead", r"read_ahead_kb"), ("oc_pl1", r"constraint_0_power_limit_uw"), ("oc_epp", r"energy_performance_preference"),
               ("oc_profile", r"platform_profile")]


def parse_persisted(text):
    """{key: [lines]} from the boot script, in order."""
    out, key = {}, None
    for i, line in enumerate((text or "").splitlines()):
        m = re.match(r"^# nodeyard:([a-z0-9_]+(?::[A-Za-z0-9_.-]+)?)$", line.strip())
        if m:
            key = m.group(1)
            out.setdefault(key, [])
            continue
        if not line.strip() or line.startswith("#!") or line.startswith("#"):
            continue
        k = key
        if k is None:             # (an older file without markers)
            g = re.search(r"nvidia-smi -i (\d+) -pl", line)
            a = re.search(r"/sys/class/drm/(card\d+)/", line)
            k = "gpu_power:%s" % g.group(1) if g else "amd_cap:%s" % a.group(1) if a else next((k2 for k2, rx in LEGACY_KEYS if re.search(rx, line)), "legacy:%d" % i)
        out.setdefault(k, []).append(line)
    return out


def persist_script(blocks):
    """The shell that writes the boot script with these kept settings (and its service), or removes both when there are none."""
    if not blocks:
        return ("systemctl disable nodeyard-tune.service >/dev/null 2>&1; rm -f /etc/systemd/system/nodeyard-tune.service /etc/nodeyard-tune.sh; "
                "systemctl daemon-reload 2>/dev/null; true")
    body = "\n".join("# nodeyard:%s\n%s" % (k, "\n".join(v)) for k, v in blocks.items())
    return PERSIST % {"body": body}


def legacy_defaults(p, kept):
    """For an older nodeyard's kept settings with no saved originals: the kernel's usual values, for those settings only."""
    cpu, out = "/sys/devices/system/cpu", []
    if "governor" in kept:
        gov = next((g for g in ("schedutil", "ondemand", "powersave") if g in (p.get("governors") or [])), None)
        if gov:
            out.append('for f in %s/cpu*/cpufreq/scaling_governor; do echo %s > "$f"; done' % (cpu, gov))
    if "max_mhz" in kept and p.get("hw_max"):
        out.append('for f in %s/cpu*/cpufreq/scaling_max_freq; do echo %d > "$f"; done' % (cpu, p["hw_max"]))
    if "turbo" in kept and p.get("turbo_kind") in ("intel", "boost"):
        out.append("echo 0 > %s/intel_pstate/no_turbo" % cpu if p["turbo_kind"] == "intel" else "echo 1 > %s/cpufreq/boost" % cpu)
    if "swappiness" in kept or "ai_memory" in kept:
        out += ["echo 60 > /proc/sys/vm/swappiness", "echo 100 > /proc/sys/vm/vfs_cache_pressure"]
    if "ai_network" in kept:
        out += ["echo 0 > /proc/sys/net/core/busy_poll; echo 0 > /proc/sys/net/core/busy_read"]
    if "cstates" in kept:
        out.append('for st in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do echo 0 > "$st"; done 2>/dev/null')
    for k in kept:
        if k.startswith("gpu_power:"):
            g = next((x for x in p.get("gpus") or [] if x["index"] == k.split(":")[1]), None)
            if g and g.get("default"):
                out.append("nvidia-smi -i %s -pl %d" % (g["index"], int(g["default"])))
    return out


def verify(info, body, a):
    """What the machine really shows now, for each thing the script set: [{"what", "ok", "now"}]."""
    res = []

    def check(what, ok, now):
        res.append({"what": what, "ok": bool(ok), "now": now})
    if info.get("restore"):
        check("Saved originals put back", not a.get("orig_saved") and not a.get("oc_saved"), "still saved" if a.get("orig_saved") or a.get("oc_saved") else "done")
        check("Keep-after-reboot service removed", not a.get("persist"), "present" if a.get("persist") else "removed")
        return res
    keys, ak, oc = set(info.get("keys") or []), body.get("ai_kernel") or {}, body.get("oc") or {}
    if "governor" in keys:
        check("CPU governor", a.get("governor") == body["governor"], a.get("governor"))
    if "max_mhz" in keys:
        want = int(float(body["max_mhz"]) * 1000)
        check("CPU speed limit", a.get("max") and abs(a["max"] - want) <= max(1000, want // 100), "%s MHz" % ((a.get("max") or 0) // 1000))
    if "turbo" in keys:
        check("Turbo boost", a.get("turbo") == (1 if body["turbo"] else 0), "on" if a.get("turbo") else "off")
    if "swappiness" in keys and "ai_memory" not in keys:
        check("Swappiness", a.get("swappiness") == int(body["swappiness"]), a.get("swappiness"))
    for k in keys:
        if k.startswith("gpu_power:"):
            idx = k.split(":")[1]
            g = next((x for x in a.get("gpus") or [] if x["index"] == idx), None)
            want = int((body.get("gpu_power") or {}).get(idx) or (body.get("gpu_power") or {}).get(int(idx)) or 0)
            check("GPU %s power limit" % idx, g and g.get("power") is not None and abs(g["power"] - want) < 1, "%s W" % (g and g.get("power")))
        elif k.startswith("amd_cap:"):
            card = k.split(":")[1]
            g = next((x for x in a.get("amdgpus") or [] if x["card"] == card), None)
            check("%s power cap" % card, g and g.get("cap") == int((oc.get("amd_cap") or {}).get(card) or 0), "%s W" % (g and g.get("cap")))
    if "ai_memory" in keys:
        check("Huge pages and memory settings", (a.get("thp") in (None, "always")) and a.get("swappiness") == 10 and a.get("vfs_cache_pressure") == 50,
              "huge pages %s, swappiness %s" % (a.get("thp"), a.get("swappiness")))
    if "ai_network" in keys and ak.get("network"):
        check("Low-latency networking", a.get("busy_poll") == 50, "busy polling %s" % a.get("busy_poll"))
    if "cstates" in keys:
        check("Deep sleep states off", a.get("cstates_deep") and a.get("cstates_deep_off") == a.get("cstates_deep"), "%s of %s off" % (a.get("cstates_deep_off"), a.get("cstates_deep")))
    if "readahead" in keys:
        check("Read-ahead", a.get("readahead") == 4096, "%s KB" % a.get("readahead"))
    if "oc_pl1" in keys:
        check("CPU sustained power limit", a.get("rapl_pl1") and abs(a["rapl_pl1"] - int(oc["pl1_w"]) * 1000000) < 1000000, "%s W" % ((a.get("rapl_pl1") or 0) // 1000000))
    if "oc_epp" in keys:
        check("CPU energy preference", a.get("epp") == oc["epp"], a.get("epp"))
    if "oc_profile" in keys:
        check("Platform profile", a.get("profile") == oc["profile"], a.get("profile"))
    if info.get("persist"):
        kept = parse_persisted(a.get("persist_script"))
        missing = [k for k in keys if k not in kept]
        check("Kept after a reboot", a.get("persist") and not missing, "missing: " + ", ".join(missing) if missing else "kept")
    if info.get("pi"):
        check("Raspberry Pi overclock written (applies after a reboot)", a.get("pi", {}).get("config") is not None, a.get("pi", {}).get("arm_freq") or "stock")
    if info.get("lean"):
        check("Lighter Kubernetes settings written", a.get("lean"), "written" if a.get("lean") else "missing")
    return res


PI_BLOCK_REMOVE = '''f='%(f)s'; [ -f "$f.nodeyard-backup" ] || cp "$f" "$f.nodeyard-backup"; sed -i '/^# nodeyard overclock start/,/^# nodeyard overclock end/d' "$f"'''
PI_BLOCK_ADD = '''printf '\\n# nodeyard overclock start\\n[all]\\n%(lines)s\\n# nodeyard overclock end\\n' >> '%(f)s\''''
PERSIST = '''cat > /etc/nodeyard-tune.sh <<'NYTUNE'
#!/bin/sh
# Set on the nodeyard dashboard's Tuning page ("Restore defaults" there removes this).
%(body)s
NYTUNE
chmod 755 /etc/nodeyard-tune.sh
cat > /etc/systemd/system/nodeyard-tune.service <<'NYTUNE'
[Unit]
Description=nodeyard performance settings (dashboard Tuning page)
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/sh /etc/nodeyard-tune.sh
[Install]
WantedBy=multi-user.target
NYTUNE
systemctl daemon-reload && systemctl enable nodeyard-tune.service >/dev/null 2>&1'''


def register(ctx, args):
    t = Tuning(ctx, args)
    ctx.tuning = t

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(h, arg)
            except TuneError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    def probe(h, b):
        node = str(b.get("node") or "")
        if node == "all":
            for n in t._nodes():
                if n.get("ready"):
                    threading.Thread(target=t.probe, args=(n["name"],), daemon=True).start()
        else:
            threading.Thread(target=t.probe, args=(node,), daemon=True).start()
        return t.view()

    ctx.get_routes["/api/tune"] = wrap(lambda h, q: t.view())
    ctx.post_routes.update({
        "/api/tune/probe": wrap(probe),
        "/api/tune/ai": wrap(lambda h, b: t.set_ai(b)),
        "/api/tune/apply": wrap(lambda h, b: t.apply(str(b.get("node") or ""), b.get("settings") or {}, b.get("confirm") is True)),
        "/api/tune/reset-all": wrap(lambda h, b: t.reset_all(b.get("confirm") is True)),
    })
