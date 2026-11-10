#!/usr/bin/env python3
"""nodeyard training worker: runs the trainer on one machine for the dashboard's Train AI page.

Standard library only, so it starts on a bare Python. On start it makes sure PyTorch and numpy are installed:
it installs them once into <root>/pydeps/<python>-<cpu type>-<cpu|cuda|mac> (pip --target, nothing system-wide)
and reuses that folder afterwards. NODEYARD_TRAIN_PYTHON picks a Python that already has them.

The dashboard talks to it over HTTP with a shared token (Authorization: Bearer <token>):
  GET  /healthz                        no token; "ok"
  GET  /status                         machine, PyTorch install progress, what the GPU can do, the current job
  GET  /ls/data | /ls/models/<model>   files with their size and version
  PUT  /f/<data|models>/...?offset=&total=&version=   upload a file in pieces (resumable)
  GET  /f/<data|models>/...            download a file
  POST /pull {url, path, dest, version}   fetch a file from another worker (machines swap weights directly)
  POST /delete {path}
  POST /train {model, data, options}   start training (one job at a time); GET /log?id=&offset= reads its output
  POST /stop                           ask the trainer to finish its step, save and exit
  POST /run {cmd, model, options|parts}   generate, explain or average, and wait for the output

Run as a pod on a cluster node (the dashboard creates it) or as a local process when there is no cluster.
"""

import argparse
import hmac
import http.client
import http.server
import json
import os
import platform
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}$")
TRAINER = "train_bpe_gpt_torch.py"
FORBIDDEN_OPTIONS = ("--app-data", "--data-file", "--result-file", "--out", "--part")
CHUNK = 1024 * 1024
LOG_READ_MAX = 256 * 1024

PROBE = r"""
import json
out = {"torch": None}
try:
    import numpy, torch
    out["torch"] = torch.__version__
    out["threads"] = torch.get_num_threads()
    out["cuda"] = False
    if torch.cuda.is_available():
        p = torch.cuda.get_device_properties(0)
        out["gpu"] = {"name": p.name, "vram": int(p.total_memory), "capability": "%d.%d" % (p.major, p.minor)}
        try:   # a build without kernels for this GPU says it's available, then fails on the first sum
            float((torch.ones(4, device="cuda") * 2).sum())
            out["cuda"] = True
        except Exception as e:
            out["cuda_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    out["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
except Exception as e:
    out["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
print(json.dumps(out))
"""


def read_memory():
    """(total, available) bytes."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f if ":" in line and line.split()[1].isdigit()}
        return info.get("MemTotal", 0), info.get("MemAvailable", info.get("MemFree", 0))
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip())
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            free = sum(int(m) for _, m in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", vm))
            return total, free * page
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    return 0, 0


def flavour():
    """Which PyTorch build this machine needs: cuda (an NVIDIA GPU is visible), mac, or cpu."""
    if sys.platform == "darwin":
        return "mac"
    if os.path.exists("/dev/nvidia0") or os.path.exists("/dev/nvidiactl") or shutil.which("nvidia-smi"):
        return "cuda"
    return "cpu"


def pip_index(kind):
    """Where pip finds the right build. CUDA 12.6 builds still include older GPUs such as the GTX 10 series."""
    if kind == "cpu":
        return ["--index-url", "https://download.pytorch.org/whl/cpu", "--extra-index-url", "https://pypi.org/simple"]
    if kind == "cuda":
        return ["--index-url", "https://download.pytorch.org/whl/cu126", "--extra-index-url", "https://pypi.org/simple"]
    return []


class Worker:
    def __init__(self, root, code, shared_data=None):
        self.root = os.path.abspath(root)
        self.code = os.path.abspath(code)
        self.data_dir = os.path.abspath(shared_data) if shared_data else os.path.join(self.root, "data")
        self.models_dir = os.path.join(self.root, "models")
        self.logs_dir = os.path.join(self.root, "logs")
        for d in (self.data_dir, self.models_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.Lock()
        self.python = sys.executable
        self.pythonpath = ""
        self.deps = {"state": "checking", "log": [], "started": time.time()}
        self.probe = {}
        self.job = {}
        self.proc = None

    # -- PyTorch
    def env(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        env["PYTHONPATH"] = os.pathsep.join(x for x in (self.pythonpath, self.code) if x)
        return env

    def say(self, line):
        self.deps["log"].append(line)
        del self.deps["log"][:-200]

    def run_probe(self):
        try:
            r = subprocess.run([self.python, "-c", PROBE], capture_output=True, text=True, timeout=180, env=self.env())
            self.probe = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            self.probe = {"torch": None, "error": str(e)}
        return bool(self.probe.get("torch"))

    def install(self):
        """Find or install PyTorch, then probe what it can use. Runs in the background from start-up."""
        try:
            own = os.environ.get("NODEYARD_TRAIN_PYTHON")
            if own:
                self.python = own
                if self.run_probe():
                    self.say("Using %s (PyTorch %s)." % (own, self.probe["torch"]))
                    self.deps["state"] = "ready"
                    return
                self.say("%s has no PyTorch; installing one instead." % own)
                self.python = sys.executable
            if self.run_probe():
                self.say("PyTorch %s is already installed." % self.probe["torch"])
                self.deps["state"] = "ready"
                return
            kind = flavour()
            arch = platform.machine().lower()
            if arch in ("armv7l", "armv6l", "arm", "i386", "i686"):
                self.deps.update(state="failed", error="PyTorch has no build for %s (a 32-bit CPU), so this machine can't train." % arch)
                self.say(self.deps["error"])
                return
            tag = "py%d%d-%s-%s" % (sys.version_info[0], sys.version_info[1], arch, kind)
            target = os.path.join(self.root, "pydeps", tag)
            self.pythonpath = target
            if os.path.isfile(os.path.join(target, ".complete")) and self.run_probe():
                self.say("PyTorch %s is installed in %s." % (self.probe["torch"], target))
                self.deps["state"] = "ready"
                return
            self.deps["state"] = "installing"
            size = {"cuda": "about 3 GB (the GPU version)", "cpu": "about 200 MB (the CPU version)", "mac": "about 300 MB"}[kind]
            self.say("Installing PyTorch and numpy: %s, once. It's kept in %s." % (size, target))
            tmp = target + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            cmd = [sys.executable, "-m", "pip", "install", "--target", tmp, "--no-cache-dir", "--disable-pip-version-check",
                   "--progress-bar", "off", "numpy", "torch"] + pip_index(kind)
            self.say("$ " + " ".join(cmd))
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=dict(os.environ, PIP_ROOT_USER_ACTION="ignore"))
            for line in p.stdout:
                line = line.rstrip()
                if line:
                    self.say(line[:300])
            code = p.wait()
            if code != 0:
                hint = " pip isn't installed for this Python: on Debian/Ubuntu run sudo apt install python3-pip." if any("No module named pip" in l for l in self.deps["log"]) else ""
                self.deps.update(state="failed", error="pip failed (exit %d).%s" % (code, hint))
                self.say(self.deps["error"])
                shutil.rmtree(tmp, ignore_errors=True)
                return
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
            open(os.path.join(target, ".complete"), "w").close()
            if not self.run_probe():
                self.deps.update(state="failed", error="PyTorch installed but won't load: %s" % self.probe.get("error"))
                self.say(self.deps["error"])
                return
            self.say("PyTorch %s is ready." % self.probe["torch"])
            self.deps["state"] = "ready"
        except Exception as e:   # noqa: BLE001 - reported to the page
            self.deps.update(state="failed", error="Setup failed: %s" % e)
            self.say(self.deps["error"])
        finally:
            if self.deps["state"] == "ready" and self.probe.get("cuda_error"):
                self.say("The GPU is there but this PyTorch can't use it (%s): training uses the CPU." % self.probe["cuda_error"])

    def status(self):
        total, free = read_memory()
        job = {k: v for k, v in self.job.items() if k != "proc"}
        job["running"] = self.running()
        dev = "cuda" if self.probe.get("cuda") else "mps" if self.probe.get("mps") else "cpu"
        return {"host": platform.node(), "node": os.environ.get("NODE_NAME", ""), "cores": os.cpu_count() or 1,
                "arch": platform.machine(), "os": platform.system(), "ram_total": total, "ram_free": free,
                "deps": {k: (v[-40:] if k == "log" else v) for k, v in self.deps.items()}, "probe": self.probe, "device": dev,
                "job": job}

    # -- files
    def resolve(self, rel, must_exist=False):
        parts = [p for p in rel.strip("/").split("/") if p]
        if not parts or parts[0] not in ("data", "models") or len(parts) > 3 or not all(SAFE.match(p) and ".." not in p for p in parts[1:]):
            raise ValueError("bad path")
        base = self.data_dir if parts[0] == "data" else self.models_dir
        path = os.path.join(base, *parts[1:])
        if must_exist and not os.path.exists(path):
            raise FileNotFoundError(rel)
        return path

    @staticmethod
    def version_of(path):
        """The version the sender gave the file, while the file is unchanged since; else its size and time."""
        st = os.stat(path)
        try:
            with open(os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".version"), "r", encoding="utf-8") as f:
                version, size, mtime = f.read().strip().rsplit("|", 2)
            if int(size) == st.st_size and int(mtime) == st.st_mtime_ns:
                return version
        except (OSError, ValueError):
            pass
        return "%d:%d" % (st.st_size, int(st.st_mtime))

    @staticmethod
    def set_version(path, version):
        if version:
            st = os.stat(path)
            with open(os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".version"), "w", encoding="utf-8") as f:
                f.write("%s|%d|%d" % (str(version)[:100].replace("|", ""), st.st_size, st.st_mtime_ns))

    def ls(self, rel):
        path = self.resolve(rel)
        out = []
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if name.startswith(".") or name.endswith(".part") or not os.path.isfile(full):
                    continue
                out.append({"name": name, "size": os.path.getsize(full), "version": self.version_of(full)})
        return out

    # -- jobs
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def check_options(self, options):
        if not isinstance(options, list) or not all(isinstance(o, str) and len(o) < 10000 for o in options):
            raise ValueError("options must be a list of strings")
        for o in options:
            if o.split("=")[0] in FORBIDDEN_OPTIONS:
                raise ValueError("%s is set by the worker" % o)
        return options

    def train(self, body):
        with self.lock:
            if self.running():
                raise RuntimeError("busy")
            if self.deps.get("state") != "ready":
                raise RuntimeError("PyTorch isn't ready on this machine yet")
            mdir = self.resolve("models/" + str(body.get("model")))
            args = [self.python, "-u", os.path.join(self.code, TRAINER), "train", "--app-data", mdir]
            for name in body.get("data") or []:
                args += ["--data-file", self.resolve("data/" + str(name), must_exist=True)]
            args += self.check_options(body.get("options") or [])
            os.makedirs(mdir, exist_ok=True)
            jid = uuid.uuid4().hex[:12]
            log_path = os.path.join(self.logs_dir, "job-%s.log" % jid)
            with open(log_path, "ab") as log:
                self.proc = subprocess.Popen(args, cwd=self.code, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             env=self.env(), start_new_session=True)
            self.job = {"id": jid, "model": body.get("model"), "log": log_path, "started": time.time(), "code": None, "ended": None}
            threading.Thread(target=self._wait, args=(self.proc, jid), daemon=True).start()
            logs = sorted((os.path.join(self.logs_dir, x) for x in os.listdir(self.logs_dir)), key=os.path.getmtime)
            for old in logs[:-6]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            return {"id": jid}

    def _wait(self, proc, jid):
        code = proc.wait()
        if self.job.get("id") == jid:
            self.job["code"], self.job["ended"] = code, time.time()

    def read_log(self, jid, offset):
        if not self.job or self.job.get("id") != jid:
            return {"id": self.job.get("id"), "text": "", "next": offset, "running": False, "code": None, "gone": True}
        running = self.running()
        with open(self.job["log"], "rb") as f:
            f.seek(offset)
            data = f.read(LOG_READ_MAX)
        cut = data.rfind(b"\n") + 1
        if not running and len(data) < LOG_READ_MAX:
            cut = len(data)
        return {"id": jid, "text": data[:cut].decode("utf-8", "replace"), "next": offset + cut, "running": running,
                "code": None if running else self.job.get("code")}

    def stop(self):
        if self.running():
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except OSError:
                self.proc.send_signal(signal.SIGINT)
            return {"stopping": True}
        return {"stopping": False}

    def run(self, body):
        if self.deps.get("state") != "ready":
            raise RuntimeError("PyTorch isn't ready on this machine yet")
        cmd = body.get("cmd")
        args = [self.python, "-u", os.path.join(self.code, TRAINER)]
        mdir = self.resolve("models/" + str(body["model"])) if body.get("model") else None
        if cmd in ("generate", "explain"):
            args.append(cmd)
            if mdir:
                args += ["--app-data", mdir]
            args += self.check_options(body.get("options") or [])
        elif cmd == "average" and mdir:
            args += ["average", "--out", mdir]
            for name, weight in body.get("parts") or []:
                if not SAFE.match(str(name)) or ".." in str(name):
                    raise ValueError("bad part")
                args += ["--part", "%s:%r" % (os.path.join(mdir, name), float(weight))]
        else:
            raise ValueError("unknown command")
        try:
            r = subprocess.run(args, cwd=self.code, capture_output=True, text=True, timeout=int(body.get("timeout") or 300), env=self.env())
        except subprocess.TimeoutExpired:
            return {"code": 124, "out": "", "err": "timed out"}
        return {"code": r.returncode, "out": r.stdout[-2_000_000:], "err": r.stderr[-4000:]}

    def pull(self, body, token):
        """Copy a file straight from another worker (so weights don't travel through the dashboard)."""
        url = urllib.parse.urlparse(str(body.get("url") or ""))
        if url.scheme != "http" or not url.hostname:
            raise ValueError("bad url")
        dest = self.resolve(str(body.get("dest") or ""))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=120)
        conn.request("GET", "/f/" + urllib.parse.quote(str(body.get("path") or "")), headers={"Authorization": "Bearer " + token})
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError("the other machine answered %d" % r.status)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while True:
                b = r.read(CHUNK)
                if not b:
                    break
                f.write(b)
        conn.close()
        os.replace(tmp, dest)
        self.set_version(dest, body.get("version"))
        return {"size": os.path.getsize(dest)}


def make_handler(w, token):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "nodeyard-trainworker"

        def log_message(self, *a):
            pass

        def send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                return True
            self.send(401, {"error": "token"})
            return False

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("too big")
            return json.loads(self.rfile.read(n) or b"{}")

        def guard(self, fn):
            try:
                fn()
            except (ValueError, KeyError, TypeError) as e:
                self.send(400, {"error": str(e)})
            except FileNotFoundError as e:
                self.send(404, {"error": "not found: %s" % e})
            except RuntimeError as e:
                self.send(409, {"error": str(e)})
            except OSError as e:
                self.send(500, {"error": str(e)})

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = urllib.parse.unquote(u.path)
            if path == "/healthz":
                return self.send(200, {"ok": True})
            if not self.authed():
                return
            if path == "/status":
                return self.send(200, w.status())
            if path.startswith("/ls/"):
                return self.guard(lambda: self.send(200, {"files": w.ls(path[4:])}))
            if path == "/log":
                return self.guard(lambda: self.send(200, w.read_log((q.get("id") or [""])[0], int((q.get("offset") or ["0"])[0]))))
            if path.startswith("/f/"):
                return self.guard(lambda: self.download(w.resolve(path[3:], must_exist=True)))
            self.send(404, {"error": "no such page"})

        def download(self, full):
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Version", w.version_of(full))
            self.end_headers()
            with open(full, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)

        def do_PUT(self):
            if not self.authed():
                return
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            self.guard(lambda: self.upload(urllib.parse.unquote(u.path), q))

        def upload(self, path, q):
            if not path.startswith("/f/"):
                raise ValueError("bad path")
            full = w.resolve(path[3:])
            offset, total = int((q.get("offset") or ["0"])[0]), int((q.get("total") or ["-1"])[0])
            n = int(self.headers.get("Content-Length") or 0)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            part = full + ".part"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if offset == 0:
                have = 0
                open(part, "wb").close()
            if offset != have:
                self.rfile.read(n)
                return self.send(409, {"error": "offset", "have": have})
            with open(part, "r+b") as f:
                f.seek(offset)
                left = n
                while left:
                    b = self.rfile.read(min(CHUNK, left))
                    if not b:
                        raise OSError("upload cut short")
                    f.write(b)
                    left -= len(b)
            have = offset + n
            done = total >= 0 and have >= total
            if done:
                os.replace(part, full)
                w.set_version(full, (q.get("version") or [""])[0])
            self.send(200, {"have": have, "done": done})

        def do_POST(self):
            if not self.authed():
                return
            path = urllib.parse.urlparse(self.path).path

            def go():
                b = self.body()
                if path == "/train":
                    self.send(200, w.train(b))
                elif path == "/stop":
                    self.send(200, w.stop())
                elif path == "/run":
                    self.send(200, w.run(b))
                elif path == "/pull":
                    self.send(200, w.pull(b, token))
                elif path == "/delete":
                    full = w.resolve(str(b.get("path") or ""), must_exist=True)
                    if os.path.isdir(full):
                        if w.running() and w.job.get("model") == os.path.basename(full):
                            raise RuntimeError("that model is training")
                        shutil.rmtree(full)
                    else:
                        os.remove(full)
                    self.send(200, {"deleted": True})
                else:
                    self.send(404, {"error": "no such page"})
            self.guard(go)

    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):   # a caller hanging up isn't news
            super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="folder for PyTorch, data, models and logs")
    ap.add_argument("--code", default=os.path.dirname(os.path.abspath(__file__)), help="folder holding the trainer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9096)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--shared-data", default=None, help="read training files from this folder instead of <root>/data")
    a = ap.parse_args()
    with open(a.token_file, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if len(token) < 16:
        sys.exit("The token is too short.")
    w = Worker(a.root, a.code, a.shared_data)
    threading.Thread(target=w.install, daemon=True).start()
    srv = Server((a.host, a.port), make_handler(w, token))

    def bye(*_):
        if w.running():
            w.stop()
            try:
                w.proc.wait(30)
            except subprocess.TimeoutExpired:
                pass
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    print("trainworker listening on %s:%d" % (a.host, srv.server_address[1]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
