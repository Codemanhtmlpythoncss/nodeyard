#!/usr/bin/env python3
"""nodeyard node agent: read-only facts about the machine it runs on.

Processes, per-core clock speeds, temperatures, load, a memory breakdown, pressure and
OOM counters, read from /proc and /sys (the pod runs with the host's process view). It never
changes anything and only answers requests that carry the shared token.

    agent.py [--port 9093] [--proc /proc] [--sys /sys] [--passwd /host/passwd]

Standard library only (Python 3.8+).
"""
import argparse
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
SAMPLE_SECONDS = 2.0
TOP_BY_MEMORY = 60
TOP_BY_CPU = 30
POD_RE = re.compile(r"pod([0-9a-f]{8}[_-][0-9a-f]{4}[_-][0-9a-f]{4}[_-][0-9a-f]{4}[_-][0-9a-f]{12})")
CONTAINER_RE = re.compile(r"(?:cri-containerd-|docker-|crio-)([0-9a-f]{64})")
SERVICE_RE = re.compile(r"/([^/]+\.service)(?:/|$)")
USER_RE = re.compile(r"/user-(\d+)\.slice")
SECRET_FLAG = re.compile(r"(--?[A-Za-z0-9_-]*(?:api[-_]?key|apikey|passw(?:or)?d|passwd|token|secret|credential|auth)[A-Za-z0-9_-]*)([=\s]+)(\S+)", re.I)
LONG_BLOB = re.compile(r"\b[A-Za-z0-9+/_=-]{40,}\b")
MEM_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapCached", "Active(file)", "Inactive(file)", "Active(anon)", "Inactive(anon)",
            "Dirty", "Shmem", "Slab", "SReclaimable", "SUnreclaim", "Mapped", "AnonPages", "KernelStack", "PageTables", "SwapTotal", "SwapFree")


def read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def redact(cmd):
    """Keep secrets that appear on command lines out of what this agent reports."""
    cmd = SECRET_FLAG.sub(lambda m: "%s%s***" % (m.group(1), m.group(2)), cmd)
    return LONG_BLOB.sub("***", cmd)[:240]


def parse_meminfo(text):
    out = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        if k in MEM_KEYS:
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[k] = int(parts[0]) * 1024
    return out


def parse_cpu_times(text):
    """{'all': (busy, total), 0: (busy, total), ...} in clock ticks."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        vals = [int(x) for x in parts[1:] if x.isdigit()]
        if len(vals) < 4:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
        key = "all" if parts[0] == "cpu" else int(parts[0][3:])
        out[key] = (total - idle, total)
    return out


def parse_pressure(text):
    """'some avg10=0.00 avg60=...' -> {'some': 0.0, 'full': 0.0} (avg10)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            for p in parts[1:]:
                if p.startswith("avg10="):
                    try:
                        out[parts[0]] = float(p[6:])
                    except ValueError:
                        pass
    return out


def parse_stat_line(text):
    """/proc/PID/stat -> (comm, state, ppid, utime+stime, threads, starttime) or None."""
    try:
        head, _, tail = text.rpartition(")")
        comm = head.partition("(")[2]
        f = tail.split()
        return comm, f[0], int(f[1]), int(f[11]) + int(f[12]), int(f[17]), int(f[19])
    except (IndexError, ValueError):
        return None


def parse_status(text):
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if k in ("Uid", "VmRSS", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads"):
            parts = v.split()
            if parts:
                out[k] = int(parts[0]) * (1024 if k.startswith(("Vm", "Rss")) and len(parts) > 1 else 1) if parts[0].isdigit() else 0
                if k == "Uid":
                    out[k] = int(parts[0]) if parts[0].isdigit() else 0
    return out


def parse_group(cgroup_text, comm, ppid, pid):
    """Where a process lives: a pod, a system service, a user session or the kernel."""
    if pid == 2 or ppid == 2:
        return {"kind": "kernel", "name": "kernel threads"}
    for line in cgroup_text.splitlines():
        m = POD_RE.search(line)
        if m:
            c = CONTAINER_RE.search(line)
            return {"kind": "pod", "uid": m.group(1).replace("_", "-"), "container": c.group(1)[:12] if c else ""}
    for line in cgroup_text.splitlines():
        s = SERVICE_RE.search(line)
        if s:
            return {"kind": "service", "name": s.group(1)}
        u = USER_RE.search(line)
        if u:
            return {"kind": "user", "name": "user session (uid %s)" % u.group(1)}
    return {"kind": "system", "name": "system"}


def read_users(path):
    users = {}
    for line in read(path).splitlines():
        p = line.split(":")
        if len(p) > 2 and p[2].isdigit():
            users[int(p[2])] = p[0]
    return users


def read_cpu_hw(sys_root):
    base = os.path.join(sys_root, "devices/system/cpu")
    cores = []
    try:
        names = sorted((n for n in os.listdir(base) if re.match(r"^cpu\d+$", n)), key=lambda n: int(n[3:]))
    except OSError:
        return cores
    for n in names:
        d = os.path.join(base, n, "cpufreq")

        def khz(f):
            v = read(os.path.join(d, f)).strip()
            return int(v) // 1000 if v.isdigit() else None
        cores.append({"id": int(n[3:]), "mhz": khz("scaling_cur_freq") or khz("cpuinfo_cur_freq"), "min": khz("cpuinfo_min_freq"),
                      "max": khz("cpuinfo_max_freq"), "governor": read(os.path.join(d, "scaling_governor")).strip(),
                      "online": read(os.path.join(base, n, "online"), "1").strip() != "0"})
    return cores


def read_temps(sys_root):
    out = []
    zones = os.path.join(sys_root, "class/thermal")
    try:
        for z in sorted(os.listdir(zones)):
            if z.startswith("thermal_zone"):
                t = read(os.path.join(zones, z, "temp")).strip()
                if re.match(r"^-?\d+$", t):
                    out.append({"name": read(os.path.join(zones, z, "type")).strip() or z, "c": int(t) / 1000.0})
    except OSError:
        pass
    hw = os.path.join(sys_root, "class/hwmon")
    try:
        for h in sorted(os.listdir(hw)):
            name = read(os.path.join(hw, h, "name")).strip() or h
            for f in sorted(os.listdir(os.path.join(hw, h))):
                if re.match(r"^temp\d+_input$", f):
                    t = read(os.path.join(hw, h, f)).strip()
                    if re.match(r"^-?\d+$", t) and not any(o["name"] == name for o in out):
                        out.append({"name": name, "c": int(t) / 1000.0})
    except OSError:
        pass
    return out


def read_power_flags(sys_root):
    """Raspberry Pi: the firmware's under-voltage alarm, if the machine reports it."""
    hw = os.path.join(sys_root, "class/hwmon")
    try:
        for h in os.listdir(hw):
            if read(os.path.join(hw, h, "name")).strip() == "rpi_volt":
                return {"undervoltage": read(os.path.join(hw, h, "in0_lcrit_alarm")).strip() == "1"}
    except OSError:
        pass
    return {}


def cpu_model(text):
    model = hardware = ""
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "model name" and not model:
            model = v
        elif k == "Model" and not model:
            model = v
        elif k == "Hardware" and not hardware:
            hardware = v
    return model or hardware


FLAGS_OF_NOTE = ("sse4_2", "avx", "avx2", "fma", "f16c", "avx512f", "avx512_vnni", "avx_vnni", "amx_tile", "aes", "sha_ni",
                 "asimd", "neon", "asimddp", "sve", "sve2", "i8mm", "bf16", "fp16", "asimdhp")


ARM_IMPL = {"0x41": "ARM", "0x61": "Apple", "0x51": "Qualcomm", "0x48": "HiSilicon"}
ARM_PARTS = {"0xd03": "Cortex-A53", "0xd04": "Cortex-A35", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
             "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76", "0xd0d": "Cortex-A77", "0xd41": "Cortex-A78",
             "0xd44": "Cortex-X1", "0xd46": "Cortex-A510", "0xd47": "Cortex-A710", "0xd48": "Cortex-X2", "0xd0c": "Neoverse-N1",
             "0xd40": "Neoverse-V1", "0xd49": "Neoverse-N2", "0xc07": "Cortex-A7", "0xc0f": "Cortex-A15"}


NVIDIA_QUERY = ("index,pci.bus_id,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,"
                "power.draw,power.limit,clocks.sm,clocks.max.sm,fan.speed")


def read_nvidia_live():
    """Live numbers for NVIDIA GPUs from nvidia-smi. It is only there in the GPU flavour of
    the agent pod (NVIDIA's container runtime adds it); elsewhere this returns []."""
    import shutil
    import subprocess
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=" + NVIDIA_QUERY, "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    def num(v):
        try:
            return float(v)
        except ValueError:
            return None
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 13:
            continue
        gpus.append({"index": int(num(f[0]) or 0), "bus": f[1].lower().replace("00000000:", "0000:"), "name": f[2], "use": num(f[3]), "mem_use": num(f[4]),
                     "mem_used": int((num(f[5]) or 0) * 1048576), "mem_total": int((num(f[6]) or 0) * 1048576), "temp_c": num(f[7]),
                     "power_w": num(f[8]), "power_limit_w": num(f[9]), "clock_mhz": num(f[10]), "clock_max_mhz": num(f[11]), "fan": num(f[12])})
    return gpus


def read_gpus(proc, sys_root):
    """Every graphics device on the PCI bus (class 0x03): what it is, which driver runs it,
    and for NVIDIA cards the model and driver version from the driver's own files."""
    out = []
    base = os.path.join(sys_root, "bus/pci/devices")
    nv_version = ""
    m = re.search(r"Kernel Module\s+([0-9.]+)", read(os.path.join(proc, "driver/nvidia/version")))
    if m:
        nv_version = m.group(1)
    try:
        devs = sorted(os.listdir(base))
    except OSError:
        devs = []
    for bus in devs:
        d = os.path.join(base, bus)
        cls = read(os.path.join(d, "class")).strip()
        if not cls.startswith("0x03"):
            continue
        drv = os.path.basename(os.path.realpath(os.path.join(d, "driver"))) if os.path.exists(os.path.join(d, "driver")) else ""
        g = {"bus": bus, "vendor": read(os.path.join(d, "vendor")).strip(), "device": read(os.path.join(d, "device")).strip(), "driver": drv,
             "boot": read(os.path.join(d, "boot_vga")).strip() == "1", "model": "", "driver_version": "", "vram": None}
        info = read(os.path.join(proc, "driver/nvidia/gpus", bus, "information"))
        mm = re.search(r"^Model:\s*(.+)$", info, re.M)
        if mm:
            g["model"], g["driver_version"] = mm.group(1).strip(), nv_version
        vram = read(os.path.join(d, "mem_info_vram_total")).strip()  # AMD (amdgpu)
        if vram.isdigit():
            g["vram"] = int(vram)
        out.append(g)
    return out


def iface_max_mbps(name):
    """The fastest speed the network card (and its driver) supports, via the ethtool
    'get settings' call, which needs no privileges. None if it can't tell."""
    try:
        import array
        import fcntl
        import struct
        data = array.array("B", struct.pack("I", 1) + b"\0" * 40)  # ETHTOOL_GSET
        addr, _ = data.buffer_info()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            fcntl.ioctl(s.fileno(), 0x8946, struct.pack("16sP", name[:15].encode(), addr))  # SIOCETHTOOL
        supported = struct.unpack_from("I", data, 4)[0]
    except (OSError, ImportError, struct.error):
        return None
    if not supported:
        return None
    for bit, mbps in ((1 << 12, 10000), (1 << 15, 2500), (1 << 5, 1000), (1 << 4, 1000), (1 << 3, 100), (1 << 2, 100), (1 << 1, 10), (1 << 0, 10)):
        if supported & bit:
            return mbps
    return None


def iface_ipv4(name):
    """The interface's IPv4 address (SIOCGIFADDR), or ''."""
    try:
        import fcntl
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            return socket.inet_ntoa(res[20:24])
    except (OSError, ImportError):
        return ""


def size_text(t):
    """'32K' / '6144K' / '8M' (sysfs cache sizes) -> bytes"""
    m = re.match(r"^(\d+)\s*([KMG]?)", t.strip())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "K": 1024, "M": 1048576, "G": 1073741824}[m.group(2)]


def read_hardware(proc, sys_root):
    """What the machine is: CPU, caches, instruction sets, board, disks, network links, GPUs.
    Everything here is world-readable; nothing needs root."""
    cpuinfo = read(os.path.join(proc, "cpuinfo"))
    flags, vendor, bogo, cores_ids, impl, part = set(), "", None, set(), "", ""
    phys = None
    for line in cpuinfo.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("flags", "Features"):
            flags.update(v.split())
        elif k == "vendor_id" and not vendor:
            vendor = v
        elif k == "CPU implementer" and not impl:
            impl = v
        elif k == "CPU part" and not part:
            part = v
        elif k.lower() == "bogomips" and bogo is None:
            try:
                bogo = float(v)
            except ValueError:
                pass
        elif k == "physical id":
            phys = v
        elif k == "core id":
            cores_ids.add((phys, v))
    base = os.path.join(sys_root, "devices/system/cpu")
    try:
        threads = len([n for n in os.listdir(base) if re.match(r"^cpu\d+$", n)])
    except OSError:
        threads = os.cpu_count() or 0
    if not cores_ids:  # Arm: no core ids in cpuinfo; count the sibling groups instead
        sib = set()
        for i in range(threads):
            t = read(os.path.join(base, "cpu%d" % i, "topology/thread_siblings_list")).strip()
            if t:
                sib.add(t)
        physical = len(sib) or threads
    else:
        physical = len(cores_ids)
    maxes = [int(read(os.path.join(base, "cpu%d" % i, "cpufreq/cpuinfo_max_freq")).strip() or 0) // 1000 for i in range(threads)
             if read(os.path.join(base, "cpu%d" % i, "cpufreq/cpuinfo_max_freq")).strip().isdigit()]
    mins = [int(read(os.path.join(base, "cpu%d" % i, "cpufreq/cpuinfo_min_freq")).strip()) // 1000 for i in range(threads)
            if read(os.path.join(base, "cpu%d" % i, "cpufreq/cpuinfo_min_freq")).strip().isdigit()]
    caches = []
    cdir = os.path.join(base, "cpu0/cache")
    try:
        for idx in sorted(os.listdir(cdir)):
            d = os.path.join(cdir, idx)
            if not idx.startswith("index"):
                continue
            size = size_text(read(os.path.join(d, "size")))
            shared = read(os.path.join(d, "shared_cpu_list")).strip()
            caches.append({"level": int(read(os.path.join(d, "level")).strip() or 0), "type": read(os.path.join(d, "type")).strip(), "size": size, "shared": shared})
    except OSError:
        pass
    dmi = lambda f: read(os.path.join(sys_root, "class/dmi/id", f)).strip()  # noqa: E731
    model = read(os.path.join(proc, "device-tree/model")).replace("\x00", "").strip() or read(os.path.join(sys_root, "firmware/devicetree/base/model")).replace("\x00", "").strip()
    board = {"model": model or " ".join(x for x in (dmi("sys_vendor"), dmi("product_name")) if x and "O.E.M" not in x and "Default" not in x),
             "board": " ".join(x for x in (dmi("board_vendor"), dmi("board_name")) if x), "bios": dmi("bios_version"), "bios_date": dmi("bios_date")}
    disks = []
    bdir = os.path.join(sys_root, "block")
    try:
        for name in sorted(os.listdir(bdir)):
            if re.match(r"^(loop|ram|zram|dm-|sr|md|fd|nbd)", name):
                continue
            d = os.path.join(bdir, name)
            sectors = read(os.path.join(d, "size")).strip()
            size = int(sectors) * 512 if sectors.isdigit() else 0
            if size <= 0:
                continue
            rot = read(os.path.join(d, "queue/rotational")).strip() == "1"
            path = os.path.realpath(d)
            kind = "NVMe SSD" if name.startswith("nvme") else "SD card / eMMC" if name.startswith("mmcblk") else "HDD" if rot else "SSD"
            usb = "/usb" in path
            sched = re.search(r"\[(\w[\w-]*)\]", read(os.path.join(d, "queue/scheduler")))
            disks.append({"name": name, "size": size, "kind": kind + (" (USB)" if usb else ""),
                          "model": (read(os.path.join(d, "device/model")) or read(os.path.join(d, "device/name"))).strip(),
                          "vendor": read(os.path.join(d, "device/vendor")).strip(), "removable": read(os.path.join(d, "removable")).strip() == "1",
                          "scheduler": sched.group(1) if sched else ""})
    except OSError:
        pass
    nics = []
    ndir = os.path.join(sys_root, "class/net")
    try:
        for name in sorted(os.listdir(ndir)):
            if re.match(r"^(lo|veth|cni|flannel|docker|br-|virbr|kube|cali|vxlan|tunl|dummy)", name):
                continue
            d = os.path.join(ndir, name)
            if not os.path.exists(os.path.join(d, "device")) and not name.startswith(("tailscale", "wg")):
                continue  # virtual
            sp = read(os.path.join(d, "speed")).strip()
            ip4 = iface_ipv4(name)
            drv = os.path.basename(os.path.realpath(os.path.join(d, "device/driver")))
            maxs = iface_max_mbps(name)
            if maxs is None and drv in ("smsc95xx",):  # Raspberry Pi 1-3B: 100 Mbit/s hardware
                maxs = 100
            nics.append({"name": name, "ipv4": ip4, "cluster": bool(ip4) and ip4 == os.environ.get("NODE_IP", ""),
                         "max_mbps": maxs, "driver": drv if drv != "driver" else "", "wireless": os.path.isdir(os.path.join(d, "wireless")) or name.startswith("wl"),
                         "speed_mbps": int(sp) if re.match(r"^\d+$", sp) and int(sp) > 0 else None,
                         "up": read(os.path.join(d, "operstate")).strip() in ("up", "unknown"), "mtu": int(read(os.path.join(d, "mtu")).strip() or 0)})
    except OSError:
        pass
    gpus = read_gpus(proc, sys_root)
    mem = parse_meminfo(read(os.path.join(proc, "meminfo")))
    core_name = ARM_PARTS.get(part.lower(), "") if impl.lower() == "0x41" else ""
    return {"cpu": {"model": cpu_model(cpuinfo), "core": core_name, "vendor": vendor or ARM_IMPL.get(impl.lower(), impl), "arch": os.uname().machine, "cores": physical, "threads": threads,
                    "max_mhz": max(maxes) if maxes else None, "min_mhz": min(mins) if mins else None, "bogomips": bogo, "caches": caches,
                    "flags": [f for f in FLAGS_OF_NOTE if f in flags]},
            "memory": {"total": mem.get("MemTotal"), "swap": mem.get("SwapTotal")}, "board": board, "disks": disks, "nics": nics, "gpus": gpus,
            "kernel": read(os.path.join(proc, "sys/kernel/osrelease")).strip()}


def read_vmstat(text, keys=("oom_kill", "pgmajfault", "pswpin", "pswpout")):
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k in keys and v.strip().isdigit():
            out[k] = int(v)
    return out


def read_diskstats(text):
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 14 and re.match(r"^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)$", p[2]):
            out.append({"name": p[2], "read": int(p[5]) * 512, "write": int(p[9]) * 512})
    return out


class Agent:
    def __init__(self, proc="/proc", sys_root="/sys", passwd="/etc/passwd"):
        self.proc, self.sys, self.passwd = proc, sys_root, passwd
        self.lock = threading.Lock()
        self.snapshot = None
        self.prev = None
        self.users = {}
        self.node = os.environ.get("NODE_NAME") or socket.gethostname()
        self.hardware = None
        self.hardware_at = 0

    def _p(self, *parts):
        return os.path.join(self.proc, *[str(p) for p in parts])

    def sample(self, now=None):
        now = now or time.time()
        users = self.users or read_users(self.passwd)
        self.users = users
        cpu_times = parse_cpu_times(read(self._p("stat")))
        procs = {}
        try:
            pids = [int(n) for n in os.listdir(self.proc) if n.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            st = parse_stat_line(read(self._p(pid, "stat")))
            if not st:
                continue
            statm = read(self._p(pid, "statm")).split()
            rss = int(statm[1]) * PAGE if len(statm) > 1 and statm[1].isdigit() else 0
            procs[pid] = {"comm": st[0], "state": st[1], "ppid": st[2], "ticks": st[3], "threads": st[4], "start_ticks": st[5], "rss": rss}
        prev, self.prev = self.prev, {"t": now, "cpu": cpu_times, "procs": {p: v["ticks"] for p, v in procs.items()}}
        dt = (now - prev["t"]) if prev else 0
        # per-core usage and per-process CPU since the last sample
        cores_use = {}
        total_use = None
        if prev and dt > 0:
            for k, (busy, total) in cpu_times.items():
                pb, pt = prev["cpu"].get(k, (busy, total))
                if total > pt:
                    cores_use[k] = max(0.0, min(100.0, 100.0 * (busy - pb) / (total - pt)))
            total_use = cores_use.pop("all", None)
        for pid, p in procs.items():
            before = prev["procs"].get(pid) if prev else None
            p["cpu"] = max(0.0, (p["ticks"] - before) / CLK / dt * 100.0) if before is not None and dt > 0 else 0.0

        meminfo = parse_meminfo(read(self._p("meminfo")))
        uptime = (read(self._p("uptime")).split() or ["0"])[0]
        boot = now - float(uptime) if re.match(r"^[0-9.]+$", uptime) else 0
        # details only for the processes that matter; groups for all of them
        pick = set(sorted(procs, key=lambda p: -procs[p]["rss"])[:TOP_BY_MEMORY]) | set(sorted(procs, key=lambda p: -procs[p]["cpu"])[:TOP_BY_CPU])
        groups = {}
        out = []
        for pid, p in procs.items():
            cg = read(self._p(pid, "cgroup"))
            g = parse_group(cg, p["comm"], p["ppid"], pid)
            p["group"] = g
            key = g.get("uid") or g.get("name") or g["kind"]
            agg = groups.setdefault(key, {"group": g, "rss": 0, "cpu": 0.0, "procs": 0})
            agg["rss"] += p["rss"]
            agg["cpu"] += p["cpu"]
            agg["procs"] += 1
            if pid not in pick:
                continue
            status = parse_status(read(self._p(pid, "status")))
            cmd = read(self._p(pid, "cmdline")).replace("\x00", " ").strip()
            out.append({"pid": pid, "ppid": p["ppid"], "name": p["comm"], "state": p["state"], "threads": status.get("Threads", p["threads"]),
                        "user": users.get(status.get("Uid", -1), str(status.get("Uid", ""))), "rss": status.get("VmRSS", p["rss"]),
                        "anon": status.get("RssAnon"), "file": status.get("RssFile"), "shmem": status.get("RssShmem"), "swap": status.get("VmSwap"),
                        "cpu": round(p["cpu"], 1), "cmd": redact(cmd), "group": g,
                        "started": boot + p["start_ticks"] / CLK if boot else 0})
        out.sort(key=lambda x: -x["rss"])
        cores = read_cpu_hw(self.sys)
        for c in cores:
            c["use"] = round(cores_use.get(c["id"], 0.0), 1) if c["id"] in cores_use else None
        load = [float(x) for x in read(self._p("loadavg")).split()[:3] if re.match(r"^[0-9.]+$", x)]
        counts = {"total": len(procs), "running": sum(1 for p in procs.values() if p["state"] == "R"),
                  "sleeping": sum(1 for p in procs.values() if p["state"] in ("S", "D", "I")), "zombie": sum(1 for p in procs.values() if p["state"] == "Z"),
                  "threads": sum(p["threads"] for p in procs.values())}
        if not self.hardware or now - self.hardware_at > 600:  # it hardly ever changes
            try:
                self.hardware, self.hardware_at = read_hardware(self.proc, self.sys), now
            except Exception as e:  # never let it break the live numbers
                sys.stderr.write("agent: hardware read failed: %s\n" % e)
        snap = {
            "gpu_live": read_nvidia_live(),
            "hardware": self.hardware,
            "node": self.node, "time": now, "uptime": float(uptime) if re.match(r"^[0-9.]+$", uptime) else 0, "load": load,
            "cpu": {"model": cpu_model(read(self._p("cpuinfo"))), "cores": cores, "use": round(total_use, 1) if total_use is not None else None},
            "memory": meminfo, "pressure": {k: parse_pressure(read(self._p("pressure", k))) for k in ("cpu", "memory", "io")},
            "vmstat": read_vmstat(read(self._p("vmstat"))), "disks": read_diskstats(read(self._p("diskstats"))),
            "temps": read_temps(self.sys), "power": read_power_flags(self.sys), "counts": counts,
            "files": [int(x) for x in read(self._p("sys/fs/file-nr")).split()[:1] if x.isdigit()],
            "processes": out, "groups": sorted(({"kind": a["group"]["kind"], "uid": a["group"].get("uid", ""), "name": a["group"].get("name", ""),
                                                 "rss": a["rss"], "cpu": round(a["cpu"], 1), "procs": a["procs"]} for a in groups.values()),
                                                key=lambda g: -g["rss"])[:40],
        }
        with self.lock:
            self.snapshot = snap
        return snap

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.sample()
            except Exception as e:  # keep serving the last good sample
                sys.stderr.write("agent: sample failed: %s\n" % e)
            stop.wait(SAMPLE_SECONDS)

    def get(self):
        with self.lock:
            return self.snapshot


class Handler(BaseHTTPRequestHandler):
    server_version = "nodeyard-agent"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if self.path != "/metrics.json":
            return self._send(404, '{"error":"not found"}')
        token = self.server.token
        if not token or not hmac.compare_digest(self.headers.get("X-Agent-Token", "").encode(), token.encode()):
            return self._send(401, '{"error":"token"}')
        snap = self.server.agent.get()
        if snap is None:
            return self._send(503, '{"error":"warming up"}')
        self._send(200, json.dumps(snap, separators=(",", ":")))

    def do_POST(self):
        self._send(405, '{"error":"read-only"}')

    do_PUT = do_DELETE = do_PATCH = do_POST


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9093)
    ap.add_argument("--proc", default="/proc")
    ap.add_argument("--sys", default="/sys")
    ap.add_argument("--passwd", default="/etc/passwd")
    a = ap.parse_args()
    token = os.environ.get("AGENT_TOKEN", "")
    if len(token) < 16:
        sys.exit("agent: AGENT_TOKEN is missing or too short")
    agent = Agent(a.proc, a.sys, a.passwd)
    stop = threading.Event()
    threading.Thread(target=agent.loop, args=(stop,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    srv.agent, srv.token = agent, token
    print("nodeyard agent for %s listening on :%d" % (agent.node, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
