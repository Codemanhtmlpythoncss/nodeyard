"""Plugins for the dashboard chat: the AI can search the web, read pages, look things up, calculate, run Python and use files,
by running yardcode's agent for the conversation and streaming what it does to the page.

Each chat gets its own `yardcode --serve-json` process (JSON lines in and out, see yardcode/src/yardcode/serve.py). Tools that only
read the internet run inside the dashboard's own sandbox; Python, files and shell run as the terminal user (never root), only over
Tailscale or your own network, and every change asks you first (a card with Allow / Deny in the chat).
"""
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

PLUGINS = [
    # id, label, what it does, tools, runs code or changes things (needs the terminal user)
    ("web", "Web search", "Searches the internet and reads pages (with sources)", ["WebSearch", "WebFetch"], False),
    ("wikipedia", "Wikipedia", "Looks things up on Wikipedia", ["Wikipedia"], False),
    ("arxiv", "arXiv", "Finds research papers", ["Arxiv"], False),
    ("weather", "Weather", "Current weather and a 3-day forecast for any place", ["Weather"], False),
    ("calculator", "Calculator", "Exact maths instead of guessing", ["Calculator"], False),
    ("tasks", "Task list", "Keeps a visible to-do list for bigger jobs", ["TodoWrite"], False),
    ("python", "Code interpreter", "Runs Python the AI writes, like a notebook (asks you first)", ["Python"], True),
    ("files", "Files and shell", "Reads and edits files and runs commands in a work folder (asks you first)", ["Read", "Write", "Edit", "LS", "Glob", "Grep", "Bash"], True),
]
BY_ID = {p[0]: p for p in PLUGINS}
MAX_PROCS = 3
IDLE_SECONDS = 20 * 60


def find_yardcode():
    for cand in (os.path.join(HERE, "..", "..", "..", "yardcode", "bin", "yardcode"), os.path.join(HERE, "yardcode", "bin", "yardcode")):
        cand = os.path.abspath(cand)
        if os.path.isfile(cand):
            return cand
    return ""


class AgentError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Proc:
    """One chat's agent process, with its events collected in a queue."""

    def __init__(self, owner, signature, argv, env):
        self.owner, self.signature = owner, signature
        self.q = queue.Queue()
        self.last = time.time()
        self.busy = False
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                continue
        self.q.put({"type": "exit"})

    def send(self, obj):
        self.last = time.time()
        try:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()
        except (OSError, ValueError):
            raise AgentError("The AI's helper stopped. Send your message again.", 410)

    @property
    def alive(self):
        return self.p.poll() is None

    def stop(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.terminate()
        except OSError:
            pass


class Agents:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.user = getattr(args, "terminal_user", "") or ""
        self.bin = find_yardcode()
        self.procs = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(60)
            now = time.time()
            with self.lock:
                for cid, pr in list(self.procs.items()):
                    if not pr.alive or (not pr.busy and now - pr.last > IDLE_SECONDS):
                        pr.stop()
                        self.procs.pop(cid, None)

    # ---- what can be switched on ------------------------------------------------------------------------
    def code_ok(self, h):
        if self.demo:
            return True, ""
        if not self.user:
            return False, "needs the terminal user (restart the dashboard with sudo from your own account)"
        if h._forwarded() is not None:
            return False, "not available through public access"
        return True, ""

    def listing(self, h):
        have = bool(self.demo or self.bin)
        out = []
        for pid, label, desc, tools, code in PLUGINS:
            ok, why = (True, "")
            if not have:
                ok, why = False, "yardcode isn't installed next to nodeyard on this server"
            elif code:
                ok, why = self.code_ok(h)
            out.append({"id": pid, "label": label, "desc": desc, "tools": tools, "code": code, "available": ok, "why": why})
        return out

    # ---- processes ----------------------------------------------------------------------------------------
    def _model(self):
        """Where the model is, from inside this machine: address, port and the key if it needs one."""
        backend = self.ctx.ai
        host, port, headers, model = backend._resolve("split")
        key = (headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
        return "http://%s:%d/v1" % (host, port), key, model

    def _argv(self, plugins, max_tokens):
        tools = sorted({t for p in plugins for t in BY_ID[p][3]})
        base, key, model = self._model()
        code = any(BY_ID[p][4] for p in plugins)
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "YARDCODE_API_KEY": key, "YARDCODE_HOME": "/tmp/yardcode-dash",
               "YARDCODE_DATA": "/tmp/yardcode-dash-data", "HOME": "/tmp"}
        cmd = [sys.executable, self.bin, "--serve-json", "--no-session", "--tools", ",".join(tools), "--api-base", base, "--model", model, "--max-tokens", str(max_tokens or 0),
               "--permission-mode", "default", "--no-color"]
        if code:   # outside the dashboard's sandbox, as the terminal user, in their home folder
            cmd = ["systemd-run", "--quiet", "--collect", "--pipe", "--wait", "--uid=" + self.user, "--setenv=YARDCODE_API_KEY", "-p", "WorkingDirectory=~",
                   "--setenv=LANG=C.UTF-8", "--description=nodeyard dashboard AI plugins"] + cmd
        else:
            cmd += ["--cwd", "/tmp"]
        return cmd, env

    def get(self, h, cid, plugins, max_tokens, create=True):
        owner = h._token() or "local"
        sig = ",".join(sorted(plugins)) + "|" + str(max_tokens or 0)
        with self.lock:
            pr = self.procs.get(cid)
            if pr and (pr.owner != owner or not pr.alive or pr.signature != sig):
                pr.stop()
                self.procs.pop(cid, None)
                pr = None
            if pr is None and create:
                if len([p for p in self.procs.values() if p.alive]) >= MAX_PROCS:
                    raise AgentError("%d AI helpers are busy already. Close a chat or wait." % MAX_PROCS, 429)
                if self.demo:
                    pr = DemoProc(owner, sig)
                else:
                    if not self.bin:
                        raise AgentError("yardcode isn't installed next to nodeyard on this server.", 501)
                    argv, env = self._argv(plugins, max_tokens)
                    pr = Proc(owner, sig, argv, env)
                self.procs[cid] = pr
            return pr, owner

    # ---- the routes ------------------------------------------------------------------------------------------
    def run(self, h, body):
        cid = str(body.get("chat") or "")[:64]
        text = str(body.get("text") or "")
        plugins = [p for p in (body.get("plugins") or []) if p in BY_ID]
        if not cid or not text.strip() or not plugins:
            return h._json({"ok": False, "error": "Say which chat, what to do and which plugins."}, 400)
        if len(text) > 200000:
            return h._json({"ok": False, "error": "That message is too long."}, 413)
        listing = {p["id"]: p for p in self.listing(h)}
        for p in plugins:
            if not listing[p]["available"]:
                return h._json({"ok": False, "error": "%s: %s." % (listing[p]["label"], listing[p]["why"])}, 403)
        try:
            pr, _ = self.get(h, cid, plugins, int(body.get("max_tokens") or 0))
        except AgentError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)
        fresh = not getattr(pr, "started", False)
        pr.started = True
        history = body.get("history")
        if fresh and isinstance(history, list) and history:
            lines = []
            for m in history[-8:]:
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                    lines.append("%s: %s" % (m["role"].upper(), m["content"][:1500]))
            if lines:
                text = "<earlier_conversation>\n%s\n</earlier_conversation>\n\n%s" % ("\n\n".join(lines), text)
        with self.lock:
            if pr.busy:
                return h._json({"ok": False, "error": "The AI is still answering the last message."}, 409)
            pr.busy = True
        h.send_response(200)
        for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Accel-Buffering", "no"),
                     ("Connection", "close")):
            h.send_header(k, v)
        h.end_headers()
        h.close_connection = True
        try:
            while not pr.q.empty():   # leftovers of an interrupted turn
                pr.q.get_nowait()
            pr.send({"type": "user", "text": text})
            while True:
                try:
                    ev = pr.q.get(timeout=15)
                except queue.Empty:
                    h.wfile.write(b": ping\n\n")
                    h.wfile.flush()
                    if not pr.alive:
                        ev = {"type": "exit"}
                    else:
                        continue
                h.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode("utf-8"))
                h.wfile.flush()
                if ev.get("type") in ("done", "exit"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError, AgentError):
            try:
                pr.send({"type": "interrupt"})
            except AgentError:
                pass
        finally:
            pr.busy = False

    def reply(self, h, body):
        pr, _ = self.get(h, str(body.get("chat") or "")[:64], [], 0, create=False) if False else (self.procs.get(str(body.get("chat") or "")[:64]), None)
        if pr is None or pr.owner != (h._token() or "local"):
            return h._json({"ok": False, "error": "No such conversation."}, 404)
        pr.send({"type": "permission_reply", "id": str(body.get("id") or ""), "decision": "allow" if body.get("decision") == "allow" else "deny",
                 "scope": "session" if body.get("scope") == "session" else "once", "feedback": str(body.get("feedback") or "")[:300]})
        h._json({"ok": True})

    def stop(self, h, body):
        pr = self.procs.get(str(body.get("chat") or "")[:64])
        if pr is not None and pr.owner == (h._token() or "local"):
            pr.send({"type": "interrupt"})
        h._json({"ok": True})

    def reset(self, h, body):
        with self.lock:
            pr = self.procs.pop(str(body.get("chat") or "")[:64], None)
        if pr is not None and pr.owner == (h._token() or "local"):
            pr.stop()
        h._json({"ok": True})


class DemoProc:
    """A pretend agent for the demo and the tests: it searches, asks permission for code, and answers. Nothing leaves this computer."""

    def __init__(self, owner, signature):
        self.owner, self.signature = owner, signature
        self.q = queue.Queue()
        self.busy = False
        self.last = time.time()
        self.started = False
        self.waiters = {}
        self.stopped = False

    @property
    def alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True

    def send(self, obj):
        self.last = time.time()
        t = obj.get("type")
        if t == "user":
            threading.Thread(target=self._turn, args=(obj.get("text", ""),), daemon=True).start()
        elif t == "permission_reply":
            w = self.waiters.get(obj.get("id"))
            if w:
                w.put(obj)

    def emit(self, **ev):
        self.q.put(ev)

    def _turn(self, text):
        e = self.emit
        e(type="text", delta="I'll look that up. ")
        e(type="text_end")
        e(type="tool_use", id="t1", name="WebSearch", summary="WebSearch(%s)" % text[:40], input={"query": text[:80]}, depth=0)
        time.sleep(0.2)
        e(type="tool_result", id="t1", name="WebSearch", ok=True, summary="3 results (demo)", text="1. Demo result\n   https://example.com\n   nothing was searched", preview=["Demo result  example.com"], diff=None, depth=0)
        if "python" in plugins_in(self.signature) and ("run" in text.lower() or "code" in text.lower() or "python" in text.lower()):
            rid = secrets.token_hex(4)
            w = queue.Queue()
            self.waiters[rid] = w
            e(type="permission", id=rid, tool="Python", summary="Python(print(6*7))", reason="it runs code", risk="", suggest="Python", command="print(6 * 7)", target=None, diff=None)
            r = w.get(timeout=120)
            if r.get("decision") == "allow":
                e(type="tool_use", id="t2", name="Python", summary="Python(print(6 * 7))", input={"code": "print(6 * 7)"}, depth=0)
                e(type="tool_result", id="t2", name="Python", ok=True, summary="Ran for 0.1s", text="42", preview=["42"], diff=None, depth=0)
            else:
                e(type="tool_result", id="t2", name="Python", ok=False, summary="Denied", text="Permission denied.", preview=[], diff=None, depth=0)
        e(type="text", delta="Here is the demo answer, built from what the tools found. With a real model this comes from your cluster.")
        e(type="text_end")
        e(type="usage", prompt=900, completion=40, tok_s=11.4, seconds=3.1, context={"used": 940, "window": 8192, "pct": 11})
        e(type="done", text="demo")


def plugins_in(signature):
    return signature.split("|")[0].split(",")


def register(ctx, args):
    if getattr(ctx, "ai", None) is None:
        return
    ag = Agents(ctx, args)
    ctx.agents_api = ag

    def plugins(h, q):
        h._json({"ok": True, "plugins": ag.listing(h)})

    ctx.get_routes["/api/ai/plugins"] = plugins
    ctx.post_routes.update({"/api/ai/agent": ag.run, "/api/ai/agent/reply": ag.reply, "/api/ai/agent/stop": ag.stop, "/api/ai/agent/reset": ag.reset})
    ctx.post_limits["/api/ai/agent"] = 400000
