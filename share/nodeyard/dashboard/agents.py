"""Reading the node agents: processes, clock speeds, temperatures and the memory breakdown of every node.

The agent (share/nodeyard/agent/agent.py) runs as a read-only DaemonSet; this module asks each one,
over the pod network, with the shared token, and merges a short summary into the cluster snapshot.
"""
import concurrent.futures
import http.client
import json
import threading
import time

AGENT_NS = "nodeyard-system"
AGENT_LABEL = ("app", "nodeyard-agent")
AGENT_PORT = 9093


PCI_IDS = ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids", "/usr/share/pci.ids")
VENDORS = {"0x10de": "NVIDIA", "0x8086": "Intel", "0x1002": "AMD", "0x14e4": "Broadcom", "0x1a03": "ASPEED", "0x15ad": "VMware", "0x1234": "QEMU"}
_pci_names = {}


def pci_name(vendor, device):
    """'NVIDIA GP107M [GeForce GTX 1050 Ti Mobile]' from this machine's PCI ID list (read once per device)."""
    key = (vendor or "").lower().replace("0x", "") + ":" + (device or "").lower().replace("0x", "")
    if key in _pci_names:
        return _pci_names[key]
    v, d = key.split(":")
    name = ""
    for path in PCI_IDS:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                in_vendor, vname = False, ""
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            break
                        if line[:4].lower() == v:
                            in_vendor, vname = True, line[4:].strip()
                    elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == d:
                        name = line[5:].strip()
                        break
                if in_vendor:
                    short = VENDORS.get("0x" + v) or vname.split(" ")[0]
                    name = (short + " " + name) if name else (short + " graphics")
                    break
        except OSError:
            continue
    if not name:
        name = (VENDORS.get("0x" + v, "") + " graphics").strip()
    _pci_names[key] = name
    return name


def summarise(p):
    """The few numbers every page wants (the full payload is only fetched for the Processes view)."""
    cores = (p.get("cpu") or {}).get("cores") or []
    mhz = [c["mhz"] for c in cores if c.get("mhz")]
    maxes = [c["max"] for c in cores if c.get("max")]
    mem = p.get("memory") or {}
    total = mem.get("MemTotal") or 0
    temps = [t["c"] for t in p.get("temps") or []]
    cached = (mem.get("Cached", 0) + mem.get("SReclaimable", 0)) if mem else 0
    return {
        "load": p.get("load") or [], "cpu_model": (p.get("cpu") or {}).get("model", ""), "cores": len(cores) or None,
        "cpu_use": (p.get("cpu") or {}).get("use"), "freq_mhz": round(sum(mhz) / len(mhz)) if mhz else None, "freq_max": max(maxes) if maxes else None,
        "freq_min": min([c["min"] for c in cores if c.get("min")] or [0]) or None, "governor": next((c["governor"] for c in cores if c.get("governor")), ""),
        "temp_c": max(temps) if temps else None, "temps": [{"name": str(t.get("name", "")), "c": t["c"]} for t in p.get("temps") or [] if t.get("c") is not None],
        "undervoltage": bool((p.get("power") or {}).get("undervoltage")),
        "mem_total": total, "mem_available": mem.get("MemAvailable"), "mem_free": mem.get("MemFree"), "mem_cached": cached,
        "mem_buffers": mem.get("Buffers"), "mem_anon": mem.get("AnonPages"), "mem_shmem": mem.get("Shmem"), "mem_slab": mem.get("Slab"),
        "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree"),
        "psi_memory": ((p.get("pressure") or {}).get("memory") or {}).get("some"), "psi_cpu": ((p.get("pressure") or {}).get("cpu") or {}).get("some"),
        "psi_io": ((p.get("pressure") or {}).get("io") or {}).get("some"), "uptime": p.get("uptime"), "processes": (p.get("counts") or {}).get("total"),
        "oom_kills": (p.get("vmstat") or {}).get("oom_kill"),
        "hardware": _named(p.get("hardware")),
        "gpu_live": p.get("gpu_live") or [],
    }


def _named(hw):
    """Give every GPU a readable name."""
    for g in (hw or {}).get("gpus") or []:
        if not g.get("name_full"):
            g["name_full"] = g.get("model") or pci_name(g.get("vendor"), g.get("device"))
    return hw


class AgentPoller:
    def __init__(self, token_file):
        self.token_file = token_file
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.lock = threading.Lock()
        self.payloads = {}      # node -> full payload
        self.oom_base = {}      # node -> oom_kill count when first seen
        self.installed = False
        self.fallback = None    # node name -> other verified addresses (connections.py), tried when the pod IP doesn't answer

    def _token(self):
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _fetch(self, ip, token):
        conn = http.client.HTTPConnection(ip, AGENT_PORT, timeout=3)
        try:
            conn.request("GET", "/metrics.json", headers={"X-Agent-Token": token})
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            return json.loads(resp.read(8 * 1024 * 1024))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def _fetch_any(self, node, ip, token):
        """The agent through its pod address, else through the node's other verified addresses (e.g. Tailscale)."""
        data = self._fetch(ip, token)
        if data is None and self.fallback:
            try:
                others = [a for a in self.fallback(node) if a != ip][:2]
            except Exception:  # noqa: BLE001
                others = []
            for other in others:
                data = self._fetch(other, token)
                if data is not None:
                    break
        return data

    def annotate(self, state):
        """Ask every agent pod and add each node's summary as node["hw"]."""
        token = self._token()
        pods = [p for p in state["pods"] if p["namespace"] == AGENT_NS and p["labels"].get(AGENT_LABEL[0]) == AGENT_LABEL[1]]
        self.installed = bool(pods)
        state["agents"] = {"installed": self.installed, "ready": 0, "pods": len(pods)}
        if not pods or not token:
            return
        futs = {p["node"]: self.pool.submit(self._fetch_any, p["node"], p["ip"], token) for p in pods if p["status"] == "Running" and p["ip"] and p["node"]}
        fresh = {}
        for node, fut in futs.items():
            data = fut.result()
            if data:
                fresh[node] = data
        with self.lock:
            self.payloads = fresh
            for node, data in fresh.items():
                self.oom_base.setdefault(node, (data.get("vmstat") or {}).get("oom_kill", 0))
        for n in state["nodes"]:
            data = fresh.get(n["name"])
            if data:
                n["hw"] = summarise(data)
                base = self.oom_base.get(n["name"], 0)
                n["hw"]["oom_new"] = max(0, (n["hw"].get("oom_kills") or 0) - base)
        state["agents"]["ready"] = len(fresh)

    def payload(self, state, node=None):
        """Full payloads with each pod's uid turned into its name."""
        names = {p["uid"]: "%s/%s" % (p["namespace"], p["name"]) for p in state["pods"] if p.get("uid")}
        with self.lock:
            src = dict(self.payloads)
        out = {}
        for name, data in src.items():
            if node and name != node:
                continue
            d = json.loads(json.dumps(data))
            for item in list(d.get("processes", [])) + list(d.get("groups", [])):
                g = item.get("group", item)
                if g.get("kind") == "pod":
                    g["name"] = names.get(g.get("uid"), "pod " + (g.get("uid") or "")[:8])
                    if "group" not in item:
                        item["name"] = g["name"]
            out[name] = d
        return out
