"""Reading a cluster: a small Kubernetes API client and the code that turns its
answers into the dashboard's snapshot.

Standard library only (Python 3.8+). Everything here is read-only: the client
only ever issues GET requests.
"""
import base64
import concurrent.futures
import http.client
import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone

# --------------------------------------------------------------------------- units

_SUFFIX = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}
_QTY = re.compile(r"^([0-9]*\.?[0-9]+)([A-Za-z]*)$")


def parse_quantity(q):
    """A Kubernetes quantity ("250m", "4", "16244588Ki", "123456789n") as a float."""
    if q is None:
        return 0.0
    m = _QTY.match(str(q).strip())
    if not m:
        return 0.0
    num, suffix = float(m.group(1)), m.group(2)
    return num * _SUFFIX.get(suffix, 1) if suffix else num


def parse_time(ts):
    """An RFC 3339 timestamp as epoch seconds (0 if it can't be read)."""
    if not ts:
        return 0.0
    try:
        base = re.sub(r"\.\d+", "", str(ts)).replace("Z", "+0000")
        return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- client

class KubeError(Exception):
    pass


def load_kubeconfig(path):
    """server, CA, client certificate/key (or token) from a kubeconfig, without
    needing a YAML library: k3s and `nodeyard kubeconfig` write them inline."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KubeError("cannot read the kubeconfig %s: %s" % (path, e.strerror or e))

    def grab(key):
        m = re.search(r"^\s*" + re.escape(key) + r":\s*(\S+)\s*$", text, re.M)
        return m.group(1).strip("\"'") if m else None

    cfg = {
        "server": grab("server"),
        "ca": grab("certificate-authority-data"),
        "cert": grab("client-certificate-data"),
        "key": grab("client-key-data"),
        "token": grab("token"),
    }
    if not cfg["server"]:
        raise KubeError("no server address in %s" % path)
    if not (cfg["token"] or (cfg["cert"] and cfg["key"])):
        raise KubeError("%s has no embedded client certificate or token" % path)
    return cfg


class KubeClient:
    def __init__(self, kubeconfig, timeout=8):
        cfg = load_kubeconfig(kubeconfig)
        u = urllib.parse.urlparse(cfg["server"])
        self.host = u.hostname
        self.port = u.port or 443
        self.server = cfg["server"]
        self.timeout = timeout
        self.token = cfg["token"]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        if cfg["ca"]:
            ctx.load_verify_locations(cadata=base64.b64decode(cfg["ca"]).decode("ascii"))
        else:
            ctx.load_default_certs()
        if cfg["cert"] and cfg["key"]:
            # load_cert_chain wants files: write them privately, load, delete.
            paths = []
            try:
                for data in (cfg["cert"], cfg["key"]):
                    fd, p = tempfile.mkstemp(prefix="nodeyard-dash-")
                    os.write(fd, base64.b64decode(data))
                    os.close(fd)
                    os.chmod(p, 0o600)
                    paths.append(p)
                ctx.load_cert_chain(paths[0], paths[1])
            finally:
                for p in paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        self.ctx = ctx

    def get(self, path, timeout=None, raw=False):
        conn = http.client.HTTPSConnection(self.host, self.port, context=self.ctx, timeout=timeout or self.timeout)
        headers = {"Accept": "application/json", "User-Agent": "nodeyard-dashboard"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read(8 * 1024 * 1024)
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise KubeError("%s: %s" % (path, e))
        finally:
            conn.close()
        if resp.status >= 400:
            raise KubeError("%s: HTTP %d" % (path, resp.status))
        if raw:
            return body.decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise KubeError("%s: not JSON" % path)


# --------------------------------------------------------------------------- pods

def pod_status(pod):
    """What `kubectl get pods` would show in the STATUS column."""
    st = pod.get("status", {})
    reason = st.get("phase", "Unknown")
    if st.get("reason"):
        reason = st["reason"]
    for ics in st.get("initContainerStatuses", []) or []:
        term = (ics.get("state") or {}).get("terminated")
        wait = (ics.get("state") or {}).get("waiting")
        if term and term.get("exitCode", 0) != 0:
            return "Init:" + (term.get("reason") or "Error")
        if wait and wait.get("reason") and wait["reason"] != "PodInitializing":
            return "Init:" + wait["reason"]
    for cs in st.get("containerStatuses", []) or []:
        state = cs.get("state") or {}
        if state.get("waiting") and state["waiting"].get("reason"):
            reason = state["waiting"]["reason"]
        elif state.get("terminated") and state["terminated"].get("reason") and st.get("phase") != "Succeeded":
            reason = state["terminated"]["reason"]
    if pod.get("metadata", {}).get("deletionTimestamp"):
        reason = "Unknown" if st.get("reason") == "NodeLost" else "Terminating"
    return reason


def build_pod(pod, usage):
    meta, spec, st = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    css = {c["name"]: c for c in st.get("containerStatuses", []) or []}
    containers = []
    ready_n = 0
    restarts = 0
    for c in spec.get("containers", []):
        cs = css.get(c["name"], {})
        state = cs.get("state") or {}
        kind = next(iter(state), "unknown")
        detail = state.get(kind) or {}
        if cs.get("ready"):
            ready_n += 1
        restarts += cs.get("restartCount", 0)
        containers.append({
            "name": c["name"], "image": c.get("image", ""), "ready": bool(cs.get("ready")),
            "restarts": cs.get("restartCount", 0), "state": kind, "reason": detail.get("reason", ""),
        })
    owners = meta.get("ownerReferences") or []
    owner = owners[0]["kind"] + "/" + owners[0]["name"] if owners else ""
    key = (meta.get("namespace", ""), meta.get("name", ""))
    cpu, mem, rss = (usage.get(key, (None, None, None)) + (None,))[:3]
    return {
        "namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "status": pod_status(pod),
        "ready": "%d/%d" % (ready_n, len(spec.get("containers", []))), "restarts": restarts,
        "node": spec.get("nodeName", ""), "ip": st.get("podIP", ""), "host_ip": st.get("hostIP", ""),
        "host_network": bool(spec.get("hostNetwork")), "created": parse_time(meta.get("creationTimestamp")),
        "started": parse_time(st.get("startTime")), "owner": owner, "qos": st.get("qosClass", ""),
        "cpu": cpu, "mem": mem, "mem_rss": rss, "containers": containers, "labels": meta.get("labels") or {}, "uid": meta.get("uid", ""),
    }


# --------------------------------------------------------------------------- collector

MiB = 1048576


def load_progress(main, split_pods, shares, rpc, gpu_node=""):
    """How far the split model is with loading, from what the pods hold in memory:
    each RPC server's memory grows towards its planned share (-ts) as the main server
    sends it the weights, and the main server's working set grows as it reads the file.
    A share after the RPC servers' is the main node's GPU (llama.cpp's device order):
    it gets the last layers, so it fills with what the main server has read past them."""
    mp = next((p for p in split_pods if p["name"].startswith("llama-main-")), None)
    if not mp or not main or not main.get("spec", {}).get("replicas", 1):
        return None
    if main.get("status", {}).get("readyReplicas"):
        return {"phase": "ready", "pct": 100}
    st = mp["status"]
    if st.startswith("Init:"):
        return {"phase": "waiting", "why": "downloading" if st.startswith("Init:1") else "getting llama.cpp", "pct": 0}
    if st not in ("Running",):
        return {"phase": "starting", "why": st, "pct": 0}
    cs = (main.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [{}])[0]
    dio = "dio" in (cs.get("args") or [])  # direct reads: the main server's memory doesn't show the file
    total = sum(shares) or 0
    per = []
    got_all = 0
    read_mib = (mp.get("mem") or 0) / MiB
    for i, mib in enumerate(shares):
        if i >= len(rpc):
            node = (gpu_node + " GPU") if gpu_node else "?"
            # (with direct reads gpu_progress() measures it on the card instead)
            got = 0.0 if dio else max(0.0, min(float(mib), read_mib - sum(shares[:i])))
            if mib and got >= 0.93 * mib:
                got = float(mib)
            got_all += got
            per.append({"node": node, "got_mib": round(got), "share_mib": round(mib)})
            continue
        node = rpc[i]
        pod = next((p for p in split_pods if p["name"].startswith("rpc-%s-" % node)), None)
        held = ((pod or {}).get("mem_rss") or (pod or {}).get("mem") or 0) / MiB
        got = max(0.0, min(float(mib), held - 48))  # (the RPC server itself uses a few dozen MiB)
        if mib and got >= 0.93 * mib:
            got = float(mib)  # the planned shares are proportions, not exact sizes: this node is done
        got_all += got
        per.append({"node": node, "got_mib": round(got), "share_mib": round(mib)})
    pct = round(100 * got_all / total, 1) if total else 0
    file_pct = min(100.0, round(100 * (mp.get("mem") or 0) / MiB / total, 1)) if total and not dio else None
    return {"phase": "warming up" if pct >= 99.5 else "loading", "pct": min(100, pct), "file_pct": file_pct, "nodes": per}


def gpu_progress(state):
    """The GPU's share of a loading split model, measured on the card: its video memory
    in use, from the GPU node's agent (the main server's own memory doesn't show it)."""
    ld = ((state.get("ai") or {}).get("split") or {}).get("load")
    if not ld or ld.get("phase") not in ("loading", "warming up") or not ld.get("nodes"):
        return
    for n in ld["nodes"]:
        if not n["node"].endswith(" GPU"):
            continue
        node = next((x for x in state.get("nodes", []) if x["name"] == n["node"][:-4]), None)
        live = ((node or {}).get("hw") or {}).get("gpu_live") or []
        if live:
            got = max(0.0, min(float(n["share_mib"]), sum(g.get("mem_used") or 0 for g in live) / MiB - 64))
            n["got_mib"] = round(n["share_mib"] if got >= 0.93 * n["share_mib"] else got)
    total = sum(n["share_mib"] for n in ld["nodes"])
    if total:
        ld["pct"] = min(100, round(100 * sum(n["got_mib"] for n in ld["nodes"]) / total, 1))
        ld["phase"] = "warming up" if ld["pct"] >= 99.5 else "loading"


def parse_bench(cm):
    """The ConfigMap `nodeyard hw bench` fills: {node: results}."""
    out = {}
    for node, text in ((cm or {}).get("data") or {}).items():
        try:
            v = json.loads(text)
        except ValueError:
            continue
        if isinstance(v, dict):
            out[node] = v
    return out


class KubeSource:
    """Collects a snapshot from a live cluster."""

    def __init__(self, kubeconfig, cluster_name="", interval=5):
        self.client = KubeClient(kubeconfig)
        self.cluster_name = cluster_name
        self.summary_ttl = max(1.5, interval * 0.9)  # ask each kubelet about as often as we read the cluster
        self.summary_cache = {}  # node -> (time, data)
        self.net_prev = {}  # node -> (time, rx, tx)
        self.lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    def _try(self, path, timeout=None):
        try:
            return self.client.get(path, timeout=timeout), None
        except KubeError as e:
            return None, str(e)

    def collect(self):
        errors = []
        paths = {
            "version": "/version", "nodes": "/api/v1/nodes", "pods": "/api/v1/pods?limit=2000",
            "deployments": "/apis/apps/v1/deployments", "daemonsets": "/apis/apps/v1/daemonsets",
            "statefulsets": "/apis/apps/v1/statefulsets", "jobs": "/apis/batch/v1/jobs",
            "cronjobs": "/apis/batch/v1/cronjobs", "services": "/api/v1/services",
            "endpoints": "/api/v1/endpoints", "ingresses": "/apis/networking.k8s.io/v1/ingresses",
            "pvcs": "/api/v1/persistentvolumeclaims", "pvs": "/api/v1/persistentvolumes",
            "events": "/api/v1/events?limit=400", "namespaces": "/api/v1/namespaces",
            "bench": "/api/v1/namespaces/nodeyard-system/configmaps/nodeyard-bench",
        }
        futures = {k: self.pool.submit(self._try, p) for k, p in paths.items()}
        raw = {}
        for k, fut in futures.items():
            data, err = fut.result()
            raw[k] = data or {}
            if err and k != "bench":  # no speed test run yet is normal
                errors.append(err)
        if not raw["nodes"]:
            raise KubeError("could not list nodes: " + (errors[0] if errors else "no answer"))

        node_names = [n["metadata"]["name"] for n in raw["nodes"].get("items", [])]
        summaries = self._summaries(node_names)

        # Usage comes from each node's own kubelet: the cluster's metrics service attributes figures by
        # host name, which goes wrong when a machine's host name differs from its node name.
        pod_usage = {}
        for summ in summaries.values():
            for sp in summ.get("pods", []) or []:
                ref = sp.get("podRef") or {}
                cpu_n = (sp.get("cpu") or {}).get("usageNanoCores")
                mem = sp.get("memory") or {}
                pod_usage[(ref.get("namespace", ""), ref.get("name", ""))] = (
                    cpu_n / 1e9 if cpu_n is not None else None, mem.get("workingSetBytes"), mem.get("rssBytes"))
        missing = [n for n in node_names if n not in summaries]
        if missing:
            errors.append("Couldn't read usage from: %s" % ", ".join(missing))

        pods = [build_pod(p, pod_usage) for p in raw["pods"].get("items", [])]
        nodes = [self._build_node(n, {}, summaries, pods) for n in raw["nodes"].get("items", [])]

        control = [n for n in nodes if any(r in ("control-plane", "master") for r in n["roles"])]
        api = self.client.server
        if control and control[0]["internal_ip"] and urllib.parse.urlparse(api).hostname in ("127.0.0.1", "localhost", "::1"):
            api = "https://%s:%s" % (control[0]["internal_ip"], self.client.port)  # the address other machines use
        state = {
            "cluster": {
                "name": self.cluster_name or (control[0]["name"] if control else "cluster"),
                "k3s_version": raw["version"].get("gitVersion", ""),
                "api_server": api, "created": min([n["created"] for n in nodes] or [0]),
                "pod_cidr": self._pod_cidr(nodes), "service_cidr": self._service_cidr(raw["services"]),
            },
            "nodes": nodes, "pods": pods,
            "workloads": self._workloads(raw),
            "services": self._services(raw["services"], raw["endpoints"]),
            "ingresses": self._ingresses(raw["ingresses"]),
            "volumes": self._volumes(raw["pvcs"], raw["pvs"]),
            "events": self._events(raw["events"]),
            "namespaces": sorted(n["metadata"]["name"] for n in raw["namespaces"].get("items", [])),
            "ai": self._ai(raw, pods),
            "bench": parse_bench(raw["bench"]),
            "errors": errors,
        }
        return state

    def load_eta(self, state):
        """Time left for the split model to load, from how fast it rose over the last 2 minutes."""
        ld = ((state.get("ai") or {}).get("split") or {}).get("load")
        hist = self.__dict__.setdefault("load_hist", [])
        if not ld or ld.get("phase") != "loading":
            hist.clear()
            return
        now = time.time()
        hist.append((now, ld["pct"]))
        while len(hist) > 2 and now - hist[0][0] > 120:
            hist.pop(0)
        t0, p0 = hist[0]
        if now - t0 >= 10 and ld["pct"] > p0:
            ld["eta"] = int((100 - ld["pct"]) / ((ld["pct"] - p0) / (now - t0)))

    # -- nodes ---------------------------------------------------------------

    def _summaries(self, names):
        now = time.time()
        due = [n for n in names if n not in self.summary_cache or now - self.summary_cache[n][0] > self.summary_ttl]
        futs = {n: self.pool.submit(self._try, "/api/v1/nodes/%s/proxy/stats/summary" % urllib.parse.quote(n), 4) for n in due}
        for n, fut in futs.items():
            data, _err = fut.result()
            if data:
                self.summary_cache[n] = (now, data)
        return {n: self.summary_cache[n][1] for n in names if n in self.summary_cache}

    def _build_node(self, n, node_usage, summaries, pods):
        meta, st, spec = n.get("metadata", {}), n.get("status", {}), n.get("spec", {})
        name = meta["name"]
        info = st.get("nodeInfo", {})
        condition_rows = st.get("conditions", [])
        conds = {c["type"]: c["status"] for c in condition_rows}
        ready_condition = next((c for c in condition_rows if c.get("type") == "Ready"), {})
        addrs = [{"type": a["type"], "address": a["address"]} for a in st.get("addresses", [])]
        internal = next((a["address"] for a in addrs if a["type"] == "InternalIP"), "")
        external = next((a["address"] for a in addrs if a["type"] == "ExternalIP"), "")
        cap, alloc = st.get("capacity", {}), st.get("allocatable", {})
        labels = meta.get("labels") or {}
        roles = sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")) or ["worker"]
        summ = summaries.get(name, {})
        snode = summ.get("node", {})
        cpu_cores = parse_quantity(cap.get("cpu"))
        mem_total = parse_quantity(cap.get("memory"))
        cpu_used, mem_used = node_usage.get(name, (None, None))
        if cpu_used is None and snode.get("cpu"):
            cpu_used = snode["cpu"].get("usageNanoCores", 0) / 1e9
        if mem_used is None and snode.get("memory"):
            mem_used = snode["memory"].get("workingSetBytes")
        smem = snode.get("memory") or {}
        sysc = []
        for c in snode.get("systemContainers", []) or []:
            sysc.append({"name": c.get("name", ""), "cpu": (c.get("cpu") or {}).get("usageNanoCores", 0) / 1e9,
                         "mem": (c.get("memory") or {}).get("workingSetBytes", 0), "rss": (c.get("memory") or {}).get("rssBytes", 0)})
        memory = {"available": smem.get("availableBytes"), "usage": smem.get("usageBytes"), "working_set": smem.get("workingSetBytes"),
                  "rss": smem.get("rssBytes"), "major_faults": smem.get("majorPageFaults"), "system": sysc}
        fs = snode.get("fs") or {}
        net = snode.get("network") or {}
        rx, tx = net.get("rxBytes"), net.get("txBytes")
        rx_rate = tx_rate = None
        if rx is not None and tx is not None:
            # The summary is cached for a few polls: a rate needs two *different* samples.
            sampled = self.summary_cache[name][0] if name in self.summary_cache else time.time()
            prev = self.net_prev.get(name)  # (sample time, rx, tx, rx_rate, tx_rate)
            if prev and sampled == prev[0]:
                rx_rate, tx_rate = prev[3], prev[4]
            else:
                if prev and sampled > prev[0] and rx >= prev[1] and tx >= prev[2]:
                    rx_rate = (rx - prev[1]) / (sampled - prev[0])
                    tx_rate = (tx - prev[2]) / (sampled - prev[0])
                self.net_prev[name] = (sampled, rx, tx, rx_rate, tx_rate)
        mine = [p for p in pods if p["node"] == name]
        images = len(st.get("images", []) or [])
        taints = ["%s%s:%s" % (t.get("key", ""), "=" + t["value"] if t.get("value") else "", t.get("effect", ""))
                  for t in spec.get("taints", []) or []]
        return {
            "name": name, "ready": conds.get("Ready") == "True", "status": "Ready" if conds.get("Ready") == "True" else "NotReady",
            "disk_limit": (meta.get("annotations") or {}).get("nodeyard/disk-limit-gib") or None,
            "roles": roles, "internal_ip": internal, "external_ip": external, "addresses": addrs,
            "os": info.get("osImage", ""), "kernel": info.get("kernelVersion", ""), "arch": info.get("architecture", ""),
            "runtime": info.get("containerRuntimeVersion", ""), "kubelet": info.get("kubeletVersion", ""),
            "cpu_cores": cpu_cores, "cpu_used": cpu_used, "mem_total": mem_total, "mem_used": mem_used, "memory": memory,
            "pods_running": sum(1 for p in mine if p["status"] == "Running"), "pods_total": len(mine),
            "pods_capacity": int(parse_quantity(cap.get("pods"))),
            "disk_total": fs.get("capacityBytes"), "disk_used": fs.get("usedBytes"),
            "net_rx": rx, "net_tx": tx, "net_rx_rate": rx_rate, "net_tx_rate": tx_rate,
            "conditions": conds, "labels": labels, "taints": taints, "created": parse_time(meta.get("creationTimestamp")),
            "ready_since": parse_time(ready_condition.get("lastTransitionTime")),
            "ready_reason": ready_condition.get("reason", ""), "ready_message": ready_condition.get("message", ""),
            "kubelet_start": parse_time(snode.get("startTime")), "unschedulable": bool(spec.get("unschedulable")),
            "pod_cidr": spec.get("podCIDR", ""), "images": images,
            "allocatable": {"cpu": parse_quantity(alloc.get("cpu")), "memory": parse_quantity(alloc.get("memory")),
                            "pods": int(parse_quantity(alloc.get("pods")))},
        }

    # -- workloads, services, storage ----------------------------------------

    def _workloads(self, raw):
        out = []

        def images(tpl):
            return [c.get("image", "") for c in (tpl.get("spec", {}).get("containers") or [])]

        for it in raw["deployments"].get("items", []):
            s, sp = it.get("status", {}), it.get("spec", {})
            out.append({"kind": "Deployment", "namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
                        "desired": sp.get("replicas", 0), "ready": s.get("readyReplicas", 0), "available": s.get("availableReplicas", 0),
                        "images": images(sp.get("template", {})), "created": parse_time(it["metadata"].get("creationTimestamp"))})
        for it in raw["daemonsets"].get("items", []):
            s = it.get("status", {})
            out.append({"kind": "DaemonSet", "namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
                        "desired": s.get("desiredNumberScheduled", 0), "ready": s.get("numberReady", 0), "available": s.get("numberAvailable", 0),
                        "images": images(it.get("spec", {}).get("template", {})), "created": parse_time(it["metadata"].get("creationTimestamp"))})
        for it in raw["statefulsets"].get("items", []):
            s, sp = it.get("status", {}), it.get("spec", {})
            out.append({"kind": "StatefulSet", "namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
                        "desired": sp.get("replicas", 0), "ready": s.get("readyReplicas", 0), "available": s.get("availableReplicas", 0),
                        "images": images(sp.get("template", {})), "created": parse_time(it["metadata"].get("creationTimestamp"))})
        for it in raw["jobs"].get("items", []):
            s, sp = it.get("status", {}), it.get("spec", {})
            out.append({"kind": "Job", "namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
                        "desired": sp.get("completions", 1), "ready": s.get("succeeded", 0), "available": s.get("active", 0),
                        "failed": s.get("failed", 0), "images": images(sp.get("template", {})),
                        "created": parse_time(it["metadata"].get("creationTimestamp"))})
        for it in raw["cronjobs"].get("items", []):
            sp = it.get("spec", {})
            out.append({"kind": "CronJob", "namespace": it["metadata"]["namespace"], "name": it["metadata"]["name"],
                        "desired": 0, "ready": 0, "available": 0, "schedule": sp.get("schedule", ""),
                        "suspended": bool(sp.get("suspend")), "last": parse_time(it.get("status", {}).get("lastScheduleTime")),
                        "images": [], "created": parse_time(it["metadata"].get("creationTimestamp"))})
        return out

    @staticmethod
    def _pod_cidr(nodes):
        """The cluster's pod range: each node gets a /24 of one /16 (k3s's default layout)."""
        ranges = [n["pod_cidr"] for n in nodes if n.get("pod_cidr")]
        if not ranges:
            return ""
        heads = {".".join(r.split(".")[:2]) for r in ranges}
        if len(heads) == 1 and all(r.endswith("/24") for r in ranges):
            return "%s.0.0/16" % heads.pop()
        return ranges[0]

    @staticmethod
    def _service_cidr(services):
        for it in services.get("items", []):
            if it["metadata"]["name"] == "kubernetes" and it["spec"].get("clusterIP"):
                parts = it["spec"]["clusterIP"].split(".")
                if len(parts) == 4:
                    return "%s.%s.0.0/16" % (parts[0], parts[1])
        return ""

    @staticmethod
    def _services(services, endpoints):
        ready = {}
        for ep in endpoints.get("items", []):
            n = 0
            for sub in ep.get("subsets", []) or []:
                n += len(sub.get("addresses", []) or [])
            ready[(ep["metadata"]["namespace"], ep["metadata"]["name"])] = n
        out = []
        for it in services.get("items", []):
            meta, sp, st = it["metadata"], it.get("spec", {}), it.get("status", {})
            lb = [i.get("ip") or i.get("hostname", "") for i in (st.get("loadBalancer", {}).get("ingress") or [])]
            ports = []
            for p in sp.get("ports", []) or []:
                s = "%s/%s" % (p.get("port"), p.get("protocol", "TCP"))
                if p.get("nodePort"):
                    s += " (node %s)" % p["nodePort"]
                ports.append(s)
            out.append({
                "namespace": meta["namespace"], "name": meta["name"], "type": sp.get("type", "ClusterIP"),
                "cluster_ip": sp.get("clusterIP", ""), "external_ips": (sp.get("externalIPs") or []) + lb,
                "ports": ports, "node_ports": [p["nodePort"] for p in sp.get("ports", []) or [] if p.get("nodePort")],
                "selector": sp.get("selector") or {}, "endpoints": ready.get((meta["namespace"], meta["name"]), 0),
                "created": parse_time(meta.get("creationTimestamp")),
            })
        return out

    @staticmethod
    def _ingresses(ingresses):
        out = []
        for it in ingresses.get("items", []):
            meta, sp = it["metadata"], it.get("spec", {})
            hosts, paths = [], []
            for r in sp.get("rules", []) or []:
                if r.get("host"):
                    hosts.append(r["host"])
                for p in (r.get("http", {}).get("paths") or []):
                    be = p.get("backend", {}).get("service", {})
                    paths.append("%s -> %s" % (p.get("path", "/"), be.get("name", "")))
            addr = [i.get("ip") or i.get("hostname", "") for i in (it.get("status", {}).get("loadBalancer", {}).get("ingress") or [])]
            out.append({"namespace": meta["namespace"], "name": meta["name"], "class": sp.get("ingressClassName", ""),
                        "hosts": hosts, "paths": paths, "addresses": addr, "created": parse_time(meta.get("creationTimestamp"))})
        return out

    @staticmethod
    def _volumes(pvcs, pvs):
        out = []
        for it in pvcs.get("items", []):
            meta, sp, st = it["metadata"], it.get("spec", {}), it.get("status", {})
            out.append({"kind": "PVC", "namespace": meta["namespace"], "name": meta["name"], "status": st.get("phase", ""),
                        "volume": sp.get("volumeName", ""), "capacity": parse_quantity((st.get("capacity") or {}).get("storage")),
                        "storage_class": sp.get("storageClassName", ""), "access": sp.get("accessModes", []),
                        "created": parse_time(meta.get("creationTimestamp"))})
        for it in pvs.get("items", []):
            meta, sp, st = it["metadata"], it.get("spec", {}), it.get("status", {})
            claim = sp.get("claimRef") or {}
            out.append({"kind": "PV", "namespace": "", "name": meta["name"], "status": st.get("phase", ""),
                        "volume": "", "capacity": parse_quantity((sp.get("capacity") or {}).get("storage")),
                        "storage_class": sp.get("storageClassName", ""), "access": sp.get("accessModes", []),
                        "claim": "%s/%s" % (claim.get("namespace", ""), claim.get("name", "")) if claim else "",
                        "created": parse_time(meta.get("creationTimestamp"))})
        return out

    @staticmethod
    def _events(events):
        out = []
        for it in events.get("items", []):
            last = parse_time(it.get("lastTimestamp") or it.get("eventTime") or it.get("metadata", {}).get("creationTimestamp"))
            obj = it.get("involvedObject", {})
            out.append({"type": it.get("type", "Normal"), "reason": it.get("reason", ""), "message": it.get("message", ""),
                        "object": "%s/%s" % (obj.get("kind", ""), obj.get("name", "")), "namespace": it.get("metadata", {}).get("namespace", ""),
                        "count": it.get("count", 1), "last": last, "first": parse_time(it.get("firstTimestamp")) or last})
        out.sort(key=lambda e: e["last"], reverse=True)
        return out[:200]

    # -- AI ------------------------------------------------------------------

    @staticmethod
    def _ai(raw, pods):
        ai = {"split": None, "ollama": None}
        split_pods = [p for p in pods if p["namespace"] == "ai-split"]
        if split_pods or any(i["metadata"]["namespace"] == "ai-split" for i in raw["deployments"].get("items", [])):
            main = next((i for i in raw["deployments"].get("items", [])
                         if i["metadata"]["namespace"] == "ai-split" and i["metadata"]["name"] == "llama-main"), None)
            args = []
            if main:
                cs = main["spec"]["template"]["spec"].get("containers") or []
                args = cs[0].get("args", []) if cs else []

            def opt(flag):
                return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else ""

            svc = next((s for s in raw["services"].get("items", [])
                        if s["metadata"]["namespace"] == "ai-split" and s["metadata"]["name"] == "llama"), None)
            node_port = None
            if svc:
                for p in svc["spec"].get("ports", []) or []:
                    node_port = p.get("nodePort") or node_port
            job = next((j for j in raw["jobs"].get("items", []) if j["metadata"]["namespace"] == "ai-split"), None)
            shares = [float(x) for x in opt("-ts").split(",") if re.match(r"^[0-9.]+$", x)]
            rpc = [x.split(".")[0].replace("rpc-", "", 1) for x in opt("--rpc").split(",") if x]
            # one share more than RPC servers: the main node's own GPU (it comes last)
            tpl = (main or {}).get("spec", {}).get("template", {}).get("spec", {})
            gpu_node = (tpl.get("nodeSelector") or {}).get("kubernetes.io/hostname", "") if tpl.get("runtimeClassName") == "nvidia" else ""
            labels = rpc + [(gpu_node + " GPU") if gpu_node else "?"] * max(0, len(shares) - len(rpc))
            gate_ds = next((d for d in raw["daemonsets"].get("items", []) if d["metadata"]["namespace"] == "ai-split" and d["metadata"]["name"] == "llama-gate"), None)
            gate = None
            if gate_ds:
                gcs = gate_ds["spec"]["template"]["spec"].get("containers") or [{}]
                genv = {e.get("name"): e.get("value", "") for e in gcs[0].get("env", []) or []}
                gate = {"port": int(genv["PORT"]) if genv.get("PORT", "").isdigit() else None, "trusted": [x for x in genv.get("TRUSTED", "").split(",") if x],
                        "ready": gate_ds.get("status", {}).get("numberReady", 0), "desired": gate_ds.get("status", {}).get("desiredNumberScheduled", 0)}
            ai["split"] = {
                "load": load_progress(main, split_pods, shares, rpc, gpu_node),
                "gate": gate,
                "model": os.path.basename(opt("-m")), "alias": opt("--alias"), "ctx": opt("-c"),
                "ready": bool(main and main.get("status", {}).get("readyReplicas")),
                "loaded": bool(main and main.get("spec", {}).get("replicas", 1) > 0),
                "node_port": node_port, "auth": "--api-key-file" in args,
                "download": ("done" if job and job.get("status", {}).get("succeeded") else ("running" if job else "none")),
                "shares": [{"node": labels[i], "mib": shares[i], "gpu": i >= len(rpc)} for i in range(len(shares))],
                "pods": [{"name": p["name"], "node": p["node"], "status": p["status"], "ready": p["ready"], "restarts": p["restarts"]}
                         for p in split_pods],
            }
        oll = [p for p in pods if p["namespace"] == "ai-inference"]
        if oll:
            ai["ollama"] = {"pods": [{"name": p["name"], "node": p["node"], "status": p["status"], "ready": p["ready"]} for p in oll]}
        return ai

    # -- logs ----------------------------------------------------------------

    def logs(self, namespace, pod, container, lines):
        q = {"tailLines": str(lines), "timestamps": "true"}
        if container:
            q["container"] = container
        path = "/api/v1/namespaces/%s/pods/%s/log?%s" % (urllib.parse.quote(namespace), urllib.parse.quote(pod), urllib.parse.urlencode(q))
        return self.client.get(path, timeout=10, raw=True)
