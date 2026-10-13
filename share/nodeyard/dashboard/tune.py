"""The Tuning page: per-device settings for running AI well, and CPU/GPU power, clock and overclock controls.

AI settings (threads and memory kept free per node) are the dashboard's own: they are used the next time a split model
is run or switched from the dashboard. Power and clock settings change the machine itself: they run as root in the
node's own namespaces through a short-lived helper pod (nodeexec.py), only after the page has shown exactly what will
run and you've said yes, and every change is in the audit log. Values come from fixed lists and the hardware's own
limits, never free text. Settings last until a reboot unless "keep after reboot" installs a small systemd service;
a Raspberry Pi's overclock is written to its config.txt (a backup is kept) and needs a reboot. "Restore defaults"
undoes all of it.
"""
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
command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,name,power.limit,power.min_limit,power.max_limit,power.default_limit,persistence_mode --format=csv,noheader,nounits 2>/dev/null | sed 's/^/gpu=/'
[ -f /etc/systemd/system/nodeyard-tune.service ] && echo "persist=1" || echo "persist=0"
true'''

# Raspberry Pi overclocks, by model: (label, config lines). Moderate ones only; they need a good power supply and cooling.
PI_PRESETS = {
    "Raspberry Pi 5": [("stock", "Stock (2.4 GHz)", []), ("mild", "2.6 GHz", ["arm_freq=2600"]),
                       ("medium", "2.8 GHz (more voltage)", ["arm_freq=2800", "over_voltage_delta=25000"]),
                       ("high", "3.0 GHz (more voltage, needs active cooling)", ["arm_freq=3000", "over_voltage_delta=50000"])],
    "Raspberry Pi 4": [("stock", "Stock (1.5-1.8 GHz)", []), ("mild", "1.9 GHz", ["arm_freq=1900", "over_voltage=2"]),
                       ("medium", "2.0 GHz", ["arm_freq=2000", "over_voltage=4"]),
                       ("high", "2.1 GHz (needs active cooling)", ["arm_freq=2147", "over_voltage=6"])],
    "Raspberry Pi 3 Model B Plus": [("stock", "Stock (1.4 GHz)", []), ("mild", "1.5 GHz", ["arm_freq=1500", "over_voltage=2"])],
    "Raspberry Pi 3": [("stock", "Stock (1.2 GHz)", []), ("mild", "1.3 GHz", ["arm_freq=1300", "over_voltage=2"])],
}


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
            if len(parts) >= 7:
                def num(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                out["gpus"].append({"index": parts[0], "name": parts[1], "power": num(parts[2]), "min": num(parts[3]), "max": num(parts[4]),
                                    "default": num(parts[5]), "persistence": parts[6].lower() == "enabled"})
        elif k.startswith("pi_"):
            out["pi"][k[3:]] = v
        elif k in ("hw_min", "hw_max", "max", "swappiness", "turbo"):
            out[k] = int(v) if v.isdigit() else None
        elif k == "governors":
            out[k] = [g for g in v.split() if re.match(r"^[a-z_]+$", g)]
        else:
            out[k] = v
    out["persist"] = out.get("persist") == "1"
    return out


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
        """(what will change, as sentences; the shell script that does it). Everything is checked against the machine."""
        with self.lock:
            p = copy.deepcopy((self.probes.get(node) or {}).get("data"))
        if not p:
            raise TuneError("Read this machine's settings first.", 409)
        lines, says = [], []
        cpu = "/sys/devices/system/cpu"
        if body.get("restore"):
            govs = p.get("governors") or []
            gov = next((g for g in ("schedutil", "ondemand", "powersave") if g in govs), None)
            if gov:
                lines.append('for f in %s/cpu*/cpufreq/scaling_governor; do echo %s > "$f"; done' % (cpu, gov))
                says.append("CPU governor back to %s" % gov)
            if p.get("hw_max"):
                lines.append('for f in %s/cpu*/cpufreq/scaling_max_freq; do echo %d > "$f"; done' % (cpu, p["hw_max"]))
                says.append("CPU speed limit back to the maximum (%.2f GHz)" % (p["hw_max"] / 1e6))
            if p.get("turbo_kind") == "intel":
                lines.append("echo 0 > %s/intel_pstate/no_turbo" % cpu)
                says.append("Turbo boost on")
            elif p.get("turbo_kind") == "boost":
                lines.append("echo 1 > %s/cpufreq/boost" % cpu)
                says.append("Turbo boost on")
            lines.append("echo 60 > /proc/sys/vm/swappiness")
            says.append("Swappiness back to 60")
            for g in p["gpus"]:
                if g["default"]:
                    lines.append("nvidia-smi -i %s -pl %d" % (g["index"], int(g["default"])))
                    says.append("GPU %s power limit back to %d W" % (g["index"], int(g["default"])))
            if p["pi"].get("config") and p["pi"].get("ours"):
                lines.append(PI_BLOCK_REMOVE % {"f": p["pi"]["config"]})
                says.append("Remove the Raspberry Pi overclock from %s (takes effect after a reboot)" % p["pi"]["config"])
            lines.append("systemctl disable nodeyard-tune.service >/dev/null 2>&1; rm -f /etc/systemd/system/nodeyard-tune.service /etc/nodeyard-tune.sh; "
                         "systemctl daemon-reload 2>/dev/null; true")
            says.append("Remove the keep-after-reboot service")
            return says, "\n".join(lines)
        runtime = []
        gov = body.get("governor")
        if gov:
            if gov not in (p.get("governors") or []):
                raise TuneError("This machine doesn't offer the %s governor." % gov)
            runtime.append('for f in %s/cpu*/cpufreq/scaling_governor; do echo %s > "$f"; done' % (cpu, gov))
            says.append("CPU governor: %s" % gov)
        if body.get("max_mhz") not in (None, ""):
            try:
                khz = int(float(body["max_mhz"]) * 1000)
            except (TypeError, ValueError):
                raise TuneError("The speed limit must be a number of MHz.")
            if not (p.get("hw_min") and p.get("hw_max") and p["hw_min"] <= khz <= p["hw_max"]):
                raise TuneError("The speed limit must be between %d and %d MHz." % ((p.get("hw_min") or 0) // 1000, (p.get("hw_max") or 0) // 1000))
            runtime.append('for f in %s/cpu*/cpufreq/scaling_max_freq; do echo %d > "$f"; done' % (cpu, khz))
            says.append("CPU speed limit: %d MHz" % (khz // 1000))
        if body.get("turbo") in (True, False):
            if p.get("turbo_kind") == "intel":
                runtime.append("echo %d > %s/intel_pstate/no_turbo" % (0 if body["turbo"] else 1, cpu))
            elif p.get("turbo_kind") == "boost":
                runtime.append("echo %d > %s/cpufreq/boost" % (1 if body["turbo"] else 0, cpu))
            else:
                raise TuneError("This machine has no turbo boost switch.")
            says.append("Turbo boost %s" % ("on" if body["turbo"] else "off"))
        if body.get("swappiness") not in (None, ""):
            try:
                sw = int(body["swappiness"])
            except (TypeError, ValueError):
                raise TuneError("Swappiness must be a number.")
            if not 0 <= sw <= 100:
                raise TuneError("Swappiness must be between 0 and 100.")
            runtime.append("echo %d > /proc/sys/vm/swappiness" % sw)
            says.append("Swappiness: %d" % sw)
        for idx, watts in (body.get("gpu_power") or {}).items():
            g = next((x for x in p["gpus"] if x["index"] == str(idx)), None)
            try:
                w = int(watts)
            except (TypeError, ValueError):
                raise TuneError("A GPU power limit must be a number of watts.")
            if not g or g["min"] is None or g["max"] is None or not g["min"] <= w <= g["max"]:
                raise TuneError("GPU %s's power limit must be between %s and %s W." % (idx, g and g["min"], g and g["max"]))
            runtime.append("nvidia-smi -pm 1 >/dev/null; nvidia-smi -i %s -pl %d" % (g["index"], w))
            says.append("GPU %s (%s) power limit: %d W" % (g["index"], g["name"], w))
        pi = body.get("pi_preset")
        if pi:
            presets = pi_presets(p.get("model"))
            pick = next((x for x in presets if x[0] == pi), None)
            if not pick or not p["pi"].get("config"):
                raise TuneError("There's no %s overclock for this machine." % pi)
            if (self._node(node).get("hw") or {}).get("undervoltage") and pi != "stock":
                raise TuneError("This Pi reports low supply voltage: overclocking it would make it crash. Fix the power supply first.")
            lines.append(PI_BLOCK_REMOVE % {"f": p["pi"]["config"]})
            if pick[2]:
                lines.append(PI_BLOCK_ADD % {"f": p["pi"]["config"], "lines": "\\n".join(pick[2])})
            says.append("Raspberry Pi clock: %s (in %s; takes effect after a reboot; a backup is kept)" % (pick[1], p["pi"]["config"]))
        if not runtime and not lines:
            raise TuneError("Nothing to change.")
        script = "\n".join(lines + runtime)
        if body.get("persist") and runtime:
            script += "\n" + PERSIST % {"body": "\n".join(runtime).replace("'", "")}
            says.append("Keep these after a reboot (a small nodeyard-tune service)")
        return says, script

    def _node(self, name):
        return next((n for n in self._nodes() if n["name"] == name), {})

    def apply(self, node, body, confirm):
        says, script = self.plan(node, body)
        if not confirm:
            return {"plan": says, "script": script}
        out = self._exec(node, script + "\n" + PROBE)
        data = parse_probe(out)
        with self.lock:
            self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        return {"done": says}

    # -- what to suggest ------------------------------------------------------------------------------------------
    def recommendations(self, n, probe, ai, rpc_threads):
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
                "tips": self.recommendations(n, data, ai.get(n["name"]) or {}, rpc_threads),
            })
        return {"nodes": out, "demo": self.demo, "agents": bool((st.get("agents") or {}).get("installed"))}

    # -- the demo: a believable machine that remembers what you set -----------------------------------------------
    def _demo_exec(self, node, script):
        st = self.demo_state.setdefault(node, {"governor": "powersave", "max": 3600000, "turbo": 1, "swappiness": 60, "gpu": 220, "persist": 0})
        for gov in re.findall(r'echo ([a-z_]+) > "\$f"; done', script):
            st["governor"] = gov
        for khz in re.findall(r'echo (\d+) > "\$f"; done', script):
            st["max"] = int(khz)
        for v in re.findall(r"echo (\d+) > /proc/sys/vm/swappiness", script):
            st["swappiness"] = int(v)
        for v in re.findall(r"-pl (\d+)", script):
            st["gpu"] = int(v)
        if "nodeyard-tune.service" in script:
            st["persist"] = 0 if "rm -f /etc/systemd" in script else 1
        gpu = "gpu=0, NVIDIA GeForce RTX 3060, %d.00, 100.00, 270.00, 170.00, Enabled\n" % st["gpu"] if node.endswith("3") else ""
        model = (((self._node(node).get("hw") or {}).get("hardware") or {}).get("cpu") or {}).get("model") or ""
        if model.startswith("Raspberry Pi"):
            st.setdefault("pi", "")
            for line in re.findall(r"arm_freq=\d+", script):
                st["pi"] = line
            if "nodeyard overclock start" not in script and "sed -i" in script:
                st["pi"] = ""
            gpu += "pi_config=/boot/firmware/config.txt\npi_throttled=0x0\n" + ("pi_%s\npi_ours=1\n" % st["pi"] if st["pi"] else "")
        return ("governor=%(governor)s\ngovernors=performance powersave\nhw_min=800000\nhw_max=3600000\nmax=%(max)d\nturbo=%(turbo)d\nturbo_kind=intel\n"
                "swappiness=%(swappiness)d\n" % st) + "model=%s\n" % model + gpu + "persist=%d\n" % st["persist"]


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
    })
