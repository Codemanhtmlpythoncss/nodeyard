"""What the dashboard says about a snapshot: totals, alerts and history samples."""
import time


def totals(state):
    nodes = state["nodes"]
    ready = [n for n in nodes if n["ready"]]

    def total(key, only=None):
        vals = [n[key] for n in (only if only is not None else nodes) if n.get(key) is not None]
        return sum(vals) if vals else None

    live = [g for n in nodes for g in ((n.get("hw") or {}).get("gpu_live") or [])]
    # every graphics device the agents found, also ones nothing reads usage from yet
    cards = [{"node": n["name"], "name": g.get("name_full") or g.get("model") or "GPU", "nvidia": g.get("vendor") == "0x10de",
              "live": bool((n.get("hw") or {}).get("gpu_live"))}
             for n in nodes for g in (((n.get("hw") or {}).get("hardware") or {}).get("gpus") or [])]
    pods = state["pods"]
    by_status = {}
    for p in pods:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    return {
        "nodes": len(nodes), "nodes_ready": len(ready),
        "cpu_total": total("cpu_cores"), "cpu_used": total("cpu_used"),
        "mem_total": total("mem_total"), "mem_used": total("mem_used"),
        "disk_total": total("disk_total"), "disk_used": total("disk_used"),
        "net_rx_rate": total("net_rx_rate"), "net_tx_rate": total("net_tx_rate"),
        "pods": len(pods), "pods_running": by_status.get("Running", 0), "pod_states": by_status,
        "pods_capacity": total("pods_capacity"),
        "namespaces": len(state["namespaces"]), "services": len(state["services"]),
        "workloads": len(state["workloads"]), "volumes": len([v for v in state["volumes"] if v["kind"] == "PVC"]),
        "restarts": sum(p["restarts"] for p in pods),
        "gpus": len(live), "gpu_use": (sum(g["use"] or 0 for g in live) / len(live)) if live else None,
        "gpu_mem_used": sum(g["mem_used"] for g in live) if live else None, "gpu_mem_total": sum(g["mem_total"] for g in live) if live else None,
        "gpu_temp": max([g["temp_c"] for g in live if g["temp_c"] is not None] or [0]) or None if live else None,
        "gpu_power": sum(g["power_w"] or 0 for g in live) if live else None,
        "gpu_cards": cards,
    }


BAD_POD = {"CrashLoopBackOff": "critical", "Error": "critical", "OOMKilled": "critical", "ImagePullBackOff": "warning",
           "ErrImagePull": "warning", "CreateContainerConfigError": "warning", "Evicted": "warning",
           "ContainerStatusUnknown": "warning", "Unknown": "warning", "InvalidImageName": "warning"}
# Finished-for-good states: the pod won't run again, a controller makes a new one.
DEAD_POD = {"Evicted", "ContainerStatusUnknown"}  # (a crashing pod also shows "Error" between restarts)
REPLACED_BY = {"ReplicaSet", "DaemonSet", "StatefulSet"}
OWN_TEMP_NS = {"nodeyard-cleanup"}


def alerts(state):
    out = []
    now = time.time()

    def add(level, title, detail, kind, ref):
        out.append({"level": level, "title": title, "detail": detail, "kind": kind, "ref": ref})

    for n in state["nodes"]:
        if not n["ready"]:
            reason = n.get("ready_reason") or ""
            message = n.get("ready_message") or "Its kubelet isn't reporting. Check the machine and its network cable."
            detail = (reason + ": " if reason and message and not message.startswith(reason) else "") + message
            since = n.get("ready_since") or 0
            down_for = now - since if since else None
            if down_for is not None and 0 <= down_for < 20:
                add("info", "%s briefly stopped reporting" % n["name"],
                    "%s Kubernetes has reported this for %d seconds and is checking whether it continues." % (detail, int(down_for)),
                    "node", n["name"])
            else:
                add("critical", "%s is not ready" % n["name"], detail, "node", n["name"])
        for cond in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            if n["conditions"].get(cond) == "True":
                add("critical" if cond != "PIDPressure" else "warning", "%s: %s" % (n["name"], cond),
                    "Kubernetes reports this node is under pressure and may evict pods.", "node", n["name"])
        if n["mem_used"] is not None and n["mem_total"]:
            f = n["mem_used"] / n["mem_total"]
            if f > 0.95:
                add("critical", "%s is out of memory" % n["name"], "%.0f%% of its memory is in use." % (f * 100), "node", n["name"])
            elif f > 0.9:
                add("warning", "%s is short of memory" % n["name"], "%.0f%% of its memory is in use." % (f * 100), "node", n["name"])
        if n["cpu_used"] is not None and n["cpu_cores"]:
            f = n["cpu_used"] / n["cpu_cores"]
            if f > 0.9:
                add("warning", "%s is busy" % n["name"], "%.0f%% of its CPU is in use." % (f * 100), "node", n["name"])
        if n["disk_used"] is not None and n["disk_total"]:
            f = n["disk_used"] / n["disk_total"]
            if f > 0.95:
                add("critical", "%s's disk is almost full" % n["name"], "%.0f%% used." % (f * 100), "node", n["name"])
            elif f > 0.85:
                add("warning", "%s's disk is filling up" % n["name"], "%.0f%% used." % (f * 100), "node", n["name"])
    for n in state["nodes"]:
        hw = n.get("hw")
        if not hw:
            continue
        if hw.get("temp_c") is not None and hw["temp_c"] >= 85:
            add("critical", "%s is very hot (%.0f °C)" % (n["name"], hw["temp_c"]), "It will slow itself down to cool off. Check airflow and the heatsink.", "node", n["name"])
        elif hw.get("temp_c") is not None and hw["temp_c"] >= 80:
            add("warning", "%s is running hot (%.0f °C)" % (n["name"], hw["temp_c"]), "Check airflow and the heatsink.", "node", n["name"])
        if hw.get("undervoltage"):
            add("warning", "%s reports low power-supply voltage" % n["name"], "Use a better power supply or shorter cable; it can crash or slow down.", "node", n["name"])
        if hw.get("oom_new"):
            add("warning", "%s killed %d process(es) for lack of memory" % (n["name"], hw["oom_new"]), "See what uses its memory on the Processes page.", "node", n["name"])
        if hw.get("mem_total") and hw.get("mem_available") is not None and hw["mem_available"] / hw["mem_total"] < 0.05:
            add("warning", "%s has almost no memory left to give" % n["name"], "Only %.0f MiB is available." % (hw["mem_available"] / 1048576.0), "node", n["name"])
        for nic in ((hw.get("hardware") or {}).get("nics") or []):
            # only when it's slower than the card itself can go (a Pi 3 is 100 Mbit/s hardware)
            if nic.get("cluster") and not nic.get("wireless") and nic.get("speed_mbps") and nic["speed_mbps"] < min(1000, nic.get("max_mbps") or 1000):
                add("warning", "%s's network runs at only %d Mbit/s" % (n["name"], nic["speed_mbps"]),
                    "Its cluster link (%s) didn't connect at gigabit speed. That is nearly always the cable (old or damaged, with broken wire pairs) "
                    "or the switch port: swap them. Until then models load and split models answer slower." % nic["name"], "node", n["name"])
            elif nic.get("cluster") and nic.get("wireless"):
                add("info", "%s uses Wi-Fi for the cluster" % n["name"], "A network cable is faster and steadier, especially for split models.", "node", n["name"])
        if (hw.get("psi_memory") or 0) >= 20:
            add("warning", "%s is waiting on memory" % n["name"], "Programs are stalling for memory (pressure %.0f%%)." % hw["psi_memory"], "node", n["name"])
    for p in state["pods"]:
        ref = "%s/%s" % (p["namespace"], p["name"])
        level = BAD_POD.get(p["status"]) or (BAD_POD.get(p["status"].split(":")[-1]) if p["status"].startswith("Init:") else None)
        owner_kind = (p.get("owner") or "").split("/")[0]
        if level and p["status"] in DEAD_POD and owner_kind in REPLACED_BY:
            # A dead pod a controller has already replaced is only an old
            # record (the kubelet keeps evicted pods around); the workload's
            # own "N of M ready" alert covers a real problem.
            add("info", "%s is an old %s pod" % (p["name"], p["status"]),
                "Its %s started a replacement. Kubernetes deletes the record by itself later." % owner_kind, "pod", ref)
        elif level and not owner_kind and p["namespace"] in OWN_TEMP_NS:
            pass  # nodeyard's own short-lived helper pods; it deletes them itself
        elif level:
            add(level, "%s is %s" % (p["name"], p["status"]), "In namespace %s%s." % (
                p["namespace"], ", restarted %d times" % p["restarts"] if p["restarts"] else ""), "pod", ref)
        elif p["status"] == "Pending" and p["created"] and now - p["created"] > 300:
            add("warning", "%s has been Pending for %d min" % (p["name"], (now - p["created"]) // 60),
                "It can't be scheduled: usually not enough memory or CPU on any node.", "pod", ref)
        elif p["restarts"] >= 5 and p["status"] == "Running":
            add("info", "%s restarted %d times" % (p["name"], p["restarts"]), "It's running now; check its logs for why it restarted.", "pod", ref)
    for w in state["workloads"]:
        ref = "%s/%s" % (w["namespace"], w["name"])
        if w["kind"] in ("Deployment", "StatefulSet", "DaemonSet") and w["ready"] < w["desired"]:
            add("warning", "%s %s has %d of %d ready" % (w["kind"], w["name"], w["ready"], w["desired"]),
                "In namespace %s." % w["namespace"], "workload", ref)
        if w["kind"] == "Job" and w.get("failed"):
            add("warning", "Job %s has failed %d time(s)" % (w["name"], w["failed"]), "In namespace %s." % w["namespace"], "workload", ref)
    for v in state["volumes"]:
        if v["kind"] == "PVC" and v["status"] not in ("Bound", ""):
            add("warning", "Volume claim %s is %s" % (v["name"], v["status"]), "In namespace %s." % v["namespace"], "volume", v["name"])
    for e in state["errors"]:
        add("info", "Partial data", e, "cluster", "")
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: order[a["level"]])
    return out


def _gpu_use(n):
    g = (n.get("hw") or {}).get("gpu_live") or []
    return (sum(x["use"] or 0 for x in g) / len(g)) if g else None


def _gpu_mem(n):
    g = (n.get("hw") or {}).get("gpu_live") or []
    return sum(x["mem_used"] for x in g) if g else None


def sample(state, t):
    return {
        "t": t, "pods": sum(1 for p in state["pods"] if p["status"] == "Running"),
        "nodes": {n["name"]: [n["cpu_used"], n["mem_used"], n["net_rx_rate"], n["net_tx_rate"],
                              (n.get("hw") or {}).get("temp_c"), (n.get("hw") or {}).get("freq_mhz"), ((n.get("hw") or {}).get("load") or [None])[0],
                              _gpu_use(n), _gpu_mem(n)]
                  for n in state["nodes"]},
    }
