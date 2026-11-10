"""Where Train AI's work runs: one training worker (share/nodeyard/jobs/trainworker.py) per machine.

On a cluster each worker is a pod pinned to its node (namespace nodeyard-train, image python:3.12-slim, the code from
a ConfigMap, PyTorch cached in /var/lib/nodeyard/train-worker on the node), reached over the pod network. Without a
cluster (or in the demo) the workers are local processes. Either way the dashboard only speaks HTTP to them, with a
shared token, and a worker installs PyTorch by itself the first time it starts. Idle workers stop after 30 minutes.
"""
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(os.path.dirname(HERE), "jobs")
WORKER = os.path.join(JOBS_DIR, "trainworker.py")
CODE_FILES = ("trainworker.py", "train_bpe_gpt_torch.py", "bpe_tokenizer.py", "sampling.py", "lr_governor.py", "training_data.py")
NS = "nodeyard-train"
IMAGE = "python:3.12-slim"
PORT = 9096
NODE_DIR = "/var/lib/nodeyard/train-worker"
IDLE_SECONDS = 30 * 60
CHUNK = 4 * 1024 * 1024

# A machine with a GPU can also train on its CPU at the same time: the CPU half runs as a second worker on the same
# machine, named "<machine> · CPU". It is a full training participant (its own copy of the model, averaged in like any
# other machine), but with the GPU hidden so PyTorch uses the CPU, and its own data folder so the two halves don't
# clash. Everything downstream just sees another machine name, so no orchestration code has to know about the split.
CPU_SUFFIX = " · CPU"


def split_variant(name):
    """('machine', 'cpu') for a '<machine> · CPU' co-worker, else (name, None)."""
    if name.endswith(CPU_SUFFIX):
        return name[:-len(CPU_SUFFIX)], "cpu"
    return name, None


def cpu_name(name):
    return name + CPU_SUFFIX


class PoolError(Exception):
    def __init__(self, message, code=503):
        super().__init__(message)
        self.code = code


def code_hash():
    h = hashlib.sha256()
    for name in CODE_FILES:
        with open(os.path.join(JOBS_DIR, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def file_version(path):
    st = os.stat(path)
    return "%d:%d" % (st.st_size, int(st.st_mtime))


class Remote:
    """HTTP client for one worker."""

    def __init__(self, name, url, token):
        self.name, self.url, self.token = name, url, token
        u = urllib.parse.urlparse(url)
        self.host, self.port = u.hostname, u.port or 80

    def _conn(self, timeout):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def call(self, method, path, body=None, timeout=30):
        conn = self._conn(timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read()
        except (OSError, http.client.HTTPException) as e:
            raise PoolError("Can't reach the trainer on %s: %s" % (self.name, e))
        finally:
            conn.close()
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            out = {"error": raw[:200].decode("utf-8", "replace")}
        if r.status >= 400:
            raise PoolError("%s: %s" % (self.name, out.get("error") or r.status), 409 if r.status == 409 else 502)
        return out

    def healthy(self):
        try:
            conn = self._conn(3)
            conn.request("GET", "/healthz")
            r = conn.getresponse()
            r.read()
            ok = r.status == 200
            conn.close()
            return ok
        except (OSError, http.client.HTTPException):
            return False

    def status(self):
        return self.call("GET", "/status", timeout=10)

    def ping(self, payload=0, timeout=20):
        """Round-trip time (seconds) to send `payload` bytes and get a reply. Empty = latency; a few MB = throughput."""
        data = b"\0" * payload
        conn = self._conn(timeout)
        t0 = time.time()
        try:
            conn.request("POST", "/ping", body=data,
                         headers={"Authorization": "Bearer " + self.token, "Content-Length": str(len(data))})
            r = conn.getresponse()
            r.read()
        except (OSError, http.client.HTTPException) as e:
            raise PoolError("Can't reach the trainer on %s: %s" % (self.name, e))
        finally:
            conn.close()
        if r.status != 200:
            raise PoolError("%s: ping failed (%d)" % (self.name, r.status))
        return time.time() - t0

    def ls(self, rel):
        return {f["name"]: f for f in self.call("GET", "/ls/" + urllib.parse.quote(rel))["files"]}

    def put_file(self, rel, local, version):
        """Upload in pieces; each piece is its own request, so a slow link never holds one connection for long."""
        total = os.path.getsize(local)
        offset = 0
        with open(local, "rb") as f:
            while True:
                data = f.read(CHUNK)
                q = urllib.parse.urlencode({"offset": offset, "total": total, "version": version})
                conn = self._conn(300)
                try:
                    conn.request("PUT", "/f/" + urllib.parse.quote(rel) + "?" + q, body=data,
                                 headers={"Authorization": "Bearer " + self.token, "Content-Length": str(len(data))})
                    r = conn.getresponse()
                    out = json.loads(r.read() or b"{}")
                except (OSError, http.client.HTTPException, ValueError) as e:
                    raise PoolError("Sending %s to %s failed: %s" % (os.path.basename(local), self.name, e))
                finally:
                    conn.close()
                if r.status != 200:
                    raise PoolError("Sending %s to %s failed: %s" % (os.path.basename(local), self.name, out.get("error")))
                offset += len(data)
                if out.get("done") or not data:
                    return

    def get_file(self, rel, local, version=None):
        """Download; with the worker's version ("size:mtime") the copy gets the same time, so it reads as unchanged."""
        conn = self._conn(300)
        tmp = local + ".part"
        try:
            conn.request("GET", "/f/" + urllib.parse.quote(rel), headers={"Authorization": "Bearer " + self.token})
            r = conn.getresponse()
            if r.status != 200:
                raise PoolError("Couldn't fetch %s from %s (%d)." % (rel, self.name, r.status), 502)
            with open(tmp, "wb") as f:
                while True:
                    b = r.read(1024 * 1024)
                    if not b:
                        break
                    f.write(b)
            os.replace(tmp, local)
            m = re.match(r"^(\d+):(\d+)$", version or "")
            if m and int(m.group(1)) == os.path.getsize(local):
                os.utime(local, (int(m.group(2)), int(m.group(2))))
        except (OSError, http.client.HTTPException) as e:
            raise PoolError("Couldn't fetch %s from %s: %s" % (rel, self.name, e), 502)
        finally:
            conn.close()
            if os.path.exists(tmp):
                os.remove(tmp)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pod_name(node, variant=None):
    base = re.sub(r"[^a-z0-9-]+", "-", node.lower()).strip("-")[:40] or "node"
    suffix = hashlib.sha256(node.encode()).hexdigest()[:5]
    return "nytrain-%s-%s%s" % (base, suffix, "-cpu" if variant == "cpu" else "")


class Pool:
    def __init__(self, root, demo=False, store=None, kubeconfig=None):
        self.root = os.path.join(root, "workers")
        os.makedirs(self.root, exist_ok=True)
        self.demo, self.store, self.kubeconfig = demo, store, kubeconfig
        self.token = self._token(os.path.join(root, "worker-token"))
        self.lock = threading.Lock()
        self.entries = {}   # name -> {"state", "detail", "log", "remote", "status", "used", "thread"}
        self.links = {}      # name -> {"rtt_ms", "mbit", "at"}: how fast the dashboard reaches each worker
        self.busy = set()    # machines a training job is using
        self.local_count = max(1, min(4, int(os.environ.get("NODEYARD_TRAIN_LOCAL_WORKERS", "1") or 1)))
        threading.Thread(target=self._reaper, daemon=True).start()

    @staticmethod
    def _token(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if len(tok) >= 32:
                return tok
        except OSError:
            pass
        tok = secrets.token_hex(24)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok

    # -- which machines exist
    def kubectl(self):
        if self.demo or not self.kubeconfig:
            return None
        sys.path.insert(0, HERE)
        try:
            import nodeexec
        finally:
            sys.path.remove(HERE)
        return nodeexec.kubectl_argv()

    def cluster_nodes(self):
        if self.demo or self.store is None or not self.kubectl():
            return []
        try:
            return ((self.store.snapshot() or {}).get("state") or {}).get("nodes") or []
        except Exception:   # noqa: BLE001 - no snapshot yet
            return []

    @property
    def mode(self):
        return "cluster" if self.cluster_nodes() else "local"

    def local_names(self):
        base = re.sub(r"[^A-Za-z0-9-]+", "-", platform.node().split(".")[0]).strip("-") or "this-machine"
        return [base] + ["%s-%d" % (base, i) for i in range(2, self.local_count + 1)]

    # -- bringing a worker up
    def entry(self, name):
        with self.lock:
            return self.entries.setdefault(name, {"state": "off", "detail": "", "log": [], "deplog": [], "remote": None, "status": None,
                                                  "used": 0.0, "thread": None})

    def ensure(self, name, node=None):
        """Start this machine's worker in the background if it isn't up; returns its entry."""
        e = self.entry(name)
        e["used"] = time.time()
        with self.lock:
            if e["thread"] and e["thread"].is_alive():
                return e
            if e["state"] == "ready" and e["remote"] and e["remote"].healthy():
                return e
            e["thread"] = threading.Thread(target=self._bring_up, args=(name, node), daemon=True)
            e["thread"].start()
        return e

    def ready(self, name, node=None, wait=0.0, say=None):
        """The Remote for a machine whose worker has PyTorch ready; waits up to `wait` seconds (saying progress)."""
        deadline = time.time() + wait
        seen = 0
        while True:
            e = self.ensure(name, node)
            if say:
                for line in e["log"][seen:]:
                    say(line)
                seen = len(e["log"])
            if e["state"] == "ready":
                return e["remote"]
            if e["state"] == "failed":
                raise PoolError("%s can't train: %s" % (name, e["detail"]), 503)
            if time.time() >= deadline:
                raise PoolError("The trainer on %s is %s%s. Try again in a minute." % (name, e["state"], (": " + e["detail"]) if e["detail"] else ""), 503)
            time.sleep(2)

    def _note(self, e, state, detail=""):
        e["state"], e["detail"] = state, detail
        line = state + (": " + detail if detail else "")
        if not e["log"] or e["log"][-1] != line:
            e["log"].append(line)
            del e["log"][:-60]

    def _bring_up(self, name, node):
        e = self.entry(name)
        try:
            remote = self._start_local(name, e) if self.mode == "local" else self._start_pod(name, node, e)
            e["remote"] = remote
            while True:
                st = remote.status()
                e["status"] = st
                dep = st.get("deps") or {}
                e["deplog"] = (dep.get("log") or [])[-12:]
                if dep.get("state") == "ready":
                    probe = st.get("probe") or {}
                    self._note(e, "ready", "PyTorch %s on %s" % (probe.get("torch"), st.get("device")))
                    return
                if dep.get("state") == "failed":
                    self._note(e, "failed", dep.get("error") or "PyTorch couldn't be installed")
                    return
                last = (dep.get("log") or [""])[-1]
                self._note(e, "installing PyTorch" if dep.get("state") == "installing" else "checking PyTorch", last[:160])
                time.sleep(3)
        except PoolError as ex:
            self._note(e, "failed", str(ex))
        except Exception as ex:   # noqa: BLE001 - shown on the page
            self._note(e, "failed", "%s: %s" % (type(ex).__name__, ex))

    def _start_local(self, name, e):
        root = os.path.join(self.root, name)
        os.makedirs(root, exist_ok=True)
        info_path = os.path.join(root, "worker.json")
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            r = Remote(name, "http://127.0.0.1:%d" % info["port"], self.token)
            if r.healthy():
                return r
        except (OSError, ValueError, KeyError):
            pass
        self._note(e, "starting")
        port = _free_port()
        token_file = os.path.join(os.path.dirname(self.root), "worker-token")
        data_dir = os.path.join(os.path.dirname(self.root), "data")
        env = dict(os.environ)
        if split_variant(name)[1] == "cpu":   # the CPU half of a GPU machine: keep the GPU hidden so it uses the CPU
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["NVIDIA_VISIBLE_DEVICES"] = "none"
        log = open(os.path.join(root, "worker.log"), "ab")
        try:
            proc = subprocess.Popen([sys.executable, WORKER, "--root", root, "--code", JOBS_DIR, "--port", str(port),
                                     "--token-file", token_file, "--shared-data", data_dir], env=env,
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            log.close()
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": proc.pid}, f)
        r = Remote(name, "http://127.0.0.1:%d" % port, self.token)
        for _ in range(60):
            if r.healthy():
                return r
            if proc.poll() is not None:
                raise PoolError("The worker stopped as it started (see %s)." % os.path.join(root, "worker.log"))
            time.sleep(0.25)
        raise PoolError("The worker didn't answer.")

    # -- pods
    def _kubectl(self, args, stdin=None, timeout=60):
        argv = self.kubectl()
        if not argv:
            raise PoolError("kubectl isn't available on the dashboard's server.")
        p = subprocess.run(argv + ["--kubeconfig", self.kubeconfig] + args, input=stdin, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def base_manifest(self):
        files = {}
        for n in CODE_FILES:
            with open(os.path.join(JOBS_DIR, n), "r", encoding="utf-8") as f:
                files[n] = f.read()
        return [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS, "labels": {"app.kubernetes.io/managed-by": "nodeyard"}}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "nodeyard-train-token", "namespace": NS}, "stringData": {"token": self.token}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "nodeyard-train-code", "namespace": NS}, "data": files},
        ]

    def pod_manifest(self, node, variant=None):
        """One trainer pod. variant="cpu" is the CPU half of a GPU machine: the GPU is hidden so PyTorch falls back to
        the CPU, it uses its own data folder so it can't clash with the GPU pod, and the two share the node's memory."""
        gpu = (node.get("labels") or {}).get("nodeyard.io/gpu") == "nvidia" and variant != "cpu"
        mem = int(node.get("mem_total") or 0)
        # Two pods on one node (GPU + its CPU half) must share the memory headroom, not each take 80%.
        both = variant == "cpu" or ((node.get("labels") or {}).get("nodeyard.io/gpu") == "nvidia" and node.get("_with_cpu"))
        frac = 0.4 if both else 0.8
        data_dir = NODE_DIR + ("-cpu" if variant == "cpu" else "")
        container = {
            "name": "worker", "image": IMAGE,
            "command": ["python", "-u", "/code/trainworker.py", "--root", "/data", "--code", "/code", "--host", "0.0.0.0",
                        "--port", str(PORT), "--token-file", "/secret/token"],
            "ports": [{"containerPort": PORT}],
            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
            "volumeMounts": [{"name": "code", "mountPath": "/code", "readOnly": True},
                             {"name": "secret", "mountPath": "/secret", "readOnly": True},
                             {"name": "data", "mountPath": "/data"}],
        }
        if mem > 0:   # leave the node room for itself: the trainer is stopped before the machine runs out
            container["resources"]["limits"] = {"memory": str(max(512 * 1024 ** 2, int(mem * frac)))}
        spec = {
            "nodeName": node["name"], "restartPolicy": "Never", "terminationGracePeriodSeconds": 45,
            "tolerations": [{"operator": "Exists"}],
            "containers": [container],
            "volumes": [{"name": "code", "configMap": {"name": "nodeyard-train-code"}},
                        {"name": "secret", "secret": {"secretName": "nodeyard-train-token"}},
                        {"name": "data", "hostPath": {"path": data_dir, "type": "DirectoryOrCreate"}}],
        }
        if gpu:
            spec["runtimeClassName"] = "nvidia"
            container["env"] += [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
                                 {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"}]
        elif variant == "cpu":   # a GPU machine's CPU half: make sure no GPU is visible, so PyTorch uses the CPU
            container["env"] += [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "none"},
                                 {"name": "CUDA_VISIBLE_DEVICES", "value": ""}]
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": _pod_name(node["name"], variant), "namespace": NS,
                             "labels": {"app": "nodeyard-train"}, "annotations": {"nodeyard/code": code_hash()}},
                "spec": spec}

    def _start_pod(self, name, node, e):
        base, variant = split_variant(name)
        node = node or next((n for n in self.cluster_nodes() if n.get("name") == base), None)
        if not node:
            raise PoolError("%s isn't in the cluster." % base)
        if variant == "cpu":   # tell the manifest the GPU pod is alongside, so both share the memory headroom
            node = dict(node, _with_cpu=True)
        pod = _pod_name(base, variant)
        code, out, _ = self._kubectl(["-n", NS, "get", "pod", pod, "-o", "json"])
        current = json.loads(out) if code == 0 else None
        if current:
            fresh = (current["metadata"].get("annotations") or {}).get("nodeyard/code") == code_hash()
            if not fresh or current["status"].get("phase") in ("Succeeded", "Failed"):
                self._note(e, "starting", "replacing the old trainer pod")
                self._kubectl(["-n", NS, "delete", "pod", pod, "--wait=true", "--timeout=60s"], timeout=90)
                current = None
        if not current:
            self._note(e, "starting", "creating the trainer pod on %s" % name)
            body = json.dumps({"apiVersion": "v1", "kind": "List", "items": self.base_manifest() + [self.pod_manifest(node, variant)]}).encode()
            code, _, err = self._kubectl(["apply", "--server-side", "--force-conflicts", "--field-manager=nodeyard-train", "-f", "-"], stdin=body, timeout=90)
            if code != 0:
                raise PoolError("Couldn't create the trainer pod: %s" % err.strip()[-300:])
        deadline = time.time() + 900
        while time.time() < deadline:
            code, out, err = self._kubectl(["-n", NS, "get", "pod", pod, "-o", "json"])
            if code != 0:
                raise PoolError("The trainer pod vanished: %s" % err.strip()[-200:])
            p = json.loads(out)
            st = p.get("status") or {}
            waiting = next((c.get("state", {}).get("waiting") for c in st.get("containerStatuses") or [] if c.get("state", {}).get("waiting")), None)
            if waiting and waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "RunContainerError"):
                raise PoolError("%s: %s" % (waiting.get("reason"), (waiting.get("message") or "")[:200]))
            if st.get("phase") in ("Failed", "Succeeded"):
                raise PoolError("The trainer pod stopped (%s)." % (st.get("reason") or st.get("phase")))
            if st.get("phase") == "Running" and st.get("podIP"):
                r = Remote(name, "http://%s:%d" % (st["podIP"], PORT), self.token)
                if r.healthy():
                    return r
                self._note(e, "starting", "waiting for the worker to answer")
            else:
                self._note(e, "starting", (waiting or {}).get("reason") or st.get("phase") or "scheduling")
            time.sleep(3)
        raise PoolError("The trainer pod didn't start within 15 minutes.")

    # -- how good the link to each worker is (latency + throughput), for deciding whether sharing is worth it
    def measure_link(self, name, remote=None, bandwidth=True, cache_seconds=0):
        """Measure (and cache) the link to a worker. rtt_ms is the round-trip latency; mbit is the measured upload
        throughput (None if not measured). Returns the cached value when it's younger than cache_seconds."""
        cached = self.links.get(name)
        if cached and cache_seconds and time.time() - cached.get("at", 0) < cache_seconds and (cached.get("mbit") or not bandwidth):
            return cached
        e = self.entry(name)
        remote = remote or e.get("remote")
        if not remote:
            return cached
        try:
            lat = min(remote.ping(0, timeout=5) for _ in range(3))   # smallest of three: least disturbed by a hiccup
        except PoolError:
            return cached
        link = {"rtt_ms": round(lat * 1000, 1), "mbit": (cached or {}).get("mbit"), "at": time.time()}
        if bandwidth:
            try:
                size = 4 * 1024 * 1024
                dt = max(1e-4, remote.ping(size, timeout=30) - lat)   # take the latency off the transfer time
                link["mbit"] = round(size * 8 / dt / 1e6, 1)
            except PoolError:
                pass
        self.links[name] = link
        return link

    # -- stopping idle workers
    def stop(self, name):
        e = self.entry(name)
        if self.mode == "local":
            try:
                with open(os.path.join(self.root, name, "worker.json"), "r", encoding="utf-8") as f:
                    pid = json.load(f)["pid"]
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, KeyError):
                pass
        else:
            base, variant = split_variant(name)
            self._kubectl(["-n", NS, "delete", "pod", _pod_name(base, variant), "--wait=false"])
        e.update(state="off", detail="stopped after %d idle minutes" % (IDLE_SECONDS // 60), remote=None, status=None)

    def _reaper(self):
        while True:
            time.sleep(60)
            for name, e in list(self.entries.items()):
                if e["state"] in ("ready", "failed") and name not in self.busy and time.time() - e["used"] > IDLE_SECONDS:
                    try:
                        self.stop(name)
                    except Exception:   # noqa: BLE001 - try again next minute
                        pass
