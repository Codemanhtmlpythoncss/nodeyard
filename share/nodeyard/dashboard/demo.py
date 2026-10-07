"""A simulated cluster for `nodeyard --demo dashboard`, tests and screenshots.

Produces the same snapshot structure as kube.KubeSource.collect(). Values drift
smoothly with time (they're a function of the clock), so history can be
back-filled and every node's usage is the sum of its pods plus system overhead.
"""
import hashlib
import math
import time

GiB = 1024 ** 3
MiB = 1024 ** 2
GB = 10 ** 9

NODES = [
    dict(name="yard-1", ip="192.168.1.10", roles=["control-plane", "etcd", "master"], cpu=4, mem=8 * GiB, disk=240 * GB,
         os="Debian GNU/Linux 12 (bookworm)", kernel="6.6.51+rpt-rpi-2712", arch="arm64", over=(0.45, 1.3 * GiB)),
    dict(name="yard-2", ip="192.168.1.11", roles=["worker"], cpu=4, mem=4 * GiB, disk=120 * GB,
         os="Debian GNU/Linux 12 (bookworm)", kernel="6.6.51+rpt-rpi-v8", arch="arm64", over=(0.2, 0.6 * GiB)),
    dict(name="yard-3", ip="192.168.1.12", roles=["worker"], cpu=8, mem=32 * GiB, disk=512 * GB,
         os="Ubuntu 24.04.1 LTS", kernel="6.8.0-48-generic", arch="amd64", over=(0.3, 1.1 * GiB)),
    dict(name="yard-4", ip="192.168.1.13", roles=["worker"], cpu=4, mem=8 * GiB, disk=256 * GB,
         os="Fedora Linux 42 (Server Edition)", kernel="6.14.3-300.fc42.x86_64", arch="amd64", over=(0.25, 0.9 * GiB)),
]
DISK_USED = {"yard-1": 0.38, "yard-2": 0.52, "yard-3": 0.31, "yard-4": 0.88}

# (namespace, name, kind, replicas, image, cpu cores, mem bytes, pinned node or None)
APPS = [
    ("kube-system", "coredns", "Deployment", 1, "rancher/mirrored-coredns-coredns:1.12.0", 0.012, 24 * MiB, "yard-1"),
    ("kube-system", "local-path-provisioner", "Deployment", 1, "rancher/local-path-provisioner:v0.0.31", 0.002, 14 * MiB, "yard-1"),
    ("kube-system", "metrics-server", "Deployment", 1, "rancher/mirrored-metrics-server:v0.7.2", 0.01, 31 * MiB, "yard-1"),
    ("kube-system", "traefik", "Deployment", 1, "rancher/mirrored-library-traefik:3.3.6", 0.01, 68 * MiB, "yard-1"),
    ("kube-system", "svclb-traefik", "DaemonSet", 4, "rancher/klipper-lb:v0.4.9", 0.001, 2 * MiB, None),
    ("nextcloud", "nextcloud", "Deployment", 1, "nextcloud:30-apache", 0.18, 612 * MiB, "yard-3"),
    ("nextcloud", "postgres", "StatefulSet", 1, "postgres:16-alpine", 0.06, 220 * MiB, "yard-3"),
    ("nextcloud", "redis", "Deployment", 1, "redis:7-alpine", 0.01, 18 * MiB, "yard-3"),
    ("media", "jellyfin", "Deployment", 1, "jellyfin/jellyfin:10.10.3", 0.42, 880 * MiB, "yard-3"),
    ("media", "navidrome", "Deployment", 1, "deluan/navidrome:0.54.2", 0.03, 96 * MiB, "yard-4"),
    ("media", "transcoder", "Deployment", 1, "linuxserver/handbrake:1.9.0", 0.0, 0, "none"),
    ("home", "home-assistant", "Deployment", 1, "ghcr.io/home-assistant/home-assistant:2025.1", 0.09, 410 * MiB, "yard-2"),
    ("home", "pihole", "Deployment", 1, "pihole/pihole:2024.07.0", 0.025, 120 * MiB, "yard-2"),
    ("home", "zigbee2mqtt", "Deployment", 1, "koenkk/zigbee2mqtt:1.42.0", 0.0, 0, "crash"),
    ("home", "uptime-kuma", "Deployment", 1, "louislam/uptime-kuma:1.23.16", 0.04, 140 * MiB, "yard-4"),
    ("monitoring", "grafana", "Deployment", 1, "grafana/grafana:11.4.0", 0.05, 190 * MiB, "yard-4"),
    ("monitoring", "prometheus", "StatefulSet", 1, "prom/prometheus:v2.55.1", 0.14, 540 * MiB, "yard-3"),
    ("monitoring", "node-exporter", "DaemonSet", 4, "prom/node-exporter:v1.8.2", 0.004, 12 * MiB, None),
    ("ai-inference", "ollama", "Deployment", 3, "ollama/ollama:0.5.4", 0.08, 0.35 * GiB, None),
    ("ai-split", "llama-main", "Deployment", 1, "ghcr.io/ggml-org/llama.cpp:server-b11160", 0.22, 0.6 * GiB, "yard-3"),
    ("ai-split", "rpc-yard-2", "Deployment", 1, "ghcr.io/ggml-org/llama.cpp:rpc-b11160", 0.35, 1.0 * GiB, "yard-2"),
    ("ai-split", "rpc-yard-3", "Deployment", 1, "ghcr.io/ggml-org/llama.cpp:rpc-b11160", 0.6, 10.7 * GiB, "yard-3"),
    ("ai-split", "rpc-yard-4", "Deployment", 1, "ghcr.io/ggml-org/llama.cpp:rpc-b11160", 0.4, 4.4 * GiB, "yard-4"),
]
NAMESPACES = ["kube-system", "nextcloud", "media", "home", "monitoring", "ai-inference", "ai-split", "backup", "default"]


def _h(*parts):
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _suffix(*parts):
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:5]


def wave(key, t, period=240.0, amp=0.25):
    """A smooth, repeatable wobble in [-amp, amp] for this key at time t."""
    ph = (_h(key) % 628) / 100.0
    return amp * (math.sin(2 * math.pi * t / period + ph) * 0.7 + math.sin(2 * math.pi * t / (period * 0.37) + ph * 2) * 0.3)


class DemoSource:
    def __init__(self):
        self.boot = time.time() - 17 * 86400 - 3 * 3600
        self.rx_prev = {}
        self.agents = DemoAgents()

    # -- pods ----------------------------------------------------------------

    def _pods(self, t):
        pods = []
        names = [n["name"] for n in NODES]
        for ns, app, kind, replicas, image, cpu, mem, pin in APPS:
            for i in range(replicas):
                if pin == "none":
                    node, status, ready, ip = "", "Pending", "0/1", ""
                elif pin == "crash":
                    node, status, ready, ip = "yard-2", "CrashLoopBackOff", "0/1", "10.42.1.%d" % (20 + i)
                elif pin:
                    node, status, ready, ip = pin, "Running", "1/1", ""
                else:
                    node, status, ready, ip = names[i % len(names)], "Running", "1/1", ""
                if kind == "DaemonSet":
                    node = names[i]
                idx = names.index(node) if node in names else 0
                if not ip and node:
                    ip = "10.42.%d.%d" % (idx, 2 + _h(ns, app, i) % 240)
                suffix = {"StatefulSet": "-0", "DaemonSet": "-" + _suffix(app, i)}.get(kind, "-%s-%s" % (_suffix(app)[:9], _suffix(app, i)))
                name = app + suffix
                running = status == "Running"
                age = (6 + _h(app) % 14) * 86400 if ns != "ai-split" else 2 * 3600 + 11 * 60
                if status == "Pending":
                    age = 40 * 60
                elif status == "CrashLoopBackOff":
                    age = 26 * 3600
                cpu_use = cpu * (1 + wave((app, i), t, 180, 0.5)) if running else None
                mem_use = mem * (1 + wave((app, i, "m"), t, 900, 0.06)) if running else None
                pods.append({
                    "namespace": ns, "name": name, "status": status, "ready": ready, "restarts": 14 if status == "CrashLoopBackOff" else (_h(app) % 3 if running else 0),
                    "node": node, "ip": ip, "host_ip": next((n["ip"] for n in NODES if n["name"] == node), ""),
                    "host_network": app in ("svclb-traefik", "node-exporter"), "created": t - age, "started": t - age + 3,
                    "owner": "%s/%s" % ({"Deployment": "ReplicaSet", "StatefulSet": "StatefulSet", "DaemonSet": "DaemonSet"}[kind], app),
                    "qos": "Burstable", "cpu": cpu_use, "mem": mem_use,
                    "uid": "%08x-%04x-4%03x-8000-%012x" % (_h(ns, app, i), _h(app) & 0xffff, _h(name) & 0xfff, _h(ns, name, i, "u")),
                    "containers": [{"name": app, "image": image, "ready": running, "restarts": 14 if status == "CrashLoopBackOff" else 0,
                                    "state": "running" if running else "waiting", "reason": "" if running else ("CrashLoopBackOff" if pin == "crash" else "Unschedulable")}],
                    "labels": {"app": app},
                })
        return pods

    # -- one snapshot ---------------------------------------------------------

    def collect(self, t=None):
        t = t or time.time()
        pods = self._pods(t)
        nodes = []
        for spec in NODES:
            mine = [p for p in pods if p["node"] == spec["name"]]
            oc, om = spec["over"]
            cpu_used = oc * (1 + wave((spec["name"], "c"), t, 200, 0.3)) + sum(p["cpu"] or 0 for p in mine)
            mem_used = om * (1 + wave((spec["name"], "m"), t, 700, 0.04)) + sum(p["mem"] or 0 for p in mine)
            cpu_used = min(cpu_used, spec["cpu"] * 0.98)
            mem_used = min(mem_used, spec["mem"] * 0.97)
            rate_rx = max(2e4, (1.6e5 if spec["name"] == "yard-3" else 4e4) * (1 + wave((spec["name"], "rx"), t, 90, 0.9)))
            rate_tx = max(1e4, rate_rx * (1.8 if spec["name"] == "yard-3" else 0.6))
            up = t - self.boot
            nodes.append({
                "name": spec["name"], "ready": True, "status": "Ready", "roles": spec["roles"], "internal_ip": spec["ip"], "external_ip": "",
                "addresses": [{"type": "InternalIP", "address": spec["ip"]}, {"type": "Hostname", "address": spec["name"]}],
                "os": spec["os"], "kernel": spec["kernel"], "arch": spec["arch"], "runtime": "containerd://2.0.4-k3s1", "kubelet": "v1.32.3+k3s1",
                "cpu_cores": float(spec["cpu"]), "cpu_used": cpu_used, "mem_total": float(spec["mem"]), "mem_used": mem_used,
                "pods_running": sum(1 for p in mine if p["status"] == "Running"), "pods_total": len(mine), "pods_capacity": 110,
                "disk_total": float(spec["disk"]), "disk_used": spec["disk"] * DISK_USED[spec["name"]],
                "net_rx": rate_rx * up, "net_tx": rate_tx * up, "net_rx_rate": rate_rx, "net_tx_rate": rate_tx,
                "conditions": {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False", "NetworkUnavailable": "False"},
                "labels": {"kubernetes.io/arch": spec["arch"], "kubernetes.io/hostname": spec["name"], "kubernetes.io/os": "linux"},
                "taints": [], "created": self.boot, "kubelet_start": t - up + 40 + _h(spec["name"]) % 30, "unschedulable": False,
                "pod_cidr": "10.42.%d.0/24" % NODES.index(spec), "images": 12 + _h(spec["name"]) % 20,
                "allocatable": {"cpu": float(spec["cpu"]), "memory": spec["mem"] * 0.96, "pods": 110},
            })
        return {
            "cluster": {"name": "homelab", "k3s_version": "v1.32.3+k3s1", "api_server": "https://192.168.1.10:6443", "created": self.boot,
                        "pod_cidr": "10.42.0.0/16", "service_cidr": "10.43.0.0/16"},
            "nodes": nodes, "pods": pods, "workloads": self._workloads(t), "services": self._services(t), "ingresses": self._ingresses(t),
            "volumes": self._volumes(t), "events": self._events(t), "namespaces": sorted(NAMESPACES),
            "ai": self._ai(pods), "errors": [], "bench": DEMO_BENCH,
        }

    def _workloads(self, t):
        out = []
        for ns, app, kind, replicas, image, _c, _m, pin in APPS:
            ready = 0 if pin in ("none", "crash") else replicas
            out.append({"kind": kind, "namespace": ns, "name": app, "desired": replicas, "ready": ready, "available": ready,
                        "images": [image], "created": t - (6 + _h(app) % 14) * 86400})
        out.append({"kind": "CronJob", "namespace": "backup", "name": "nightly-snapshot", "desired": 0, "ready": 0, "available": 0,
                    "schedule": "0 3 * * *", "suspended": False, "last": t - 8 * 3600, "images": [], "created": t - 12 * 86400})
        out.append({"kind": "Job", "namespace": "backup", "name": "nightly-snapshot-29012345", "desired": 1, "ready": 1, "available": 0,
                    "failed": 0, "images": ["rancher/k3s:v1.32.3-k3s1"], "created": t - 8 * 3600})
        return out

    def _services(self, t):
        def svc(ns, name, typ, cip, ports, node_ports=(), ext=(), ep=1):
            return {"namespace": ns, "name": name, "type": typ, "cluster_ip": cip, "external_ips": list(ext), "ports": ports,
                    "node_ports": list(node_ports), "selector": {"app": name}, "endpoints": ep, "created": t - 9 * 86400}
        ips = [n["ip"] for n in NODES]
        return [
            svc("default", "kubernetes", "ClusterIP", "10.43.0.1", ["443/TCP"]),
            svc("kube-system", "kube-dns", "ClusterIP", "10.43.0.10", ["53/UDP", "53/TCP", "9153/TCP"]),
            svc("kube-system", "metrics-server", "ClusterIP", "10.43.77.41", ["443/TCP"]),
            svc("kube-system", "traefik", "LoadBalancer", "10.43.201.5", ["80/TCP (node 30080)", "443/TCP (node 30443)"], (30080, 30443), ips, 1),
            svc("nextcloud", "nextcloud", "ClusterIP", "10.43.118.20", ["80/TCP"]),
            svc("nextcloud", "postgres", "ClusterIP", "10.43.118.21", ["5432/TCP"]),
            svc("media", "jellyfin", "NodePort", "10.43.140.8", ["8096/TCP (node 30096)"], (30096,)),
            svc("media", "navidrome", "ClusterIP", "10.43.140.9", ["4533/TCP"]),
            svc("media", "transcoder", "ClusterIP", "10.43.140.10", ["8080/TCP"], (), (), 0),
            svc("home", "home-assistant", "NodePort", "10.43.150.2", ["8123/TCP (node 30123)"], (30123,)),
            svc("home", "pihole", "LoadBalancer", "10.43.150.3", ["53/UDP", "80/TCP"], (), [ips[1]]),
            svc("home", "uptime-kuma", "ClusterIP", "10.43.150.4", ["3001/TCP"]),
            svc("monitoring", "grafana", "NodePort", "10.43.160.3", ["3000/TCP (node 30300)"], (30300,)),
            svc("monitoring", "prometheus", "ClusterIP", "10.43.160.4", ["9090/TCP"]),
            svc("ai-inference", "ollama", "ClusterIP", "10.43.170.2", ["11434/TCP"], (), (), 3),
            svc("ai-split", "llama", "NodePort", "10.43.180.9", ["8080/TCP (node 31435)"], (31435,)),
        ]

    def _ingresses(self, t):
        return [
            {"namespace": "nextcloud", "name": "nextcloud", "class": "traefik", "hosts": ["cloud.home.lan"], "paths": ["/ -> nextcloud"],
             "addresses": ["192.168.1.10", "192.168.1.11", "192.168.1.12", "192.168.1.13"], "created": t - 9 * 86400},
            {"namespace": "media", "name": "jellyfin", "class": "traefik", "hosts": ["media.home.lan"], "paths": ["/ -> jellyfin"],
             "addresses": ["192.168.1.10", "192.168.1.11", "192.168.1.12", "192.168.1.13"], "created": t - 9 * 86400},
            {"namespace": "monitoring", "name": "grafana", "class": "traefik", "hosts": ["grafana.home.lan"], "paths": ["/ -> grafana"],
             "addresses": ["192.168.1.10", "192.168.1.11", "192.168.1.12", "192.168.1.13"], "created": t - 5 * 86400},
        ]

    def _volumes(self, t):
        specs = [("nextcloud", "nextcloud-data", 200), ("nextcloud", "postgres-data", 20), ("media", "jellyfin-config", 5),
                 ("monitoring", "grafana-data", 2), ("monitoring", "prometheus-data", 50), ("ai-inference", "ollama-models", 100)]
        out = []
        for ns, name, gi in specs:
            pv = "pvc-%s" % _suffix(name)
            out.append({"kind": "PVC", "namespace": ns, "name": name, "status": "Bound", "volume": pv, "capacity": gi * GiB,
                        "storage_class": "local-path", "access": ["ReadWriteOnce"], "created": t - 9 * 86400})
            out.append({"kind": "PV", "namespace": "", "name": pv, "status": "Bound", "volume": "", "capacity": gi * GiB, "storage_class": "local-path",
                        "access": ["ReadWriteOnce"], "claim": "%s/%s" % (ns, name), "created": t - 9 * 86400})
        return out

    def _events(self, t):
        def ev(kind, reason, msg, obj, ns, count, ago):
            return {"type": kind, "reason": reason, "message": msg, "object": obj, "namespace": ns, "count": count,
                    "last": t - ago, "first": t - ago - (count * 40 if count > 1 else 0)}
        return [
            ev("Warning", "BackOff", "Back-off restarting failed container zigbee2mqtt in pod zigbee2mqtt-%s" % _suffix("zigbee2mqtt")[:5], "Pod/zigbee2mqtt", "home", 142, 35),
            ev("Warning", "FailedScheduling", "0/4 nodes are available: 1 Insufficient memory, 3 Insufficient cpu.", "Pod/transcoder", "media", 31, 120),
            ev("Normal", "Pulled", "Container image \"ghcr.io/ggml-org/llama.cpp:server-b11160\" already present on machine", "Pod/llama-main", "ai-split", 1, 410),
            ev("Normal", "Started", "Started container llama-server", "Pod/llama-main", "ai-split", 1, 405),
            ev("Normal", "Scheduled", "Successfully assigned ai-split/rpc-yard-3 to yard-3", "Pod/rpc-yard-3", "ai-split", 1, 7300),
            ev("Normal", "SuccessfulCreate", "Created pod: nightly-snapshot-29012345-x7k2p", "Job/nightly-snapshot-29012345", "backup", 1, 8 * 3600),
            ev("Normal", "Completed", "Job completed", "Job/nightly-snapshot-29012345", "backup", 1, 8 * 3600 - 62),
            ev("Normal", "NodeReady", "Node yard-4 status is now: NodeReady", "Node/yard-4", "", 1, 3 * 86400),
            ev("Warning", "ImagePull", "Failed to pull image: temporary DNS failure (retrying)", "Pod/uptime-kuma", "home", 2, 2 * 86400),
            ev("Normal", "Pulled", "Successfully pulled image \"louislam/uptime-kuma:1.23.16\"", "Pod/uptime-kuma", "home", 1, 2 * 86400 - 40),
        ]

    def _ai(self, pods):
        sp = [p for p in pods if p["namespace"] == "ai-split"]
        return {
            "split": {
                "model": "Qwen3-Coder-30B-A3B-Instruct-abliterated.i1-Q4_K_M.gguf", "alias": "qwen3-coder-30b", "ctx": "8192", "ready": True,
                "node_port": None, "auth": True, "download": "done", "loaded": True,
                "gate": {"port": 31435, "trusted": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"], "ready": 4, "desired": 4},
                "shares": [{"node": "yard-3", "mib": 10963}, {"node": "yard-4", "mib": 4915}, {"node": "yard-2", "mib": 1843}],
                "pods": [{"name": p["name"], "node": p["node"], "status": p["status"], "ready": p["ready"], "restarts": p["restarts"]} for p in sp],
            },
            "ollama": {"pods": [{"name": p["name"], "node": p["node"], "status": p["status"], "ready": p["ready"]} for p in pods if p["namespace"] == "ai-inference"]},
        }

    # -- history and logs ------------------------------------------------------

    def seed_history(self, points, interval):
        from analysis import sample
        now = time.time()
        out = []
        for i in range(points, 0, -1):
            t = now - i * interval
            st = self.collect(t)
            self.agents.annotate(st, t)
            out.append(sample(st, t))
        return out

    def logs(self, namespace, pod, container, lines):
        t = time.time()
        out = []
        for i in range(min(lines, 120)):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - (120 - i) * 7))
            if "zigbee" in pod:
                msg = ["Zigbee2MQTT:info  Logging to console", "Zigbee2MQTT:error Error: Error while opening serial port '/dev/ttyUSB0': ENOENT: no such file or directory",
                       "Zigbee2MQTT:error Failed to start zigbee", "Zigbee2MQTT:error Exiting..."][i % 4]
            elif "llama" in pod or "rpc" in pod:
                msg = ["slot update_slots: id  0 | task 1180 | prompt processing progress, n_past = 512, n_tokens = 512", "srv  log_server_r: request: POST /v1/chat/completions 200",
                       "slot print_timing: id  0 | task 1180 | generation eval time = 3120.41 ms / 34 tokens (11.2 tokens per second)"][i % 3]
            else:
                msg = "level=info msg=\"request handled\" path=/healthz status=200 duration=%dms" % (1 + i % 7)
            out.append("%s %s" % (ts, msg))
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- node agents

PROCESS_NAMES = {"nextcloud": "apache2", "postgres": "postgres", "redis": "redis-server", "jellyfin": "jellyfin", "navidrome": "navidrome",
                 "home-assistant": "python3", "pihole": "pihole-FTL", "uptime-kuma": "node", "grafana": "grafana", "prometheus": "prometheus",
                 "ollama": "ollama", "llama-main": "llama-server", "rpc-yard-2": "rpc-server", "rpc-yard-3": "rpc-server", "rpc-yard-4": "rpc-server",
                 "coredns": "coredns", "traefik": "traefik", "metrics-server": "metrics-server", "local-path-provisioner": "local-path-provisioner",
                 "node-exporter": "node_exporter", "svclb-traefik": "klipper-lb"}
SYSTEM_PROCESSES = [("k3s-server", "root", 0.10, 560, "k3s.service", "/usr/local/bin/k3s server"), ("containerd", "root", 0.03, 160, "k3s.service", "containerd -c /var/lib/rancher/k3s/agent/etc/containerd/config.toml"),
                    ("systemd-journald", "root", 0.004, 55, "systemd-journald.service", "/lib/systemd/systemd-journald"), ("tailscaled", "root", 0.01, 48, "tailscaled.service", "/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state"),
                    ("sshd", "root", 0.001, 9, "ssh.service", "sshd: /usr/sbin/sshd -D"), ("systemd", "root", 0.0, 12, "init.scope", "/sbin/init")]


DEMO_BENCH = {
    "yard-1": {"time": 1791300000, "cores": 4, "mem_copy_1core_gbs": 6.1, "mem_copy_all_gbs": 9.8, "cpu_1core": 21.4, "cpu_all": 84.0,
               "disk": {"write_mbs": 38.2, "read_mbs": 88.5, "rand_read_iops": 2400, "test_size": 1073741824}},
    "yard-3": {"time": 1791300040, "cores": 8, "mem_copy_1core_gbs": 9.4, "mem_copy_all_gbs": 18.7, "cpu_1core": 30.2, "cpu_all": 151.0,
               "disk": {"write_mbs": 412.0, "read_mbs": 1860.0, "rand_read_iops": 61000, "test_size": 1073741824}},
}


def demo_hardware(node):
    name = node["name"]
    if node["arch"] == "arm64":
        return {"cpu": {"model": "Raspberry Pi 5 Model B Rev 1.0" if name == "yard-1" else "Raspberry Pi 4 Model B Rev 1.4", "core": "Cortex-A76" if name == "yard-1" else "Cortex-A72",
                        "vendor": "ARM", "arch": "aarch64", "cores": 4, "threads": 4, "max_mhz": 2400 if name == "yard-1" else 1800, "min_mhz": 1500 if name == "yard-1" else 600,
                        "bogomips": 108.0, "flags": ["aes", "asimd", "asimddp"], "caches": [{"level": 1, "type": "Data", "size": 65536, "shared": "0"},
                        {"level": 2, "type": "Unified", "size": 524288, "shared": "0"}, {"level": 3, "type": "Unified", "size": 2097152, "shared": "0-3"}]},
                "memory": {"total": int(node["mem_total"]), "swap": 0}, "board": {"model": "Raspberry Pi 5 Model B Rev 1.0" if name == "yard-1" else "Raspberry Pi 4 Model B Rev 1.4", "board": "", "bios": "", "bios_date": ""},
                "disks": [{"name": "mmcblk0", "size": 128 * 10 ** 9, "kind": "SD card / eMMC", "model": "SD128", "vendor": "", "removable": False, "scheduler": "mq-deadline"}],
                "nics": [{"name": "eth0", "wireless": False, "speed_mbps": 1000, "up": True, "mtu": 1500}, {"name": "wlan0", "wireless": True, "speed_mbps": None, "up": True, "mtu": 1500}],
                "gpus": [{"bus": "1002000000.v3d", "vendor": "", "device": "", "driver": "v3d", "boot": True, "model": "", "driver_version": "", "vram": None, "name_full": "Broadcom VideoCore VII"}], "kernel": "6.12.47+rpt-rpi-2712"}
    return {"cpu": {"model": "Intel(R) Core(TM) i5-8259U CPU @ 2.30GHz" if name == "yard-3" else "Intel(R) Core(TM) i7-6500U CPU @ 2.50GHz", "core": "", "vendor": "GenuineIntel",
                    "arch": "x86_64", "cores": int(node["cpu_cores"]) // 2, "threads": int(node["cpu_cores"]), "max_mhz": 3800 if name == "yard-3" else 3100, "min_mhz": 400,
                    "bogomips": 4599.9, "flags": ["sse4_2", "avx", "avx2", "fma", "f16c", "aes"], "caches": [{"level": 1, "type": "Data", "size": 32768, "shared": "0,4"},
                    {"level": 2, "type": "Unified", "size": 262144, "shared": "0,4"}, {"level": 3, "type": "Unified", "size": 6291456, "shared": "0-7"}]},
            "memory": {"total": int(node["mem_total"]), "swap": 2 * 1024 ** 3}, "board": {"model": "Intel(R) Client Systems NUC8i5BEH", "board": "Intel Corporation NUC8BEB", "bios": "BECFL357.86A.0089", "bios_date": "03/09/2022"},
            "disks": [{"name": "nvme0n1", "size": 512 * 10 ** 9, "kind": "NVMe SSD", "model": "Samsung SSD 970 EVO Plus 500GB", "vendor": "", "removable": False, "scheduler": "none"},
                      {"name": "sda", "size": 2 * 10 ** 12, "kind": "HDD (USB)", "model": "My Passport 2626", "vendor": "WD", "removable": False, "scheduler": "mq-deadline"}],
            "nics": [{"name": "eno1", "wireless": False, "speed_mbps": 1000, "up": True, "mtu": 1500}, {"name": "tailscale0", "wireless": False, "speed_mbps": None, "up": True, "mtu": 1280}],
            "gpus": [{"bus": "0000:00:02.0", "vendor": "0x8086", "device": "0x3ea5", "driver": "i915", "boot": True, "model": "", "driver_version": "", "vram": None},
                     {"bus": "0000:01:00.0", "vendor": "0x10de", "device": "0x1c8c", "driver": "nvidia", "boot": False, "model": "NVIDIA GeForce GTX 1050 Ti", "driver_version": "550.163.01", "vram": None}], "kernel": "6.12.107+deb13-amd64"}


class DemoAgents:
    """What the node agents would report for the demo cluster."""

    def _hw(self, node, t, cpu_frac):
        name = node["name"]
        pi = node["arch"] == "arm64"
        maxmhz = 2400 if pi else 4200
        minmhz = 600 if pi else 800
        cores = []
        for i in range(int(node["cpu_cores"])):
            use = max(0.0, min(100.0, 100 * cpu_frac * (1 + wave((name, "core", i), t, 60, 0.7))))
            mhz = minmhz + (maxmhz - minmhz) * min(1.0, use / 70.0 + 0.05 * (1 + wave((name, "f", i), t, 30, 1)))
            cores.append({"id": i, "mhz": round(mhz), "min": minmhz, "max": maxmhz, "governor": "ondemand" if pi else "powersave", "online": True, "use": round(use, 1)})
        return cores

    def payload_for(self, node, state, t):
        name = node["name"]
        cpu_frac = (node["cpu_used"] or 0) / node["cpu_cores"]
        mem_total = node["mem_total"]
        used = node["mem_used"]
        cached = min(mem_total - used, 0.35 * mem_total)
        pods = [p for p in state["pods"] if p["node"] == name and p["mem"]]
        procs = []
        pid = 800
        for comm, user, cpu, rss_mib, svc, cmd in SYSTEM_PROCESSES:
            pid += 7
            procs.append({"pid": pid, "ppid": 1, "name": comm, "state": "S", "threads": 12, "user": user, "rss": rss_mib * MiB, "anon": int(rss_mib * MiB * .8),
                          "file": int(rss_mib * MiB * .2), "shmem": 0, "swap": 0, "cpu": round(cpu * 100 * (1 + wave((name, comm), t, 50, .5)) / node["cpu_cores"], 1),
                          "cmd": cmd, "group": {"kind": "service", "name": svc}, "started": t - 17 * 86400})
        for p in pods:
            pid += 31
            comm = PROCESS_NAMES.get(p["name"].rsplit("-", 2)[0] if p["owner"].startswith("ReplicaSet") else p["name"].rsplit("-", 1)[0], p["name"].split("-")[0])
            procs.append({"pid": pid, "ppid": 4200, "name": comm, "state": "S", "threads": 8 + _h(p["name"]) % 40, "user": "root" if _h(p["name"]) % 3 else "1000",
                          "rss": int(p["mem"] * 0.92), "anon": int(p["mem"] * 0.6), "file": int(p["mem"] * 0.32), "shmem": 0, "swap": 0,
                          "cpu": round((p["cpu"] or 0) / node["cpu_cores"] * 100, 1), "cmd": "/usr/bin/%s --config /etc/%s.yaml" % (comm, comm),
                          "group": {"kind": "pod", "uid": p["uid"], "container": "%012x" % _h(p["name"], "c")}, "started": p["started"]})
        if name == "yard-1":
            procs.append({"pid": 2701, "ppid": 1, "name": "mysqld", "state": "S", "threads": 38, "user": "mysql", "rss": 363 * MiB, "anon": 330 * MiB, "file": 33 * MiB,
                          "shmem": 0, "swap": 0, "cpu": 0.8, "cmd": "/usr/sbin/mariadbd", "group": {"kind": "service", "name": "mariadb.service"}, "started": t - 9 * 86400})
        procs.sort(key=lambda x: -x["rss"])
        groups = {}
        for p in procs:
            g = p["group"]
            key = g.get("uid") or g.get("name")
            a = groups.setdefault(key, {"kind": g["kind"], "uid": g.get("uid", ""), "name": g.get("name", ""), "rss": 0, "cpu": 0.0, "procs": 0})
            a["rss"] += p["rss"]
            a["cpu"] = round(a["cpu"] + p["cpu"], 1)
            a["procs"] += 1
        temp = (45 if node["arch"] == "arm64" else 41) + 38 * cpu_frac + 5 * (1 + wave((name, "t"), t, 120, 1))
        cores = self._hw(node, t, cpu_frac)
        return {
            "hardware": demo_hardware(node),
            "node": name, "time": t, "uptime": t - self.boot_of(name), "load": [round(node["cpu_cores"] * cpu_frac * 1.1, 2), round(node["cpu_cores"] * cpu_frac, 2), round(node["cpu_cores"] * cpu_frac * .9, 2)],
            "cpu": {"model": "Raspberry Pi 5 Model B Rev 1.0" if name == "yard-1" else "Cortex-A72 (Raspberry Pi 4 Model B)" if name == "yard-2" else "Intel(R) Core(TM) i5-8259U CPU @ 2.30GHz" if name == "yard-3" else "Intel(R) Core(TM) i7-6500U CPU @ 2.50GHz",
                    "cores": cores, "use": round(100 * cpu_frac, 1)},
            "memory": {"MemTotal": int(mem_total), "MemFree": int(max(0, mem_total - used - cached)), "MemAvailable": int(mem_total - used * .85), "Buffers": int(cached * .1),
                       "Cached": int(cached * .9), "SReclaimable": int(cached * .05), "AnonPages": int(used * .8), "Shmem": 40 * MiB, "Slab": 160 * MiB, "SwapTotal": 0, "SwapFree": 0,
                       "Dirty": 2 * MiB, "Mapped": int(used * .15), "KernelStack": 6 * MiB, "PageTables": 12 * MiB},
            "pressure": {"cpu": {"some": round(cpu_frac * 4, 2)}, "memory": {"some": round(max(0, used / mem_total - .7) * 20, 2)}, "io": {"some": 0.3}},
            "vmstat": {"oom_kill": 0, "pgmajfault": 1204, "pswpin": 0, "pswpout": 0}, "disks": [], "temps": [{"name": "cpu_thermal" if node["arch"] == "arm64" else "x86_pkg_temp", "c": round(temp, 1)}],
            "power": {}, "counts": {"total": 180 + len(pods) * 4, "running": 2, "sleeping": 170 + len(pods) * 4, "zombie": 0, "threads": 900 + len(pods) * 30}, "files": [2400],
            "processes": procs[:60], "groups": sorted(groups.values(), key=lambda g: -g["rss"])}

    @staticmethod
    def boot_of(name):
        return time.time() - (17 * 86400 + 3 * 3600)

    def annotate(self, state, t=None):
        import agents as agentsmod
        t = t or time.time()
        state["agents"] = {"installed": True, "ready": len(state["nodes"]), "pods": len(state["nodes"])}
        self._last = {}
        for n in state["nodes"]:
            data = self.payload_for(n, state, t)
            self._last[n["name"]] = data
            n["hw"] = agentsmod.summarise(data)
            n["hw"]["oom_new"] = 0

    def payload(self, state, node=None):
        import agents as agentsmod
        poller = agentsmod.AgentPoller("")
        poller.payloads = dict(getattr(self, "_last", {}))
        return poller.payload(state, node)
