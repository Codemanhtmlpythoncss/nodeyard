"""Cluster stats over the server API key: every machine's CPU, memory, disk, network,
temperature and GPUs, plus a short history, in one small JSON reply.

For desktop widgets (LiveWall) and scripts. It reads the dashboard's last poll, so it is cheap
to call every few seconds and never touches Kubernetes itself. Read-only.
"""

POINTS = 60          # history points a widget gets by default (5 minutes at the default 5 s poll)
MAX_POINTS = 720


def _f(v, nd=1):
    try:
        return None if v is None else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def gpu(g):
    return {"index": g.get("index"), "name": g.get("name") or "GPU", "use": _f(g.get("use")), "mem_used": g.get("mem_used"),
            "mem_total": g.get("mem_total"), "temp_c": _f(g.get("temp_c")), "power_w": _f(g.get("power_w")),
            "power_limit_w": _f(g.get("power_limit_w")), "clock_mhz": _f(g.get("clock_mhz"), 0), "fan": _f(g.get("fan"))}


def node(n):
    hw = n.get("hw") or {}
    return {
        "name": n.get("name", ""), "ready": bool(n.get("ready")), "status": n.get("status", ""), "roles": n.get("roles") or [],
        "arch": n.get("arch", ""), "cpu_model": hw.get("cpu_model", ""),
        "cpu_cores": n.get("cpu_cores"), "cpu_used": _f(n.get("cpu_used"), 3), "cpu_use": _f(hw.get("cpu_use")),
        "load": _f((hw.get("load") or [None])[0], 2), "freq_mhz": hw.get("freq_mhz"),
        "mem_total": n.get("mem_total"), "mem_used": n.get("mem_used"),
        "disk_total": n.get("disk_total"), "disk_used": n.get("disk_used"),
        "net_rx_rate": n.get("net_rx_rate"), "net_tx_rate": n.get("net_tx_rate"),
        "temp_c": _f(hw.get("temp_c")), "pods_running": n.get("pods_running"), "pods_total": n.get("pods_total"),
        "uptime": hw.get("uptime"), "gpus": [gpu(g) for g in hw.get("gpu_live") or []],
        "gpu_cards": len(((hw.get("hardware") or {}).get("gpus")) or []),
    }


# history sample per node (analysis.sample): cpu_used, mem_used, rx, tx, temp, freq, load, gpu_use, gpu_mem, disk
SERIES = {"cpu": 0, "mem": 1, "rx": 2, "tx": 3, "temp": 4, "gpu": 7, "gpu_mem": 8, "disk": 9}


def history(samples):
    out = {"t": [s.get("t") for s in samples], "nodes": {}, "cluster": {k: [] for k in ("cpu", "mem", "rx", "tx", "gpu")}}
    names = []
    for s in samples:
        for name in s.get("nodes") or {}:
            if name not in names:
                names.append(name)
    for name in names:
        out["nodes"][name] = {k: [((s.get("nodes") or {}).get(name) or [None] * 10)[i] for s in samples] for k, i in SERIES.items()}
    for s in samples:
        rows = list((s.get("nodes") or {}).values())

        def tot(i):
            vals = [r[i] for r in rows if len(r) > i and r[i] is not None]
            return round(sum(vals), 3) if vals else None
        gpus = [r[7] for r in rows if len(r) > 7 and r[7] is not None]
        out["cluster"]["cpu"].append(tot(0))
        out["cluster"]["mem"].append(tot(1))
        out["cluster"]["rx"].append(tot(2))
        out["cluster"]["tx"].append(tot(3))
        out["cluster"]["gpu"].append(round(sum(gpus) / len(gpus), 1) if gpus else None)
    return out


def cluster(store, points=POINTS):
    snap = store.snapshot()
    st = snap.get("state") or {}
    tot = snap.get("totals") or {}
    levels = {"critical": 0, "warning": 0, "info": 0}
    for a in snap.get("alerts") or []:
        levels[a.get("level", "info")] = levels.get(a.get("level", "info"), 0) + 1
    keep = ("nodes", "nodes_ready", "cpu_total", "cpu_used", "mem_total", "mem_used", "disk_total", "disk_used", "net_rx_rate", "net_tx_rate",
            "pods", "pods_running", "gpus", "gpu_use", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_power")
    return {
        "ok": bool(st), "updated": snap.get("updated") or 0, "error": snap.get("error"),
        "totals": {k: tot.get(k) for k in keep}, "alerts": levels,
        "nodes": [node(n) for n in st.get("nodes") or []],
        "history": history(store.history_slice(points) if hasattr(store, "history_slice") else []),
    }


def register(ctx, args):
    def route(h, q):
        store = getattr(ctx, "store", None)
        if store is None:
            return h._json({"ok": False, "error": "no cluster data on this server"}, 503)
        try:
            n = max(2, min(MAX_POINTS, int((q.get("points") or [POINTS])[0])))
        except (TypeError, ValueError):
            n = POINTS
        h._json(cluster(store, n))

    ctx.get_routes["/api/v1/cluster"] = route
