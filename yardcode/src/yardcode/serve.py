"""The machine interface: events as JSON lines on stdout, commands as JSON lines on stdin.

Used by `yardcode -p ... --output-format stream-json` and by the nodeyard dashboard, which runs an agent per
conversation through `yardcode --serve-json`.

Out (one JSON object per line):
  ready {version, model, context, tools, mode}      text {delta}           thinking {delta}        text_end
  tool_use {id, name, summary, input, depth}         tool_result {id, name, ok, summary, text, preview, diff, depth}
  permission {id, tool, summary, reason, risk, suggest, command, diff}      -> wait for permission_reply
  ask {id, questions}                                 -> wait for ask_reply
  plan {id, plan}                                     -> wait for plan_reply
  todos {items}  usage {prompt, completion, tok_s, context}  info/warn/error {text}  compacting/compacted  mode {mode}  done {text}
In:
  {"type": "user", "text": "..."}          {"type": "interrupt"}              {"type": "quit"}
  {"type": "permission_reply", "id": "...", "decision": "allow|deny", "scope": "once|session|project", "feedback": ""}
  {"type": "ask_reply", "id": "...", "answers": {...}}      {"type": "plan_reply", "id": "...", "decision": "auto|ask|no"}
  {"type": "command", "name": "compact|clear|mode|max_tokens|model", "arg": "..."}
"""
import json
import queue
import sys
import threading
import uuid

from . import __version__, util
from .frontend import Frontend


class JsonFrontend(Frontend):
    def __init__(self, emit, interactive=True):
        self.emit = emit
        self.interactive = interactive
        self.waiters = {}
        self.lock = threading.Lock()
        self.agent = None

    def _wait(self, kind, payload):
        rid = uuid.uuid4().hex[:10]
        q = queue.Queue()
        with self.lock:
            self.waiters[rid] = q
        self.emit(dict(payload, type=kind, id=rid))
        try:
            return q.get(timeout=3600)
        except queue.Empty:
            return None
        finally:
            with self.lock:
                self.waiters.pop(rid, None)

    def reply(self, msg):
        with self.lock:
            q = self.waiters.get(msg.get("id"))
        if q is not None:
            q.put(msg)

    def on_text(self, delta):
        self.emit({"type": "text", "delta": delta})

    def on_thinking(self, delta):
        self.emit({"type": "thinking", "delta": delta})

    def end_text(self):
        self.emit({"type": "text_end"})

    def waiting(self, label, tokens=0):
        self.emit({"type": "waiting", "label": label})

    def progress(self, done, total, cached=0):
        self.emit({"type": "progress", "done": done, "total": total, "cached": cached})

    def tool_use(self, call_id, name, summary, args, depth=0):
        shown = {k: (v if not isinstance(v, str) or len(v) <= 2000 else v[:2000] + "…") for k, v in args.items() if not k.startswith("_")}
        self.emit({"type": "tool_use", "id": call_id, "name": name, "summary": summary, "input": shown, "depth": depth})

    def tool_progress(self, call_id, line):
        self.emit({"type": "tool_progress", "id": call_id, "line": line[:200]})

    def tool_result(self, call_id, name, result, depth=0):
        d = None
        if result.diff:
            from .ui import strip_ansi
            d = {"lines": [strip_ansi(l) for l in result.diff[0][:80]], "adds": result.diff[1], "dels": result.diff[2]}
        self.emit({"type": "tool_result", "id": call_id, "name": name, "ok": not result.error, "summary": result.summary,
                   "text": result.text[:6000], "preview": result.preview[:6], "diff": d, "depth": depth})

    def request_permission(self, tool, args, decision, summary):
        diff = None
        try:
            if tool.kind == "edit" and self.agent:
                d = tool.preview_diff(args, self.agent.ctx)
                if d:
                    from .ui import strip_ansi
                    diff = {"lines": [strip_ansi(l) for l in d[0][:80]], "adds": d[1], "dels": d[2]}
        except Exception:
            diff = None
        spec = tool.specifier(args, self.agent.ctx) if self.agent else ""
        r = self._wait("permission", {"tool": tool.name, "summary": summary, "reason": decision.reason, "risk": decision.risk, "suggest": decision.suggest,
                                      "command": args.get("command") if tool.name == "Bash" else (args.get("code") if tool.name == "Python" else None),
                                      "target": spec if tool.kind in ("edit", "read") else None, "diff": diff})
        if not r:
            return "deny", "once", "no answer"
        return r.get("decision", "deny"), r.get("scope", "once"), r.get("feedback", "")

    def todos(self, items):
        self.emit({"type": "todos", "items": items})

    def ask_user(self, questions):
        r = self._wait("ask", {"questions": questions})
        return (r or {}).get("answers")

    def approve_plan(self, plan):
        r = self._wait("plan", {"plan": plan})
        return (r or {}).get("decision", "no")

    def usage(self, comp, session, context):
        self.emit({"type": "usage", "prompt": comp.prompt_tokens, "completion": comp.completion_tokens, "tok_s": round(comp.tokens_per_second, 1),
                   "seconds": round(comp.seconds, 1), "context": context})

    def info(self, text):
        self.emit({"type": "info", "text": text})

    def warn(self, text):
        self.emit({"type": "warn", "text": text})

    def error(self, text):
        self.emit({"type": "error", "text": text})

    def compacting(self, label):
        self.emit({"type": "compacting", "label": label})

    def compacted(self, before, after, summary):
        self.emit({"type": "compacted", "before": before, "after": after})

    def mode_changed(self, mode):
        self.emit({"type": "mode", "mode": mode})

    def end_turn(self, final_text):
        self.emit({"type": "done", "text": final_text})


def make_emitter(stream=None):
    out = stream or sys.stdout
    lock = threading.Lock()

    def emit(obj):
        line = json.dumps(obj, ensure_ascii=False)
        with lock:
            try:
                out.write(line + "\n")
                out.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
    return emit


def serve(app, window):
    """Run until stdin closes: one conversation, driven by JSON commands."""
    emit = make_emitter()
    app.settings.data["web"] = dict(app.settings.data.get("web") or {}, via="local")   # (this IS the server: no detour)
    fe = JsonFrontend(emit, interactive=True)
    app.tui = fe
    ag = app.new_agent(window=window)
    ag.fe = fe
    ag.ctx.frontend = fe
    fe.agent = ag
    inbox = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            t = msg.get("type")
            if t == "interrupt":
                ag.interrupt()
            elif t in ("permission_reply", "ask_reply", "plan_reply"):
                fe.reply(msg)
            else:
                inbox.put(msg)
        inbox.put(None)
        ag.interrupt()

    threading.Thread(target=reader, daemon=True).start()
    emit({"type": "ready", "version": __version__, "model": app.client.model, "context": ag.context_window, "mode": ag.perms.mode,
          "tools": sorted(ag.tools), "session": app.session.id, "max_tokens": int(app.settings.get("max_tokens", 0) or 0)})
    while True:
        msg = inbox.get()
        if msg is None or msg.get("type") == "quit":
            break
        t = msg.get("type")
        try:
            if t == "user":
                ag.run(str(msg.get("text", "")))
            elif t == "command":
                command(app, ag, fe, msg)
        except Exception as e:  # keep the conversation alive
            emit({"type": "error", "text": "%s: %s" % (type(e).__name__, e)})
            emit({"type": "done", "text": ""})
    app.shutdown()


def command(app, ag, fe, msg):
    name, arg = msg.get("name"), msg.get("arg", "")
    if name == "compact":
        try:
            ag.maybe_compact(force=True, instructions=str(arg), reason="manual")
        except ValueError as e:
            fe.warn(str(e))
        fe.emit({"type": "done", "text": ""})
    elif name == "clear":
        window = ag.context_window
        ag = app.new_agent(window=window)
        ag.fe = fe
        ag.ctx.frontend = fe
        fe.agent = ag
        fe.info("New conversation.")
    elif name == "mode":
        if arg in ("default", "acceptEdits", "plan"):
            ag.perms.mode = arg
            fe.mode_changed(arg)
    elif name == "max_tokens":
        try:
            app.settings.overrides["max_tokens"] = max(0, int(arg))
            app.settings.data["max_tokens"] = max(0, int(arg))
        except (TypeError, ValueError):
            pass
    elif name == "model":
        app.client.model = str(arg)
    elif name == "compact_settings":
        app.settings.data["compact"] = dict(app.settings.data.get("compact") or {}, **(arg if isinstance(arg, dict) else {}))


_ = util
