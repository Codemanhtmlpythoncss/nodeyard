"""Hardware care: a maintenance guide for every machine, its fans (and, where the hardware really allows it, an idle fan
setting), and what a laptop does when its lid closes. The Hardware page's Maintenance, Cooling and Lid tabs.

Everything starts from facts: the node agents' readings (CPU, disks, temperatures) plus a read-only check run on demand
on the machine itself (PROBE, through the same short-lived helper pod as the Tuning page): what kind of machine it is
(DMI chassis type, Raspberry Pi model, virtual machine), memory slots, fans and whether Linux may set their speed, the
CPU's thermal throttle counters, disk health (SMART, when smartmontools is there), battery wear, and how lid events are
handled (systemd-logind or elogind, drop-ins, inhibitors, acpid). Nothing is guessed: when a fact can't be read, the
guide says so instead of assuming, and every recommendation says what it is based on.

Changes (an idle fan setting, the lid action) are shown in full first, run as root on that machine only when you say
yes, and can be undone with Restore defaults:
  - fans: on a fan whose speed Linux may set (a writable hwmon pwm, mode 644), a small nodeyard-fan service runs the
    curve: the idle speed below the idle temperature, rising to full speed at the high temperature, and above the
    emergency temperature (or if the sensor can't be read) it hands the fan straight back to the firmware's own control
    at full speed. Stopping the service (Restore defaults) gives the fan back to the firmware. On a Raspberry Pi 5 the
    same settings go into config.txt as the firmware's own fan_temp parameters (after a reboot), and the kernel's
    throttling and shutdown limits still apply. Fans run by a laptop's or mini PC's embedded controller can't be set
    from Linux: the page shows their speed and says so.
  - the lid: a logind drop-in (/etc/systemd/logind.conf.d/90-nodeyard-lid.conf), read back from the running logind.
    The battery's critical action (UPower) and the CPU's thermal limits are never touched.

  GET  /api/hwcare                     every machine: facts, guide, fans, lid
  POST /api/hwcare/probe               {node | "all"}: read a machine again
  POST /api/hwcare/fan                 {node, enabled, idle_c, idle_pct, high_c, emergency_c, min_pct, persist, confirm} or {node, restore}
  POST /api/hwcare/lid                 {node, battery, ac, docked, confirm} or {node, restore}
"""
import copy
import re
import threading
import time

import nodeexec

PROBE = r'''d=/sys/class/dmi/id
for k in sys_vendor product_name product_version chassis_type bios_date bios_version; do [ -r $d/$k ] && echo "dmi_$k=$(head -c 100 $d/$k 2>/dev/null | tr -d '\n')"; done
echo "dt_model=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')"
echo "virt=$(systemd-detect-virt 2>/dev/null)"
. /etc/os-release 2>/dev/null; echo "os=$PRETTY_NAME"
echo "init=$(cat /proc/1/comm 2>/dev/null)"
echo "mem_kb=$(awk '/^MemTotal/ {print $2}' /proc/meminfo)"
echo "swap_kb=$(awk '/^SwapTotal/ {print $2}' /proc/meminfo)"
if command -v dmidecode >/dev/null 2>&1; then
  dmidecode -t 16 2>/dev/null | awk -F': ' '/Maximum Capacity/ {print "mem_max=" $2} /Number Of Devices/ {print "mem_slots=" $2}' | head -n 2
  dmidecode -t 17 2>/dev/null | awk -F': ' '/^\tSize:/ {s=$2} /^\tForm Factor:/ {f=$2} /^\tType:/ {t=$2} /^\tSpeed:/ {p=$2} /^\tLocator:/ {l=$2}
    /^$/ {if (s != "") print "dimm=" l "|" s "|" f "|" t "|" p; s=""} END {if (s != "") print "dimm=" l "|" s "|" f "|" t "|" p}' | head -n 8
else echo "dmidecode=missing"; fi
for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name 2>/dev/null)
  for f in $h/fan*_input; do [ -r "$f" ] || continue; b=${f%_input}; echo "fan=$n|$(basename $b)|$(cat $f 2>/dev/null)|$(cat ${b}_label 2>/dev/null)"; done
  for p in $h/pwm[0-9]; do [ -e "$p" ] || continue; echo "pwm=$n|$(basename $p)|$(cat $p 2>/dev/null)|$(cat ${p}_enable 2>/dev/null)|$(stat -c %a $p 2>/dev/null)"; done
  for t in $h/temp1_crit $h/temp1_max; do [ -r "$t" ] && echo "tlimit=$n|$(basename $t)|$(cat $t 2>/dev/null)"; done
  for t in $h/temp*_input; do [ -r "$t" ] && echo "tsens=$n|$(basename ${t%_input})|$(cat ${t%_input}_label 2>/dev/null)"; done
done 2>/dev/null | head -n 40
for c in /sys/class/thermal/cooling_device*; do [ -r $c/type ] && echo "cool=$(cat $c/type)|$(cat $c/cur_state 2>/dev/null)|$(cat $c/max_state 2>/dev/null)"; done 2>/dev/null | grep -iv 'processor\|intel_powerclamp\|tcc' | head -n 6
for z in /sys/class/thermal/thermal_zone*; do [ -r $z/type ] || continue; for tp in $z/trip_point_*_type; do [ -r "$tp" ] || continue
  case "$(cat $tp)" in critical|hot|passive) echo "trip=$(cat $z/type)|$(cat $tp)|$(cat ${tp%_type}_temp 2>/dev/null)";; esac; done; done 2>/dev/null | head -n 8
t=; for f in /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count; do [ -r "$f" ] && t=$((${t:-0} + $(cat $f))); done; [ -n "$t" ] && echo "throttle_core=$t"
[ -r /sys/module/thinkpad_acpi/parameters/fan_control ] && echo "tp_fan_control=$(cat /sys/module/thinkpad_acpi/parameters/fan_control)"
command -v vcgencmd >/dev/null 2>&1 && echo "pi_throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
for f in /boot/firmware/config.txt /boot/config.txt; do [ -f "$f" ] && { echo "pi_config=$f"; grep -E '^dtparam=fan_temp[0-3]' "$f" | head -n 12 | sed 's/^/pi_fan=/'; grep -q '^# nodeyard fan start' "$f" && echo "pi_fan_ours=1"; break; }; done
command -v rpi-eeprom-update >/dev/null 2>&1 && echo "pi_eeprom=$(rpi-eeprom-update 2>/dev/null | grep -m1 -E 'BOOTLOADER: (up to date|update available)')"
for b in /sys/class/power_supply/*; do [ -r $b/type ] || continue; ty=$(cat $b/type)
  if [ "$ty" = Battery ]; then echo "bat=$(basename $b)|$(cat $b/energy_full 2>/dev/null || cat $b/charge_full 2>/dev/null)|$(cat $b/energy_full_design 2>/dev/null || cat $b/charge_full_design 2>/dev/null)|$(cat $b/cycle_count 2>/dev/null)|$(cat $b/status 2>/dev/null)|$(cat $b/capacity 2>/dev/null)|$(cat $b/charge_control_end_threshold 2>/dev/null)"
  elif [ "$ty" = Mains ]; then echo "ac=$(cat $b/online 2>/dev/null)"; fi; done 2>/dev/null
ls /proc/acpi/button/lid/*/state >/dev/null 2>&1 && echo "lid=$(cat /proc/acpi/button/lid/*/state 2>/dev/null | awk '{print $2}' | head -n 1)"
echo "sleep_states=$(cat /sys/power/state 2>/dev/null)"
grep -q 'resume=' /proc/cmdline 2>/dev/null && echo "resume_param=1"
echo "lockdown=$(cat /sys/kernel/security/lockdown 2>/dev/null | sed 's/.*\[\(.*\)\].*/\1/')"
systemctl is-active --quiet systemd-logind 2>/dev/null && echo "logind=systemd"
[ -z "$(pidof elogind 2>/dev/null)" ] || echo "logind=elogind"
pidof acpid >/dev/null 2>&1 && echo "acpid=1" && grep -lis 'button/lid' /etc/acpi/events/* 2>/dev/null | head -n 3 | sed 's/^/acpid_lid=/'
if command -v busctl >/dev/null 2>&1; then for k in HandleLidSwitch HandleLidSwitchExternalPower HandleLidSwitchDocked LidSwitchIgnoreInhibited; do
  echo "live_$k=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager $k 2>/dev/null | awk '{print $2}' | tr -d '"')"; done; fi
for f in /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf /usr/lib/systemd/logind.conf.d/*.conf /run/systemd/logind.conf.d/*.conf /etc/elogind/logind.conf /etc/elogind/logind.conf.d/*.conf; do
  [ -f "$f" ] && grep -E '^[[:space:]]*(HandleLidSwitch|HandleLidSwitchExternalPower|HandleLidSwitchDocked)[[:space:]]*=' "$f" | sed "s|^|lidcfg=$f\||"; done 2>/dev/null | head -n 12
command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --list --no-pager --no-legend 2>/dev/null | grep -i 'handle-lid-switch' | awk '{print "inhibit=" $1 "|" $NF}' | head -n 4
[ -f /etc/UPower/UPower.conf ] && echo "upower_critical=$(sed -n 's/^CriticalPowerAction=//p' /etc/UPower/UPower.conf | head -n 1)"
lsblk -dn -b -o NAME,ROTA,SIZE,TRAN,TYPE 2>/dev/null | awk '$5 == "disk" {print "disk=" $1 "|" $2 "|" $3 "|" $4}' | head -n 6
if command -v smartctl >/dev/null 2>&1; then for dv in $(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2 == "disk" {print $1}' | grep -v '^zram\|^loop' | head -n 4); do
  smartctl -H -A /dev/$dv 2>/dev/null | awk -v d=$dv '/overall-health|SMART Health Status/ {sub(/.*: */, ""); print "smart=" d "|health|" $0}
    /Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Power_On_Hours|Wear_Leveling_Count|Percent_Lifetime_Remain/ {print "smart=" d "|" $2 "|" $NF}
    /^Percentage Used:|^Media and Data Integrity Errors:|^Power On Hours:|^Available Spare:/ {v=$NF; sub(/:.*/, ""); print "smart=" d "|" $0 "|" v}' | head -n 8; done
else echo "smartctl=missing"; fi
echo "root_use=$(df -P / 2>/dev/null | awk 'NR == 2 {print $5}')"
echo "journal=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMGT]' | head -n 1)"
echo "kerrs=$(journalctl -k -b -p err --no-pager -q 2>/dev/null | grep -ciE 'i/o error|ata[0-9].*error|nvme.*(error|timeout)|mce|hardware error|thermal|throttl|under-voltage')"
command -v fwupdmgr >/dev/null 2>&1 && echo "fwupd=1"
[ -f /etc/systemd/system/nodeyard-fan.service ] && echo "fan_ours=1" && sed -n 's/^# settings: //p' /etc/nodeyard-fan.sh 2>/dev/null | sed 's/^/fan_set=/'
[ -f /etc/systemd/logind.conf.d/90-nodeyard-lid.conf ] && echo "lid_ours=1"
[ -f /etc/elogind/logind.conf.d/90-nodeyard-lid.conf ] && echo "lid_ours=1"
true'''

CHASSIS = {3: "desktop", 4: "desktop", 5: "desktop", 6: "desktop", 7: "desktop", 15: "desktop", 16: "desktop", 24: "desktop", 13: "all-in-one",
           8: "laptop", 9: "laptop", 10: "laptop", 14: "laptop", 31: "laptop", 32: "laptop", 30: "tablet",
           17: "server", 23: "server", 25: "server", 28: "server", 29: "server", 35: "mini PC", 36: "mini PC", 34: "embedded"}
LID_ACTIONS = {"suspend": "Suspend (sleep)", "hibernate": "Hibernate", "poweroff": "Shut down", "ignore": "Keep running"}
LID_DEFAULTS = {"battery": "suspend", "ac": "suspend", "docked": "ignore"}
FAN_DEFAULTS = {"idle_c": 45, "idle_pct": 35, "high_c": 70, "emergency_c": 80, "min_pct": 20}
PI_FAN_DEFAULTS = "fan_temp0=50000 (75), fan_temp1=60000 (125), fan_temp2=67500 (175), fan_temp3=75000 (250)"
DROPIN = {"systemd": "/etc/systemd/logind.conf.d/90-nodeyard-lid.conf", "elogind": "/etc/elogind/logind.conf.d/90-nodeyard-lid.conf"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


class CareError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _int(v):
    try:
        return int(str(v).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def parse_probe(text):
    """The probe's key=value lines as a dict (lists for the repeated keys)."""
    p = {"dimms": [], "fans": [], "pwms": [], "tlimits": [], "tsens": [], "cooling": [], "trips": [], "bats": [], "disks": [], "smart": {},
         "pi_fan": [], "lidcfg": [], "inhibit": [], "acpid_lid": [], "fan_set": {}}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        v = v.strip()
        f = v.split("|")
        if k == "dimm" and len(f) >= 5:
            p["dimms"].append({"slot": f[0], "size": f[1], "form": f[2], "type": f[3], "speed": f[4]})
        elif k == "fan" and len(f) >= 4:
            p["fans"].append({"chip": f[0], "fan": f[1], "rpm": _int(f[2]), "label": f[3]})
        elif k == "pwm" and len(f) >= 5 and NAME_RE.match(f[0] or "-") and re.match(r"^pwm\d$", f[1]):
            mode = f[4]
            p["pwms"].append({"chip": f[0], "pwm": f[1], "value": _int(f[2]), "enable": _int(f[3]),
                              "writable": bool(re.match(r"^[0-7]{3}$", mode) and int(mode[0]) & 2)})
        elif k == "tlimit" and len(f) >= 3:
            p["tlimits"].append({"chip": f[0], "kind": f[1], "c": (_int(f[2]) or 0) / 1000})
        elif k == "tsens" and len(f) >= 3 and NAME_RE.match(f[0] or "-") and re.match(r"^temp\d+$", f[1]):
            p["tsens"].append({"chip": f[0], "temp": f[1], "label": f[2]})
        elif k == "cool" and len(f) >= 3:
            p["cooling"].append({"type": f[0], "cur": _int(f[1]), "max": _int(f[2])})
        elif k == "trip" and len(f) >= 3:
            p["trips"].append({"zone": f[0], "kind": f[1], "c": (_int(f[2]) or 0) / 1000})
        elif k == "bat" and len(f) >= 7:
            full, design = _int(f[1]), _int(f[2])
            p["bats"].append({"name": f[0], "health": round(100.0 * full / design) if full and design else None, "cycles": _int(f[3]), "status": f[4],
                              "capacity": _int(f[5]), "limit": _int(f[6])})
        elif k == "disk" and len(f) >= 4:
            p["disks"].append({"name": f[0], "rotational": f[1] == "1", "size": _int(f[2]), "tran": f[3]})
        elif k == "smart" and len(f) >= 3:
            p["smart"].setdefault(f[0], {})[f[1].strip()] = f[2].strip()
        elif k == "pi_fan":
            m = re.match(r"^dtparam=(fan_temp[0-3](?:_hyst|_speed)?)=(\d+)$", v)
            if m:
                p["pi_fan"].append((m.group(1), int(m.group(2))))
        elif k == "lidcfg" and len(f) >= 2:
            key, _, val = f[1].partition("=")
            p["lidcfg"].append({"file": f[0], "key": key.strip(), "value": val.strip().split("#")[0].strip()})
        elif k == "inhibit" and len(f) >= 2:
            p["inhibit"].append({"who": f[0], "mode": f[1]})
        elif k == "acpid_lid":
            p["acpid_lid"].append(v)
        elif k == "fan_set":
            for kv in v.split():
                a, _, b = kv.partition("=")
                if _int(b) is not None:
                    p["fan_set"][a] = _int(b)
        elif k in ("mem_kb", "swap_kb", "throttle_core", "throttle_pkg", "kerrs"):
            p[k] = _int(v)
        elif k.startswith("dmi_"):
            p[k] = v
        elif k == "logind" and p.get("logind"):
            continue
        else:
            p[k] = v
    for w in p["pwms"]:
        if w["chip"] == "thinkpad" and p.get("tp_fan_control") != "Y":
            w["writable"] = False      # (thinkpad_acpi refuses writes unless loaded with fan_control=1)
    ct = _int(p.get("dmi_chassis_type"))
    p["chassis"] = CHASSIS.get(ct, "") if ct else ""
    model = p.get("dt_model") or " ".join(x for x in (p.get("dmi_sys_vendor"), p.get("dmi_product_name")) if x and x.lower() not in ("to be filled by o.e.m.", "default string", "system product name"))
    p["model"] = model.strip()
    if p.get("dt_model", "").startswith("Raspberry Pi"):
        p["chassis"] = "raspberry pi"
    if p.get("virt") not in (None, "", "none"):
        p["chassis"] = "virtual machine"
    p["laptop"] = p["chassis"] in ("laptop", "tablet") or bool(p.get("lid")) or (bool(p["bats"]) and p["chassis"] not in ("desktop", "server", "raspberry pi"))
    return p


# ---------------------------------------------------------------------------------------------------- the guide
def _task(title, why, steps, tools, difficulty, benefit, risks, basis):
    return {"title": title, "why": why, "steps": steps, "tools": tools, "difficulty": difficulty, "benefit": benefit, "risks": risks, "basis": basis}


ESD = "Unplug it (and take the battery out if it's removable), then touch bare metal on the case to discharge static before touching anything inside."


def guide(n, p):
    """One machine's maintenance guide: {"facts": [...], "tasks": [...], "unknown": [...]} from measurements only."""
    hw = n.get("hw") or {}
    hard = hw.get("hardware") or {}
    facts, tasks, unknown = [], [], []

    def fact(k, v, source):
        if v not in (None, ""):
            facts.append({"k": k, "v": v, "source": source})

    kind = p.get("chassis") if p else ""
    fact("Model", (p or {}).get("model") or (hard.get("board") or {}).get("model"), "firmware (DMI / device tree)")
    fact("Kind of machine", kind, "chassis type reported by the firmware")
    cpu = hard.get("cpu") or {}
    fact("CPU", cpu.get("model") or hw.get("cpu_model"), "node agent")
    gpus = [g.get("name_full") or g.get("model") for g in hard.get("gpus") or [] if not g.get("boot")] or [g.get("name") for g in hw.get("gpu_live") or []]
    fact("GPU", ", ".join(x for x in gpus if x), "node agent")
    mem = n.get("mem_total") or hw.get("mem_total")
    dimms = [d for d in (p or {}).get("dimms") or [] if d["size"] and not d["size"].lower().startswith("no module")]
    empty_slots = [d for d in (p or {}).get("dimms") or [] if d["size"].lower().startswith("no module")]
    soldered = any("row of chips" in d["form"].lower() for d in dimms)
    fact("RAM", ("%.1f GB" % (mem / 1073741824)) + (", %d module%s (%s)" % (len(dimms), "" if len(dimms) == 1 else "s", ", ".join("%s %s %s" % (d["size"], d["type"], d["speed"]) for d in dimms))
                                                     if dimms else "") + (", %d empty slot%s" % (len(empty_slots), "" if len(empty_slots) == 1 else "s") if empty_slots else "")
         + (", soldered to the board" if soldered else "") if mem else None, "kernel and SMBIOS (dmidecode)")
    for d in hard.get("disks") or []:
        fact("Disk " + d.get("name", ""), "%s, %s%s" % (d.get("kind", ""), _gb(d.get("size")), (" (" + d["model"] + ")") if d.get("model") else ""), "node agent")
    temps = sorted(hw.get("temps") or [], key=lambda t: -(t.get("c") or 0))
    if temps:
        fact("Temperatures now", ", ".join("%s %.0f °C" % (t["name"], t["c"]) for t in temps[:4]), "kernel sensors (node agent)")
    if p:
        if p["fans"]:
            fact("Fans", ", ".join("%s %s RPM" % (f["label"] or f["fan"], f["rpm"] if f["rpm"] is not None else "?") for f in p["fans"][:4]), "hwmon fan sensors")
        elif p["cooling"]:
            fact("Fans", ", ".join("%s (level %s of %s)" % (c["type"], c["cur"], c["max"]) for c in p["cooling"][:3]), "kernel cooling devices")
        fact("Operating system", p.get("os"), "/etc/os-release")
        for b in p["bats"]:
            fact("Battery " + b["name"], "%s%% of its design capacity%s" % (b["health"], ", %d charge cycles" % b["cycles"] if b["cycles"] else "") if b["health"] else b["status"],
                 "battery firmware (power_supply)")
        fact("Root disk used", p.get("root_use"), "df")
        if p.get("throttle_core") is not None:
            fact("CPU throttled for heat since boot", ("%d times (all cores)" % p["throttle_core"]) if p["throttle_core"] else "no", "CPU thermal throttle counters")
        if p.get("pi_throttled"):
            fact("Raspberry Pi throttle flags", p["pi_throttled"], "vcgencmd get_throttled")
    fact("Memory pressure", "%.1f%% of the time something waited for memory" % hw["psi_memory"] if hw.get("psi_memory") is not None else None, "kernel pressure stall info")
    if hw.get("swap_total"):
        fact("Swap in use", _gb((hw.get("swap_total") or 0) - (hw.get("swap_free") or 0)), "kernel")

    if not p:
        unknown.append("Press Check to read this machine's model, fans, memory slots, disk health and battery.")
        return {"facts": facts, "tasks": [], "unknown": unknown, "kind": ""}
    vm = kind == "virtual machine"
    if vm:
        tasks.append(_task("Look after the host, not this virtual machine", "Linux reports it runs in a virtual machine (%s): it has no fans, heatsinks or disks of its own." % p.get("virt"),
                           ["Give it more virtual CPUs or memory from the hypervisor if the pressure figures above are high.",
                            "Do the hardware maintenance on the physical host instead."], [], "Beginner",
                           "Hardware work here would change nothing.", "None.", "systemd-detect-virt"))
        return {"facts": facts, "tasks": tasks, "unknown": unknown, "kind": kind}

    hottest = temps[0]["c"] if temps else None
    use = hw.get("cpu_use")
    hot = hottest is not None and hottest >= 80
    warm_idle = hottest is not None and hottest >= 65 and (use or 0) < 25
    throttled = (p.get("throttle_core") or 0) > 0 or (p.get("pi_throttled") not in (None, "", "0x0", "0"))
    stopped_fan = [f for f in p["fans"] if f["rpm"] == 0]

    # cleaning, by kind of machine
    basis = []
    if hot:
        basis.append("it reads %.0f °C now" % hottest)
    if warm_idle:
        basis.append("it's %.0f °C while nearly idle (%s%% CPU)" % (hottest, round(use or 0)))
    if throttled:
        basis.append("it has slowed itself down for heat since it booted")
    why = ("Because " + " and ".join(basis) + ". ") if basis else "Routine: dust builds up in every fan and vent over months. "
    if kind == "laptop":
        tasks.append(_task("Clean the vents and fans from outside", why + "Most laptops can be cleaned without opening them.",
                           ["Shut it down and unplug it; let it cool for 10 minutes.",
                            "Find the intake (usually underneath) and exhaust vents (side or back).",
                            "Hold the can of compressed air upright and blow short bursts (1 second) into the exhaust vent, then the intake. Don't tip the can: the liquid propellant can freeze parts.",
                            "If you can see the fan through a vent, hold it still with a plastic toothpick so the air doesn't spin it faster than it's built for.",
                            "Wipe lint off the vent grilles with a soft brush.",
                            "Start it again and compare the temperature at idle with the figure above after 10 minutes."],
                           ["Can of compressed air (or an electric air duster)", "Soft brush", "Plastic toothpick or spudger"], "Beginner",
                           "Often 5-15 °C cooler under load when the vents were clogged; little change if they weren't.",
                           "Never use a vacuum cleaner on the vents (static). Don't open the case if it's under warranty or sealed: an authorised service can do an internal clean.",
                           "chassis type: laptop" + ("; " + ", ".join(basis) if basis else "")))
        if hot or throttled or stopped_fan:
            tasks.append(_task("Internal clean and new thermal paste (only if the outside clean didn't help)",
                               "It still runs hot after cleaning the vents: dust is often packed against the heatsink fins inside, and old thermal paste dries out.",
                               ["Look up the service manual for %s (the maker's support site) and follow its steps for removing the bottom cover." % (p.get("model") or "this laptop"),
                                ESD, "Disconnect the battery connector first.",
                                "Remove dust from the fan and the heatsink fins with a brush and short air bursts, holding the fan still.",
                                "Only if you're comfortable: remove the heatsink, clean the old paste off with isopropyl alcohol (90%+) and a lint-free cloth, apply a pea-sized dot of new paste and refit with the screws in the order the manual gives."],
                               ["Precision screwdriver set", "Plastic opening tools", "Isopropyl alcohol 90%+", "Lint-free cloths", "Thermal paste", "Anti-static wrist strap"], "Advanced",
                               "Can be 10-20 °C cooler when the paste had dried out or the fins were blocked.",
                               "Can void the warranty; clips and ribbon cables break easily; an unevenly seated heatsink makes it hotter. Don't open a swollen battery or a sealed machine.",
                               ", ".join(basis) or "measured temperatures"))
    elif kind in ("desktop", "server", "all-in-one"):
        tasks.append(_task("Dust the fans, filters and heatsinks", why,
                           ["Shut it down, switch off the power supply and unplug it.", "Take the side panel off (usually two thumbscrews at the back).",
                            ESD, "Pull out and rinse the dust filters (front, bottom, top); let them dry fully.",
                            "Hold each fan still with a finger, then blow short bursts of air through it and through the CPU heatsink fins, from the inside out.",
                            "Brush dust off the graphics card's fans and the power supply's grille (never open the power supply).",
                            "Refit, close and compare the idle temperature after 10 minutes."],
                           ["Phillips screwdriver", "Compressed air or electric duster", "Soft brush", "Anti-static wrist strap (recommended)"], "Beginner",
                           "Usually several °C cooler and quieter if there was visible dust.", "Spinning fans with air can damage their bearings; hold them still. Never open the power supply.",
                           "chassis type: %s" % kind + ("; " + ", ".join(basis) if basis else "")))
        tasks.append(_task("Check the airflow path", "Fans should move air through the case in one direction.",
                           ["Front (and bottom) fans should pull air in; back and top fans should push it out (look for the arrow on the fan frame).",
                            "Keep at least 10 cm free behind and beside the case; don't run it in a closed cupboard.",
                            "Tie loose cables away from the fans and the front intake."], ["Cable ties"], "Beginner",
                           "A few degrees lower when air was recirculating.", "None.", "chassis type: %s" % kind))
    elif kind == "mini PC":
        tasks.append(_task("Clean the vents (and the fan if the bottom cover comes off easily)", why,
                           ["Shut it down and unplug it.", "Blow short bursts of compressed air into the vents, holding the can upright.",
                            "Many mini PCs (Intel NUC and similar) have four screws in the feet; under the cover the fan is reachable: hold it still and clean it.",
                            "Make sure nothing stands on top of it or blocks its vents."],
                           ["Compressed air", "Phillips screwdriver", "Soft brush"], "Beginner", "Mini PCs heat up quickly when their small fan clogs; cleaning often brings back the full clock.",
                           "Check the warranty terms before opening; don't pull on the fan cable.", "chassis type: mini PC" + ("; " + ", ".join(basis) if basis else "")))
    elif kind == "raspberry pi":
        has_fan = bool(p["fans"]) or any("fan" in c["type"].lower() for c in p["cooling"])
        if not has_fan:
            tasks.append(_task("Fit a heatsink or a fan case", ("Because " + " and ".join(basis) + "." if basis else "It has no fan; under steady load a bare Pi slows itself at %d °C." %
                                                                 (60 if "3 Model B Plus" in p.get("model", "") else 80)),
                               ["Shut it down and unplug the power.", "Stick the heatsinks on the SoC (and the RAM/USB chips if the kit has them) after wiping them with isopropyl alcohol.",
                                "Or fit a case with a fan (on a Pi 5, the official Active Cooler clips into its mounting holes and plugs into the FAN connector)."],
                               ["Heatsink kit or fan case / Active Cooler", "Isopropyl alcohol"], "Beginner",
                               "Keeps it out of thermal throttling under long jobs (it stays at full clock).", "Make sure fan wires don't touch the GPIO pins you use.", "no fan detected"))
        else:
            tasks.append(_task("Clean the fan", why, ["Unplug the power.", "Blow dust out of the fan and heatsink fins in short bursts, holding the fan still."],
                               ["Compressed air", "Soft brush"], "Beginner", "Keeps the fan quiet and effective.", "Hold the fan still while blowing.", "fan present"))
    if kind not in ("laptop", "desktop", "server", "all-in-one", "mini PC", "raspberry pi"):
        tasks.append(_task("Clean the vents and any fan", why + "The firmware didn't say what kind of machine this is, so these are general steps.",
                           ["Shut it down and unplug it.", "Blow short bursts of compressed air through its vents (can upright), holding any visible fan still.",
                            "Keep its vents clear of walls, cables and other machines."], ["Compressed air", "Soft brush"], "Beginner",
                           "Cooler and quieter if dust had built up.", "Don't open it unless you know how its case comes apart and the warranty allows it.",
                           "chassis type not reported" + ("; " + ", ".join(basis) if basis else "")))
    if stopped_fan and (hottest or 0) >= 55:
        tasks.append(_task("Check the fan that reads 0 RPM", "%s reports 0 RPM while the machine is at %.0f °C." % (", ".join(f["label"] or f["fan"] for f in stopped_fan), hottest),
                           ["Listen and look: does it spin at all? Some fans stop on purpose at low temperatures, but not at this one.",
                            "Shut down; check the fan cable is fully seated in its header; spin it by hand: it should turn freely.",
                            "A fan that grinds, wobbles or doesn't turn needs replacing with the same size and connector."],
                           ["Torch", "Replacement fan (same size and connector)"], "Intermediate", "Prevents overheating and throttling.",
                           "Unplug before touching the fan; on laptops fan replacement is an Advanced job (see the service manual).", "hwmon fan speed 0 RPM"))
    tasks.append(_task("Check for thermal throttling", "So you know whether heat is costing speed." + (" It already has." if throttled else ""),
                       ["Run a long job (an AI answer, or `stress-ng --cpu 0 --timeout 120`).",
                        "Watch the clock speed and temperature on this node's page while it runs: a clock that drops while the temperature sits at its limit means throttling.",
                        "On a Raspberry Pi: `vcgencmd get_throttled` (0x0 means never; bit 0x40000 or 0x4 means it throttled).",
                        "On Intel: `grep . /sys/devices/system/cpu/cpu*/thermal_throttle/*_count` (numbers that grow during the test)."],
                       ["Nothing (the dashboard shows the clock and temperature)"], "Beginner", "Tells you whether cooling work is worth doing.", "None.",
                       "CPU throttle counters" if p.get("throttle_core") is not None or p.get("pi_throttled") else "general"))

    # memory
    psi = hw.get("psi_memory")
    swap_used = (hw.get("swap_total") or 0) - (hw.get("swap_free") or 0)
    pressured = (psi is not None and psi >= 5) or (hw.get("oom_new") or 0) > 0 or swap_used > 512 * 1048576
    if pressured:
        if soldered and not empty_slots:
            tasks.append(_task("Memory is short, and it can't be upgraded", "The memory is soldered to the board (SMBIOS: Row of chips).",
                               ["Free memory instead: Processes › Free up memory lists what this machine doesn't need.", "Move memory-hungry pods to a machine with more RAM."],
                               [], "Beginner", "Fewer stalls.", "None.", "SMBIOS form factor; memory pressure %s" % psi))
        else:
            slot_txt = ("It has %d empty slot%s, so a module can be added." % (len(empty_slots), "" if len(empty_slots) == 1 else "s")) if empty_slots else (
                "All slots are full: replace modules with bigger ones (in matched pairs)." if dimms else "dmidecode isn't installed, so the slots couldn't be read (apt install dmidecode).")
            typ = dimms[0]["type"] + " " + dimms[0]["speed"] if dimms else "the same type and speed as the installed memory"
            tasks.append(_task("Add memory", "It is short of memory: %s. %s" % (
                ", ".join(x for x in ("something waited for memory %.0f%% of the time" % psi if psi is not None and psi >= 5 else "",
                                      "%s of swap in use" % _gb(swap_used) if swap_used > 512 * 1048576 else "",
                                      "the kernel stopped programs for lack of memory" if hw.get("oom_new") else "") if x), slot_txt),
                ["Find the exact module type: %s%s." % (typ, " (max %s for this board)" % p["mem_max"] if p.get("mem_max") else ""),
                 "Shut down, unplug, discharge static (" + ESD + ")",
                 "Desktop: open the side panel; laptop: the bottom cover (check the service manual). Push the slot's clips out, insert the module at an angle (laptop) or straight down (desktop) until the clips click.",
                 "Start it and check the new total on this node's page."],
                ["Matching memory module(s)", "Screwdriver", "Anti-static wrist strap"], "Intermediate",
                "Ends swapping stalls; for AI models more memory means bigger models fit.",
                "Wrong type won't fit or boot; laptops may need the cover removed (warranty). Mixing speeds runs all at the slowest.",
                "memory pressure, swap and OOM counters"))
    elif mem:
        tasks.append(_task("Memory: no upgrade needed now", "Nothing waited for memory long enough to matter (pressure %s%%, %s swap in use)." % (psi if psi is not None else "?", _gb(swap_used)),
                           ["Check again after adding workloads: Processes shows what uses memory."], [], "Beginner", "None needed.", "None.", "kernel pressure stall info"))

    # storage
    for d in hard.get("disks") or []:
        k = (d.get("kind") or "")
        if k == "HDD":
            tasks.append(_task("Replace the hard disk with an SSD", "%s is a spinning hard disk (rotational) and holds the system or data." % d.get("name"),
                               ["Buy an SSD at least as big as the data on it (SATA 2.5\" fits where the HDD was; NVMe if the board has an M.2 slot).",
                                "Clone the disk with Clonezilla or `dd`, or reinstall and copy the data.",
                                "Fit it (" + ESD + ") and check it boots."], ["SSD", "USB-SATA adapter (for cloning)", "Screwdriver"], "Intermediate",
                               "Boot, image pulls and model loading several times faster.", "Back up first; cloning to the wrong disk erases it.", "rotational flag from the kernel"))
        elif k.startswith("SD"):
            tasks.append(_task("Move the system off the SD card", "It runs from an SD card / eMMC: slow, and SD cards wear out under a busy system's writes.",
                               ["Pi 4/5: boot from a USB SSD (or an NVMe HAT on a Pi 5): flash it with Raspberry Pi Imager, set the boot order with `sudo raspi-config` › Advanced › Boot Order.",
                                "Keep a backup image of the card."], ["USB 3 SSD or NVMe HAT", "Raspberry Pi Imager"], "Intermediate",
                               "Faster and far less likely to corrupt.", "Re-imaging erases the target drive.", "disk kind from the node agent"))
    smart = p.get("smart") or {}
    for dev, s in smart.items():
        bad = []
        for key in ("Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable", "Media and Data Integrity Errors"):
            if _int(s.get(key)):
                bad.append("%s %s" % (key.replace("_", " "), s[key]))
        used = _int((s.get("Percentage Used") or "").rstrip("%"))
        health = s.get("health", "")
        if (health and not re.search(r"PASSED|OK", health, re.I)) or bad or (used is not None and used >= 80):
            tasks.append(_task("Back up %s and plan to replace it" % dev, "Its SMART data says: %s." % ", ".join(
                x for x in ([("overall health " + health) if health and not re.search(r"PASSED|OK", health, re.I) else ""] + bad + (["%d%% of its rated life used" % used] if used is not None and used >= 80 else [])) if x),
                ["Back up anything on it now.", "Run a long self-test: `sudo smartctl -t long /dev/%s`, then `sudo smartctl -a /dev/%s` when it finishes." % (dev, dev),
                 "Replace it if the counts grow or the test fails."], ["New disk", "Backup space"], "Intermediate",
                "Avoids losing data to a failing disk.", "Don't keep the only copy of anything on it.", "SMART (smartctl)"))
    if p.get("smartctl") == "missing" and hard.get("disks"):
        unknown.append("Disk health wasn't read: smartmontools isn't installed. `sudo apt install smartmontools` (or your distribution's package), then Check again.")
    ru = _int((p.get("root_use") or "").rstrip("%"))
    if ru is not None and ru >= 85:
        tasks.append(_task("Free disk space", "The system disk is %d%% full." % ru,
                           ["See what fills it on the Storage page (disk usage per node).", "Remove unused container images: `sudo k3s crictl rmi --prune`.",
                            "Shrink the system journal: `sudo journalctl --vacuum-size=200M` (it uses %s now)." % (p.get("journal") or "?")],
                           [], "Beginner", "Avoids failed writes and evicted pods.", "Only delete what you know isn't needed.", "df"))
    if (p.get("kerrs") or 0) > 0:
        tasks.append(_task("Look at the hardware errors in the kernel log", "The kernel logged %d disk, memory, thermal or power errors since boot." % p["kerrs"],
                           ["Run `journalctl -k -b -p err` on the machine (Terminal page) and read what they are.",
                            "Disk I/O errors: check the cable and the disk's SMART data. Under-voltage: a better power supply. Machine check (MCE): run a memory test (memtest86+)."],
                           [], "Intermediate", "Catches failing parts early.", "None.", "kernel log"))

    # battery
    for b in p["bats"]:
        if b["health"] is not None and b["health"] < 70:
            tasks.append(_task("The battery is worn (%d%% of its design capacity)" % b["health"], "Its firmware reports %d%% of the capacity it had new%s." % (
                b["health"], ", after %d cycles" % b["cycles"] if b["cycles"] else ""),
                ["For a machine that stays plugged in, it mostly matters as a UPS: replace it if it should still bridge power cuts.",
                 "Use the maker's part number for %s; many laptops need the bottom cover off (service manual)." % (p.get("model") or "this laptop"),
                 "If it ever bulges, lifts the touchpad or smells, stop using it and replace it."], ["Replacement battery", "Screwdriver"], "Intermediate",
                "Restores battery runtime.", "Lithium batteries can catch fire if punctured or bent; never charge a swollen one.", "battery firmware"))
        if b["status"] in ("Full", "Charging", "Not charging") and b.get("limit") in (None, 100) and (b["health"] or 100) < 95:
            tasks.append(_task("Limit charging on an always-plugged-in laptop", "It sits on the charger; holding a lithium battery at 100% wears it faster.",
                               ["If this laptop supports it (charge_control_end_threshold exists), set it to 80: `echo 80 | sudo tee /sys/class/power_supply/%s/charge_control_end_threshold`." % b["name"],
                                "To keep it after reboots use the vendor's tool or TLP (`START_CHARGE_THRESH_BAT0`/`STOP_CHARGE_THRESH_BAT0`)."], ["TLP (optional)"], "Beginner",
                               "Slows battery wear.", "Less runtime on battery.", "battery status and charge limit"))

    # software
    procs = ((n.get("_procs") or [])[:5])
    tasks.append(_task("Keep background programs in check", "Every program left running takes memory and CPU from the cluster.",
                       ["Open Processes › Free up memory: it lists what this machine doesn't need, with a one-click stop or swap.",
                        "Disable services you don't use: `systemctl list-unit-files --state=enabled`, then `sudo systemctl disable --now NAME`."] +
                       (["The biggest now: " + ", ".join("%s (%s)" % (x["name"], _gb(x["rss"])) for x in procs)] if procs else []),
                       [], "Beginner", "More memory for workloads.", "Don't disable something you don't recognise without looking it up.", "node agent process list"))
    if p.get("dt_model", "").startswith("Raspberry Pi") and "update available" in (p.get("pi_eeprom") or ""):
        tasks.append(_task("Update the Pi's bootloader firmware", "rpi-eeprom-update says an update is available.",
                           ["`sudo rpi-eeprom-update -a` then reboot."], [], "Beginner", "Fixes and better power/USB boot handling.",
                           "Don't remove power while it updates.", "rpi-eeprom-update"))
    elif p.get("fwupd"):
        tasks.append(_task("Check for firmware updates", "fwupd is installed, so the maker's firmware updates can be checked.",
                           ["`fwupdmgr refresh && fwupdmgr get-updates` lists them.", "Read each update's notes: install it only if it fixes something you have (stability, security, a device you use)."],
                           [], "Intermediate", "Fixes and security patches.", "A firmware update interrupted by power loss can leave a device unusable; keep it plugged in.", "fwupd present"))
    elif p.get("dmi_bios_date"):
        unknown.append("Firmware: dated %s. Check %s's support site for newer firmware, and install it only if its notes fix something you have." % (
            p["dmi_bios_date"], p.get("dmi_sys_vendor") or "the maker"))
    return {"facts": facts, "tasks": tasks, "unknown": unknown, "kind": kind}


def _gb(b):
    if not b:
        return "0 GB"
    return ("%.1f GB" % (b / 1073741824)) if b < 1 << 40 else ("%.1f TB" % (b / (1 << 40)))


# ---------------------------------------------------------------------------------------------------- fans
def fan_view(n, p):
    """What can be done with this machine's fans: {"mode": hwmon|pi5|monitor|none, "why", "channels", "current"}."""
    if not p:
        return {"mode": "unknown", "why": "Press Check to read this machine's fans."}
    hw = n.get("hw") or {}
    base = {"fans": p["fans"], "cooling": p["cooling"], "trips": p["trips"], "current": p.get("fan_set") or None, "ours": p.get("fan_ours") == "1",
            "defaults": FAN_DEFAULTS, "temp_now": hw.get("temp_c")}
    if p.get("chassis") == "virtual machine":
        return dict(base, mode="none", why="It's a virtual machine: it has no fans of its own.")
    writable = [w for w in p["pwms"] if w["writable"] and w["enable"] is not None]
    if writable:
        sensors = [t for t in p["tsens"] if t["chip"] in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal") or "pkg" in t["label"].lower() or "package" in t["label"].lower() or "tctl" in t["label"].lower()]
        crit = [t["c"] for t in p["tlimits"] if t["c"]] + [t["c"] for t in p["trips"] if t["kind"] in ("critical", "hot") and t["c"]]
        return dict(base, mode="hwmon", channels=writable, sensors=sensors or p["tsens"][:4], crit=min(crit) if crit else None,
                    why="Linux may set these fans' speed (hwmon %s). nodeyard runs the curve in a small service; if the sensor can't be read, or it passes the "
                        "emergency temperature, the fan goes straight back to the firmware's own control." % ", ".join(sorted({w["chip"] for w in writable})))
    if p.get("dt_model", "").startswith("Raspberry Pi 5") and p.get("pi_config") and any("fan" in c["type"].lower() for c in p["cooling"]):
        return dict(base, mode="pi5", pi_fan=p["pi_fan"], ours=p.get("pi_fan_ours") == "1",
                    why="The Pi 5's fan is run by its firmware from config.txt (fan_temp0-3). nodeyard writes those (taking effect after a reboot); "
                        "the kernel's own throttling and shutdown limits still apply. Defaults: " + PI_FAN_DEFAULTS + ".")
    if p["pwms"]:
        return dict(base, mode="monitor", why="Its fan controls are read-only to Linux (%s): the firmware runs the fans. Change them in the BIOS/UEFI setup%s." % (
            ", ".join(sorted({w["chip"] for w in p["pwms"]})), "; on a ThinkPad, loading thinkpad_acpi with fan_control=1 lets Linux set fan levels" if any(w["chip"] == "thinkpad" for w in p["pwms"]) else ""))
    if p["fans"] or p["cooling"]:
        return dict(base, mode="monitor", why="Its fans are run by the %s firmware (embedded controller); Linux can read their speed but not set it. "
                                              "Use the BIOS/UEFI fan settings or the maker's tool." % ("laptop's" if p.get("laptop") else "machine's"))
    return dict(base, mode="none", why="Linux sees no fan on this machine (passive cooling, or a fan without a speed sensor).")


def validate_fan(b, crit=None):
    try:
        v = {k: int(b.get(k, FAN_DEFAULTS[k])) for k in FAN_DEFAULTS}
    except (TypeError, ValueError):
        raise CareError("Temperatures and speeds must be whole numbers.")
    cap = 90 if crit is None else max(50, min(90, int(crit) - 5))
    if not 25 <= v["idle_c"] < v["high_c"] < v["emergency_c"] <= cap:
        raise CareError("Temperatures must rise: 25 ≤ idle (%d) < full speed (%d) < emergency (%d) ≤ %d °C%s." % (
            v["idle_c"], v["high_c"], v["emergency_c"], cap, " (5 °C under this chip's critical limit)" if crit else ""))
    if v["high_c"] - v["idle_c"] < 10:
        raise CareError("Leave at least 10 °C between the idle and full-speed temperatures.")
    if not 0 <= v["min_pct"] <= 100 or not 0 <= v["idle_pct"] <= 100:
        raise CareError("Speeds are 0 to 100%.")
    if v["min_pct"] < 20 and v["idle_c"] > 45:
        raise CareError("A minimum under 20% (or a fan that stops) only with an idle temperature of 45 °C or less.")
    if v["idle_pct"] < v["min_pct"]:
        raise CareError("The idle speed can't be below the minimum speed.")
    return v


FAN_SCRIPT = r'''#!/bin/sh
# nodeyard fan curve (dashboard: Hardware > Cooling). Stopping this gives the fan back to the firmware.
# settings: idle_c=%(idle_c)d idle_pct=%(idle_pct)d high_c=%(high_c)d emergency_c=%(emergency_c)d min_pct=%(min_pct)d chip=%(chip)s pwm=%(pwm)s sensor=%(schip)s/%(stemp)s
HW=${NODEYARD_HWMON:-/sys/class/hwmon}; STATE=${NODEYARD_STATE:-/var/lib}
find_dev() { for h in $HW/hwmon*; do [ "$(cat $h/name 2>/dev/null)" = "$1" ] && [ -e "$h/$2" ] && { echo "$h/$2"; return; }; done; }
P=$(find_dev %(chip)s %(pwm)s); T=$(find_dev %(schip)s %(stemp)s_input)
[ -n "$P" ] || { echo "nodeyard-fan: %(chip)s/%(pwm)s not found"; exit 1; }
E=${P}_enable; ORIG=$(cat $STATE/nodeyard-fan.orig 2>/dev/null || echo 2)
firmware() { echo "$ORIG" > "$E" 2>/dev/null || echo 2 > "$E" 2>/dev/null; echo 255 > "$P" 2>/dev/null; }
trap 'firmware; exit 0' TERM INT
IDLE=$((%(idle_pct)d * 255 / 100)); MIN=$((%(min_pct)d * 255 / 100))
while :; do
  t=$(cat "$T" 2>/dev/null)
  case "$t" in ''|*[!0-9-]*) firmware; logger -t nodeyard-fan "sensor unreadable: fan back to the firmware"; sleep 15; continue;; esac
  c=$((t / 1000))
  if [ "$c" -ge %(emergency_c)d ]; then
    firmware; logger -t nodeyard-fan "$c C >= %(emergency_c)d C: fan back to the firmware at full speed"
    while t=$(cat "$T" 2>/dev/null) && [ -n "$t" ] && [ $((t / 1000)) -ge %(release_c)d ]; do sleep 10; done
    continue
  fi
  if [ "$c" -le %(idle_c)d ]; then v=$IDLE; elif [ "$c" -ge %(high_c)d ]; then v=255; else v=$((IDLE + (255 - IDLE) * (c - %(idle_c)d) / (%(high_c)d - %(idle_c)d))); fi
  [ "$v" -lt "$MIN" ] && v=$MIN
  [ "$(cat "$E" 2>/dev/null)" = 1 ] || echo 1 > "$E" || { firmware; exit 1; }
  echo "$v" > "$P"
  sleep 5
done
'''
FAN_UNIT = '''[Unit]
Description=nodeyard fan curve (dashboard Hardware > Cooling)
After=multi-user.target
[Service]
ExecStartPre=/bin/sh -c 'h=$(for d in /sys/class/hwmon/hwmon*; do [ "$(cat $d/name 2>/dev/null)" = "%(chip)s" ] && [ -e "$d/%(pwm)s_enable" ] && echo $d; done | head -n 1); [ -f /var/lib/nodeyard-fan.orig ] || cat "$h/%(pwm)s_enable" > /var/lib/nodeyard-fan.orig'
ExecStart=/bin/sh /etc/nodeyard-fan.sh
ExecStopPost=/bin/sh -c 'for d in /sys/class/hwmon/hwmon*; do [ "$(cat $d/name 2>/dev/null)" = "%(chip)s" ] && [ -e "$d/%(pwm)s_enable" ] && { cat /var/lib/nodeyard-fan.orig > "$d/%(pwm)s_enable" 2>/dev/null || echo 2 > "$d/%(pwm)s_enable"; }; done; true'
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
'''
FAN_REMOVE = ("systemctl disable --now nodeyard-fan.service >/dev/null 2>&1; rm -f /etc/systemd/system/nodeyard-fan.service /etc/nodeyard-fan.sh /var/lib/nodeyard-fan.orig; "
              "systemctl daemon-reload")
PI_FAN_REMOVE = '''f='%(f)s'; [ -f "$f.nodeyard-backup" ] || cp "$f" "$f.nodeyard-backup"; sed -i '/^# nodeyard fan start/,/^# nodeyard fan end/d' "$f"'''


def fan_plan(n, p, b):
    fv = fan_view(n, p)
    if b.get("restore"):
        if fv["mode"] == "pi5" and p.get("pi_config"):
            return (["Remove nodeyard's fan settings from %s: the Pi's default fan curve after a reboot" % p["pi_config"]], PI_FAN_REMOVE % {"f": p["pi_config"]})
        if not fv.get("ours"):
            raise CareError("nodeyard isn't running this machine's fans.", 409)
        return (["Stop nodeyard's fan curve: the fan goes back to the firmware's own control"], FAN_REMOVE)
    if fv["mode"] not in ("hwmon", "pi5"):
        raise CareError(fv["why"], 409)
    v = validate_fan(b, fv.get("crit"))
    if fv["mode"] == "pi5":
        # the firmware's four steps: off below idle; idle speed at idle_c; rising to full at high_c (hysteresis 5 °C)
        mid = (v["idle_c"] + v["high_c"]) // 2
        steps = [(v["idle_c"], max(v["idle_pct"], v["min_pct"])), (mid, (max(v["idle_pct"], v["min_pct"]) + 100) // 2), (v["high_c"], 90), (min(v["emergency_c"], 80), 100)]
        lines = ["dtparam=fan_temp%d=%d\\ndtparam=fan_temp%d_hyst=5000\\ndtparam=fan_temp%d_speed=%d" % (i, c * 1000, i, i, round(pct * 255 / 100)) for i, (c, pct) in enumerate(steps)]
        f = p["pi_config"]
        script = (PI_FAN_REMOVE % {"f": f}) + "\nprintf '\\n# nodeyard fan start\\n[all]\\n%s\\n# nodeyard fan end\\n' >> '%s'" % ("\\n".join(lines), f)
        says = ["Fan at %d%% from %d °C, %d%% at %d °C, 90%% at %d °C and full speed from %d °C (in %s; after a reboot; a backup is kept)" % (
            steps[0][1], steps[0][0], steps[1][1], steps[1][0], steps[2][0], steps[3][0], f),
            "The firmware still slows the CPU from 80 °C (more at 85 °C) whatever this says" + "".join(
                "; this Pi's kernel shuts down at %d °C" % t["c"] for t in p["trips"][:1] if t["kind"] == "critical")]
        return says, script
    ch = b.get("channel") or "%s/%s" % (fv["channels"][0]["chip"], fv["channels"][0]["pwm"])
    chip, _, pwm = str(ch).partition("/")
    if not any(w["chip"] == chip and w["pwm"] == pwm for w in fv["channels"]):
        raise CareError("That fan channel isn't one Linux may set on this machine.")
    sens = b.get("sensor") or ("%s/%s" % (fv["sensors"][0]["chip"], fv["sensors"][0]["temp"]) if fv.get("sensors") else "")
    schip, _, stemp = str(sens).partition("/")
    if not any(t["chip"] == schip and t["temp"] == stemp for t in p["tsens"]):
        raise CareError("Pick the temperature sensor the curve follows.")
    if not (NAME_RE.match(chip) and re.match(r"^pwm\d$", pwm) and NAME_RE.match(schip) and re.match(r"^temp\d+$", stemp)):
        raise CareError("Unexpected fan or sensor name.")
    vals = dict(v, chip=chip, pwm=pwm, schip=schip, stemp=stemp, release_c=v["emergency_c"] - 10)
    script = ("cat > /etc/nodeyard-fan.sh <<'NYFAN'\n%sNYFAN\nchmod 755 /etc/nodeyard-fan.sh\ncat > /etc/systemd/system/nodeyard-fan.service <<'NYFAN'\n%sNYFAN\n"
              "systemctl daemon-reload && systemctl %s nodeyard-fan.service && systemctl restart nodeyard-fan.service; sleep 3; systemctl is-active nodeyard-fan.service") % (
        FAN_SCRIPT % vals, FAN_UNIT % vals, "enable" if b.get("persist", True) else "disable")
    says = ["%s/%s at %d%% up to %d °C (never under %d%%), rising to full speed at %d °C, following %s/%s" % (chip, pwm, v["idle_pct"], v["idle_c"], v["min_pct"], v["high_c"], schip, stemp),
            "From %d °C, or if the sensor can't be read, the fan goes back to the firmware's control at full speed (until it's under %d °C)" % (v["emergency_c"], v["emergency_c"] - 10),
            "Kept after reboots (a nodeyard-fan service)" if b.get("persist", True) else "Until the next reboot"]
    return says, script


# ---------------------------------------------------------------------------------------------------- the lid
def lid_view(p):
    if not p:
        return {"supported": False, "why": "Press Check to read this machine."}
    if not p.get("laptop"):
        return {"supported": False, "laptop": False, "why": "It isn't a laptop (no lid switch, chassis type %s)." % (p.get("chassis") or "unknown")}
    mgr = p.get("logind") or ""
    states = (p.get("sleep_states") or "").split()
    bat = p["bats"][0] if p["bats"] else None
    swap_ok = (p.get("swap_kb") or 0) >= (p.get("mem_kb") or 1)
    hib_why = []
    if "disk" not in states:
        hib_why.append("the kernel doesn't offer hibernation")
    if not swap_ok:
        hib_why.append("swap is smaller than the RAM")
    if p.get("lockdown") in ("integrity", "confidentiality"):
        hib_why.append("Secure Boot lockdown blocks it")
    if not p.get("resume_param"):
        hib_why.append("no resume= kernel parameter, so it may not find the image when it starts")
    actions = {"suspend": "mem" in states or "freeze" in states, "hibernate": not hib_why, "poweroff": True, "ignore": True}
    live = {k: p.get("live_" + k) for k in ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked")}
    conflicts = []
    ours = DROPIN.get(mgr, "")
    for c in p["lidcfg"]:
        if c["file"] != ours and c["file"].rsplit("/", 1)[-1] > "90-nodeyard-lid.conf" and ".conf.d/" in c["file"]:
            conflicts.append("%s sets %s=%s and is read after nodeyard's file, so it wins" % (c["file"], c["key"], c["value"]))
    for i in p["inhibit"]:
        conflicts.append("%s holds a lid-switch lock (%s): while it runs, it decides what the lid does (change it in its own power settings)" % (i["who"], i["mode"]))
    for f in p["acpid_lid"]:
        conflicts.append("acpid handles the lid too (%s): it may suspend regardless; remove or edit that rule" % f)
    if not mgr:
        why = "Neither systemd-logind nor elogind is running here, so there is no standard lid setting to change%s." % (
            " (acpid handles lid events: edit its rule in /etc/acpi/events)" if p.get("acpid") else "")
        return {"supported": False, "laptop": True, "why": why, "conflicts": conflicts}
    return {"supported": True, "laptop": True, "manager": "systemd-logind" if mgr == "systemd" else "elogind", "actions": actions, "hibernate_why": hib_why,
            "live": live, "ours": p.get("lid_ours") == "1", "conflicts": conflicts, "lid": p.get("lid"), "ac": p.get("ac"),
            "battery": bat, "upower_critical": p.get("upower_critical"), "defaults": LID_DEFAULTS, "labels": LID_ACTIONS}


def lid_plan(p, b):
    lv = lid_view(p)
    if not lv.get("supported"):
        raise CareError(lv.get("why") or "Not supported here.", 409)
    path = DROPIN["systemd" if lv["manager"] == "systemd-logind" else "elogind"]
    svc = "systemd-logind" if lv["manager"] == "systemd-logind" else "elogind"
    reload_ = ("systemctl kill -s HUP %s 2>/dev/null || pkill -HUP -x %s; sleep 1" % (svc, svc)) if svc == "systemd-logind" else "pkill -HUP -x elogind; sleep 1"
    verify = ("for k in HandleLidSwitch HandleLidSwitchExternalPower HandleLidSwitchDocked; do echo \"live_$k=$(busctl get-property org.freedesktop.login1 "
              "/org/freedesktop/login1 org.freedesktop.login1.Manager $k 2>/dev/null | awk '{print $2}' | tr -d '\"')\"; done")
    if b.get("restore"):
        return (["Remove nodeyard's lid setting (%s): back to the system's own (suspend on battery and on power, nothing when docked, unless your other config says otherwise)" % path],
                "rm -f %s\n%s\n%s" % (path, reload_, verify))
    want = {}
    for k in ("battery", "ac", "docked"):
        a = b.get(k, LID_DEFAULTS[k])
        if a not in LID_ACTIONS:
            raise CareError("Pick %s for the lid." % ", ".join(LID_ACTIONS))
        if not lv["actions"].get(a):
            raise CareError("%s isn't available here: %s." % (LID_ACTIONS[a], "; ".join(lv["hibernate_why"]) if a == "hibernate" else "the kernel doesn't offer it"))
        want[k] = a
    body = ("# Set on the nodeyard dashboard (Hardware > Lid). Restore defaults there removes this file.\n[Login]\nHandleLidSwitch=%s\n"
            "HandleLidSwitchExternalPower=%s\nHandleLidSwitchDocked=%s\n") % (want["battery"], want["ac"], want["docked"])
    script = "mkdir -p %s\ncat > %s.tmp <<'NYLID'\n%sNYLID\nmv -f %s.tmp %s\n%s\n%s" % (path.rsplit("/", 1)[0], path, body, path, path, reload_, verify)
    says = ["Lid closed on battery: %s" % LID_ACTIONS[want["battery"]], "Lid closed on mains power: %s" % LID_ACTIONS[want["ac"]],
            "Lid closed with an external screen or dock: %s" % LID_ACTIONS[want["docked"]],
            "Written to %s (kept after reboots) and read by %s now" % (path, lv["manager"])]
    return says, script, want


class Care:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.lock = threading.Lock()
        self.probes = {}
        self.demo_state = {}

    def _nodes(self):
        return (self.ctx.store.snapshot().get("state") or {}).get("nodes") or []

    def _node(self, name):
        n = next((x for x in self._nodes() if x["name"] == name), None)
        if n is None:
            raise CareError("No such machine.", 404)
        return n

    def _exec(self, node, script):
        if self.demo:
            return demo_exec(self, node, script)
        try:
            return nodeexec.run(self.kubeconfig, node, script, host=True, deadline=90, kind="hwcare")
        except nodeexec.NodeExecError as e:
            raise CareError(str(e), 502)

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
        except CareError as e:
            with self.lock:
                self.probes[node] = dict(self.probes.get(node) or {}, error=str(e), busy=False)

    def _probe_of(self, node, max_age=None):
        with self.lock:
            pr = copy.deepcopy(self.probes.get(node) or {})
        if not pr.get("data"):
            raise CareError("Check this machine first.", 409)
        if max_age and time.time() - pr["at"] > max_age:
            self.probe(node)
            return self._probe_of(node)
        return pr["data"]

    def view(self):
        out = []
        ag = getattr(self.ctx.store, "agents", None)
        with self.lock:
            probes = copy.deepcopy(self.probes)
        for n in self._nodes():
            pr = probes.get(n["name"]) or {}
            p = pr.get("data")
            nn = dict(n, _procs=self._procs(n["name"], ag))
            out.append({"node": n["name"], "ready": bool(n.get("ready")), "os": n.get("os"), "probe_at": pr.get("at"), "probe_error": pr.get("error", ""),
                        "probing": bool(pr.get("busy")), "agent": bool(n.get("hw")), "guide": guide(nn, p), "fan": fan_view(n, p), "lid": lid_view(p)})
        return {"nodes": out, "demo": self.demo}

    @staticmethod
    def _procs(node, ag):
        try:
            data = (ag.payloads or {}).get(node) if ag else None
            return sorted(({"name": x.get("name"), "rss": x.get("rss") or 0} for x in (data or {}).get("processes") or []), key=lambda x: -x["rss"])
        except Exception:  # noqa: BLE001 -- a hint, not a requirement
            return []

    def fan(self, body):
        node = str(body.get("node") or "")
        n = self._node(node)
        p = self._probe_of(node, max_age=600)
        says, script = fan_plan(n, p, body)
        if body.get("confirm") is not True:
            return {"plan": says, "script": script}
        out = self._exec(node, script + "\n" + PROBE)
        data = parse_probe(out)
        with self.lock:
            self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        fv = fan_view(n, data)
        if not body.get("restore") and fv["mode"] == "hwmon" and not re.search(r"^active$", out, re.M):
            return {"done": says, "warning": "The fan service didn't report itself running: check `journalctl -u nodeyard-fan` on %s." % node}
        if not body.get("restore") and fv["mode"] == "hwmon":
            says = says + ["Checked: the nodeyard-fan service is running"]
        return {"done": says}

    def lid(self, body):
        node = str(body.get("node") or "")
        self._node(node)
        p = self._probe_of(node, max_age=600)
        plan = lid_plan(p, body)
        says, script = plan[0], plan[1]
        if body.get("confirm") is not True:
            return {"plan": says, "script": script}
        out = self._exec(node, script + "\n" + PROBE)
        data = parse_probe(out)
        with self.lock:
            self.probes[node] = {"at": time.time(), "data": data, "error": "", "busy": False}
        if body.get("restore"):
            return {"done": says}
        want = plan[2]
        live = {"battery": data.get("live_HandleLidSwitch"), "ac": data.get("live_HandleLidSwitchExternalPower"), "docked": data.get("live_HandleLidSwitchDocked")}
        if all(v in (None, "") for v in live.values()):
            return {"done": says, "warning": "Saved, but the running logind couldn't be asked to confirm it. It applies after a reboot."}
        wrong = [k for k in want if live.get(k) and live[k] != want[k]]
        if wrong:
            return {"done": says, "warning": "Saved for the next reboot, but the running logind still says %s (an older logind that doesn't reload on SIGHUP, or "
                                             "another file overrides it: see Conflicts)." % ", ".join("%s=%s" % (k, live[k]) for k in wrong)}
        return {"done": says + ["Checked: the running logind now uses these settings"]}


def demo_exec(care, node, script):
    """The demo's machines, with what was set remembered."""
    st = care.demo_state.setdefault(node, {"fan": None, "lid": None, "pifan": None})
    if "cat > /etc/nodeyard-fan.sh" in script:
        m = re.search(r"# settings: (.*)", script)
        st["fan"] = m.group(1) if m else ""
    if "rm -f /etc/systemd/system/nodeyard-fan.service" in script:
        st["fan"] = None
    if "# nodeyard fan start" in script:
        st["pifan"] = re.findall(r"dtparam=fan_temp\d(?:_hyst|_speed)?=\d+", script)
    elif "/^# nodeyard fan start/" in script:
        st["pifan"] = None
    m = re.search(r"HandleLidSwitch=(\w+)\nHandleLidSwitchExternalPower=(\w+)\nHandleLidSwitchDocked=(\w+)", script)
    if m:
        st["lid"] = m.groups()
    if script.startswith("rm -f /etc/systemd/logind.conf.d/90-nodeyard-lid.conf"):
        st["lid"] = None
    i = int(re.sub(r"\D", "", node) or 0)
    lines = ["os=Debian GNU/Linux 12 (bookworm)", "init=systemd", "root_use=%d%%" % (40 + 9 * i), "journal=512.0M", "kerrs=0", "smartctl=missing" if i == 2 else "fwupd=1"]
    if i in (1, 2):
        pi5 = i == 1
        lines += ["dt_model=Raspberry Pi %s Model B Rev 1.%d" % ("5" if pi5 else "4", 0 if pi5 else 4), "virt=none", "mem_kb=8131400", "swap_kb=0",
                  "pi_throttled=0x0", "pi_config=/boot/firmware/config.txt", "disk=%s|0|%d|" % ("nvme0n1" if pi5 else "mmcblk0", 256 * 10 ** 9 if pi5 else 64 * 10 ** 9),
                  "pi_eeprom=BOOTLOADER: up to date", "trip=cpu-thermal|critical|110000", "dmidecode=missing"]
        if pi5:   # (the Pi 5's fan is the firmware's: a cooling device, not a free pwm)
            lines += ["fan=pwmfan|fan1|2210|", "cool=pwm-fan|1|4", "tsens=cpu_thermal|temp1|"]
            lines += ["pi_fan=" + x for x in (st["pifan"] or [])] + (["pi_fan_ours=1"] if st["pifan"] else [])
    else:
        laptop = i == 4
        lines += ["dmi_sys_vendor=%s" % ("LENOVO" if laptop else "Micro-Star International Co., Ltd."), "dmi_product_name=%s" % ("ThinkPad T460" if laptop else "MS-7C02"),
                  "dmi_chassis_type=%d" % (10 if laptop else 3), "dmi_bios_date=%s" % ("03/14/2019" if laptop else "07/27/2020"), "virt=none",
                  "mem_kb=%d" % (16 * 1024 * 1024 if laptop else 32 * 1024 * 1024), "swap_kb=%d" % (2 * 1024 * 1024),
                  "mem_max=32 GB", "mem_slots=%d" % (1 if laptop else 2),
                  "dimm=ChannelA-DIMM0|16 GB|SODIMM|DDR4|2133 MT/s" if laptop else "dimm=ChannelA-DIMM0|16 GB|SODIMM|DDR4|2400 MT/s",
                  "" if laptop else "dimm=ChannelB-DIMM0|16 GB|SODIMM|DDR4|2400 MT/s", "throttle_core=%d" % (37 if laptop else 0), "throttle_pkg=0",
                  "disk=%s|0|%d|%s" % ("sda" if laptop else "nvme0n1", 256 * 10 ** 9 if laptop else 512 * 10 ** 9, "sata" if laptop else "nvme"),
                  "smart=%s|health|PASSED" % ("sda" if laptop else "nvme0n1"), "smart=nvme0n1|Percentage Used|4%" if not laptop else "smart=sda|Reallocated_Sector_Ct|0",
                  "tsens=coretemp|temp1|Package id 0", "tsens=coretemp|temp2|Core 0", "tlimit=coretemp|temp1_crit|100000", "trip=x86_pkg_temp|passive|95000",
                  "fan=%s|fan1|%d|" % ("thinkpad" if laptop else "nct6798", 2650 if laptop else 1180)]
        if laptop:
            lines += ["pwm=thinkpad|pwm1|128|2|644", "tp_fan_control=N", "bat=BAT0|17120000|23480000|612|Full|100|", "ac=1", "lid=open",
                      "sleep_states=freeze mem disk", "lockdown=none", "logind=systemd", "upower_critical=HybridSleep",
                      "lidcfg=/etc/systemd/logind.conf|HandleLidSwitch=suspend"]
            lid = st["lid"] or ("suspend", "suspend", "ignore")
            lines += ["live_HandleLidSwitch=%s" % lid[0], "live_HandleLidSwitchExternalPower=%s" % lid[1], "live_HandleLidSwitchDocked=%s" % lid[2], "live_LidSwitchIgnoreInhibited=yes"]
            if st["lid"]:
                lines.append("lid_ours=1")
        else:
            lines += ["pwm=nct6798|pwm2|102|5|644", "tsens=nct6798|temp1|SYSTIN", "sleep_states=freeze mem disk", "logind=systemd"]
            if st["fan"] is not None:
                lines += ["fan_ours=1", "fan_set=" + " ".join(x for x in st["fan"].split() if "=" in x and x.split("=")[1].isdigit())]
    out = "\n".join(x for x in lines if x) + "\n"
    if "systemctl is-active nodeyard-fan.service" in script:
        out = "active\n" + out
    return out


def register(ctx, args):
    c = Care(ctx, args)
    ctx.hwcare = c

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(h, arg)
            except CareError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    def probe(h, b):
        node = str(b.get("node") or "")
        names = [n["name"] for n in c._nodes() if n.get("ready")] if node == "all" else [c._node(node)["name"]]
        for name in names:
            threading.Thread(target=c.probe, args=(name,), daemon=True).start()
        time.sleep(0.05)
        return c.view()

    ctx.get_routes["/api/hwcare"] = wrap(lambda h, q: c.view())
    ctx.post_routes.update({
        "/api/hwcare/probe": wrap(probe),
        "/api/hwcare/fan": wrap(lambda h, b: c.fan(b)),
        "/api/hwcare/lid": wrap(lambda h, b: c.lid(b)),
    })
