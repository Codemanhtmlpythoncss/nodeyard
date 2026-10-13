"""What is filling each node's disk: the biggest folders and files, what they are, and how to free the space.

The node the dashboard runs on is scanned directly (no pod: it works even when that disk is so full that the node
refuses new pods). Every other node gets a short-lived, read-only helper pod with the node's root filesystem mounted
at /host. The helper's answer comes back as its termination message, which the node's kubelet reports to the API
server itself, so it arrives even when the control node can't reach that kubelet (`kubectl logs` can't).

Nothing is changed or deleted: the page shows what each big folder is and the command that frees it, for you to run.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

NS = "nodeyard-shell"
IMAGE = "busybox:1.36"
SCAN_DEADLINE = 300

# One shell script for busybox (the helper pod) and coreutils (this machine). $1 is the root to scan.
# Prints:  DISK totalKiB usedKiB freeKiB / FILE KiB path (the 6 biggest files) / DU depth KiB path (the biggest at each
# depth, 6 deep: container images sit at /var/lib/rancher/k3s/agent/containerd). Most useful first, as a termination
# message only keeps the first 4 KB.
SCAN = r'''R="${1:-/}"; P="${R%/}"; T="$(printf '\t')"
set -- $(df -Pk "$R" 2>/dev/null | tail -1); echo "DISK $2 $3 $4"
find "$R" -xdev -type f -size +262144k 2>/dev/null | head -300 | while IFS= read -r f; do
  s=$(du -k "$f" 2>/dev/null | cut -f1); echo "$s$T${f#$P}"; done | sort -t "$T" -k1,1rn | head -6 | awk -F"$T" '{print "FILE\t" $1 "\t" $2}'
du -xk -d 6 "$R" 2>/dev/null | awk -F"$T" -v p="$P" '{path=$2; if (p != "") path=substr(path, length(p)+1); if (path == "") path="/";
  n=split(path, a, "/") - 1; if (path == "/") n=0; print n "\t" $1 "\t" path}' |
  sort -t "$T" -k1,1n -k2,2rn | awk -F"$T" '{c[$1]++; if (c[$1] <= ($1 <= 1 ? 12 : 7)) print "DU\t" $1 "\t" $2 "\t" $3}'
true'''

# What a folder is, and how to free it (the most specific rule wins).
RULES = [
    ("/var/lib/rancher/k3s/agent/containerd", "Container images", "Images and layers of every container this node has run.",
     "sudo k3s crictl rmi --prune"),
    ("/var/lib/rancher/k3s/agent/images", "k3s's bundled images", "Images k3s ships with (imported at start).", ""),
    ("/var/lib/rancher/k3s/storage", "Persistent volumes", "Data of the cluster's local-path volumes (see Volume claims below).",
     "Delete volume claims you no longer need"),
    ("/var/lib/rancher/k3s/server/db", "Kubernetes datastore", "k3s's own database.", ""),
    ("/var/lib/rancher", "Kubernetes (k3s)", "k3s and everything it keeps.", ""),
    ("/var/lib/kubelet/pods", "Pod scratch space", "emptyDir volumes of the pods on this node.", "Restart or delete the pod that writes a lot"),
    ("/var/log/pods", "Container logs", "What the containers on this node have printed.", "Restart the chatty pod; k3s rotates these logs"),
    ("/var/lib/nodeyard/models", "AI models", "Downloaded model files.", "Remove models you don't use in AI › Models (or: nodeyard ai split rm FILE)"),
    ("/var/lib/nodeyard/rpc-cache", "AI weight caches", "Model layers kept by the split model's RPC servers.", "sudo nodeyard ai split clean"),
    ("/var/lib/nodeyard/llama.cpp", "llama.cpp builds", "Downloaded llama.cpp programs.", "sudo nodeyard ai split clean"),
    ("/var/lib/nodeyard/dashboard", "Dashboard data", "Music cache, wallpapers, history.", "Lower Music › Settings › recent song cache; remove unused wallpapers"),
    ("/var/lib/nodeyard", "nodeyard data", "", ""),
    ("/var/lib/docker", "Docker", "Docker images, containers and volumes.", "docker system prune  (add -a to drop every unused image)"),
    ("/var/lib/containerd", "containerd", "A separate containerd's images.", ""),
    ("/var/lib/ollama", "Ollama models", "Models Ollama downloaded.", "ollama rm MODEL"),
    ("/var/log/journal", "System journal", "The systemd journal (every service's log).", "sudo journalctl --vacuum-size=200M"),
    ("/var/log", "Logs", "System and program logs.", "Find the one big file and rotate or empty it"),
    ("/var/cache/apt", "Package downloads", "Debian/Ubuntu package files kept after installs.", "sudo apt clean"),
    ("/var/cache/pacman", "Package downloads", "Arch package files kept after installs.", "sudo pacman -Sc"),
    ("/var/cache", "Caches", "Programs' caches.", ""),
    ("/var/lib/snapd", "Snap packages", "Snap apps and their old versions.", "sudo snap set system refresh.retain=2"),
    ("/snap", "Snap packages", "Snap apps and their old versions.", "sudo snap set system refresh.retain=2"),
    ("/var/lib/flatpak", "Flatpak apps", "", "flatpak uninstall --unused"),
    ("/var/tmp", "Temporary files", "", ""),
    ("/tmp", "Temporary files", "", "Usually safe to clear (they also go at a reboot)"),
    ("/home", "Home folders", "People's own files.", ""),
    ("/root", "root's home folder", "", ""),
    ("/swapfile", "Swap file", "Memory overflow space on disk.", ""),
    ("/swap.img", "Swap file", "Memory overflow space on disk.", ""),
    ("/boot", "Boot files and kernels", "", "Remove old kernels (sudo apt autoremove)"),
    ("/usr", "Installed software", "Programs and libraries.", ""),
    ("/opt", "Add-on software", "", ""),
    ("/srv", "Served files", "", ""),
    ("/mnt", "Mounted disks", "", ""),
    ("/media", "Mounted disks", "", ""),
]


def describe(path):
    for prefix, label, what, tip in RULES:
        if path == prefix or path.startswith(prefix + "/"):
            if prefix == "/home" and path.count("/") >= 2:
                return "%s's files" % path.split("/")[2], what, tip
            return label, what, tip
    return os.path.basename(path.rstrip("/")) or "/", "", ""


def parse(text):
    """The scan's lines -> {"disk": {...}, "dirs": [...], "files": [...]} (sizes in bytes)."""
    disk, dirs, files = None, [], []
    for line in (text or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split(" ")
        try:
            if parts[0] == "DISK" and len(parts) >= 4:
                disk = {"total": int(parts[1]) * 1024, "used": int(parts[2]) * 1024, "free": int(parts[3]) * 1024}
            elif parts[0] == "DU" and len(parts) >= 4:
                dirs.append({"depth": int(parts[1]), "bytes": int(parts[2]) * 1024, "path": "\t".join(parts[3:])})
            elif parts[0] == "FILE" and len(parts) >= 3:
                files.append({"bytes": int(parts[1]) * 1024, "path": "\t".join(parts[2:])})
        except ValueError:
            continue
    return {"disk": disk, "dirs": dirs, "files": files}


def hotspots(dirs, limit=10):
    """The folders that explain the space: follow every big folder (3% of the disk's used space or more) down to the
    deepest ones that are still big, so two big neighbours (container images and AI models) both show."""
    children = {}
    for d in dirs:
        if d["depth"] > 0:
            parent = os.path.dirname(d["path"]) or "/"
            children.setdefault(parent, []).append(d)
    root = next((d for d in dirs if d["path"] == "/"), None)
    used = (root["bytes"] if root else sum(d["bytes"] for d in dirs if d["depth"] == 1)) or 1
    floor = used * 0.03

    def leaves(d):
        big = [k for k in children.get(d["path"], []) if k["bytes"] >= floor]
        if not big:
            return [d]
        out = []
        for k in big:
            out += leaves(k)
        return out

    found = []
    for top in (d for d in dirs if d["depth"] == 1 and d["bytes"] >= floor):
        found += leaves(top)
    out = []
    for d in found:
        label, what, tip = describe(d["path"])
        out.append({"path": d["path"], "bytes": d["bytes"], "label": label, "what": what, "tip": tip})
    out.sort(key=lambda x: -x["bytes"])
    return out[:limit]


class DiskUsage:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.kubeconfig = getattr(args, "kubeconfig", "") or ""
        self.path = os.path.join(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", "disk-usage.json")
        self.lock = threading.Lock()
        self.results = self._load()
        self.running = set()
        k3s = shutil.which("k3s") or ("/usr/local/bin/k3s" if os.path.exists("/usr/local/bin/k3s") else "")
        kubectl = shutil.which("kubectl")
        self.kubectl = [k3s, "kubectl"] if k3s else ([kubectl] if kubectl else None)

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.results, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _nodes(self):
        st = (self.ctx.store.snapshot().get("state") or {})
        return st.get("nodes") or []

    def _local_node(self, nodes):
        """The node this dashboard runs on (its name is normally the host name; else the control plane)."""
        host = socket.gethostname().split(".")[0].lower()
        for n in nodes:
            if n["name"].lower() == host:
                return n["name"]
        cp = [n["name"] for n in nodes if any(r in ("control-plane", "master") for r in n.get("roles", []))]
        return cp[0] if len(cp) == 1 else ""

    def view(self):
        nodes = self._nodes()
        local = self._local_node(nodes)
        out = []
        with self.lock:
            for n in nodes:
                r = self.results.get(n["name"]) or {}
                out.append({"node": n["name"], "ready": bool(n.get("ready")), "local": n["name"] == local,
                            "disk_total": n.get("disk_total"), "disk_used": n.get("disk_used"),
                            "scanning": n["name"] in self.running, "at": r.get("at"), "error": r.get("error", ""), "took": r.get("took"),
                            "disk": r.get("disk"), "hotspots": r.get("hotspots") or [], "files": r.get("files") or [], "dirs": r.get("dirs") or []})
        return {"nodes": out, "can_scan": self.demo or bool(self.kubectl and os.path.exists(self.kubeconfig)) or bool(local)}

    def scan(self, node):
        nodes = self._nodes()
        names = [n["name"] for n in nodes]
        targets = names if node == "all" else [node]
        if not targets or any(t not in names for t in targets):
            raise ValueError("No such node.")
        local = self._local_node(nodes)
        started = []
        for t in targets:
            with self.lock:
                if t in self.running:
                    continue
                self.running.add(t)
            started.append(t)
            threading.Thread(target=self._run, args=(t, t == local), daemon=True, name="disk-scan-" + t).start()
        return started

    def _run(self, node, local):
        t0 = time.time()
        try:
            if self.demo:
                time.sleep(1.5)
                text = demo_scan(node)
            elif local:
                text = self._scan_here()
            else:
                text = self._scan_with_pod(node)
            data = parse(text)
            if not data["dirs"] and not data["disk"]:
                raise RuntimeError("the scan gave no answer")
            result = {"at": time.time(), "took": round(time.time() - t0, 1), "error": "", "disk": data["disk"],
                      "hotspots": hotspots(data["dirs"]), "files": data["files"],
                      "dirs": [d for d in data["dirs"] if d["depth"] <= 2]}
        except Exception as e:  # noqa: BLE001 -- shown on the page; the last good scan is kept
            with self.lock:
                prev = dict(self.results.get(node) or {})
            prev.update({"error": str(e)[:300], "error_at": time.time()})
            result = prev
        with self.lock:
            self.results[node] = result
            self.running.discard(node)
            self._save()

    def _scan_here(self):
        argv = ["sh", "-c", SCAN, "scan", "/"]
        if shutil.which("ionice"):
            argv = ["ionice", "-c3"] + argv          # (idle priority: the scan never slows the node's real work)
        if shutil.which("nice"):
            argv = ["nice", "-n", "15"] + argv
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=SCAN_DEADLINE)
        return p.stdout.decode("utf-8", "replace")

    def _kubectl(self, args, stdin=None, timeout=30):
        if not self.kubectl:
            raise RuntimeError("kubectl isn't available on the dashboard's server")
        p = subprocess.run(self.kubectl + ["--kubeconfig", self.kubeconfig] + args, input=stdin, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def _scan_with_pod(self, node):
        pod = "disk-%s-%s" % (re.sub(r"[^a-z0-9-]+", "-", node.lower()).strip("-")[:40] or "node", os.urandom(3).hex())
        wrapper = 'out=$(nice -n 15 sh -c "$0" scan /host 2>/dev/null); printf "%s\\n" "$out" | head -c 3900 > /dev/termination-log; printf "%s\\n" "$out"'
        manifest = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod, "namespace": NS, "labels": {"app": "nodeyard-disk-scan"}},
            "spec": {"nodeName": node, "restartPolicy": "Never", "activeDeadlineSeconds": SCAN_DEADLINE, "automountServiceAccountToken": False,
                     "terminationGracePeriodSeconds": 0, "tolerations": [{"operator": "Exists"}],
                     "containers": [{"name": "scan", "image": IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["sh", "-c", wrapper, SCAN],
                                     "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}, "limits": {"memory": "128Mi"}},
                                     "volumeMounts": [{"name": "host", "mountPath": "/host", "readOnly": True}]}],
                     "volumes": [{"name": "host", "hostPath": {"path": "/"}}]},
        }
        self._kubectl(["create", "namespace", NS])     # (already there is fine)
        rc, _out, err = self._kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest).encode())
        if rc:
            raise RuntimeError("couldn't start the scan on %s: %s" % (node, err.strip()[-200:]))
        try:
            deadline = time.time() + SCAN_DEADLINE + 30
            while time.time() < deadline:
                time.sleep(3)
                rc, out, _err = self._kubectl(["-n", NS, "get", "pod", pod, "-o", "json"])
                if rc:
                    continue
                st = json.loads(out).get("status") or {}
                phase = st.get("phase")
                if phase in ("Succeeded", "Failed"):
                    term = (((st.get("containerStatuses") or [{}])[0].get("state") or {}).get("terminated") or {})
                    text = term.get("message") or ""
                    if text.count("\n") < 3:     # nothing came back that way: try the logs (needs the kubelet reachable)
                        rc, logs, _ = self._kubectl(["-n", NS, "logs", pod], timeout=20)
                        text = logs if rc == 0 and logs.strip() else text
                    if not text.strip():
                        raise RuntimeError("the scan on %s %s without an answer" % (node, "failed" if phase == "Failed" else "finished"))
                    return text
                if phase == "Pending" and st.get("reason") in ("OutOfpods", "OutOfDisk") or (st.get("reason") or "").startswith("Evicted"):
                    raise RuntimeError("%s refused the scan (%s): its disk may be completely full" % (node, st.get("reason")))
            raise RuntimeError("the scan on %s didn't finish in time (is the node up?)" % node)
        finally:
            self._kubectl(["-n", NS, "delete", "pod", pod, "--ignore-not-found", "--wait=false"])


def demo_scan(node):
    """A believable disk for the demo."""
    h = sum(ord(c) for c in node)
    gib = 1024 * 1024
    images, models, journal, home = (18 + h % 9) * gib, (17 if h % 2 else 2) * gib, (1 + h % 3) * gib, (4 + h % 5) * gib
    total = 235 * gib
    used = images + models + journal + home + 9 * gib
    lines = ["DISK %d %d %d" % (total, used, total - used),
             "DU\t0\t%d\t/" % used,
             "DU\t1\t%d\t/var" % (images + models + journal + 2 * gib), "DU\t1\t%d\t/home" % home, "DU\t1\t%d\t/usr" % (5 * gib), "DU\t1\t%d\t/opt" % gib,
             "DU\t2\t%d\t/var/lib" % (images + models + gib), "DU\t2\t%d\t/var/log" % (journal + gib // 2), "DU\t2\t%d\t/home/barn" % home,
             "DU\t3\t%d\t/var/lib/rancher" % images, "DU\t3\t%d\t/var/lib/nodeyard" % models, "DU\t3\t%d\t/var/log/journal" % journal,
             "DU\t4\t%d\t/var/lib/rancher/k3s" % images, "DU\t4\t%d\t/var/lib/nodeyard/models" % (models - gib // 4),
             "DU\t5\t%d\t/var/lib/rancher/k3s/agent" % (images - gib), "DU\t6\t%d\t/var/lib/rancher/k3s/agent/containerd" % (images - 2 * gib)]
    if models > 5 * gib:
        lines.append("FILE\t%d\t/var/lib/nodeyard/models/Qwen3-Coder-30B-A3B-Q4_K_M.gguf" % models)
    lines.append("FILE\t%d\t/home/barn/Videos/recording.mkv" % (3 * gib))
    return "\n".join(lines)


def register(ctx, args):
    du = DiskUsage(ctx, args)
    ctx.diskusage = du

    def view(h, q):
        h._json(dict(du.view(), ok=True))

    def scan(h, body):
        try:
            started = du.scan(str(body.get("node") or ""))
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        h._json(dict(du.view(), ok=True, started=started))

    ctx.get_routes["/api/storage/usage"] = view
    ctx.post_routes["/api/storage/scan"] = scan
