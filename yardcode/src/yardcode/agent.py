"""The agent loop: send the conversation to the model, run the tools it asks for (with permission), repeat until it answers.

It handles native tool calls and the text fallback, permission rules and hooks, interruption, automatic context
compression, and sub-agents (the Task tool).
"""
import json
import os
import platform
import re
import subprocess
import threading
import time

from . import compact, hooks, memory, util
from .client import APIError, Cancelled, parse_text_tool_calls
from .frontend import Frontend
from .perms import Permissions
from .session import Session
from .tools.base import Context, Result, Tool, ToolError, need, parse_args

MAX_TURNS_DEFAULT = 60

SYSTEM = """You are yardcode, a coding agent running in the user's terminal. You help with software engineering, system administration and research by using tools: reading and editing files, running commands, searching code and the web.

# How to work
- Be concise and direct; your answer is shown in a terminal (markdown is fine). No filler or flattery.
- Find out facts with tools instead of guessing. Read a file before you edit it. Prefer Edit over Write for existing files, and Grep/Glob/LS over shell find/grep/ls.
- For work with several steps, keep a list with TodoWrite and update it as you go.
- Make the smallest change that solves the problem, match the existing style, and don't add things nobody asked for.
- After changing code, check it: run the tests, linter or build if you can tell how.
- Don't commit, push or delete things unless asked. Commands run on the user's real machine; the user approves risky ones. If something is denied, don't retry it: adapt or ask.
- For current facts, news or documentation use WebSearch, then WebFetch the best results, and cite the addresses you used.
- If you really need a decision or missing information, use AskUserQuestion.
- When you finish, say what you did and anything the user must do next, in a few lines."""

SYSTEM_SMALL = """You are yardcode, a coding agent in the user's terminal. Use your tools to look things up and to read, edit and run code; don't guess. Read a file before you edit it. Be brief. For weather use Weather; for other current facts use WebSearch then WebFetch, and name the addresses you used. If a tool is denied, adapt or ask. When done, say what you did in a few lines."""

SUBAGENT_SYSTEM = """You are a sub-agent of yardcode, started to do one focused job and report back. Use your tools to do it thoroughly, then finish with a clear, complete report: your last message is the only thing the main agent sees. Be factual and include file paths, line numbers and addresses where they matter."""

TEXT_TOOLS_NOTE = """# Tools (text format)
You can't call tools natively here, so write each call as:
<tool_call>
{"name": "ToolName", "arguments": {"arg": "value"}}
</tool_call>
Then stop and wait: the result comes back in the next message inside <tool_response>. Make one call at a time. When you are done, answer normally without a tool_call.
Available tools:
%s"""


def git_summary(cwd):
    try:
        def git(*a):
            return subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True, timeout=3).stdout.strip()
        if git("rev-parse", "--is-inside-work-tree") != "true":
            return ""
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        changed = len([l for l in git("status", "--short").split("\n") if l.strip()])
        last = git("log", "-1", "--format=%h %s")
        return "Git: branch %s, %d changed file%s; last commit: %s" % (branch, changed, "" if changed == 1 else "s", last[:80])
    except (OSError, subprocess.SubprocessError):
        return ""


class Agent:
    def __init__(self, settings, client, frontend=None, session=None, ctx=None, tools=None, perms=None, depth=0, system_extra="", max_turns=0, allow_names=None):
        self.settings = settings
        self.client = client
        self.fe = frontend or Frontend()
        self.session = session or Session(os.getcwd(), persist=False)
        self.ctx = ctx or Context(settings, frontend=self.fe)
        self.ctx.frontend = self.fe
        self.ctx.agent = self
        self.ctx.depth = depth
        self.stop = threading.Event()
        self.ctx.abort = self.stop
        self.ctx.checkpoint = self.session.checkpoint_file if depth == 0 else None
        self.perms = perms or Permissions(settings, self.ctx, interactive=self.fe.interactive)
        self.depth = depth
        self.system_extra = system_extra
        self.max_turns = max_turns or int(settings.get("max_turns", 0) or 0) or (25 if depth else MAX_TURNS_DEFAULT)
        self.tools = {}
        for t in tools or []:
            if allow_names is None or t.name in allow_names:
                self.tools[t.name] = t
        self.context_window = int(settings.get("context_window", 0) or 0)
        self.last_prompt_tokens = 0
        self.prompt_len_at_usage = 0
        self.text_mode = settings.get("tool_mode", "auto") == "text"
        self.recent_fail = {}
        self.last_text = ""
        self.memory_text, self.memory_info = memory.load(self.ctx.cwd)
        self._system_cache = None
        self._system_key = None
        self._tools_cache = None
        self._tools_key = None
        self.turns_in_run = 0

    # ---- prompt ------------------------------------------------------------------------------------------
    def tier(self):
        """How much of the tool box goes into the prompt: full, lean (rare tools left out) or tiny (essentials only).
        Reading the prompt costs seconds per hundred tokens on a CPU cluster, so a small context gets a small prompt."""
        prof = self.settings.get("tools.profile", "auto")
        if prof in ("full", "lean", "tiny"):
            return prof
        w = self.context_window
        if self.depth == 0 and w:
            return "tiny" if w < 12000 else ("lean" if w < 20000 else "full")
        return "full"

    def system_prompt(self):
        """The same text every time (so a model server can keep its cache of it between conversations): anything that
        changes (date, folder, git state) goes into the first message instead, see environment()."""
        key = (self.tier(), self.text_mode)
        if self._system_cache is not None and self._system_key == key:
            return self._system_cache
        parts = [SUBAGENT_SYSTEM if self.depth else (SYSTEM_SMALL if key[0] == "tiny" else SYSTEM)]
        if self.system_extra:
            parts.append(self.system_extra)
        if self.memory_text and not self.depth:
            parts.append("# Instructions from the user's files (follow them)\n" + self.memory_text)
        if self.settings.get("system_prompt_extra"):
            parts.append(self.settings.get("system_prompt_extra"))
        if self.text_mode:
            parts.append(TEXT_TOOLS_NOTE % "\n".join("- %s: %s | args: %s" % (t.name, t.description, json.dumps(t.parameters.get("properties", {}))) for t in self.active_tools()))
        self._system_cache = "\n\n".join(parts)
        self._system_key = key
        return self._system_cache

    def environment(self):
        """Facts that change between conversations, sent once with the first message."""
        env = ["<environment>", "Working directory: %s" % self.ctx.cwd, "Platform: %s %s (%s)" % (platform.system(), platform.release(), platform.machine()),
               "Today: %s" % time.strftime("%A %d %B %Y")]
        g = git_summary(self.ctx.cwd)
        if g:
            env.append(g)
        env.append("</environment>")
        return "\n".join(env)

    def refresh_prompt(self):
        self._system_cache = None
        self._tools_cache = None
        self.memory_text, self.memory_info = memory.load(self.ctx.cwd)

    LEAN_HIDDEN = {"Arxiv", "Wikipedia", "FileSearch", "Memory", "BashOutput", "KillShell", "MultiEdit"}
    TINY_KEEP = {"Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch", "WebFetch", "Weather"}

    def lean(self):
        return self.tier() != "full"

    def active_tools(self):
        t = self.tier()
        if t == "tiny":
            return [x for n, x in self.tools.items() if n in self.TINY_KEEP or n.startswith("mcp__") or getattr(x, "source", None)]
        if t == "lean":
            return [x for n, x in self.tools.items() if n not in self.LEAN_HIDDEN]
        return list(self.tools.values())

    def schemas(self):
        key = (self.text_mode, self.tier())
        if self._tools_cache is None or self._tools_key != key:
            self._tools_key = key
            self._tools_cache = [] if self.text_mode else [t.schema() for t in self.active_tools()]
        return self._tools_cache

    def overhead_tokens(self):
        return util.est_tokens(self.system_prompt()) + util.est_tokens(json.dumps(self.schemas()))

    def build_messages(self):
        return [{"role": "system", "content": self.system_prompt()}] + self.session.api_messages()

    # ---- context size ------------------------------------------------------------------------------------
    def context_used(self):
        msgs = self.session.messages
        if self.last_prompt_tokens and self.prompt_len_at_usage <= len(msgs):
            return self.last_prompt_tokens + compact.estimate(msgs[self.prompt_len_at_usage:])
        return self.overhead_tokens() + compact.estimate(msgs)

    def maybe_compact(self, force=False, instructions="", reason="auto"):
        cfg = self.settings.get("compact") or {}
        if not force and not cfg.get("auto", True):
            return False
        window = self.context_window
        if not force and not window:
            return False
        threshold = float(cfg.get("threshold", 0.8))
        used = self.context_used()
        if not force and used < window * threshold:
            return False
        keep = int(cfg.get("keep_turns", 3))
        # 1. cheap: shorten old tool output
        new, saved = compact.prune(self.session.messages, int(cfg.get("prune_after", 6)) if not force else keep, int(cfg.get("prune_chars", 600)))
        if saved > 0:
            self.session.reset(new)
            self.last_prompt_tokens = 0
            self.fe.info("Trimmed old tool output to free about %s tokens." % util.human_tokens(saved // 4))
            used = self.context_used()
            if not force and used < window * threshold * 0.9:
                return True
        if not force and used < window * threshold:
            return True
        # 2. summarize the earlier conversation
        res = hooks.run_hooks(self.settings, "PreCompact", {"reason": reason}, self.ctx.cwd, session_id=self.session.id)
        _ = res
        self.fe.compacting("Compressing the conversation")
        try:
            new, summary, before, after = compact.compact(self.client, self.session.messages, window, keep, instructions,
                                                          on_progress=lambda s: self.fe.compacting(s), stop=self.stop)
        except ValueError as e:
            if force:
                raise
            self.fe.warn(str(e))
            return saved > 0
        self.session.reset(new)
        self.last_prompt_tokens = 0
        self.fe.compacted(before, after, summary)
        return True

    # ---- the loop -------------------------------------------------------------------------------------------
    def interrupt(self):
        self.stop.set()
        self.client.abort()

    def run(self, text, extra_context=None):
        """Handle one user message to completion. Returns the final answer text."""
        self.stop.clear()
        self.recent_fail = {}
        self.turns_in_run = 0
        if self.depth == 0:
            for u in re.findall(r"https?://[^\s)>\"']+", text):
                self.perms.known_urls.add(u.rstrip(".,;"))
            r = hooks.run_hooks(self.settings, "UserPromptSubmit", {"prompt": text}, self.ctx.cwd, session_id=self.session.id)
            if r.blocked:
                self.fe.warn("Blocked by a hook: %s" % r.message)
                return ""
            for w in r.warnings:
                self.fe.warn(w)
            if r.context:
                text += "\n\n" + "\n".join(r.context)
        if extra_context:
            text += "\n\n" + extra_context
        if not self.session.messages:
            text += "\n\n" + self.environment()      # (at the end: the conversation's title comes from the first line)
        self.fe.begin_turn(text)
        self.session.add({"role": "user", "content": text})
        self.session.set_model(self.client.model)
        stop_hook_rounds = 0
        final = ""
        try:
            while True:
                final = self._until_answer()
                if self.depth == 0 and not self.stop.is_set():
                    r = hooks.run_hooks(self.settings, "Stop", {"stop_reason": "end_turn"}, self.ctx.cwd, session_id=self.session.id)
                    if r.blocked and stop_hook_rounds < 3:
                        stop_hook_rounds += 1
                        self.session.add({"role": "user", "_synthetic": "hook", "content": "[hook] " + r.message})
                        continue
                break
        except KeyboardInterrupt:
            self.interrupt()
            self._close_pending_calls()
            self.fe.warn("Interrupted.")
        self.last_text = final
        self.fe.end_turn(final)
        return final

    def _close_pending_calls(self):
        """After an interruption every tool call the model made needs an answer, or the next request is invalid."""
        msgs = self.session.messages
        answered = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for c in m["tool_calls"]:
                    if c["id"] not in answered:
                        self.session.add({"role": "tool", "tool_call_id": c["id"], "content": "[interrupted by the user before this ran]"})
                break
            if m.get("role") == "user":
                break

    def _until_answer(self):
        final = ""
        retried_overflow = False
        while True:
            self.turns_in_run += 1
            if self.turns_in_run > self.max_turns:
                self.fe.warn("Stopped after %d steps. Say \"continue\" to keep going." % self.max_turns)
                return final
            self.maybe_compact()
            try:
                comp = self._complete()
            except Cancelled:
                self.fe.warn("Interrupted.")
                self._close_pending_calls()
                return final
            except APIError as e:
                if e.kind == "context" and not retried_overflow:
                    retried_overflow = True
                    self.fe.warn("The conversation doesn't fit the model's context. Compressing it...")
                    if not self.context_window:
                        self.context_window = 8192
                    try:
                        self.maybe_compact(force=True, reason="overflow")
                    except ValueError as err:
                        self.fe.error(str(err))
                        return final
                    continue
                if e.kind == "tools" and not self.text_mode:
                    self.fe.warn("This model server can't do tool calls itself; switching to the text format.")
                    self.text_mode = True
                    self._system_cache = self._tools_cache = None
                    continue
                self.fe.error(str(e))
                return final
            self.session.add_usage(comp)
            self.last_prompt_tokens = comp.prompt_tokens + comp.completion_tokens if comp.prompt_tokens else 0
            content = comp.content
            calls = list(comp.tool_calls)
            if self.text_mode or (not calls and "<tool_call>" in content):
                visible, parsed = parse_text_tool_calls(content, set(self.tools))
                if parsed:
                    calls = parsed
                    if not self.text_mode:
                        content = visible
            msg = {"role": "assistant", "content": content}
            if calls and not self.text_mode:
                msg["tool_calls"] = calls
            self.session.add(msg)
            self.prompt_len_at_usage = len(self.session.messages)
            if content.strip():
                final = content
            self.fe.end_text()
            self.fe.usage(comp, self.session, self.context_status())
            if not calls:
                if not content.strip() and comp.finish == "length":
                    self.fe.warn("The model ran out of room before answering (context full or max reply length reached).")
                return final
            for call in calls:
                if self.stop.is_set():
                    self._close_pending_calls()
                    return final
                result_msg = self._exec(call)
                if self.text_mode:
                    self.session.add({"role": "user", "_synthetic": "tool",
                                      "content": "<tool_response name=\"%s\">\n%s\n</tool_response>" % (call["function"]["name"], result_msg["content"])})
                else:
                    self.session.add(result_msg)
            if self.stop.is_set():
                return final

    def context_status(self):
        used = self.context_used()
        return {"used": used, "window": self.context_window, "pct": (used * 100.0 / self.context_window) if self.context_window else None}

    def _complete(self):
        fe = self.fe
        state = {"first": False, "chars": 0, "tail": ""}

        def on_text(d):
            state["first"] = True
            if self.depth == 0:
                fe.on_text(d)
            state["tail"] = (state["tail"] + d)[-1800:]
            state["chars"] += len(d)
            if state["chars"] % 40 < len(d) and self._looping(state["tail"]):
                self.stop.set()
                fe.warn("The model is repeating itself, so the reply was stopped.")
                self.client.abort()

        def on_think(d):
            state["first"] = True
            if self.depth == 0:
                fe.on_thinking(d)

        def on_tool(i, c):
            if not state["first"]:
                state["first"] = True

        msgs = self.build_messages()
        est = self.overhead_tokens() + compact.estimate(self.session.messages)
        fe.waiting("Reading the prompt (about %s tokens; the model server keeps what it has already read)" % util.human_tokens(est) if est > 1500 else "Thinking")
        max_tokens = int(self.settings.get("max_tokens", 0) or 0)
        return self.client.chat(msgs, tools=self.schemas() or None, max_tokens=max_tokens, temperature=float(self.settings.get("temperature", 0.2)),
                                on_text=on_text, on_thinking=on_think, on_tool=on_tool, stop=self.stop,
                                on_progress=(lambda done, total, cached=0: fe.progress(done, total, cached)) if self.depth == 0 else None)

    @staticmethod
    def _looping(tail):
        if len(tail) < 400:
            return False
        probe = tail[-60:]
        return len(probe.strip()) > 20 and tail.count(probe) >= 6

    # ---- tools -----------------------------------------------------------------------------------------------
    def find_tool(self, name):
        if name in self.tools:
            return self.tools[name]
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        alias = {"readfile": "Read", "writefile": "Write", "editfile": "Edit", "bashcommand": "Bash", "shell": "Bash", "runcommand": "Bash", "execute": "Bash",
                 "listdir": "LS", "listdirectory": "LS", "search": "Grep", "websearch": "WebSearch", "fetch": "WebFetch", "todo": "TodoWrite", "ls": "LS"}
        want = alias.get(norm(name))
        if want and want in self.tools:
            return self.tools[want]
        for n, t in self.tools.items():
            if norm(n) == norm(name):
                return t
        return None

    def _tool_msg(self, call, text):
        return {"role": "tool", "tool_call_id": call["id"], "content": text, "_name": call["function"]["name"]}

    def _exec(self, call):
        fe, ctx = self.fe, self.ctx
        name = call["function"]["name"]
        cid = call["id"]
        tool = self.find_tool(name)
        if tool is None:
            res = Result("Unknown tool %r. Available tools: %s" % (name, ", ".join(self.tools)), error=True, summary="Unknown tool")
            fe.tool_use(cid, name, "%s(?)" % name, {}, self.depth)
            fe.tool_result(cid, name, res, self.depth)
            return self._tool_msg(call, res.text)
        try:
            args = parse_args(call["function"].get("arguments"))
        except ToolError as e:
            res = Result(str(e), error=True, summary="Bad arguments")
            fe.tool_use(cid, tool.name, "%s(?)" % tool.name, {}, self.depth)
            fe.tool_result(cid, tool.name, res, self.depth)
            return self._tool_msg(call, res.text)
        missing = [k for k in tool.parameters.get("required", []) if k not in args and not (k == "path" and "file_path" in args)]
        summary = tool.summary(args, ctx)
        fe.tool_use(cid, tool.name, summary, args, self.depth)
        if missing:
            res = Result("Missing required argument%s: %s" % ("s" if len(missing) > 1 else "", ", ".join(missing)), error=True, summary="Bad arguments")
            fe.tool_result(cid, tool.name, res, self.depth)
            return self._tool_msg(call, res.text)

        # permission
        decision = self.perms.decide(tool, args)
        denial = ""
        if decision.action == "deny":
            denial = decision.reason
        elif decision.action == "ask":
            if not (self.fe.interactive and self.perms.interactive):
                denial = "needs permission (%s), but this run can't ask. Allow it with --allowedTools or a permission rule" % decision.reason
            else:
                verdict, scope, feedback = fe.request_permission(tool, args, decision, summary)
                if verdict == "allow":
                    if scope != "once":
                        self.perms.remember(decision.suggest, scope)
                    if tool.name == "WebFetch":
                        args["_private_ok"] = True
                else:
                    denial = "the user said no" + (": " + feedback if feedback else ". Don't retry the same thing; ask what they'd prefer or take another approach")
        if denial:
            res = Result("Permission denied: %s." % denial, error=True, summary="Denied")
            fe.tool_result(cid, tool.name, res, self.depth)
            return self._tool_msg(call, res.text)
        if self.depth == 0:
            h = hooks.run_hooks(self.settings, "PreToolUse", {"tool_name": tool.name, "tool_input": args}, ctx.cwd, tool.name, self.session.id)
            if h.blocked:
                res = Result("Blocked by a hook: %s" % h.message, error=True, summary="Blocked")
                fe.tool_result(cid, tool.name, res, self.depth)
                return self._tool_msg(call, res.text)

        ctx.progress = (lambda line: fe.tool_progress(cid, line))
        try:
            res = tool.run(args, ctx)
        except ToolError as e:
            res = Result(str(e), error=True, summary="Failed")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # a bug in a tool must not end the conversation
            res = Result("The %s tool crashed: %s: %s" % (tool.name, type(e).__name__, e), error=True, summary="Crashed")
        text = util.truncate_middle(res.text or "", max(2000, ctx.limit()), "characters")
        res.text = text

        # remember what the model may fetch later: addresses in search results
        if tool.name == "WebSearch" and not res.error:
            for u in re.findall(r"^\s+(https?://\S+)$", text, re.M):
                self.perms.known_urls.add(u)
        # a call that keeps failing the same way gets a nudge
        key = (tool.name, json.dumps(args, sort_keys=True)[:300])
        if res.error:
            self.recent_fail[key] = self.recent_fail.get(key, 0) + 1
            if self.recent_fail[key] >= 3:
                res.text += "\n[This exact call has failed %d times. Stop repeating it: change the approach or ask the user.]" % self.recent_fail[key]
        if self.depth == 0:
            h = hooks.run_hooks(self.settings, "PostToolUse", {"tool_name": tool.name, "tool_input": args, "tool_response": res.text[:4000]}, ctx.cwd, tool.name, self.session.id)
            if h.blocked:
                res.text += "\n[hook] " + h.message
        fe.tool_result(cid, tool.name, res, self.depth)
        return self._tool_msg(call, res.text)


# ---- sub-agents -------------------------------------------------------------------------------------------------

READ_ONLY_NAMES = {"Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "Wikipedia", "Arxiv", "FileSearch", "Calculator", "Weather", "TodoWrite"}


def parse_agent_file(path):
    text = util.read_text(path)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    meta, body = {}, text
    if m:
        for line in m.group(1).split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
    tools = [t.strip() for t in re.split(r"[,\s]+", meta.get("tools", "")) if t.strip()]
    return {"name": name, "description": meta.get("description", ""), "tools": tools, "prompt": body.strip(), "path": path}


def load_agents(settings):
    agents = {"general-purpose": {"name": "general-purpose", "description": "Does multi-step research or coding work on its own and reports back.", "tools": [], "prompt": ""},
              "explore": {"name": "explore", "description": "Read-only search of the code or the web; fast and safe.", "tools": sorted(READ_ONLY_NAMES), "prompt": "You only read and search; you never change anything."},
              "plan": {"name": "plan", "description": "Reads the code and writes an implementation plan; changes nothing.", "tools": sorted(READ_ONLY_NAMES),
                       "prompt": "You design implementation plans: read the relevant code, then report a step-by-step plan with the files to change."}}
    dirs = [os.path.join(util.config_dir(), "agents")]
    if settings.trusted:
        dirs.append(os.path.join(settings.project_dir, "agents"))
    for d in dirs:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".md"):
                    a = parse_agent_file(os.path.join(d, fn))
                    agents[a["name"]] = a
    return agents


class SubFrontend(Frontend):
    """Shows what a sub-agent does as indented tool lines, without its streamed text."""

    def __init__(self, parent):
        self.parent = parent
        self.interactive = parent.interactive
        self.style = parent.style

    def tool_use(self, call_id, name, summary, args, depth=0):
        self.parent.tool_use(call_id, name, summary, args, depth)

    def tool_result(self, call_id, name, result, depth=0):
        self.parent.tool_result(call_id, name, result, depth)

    def request_permission(self, tool, args, decision, summary):
        return self.parent.request_permission(tool, args, decision, summary)

    def warn(self, text):
        self.parent.warn(text)

    def error(self, text):
        self.parent.error(text)


class TaskTool(Tool):
    name = "Task"
    kind = "read"
    read_only = True
    description = "Run a sub-agent with fresh context for a focused job (research, searching a big codebase); returns its report. Types: general-purpose, explore, plan."
    parameters = {"type": "object", "properties": {
        "description": {"type": "string", "description": "3-6 words naming the job"},
        "prompt": {"type": "string", "description": "Everything the sub-agent needs to know, and what to report back"},
        "subagent_type": {"type": "string", "description": "general-purpose (default), explore, plan, or a custom agent name"}}, "required": ["description", "prompt"]}

    def summary(self, args, ctx):
        return "Task(%s)" % (args.get("description") or "")[:80]

    def run(self, args, ctx):
        parent = ctx.agent
        if parent is None or parent.depth >= 1:
            raise ToolError("Sub-agents can't start sub-agents.")
        prompt = need(args, "prompt")
        agents = load_agents(parent.settings)
        kind = args.get("subagent_type") or "general-purpose"
        spec = agents.get(kind)
        if spec is None:
            raise ToolError("No agent called %r. Available: %s" % (kind, ", ".join(agents)))
        allow = set(spec["tools"]) if spec["tools"] else {n for n in parent.tools if n not in ("Task", "AskUserQuestion", "ExitPlanMode")}
        sub = Agent(parent.settings, parent.client, SubFrontend(parent.fe), Session(ctx.cwd, persist=False), Context(parent.settings, ctx.cwd, parent.fe),
                    list(parent.tools.values()), parent.perms, depth=parent.depth + 1, system_extra=spec.get("prompt", ""), allow_names=allow)
        sub.context_window = parent.context_window
        sub.text_mode = parent.text_mode
        sub.ctx.shell_cwd = getattr(ctx, "shell_cwd", ctx.cwd)
        sub.ctx.read_state = ctx.read_state
        sub.stop = parent.stop
        sub.ctx.abort = parent.stop
        out = sub.run(prompt)
        if not out.strip():
            return Result("The sub-agent finished without a report.", error=True, summary="No report")
        return Result(out, summary="Sub-agent done (%d steps)" % sub.turns_in_run)
