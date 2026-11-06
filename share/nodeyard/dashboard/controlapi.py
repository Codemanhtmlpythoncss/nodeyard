"""The control API: see, load, unload and download models from any program, with the server API key.

    curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/models
    curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"model":"qwen2.5-coder-7b.gguf"}' http://DASHBOARD:9092/api/v1/models/load

Endpoints (all JSON):
  GET  /api/v1/status                    what is loaded and whether it is ready
  GET  /api/v1/targets                   chat-ready split and Ollama models
  GET  /api/v1/models                    OpenAI-compatible catalog, including unloaded installed models
  POST /api/v1/chat/completions          OpenAI-compatible chat; unloaded models load on demand
  POST /api/v1/models/load     {"model": "file.gguf" | "ollama-name", "ctx": 8192}   -> {"job": id}
  POST /api/v1/models/unload   {"model": optional name}                              -> {"job": id}
  POST /api/v1/models/download {"repo": "owner/name", "file": "x.gguf"}              -> {"job": id}
  GET  /api/v1/search?q=words            Hugging Face GGUF models
  GET  /api/v1/files?repo=owner/name     the GGUF files of one repo, with whether they fit
  GET  /api/v1/jobs?id=ID&since=N        progress lines of a task

The server API key (Authorization: Bearer ...) works for the control API and every model; a signed-in dashboard session works too. Nobody gets in
without one of them, from any network, because loading a model changes what the whole cluster runs. Deleting
models, running commands and changing settings are NOT in this API: those need the dashboard's password.
"""
import hmac
import os
import re
import secrets
import socket
import threading
import time

import aiapi
import aisearch
import turns

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
            h._json({"ok": False, "error": "That isn't this server's API key. It is under Settings > Server API key (not the dashboard password). If it works with the running model, choose Settings > Server API key > Use the running model's key to make it the shared key."}, 401)
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


def prepare_chat_target(backend, target, body, rows):
    """Start an unloaded split target, or switch a downloaded split file into service.

    Ollama targets are returned unchanged: Ollama loads an installed model when its
    OpenAI chat endpoint receives the request.
    """
    sp = split_state(backend) or {}
    if target == "split":
        if not sp:
            raise aiapi.AIError("No split model is deployed.", 404)
        if sp.get("ready") and not sp.get("stale"):
            return "split", ""
        if sp.get("loaded") and not sp.get("stale"):
            return "split", ""  # deployment is already loading; follow its live state
        if sp.get("stale"):
            file = str(sp.get("model") or "")
            if not file:
                raise aiapi.AIError("The split model needs reloading, but its file name is unknown.", 503)
            params = {"local": True, "file": file, "ctx": sp.get("ctx") or 8192, "keep_old": True}
            return "split", backend.run("switch", params)
        return "split", backend.run("split-load", {})

    if target.startswith("split:"):
        row = next((item for item in rows if item.get("id") == target and item.get("kind") == "split"), None)
        if row is None:
            raise aiapi.AIError("No downloaded split model matches %r." % target, 404)
        file = row["file"]
        if sp and sp.get("model") == file:
            if sp.get("ready") and not sp.get("stale"):
                return "split", ""
            if sp.get("loaded") and not sp.get("stale"):
                return "split", ""  # an in-progress load already owns the model slot
            if not sp.get("stale"):
                return "split", backend.run("split-load", {})
        ctx = body.get("ctx") or body.get("context_length") or (sp.get("ctx") if sp else None) or 8192
        return "split", backend.run("switch", {"local": True, "file": file, "ctx": ctx, "keep_old": True})
    return target, ""


LOAD_UPDATE_SECONDS = 30


def estimate_eta(samples, now, pct):
    """Estimate seconds remaining only from recent, increasing measured progress."""
    fresh = [(at, value) for at, value in samples if 0 <= now - at <= 120]
    if pct is None or len(fresh) < 2:
        return None
    start, first = fresh[0]
    elapsed = now - start
    rate = (float(pct) - float(first)) / elapsed if elapsed > 0 else 0.0
    if rate <= 0.001:
        return None
    return max(0, round((100.0 - float(pct)) / rate))


def loading_chunk(turn_id, model_id, backend, phase, pct, eta):
    """OpenAI-compatible empty delta with Nodeyard loading telemetry in an extension field."""
    return {"id": turn_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            "nodeyard": {"backend": backend, "stage": phase, "progress_pct": pct, "eta_seconds": eta,
                         "update_interval_seconds": LOAD_UPDATE_SECONDS}}


def wait_for_split_load(backend, model_id, job_id, turn, client_gone=lambda: False, timeout=1800,
                        clock=time.monotonic, pause=time.sleep):
    """Follow a split model load, emitting live OpenAI-shaped telemetry every 30 seconds."""
    started = clock()
    next_update = started
    samples = []
    while True:
        if client_gone():
            return False
        now = clock()
        state = (backend.store.snapshot().get("state") or {})
        sp = (state.get("ai") or {}).get("split") or {}
        if sp.get("ready") and not sp.get("stale"):
            turn.push(loading_chunk(turn.id, model_id, "clustered", "ready", 100, 0))
            return True
        if sp.get("stale"):
            raise aiapi.AIError("The split model needs reloading: %s." % sp["stale"].get("why", "a node lost its model share"), 503)
        if job_id:
            job = backend.job(job_id, 0)
            if job is None:
                raise aiapi.AIError("The split model load task disappeared before it was ready.", 502)
            if job.get("status") in ("failed", "cancelled"):
                detail = (job.get("lines") or ["The load task failed."])[-1]
                raise aiapi.AIError("The split model didn't load: %s" % detail, 502)
        load = sp.get("load") or {}
        raw_pct = load.get("pct")
        pct = float(raw_pct) if isinstance(raw_pct, (int, float)) and not isinstance(raw_pct, bool) else None
        if pct is not None and (not samples or pct != samples[-1][1]):
            samples.append((now, pct))
            samples = [(at, value) for at, value in samples if now - at <= 120]
        if now >= next_update:
            turn.push(loading_chunk(turn.id, model_id, "clustered", load.get("phase") or "starting",
                                    pct, estimate_eta(samples, now, pct)))
            next_update = now + LOAD_UPDATE_SECONDS
        if now - started >= timeout:
            raise aiapi.AIError("Timed out waiting for the split model to load.", 504)
        pause(1)


_OLLAMA_LOAD_HISTORY = {}
_OLLAMA_LOAD_HISTORY_LOCK = threading.Lock()


def ollama_loaded(backend, target):
    """Whether this installed Ollama target is in memory; None means status is unknown."""
    match = re.match(r"^ollama:([a-z0-9-]+):(.+)$", target or "")
    if not match:
        return None
    pod, name = match.groups()
    try:
        entries = backend.ollama_overview()
    except Exception:  # noqa: BLE001 -- status telemetry must not block a chat request
        return None
    entry = next((item for item in entries if item.get("pod") == pod), None)
    if entry is None:
        return None
    model = next((item for item in entry.get("models", []) if item.get("name") == name), None)
    return bool(model.get("loaded")) if model else None


def _ollama_eta(target, elapsed):
    with _OLLAMA_LOAD_HISTORY_LOCK:
        samples = list(_OLLAMA_LOAD_HISTORY.get(target, []))
    if not samples:
        return None
    return max(0, round(sum(samples) / len(samples) - elapsed))


def _remember_ollama_load(target, seconds):
    with _OLLAMA_LOAD_HISTORY_LOCK:
        samples = _OLLAMA_LOAD_HISTORY.setdefault(target, [])
        samples.append(max(0.0, seconds))
        del samples[:-5]


def watch_ollama_load(backend, target, turn, stop, clock=time.monotonic):
    """Report observable Ollama load state, with a learned ETA only after real samples."""
    started = clock()
    next_update = started
    while not stop.is_set():
        now = clock()
        loaded = ollama_loaded(backend, target)
        if loaded is True:
            _remember_ollama_load(target, now - started)
            turn.push(loading_chunk(turn.id, target, "ollama", "ready", 100, 0))
            return
        if now >= next_update:
            turn.push(loading_chunk(turn.id, target, "ollama", "loading" if loaded is False else "starting",
                                    None, _ollama_eta(target, now - started)))
            next_update = now + LOAD_UPDATE_SECONDS
        stop.wait(min(3, max(0.1, next_update - now)))


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

    def targets(h, q):
        try:
            h._json(dict(backend.targets(), ok=True))
        except aiapi.AIError as e:
            fail(h, e)

    def models(h, q):
        try:
            rows, downloads = describe(backend)
        except aiapi.AIError as e:
            return fail(h, e)
        try:
            available = backend.targets().get("targets", [])
        except aiapi.AIError:
            available = []
        target_by_id = {}
        deployed_files = set()
        for target in available:
            if not isinstance(target, dict):
                continue
            model_id = target.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            target_by_id.setdefault(model_id, target)
            if target.get("kind") == "split" and target.get("model"):
                deployed_files.add(target["model"])
        for row in rows:
            if row.get("kind") != "split" or row.get("file") in deployed_files:
                continue
            model_id = row.get("id")
            if isinstance(model_id, str) and model_id:
                target_by_id.setdefault(model_id, {"kind": "clustered", "ready": bool(row.get("ready")), "model": row.get("file", "")})
        ollama_inventory = {row.get("name"): row for row in rows if row.get("kind") == "ollama"}
        split = split_state(backend) or {}
        openai_models = []
        for model_id, target in target_by_id.items():
            kind = target.get("kind", "")
            if kind == "split":
                loaded = bool(split.get("loaded"))
                state = "stale" if target.get("stale") else "ready" if target.get("ready") else "loading" if loaded else "unloaded"
                backend_name = "clustered"
            elif kind == "ollama":
                inventory = ollama_inventory.get(target.get("name"))
                loaded = bool(inventory.get("loaded")) if inventory else None
                state = "loaded" if loaded else "unloaded" if inventory else "available"
                backend_name = "ollama"
            else:  # downloaded split files that are not the active deployment
                loaded, state, backend_name = False, "unloaded", "clustered"
            openai_models.append({"id": model_id, "object": "model", "created": 0, "owned_by": "nodeyard",
                                  "nodeyard": {"backend": backend_name, "ready": bool(target.get("ready")),
                                               "loaded": loaded, "state": state}})
        # Keep Nodeyard's inventory fields for existing clients while also satisfying
        # OpenAI-compatible clients (including Hermes) that read /models -> data[].
        h._json({"ok": True, "models": rows, "downloads": downloads,
                 "object": "list", "data": openai_models})

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

    def chat_completions(h, body):
        """Stream OpenAI-compatible chat through the shared-key control API."""
        target = str(body.get("model") or body.get("target") or "")[:240]
        messages = body.get("messages")
        if not target:
            return h._json({"ok": False, "error": "Choose a model from /api/v1/targets."}, 400)
        if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
            return h._json({"ok": False, "error": "Send between 1 and 200 messages."}, 400)
        try:
            clean = aiapi.clean_chat_messages(messages)
            temp = min(2.0, max(0.0, float(body.get("temperature", 0.7))))
            raw_max = body.get("max_tokens", 1024)
            max_tokens = 0 if raw_max in (None, 0, "0", "", "none") else min(65536, max(1, int(raw_max)))
        except OverflowError as e:
            return h._json({"ok": False, "error": str(e)}, 413)
        except (TypeError, ValueError) as e:
            return h._json({"ok": False, "error": str(e) or "Bad temperature or token limit."}, 400)
        search_ok = aisearch.allowed(ctx, body)
        if not search_ok:
            clean = aisearch.with_note(clean)
        requested_model = target
        load_job = ""
        wait_for_split = False
        if target == "split" or target.startswith("split:"):
            try:
                rows, _ = describe(backend)
                target, load_job = prepare_chat_target(backend, target, body, rows)
                split = split_state(backend) or {}
                wait_for_split = not (split.get("ready") and not split.get("stale"))
            except aiapi.AIError as e:
                return fail(h, e)
        payload = {"messages": clean, "temperature": temp, "stream": True}
        if body.get("return_progress") is True and target == "split":
            payload["return_progress"] = True   # llama.cpp's prompt-reading progress, for clients that ask
        if max_tokens:
            payload["max_tokens"] = max_tokens
        tools = body.get("tools")
        if tools is not None and not search_ok and isinstance(tools, list):   # (web search off: no browser search or page opening)
            tools = [t for t in tools if not (isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name") in aisearch.WEB_CHAT_TOOLS)] or None
        if tools is not None:
            allowed = aiapi.CHAT_TOOL_NAMES
            if not isinstance(tools, list) or not 1 <= len(tools) <= len(allowed):
                return h._json({"ok": False, "error": "Send between 1 and %d supported tools." % len(allowed)}, 400)
            clean_tools = []
            for item in tools:
                if not isinstance(item, dict):
                    return h._json({"ok": False, "error": "One of the requested tools is not supported."}, 400)
                fn = item.get("function")
                name = fn.get("name") if isinstance(fn, dict) else None
                description = fn.get("description", "") if isinstance(fn, dict) else None
                parameters = fn.get("parameters") if isinstance(fn, dict) else None
                if (item.get("type") != "function" or not isinstance(name, str) or name not in allowed or not isinstance(description, str)
                        or len(description) > 1200 or not isinstance(parameters, dict)):
                    return h._json({"ok": False, "error": "One of the requested tools is not supported."}, 400)
                clean_tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": parameters}})
            payload["tools"] = clean_tools
            payload["tool_choice"] = "auto"
        # Headers and a keep-alive go out at once, and then every 15 s while the model reads the prompt: the Mac app
        # gives up after 30 s without a byte, and a big prompt takes longer than that before the first word. The
        # model's own failures arrive as an error event in the stream. These clients don't resume, so when one goes
        # away the model is stopped straight away (it used to be, and nothing would read the rest).
        turn = turns.Turn(secrets.token_hex(8), "api", "api")
        box = {}

        def work():
            conn = None
            ollama_stop = None
            try:
                if wait_for_split and not wait_for_split_load(backend, requested_model, load_job, turn,
                                                               client_gone=lambda: box.get("gone", False)):
                    turn.finish("stopped", "The client left while the model was loading.")
                    return
                if target.startswith("ollama:") and ollama_loaded(backend, target) is not True:
                    ollama_stop = threading.Event()
                    threading.Thread(target=watch_ollama_load, args=(backend, target, turn, ollama_stop),
                                     name="ollama-load-progress", daemon=True).start()
                conn, resp = backend.open_chat(target, payload)
                box["conn"] = conn
                if box.get("gone"):
                    raise aiapi.AIError("The client went away.", 499)
                turn.finish(aiapi.pump_stream(turn, conn, resp, lambda: backend.cut_message(target) if hasattr(backend, "cut_message") else
                                              "The model stopped in the middle of its answer: the connection to it ended early."))
            except aiapi.AIError as e:
                turn.push({"error": {"message": str(e), "type": "model", "code": getattr(e, "code", 502)}})
                turn.finish("error", str(e))
            except Exception as e:  # noqa: BLE001 -- one failed answer must never take the dashboard down
                turn.push({"error": {"message": "The dashboard hit an error while answering: %s" % e, "type": "server"}})
                turn.finish("error", repr(e))
            finally:
                if ollama_stop is not None:
                    ollama_stop.set()
                if conn is not None:
                    conn.close()

        threading.Thread(target=work, name="api-chat", daemon=True).start()
        try:
            turns.send_headers(h, turn)
        except OSError:
            box["gone"] = True
            return
        if turn.follow(h, 0) == "gone":
            box["gone"] = True
            conn = box.get("conn")
            if conn is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)     # (llama.cpp stops working on it)
                except (OSError, AttributeError):
                    pass

    import webtools
    webtools.register(ctx, args)
    ctx.get_routes.update({PREFIX + "status": status, PREFIX + "targets": targets, PREFIX + "models": models, PREFIX + "search": search, PREFIX + "files": files, PREFIX + "jobs": job})
    ctx.post_routes.update({PREFIX + "models/load": load, PREFIX + "models/unload": unload, PREFIX + "models/download": download})
    ctx.post_routes[PREFIX + "chat/completions"] = chat_completions
    ctx.post_limits[PREFIX + "chat/completions"] = aiapi.MAX_CHAT_BODY_BYTES


_ = os
