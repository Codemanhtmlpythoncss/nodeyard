"""The dashboard's AI features: chat with running models, search for models, run them.

Everything here sits behind the sign-in. Chat goes straight from this server to the model's own API
(the server API key never reaches the browser). Model search asks Hugging Face. Running or removing a
model starts one of a short list of nodeyard commands; nothing else can be started from the page.
"""
import base64
import concurrent.futures
import http.client
import ipaddress
import json
import os
import re
import secrets
import select
import signal
import shlex
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse

UA = "nodeyard-dashboard"
HF_HOST = "huggingface.co"
SPLIT_NS, SPLIT_SVC, SPLIT_PORT = "ai-split", "llama", 8080
OLLAMA_NS, OLLAMA_PORT = "ai-inference", 11434
SPLIT_KEY_NAME = "ai-split-api-key"
MAX_CHAT_CHARS = 2000000  # text for a whole conversation; inline image bytes have a separate limit
MAX_CHAT_IMAGE_BYTES = 8 * 1024 * 1024
MAX_CHAT_IMAGES = 8
MAX_CHAT_BODY_BYTES = 20 * 1024 * 1024

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()+=/-]{0,250}\.gguf$")
CHECK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CLUSTER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
LOCAL_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,250}\.gguf$")  # a file on a node (no folders)
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
PART_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")
QUANT_RE = re.compile(r"(?:^|[-_.])((?:UD-)?(?:IQ\d(?:_[A-Z0-9]+)*|Q\d(?:_[A-Z0-9]+)+|Q\d_\d|BF16|F16|F32|MXFP4))(?=[-_.]|$)", re.I)
CHAT_IMAGE_RE = re.compile(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]*={0,2})$")
CHAT_TOOL_NAMES = frozenset(("browser_search", "browser_open", "computer_read_screen", "computer_click",
                            "computer_set_text", "computer_press_key", "computer_open_app"))
CHAT_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
NOT_REACHED_RE = re.compile(r"^\s+([a-z0-9][-a-z0-9.]*): not reached\b")


def clean_chat_messages(messages):
    """Validate text and inline image parts before forwarding chat to a model."""
    clean, text_chars, image_bytes, image_count = [], 0, 0, 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
            raise ValueError("Each message needs a role and text.")
        role, content = message["role"], message.get("content")
        if role == "tool":
            tool_id, name = message.get("tool_call_id"), message.get("name")
            if (not isinstance(content, str) or not isinstance(tool_id, str) or not CHAT_TOOL_ID_RE.fullmatch(tool_id)
                    or not isinstance(name, str) or name not in CHAT_TOOL_NAMES):
                raise ValueError("That tool result isn't valid.")
            text_chars += len(content)
            clean.append({"role": "tool", "tool_call_id": tool_id, "name": name, "content": content})
            continue
        calls = message.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 8 or content not in (None, ""):
                raise ValueError("That model tool request isn't valid.")
            normalized = []
            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("That model tool request isn't valid.")
                fn = call.get("function")
                cid, name, args = call.get("id"), fn.get("name") if isinstance(fn, dict) else None, fn.get("arguments") if isinstance(fn, dict) else None
                if (call.get("type") != "function" or not isinstance(cid, str) or not CHAT_TOOL_ID_RE.fullmatch(cid)
                        or not isinstance(name, str) or name not in CHAT_TOOL_NAMES or not isinstance(args, str) or len(args) > 20000):
                    raise ValueError("That model tool request isn't valid.")
                try:
                    parsed = json.loads(args)
                except ValueError:
                    raise ValueError("That model tool request has invalid arguments.")
                if not isinstance(parsed, dict):
                    raise ValueError("That model tool request has invalid arguments.")
                normalized.append({"id": cid, "type": "function", "function": {"name": name, "arguments": args}})
            clean.append({"role": "assistant", "content": content, "tool_calls": normalized})
            continue
        if isinstance(content, str):
            text_chars += len(content)
            clean.append({"role": role, "content": content})
            continue
        if role != "user" or not isinstance(content, list) or not 1 <= len(content) <= 32:
            raise ValueError("Each message needs a role and text.")
        parts = []
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("That image or text attachment isn't valid.")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text_chars += len(part["text"])
                parts.append({"type": "text", "text": part["text"]})
                continue
            image = part.get("image_url")
            if part.get("type") != "image_url" or not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise ValueError("Only text and PNG, JPEG or WebP images can be attached.")
            match = CHAT_IMAGE_RE.fullmatch(image["url"])
            if not match:
                raise ValueError("Images must be attached directly from this device.")
            encoded = match.group(2)
            if len(encoded) > ((MAX_CHAT_IMAGE_BYTES + 2) // 3) * 4:
                raise ValueError("Images are too large. Choose smaller images or fewer files.")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error):
                raise ValueError("That image attachment isn't valid.")
            if not decoded or len(decoded) > MAX_CHAT_IMAGE_BYTES:
                raise ValueError("Images are too large. Choose smaller images or fewer files.")
            image_count += 1
            image_bytes += len(decoded)
            if image_count > MAX_CHAT_IMAGES or image_bytes > MAX_CHAT_IMAGE_BYTES:
                raise ValueError("Attach up to 8 images totaling 8 MiB per chat request.")
            parts.append({"type": "image_url", "image_url": {"url": image["url"]}})
        clean.append({"role": role, "content": parts})
    if text_chars > MAX_CHAT_CHARS:
        raise OverflowError("That conversation (with its files) is too long to send.")
    return clean


def chat_payload_for_target(target, payload):
    """Ollama's OpenAI-compatible endpoint uses a string image_url value."""
    if not str(target).startswith("ollama:"):
        return payload
    converted = dict(payload)
    converted["messages"] = []
    for message in payload.get("messages", []):
        item = dict(message)
        if isinstance(item.get("content"), list):
            item["content"] = [dict(part, image_url=part["image_url"]["url"])
                               if part.get("type") == "image_url" and isinstance(part.get("image_url"), dict)
                               else part for part in item["content"]]
        converted["messages"].append(item)
    return converted
SORTS = {"downloads": "downloads", "likes": "likes", "trending": "trendingScore", "recent": "lastModified"}


class AIError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- Hugging Face

class HuggingFace:
    TOKEN_FILE = "/etc/nodeyard/secrets/hf-token"

    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()
        self.token = ""
        self.reload_token()

    def reload_token(self):
        """The optional access token (`nodeyard ai hf token`), for gated models."""
        try:
            with open(self.TOKEN_FILE, "r", encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
        with self.lock:
            self.token = tok if re.match(r"^hf_[A-Za-z0-9]{20,100}$", tok) else ""
            self.cache.clear()

    def _get(self, path, ttl=300):
        now = time.time()
        with self.lock:
            hit = self.cache.get(path)
            if hit and now - hit[0] < ttl:
                return hit[1]
        conn = http.client.HTTPSConnection(HF_HOST, timeout=15)
        try:
            headers = {"User-Agent": UA, "Accept": "application/json"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(8 * 1024 * 1024)
        except (OSError, http.client.HTTPException) as e:
            raise AIError("Couldn't reach Hugging Face from this server: %s" % (getattr(e, "strerror", None) or e), 502)
        finally:
            conn.close()
        if resp.status == 404:
            raise AIError("Hugging Face doesn't have that.", 404)
        if resp.status != 200:
            raise AIError("Hugging Face answered HTTP %d." % resp.status, 502)
        try:
            data = json.loads(raw)
        except ValueError:
            raise AIError("Hugging Face sent something unreadable.", 502)
        with self.lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[path] = (now, data)
        return data

    def search(self, q, sort, limit):
        q = (q or "").strip()[:100]
        params = {"filter": "gguf", "sort": SORTS.get(sort, "downloads"), "direction": "-1", "limit": str(max(1, min(40, limit)))}
        if q:
            params["search"] = q
        data = self._get("/api/models?" + urllib.parse.urlencode(params))
        out = []
        for m in data if isinstance(data, list) else []:
            mid = m.get("id") or m.get("modelId") or ""
            if not REPO_RE.match(mid):
                continue
            out.append({"id": mid, "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
                        "updated": m.get("lastModified") or m.get("createdAt") or "", "pipeline": m.get("pipeline_tag") or "",
                        "tags": [t for t in (m.get("tags") or []) if ":" not in t and t not in ("gguf", "endpoints_compatible", "region:us")][:8]})
        return out

    def files(self, repo):
        if not REPO_RE.match(repo or ""):
            raise AIError("That isn't a Hugging Face repository name (owner/name).")
        data = self._get("/api/models/%s/tree/main?recursive=true" % urllib.parse.quote(repo, safe="/"))
        grouped = {}
        for e in data if isinstance(data, list) else []:
            path = e.get("path", "")
            if e.get("type") != "file" or not path.lower().endswith(".gguf") or ".." in path or not FILE_RE.match(path):
                continue
            size = (e.get("lfs") or {}).get("size") or e.get("size") or 0
            m = PART_RE.match(path)
            key = m.group(1) if m else path
            g = grouped.setdefault(key, {"file": path, "size": 0, "parts": 0})
            g["size"] += size
            g["parts"] += 1
            if m and m.group(2) == "00001":
                g["file"] = path
        out = []
        for g in grouped.values():
            qm = QUANT_RE.search(os.path.basename(g["file"]))
            quant = qm.group(1).upper() if qm else ""
            out.append({"file": g["file"], "size": g["size"], "parts": g["parts"], "quant": quant,
                        "split_ok": g["parts"] == 1, "ollama": ("hf.co/%s:%s" % (repo, quant)) if quant else ""})
        out.sort(key=lambda f: f["size"])
        return out


# --------------------------------------------------------------------------- jobs

# Actions that may run next to anything else: they only read, or (downloads)
# write a separate file. Everything else changes the running model, so only
# one of those runs at a time.
# Actions that change system files, so they run outside the dashboard's sandbox.
OUTSIDE_ACTIONS = {"doctor-fix", "cli", "restart-cluster", "reboot-cluster"}
# Commands the Commands page may run: filled from `nodeyard commands --json` (settings.py).
# Interactive ones need a real terminal (use the Terminal page for those).
COMMAND_PATHS = None
NOT_FROM_PAGE = {"menu", "wizard", "dashboard run", "completion"}
SHARED_ACTIONS = {"plan", "status", "test", "download", "models", "cluster-name", "disk-limit", "hw-bench"}


class Jobs:
    """nodeyard commands run for the page, with their output kept for it to show.
    Commands that change the running model run one at a time; downloads and
    read-only ones run alongside them."""

    MAX_LINES = 4000

    def __init__(self, nodeyard_bin):
        self.bin = nodeyard_bin
        self.lock = threading.Lock()
        self.jobs = {}
        self.order = []

    def running(self):
        with self.lock:
            return next((j for j in self.jobs.values() if j["status"] == "running" and j["exclusive"]), None)

    def start(self, title, argv, exclusive=True, outside=False, stdin=None, on_done=None):
        if not self.bin or not os.access(self.bin, os.X_OK):
            raise AIError("This dashboard can't run nodeyard commands (nodeyard wasn't found).", 501)
        with self.lock:
            running = [j for j in self.jobs.values() if j["status"] == "running"]
            if exclusive and any(j["exclusive"] for j in running):
                raise AIError("Another change to the model is still running (%s). Wait for it to finish." %
                              next(j["title"] for j in running if j["exclusive"]), 409)
            if len(running) >= 8:
                raise AIError("Eight tasks are already running. Wait for one to finish.", 409)
            if any(j["status"] == "running" and j["cmd"] == "nodeyard " + " ".join(argv) for j in running):
                raise AIError("That is already running.", 409)
            jid = secrets.token_hex(6)
            job = {"id": jid, "title": title, "status": "running", "rc": None, "lines": [], "started": time.time(),
                   "cmd": "nodeyard " + " ".join(argv), "exclusive": exclusive, "outside": outside, "stdin": stdin,
                   "on_done": on_done, "process": None, "cancel_requested": False}
            self.jobs[jid] = job
            self.order.append(jid)
            # forget old finished tasks, never running ones
            while len(self.order) > 25:
                old = next((j for j in self.order if self.jobs.get(j, {}).get("status") != "running"), None)
                if old is None:
                    break
                self.order.remove(old)
                self.jobs.pop(old, None)
        threading.Thread(target=self._run, args=(job, argv), daemon=True).start()
        return jid

    def _add(self, job, line):
        with self.lock:
            job["lines"].append(line.rstrip("\n")[:2000])
            del job["lines"][:-self.MAX_LINES]

    def _run(self, job, argv):
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "NODEYARD_COLOR": "never"}
        try:
            cmd = [self.bin, "--no-color"] + argv
            if job.get("outside"):
                # Outside the dashboard's own sandbox (it may only write a few
                # folders): doctor's fixes change system files.
                cmd = ["systemd-run", "--quiet", "--collect", "--wait", "--pipe", "--service-type=exec", "--setenv=HOME=/root",
                       "--setenv=NODEYARD_COLOR=never"] + cmd
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE if job.get("stdin") else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, text=True, errors="replace", bufsize=1, start_new_session=True)
        except OSError as e:
            self._add(job, "Couldn't start nodeyard: %s" % e)
            with self.lock:
                cancelled = job.get("cancel_requested", False)
                job["status"], job["rc"] = ("cancelled", -signal.SIGTERM) if cancelled else ("failed", -1)
            self._notify_done(job)
            return
        with self.lock:
            job["process"] = p
            cancel_now = job.get("cancel_requested", False)
        if cancel_now:
            self._signal_group(p, signal.SIGTERM)
            self._force_kill_later(p)
        if job.get("stdin"):
            try:
                p.stdin.write(job["stdin"])
                p.stdin.close()
            except OSError:
                pass
            job["stdin"] = None  # don't keep secrets around
        killer = threading.Timer(40 * 60, p.kill)
        killer.start()
        try:
            for line in p.stdout:
                self._add(job, line)
            rc = p.wait()
        finally:
            killer.cancel()
            p.stdout.close()
        with self.lock:
            cancelled = job.get("cancel_requested", False) and rc != 0
            job["process"] = None
            job["status"], job["rc"] = ("cancelled" if cancelled else "ok" if rc == 0 else "failed"), rc
        self._notify_done(job)

    @staticmethod
    def _signal_group(process, sig):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.send_signal(sig)
            except OSError:
                pass

    @classmethod
    def _force_kill_later(cls, process):
        def force_if_running():
            cls._signal_group(process, signal.SIGKILL)

        timer = threading.Timer(5, force_if_running)
        timer.daemon = True
        timer.start()

    def cancel(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                raise AIError("No such task.", 404)
            if job["status"] != "running":
                raise AIError("That task has already finished.", 409)
            if job.get("outside"):
                raise AIError("This task runs outside the dashboard and can't be safely cancelled here.", 409)
            if job.get("cancel_requested"):
                return True
            process = job.get("process")
            if process is not None and process.poll() is not None:
                raise AIError("That task has already finished.", 409)
            job["cancel_requested"] = True
            job["lines"].append("Cancellation requested; stopping nodeyard and its child processes…")
            del job["lines"][:-self.MAX_LINES]
        if process is not None:
            self._signal_group(process, signal.SIGTERM)
            self._force_kill_later(process)
        return True

    @staticmethod
    def _notify_done(job):
        callback = job.pop("on_done", None)
        if callback:
            try:
                callback(job)
            except Exception:  # noqa: BLE001 -- a cache refresh must not break job reporting
                pass

    def view(self, jid, since):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return None
            total = len(job["lines"])
            return {"id": jid, "title": job["title"], "status": job["status"], "rc": job["rc"], "cmd": job["cmd"], "started": job["started"],
                    "lines": job["lines"][since:], "next": total, "cancelable": job["status"] == "running" and not job.get("outside"),
                    "cancel_requested": job.get("cancel_requested", False)}

    def recent(self):
        with self.lock:
            return [{"id": j, "title": self.jobs[j]["title"], "status": self.jobs[j]["status"], "started": self.jobs[j]["started"]}
                    for j in reversed(self.order) if j in self.jobs]


def build_command(action, p, nodes, key_path):
    """The nodeyard arguments for an allowed action, or an AIError."""
    def need(name, rx):
        v = str(p.get(name, "")).strip()
        if not rx.match(v):
            raise AIError("Bad or missing %s." % name)
        return v

    if action in ("plan", "deploy", "switch"):
        if p.get("local") is True:  # already downloaded on a node: run it from there
            file = need("file", LOCAL_FILE_RE)
            spec = "local:" + file
        else:
            repo, file = need("repo", REPO_RE), need("file", FILE_RE)
            if ".." in file or file.startswith("/"):
                raise AIError("Bad file name.")
            spec = "%s:%s" % (repo, file)
        try:
            ctx = int(p.get("ctx", 8192))
        except (TypeError, ValueError):
            raise AIError("Bad context length.")
        if not 512 <= ctx <= 131072:
            raise AIError("The context length must be between 512 and 131072.")
        argv = ["ai", "split", action, "--model", spec, "--ctx", str(ctx)]
        if action == "switch" and p.get("keep_old") is True:
            argv.append("--keep-old")
        chosen = p.get("nodes") or []
        if chosen:
            if not isinstance(chosen, list) or len(chosen) > 30 or not all(isinstance(n, str) and NODE_RE.match(n) and n in nodes for n in chosen):
                raise AIError("Unknown node in the list.")
            argv += ["--nodes", ",".join(chosen)]
        if action in ("deploy", "switch"):
            alias = str(p.get("alias") or "").strip() or re.sub(r"[^a-z0-9._-]+", "-", os.path.basename(file)[:-5].lower()).strip("-.")[:60]
            if not ALIAS_RE.match(alias):
                raise AIError("Bad model name.")
            argv += ["--alias", alias, "--api-key-file", key_path, "--yes"]
        title = {"plan": "Check fit: ", "deploy": "Run: ", "switch": "Switch to: "}[action] + os.path.basename(file)
        return title, argv
    if action == "download":
        repo, file = need("repo", REPO_RE), need("file", FILE_RE)
        if ".." in file or file.startswith("/"):
            raise AIError("Bad file name.")
        return "Download: " + os.path.basename(file), ["ai", "split", "download", "--model", "%s:%s" % (repo, file)]
    if action == "split-rm":
        file = need("file", LOCAL_FILE_RE)
        return "Delete %s from every node" % file, ["ai", "split", "rm", file, "--yes"]
    if action == "clean":
        return ("Free up space (models too)" if p.get("models") is True else "Free up space"), \
            ["ai", "split", "clean", "--yes"] + (["--models"] if p.get("models") is True else [])
    if action == "undeploy":
        return "Stop and remove the split model", ["ai", "split", "undeploy", "--yes"]
    if action == "force-stop":
        return "Force stop model and cancel downloads", ["ai", "split", "undeploy", "--force", "--yes"]
    if action == "status":
        return "Model status", ["ai", "split", "status"]
    if action == "test":
        return "Speed test", ["ai", "split", "test", "--api-key-file", key_path]
    if action == "split-unload":
        return "Unload the split model (free its memory)", ["ai", "split", "unload", "--yes"]
    if action == "split-load":
        return "Load the split model", ["ai", "split", "load", "--yes"]
    if action == "split-auto-unload":   # started by lifecycle.py, never by the page itself
        try:
            idle = max(0, int(p.get("idle") or 0))
        except (TypeError, ValueError):
            idle = 0
        return "Automatic unload: idle for %d min" % round(idle / 60), ["ai", "split", "unload", "--yes"]
    if action == "ollama-rm":
        name = need("name", MODEL_RE)
        return "Delete %s from every Ollama node" % name, ["ai", "model", "rm", name, "--yes"]
    if action == "agent-install":
        return "Install the node agents", ["dashboard", "agent", "install", "--yes"]
    if action == "agent-remove":
        return "Remove the node agents", ["dashboard", "agent", "remove", "--yes"]
    if action == "pull":
        name = need("name", MODEL_RE)
        return "Download %s to every Ollama node" % name, ["ai", "model", "install", name]
    if action == "ollama-deploy":
        return "Set up Ollama on the cluster", ["ai", "deploy", "--yes"]
    # -- settings and doctor -------------------------------------------------
    if action == "cli":
        path = str(p.get("path", "")).strip()
        known = COMMAND_PATHS() if callable(COMMAND_PATHS) else set()
        if path not in known:
            raise AIError("Unknown command.")
        if path in NOT_FROM_PAGE:
            raise AIError("'%s' is interactive: use the Terminal page for it." % path)
        try:
            extra = shlex.split(str(p.get("args") or ""))
        except ValueError as e:
            raise AIError("Couldn't read the options: %s" % e)
        if len(extra) > 60 or any(len(a) > 1000 for a in extra):
            raise AIError("Too many or too long options.")
        flags = (["--yes"] if p.get("yes") is True else []) + (["--dry-run"] if p.get("dry_run") is True else []) + (["--json"] if p.get("json") is True else [])
        return "nodeyard " + " ".join([path] + extra + flags), path.split() + extra + flags
    if action == "restart-cluster":
        return "Restart Kubernetes on every node", ["restart-cluster", "--yes"]
    if action == "reboot-cluster":
        return "Reboot every machine in the cluster", ["reboot-cluster", "--yes"]
    if action == "doctor-fix":
        only = str(p.get("only") or "").strip()
        if only and not CHECK_RE.match(only):
            raise AIError("Bad check name.")
        return ("Fix: " + only if only else "Fix every problem doctor found"), ["doctor", "--fix", "--yes"] + (["--only", only] if only else [])
    if action == "gate-install":
        nets = p.get("trusted") or []
        if not isinstance(nets, list) or not 1 <= len(nets) <= 20:
            raise AIError("Give between 1 and 20 networks.")
        clean = []
        for n in nets:
            try:
                clean.append(str(ipaddress.ip_network(str(n).strip(), strict=False)))
            except ValueError:
                raise AIError("%s isn't a network (like 192.168.1.0/24)." % str(n)[:60])
        return "Model gate: no key needed from %d network(s)" % len(clean), ["ai", "gate", "install", "--trusted", ",".join(clean), "--yes"]
    if action == "hw-bench":
        node = str(p.get("node") or "").strip()
        if node and (not NODE_RE.match(node) or node not in nodes):
            raise AIError("Unknown node.")
        return ("Speed test: " + node if node else "Speed test on every node"), ["hw", "bench"] + (["--node", node] if node else [])
    if action in ("public-on", "public-off"):
        what = str(p.get("what") or "both")
        if what not in ("dashboard", "api", "both"):
            raise AIError("Say dashboard, api or both.")
        flags = [] if what == "both" else ["--" + what]
        verb = "on" if action == "public-on" else "off"
        title = ("Public access on: " if verb == "on" else "Public access off: ") + {"both": "dashboard and model API", "dashboard": "dashboard", "api": "model API"}[what]
        return title, ["public", verb, "--yes"] + flags
    if action == "gate-remove":
        return "Remove the model gate", ["ai", "gate", "remove", "--yes"]
    if action == "key-rotate":
        return "New server API key for every model", ["ai", "key", "--rotate", "--yes"]
    if action == "cluster-name":
        name = need("name", CLUSTER_RE)
        return "Rename the cluster to " + name, ["config", "set", "cluster.name", name]
    if action == "disk-limit":
        node = need("node", NODE_RE)
        if node not in nodes:
            raise AIError("Unknown node.")
        gib = str(p.get("gib", "")).strip()
        if gib != "off" and not (gib.isdigit() and 1 <= int(gib) <= 100000):
            raise AIError("The limit is a number of GiB, or off.")
        return "Disk limit for %s: %s" % (node, gib + (" GiB" if gib != "off" else "")), ["ai", "disk", "limit", node, gib]
    raise AIError("Unknown action.")


# --------------------------------------------------------------------------- live backend

class Live:
    MODEL_CACHE_TTL = 120
    MODEL_SCAN_TIMEOUT = 90
    MODEL_MISSING_NODE_RETENTION = 30 * 24 * 60 * 60
    DOWNLOAD_CACHE_TTL = 2.5

    def __init__(self, store, ai_key_file, nodeyard_bin, state_dir=""):
        self.store = store
        self.ai_key_file = ai_key_file or ""
        self.state_dir = state_dir or "/var/lib/nodeyard/dashboard"
        self.jobs = Jobs(nodeyard_bin)
        self.hf = HuggingFace()
        self.ollama_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="nodeyard-ollama")
        self.tags_cache = {}
        self.tags_lock = threading.Lock()
        self.models_lock = threading.Lock()
        self.models_cache = self._load_models_cache()
        self.models_refreshing = False
        self.models_generation = 0
        self.models_dirty = False
        self.models_error = ""
        self.downloads_lock = threading.Lock()
        self.downloads_cache = []
        self.downloads_at = 0
        self.downloads_refreshing = False
        self.lifecycle = None   # lifecycle.Lifecycle, set by lifecycle.register
        self.models_deleted = {}  # file -> (time, nodes it is still on): so no browser shows a deleted model again

    def jobs_running(self):
        return self.jobs.running() is not None

    def split_slots(self):
        """llama.cpp's /slots: is a request being worked on, and the newest task number (it grows with every request)."""
        host, port, headers, _ = self._resolve("split")
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("GET", "/slots", headers=dict(headers, **{"Accept": "application/json", "User-Agent": UA}))
            resp = conn.getresponse()
            raw = resp.read(1024 * 1024)
        except (OSError, http.client.HTTPException) as e:
            raise AIError("Couldn't read the model's activity: %s" % (getattr(e, "strerror", None) or e), 502)
        finally:
            conn.close()
        if resp.status != 200:
            raise AIError("The model's activity endpoint (/slots) answered HTTP %d." % resp.status, 502)
        try:
            slots = json.loads(raw)
        except ValueError:
            raise AIError("The model's activity endpoint sent something unreadable.", 502)
        if not isinstance(slots, list):
            raise AIError("The model's activity endpoint sent something unexpected.", 502)
        tasks = [s.get("id_task") for s in slots if isinstance(s, dict) and isinstance(s.get("id_task"), int)]
        return {"processing": any(isinstance(s, dict) and s.get("is_processing") for s in slots), "task": max(tasks) if tasks else None}

    def ollama_keep_alive(self, pod_name, model, keep_alive):
        """Set how long Ollama keeps a model loaded after its last use (-1 = until unloaded, 0 = unload now)."""
        if not MODEL_RE.match(model or ""):
            raise AIError("Bad model name.")
        pod = next((p for p in self._ollama_pods() if p["name"] == pod_name), None)
        if not pod:
            raise AIError("No such Ollama pod.", 404)
        try:
            status, data = self._ollama_json(pod, "POST", "/api/generate", {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive},
                                             timeout=600 if keep_alive != 0 else 30)
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise AIError("Couldn't reach Ollama: %s" % (getattr(e, "strerror", None) or e), 502)
        if status >= 400:
            raise AIError("Ollama said: %s" % (data.get("error") if isinstance(data, dict) else status), 502)

    def _models_cache_path(self):
        return os.path.join(self.state_dir, "ai-disk-models.json")

    def _load_models_cache(self):
        try:
            with open(self._models_cache_path(), "r", encoding="utf-8") as f:
                saved = json.load(f)
            at, data = float(saved.get("saved_at", 0)), saved.get("inventory")
            if at > 0 and isinstance(data, dict) and data.get("ok") and isinstance(data.get("nodes"), list):
                return (at, data)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def _save_models_cache(self, cache):
        tmp = ""
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ai-disk-models-", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"saved_at": cache[0], "inventory": cache[1]}, f, separators=(",", ":"))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._models_cache_path())
            try:
                dfd = os.open(self.state_dir, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass  # Some filesystems don't support syncing directory entries.
            return True
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

    @staticmethod
    def _models_save_error():
        return "The models were found, but the dashboard couldn't save their locations. Check write access to /var/lib/nodeyard/dashboard."

    def _forget_models_cache(self, file=None, keep_nodes=()):
        """Rescan soon; with FILE, drop it from the saved inventory now, except on KEEP_NODES
        (nodes the delete couldn't reach: their copy is still there as far as anyone knows)."""
        with self.models_lock:
            self.models_generation += 1
            self.models_dirty = True
            self.models_error = ""
            if file and self.models_cache:
                base = file[:-5] if file.lower().endswith(".gguf") else file
                cache_key = re.sub(r"[^a-z0-9-]", "-", base.lower())[:40]
                data = dict(self.models_cache[1])
                data["nodes"] = [n if n.get("node") in keep_nodes else
                                 dict(n, items=[it for it in n.get("items", [])
                                                if not (it.get("kind") == "model" and it.get("name") == file)
                                                and not (it.get("kind") == "partial" and re.sub(r"\.(part\d*|joining|copying)$", "", it.get("name", "")) == file)
                                                and not (it.get("kind") == "cache" and it.get("name") == cache_key)])
                                 for n in data.get("nodes", [])]
                self.models_cache = (time.time(), data)
                if not self._save_models_cache(self.models_cache):
                    self.models_error = self._models_save_error()
            # Keep the last known inventory while a background rescan runs. A
            # download or deploy can take minutes; hiding a good snapshot here
            # made the UI claim that nothing was downloaded during that time.

    def _model_job_done(self, action, params, job):
        if job.get("status") != "ok":
            self._forget_models_cache()
            return
        if action == "split-rm":
            # "  NODE: not reached (...)": that node was NotReady or gone, so its copy stays listed (as last seen).
            skipped = {m.group(1) for m in (NOT_REACHED_RE.match(line) for line in job.get("lines", [])) if m}
            file = str(params.get("file") or "")
            with self.models_lock:
                self.models_deleted[file] = (time.time(), sorted(skipped))
                for old in [f for f, (at, _) in self.models_deleted.items() if time.time() - at > 7 * 24 * 3600][:100]:
                    self.models_deleted.pop(old, None)
            self._forget_models_cache(file, keep_nodes=skipped)
        else:
            self._forget_models_cache()

    def _state(self):
        st = self.store.snapshot()["state"]
        if not st:
            raise AIError("The dashboard hasn't read the cluster yet. Try again in a few seconds.", 503)
        return st

    def api_key(self):
        """The server-wide API key, read fresh from its root-only file (empty if none)."""
        if not self.ai_key_file:
            return ""
        try:
            with open(self.ai_key_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def ensure_key_file(self):
        if not self.ai_key_file:
            raise AIError("This dashboard doesn't know where the server API key lives.", 501)
        if not os.path.exists(self.ai_key_file):
            os.makedirs(os.path.dirname(self.ai_key_file), mode=0o700, exist_ok=True)
            fd = os.open(self.ai_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(24))
        return self.ai_key_file

    # -- what can be chatted with --------------------------------------------

    def _ollama_models(self, pod):
        now = time.time()
        with self.tags_lock:
            hit = self.tags_cache.get(pod["name"])
            if hit and now - hit[0] < 20:
                return hit[1]
        models = []
        try:
            conn = http.client.HTTPConnection(pod["ip"], OLLAMA_PORT, timeout=4)
            conn.request("GET", "/api/tags")
            resp = conn.getresponse()
            data = json.loads(resp.read(1024 * 1024))
            models = [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in data.get("models", []) if MODEL_RE.match(m.get("name", ""))]
            conn.close()
        except (OSError, ValueError, http.client.HTTPException):
            models = []
        with self.tags_lock:
            self.tags_cache[pod["name"]] = (now, models)
        return models

    def _ollama_json(self, pod, method, path, body=None, timeout=5):
        conn = http.client.HTTPConnection(pod["ip"], OLLAMA_PORT, timeout=timeout)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read(2 * 1024 * 1024)
            return resp.status, (json.loads(data) if data else {})
        finally:
            conn.close()

    def _ollama_pods(self):
        return [p for p in self._state()["pods"] if p["namespace"] == OLLAMA_NS and p["name"].startswith("ollama") and p["status"] == "Running" and p["ip"]]

    def ollama_overview(self):
        """Every Ollama pod with the models on its disk and which are loaded in memory right now."""
        pods = self._ollama_pods()
        requests = {}
        for p in pods:
            requests[(p["name"], "tags")] = self.ollama_pool.submit(self._ollama_json, p, "GET", "/api/tags")
            requests[(p["name"], "ps")] = self.ollama_pool.submit(self._ollama_json, p, "GET", "/api/ps")
        out = []
        for p in pods:
            entry = {"pod": p["name"], "node": p["node"], "models": [], "error": ""}
            try:
                _, tags = requests[(p["name"], "tags")].result()
                _, ps = requests[(p["name"], "ps")].result()
                loaded = {m.get("name"): m for m in ps.get("models", [])}
                for m in tags.get("models", []):
                    name = m.get("name", "")
                    if MODEL_RE.match(name):
                        lm = loaded.get(name)
                        entry["models"].append({"name": name, "size": m.get("size", 0), "loaded": bool(lm), "memory": (lm or {}).get("size_vram") or (lm or {}).get("size") or 0,
                                                "expires": (lm or {}).get("expires_at", ""), "params": (m.get("details") or {}).get("parameter_size", ""),
                                                "quant": (m.get("details") or {}).get("quantization_level", "")})
            except (OSError, ValueError, http.client.HTTPException) as e:
                entry["error"] = "Couldn't ask this Ollama: %s" % (getattr(e, "strerror", None) or e)
            out.append(entry)
        return out

    def ollama_load(self, pod_name, model, on):
        # An explicit Load stays loaded until Unload (keep-alive -1), or, with automatic
        # unloading on, until it has been idle for the chosen time (lifecycle.py).
        keep = (self.lifecycle.load_keep_alive() if self.lifecycle else -1) if on else 0
        self.ollama_keep_alive(pod_name, model, keep)
        if self.lifecycle:
            self.lifecycle.set_pinned(pod_name, model, on)

    def reveal_key(self):
        key = self.api_key()
        if not key:
            raise AIError("There's no API key file for the model (looked in %s)." % (self.ai_key_file or "nowhere"), 404)
        return key

    def targets(self):
        st = self._state()
        out = []
        sp = (st.get("ai") or {}).get("split")
        if sp:
            out.append({"id": "split", "kind": "split", "name": sp.get("alias") or "split model", "model": sp.get("model", ""),
                        "detail": "split across %d machine%s" % (len(sp.get("shares", [])), "" if len(sp.get("shares", [])) == 1 else "s"),
                        "ready": bool(sp.get("ready"))})
        for p in st["pods"]:
            if p["namespace"] == OLLAMA_NS and p["name"].startswith("ollama") and p["status"] == "Running" and p["ip"]:
                for m in self._ollama_models(p):
                    out.append({"id": "ollama:%s:%s" % (p["name"], m["name"]), "kind": "ollama", "name": m["name"], "model": m["name"],
                                "detail": "Ollama on %s" % (p["node"] or p["name"]), "ready": True, "size": m["size"]})
        busy = self.jobs.running()
        return {"targets": out, "can_run": bool(self.jobs.bin and os.access(self.jobs.bin, os.X_OK)), "busy": busy["id"] if busy else None,
                "has_key": bool(self.api_key()), "recent": self.jobs.recent()}

    def _resolve(self, target):
        st = self._state()
        if target == "split":
            sp = (st.get("ai") or {}).get("split")
            svc = next((s for s in st["services"] if s["namespace"] == SPLIT_NS and s["name"] == SPLIT_SVC), None)
            if not sp or not svc or not svc["cluster_ip"]:
                raise AIError("No split model is deployed.", 404)
            if not sp.get("ready"):
                raise AIError("The model isn't loaded yet. Check its progress on the Models tab.", 503)
            headers = {}
            key = self.api_key()
            if sp.get("auth"):
                if not key:
                    raise AIError("The model needs an API key, and the dashboard can't read it (looked in %s)." % (self.ai_key_file or "nowhere"), 500)
                headers["Authorization"] = "Bearer " + key
            return svc["cluster_ip"], SPLIT_PORT, headers, sp.get("alias") or "model"
        m = re.match(r"^ollama:([a-z0-9-]+):(.+)$", target or "")
        if m and MODEL_RE.match(m.group(2)):
            pod = next((p for p in st["pods"] if p["namespace"] == OLLAMA_NS and p["name"] == m.group(1) and p["status"] == "Running" and p["ip"]), None)
            if pod:
                return pod["ip"], OLLAMA_PORT, {}, m.group(2)
        raise AIError("Unknown model.", 404)

    def open_chat(self, target, payload, on_conn=None):
        lc = self.lifecycle
        token = lc.begin(target) if lc else None
        try:
            conn, resp = self._open_chat(target, payload, on_conn)
        except BaseException:
            if lc:
                lc.end(token)
            raise
        if lc:
            close = conn.close

            def close_and_record():
                try:
                    close()
                finally:
                    lc.end(token)   # (ends once: later calls are ignored)
            conn.close = close_and_record
        return conn, resp

    def _open_chat(self, target, payload, on_conn=None):
        host, port, headers, model = self._resolve(target)
        body = dict(chat_payload_for_target(target, payload), model=model, stream=True)
        headers = dict(headers, **{"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": UA})
        # A stale Kubernetes service or unreachable pod must fail quickly. Keep a
        # long read timeout after connecting because prompt evaluation can take time
        # on a large model or a slower node.
        conn = http.client.HTTPConnection(host, port, timeout=10)
        if on_conn:
            on_conn(conn)  # so Stop can cut it, even while the model still reads the prompt
        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(900)
            conn.request("POST", "/v1/chat/completions", body=json.dumps(body), headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise AIError("Couldn't reach the model: %s" % (getattr(e, "strerror", None) or e), 502)
        if resp.status >= 400:
            raw = resp.read(4096)
            conn.close()
            msg = ""
            try:
                err = json.loads(raw).get("error", "")
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
            except ValueError:
                pass
            raise AIError("The model answered HTTP %d%s" % (resp.status, (": " + msg) if msg else "."), 502)
        return conn, resp

    # -- running models ----------------------------------------------------------

    def run(self, action, p):
        st = self._state()
        key_path = ""
        if action in ("deploy", "switch", "test"):
            key_path = self.ensure_key_file()
        title, argv = build_command(action, p, {n["name"] for n in st["nodes"]}, key_path)
        stdin = p.get("stdin") if action == "cli" and isinstance(p.get("stdin"), str) and p.get("stdin") else None
        if stdin is not None and len(stdin) > 65536:
            raise AIError("The input is too long.")
        exclusive = action not in SHARED_ACTIONS and not (action == "cli" and p.get("dry_run") is True)
        changes_models = action in {"deploy", "switch", "download", "split-rm", "clean", "undeploy", "force-stop"}
        on_done = (lambda job: self._model_job_done(action, p, job)) if changes_models else None
        if action == "ollama-rm" and self.lifecycle:
            name = str(p.get("name") or "")
            on_done = lambda job: [self.lifecycle.set_pinned(pod, name, False) for pod, model in self.lifecycle.pinned() if model == name]  # noqa: E731
        return self.jobs.start(title, argv, exclusive=exclusive, outside=action in OUTSIDE_ACTIONS, stdin=stdin, on_done=on_done)

    # -- what is on each node's disk ------------------------------------------------

    def disk_models(self, force=False):
        """Return the last inventory immediately; refresh cluster disks in the background."""
        now = time.time()
        with self.models_lock:
            cache = self.models_cache
            stale = self.models_dirty or not cache or now - cache[0] >= self.MODEL_CACHE_TTL
            start = (force or stale) and not self.models_refreshing
            if start:
                self.models_refreshing = True
                generation = self.models_generation
            refreshing = self.models_refreshing
            error = self.models_error
        if start:
            try:
                threading.Thread(target=self._refresh_models, args=(generation,), name="nodeyard-ai-disk-scan", daemon=True).start()
            except RuntimeError as exc:
                with self.models_lock:
                    self.models_refreshing = False
                    self.models_error = "Couldn't start the disk scan: %s" % exc
                refreshing = False
                error = self.models_error
        downloads = self._downloads()
        if not cache:
            if not refreshing:
                raise AIError(error or "Couldn't look at the nodes' disks.", 502)
            data = {"ok": True, "nodes": [], "in_use": ""}
        else:
            data = dict(cache[1])
        with self.models_lock:
            deleted = [{"file": f, "at": at, "kept_on": kept} for f, (at, kept) in self.models_deleted.items()]
        data.update({"downloads": downloads, "scanning": refreshing, "updated": cache[0] if cache else None,
                     "inventory_stale": bool(stale or refreshing), "deleted": deleted})
        if error:
            data["scan_error"] = error
        return data

    def _refresh_models(self, generation):
        while True:
            if not self.jobs.bin or not os.access(self.jobs.bin, os.X_OK):
                error = "This dashboard can't run nodeyard commands (nodeyard wasn't found)."
                valid = False
                data = None
            else:
                env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "NODEYARD_COLOR": "never"}
                try:
                    out = subprocess.run([self.jobs.bin, "--no-color", "ai", "split", "models", "--json"], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=self.MODEL_SCAN_TIMEOUT, env=env)
                    data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else None
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    data = None
                valid = isinstance(data, dict) and bool(data.get("ok")) and isinstance(data.get("nodes"), list)
                error = "Couldn't look at the nodes' disks."
                if valid:
                    previous_nodes = {n.get("node"): n for n in (self.models_cache or (0, {"nodes": []}))[1].get("nodes", [])}
                    current_names = {n.get("node") for n in data["nodes"] if n.get("node")}
                    missed = [n.get("node", "unknown") for n in data["nodes"] if n.get("scan_error")]
                    retained = []
                    if missed:
                        for i, node in enumerate(data["nodes"]):
                            previous = previous_nodes.get(node.get("node"))
                            if node.get("scan_error") and previous:
                                node = dict(previous, scan_error=node["scan_error"], inventory_stale=True)
                                data["nodes"][i] = node
                                retained.append(node.get("node", "unknown"))
                    # During a Kubernetes restart, a worker may temporarily
                    # disappear from the API's node list. Keep its persisted
                    # inventory as last-seen data instead of replacing the
                    # cache with a smaller, apparently empty cluster.
                    now = time.time()
                    for name, previous in previous_nodes.items():
                        if not name or name in current_names:
                            continue
                        try:
                            missing_since = float(previous.get("inventory_missing_since", now))
                        except (TypeError, ValueError):
                            missing_since = now
                        if now - missing_since > self.MODEL_MISSING_NODE_RETENTION:
                            continue
                        data["nodes"].append(dict(
                            previous,
                            scan_error="This machine is not currently listed by Kubernetes.",
                            inventory_stale=True,
                            inventory_missing_since=missing_since,
                        ))
                        retained.append(name)
                    if missed or retained:
                        details = []
                        if missed:
                            details.append("disk checks failed on " + ", ".join(missed))
                        if retained:
                            details.append("showing last-seen inventory for " + ", ".join(sorted(set(retained))))
                        data["scan_error"] = "Some model locations may be stale: " + "; ".join(details) + "."

            with self.models_lock:
                # A model may have been downloaded or deleted while the scan ran.
                # Discard that result and rescan so stale locations never return.
                if generation != self.models_generation:
                    generation = self.models_generation
                    continue
                self.models_refreshing = False
                if valid:
                    # A deleted model that a scan finds again (downloaded again) is no longer "deleted".
                    found = {it.get("name") for n in data["nodes"] if not n.get("scan_error") for it in n.get("items", []) if it.get("kind") == "model"}
                    for f in [f for f in self.models_deleted if f in found]:
                        self.models_deleted.pop(f, None)
                    cache = (time.time(), data)
                    self.models_cache = cache
                    self.models_dirty = False
                    self.models_error = ""
                    if not self._save_models_cache(cache):
                        self.models_error = self._models_save_error()
                else:
                    self.models_error = error
                return

    def _downloads(self):
        """Return cached download progress and refresh it off the request thread."""
        now = time.time()
        with self.downloads_lock:
            start = now - self.downloads_at >= self.DOWNLOAD_CACHE_TTL and not self.downloads_refreshing
            if start:
                self.downloads_refreshing = True
            result = list(self.downloads_cache)
        if start:
            threading.Thread(target=self._refresh_downloads, name="nodeyard-ai-download-status", daemon=True).start()
        return result

    def _refresh_downloads(self):
        try:
            result = self._query_downloads()
        except Exception:  # noqa: BLE001 -- don't leave the background refresh stuck
            result = []
        finally:
            with self.downloads_lock:
                self.downloads_cache = result
                self.downloads_at = time.time()
                self.downloads_refreshing = False

    def _query_downloads(self):
        """Download Jobs with their progress (from their last log line)."""
        try:
            jobs = self.store.source.client.get("/apis/batch/v1/namespaces/ai-split/jobs?labelSelector=app.kubernetes.io%2Fcomponent%3Dmodel-download", timeout=5)
        except Exception:  # noqa: BLE001 -- no namespace yet, or the API is busy
            return []
        out = []
        for j in (jobs or {}).get("items", []):
            ann = j["metadata"].get("annotations", {}) or {}
            st = j.get("status", {})
            d = {"job": j["metadata"]["name"], "file": ann.get("nodeyard/file", ""), "node": ann.get("nodeyard/node", ""),
                 "size": int(ann.get("nodeyard/size", "0") or 0), "got": 0, "state": "done" if st.get("succeeded") else ("running" if st.get("active") else "failed")}
            if d["state"] == "running":
                try:
                    pods = self.store.source.client.get("/api/v1/namespaces/ai-split/pods?labelSelector=job-name%3D" + urllib.parse.quote(d["job"]), timeout=5)
                    name = pods["items"][-1]["metadata"]["name"]
                    text = self.store.source.client.get("/api/v1/namespaces/ai-split/pods/%s/log?tailLines=8" % urllib.parse.quote(name), timeout=5, raw=True)
                    lines = (text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)).strip().splitlines()
                    last = lines[-1] if lines else ""
                    prog = [x.split() for x in lines if x.startswith("progress ") and len(x.split()) >= 4]
                    if last.startswith("progress "):
                        d["got"] = int(last.split()[1])
                    if len(prog) >= 2 and int(prog[-1][3]) > int(prog[0][3]):
                        # speed over the last ~70 s, and the time left at that speed
                        d["rate"] = (int(prog[-1][1]) - int(prog[0][1])) / (int(prog[-1][3]) - int(prog[0][3]))
                        if d["rate"] > 0 and d["size"] > d["got"]:
                            d["eta"] = int((d["size"] - d["got"]) / d["rate"])
                    elif "NOT ENOUGH DISK" in last:
                        d["state"], d["note"] = "stuck", last
                    elif "joining" in last or "verifying" in last:
                        d["got"], d["note"] = d["size"], last.split(" ", 1)[-1]
                except Exception:  # noqa: BLE001 -- pod not started yet
                    pass
            out.append(d)
        return out

    def job(self, jid, since):
        return self.jobs.view(jid, since)

    def cancel_job(self, jid):
        return self.jobs.cancel(jid)


# --------------------------------------------------------------------------- the pages' routes

def register(ctx, args):
    if getattr(args, "demo", False):
        import demoai
        backend = demoai.DemoAI(ctx.store)
    else:
        backend = Live(ctx.store, getattr(args, "ai_key_file", ""), getattr(args, "nodeyard_bin", ""), getattr(args, "state_dir", ""))
    ctx.ai = backend

    def fail(h, e):
        h._json({"ok": False, "error": str(e)}, getattr(e, "code", 400) if isinstance(e, AIError) else 500)

    def targets(h, q):
        try:
            h._json(dict(backend.targets(), ok=True, demo=bool(getattr(args, "demo", False))))
        except AIError as e:
            fail(h, e)

    def search(h, q):
        try:
            limit = int(q.get("limit", ["24"])[0])
        except ValueError:
            limit = 24
        try:
            h._json({"ok": True, "results": backend.hf.search(q.get("q", [""])[0], q.get("sort", ["downloads"])[0], limit)})
        except AIError as e:
            fail(h, e)

    def files(h, q):
        try:
            h._json({"ok": True, "files": backend.hf.files(q.get("repo", [""])[0])})
        except AIError as e:
            fail(h, e)

    def disk_models(h, q):
        try:
            h._json(dict(backend.disk_models(force=q.get("refresh", [""])[0] == "1"), ok=True))
        except AIError as e:
            fail(h, e)

    def ollama(h, q):
        try:
            h._json({"ok": True, "pods": backend.ollama_overview()})
        except AIError as e:
            fail(h, e)

    def ollama_load(h, body):
        try:
            backend.ollama_load(str(body.get("pod", "")), str(body.get("model", "")), bool(body.get("load", True)))
            h._json({"ok": True})
        except AIError as e:
            fail(h, e)

    def reveal(h, body):
        try:
            h._json({"ok": True, "key": backend.reveal_key()})
        except AIError as e:
            fail(h, e)

    def job(h, q):
        try:
            since = max(0, int(q.get("since", ["0"])[0]))
        except ValueError:
            since = 0
        v = backend.job(q.get("id", [""])[0], since)
        if v is None:
            return h._json({"ok": False, "error": "No such task."}, 404)
        h._json(dict(v, ok=True))

    def cancel_job(h, body):
        jid = str(body.get("id", ""))[:64]
        try:
            backend.cancel_job(jid)
            h._json({"ok": True, "cancelling": True})
        except AIError as e:
            fail(h, e)

    def run(h, body):
        action = str(body.get("action", ""))
        if action in ("cli", "restart-cluster", "reboot-cluster") and h._forwarded() is not None:
            return h._json({"ok": False, "error": "This only works over Tailscale or your own network, not through public access."}, 403)
        try:
            h._json({"ok": True, "job": backend.run(action, body)})
        except AIError as e:
            fail(h, e)

    # Chats being answered, so Stop can end them: id -> (owner, upstream connection).
    # Closing the connection to the model makes llama.cpp stop working on the answer.
    streams = {}
    streams_lock = threading.Lock()

    def cut(conn):
        try:
            sock = conn.sock
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass

    def watch(h, conn, done):
        """Ends the model's work when the page goes away (closed tab, aborted request),
        not only at the next word it would have sent."""
        sock = h.connection
        if isinstance(sock, ssl.SSLSocket):
            return  # (can't peek through TLS; the Stop button still works)
        while not done.is_set():
            try:
                r, _, _ = select.select([sock], [], [], 1.0)
                if r and not sock.recv(1, socket.MSG_PEEK):
                    cut(conn)
                    return
                if r:
                    done.wait(1.0)
            except (OSError, ValueError):
                cut(conn)
                return

    def stop(h, body):
        sid = str(body.get("id", ""))[:64]
        with streams_lock:
            e = streams.get(sid)
        if e and e[0] == (h._token() or "local"):
            cut(e[1])
        h._json({"ok": True, "stopped": bool(e)})

    def chat(h, body):
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not 1 <= len(msgs) <= 200:
            return h._json({"ok": False, "error": "Send between 1 and 200 messages."}, 400)
        try:
            clean = clean_chat_messages(msgs)
        except OverflowError as e:
            return h._json({"ok": False, "error": str(e)}, 413)
        except ValueError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        try:
            temp = min(2.0, max(0.0, float(body.get("temperature", 0.7))))
            raw_max = body.get("max_tokens", 1024)
            max_tokens = 0 if raw_max in (None, 0, "0", "", "none") else min(65536, max(1, int(raw_max)))   # 0 = no limit
        except (TypeError, ValueError):
            return h._json({"ok": False, "error": "Bad temperature or token limit."}, 400)
        sid = str(body.get("stream_id") or "")[:64] or secrets.token_hex(8)
        owner = h._token() or "local"
        done = threading.Event()

        def on_conn(conn):
            with streams_lock:
                streams[sid] = (owner, conn)
            threading.Thread(target=watch, args=(h, conn, done), daemon=True).start()
        try:
            try:
                target = str(body.get("target", ""))
                payload = dict({"messages": clean, "temperature": temp}, **({"max_tokens": max_tokens} if max_tokens else {}))
                if target == "split":
                    payload["return_progress"] = True   # llama.cpp says how far it has read the prompt (the page shows it)
                conn, resp = backend.open_chat(target, payload, on_conn=on_conn)
            except AIError as e:
                return fail(h, e)
            try:
                h.send_response(200)
                for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                             ("X-Accel-Buffering", "no"), ("Connection", "close")):
                    h.send_header(k, v)
                h.end_headers()
                h.close_connection = True
                for line in iter(resp.readline, b""):
                    h.wfile.write(line)
                    h.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass  # stopped, or the page went away
            finally:
                conn.close()
        finally:
            done.set()
            with streams_lock:
                streams.pop(sid, None)


    def web(h, body):
        import webtools
        try:
            h._json(webtools.research(body.get("query"), demo=bool(getattr(args, "demo", False))))
        except webtools.WebError as e:
            h._json({"ok": False, "error": str(e)}, 502)

    ctx.post_routes["/api/ai/web"] = web
    ctx.get_routes.update({"/api/ai/targets": targets, "/api/ai/search": search, "/api/ai/files": files, "/api/job": job, "/api/ai/ollama": ollama,
                           "/api/ai/models": disk_models})
    ctx.post_routes.update({"/api/ai/chat": chat, "/api/ai/stop": stop, "/api/run": run, "/api/job/cancel": cancel_job,
                            "/api/ai/ollama-load": ollama_load, "/api/ai/reveal-key": reveal})
    ctx.post_limits["/api/ai/chat"] = MAX_CHAT_BODY_BYTES
