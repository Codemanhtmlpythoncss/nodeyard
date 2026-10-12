"""The 3D galaxy screens, for k3s: a copy of homelab-galaxy-dashboard by NetworkBound (MIT, see galaxy3d/LICENSE),
fed from nodeyard's own view of the cluster instead of Proxmox.

  /g3/            the wall, for a desk monitor: the network graph with measured traffic, panels, the AI box
  /g3/nexus       the estate from orbit, for a TV across the room
  /g3/desk        the older flat HUD
  /g3/static/...  their scripts and baked 3D assets (paths rewritten to live under /g3/)
  /g3/api/...     their JSON API, answered from k3s: nodes are the galaxies ("Proxmox nodes"), pods the stars
                  ("guests", by category), nodeyard's alerts the problems, the tailnet the clients, each node's
                  measured network rate its link, the split model's RPC traffic a flow, and the AI box asks the model.

Signed-in only, like every other page; nothing here changes the cluster.
"""
import json
import mimetypes
import os
import re
import socket
import threading
import time

import connections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "galaxy3d")
PAGES = {"/g3": "wall", "/g3/": "wall", "/g3/wall": "wall", "/g3/embed": "wall", "/g3/nexus": "nexus", "/g3/desk": "desk"}
CATS = ["ai", "media", "network", "monitor", "web", "infra"]
PALETTE = {"ai": "#ff4fa3", "media": "#38e1ff", "network": "#37f5a0", "monitor": "#ffb347", "web": "#9b8cff", "infra": "#8fb0d0", "stopped": "#ff5a6a"}
ACCENTS = ["#5ad7ff", "#ffb347", "#b48cff", "#5dffa8", "#ff7aa8", "#ffd84d"]
MODES = ("wall", "showcase", "ops", "night", "alert")
SEV = {"critical": 4, "warning": 2, "info": 1}


def pod_cat(p):
    ns, name = p.get("namespace", ""), p.get("name", "")
    both = ns + "/" + name
    if ns.startswith(("ai-", "ollama")) or re.search(r"llama|ollama|^rpc-|whisper|comfy|vllm|stable-diffusion", name):
        return "ai"
    if re.search(r"jellyfin|plex|emby|sonarr|radarr|lidarr|prowlarr|immich|navidrome|tdarr|qbittorrent|transmission|frigate", both):
        return "media"
    if re.search(r"traefik|coredns|svclb|tailscale|metallb|ingress|nginx|flannel|cilium|calico|kube-proxy|llama-gate|wireguard|pihole|adguard", both):
        return "network"
    if re.search(r"metrics|prometheus|grafana|loki|alertmanager|uptime|nodeyard-agent|exporter|zabbix|netdata", both):
        return "monitor"
    if ns.startswith(("kube-", "nodeyard", "cert-manager", "local-path", "longhorn")):
        return "infra"
    return "web"


def short_name(p):
    owner = str(p.get("owner") or "")
    if "/" in owner:
        return owner.split("/", 1)[1]
    return re.sub(r"(-[a-z0-9]{8,10})?-[a-z0-9]{5}$", "", p.get("name", ""))


def vmid(p):
    h = 2166136261
    for ch in (p.get("namespace", "") + "/" + p.get("name", "")):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return 100 + h % 899900


class Galaxy3D:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.lock = threading.Condition()
        self.mode = {"mode": "wall", "id": 0, "prev": "wall", "by": "boot", "ts": 0.0, "auto": False, "reason": None}
        self.presence = {"active": True, "by": "nodeyard", "id": 0, "ts": time.time(), "hold_until": 0}
        self.light = {"rgb": [0, 128, 255], "hue": 210, "lit": 0, "id": 0}
        self.takeover = {"mode": "off", "id": 0, "secs": 120, "idle": 15, "delay": 10, "ts": 0}
        self.hist = []          # cluster network, every 30 s for two hours
        self.gpu_hist = {}      # GPU index -> samples
        threading.Thread(target=self._sampler, daemon=True).start()

    # -- what the cluster looks like -----------------------------------------------------------------------
    def snap(self):
        s = self.ctx.store.snapshot()
        return s.get("state") or {}, s.get("alerts") or [], s.get("updated") or 0

    def _act(self):
        ai = getattr(self.ctx, "ai", None)
        try:
            return ai.split_slots() if ai is not None and hasattr(ai, "split_slots") else {}
        except Exception:  # noqa: BLE001 -- no model, or it isn't answering: just nothing to show
            return {}

    def _tailnet(self):
        t = getattr(self.ctx, "terminals", None)
        try:
            return [x for x in (t.targets()["targets"] if t else []) if x.get("kind") == "tailnet"]
        except Exception:  # noqa: BLE001
            return []

    def _conn(self):
        c = getattr(self.ctx, "connections", None)
        try:
            return {d["name"]: d for d in (c.view().get("devices") or [])} if c else {}
        except Exception:  # noqa: BLE001
            return {}

    def config(self):
        st, _, _ = self.snap()
        nodes = [{"id": n["name"], "label": n["name"].upper(), "accent": ACCENTS[i % len(ACCENTS)]} for i, n in enumerate(st.get("nodes") or [])]
        name = ((st.get("cluster") or {}).get("name") or "k3s").upper()
        return {"version": "2.0.0-nodeyard", "demo": getattr(self.ctx, "mode", "") == "demo", "brand": name, "domain": socket.gethostname(),
                "tagline": "k3s · live topology · traffic · ai", "nodes": nodes, "wan_label": "INTERNET", "categories": CATS, "palette": PALETTE,
                "features": {"nexus": True, "cameras": False, "threats": False, "audience": False, "lights": False, "doom": False, "setup": False,
                             "chat": True, "snmp": False, "cinematics": True},
                "cinematics": {"laps": 3, "moves": ["roll", "explode", "closeup", "arc"]}, "addons": []}

    def guests(self, st):
        nodes = {n["name"]: n for n in st.get("nodes") or []}
        out = []
        for p in st.get("pods") or []:
            if re.search(r"Succeeded|Completed", p.get("status", "")):
                continue
            n = nodes.get(p.get("node") or "") or {}
            cores = n.get("cpu_cores") or 1
            out.append({"id": vmid(p), "name": short_name(p), "node": p.get("node") or "", "type": "pod", "cat": pod_cat(p),
                        "status": "running" if p.get("status") == "Running" else "stopped",
                        "cpu": round(100.0 * (p.get("cpu") or 0) / cores, 2), "mem": int(p.get("mem") or 0),
                        "maxmem": int(max(p.get("mem") or 0, 128 * 1048576)), "namespace": p.get("namespace", ""), "pod": p.get("name", ""),
                        "phase": p.get("status", ""), "restarts": p.get("restarts", 0)})
        return out

    def node_rows(self, st):
        out, out2 = [], []
        for n in st.get("nodes") or []:
            hw = n.get("hw") or {}
            load = hw.get("load") or [0, 0, 0]
            cpu = 100.0 * (n.get("cpu_used") or 0) / (n.get("cpu_cores") or 1)
            up = hw.get("uptime") or (time.time() - (n.get("kubelet_start") or time.time()))
            out.append({"node": n["name"], "cores": n.get("cpu_cores"), "cpu": round(cpu, 1), "load": round(load[0] if load else 0, 2),
                        "mem_total": n.get("mem_total"), "mem_used": n.get("mem_used"), "uptime": int(up), "status": "online" if n.get("ready") else "offline"})
            disk_t, disk_u = n.get("disk_total") or 0, n.get("disk_used") or 0
            out2.append({"node": n["name"], "cores": n.get("cpu_cores"), "cpu": round(cpu, 1), "kernel": n.get("kernel", ""), "load": [round(x, 2) for x in load[:3]],
                         "mem_pct": round(100.0 * (n.get("mem_used") or 0) / (n.get("mem_total") or 1), 1), "mem_total": n.get("mem_total"), "mem_used": n.get("mem_used"),
                         "rootfs_pct": round(100.0 * disk_u / disk_t, 1) if disk_t else None, "rootfs_total": disk_t, "rootfs_used": disk_u,
                         "temp": hw.get("temp_c"), "uptime": int(up), "status": "online" if n.get("ready") else "offline"})
        return out, out2

    def problems(self, st, alerts):
        hosts, recent, sev = {}, [], {}
        names = {n["name"] for n in st.get("nodes") or []}
        for a in alerts:
            # the screens call severity 4+ "HOST DOWN": only a machine that is down is that; a failing pod is a warning
            lvl = SEV.get(a.get("level"), 1)
            if lvl < 2:
                continue
            if lvl >= 4 and a.get("kind") != "node":
                lvl = 3
            ref = str(a.get("ref") or "")
            host = ref if ref in names else next((n for n in names if n in (a.get("title", "") + " " + a.get("detail", ""))), "cluster")
            h = hosts.setdefault(host, {"n": 0, "names": [], "sev": 0, "unack": 0})
            h["n"] += 1; h["unack"] += 1; h["sev"] = max(h["sev"], lvl); h["names"].append((a.get("title", "") + ": " + a.get("detail", ""))[:60])
            sev[str(lvl)] = sev.get(str(lvl), 0) + 1
            recent.append({"age": 0, "host": host, "name": a.get("title", ""), "sev": lvl})
        return {"alert_hosts": hosts, "hosts": len(names), "problems": sum(h["n"] for h in hosts.values()), "sev": sev}, {"recent": recent[:8], "version": "nodeyard"}

    def topology(self, st=None):
        if st is None:
            st, _, _ = self.snap()
        conn = self._conn()
        nodes, links, guests = [], [], self.guests(st)
        rx_all = sum((n.get("net_rx_rate") or 0) for n in st.get("nodes") or [])
        tx_all = sum((n.get("net_tx_rate") or 0) for n in st.get("nodes") or [])
        nodes.append({"id": "wan", "kind": "wan", "label": "INTERNET", "ip": "", "measured": False, "meta": {}, "parent": None, "rx": None, "tx": None, "status": "up"})
        nics = {n["name"]: connections.discover(n) for n in st.get("nodes") or []}
        wifi = sorted({a["address"].rsplit(".", 1)[0] + ".x" for f in nics.values() for a in f["lan"] if a.get("wireless")})
        wired = sorted({(n.get("internal_ip") or "").rsplit(".", 1)[0] + ".x" for n in st.get("nodes") or [] if n.get("internal_ip")})
        nodes.append({"id": "gw", "kind": "gateway", "label": "Router", "ip": ("LAN " + ", ".join(wifi)) if wifi else "", "measured": False,
                      "meta": {"model": "home router"}, "parent": "wan", "rx": None, "tx": None, "status": "up"})
        nodes.append({"id": "sw", "kind": "switch", "label": "Cluster network", "ip": ", ".join(wired), "measured": True, "meta": {"model": "k3s · flannel"},
                      "parent": "gw", "rx": rx_all, "tx": tx_all, "status": "up"})
        links.append({"source": "wan", "target": "gw", "bps": None, "capacity_bps": None, "measured": False})
        links.append({"source": "gw", "target": "sw", "bps": (rx_all + tx_all) * 8, "capacity_bps": 1e9, "measured": True})
        for n in st.get("nodes") or []:
            name, rx, tx = n["name"], n.get("net_rx_rate"), n.get("net_tx_rate")
            c = (conn.get(name) or {}).get("lan") or {}
            mine = [g for g in guests if g["node"] == name]
            # its physical addresses: the wired cluster one first, then Wi-Fi; Tailscale in the hover card
            f = nics.get(name) or {"lan": [], "tailscale": []}
            phys = [n.get("internal_ip", "")] if n.get("internal_ip") else []
            phys += [("wifi " if a.get("wireless") else "") + a["address"] for a in f["lan"] if a["address"] not in phys]
            ts = [a["address"] for a in f["tailscale"]]
            nodes.append({"id": "pve:" + name, "kind": "pvehost", "label": name, "ip": " · ".join(phys), "parent": "sw", "measured": rx is not None,
                          "rx": rx, "tx": tx, "status": "up" if n.get("ready") else "down",
                          "meta": {"guests": len(mine), "running": sum(1 for g in mine if g["status"] == "running"), "rtt": c.get("ms"), "jitter": 0,
                                   "loss": 0 if c.get("state") != "unreachable" else 100, "port": None,
                                   "model": ("tailscale " + ", ".join(ts)) if ts else "", "addresses": f["lan"] + f["tailscale"]}})
            links.append({"source": "sw", "target": "pve:" + name, "bps": ((rx or 0) + (tx or 0)) * 8 if rx is not None else None, "capacity_bps": 1e9, "measured": rx is not None})
        for g in guests:
            gid = "guest:%s:%d" % (g["node"], g["id"])
            nodes.append({"id": gid, "kind": "guest", "label": g["name"], "ip": "", "parent": "pve:" + g["node"], "measured": False, "rx": None, "tx": None,
                          "status": "up" if g["status"] == "running" else "down",
                          "meta": {"cat": g["cat"], "cpu": g["cpu"], "mem_pct": None, "type": "pod", "vmid": g["id"], "namespace": g["namespace"]}})
        clients = self._tailnet()
        for d in clients:
            nodes.append({"id": "client:" + d["address"], "kind": "client", "label": d["name"], "ip": d["address"], "parent": "gw", "measured": False,
                          "rx": None, "tx": None, "status": "up" if d.get("online") else "down",
                          "meta": {"cat": "tailscale", "hostname": d["name"], "vendor": d.get("os", ""), "wired": False, "mac": ""}})
        # the split model's own traffic: its main server and the machines holding its layers
        flows, sp = [], (st.get("ai") or {}).get("split") or {}
        act = self._act()
        main = next((p.get("node") for p in sp.get("pods") or [] if str(p.get("name", "")).startswith("llama-main")), "")
        by = {n["name"]: n for n in st.get("nodes") or []}
        if main and act.get("processing"):
            for node in sorted({s["node"].replace(" GPU", "") for s in sp.get("shares") or []} - {main}):
                b = by.get(node) or {}
                flows.append({"src": "pve:" + main, "dst": "pve:" + node, "src_label": main, "dst_label": node, "src_ip": (by.get(main) or {}).get("internal_ip", ""),
                              "dst_ip": b.get("internal_ip", ""), "bps": ((b.get("net_rx_rate") or 0) + (b.get("net_tx_rate") or 0)) * 8, "bytes": 0,
                              "proto": "llama RPC", "internal": True, "measured": b.get("net_rx_rate") is not None})
        online = [d for d in clients if d.get("online")]
        return {"nodes": nodes, "links": links, "flows": flows, "ts": time.time(),
                "totals": {"client_count": len(clients), "clients_capped": False, "clients_culled": 0, "clients_offline": len(clients) - len(online),
                           "clients_shown": len(clients), "guest_count": len(guests), "lan_bps": (rx_all + tx_all) * 8, "snmp_devices": 0, "snmp_up": 0,
                           "wan_measured": False, "wan_rx_bps": None, "wan_tx_bps": None},
                "flow_meta": {"error": None, "flow_count": len(flows), "ifid": 0, "shown": len(flows), "ts": time.time(), "up": True},
                "edge": self.edge(), "external": {"up": False, "by_country": [], "talkers": [], "top_dst": []}}

    @staticmethod
    def edge():
        return {"cloudflare": {"up": False}, "crowdsec": {"up": False}, "secmon": {"up": False}, "audience": {"up": False}, "threats": {"up": False}}

    def gpus(self, st=None):
        if st is None:
            st, _, _ = self.snap()
        out = []
        for n in st.get("nodes") or []:
            for g in (n.get("hw") or {}).get("gpu_live") or []:
                out.append({"host": n["name"], "index": len(out), "name": g.get("name", "GPU"), "util": round(g.get("use") or 0), "temp": g.get("temp_c"),
                            "mem_used": int((g.get("mem_used") or 0) / 1048576), "mem_total": int((g.get("mem_total") or 0) / 1048576), "power": g.get("power_w")})
        return out

    def ai_servers(self, st):
        sp = (st.get("ai") or {}).get("split") or {}
        if not sp:
            return []
        gpus = self.gpus(st)
        main = next((p for p in sp.get("pods") or [] if str(p.get("name", "")).startswith("llama-main")), {})
        return [{"name": sp.get("alias") or sp.get("model") or "model", "ip": "", "cat": "ai", "up": bool(sp.get("ready")), "models": 1,
                 "gpus": [g["index"] for g in gpus], "gpu_count": len(gpus), "gpu_mem_used": sum(g["mem_used"] for g in gpus),
                 "gpu_mem_total": sum(g["mem_total"] for g in gpus), "gpu_util": max([g["util"] for g in gpus] or [0]),
                 "node_exporter_up": True, "ollama_up": bool(sp.get("ready")), "src": "nodeyard", "node": main.get("node", "")}]

    def showtime(self):
        st, _, _ = self.snap()
        sp = (st.get("ai") or {}).get("split") or {}
        act = self._act()
        gpus = self.gpus(st)
        ollama = []
        if sp:
            ollama.append({"name": "split model", "up": bool(sp.get("ready")), "url": "", "available": 1, "gpus": [g["index"] for g in gpus],
                           "gpu_label": " + ".join(g["name"].replace("NVIDIA GeForce ", "") for g in gpus) or "CPU",
                           "vram_used_mb": sum(g["mem_used"] for g in gpus),
                           "loaded": [{"name": sp.get("alias") or sp.get("model", ""), "busy": bool(act.get("processing")), "ctx": int(act.get("n_ctx") or sp.get("ctx") or 0),
                                       "family": "llama.cpp", "params": "", "quant": "", "size_mb": 0, "vram_mb": sum(g["mem_used"] for g in gpus)}] if sp.get("loaded") is not False else []})
        return {"ollama": ollama, "emby": {"up": False, "configured": False, "count": 0, "playing": [], "sessions": []}, "frigate": {"up": False, "cameras": []},
                "coral": None, "gpu_series": {}, "mode": self.mode_body(), "polled": time.time(), "ts": round(time.time(), 3), "served_ms": 0.0}

    def all(self):
        st, alerts, updated = self.snap()
        guests = self.guests(st)
        nodes, nodes2 = self.node_rows(st)
        zbx, zbx2 = self.problems(st, alerts)
        conn = self._conn()
        storage = [{"name": "disk", "node": n["node"], "pct": n["rootfs_pct"] or 0, "total": n["rootfs_total"], "used": n["rootfs_used"], "type": "dir"} for n in nodes2 if n["rootfs_total"]]
        latency, rtt = [], {}
        for name, d in conn.items():
            c = d.get("lan") if (d.get("lan") or {}).get("ms") is not None else (d.get("tailscale") or {})
            ms = c.get("ms")
            latency.append({"name": name, "cat": "infra", "avg": ms, "last": ms, "min": ms, "max": ms, "spark": [ms or 0] * 20, "up": c.get("state") == "reachable"})
            if c.get("address"):
                rtt[c["address"]] = {"rtt": ms, "jitter": 0.0, "loss": 0 if c.get("state") == "reachable" else 100, "recv": 1, "sent": 1, "ts": time.time()}
        health = []
        for svc in st.get("services") or []:
            if svc.get("namespace") in ("kube-system",) and svc.get("name") == "kube-dns":
                continue
            eps = svc.get("endpoints")
            up = (eps if isinstance(eps, int) else len(eps or [])) > 0 if eps is not None else True
            health.append({"name": svc.get("name", ""), "cat": pod_cat({"namespace": svc.get("namespace", ""), "name": svc.get("name", "")}), "code": 200 if up else 503,
                           "ip": svc.get("cluster_ip", ""), "ms": None, "src": "kubernetes", "up": up})
        tail = self._tailnet()
        netmap = {"devices": [{"group": "tailscale", "ip": d["address"], "name": d["name"], "up": 1} for d in tail if d.get("online")] +
                             [{"group": "k3s node", "ip": n.get("internal_ip", ""), "name": n["name"], "up": 1 if n.get("ready") else 0} for n in st.get("nodes") or []]}
        netmap["total"] = len(netmap["devices"]); netmap["up"] = sum(d["up"] for d in netmap["devices"])
        top_cpu = sorted(guests, key=lambda g: -g["cpu"])[:5]
        top_mem = sorted(guests, key=lambda g: -g["mem"])[:5]
        totals = (self.ctx.store.snapshot().get("totals") or {})
        running = sum(1 for g in guests if g["status"] == "running")
        return {"guests": guests, "nodes": nodes, "nodes2": nodes2, "storage": storage, "pools": [], "zfs": [],
                "backups": {"archives": 0, "backed": 0, "guests": len(guests), "missing": 0, "newest_age_h": None, "stale7": 0, "storages": [], "ts": int(time.time()), "worst": []},
                "unifi": {"up": False, "clients": len([d for d in tail if d.get("online")]), "devices": 0, "isp": "—", "latency": None,
                          "wan_up_known": bool(tail),
                          "rx": round((totals.get("net_rx_rate") or 0) * 8 / 1e6, 2), "tx": round((totals.get("net_tx_rate") or 0) * 8 / 1e6, 2), "wan_ip": "", "wan_up": "ok" if any(d.get("online") for d in tail) else "n/a", "wifi": 0},
                "zabbix": zbx, "zbx2": zbx2, "cameras": [], "frigate": {"up": False},
                "sources": {"zabbix": {"up": False}, "prometheus": {"up": False}, "grafana": {"up": False},
                            "kubernetes": {"up": bool(st), "nodes": len(nodes), "pods": len(guests), "version": (st.get("cluster") or {}).get("k3s_version", "")}},
                "health": health[:20], "latency": latency, "netmap": netmap,
                "top": {"cpu": [{"name": g["name"], "node": g["node"], "v": g["cpu"]} for g in top_cpu],
                        "mem": [{"name": g["name"], "node": g["node"], "gb": round(g["mem"] / 1e9, 2), "pct": 0} for g in top_mem], "full": []},
                "certs": {"hosts": []}, "telemetry": {}, "probe": {"arp": {}, "err": None, "rtt": rtt}, "snmp": {},
                "topology": self.topology(st), "edge": self.edge(), "external": {"up": False, "by_country": [], "talkers": [], "top_dst": []},
                "addons": {}, "ai_servers": self.ai_servers(st), "running": running, "total": len(guests),
                "ts": updated or time.time(), "ts_age": round(time.time() - (updated or time.time()), 1), "ts_err": None}

    # -- the small shared states the two screens keep in step with -----------------------------------------
    def mode_body(self):
        return dict(self.mode, modes=list(MODES), alert_sev=3)

    def presence_body(self):
        return dict(self.presence, effective=True, hold_s=0, grace_s=0)

    def pulse(self):
        st, alerts, _ = self.snap()
        problems = sum(1 for a in alerts if SEV.get(a.get("level"), 1) >= 2)
        down = sum(1 for n in st.get("nodes") or [] if not n.get("ready"))
        return {"beat": int(time.time() // 4), "ts": time.time(), "sev": 3 if problems else 2 if down else 0, "problems": problems, "net_down": down}

    def threats(self):
        return {"counts": {}, "events": [], "latest": None, "level": 0, "name": "GREEN", "seq": 0, "ts": time.time()}

    def _sampler(self):
        while True:
            try:
                st, alerts, _ = self.snap()
                if st:
                    totals = self.ctx.store.snapshot().get("totals") or {}
                    self.hist.append({"ts": int(time.time()), "clients": len([d for d in self._tailnet() if d.get("online")]),
                                      "rx": round((totals.get("net_rx_rate") or 0) * 8 / 1e6, 3), "tx": round((totals.get("net_tx_rate") or 0) * 8 / 1e6, 3),
                                      "problems": sum(1 for a in alerts if SEV.get(a.get("level"), 1) >= 2)})
                    del self.hist[:-240]
                    for g in self.gpus(st):
                        h = self.gpu_hist.setdefault(g["index"], [])
                        h.append({"ts": int(time.time()), "util": g["util"], "mem": g["mem_used"], "temp": g["temp"], "power": g["power"]})
                        del h[:-240]
            except Exception:  # noqa: BLE001 -- a sample is never worth stopping for
                pass
            time.sleep(30)

    def history(self, q):
        mins = int((q.get("mins") or ["120"])[0] or 120)
        cut = time.time() - mins * 60
        metric = (q.get("metric") or [""])[0]
        if metric == "net":
            return [h for h in self.hist if h["ts"] > cut]
        if metric == "gpu":
            try:
                idx = int((q.get("idx") or ["0"])[0])
            except ValueError:
                idx = 0
            return [h for h in self.gpu_hist.get(idx, []) if h["ts"] > cut]
        return []

    # -- the AI box -----------------------------------------------------------------------------------------
    def brief(self):
        st, alerts, _ = self.snap()
        lines = []
        for n in st.get("nodes") or []:
            hw = n.get("hw") or {}
            lines.append("- %s: %s, CPU %d%% of %s cores, memory %.1f/%.1f GiB%s%s" % (
                n["name"], "Ready" if n.get("ready") else "NOT READY", 100 * (n.get("cpu_used") or 0) / (n.get("cpu_cores") or 1), n.get("cpu_cores"),
                (n.get("mem_used") or 0) / 2 ** 30, (n.get("mem_total") or 0) / 2 ** 30, ", %d C" % hw["temp_c"] if hw.get("temp_c") is not None else "",
                ", GPU " + ", ".join(g.get("name", "") for g in hw.get("gpu_live") or []) if hw.get("gpu_live") else ""))
        bad = ["%s/%s %s" % (p.get("namespace"), p.get("name"), p.get("status")) for p in st.get("pods") or [] if p.get("status") not in ("Running", "Succeeded", "Completed")][:8]
        sp = (st.get("ai") or {}).get("split") or {}
        return ("You are the AI on the galaxy wall display of the user's home k3s cluster (run with nodeyard). Answer in 1 to 4 short plain sentences, "
                "from the live state below; say so if it isn't there.\nNodes:\n" + "\n".join(lines) +
                "\nModel: " + ((sp.get("alias") or sp.get("model") or "") + (" (ready)" if sp.get("ready") else " (not ready)") if sp else "none") +
                ("\nPods not running: " + "; ".join(bad) if bad else "") +
                ("\nAlerts: " + "; ".join(a.get("title", "") for a in alerts[:6]) if alerts else ""))

    def chat(self, message):
        ai = getattr(self.ctx, "ai", None)
        if ai is None:
            return "(no AI backend on this dashboard)"
        try:
            targets = [t for t in (ai.targets().get("targets") or []) if t.get("ready")]
        except Exception:  # noqa: BLE001
            targets = []
        target = next((t["id"] for t in targets if t["id"] == "split"), targets[0]["id"] if targets else "")
        if not target:
            return "No model is ready. Start one in nodeyard: AI › Models."
        payload = {"messages": [{"role": "system", "content": self.brief()}, {"role": "user", "content": str(message)[:2000]}], "temperature": 0.3, "max_tokens": 260}
        try:
            conn, resp = ai.open_chat(target, payload)
        except Exception as e:  # noqa: BLE001
            return "(the model didn't answer: %s)" % e
        text = []
        try:
            for line in iter(resp.readline, b""):
                line = line.strip()
                if not line.startswith(b"data:") or line.endswith(b"[DONE]"):
                    continue
                try:
                    j = json.loads(line[5:])
                except ValueError:
                    continue
                for ch in j.get("choices") or []:
                    text.append(((ch.get("delta") or {}).get("content")) or "")
        except OSError:
            pass
        finally:
            conn.close()
        return re.sub(r"<think>.*?</think>", "", "".join(text), flags=re.S).strip() or "(no answer)"


def register(ctx, args):
    g = Galaxy3D(ctx, args)
    ctx.galaxy3d = g

    def page(h, q):
        path = h.path.split("?", 1)[0]
        name = PAGES.get(path, "wall")
        with open(os.path.join(ROOT, "pages", name + ".html"), "rb") as f:
            h._send(200, f.read(), "text/html; charset=utf-8")

    def static(h, path, q):
        full = os.path.realpath(os.path.join(ROOT, "static", path[len("/g3/static/"):]))
        base = os.path.realpath(os.path.join(ROOT, "static"))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return h._send(404, "Not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        with open(full, "rb") as f:
            h._send(200, f.read(), ctype, {"Cache-Control": "private, max-age=3600"})

    def stream(body_fn, key_fn):
        def route(h, q):
            h.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"), ("Connection", "close")):
                h.send_header(k, v)
            h.end_headers()
            h.close_connection = True
            last, started = object(), time.time()
            try:
                h.wfile.write(b"retry: 3000\n\n")
                while time.time() - started < 600:      # the page reconnects; nothing stays open for ever
                    with g.lock:
                        g.lock.wait_for(lambda: key_fn() != last, timeout=4)
                        cur = key_fn()
                        body = body_fn() if cur != last else None
                    if body is not None:
                        last = cur
                        h.wfile.write(("data: " + json.dumps(body) + "\n\n").encode())
                    else:
                        h.wfile.write(b": ka\n\n")
                    h.wfile.flush()
                    if ctx.auth is not None and not h._authed():
                        return None
            except (BrokenPipeError, ConnectionResetError, OSError):
                return None
        return route

    def post_state(attr, allowed=None):
        def route(h, body):
            with g.lock:
                cur = getattr(g, attr)
                if attr == "mode":
                    m = str(body.get("mode", "")).lower()
                    if m not in MODES:
                        return h._json({"ok": False, "err": "mode must be one of " + ", ".join(MODES)}, 400)
                    cur.update(prev=cur["mode"], mode=m, ts=time.time(), by="api")
                elif attr == "light":
                    rgb = body.get("rgb")
                    if isinstance(rgb, list) and len(rgb) == 3:
                        cur.update({k: v for k, v in body.items() if k in ("rgb", "hue", "lit", "mode", "stop", "stop_name", "phase", "prog", "idx", "n", "stops", "bri", "from", "hold_s", "beat")})
                else:
                    cur.update({k: v for k, v in body.items() if k in (allowed or ())})
                cur["id"] = cur.get("id", 0) + 1
                g.lock.notify_all()
                return h._json(dict(cur, ok=True))
        return route

    def j(fn):
        return lambda h, q: h._json(fn())

    for p in PAGES:
        ctx.get_routes[p] = page
    ctx.prefix_routes["/g3/static/"] = static
    A = "/g3/api/"
    ctx.get_routes.update({
        A + "config": j(g.config), A + "all": j(g.all), A + "topology": j(g.topology),
        A + "gpu": j(lambda: {"gpus": g.gpus(), "render_backend": "browser"}), A + "showtime": j(g.showtime),
        A + "threats": j(g.threats), A + "threats/stream": stream(g.threats, lambda: 0),
        A + "mode": j(g.mode_body), A + "mode/stream": stream(g.mode_body, lambda: g.mode["id"]),
        A + "presence": j(g.presence_body), A + "presence/stream": stream(g.presence_body, lambda: g.presence["id"]),
        A + "lightstate": j(lambda: dict(g.light)), A + "lightstate/stream": stream(lambda: dict(g.light), lambda: g.light["id"]),
        A + "takeover": j(lambda: dict(g.takeover)), A + "takeover/stream": stream(lambda: dict(g.takeover), lambda: g.takeover["id"]),
        A + "pulse/stream": stream(g.pulse, lambda: int(time.time() // 4)),
        A + "history": lambda h, q: h._json(g.history(q)),
        A + "inventory": j(lambda: {"ai_servers": [], "guests_seen": len(g.guests(g.snap()[0])), "health": [], "pinned": 0, "pve_endpoints": 0, "pve_nodes": [], "resolved": 0}),
        A + "telemetry": j(lambda: {}), A + "rtt/baseline": j(lambda: {}),
    })
    ctx.post_routes.update({
        A + "mode": post_state("mode"), A + "lightstate": post_state("light"), A + "lightcolor": post_state("light"),
        A + "lightenable": lambda h, b: h._json({"ok": True}),
        A + "takeover": lambda h, b: h._json(dict(g.takeover, ok=False, err="Not available on nodeyard.")),
        A + "chat": lambda h, b: h._json({"reply": g.chat(b.get("message", ""))}),
    })
