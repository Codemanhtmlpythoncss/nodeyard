"""Plugins: one list of the tool areas Nodeyard's AI and pages can use, with live health, on/off switches the server
enforces, and an audit log of what was done.

A plugin here is a description of a real feature (its routes, its clients, what it may change) plus a health check,
not a separate package: each feature keeps its own code. Turning one off makes the server refuse its routes (HTTP 403),
and the AI skills it provides disappear from chat; features marked "core" can't be turned off. Features that run in
the Mac app (Agent Browser, using the Mac) are listed for completeness; the app's own settings control them.

Every request to a plugin's routes that changes something (POST) is written to the audit log (audit.jsonl in the
dashboard's state folder, rotated at 5 MB): when, which plugin and route, who (signed-in page or API key), a short
summary of the request made only from known harmless fields, and the result. Request bodies, keys, passwords and file
contents are never logged.
"""
import json
import os
import threading
import time

AUDIT_MAX = 5 * 1024 * 1024
SAFE_FIELDS = ("action", "target", "model", "file", "name", "tool", "pod", "node", "id", "depth", "enabled", "idle_seconds",
               "load", "preference", "reset", "only", "path")


def _yardcode_tools():
    try:
        import webtools   # noqa: F401 -- puts yardcode on the path
        from yardcode.tools import web  # noqa: F401
        return True, "yardcode's web tools are installed next to nodeyard"
    except ImportError:
        return False, "yardcode isn't installed next to nodeyard on this server"


PLUGINS = [
    # id, name, description, clients, permission, toggle, routes (prefixes), skills
    ("web", "Web search and page reading", "Searches the web and reads public pages from this server's internet connection (private addresses are never fetched).",
     ["dashboard chat", "Mac app", "yardcode"], "read-only", True, ["/api/ai/web", "/api/v1/web/tool"], ["web"]),
    ("research", "Research Mode", "Plans searches, reads pages and writes a report citing only what was read.",
     ["dashboard", "Mac app (API)"], "read-only (uses a model)", True, ["/api/ai/research", "/api/v1/research"], []),
    ("knowledge", "Reference tools", "Wikipedia, arXiv, weather, a calculator and a visible task list for the chat's AI skills.",
     ["dashboard chat"], "read-only", True, [], ["wikipedia", "arxiv", "weather", "calculator", "tasks"]),
    ("files-code", "Python, files and shell", "Runs Python and edits files and runs commands in a work folder, as the terminal user (never root).",
     ["dashboard chat"], "asks before every change", True, ["/api/ai/run-code"], ["python", "files"]),
    ("terminal", "Terminal", "An interactive shell on this server as the terminal user, over Tailscale or your own network only.",
     ["dashboard"], "full shell (you type every command)", True, ["/api/term/"], []),
    ("devices", "Device connections", "Finds each machine's Wi-Fi/LAN and Tailscale addresses and checks them; per-device overrides.",
     ["dashboard", "Mac app"], "read-only checks; settings change only how devices are reached", True, ["/api/devices/"], []),
    ("models", "Model management", "Downloads, runs, switches, unloads and deletes models (split llama.cpp and Ollama), and automatic unloading.",
     ["dashboard", "Mac app", "yardcode"], "changes what runs (confirmations for deletes)", False, [], []),
    ("doctor", "Doctor", "Checks this cluster for common problems and applies fixes you choose.",
     ["dashboard", "Mac app", "CLI"], "fixes change system settings (each asks)", False, [], []),
    ("vision", "Images and OCR", "Sends attached images to models that can see (OCR, screenshots, diagrams).",
     ["dashboard chat", "Mac app"], "read-only", False, [], []),
    ("agent-browser", "Agent Browser", "A private browser window the AI can open, read, click and type in.",
     ["Mac app"], "asks before every click and text entry", False, [], []),
    ("computer-use", "Use this Mac", "Reads the frontmost app's visible text and clicks, types and opens apps through macOS Accessibility.",
     ["Mac app"], "asks before every action", False, [], []),
    ("software-install", "Software installation", "Installing packages on nodes from the AI or the pages.",
     [], "not available", False, [], []),
]
BY_ID = {p[0]: p for p in PLUGINS}


class PluginError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Registry:
    def __init__(self, ctx, args, state_dir):
        self.ctx = ctx
        self.args = args
        self.audit_path = os.path.join(state_dir, "audit.jsonl")
        self.lock = threading.Lock()

    def _prefs(self):
        s = getattr(self.ctx, "settings", None)
        return (s.prefs() if s else {}) or {}

    def enabled(self, pid):
        p = BY_ID.get(pid)
        if not p or not p[5]:
            return True
        return (self._prefs().get("plugins") or {}).get(pid, True) is not False

    def set_enabled(self, pid, on):
        p = BY_ID.get(pid)
        if not p:
            raise PluginError("No such plugin.", 404)
        if not p[5]:
            raise PluginError("%s is part of Nodeyard and can't be turned off here." % p[1])
        if not isinstance(on, bool):
            raise PluginError("Say on or off.")
        s = self.ctx.settings
        with s.lock:
            prefs = s.prefs()
            prefs.setdefault("plugins", {})[pid] = on
            s._save_prefs(prefs)

    def skill_enabled(self, skill):
        return all(self.enabled(p[0]) for p in PLUGINS if skill in p[7])

    def health(self, pid):
        """(status, detail): ok, warn, off, client (runs elsewhere), unavailable."""
        if not self.enabled(pid):
            return "off", "Turned off in Settings › Plugins."
        demo = bool(getattr(self.args, "demo", False))
        st = (self.ctx.store.snapshot().get("state") or {}) if getattr(self.ctx, "store", None) else {}
        if pid in ("web", "knowledge"):
            if demo:
                return "ok", "Demo: nothing is fetched from the internet."
            ok, detail = _yardcode_tools()
            return ("ok" if ok else "unavailable"), detail
        if pid == "research":
            ok, detail = (True, "") if demo else _yardcode_tools()
            if not ok or not self.enabled("web"):
                return "unavailable", detail or "Needs web search, which is turned off."
            ai = getattr(self.ctx, "ai", None)
            try:
                ready = [t for t in ai.targets().get("targets", []) if t.get("ready")] if ai else []
            except Exception:  # noqa: BLE001
                ready = []
            return ("ok", "%d model(s) ready to write reports" % len(ready)) if ready else ("warn", "No model is ready to write a report.")
        if pid == "files-code":
            user = getattr(self.args, "terminal_user", "") or ""
            if demo:
                return "ok", "Demo: nothing is run."
            if not user:
                return "unavailable", "Needs the terminal user (start the dashboard with sudo from your own account)."
            ok, detail = _yardcode_tools()
            return ("ok", "Runs as %s" % user) if ok else ("unavailable", detail)
        if pid == "terminal":
            user = getattr(self.args, "terminal_user", "") or ""
            if demo:
                return "ok", "Demo: no shell is started."
            return ("ok", "Shells run as %s" % user) if user else ("unavailable", "No terminal user is configured.")
        if pid == "devices":
            c = getattr(self.ctx, "connections", None)
            if not c:
                return "unavailable", "Device monitoring isn't running."
            view = c.view()
            bad = [d["name"] for d in view if d.get("status") in ("unreachable", "partial")]
            return ("warn", "Not answering: " + ", ".join(bad)) if bad else ("ok", "%d device(s) checked" % len(view))
        if pid == "models":
            ai = st.get("ai") or {}
            parts = []
            if ai.get("split"):
                parts.append("split model %s" % ("ready" if ai["split"].get("ready") else "not ready"))
            if ai.get("ollama"):
                parts.append("Ollama on %d node(s)" % len(ai["ollama"].get("pods", [])))
            return "ok", (", ".join(parts) or "No models deployed yet.")
        if pid == "doctor":
            return "ok", "nodeyard doctor"
        if pid == "vision":
            return "ok", "Works with models that accept images; others answer that they can't see them."
        if pid in ("agent-browser", "computer-use"):
            return "client", "Runs in Nodeyard AI for macOS; its settings there turn it on or off."
        return "unavailable", "Not built yet. Install software on a node yourself (SSH or the Terminal page)."

    def listing(self):
        out = []
        for pid, name, desc, clients, perm, toggle, routes, skills in PLUGINS:
            status, detail = self.health(pid)
            out.append({"id": pid, "name": name, "description": desc, "clients": clients, "permission": perm, "can_disable": toggle,
                        "enabled": self.enabled(pid), "status": status, "detail": detail, "routes": routes, "version": getattr(self.args, "nodeyard_version", "") or "main"})
        return out

    # -- audit ------------------------------------------------------------------------------------------------

    def audit(self, plugin, route, actor, summary, ok, code):
        entry = {"time": round(time.time(), 3), "plugin": plugin, "route": route, "actor": actor, "summary": summary, "ok": ok, "code": code}
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.audit_path), mode=0o700, exist_ok=True)
                if os.path.exists(self.audit_path) and os.path.getsize(self.audit_path) > AUDIT_MAX:
                    os.replace(self.audit_path, self.audit_path + ".1")
                fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass

    def recent(self, limit=200):
        try:
            with open(self.audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def summarise(body):
    if not isinstance(body, dict):
        return ""
    parts = []
    for k in SAFE_FIELDS:
        if k in body and isinstance(body[k], (str, int, float, bool)):
            parts.append("%s=%s" % (k, str(body[k])[:80]))
    if isinstance(body.get("question"), str):
        parts.append("question=%s" % body["question"][:80])
    return " ".join(parts)[:300]


def plugin_for(path):
    for p in PLUGINS:
        if any(path == r or path.startswith(r if r.endswith("/") else r + "/") or path == r for r in p[6]):
            return p[0]
    if path in ("/api/run", "/api/job/cancel", "/api/ai/ollama-load", "/api/ai/lifecycle", "/api/v1/lifecycle") or path.startswith("/api/v1/models/"):
        return "models"
    return None


def register(ctx, args):
    state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    if getattr(args, "demo", False):
        import tempfile
        state_dir = os.path.join(tempfile.gettempdir(), "nodeyard-demo-dashboard-%d" % os.getuid())
    reg = Registry(ctx, args, state_dir)
    ctx.plugins = reg
    if getattr(ctx, "connections", None) is not None:
        ctx.connections.active = lambda: reg.enabled("devices")

    def guard(path, fn, post):
        pid = plugin_for(path)
        if pid is None:
            return fn

        def route(h, arg):
            if not reg.enabled(pid):
                return h._json({"ok": False, "error": "%s is turned off in Settings › Plugins." % BY_ID[pid][1], "plugin": pid}, 403)
            if not post:
                return fn(h, arg)
            seen = {}
            real = h._json

            def spy(obj, code=200, *a, **k):
                seen.setdefault("code", code)
                seen.setdefault("ok", obj.get("ok") if isinstance(obj, dict) else None)
                return real(obj, code, *a, **k)
            h._json = spy
            actor = "api key" if getattr(h, "v1_bearer", False) else ("public access" if h._forwarded() else "signed-in page")
            try:
                return fn(h, arg)
            finally:
                h._json = real
                reg.audit(pid, path, actor, summarise(arg), seen.get("ok", True) if seen else True, seen.get("code", 200))
        return route

    for path in list(ctx.get_routes):
        ctx.get_routes[path] = guard(path, ctx.get_routes[path], False)
    for path in list(ctx.post_routes):
        ctx.post_routes[path] = guard(path, ctx.post_routes[path], True)

    def listing(h, q):
        h._json({"ok": True, "plugins": reg.listing()})

    def toggle(h, body):
        try:
            reg.set_enabled(str(body.get("id") or ""), body.get("enabled"))
            reg.audit("plugins", "/api/plugins", "api key" if getattr(h, "v1_bearer", False) else "signed-in page",
                      "id=%s enabled=%s" % (str(body.get("id"))[:40], body.get("enabled")), True, 200)
            h._json({"ok": True, "plugins": reg.listing()})
        except PluginError as e:
            h._json({"ok": False, "error": str(e)}, e.code)

    def audit(h, q):
        try:
            limit = max(1, min(1000, int(q.get("limit", ["200"])[0])))
        except ValueError:
            limit = 200
        h._json({"ok": True, "entries": reg.recent(limit)})

    ctx.get_routes.update({"/api/plugins": listing, "/api/v1/plugins": listing, "/api/audit": audit})
    ctx.post_routes["/api/plugins"] = toggle
    return reg
