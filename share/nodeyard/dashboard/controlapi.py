"""The control API: see, load, unload and download models from any program, with the server API key.

    curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/models
    curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"model":"qwen2.5-coder-7b.gguf"}' http://DASHBOARD:9092/api/v1/models/load

Endpoints (all JSON):
  GET  /api/v1/status                    what is loaded and whether it is ready
  GET  /api/v1/targets                   chat-ready split and Ollama models
  GET  /api/v1/models                    OpenAI-compatible catalog, including unloaded installed models
  POST /api/v1/chat/completions          OpenAI-compatible chat; JSON by default, SSE with stream=true
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
import http.client
import inspect
import json
import os
import re
import secrets
import select
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
MAX_OPENAI_EVENT_BYTES = 1 << 20


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


def _split_base_name(file):
    """A split model's real, selectable name: its file name without the .gguf suffix."""
    base = os.path.basename(str(file or ""))
    return base[:-5] if base.endswith(".gguf") else base


def model_display_id(internal_id, kind, name, model_file):
    """The id an OpenAI client (Hermes, etc.) sees for a model. Split models are shown by their
    real name — the deployment's alias, or a downloaded file's base name — never the bare sentinel
    "split", which is useless in a model picker. Ollama ids (and anything else) pass through."""
    if kind not in ("split", "clustered"):
        return internal_id
    token = str(name or "").strip()
    if not token or " " in token:          # "split model" or blank -> use the file name instead
        token = _split_base_name(model_file)
    return token or internal_id


def resolve_model_id(backend, requested):
    """Map the id an OpenAI client sends back onto an internal chat target. A real split model name
    (the deployed alias, or a downloaded file's name) resolves to the split deployment or that file;
    "split", "split:<file>" and "ollama:*" are already internal and pass through unchanged, so older
    clients keep working."""
    wanted = str(requested or "").strip()
    if not wanted or wanted == "split" or wanted.startswith("split:") or wanted.startswith("ollama:"):
        return wanted
    sp = split_state(backend) or {}
    if sp and wanted == str(sp.get("alias") or "").strip():
        return "split"
    try:
        rows, _ = describe(backend)
    except aiapi.AIError:
        rows = []
    for r in rows:
        if r.get("kind") == "split" and wanted in (r.get("name"), r.get("file"), _split_base_name(r.get("file"))):
            return r.get("id") or wanted
    return wanted


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


def collect_openai_stream(conn, resp, requested_model):
    """Collect an upstream OpenAI SSE stream into one Chat Completions response."""
    choices = {}
    response_id = ""
    created = None
    model = requested_model
    usage = None
    total_bytes = 0
    done = False

    def choice_at(index):
        return choices.setdefault(index, {"content": [], "tool_calls": {}, "finish_reason": None, "extra": {}, "role": "assistant"})

    while True:
        try:
            line = resp.readline(MAX_OPENAI_EVENT_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError):
            line = b""
        if len(line) > MAX_OPENAI_EVENT_BYTES:
            raise aiapi.AIError("The model returned an SSE event larger than the 1 MiB event limit.", 413)
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == b"[DONE]":
            done = True
            break
        total_bytes += len(data)
        if total_bytes > 8 << 20:
            raise aiapi.AIError("The model's completion exceeded the 8 MiB response limit.", 413)
        try:
            chunk = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise aiapi.AIError("The model returned an invalid OpenAI stream chunk.", 502)
        if not isinstance(chunk, dict):
            raise aiapi.AIError("The model returned an invalid OpenAI stream chunk.", 502)
        if isinstance(chunk.get("error"), dict):
            detail = chunk["error"].get("message") or "The model returned an error."
            code = chunk["error"].get("code")
            raise aiapi.AIError(str(detail), code if isinstance(code, int) and not isinstance(code, bool) else 502)
        response_id = response_id or str(chunk.get("id") or "")
        created = created if created is not None else chunk.get("created")
        model = str(chunk.get("model") or model)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for item in chunk.get("choices") or []:
            if not isinstance(item, dict):
                continue
            try:
                index = max(0, int(item.get("index", 0)))
            except (TypeError, ValueError):
                index = 0
            out = choice_at(index)
            delta = item.get("delta") or item.get("message") or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("role"), str):
                out["role"] = delta["role"]
            content = delta.get("content")
            if isinstance(content, str):
                out["content"].append(content)
            elif isinstance(content, list):
                out["extra"].setdefault("content_parts", []).extend(content)
            if isinstance(delta.get("refusal"), str):
                out["extra"].setdefault("refusal", []).append(delta["refusal"])
            legacy = delta.get("function_call")
            if isinstance(legacy, dict):
                fn = out["extra"].setdefault("function_call", {"name": "", "arguments": ""})
                if isinstance(legacy.get("name"), str):
                    fn["name"] += legacy["name"]
                if isinstance(legacy.get("arguments"), str):
                    fn["arguments"] += legacy["arguments"]
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                try:
                    call_index = max(0, int(call.get("index", 0)))
                except (TypeError, ValueError):
                    call_index = 0
                tool = out["tool_calls"].setdefault(call_index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if isinstance(call.get("id"), str):
                    tool["id"] = call["id"]
                if isinstance(call.get("type"), str):
                    tool["type"] = call["type"]
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                if isinstance(function.get("name"), str):
                    tool["function"]["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    tool["function"]["arguments"] += function["arguments"]
            for key, value in delta.items():
                if key not in ("role", "content", "refusal", "function_call", "tool_calls"):
                    out["extra"][key] = value
            if item.get("finish_reason") is not None:
                out["finish_reason"] = item["finish_reason"]
    if not done:
        status = "The model stopped before the completion was finished."
        if getattr(conn, "nodeyard_stopped", False):
            status = "The model stopped before the completion was finished."
        raise aiapi.AIError(status, 502)
    if not choices:
        raise aiapi.AIError("The model returned no completion choices.", 502)
    result_choices = []
    for index in sorted(choices):
        out = choices[index]
        message = {"role": out["role"], "content": "".join(out["content"]) or None}
        if out["tool_calls"]:
            message["tool_calls"] = [out["tool_calls"][key] for key in sorted(out["tool_calls"])]
        if out["extra"].get("function_call"):
            message["function_call"] = out["extra"].pop("function_call")
        if out["extra"].get("refusal"):
            message["refusal"] = "".join(out["extra"].pop("refusal"))
        if out["extra"].get("content_parts"):
            message["content"] = out["extra"].pop("content_parts")
        message.update(out["extra"])
        result_choices.append({"index": index, "message": message, "finish_reason": out["finish_reason"] or "stop"})
    result = {"id": response_id or "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion",
              "created": int(created) if isinstance(created, (int, float)) else int(time.time()),
              "model": model, "choices": result_choices}
    if usage is not None:
        result["usage"] = usage
    return result


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


def openai_error(handler, message, code=400):
    """Return an OpenAI error envelope while retaining Nodeyard's legacy ok field."""
    status = code if isinstance(code, int) and 400 <= code <= 599 else 502
    kind = "invalid_request_error" if status < 500 else "server_error"
    text = str(message)
    return handler._json({"error": {"message": text, "type": kind, "code": code}, "ok": False,
                          "nodeyard_error": text}, status)


def client_disconnected(handler):
    """Best-effort non-consuming check for EOF on the request socket."""
    sock = getattr(handler, "connection", None) or getattr(handler, "request", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, ValueError):
        # TLS socket wrappers may not support MSG_PEEK; don't mistake that for a disconnect.
        return False
    except OSError:
        return True


def chat_nonstream(handler, backend, target, requested_model, payload, wait_for_split, load_job):
    """Handle an OpenAI non-stream request while upstream model services stream internally."""
    class SilentTurn:
        id = secrets.token_hex(8)

        @staticmethod
        def push(_event):
            pass

    conn = None
    monitor_stop = threading.Event()
    client_gone = threading.Event()
    monitor = None
    turn = SilentTurn()

    def disconnected():
        return client_gone.is_set() or client_disconnected(handler)

    def watch_client(upstream):
        while not monitor_stop.wait(0.1):
            if client_disconnected(handler):
                client_gone.set()
                try:
                    upstream.nodeyard_stopped = True
                except AttributeError:
                    pass
                try:
                    upstream_sock = getattr(upstream, "sock", None)
                    if upstream_sock is not None:
                        upstream_sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                try:
                    upstream.close()
                except OSError:
                    pass
                return

    def start_monitor(upstream):
        nonlocal monitor
        if monitor is None:
            monitor = threading.Thread(target=watch_client, args=(upstream,), name="api-chat-client-watch", daemon=True)
            monitor.start()

    try:
        if wait_for_split and not wait_for_split_load(backend, requested_model, load_job, turn, client_gone=disconnected):
            return None
        if disconnected():
            return None
        try:
            supports_on_conn = "on_conn" in inspect.signature(backend.open_chat).parameters
        except (TypeError, ValueError):
            supports_on_conn = False
        if supports_on_conn:
            conn, resp = backend.open_chat(target, payload, on_conn=start_monitor)
        else:
            conn, resp = backend.open_chat(target, payload)
            start_monitor(conn)
        if monitor is None:
            start_monitor(conn)
        completion = collect_openai_stream(conn, resp, requested_model)
        if disconnected():
            return None
        return handler._json(completion)
    except aiapi.AIError as error:
        if disconnected():
            return None
        return openai_error(handler, str(error), getattr(error, "code", 502))
    except Exception as error:  # noqa: BLE001 -- one failed completion must not take down the dashboard
        if disconnected():
            return None
        return openai_error(handler, "The dashboard hit an error while answering: %s" % error, 500)
    finally:
        monitor_stop.set()
        if monitor is not None:
            monitor.join(timeout=0.25)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


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
            display_id = model_display_id(model_id, kind, target.get("name"), target.get("model"))
            openai_models.append({"id": display_id, "object": "model", "created": 0, "owned_by": "nodeyard",
                                  "nodeyard": {"backend": backend_name, "ready": bool(target.get("ready")),
                                               "loaded": loaded, "state": state, "target": model_id}})
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
        """OpenAI-compatible chat through the shared-key control API."""
        requested_id = str(body.get("model") or body.get("target") or "")[:240]
        target = resolve_model_id(backend, requested_id)
        messages = body.get("messages")
        if not target:
            return openai_error(h, "Choose a model from /api/v1/models.", 400)
        if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
            return openai_error(h, "Send between 1 and 200 messages.", 400)
        streaming = body.get("stream", False)
        if not isinstance(streaming, bool):
            return openai_error(h, "stream must be true or false.", 400)
        raw_n = body.get("n", 1)
        if isinstance(raw_n, bool) or not isinstance(raw_n, int) or raw_n != 1:
            return openai_error(h, "This gateway supports one completion choice per request (n=1).", 400)
        if body.get("logprobs") or body.get("top_logprobs") not in (None, 0):
            return openai_error(h, "This model gateway does not provide token log probabilities.", 400)
        if body.get("logit_bias") not in (None, {}):
            return openai_error(h, "This model gateway does not support logit_bias.", 400)
        try:
            clean = aiapi.clean_chat_messages(messages, allow_custom_tools=True)
            raw_temp = body.get("temperature", 1.0)
            if isinstance(raw_temp, bool):
                raise ValueError("temperature must be a number between 0 and 2.")
            temp = float(raw_temp)
            if not 0.0 <= temp <= 2.0:
                raise ValueError("temperature must be between 0 and 2.")
            if "max_tokens" in body and "max_completion_tokens" in body:
                raise ValueError("Send max_tokens or max_completion_tokens, not both.")
            raw_max = body.get("max_completion_tokens", body.get("max_tokens", 1024))
            if raw_max in (None, 0, "0", "", "none") and not isinstance(raw_max, bool):
                max_tokens = 0
            elif isinstance(raw_max, int) and not isinstance(raw_max, bool):
                max_tokens = raw_max
            elif isinstance(raw_max, str) and raw_max.isdigit():
                max_tokens = int(raw_max)
            else:
                raise ValueError("The token limit must be a whole number.")
            if max_tokens < 0 or max_tokens > 65536:
                raise ValueError("The token limit must be between 1 and 65536, or zero for no limit.")
            options = {}
            for key, low, high in (("top_p", 0.0, 1.0), ("presence_penalty", -2.0, 2.0), ("frequency_penalty", -2.0, 2.0)):
                if key in body:
                    value = body[key]
                    if isinstance(value, bool):
                        raise ValueError("%s must be a number between %s and %s." % (key, low, high))
                    value = float(value)
                    if not low <= value <= high:
                        raise ValueError("%s must be between %s and %s." % (key, low, high))
                    options[key] = value
            if "stop" in body:
                stop = body["stop"]
                if isinstance(stop, str):
                    if len(stop) > 256:
                        raise ValueError("Each stop sequence is limited to 256 characters.")
                elif isinstance(stop, list) and 1 <= len(stop) <= 4 and all(isinstance(item, str) and len(item) <= 256 for item in stop):
                    pass
                else:
                    raise ValueError("stop must be a string or a list of up to four strings.")
                options["stop"] = stop
            if "seed" in body:
                seed = body["seed"]
                if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2147483647:
                    raise ValueError("seed must be an integer between 0 and 2147483647.")
                options["seed"] = seed
            if "user" in body:
                user = body["user"]
                if not isinstance(user, str) or len(user) > 128:
                    raise ValueError("user must be a string of at most 128 characters.")
                options["user"] = user
            if "response_format" in body:
                response_format = body["response_format"]
                if not isinstance(response_format, dict) or response_format.get("type") not in ("text", "json_object", "json_schema"):
                    raise ValueError("response_format must use text, json_object, or json_schema.")
                options["response_format"] = response_format
        except OverflowError as e:
            return openai_error(h, str(e), 413)
        except (TypeError, ValueError) as e:
            return openai_error(h, str(e) or "Bad temperature or token limit.", 400)

        search_ok = aisearch.allowed(ctx, body)
        if not search_ok:
            clean = aisearch.with_note(clean)
        requested_model = requested_id or target   # echo back what the client asked for (OpenAI convention)
        load_job = ""
        wait_for_split = False
        payload = {"messages": clean, "temperature": temp, "stream": True}
        payload.update(options)
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if body.get("n") not in (None, 1):
            return openai_error(h, "This gateway supports one completion choice per request (n=1).", 400)
        tools = body.get("tools")
        if tools is None and isinstance(body.get("functions"), list):
            tools = [{"type": "function", "function": fn} for fn in body["functions"]]
        tool_choice = body.get("tool_choice", body.get("function_call", "auto"))
        if isinstance(tool_choice, dict) and "name" in tool_choice and "function" not in tool_choice:
            tool_choice = {"type": "function", "function": {"name": tool_choice["name"]}}
        if tools is not None:
            if not isinstance(tools, list) or not 1 <= len(tools) <= 64:
                return openai_error(h, "Send between 1 and 64 function tools.", 400)
            clean_tools, tool_names = [], set()
            for item in tools:
                if not isinstance(item, dict) or item.get("type") != "function" or not isinstance(item.get("function"), dict):
                    return openai_error(h, "Only function tools are supported by this gateway.", 400)
                fn = item["function"]
                name = fn.get("name")
                description = fn.get("description", "")
                parameters = fn["parameters"] if "parameters" in fn else {"type": "object", "properties": {}}
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                        or not isinstance(description, str) or len(description) > 4096 or not isinstance(parameters, dict)):
                    return openai_error(h, "A function tool has an invalid name, description or parameters schema.", 400)
                clean_fn = {"name": name, "description": description, "parameters": parameters}
                if isinstance(fn.get("strict"), bool):
                    clean_fn["strict"] = fn["strict"]
                clean_tools.append({"type": "function", "function": clean_fn})
                tool_names.add(name)
            if len(json.dumps(clean_tools, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
                return openai_error(h, "The function tool schemas exceed the 128 KiB limit.", 413)
            if not search_ok:
                clean_tools = [tool for tool in clean_tools if tool["function"]["name"] not in aisearch.WEB_CHAT_TOOLS]
                tool_names = {tool["function"]["name"] for tool in clean_tools}
            if clean_tools:
                if tool_choice not in ("auto", "none", "required"):
                    chosen = tool_choice.get("function", {}).get("name") if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict) else None
                    if not isinstance(chosen, str) or tool_choice.get("type") != "function" or chosen not in tool_names:
                        return openai_error(h, "tool_choice must be auto, none, required, or a submitted function name.", 400)
                payload["tools"] = clean_tools
                payload["tool_choice"] = tool_choice
                if "parallel_tool_calls" in body:
                    if not isinstance(body["parallel_tool_calls"], bool):
                        return openai_error(h, "parallel_tool_calls must be true or false.", 400)
                    payload["parallel_tool_calls"] = body["parallel_tool_calls"]
            elif tool_choice not in ("auto", "none"):
                return openai_error(h, "The requested tool_choice was removed by the web-search permission filter.", 403)
        elif tool_choice not in (None, "auto", "none"):
            return openai_error(h, "tool_choice requires function tools.", 400)
        for key in ("logprobs", "top_logprobs", "logit_bias", "service_tier", "store"):
            if key in body and body[key] not in (None, False, 0, {}):
                return openai_error(h, "%s is not supported by this model gateway." % key, 400)
        if target == "split" or target.startswith("split:"):
            try:
                rows, _ = describe(backend)
                target, load_job = prepare_chat_target(backend, target, body, rows)
                split = split_state(backend) or {}
                wait_for_split = not (split.get("ready") and not split.get("stale"))
            except aiapi.AIError as e:
                return openai_error(h, str(e), getattr(e, "code", 502))
        if streaming and body.get("return_progress") is True and target == "split":
            payload["return_progress"] = True   # llama.cpp's prompt-reading progress, for streaming clients
        # OpenAI Chat Completions defaults to JSON unless the caller explicitly requests streaming.
        if not streaming:
            return chat_nonstream(h, backend, target, requested_model, payload, wait_for_split, load_job)
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
