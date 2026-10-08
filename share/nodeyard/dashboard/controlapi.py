"""The control API: see, load, unload and download models from any program, with the model's API key.

    curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/models
    curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"model":"qwen2.5-coder-7b.gguf"}' http://DASHBOARD:9092/api/v1/models/load

Endpoints (all JSON):
  GET  /api/v1/status                    what is loaded and whether it is ready
  GET  /api/v1/models                    downloaded models (and Ollama's), which one is loaded, running downloads
  POST /api/v1/models/load     {"model": "file.gguf" | "ollama-name", "ctx": 8192}   -> {"job": id}
  POST /api/v1/models/unload   {"model": optional name}                              -> {"job": id}
  POST /api/v1/models/download {"repo": "owner/name", "file": "x.gguf"}              -> {"job": id}
  GET  /api/v1/search?q=words            Hugging Face GGUF models
  GET  /api/v1/files?repo=owner/name     the GGUF files of one repo, with whether they fit
  GET  /api/v1/jobs?id=ID&since=N        progress lines of a task

The key is the model's API key (Authorization: Bearer ...); a signed-in dashboard session works too. Nobody gets in
without one of them, from any network, because loading a model changes what the whole cluster runs. Deleting
models, running commands and changing settings are NOT in this API: those need the dashboard's password.
"""
import hmac
import os
import re
import threading
import time

import aiapi

PREFIX = "/api/v1/"
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.gguf$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,120}$")
WINDOW, LIMIT = 300.0, 12


class KeyGate:
    """Checks the Authorization header against the server's API key, and slows down anyone who guesses."""

    def __init__(self, backend):
        self.backend = backend
        self.fails = {}
        self.lock = threading.Lock()

    def _ip(self, h):
        fwd = h._forwarded()
        return ("internet:" + fwd) if fwd else h.client_address[0]

    def __call__(self, h):
        """True when the request may go on; otherwise the answer has been sent."""
        ctx = h.ctx
        if ctx.auth is None:     # a password-less dashboard only answers on its own machine
            if not h._host_ok():
                h._json({"ok": False, "error": "This dashboard only answers on localhost."}, 403)
                return False
            return True
        auth = h.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
            key = self.backend.api_key() if hasattr(self.backend, "api_key") else self.backend.reveal_key()
            if not key:
                h._json({"ok": False, "error": "This server has no API key yet, so the control API is off. Make one in the dashboard (Settings > Server API key) or run: sudo nodeyard ai key --rotate"}, 403)
                return False
            ip = self._ip(h)
            now = time.time()
            with self.lock:
                self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < WINDOW]
                locked = len(self.fails[ip]) >= LIMIT
            if locked:
                h._json({"ok": False, "error": "Too many wrong keys. Try again in a few minutes."}, 429, {"Retry-After": "300"})
                return False
            if hmac.compare_digest(supplied.encode(), key.encode()):
                h.v1_bearer = True
                return True
            with self.lock:
                self.fails.setdefault(ip, []).append(now)
            time.sleep(0.3)
            h._json({"ok": False, "error": "That isn't this server's API key. It is the one under Settings > Server API key in the dashboard (not the dashboard password). If the model was deployed with its own key, set that same key there: Settings > Server API key > Or set your own."}, 401)
            return False
        if ctx.auth.valid(h._token()):   # the dashboard's own page, signed in
            return True
        h._json({"ok": False, "error": "Send the server's API key: Authorization: Bearer KEY (see Settings > Server API key)."}, 401, {"WWW-Authenticate": "Bearer"})
        return False


def split_state(backend):
    st = backend.store.snapshot()["state"] or {}
    return (st.get("ai") or {}).get("split")


def describe(backend):
    """Every model that could run: files downloaded on the nodes, Ollama's models, and what is loaded now."""
    sp = split_state(backend)
    models = []
    in_use = ""
    nodes_by_file = {}
    sizes = {}
    try:
        disk = backend.disk_models()
    except aiapi.AIError:
        disk = {"in_use": "", "nodes": [], "downloads": []}
    in_use = disk.get("in_use", "") or ""
    for n in disk.get("nodes", []):
        for it in n.get("items", []):
            if it.get("kind") == "model":
                nodes_by_file.setdefault(it["name"], []).append(n["node"])
                sizes[it["name"]] = max(sizes.get(it["name"], 0), int(it.get("bytes", 0)))
    running = (sp or {}).get("model", "")
    for f in sorted(nodes_by_file):
        on = bool(sp and sp.get("loaded") and f == running)
        models.append({"id": "split:" + f, "kind": "split", "name": (sp or {}).get("alias") if f == running and (sp or {}).get("alias") else f[:-5], "file": f,
                       "size": sizes[f], "nodes": nodes_by_file[f], "downloaded": True, "loaded": on, "active": f == (running or in_use),
                       "ready": bool(on and sp.get("ready"))})
    ollama = {}
    try:
        for e in backend.ollama_overview():
            for m in e.get("models", []):
                r = ollama.setdefault(m["name"], {"id": "ollama:" + m["name"], "kind": "ollama", "name": m["name"], "file": "", "size": m.get("size", 0), "nodes": [],
                                                 "downloaded": True, "loaded": False, "active": False, "ready": False, "loaded_on": [], "pods": {}})
                r["nodes"].append(e["node"])
                r["pods"][e["node"]] = e["pod"]
                if m.get("loaded"):
                    r["loaded"] = r["active"] = r["ready"] = True
                    r["loaded_on"].append(e["node"])
    except aiapi.AIError:
        pass
    models += [ollama[k] for k in sorted(ollama)]
    downloads = [{"file": d.get("file", ""), "node": d.get("node", ""), "state": d.get("state", ""), "size": d.get("size", 0), "got": d.get("got", 0),
                  "progress": d.get("progress", "")} for d in disk.get("downloads", []) if d.get("state") != "done"]
    return models, downloads


def register(ctx, args):
    backend = getattr(ctx, "ai", None)
    if backend is None:
        return
    gate = KeyGate(backend)
    ctx.v1_gate = gate

    def fail(h, e):
        h._json({"ok": False, "error": str(e)}, getattr(e, "code", 400) if isinstance(e, aiapi.AIError) else 500)

    def status(h, q):
        sp = split_state(backend)
        if not sp:
            model = {"state": "none", "alias": "", "file": "", "ctx": 0, "ready": False}
        else:
            state = "unloaded" if not sp.get("loaded") else ("serving" if sp.get("ready") else ("downloading" if sp.get("download") == "running" else "loading"))
            model = {"state": state, "alias": sp.get("alias", ""), "file": sp.get("model", ""), "ctx": int(sp.get("ctx") or 0), "ready": bool(sp.get("ready")),
                     "machines": [s.get("node") for s in sp.get("shares", [])]}
        st = backend.store.snapshot()["state"] or {}
        h._json({"ok": True, "model": model, "nodes": len(st.get("nodes", [])), "version": getattr(args, "nodeyard_version", "")})

    def models(h, q):
        try:
            rows, downloads = describe(backend)
        except aiapi.AIError as e:
            return fail(h, e)
        h._json({"ok": True, "models": rows, "downloads": downloads})

    def find(rows, wanted):
        wanted = str(wanted or "").strip()
        if not wanted:
            raise aiapi.AIError("Say which model: {\"model\": \"name or file\"}.")
        exact = [r for r in rows if wanted in (r["id"], r["file"], r["name"])]
        if len(exact) == 1:
            return exact[0]
        loose = [r for r in rows if wanted.lower() in (r["file"] or r["name"]).lower() or wanted.lower() in r["name"].lower()]
        if len(loose) == 1:
            return loose[0]
        if not (exact or loose):
            raise aiapi.AIError("No downloaded model matches %r. See /api/v1/models, or download one first." % wanted, 404)
        raise aiapi.AIError("%r matches several models: %s. Use the full file name." % (wanted, ", ".join((r["file"] or r["name"]) for r in (exact or loose)[:5])), 409)

    def load(h, body):
        try:
            rows, _ = describe(backend)
            row = find(rows, body.get("model"))
            if row["kind"] == "ollama":
                node = str(body.get("node") or "")
                if node and node not in row["pods"]:
                    raise aiapi.AIError("%s isn't on %s (it is on: %s)." % (row["name"], node, ", ".join(row["nodes"])), 404)
                node = node or row["nodes"][0]
                backend.ollama_load(row["pods"][node], row["name"], True)
                return h._json({"ok": True, "job": "", "message": "Loaded %s on Ollama (%s)." % (row["name"], node)})
            if row["loaded"]:
                return h._json({"ok": True, "job": "", "message": "%s is already %s." % (row["file"], "running" if row["ready"] else "loading")})
            params = {"local": True, "file": row["file"], "ctx": body.get("ctx") or 8192, "keep_old": True}
            if row["active"] and not row["loaded"]:
                job = backend.run("split-load", {})       # the same model, only unloaded
            else:
                job = backend.run("switch", params)
            h._json({"ok": True, "job": job, "message": "Switching to %s." % row["file"]})
        except aiapi.AIError as e:
            fail(h, e)

    def unload(h, body):
        try:
            wanted = body.get("model")
            if wanted:
                rows, _ = describe(backend)
                row = find(rows, wanted)
                if row["kind"] == "ollama":
                    for node in row["loaded_on"] or []:
                        backend.ollama_load(row["pods"][node], row["name"], False)
                    return h._json({"ok": True, "job": "", "message": "Unloaded %s from Ollama." % row["name"]})
            h._json({"ok": True, "job": backend.run("split-unload", {})})
        except aiapi.AIError as e:
            fail(h, e)

    def download(h, body):
        try:
            repo, file = str(body.get("repo", "")), str(body.get("file", ""))
            if not FILE_RE.match(file):
                raise aiapi.AIError("The file must be a .gguf file name.")
            h._json({"ok": True, "job": backend.run("download", {"repo": repo, "file": file})})
        except aiapi.AIError as e:
            fail(h, e)

    def search(h, q):
        try:
            limit = max(1, min(30, int(q.get("limit", ["12"])[0])))
        except ValueError:
            limit = 12
        try:
            h._json({"ok": True, "results": backend.hf.search(q.get("q", [""])[0], q.get("sort", ["downloads"])[0], limit)})
        except aiapi.AIError as e:
            fail(h, e)

    def files(h, q):
        try:
            h._json({"ok": True, "files": backend.hf.files(q.get("repo", [""])[0])})
        except aiapi.AIError as e:
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

    import webtools
    webtools.register(ctx, args)
    ctx.get_routes.update({PREFIX + "status": status, PREFIX + "models": models, PREFIX + "search": search, PREFIX + "files": files, PREFIX + "jobs": job})
    ctx.post_routes.update({PREFIX + "models/load": load, PREFIX + "models/unload": unload, PREFIX + "models/download": download})


_ = os
