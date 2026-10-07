"""The interactive program: start-up, the prompt loop, @file mentions, ! shell lines, # notes and /commands."""
import os
import re
import subprocess
import sys
import time

from . import __version__, compact, memory, ui, util
from .agent import Agent, TaskTool
from .client import APIError, Client
from .config import Settings
from .mcp import start_servers
from .modelapi import ModelAPI
from .perms import MODE_LABEL, Permissions
from .plugins import load_plugins
from .session import Session
from .tools import builtin
from .tools.base import Context
from . import shellmode, sync
from .lineedit import LineEditor, default_files
from .tui import TUI, read_prompt, save_history, setup_readline

MENTION_RE = re.compile(r"(?<![\w/])@((?:~|\.{1,2}|/)?[\w.\-/~]+)")


class App:
    def __init__(self, settings, persist=True):
        self.settings = settings
        self.persist = persist
        self.client = Client(settings.api_base, settings.get("api_key", ""), settings.get("model", ""))
        self.modelapi = ModelAPI(settings.control_url(), settings.get("api_key", ""))
        self.tui = TUI(settings)
        self.session = None
        self.agent = None
        self.mcp_servers = []
        self.plugin_errors = []
        self.custom = {}
        self.last_interrupt = 0.0
        self.history_file = os.path.join(util.data_dir(), "history")
        self.commands = []
        self.unreachable = ""
        self.editor = None
        self._shell = None

    # ---- setup ------------------------------------------------------------------------------------------------
    def tool_list(self):
        s = self.settings
        tools = builtin(s, with_cluster=self.modelapi.available)
        tools.append(TaskTool())
        disabled = set(s.get("tools.disabled") or [])
        extra = []
        errs = []
        extra += load_plugins(s, on_error=lambda p, e: errs.append("%s: %s" % (p, e)))
        if self.mcp_servers == [] and s.get("mcpServers"):
            self.mcp_servers, mcp_tools = start_servers(s, os.getcwd(), on_error=lambda n, e: errs.append("MCP %s: %s" % (n, e)))
            self._mcp_tools = mcp_tools
        extra += getattr(self, "_mcp_tools", [])
        self.plugin_errors = errs
        return [t for t in tools + extra if t.name not in disabled]

    def discover(self):
        """Ask the server what it is: context length and model name."""
        s = self.settings
        window = int(s.get("context_window", 0) or 0)
        self.unreachable = ""
        if self.client.base_url:
            self.unreachable = self.client.probe()
            if self.unreachable:
                return window            # don't wait on every question below when nothing is there
        if not window and self.client.base_url:
            try:
                window = self.client.context_window()
            except APIError:
                window = 0
        names = []
        if self.client.base_url and not self.client.model:
            try:
                names = self.client.models()
            except APIError:
                names = []
            if names:
                self.client.model = names[0]
        return window

    def new_agent(self, session=None, window=None):
        s = self.settings
        self.session = session or Session(os.getcwd(), persist=self.persist)
        ctx = Context(s, os.getcwd(), self.tui)
        ctx.modelapi = self.modelapi
        ctx.web_proxy = self.make_web_proxy()
        perms = Permissions(s, ctx, interactive=True)
        self.agent = Agent(s, self.client, self.tui, self.session, ctx, self.tool_list(), perms)
        if window is not None:
            self.agent.context_window = window
        self.tui.agent = self.agent
        self.session.set_model(self.client.model)
        return self.agent

    def web_via(self):
        """Where web tools run: 'server' (the dashboard machine's internet), or 'local' (this computer's)."""
        via = self.settings.get("web.via", "auto")
        if via == "local" or not self.modelapi.available or not self.settings.get("api_key"):
            return "local"
        return "server"

    def make_web_proxy(self):
        """Web search and page reading go through the nodeyard server, so they use ITS internet connection: the AI searches from where
        it lives, not from whatever network this computer is on (a school filter, hotel wifi...). Falls back to this computer only when
        web.via is auto and the server can't be used."""
        from .modelapi import ModelAPIError
        from .tools.base import Result, ToolError
        app, state = self, {"down": False}

        def proxy(name, args):
            if app.web_via() == "local" or state["down"]:
                return None
            body = {"tool": name, "args": {k: v for k, v in args.items() if not str(k).startswith("_")}}
            try:
                d = app.modelapi.request("POST", "/web/tool", body, timeout=75)
            except ModelAPIError as e:
                if e.status in (400, 429, 502):        # the server tried and the search itself failed: that's the answer
                    raise ToolError("%s (searched from the server)" % e)
                if app.settings.get("web.via", "auto") == "server":
                    raise ToolError("The server can't be reached for web search: %s" % e)
                state["down"] = True
                app.tui.warn("The server isn't answering for web search, so searching from this computer instead.")
                return None
            return Result(d.get("text", ""), error=bool(d.get("error")), summary=(d.get("summary") or name) + " · via the server", preview=d.get("preview") or [])
        return proxy

    def load_custom_commands(self):
        out = {}
        dirs = [os.path.join(util.config_dir(), "commands"), os.path.join(self.settings.project_dir, "commands")]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".md"):
                    continue
                name = fn[:-3]
                text = util.read_text(os.path.join(d, fn))
                desc = ""
                m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
                if m:
                    for line in m.group(1).split("\n"):
                        if line.lower().startswith("description:"):
                            desc = line.split(":", 1)[1].strip()
                    text = m.group(2)
                out[name] = {"prompt": text.strip(), "description": desc or "custom command", "path": os.path.join(d, fn)}
        self.custom = out
        return out

    # ---- screen --------------------------------------------------------------------------------------------------
    def banner(self):
        S = self.tui.style
        w = min(ui.terminal_width(), 76)
        ag = self.agent
        n_tools = len(ag.active_tools())
        model = self.client.model or "(no model yet)"
        ctx = ag.context_window
        lines = [S.bold("yardcode") + S.muted(" " + __version__),
                 S.accent(model) + S.muted("  " + (self.client.base_url or "not connected")),
                 S.muted(util.shorten_path(ag.ctx.cwd, '/')),
                 S.muted("%s · %s tools%s%s" % (MODE_LABEL.get(ag.perms.mode, ag.perms.mode), n_tools, " (small prompt for a small context; /tools shows all)" if ag.tier() != "full" else "", " · %s context" % util.human_tokens(ctx) if ctx else ""))]
        for l in ui.box("", lines, S, width=w, color="accent"):
            self.tui.w(l)
        self.tui.w(S.muted("  type / for commands · @file to include a file · !cmd to run a command · #note to remember · esc to interrupt"))
        if self.settings.pending_trust and not self.settings.trusted:
            self.tui.w(S.warn("  This project has settings that can run commands (hooks, MCP servers, allow rules). They are off until you run /trust."))
        for e in self.plugin_errors:
            self.tui.w(S.warn("  " + e))

    def status_line(self):
        S = self.tui.style
        ag = self.agent
        st = ag.context_status()
        bits = []
        if self.client.model:
            bits.append(self.client.model)
        if st["window"]:
            bits.append("%d%% of %s context" % (round(st["pct"]), util.human_tokens(st["window"])))
        else:
            bits.append("%s tokens" % util.human_tokens(st["used"]))
        bits.append(MODE_LABEL.get(ag.perms.mode, ag.perms.mode) + " (shift+tab)")
        text = " " + " · ".join(bits) + " "
        width = ui.terminal_width()
        rule = S.g("hr") * 2 + S.muted(text) + S.g("hr") * max(2, width - len(text) - 3)
        if ag.perms.mode != "default":
            rule = S.fg("warn" if ag.perms.mode == "bypassPermissions" else "accent", S.g("hr") * 2) + S.muted(text) + S.muted(S.g("hr") * max(2, width - len(text) - 3))
            return rule
        return S.muted(rule)

    # ---- input handling --------------------------------------------------------------------------------------------
    def expand_mentions(self, text):
        """@path pulls a file (or a folder listing) into the message."""
        added, seen = [], set()
        for m in MENTION_RE.finditer(text):
            raw = m.group(1).rstrip(".,;:)")
            path = self.agent.ctx.resolve(raw)
            if path in seen or not os.path.exists(path):
                continue
            seen.add(path)
            if os.path.isdir(path):
                try:
                    names = sorted(os.listdir(path))[:100]
                except OSError:
                    continue
                added.append('<directory path="%s">\n%s\n</directory>' % (util.shorten_path(path), "\n".join(names)))
            else:
                try:
                    with open(path, "rb") as f:
                        data = f.read(200_000)
                except OSError:
                    continue
                if util.is_binary(data):
                    continue
                body = data.decode("utf-8", "replace")
                self.agent.ctx.read_state[path] = os.path.getmtime(path)
                added.append('<file path="%s">\n%s\n</file>' % (util.shorten_path(path), util.truncate_middle(body, 40000, "characters")))
        if added:
            self.tui.info("Included %d file%s" % (len(added), "" if len(added) == 1 else "s"))
            return text + "\n\n" + "\n\n".join(added)
        return text

    def shell_session(self):
        """The shell that ! lines and shell mode run in: it keeps its folder and exported variables between lines."""
        if self._shell is None:
            self._shell = shellmode.Shell(self.agent.ctx.cwd, rc=bool(self.settings.get("shell.rc", True)))
        return self._shell

    def shell_prompt(self):
        return self.shell_session().prompt_text()

    def footer_line(self):
        """The line under the input: permission mode, model and how full the context is."""
        ag, S = self.agent, self.tui.style
        st = ag.context_status()
        bits = ["%s %s (shift+tab to cycle)" % ("▶▶" if S.unicode else ">>", MODE_LABEL.get(ag.perms.mode, ag.perms.mode))]
        if self.client.model:
            bits.append(self.client.model)
        bits.append(("%d%% of %s context" % (round(st["pct"]), util.human_tokens(st["window"]))) if st["window"] else "%s tokens" % util.human_tokens(st["used"]))
        tone = {"bypassPermissions": "warn", "acceptEdits": "accent", "plan": "accent"}.get(ag.perms.mode, "muted")
        return " · ".join(bits), tone

    def shell_line(self, cmd):
        """A line starting with ! runs directly (not by the model) and its output joins the conversation."""
        S = self.tui.style
        if shellmode.available():
            return shellmode.run_line(self, cmd)
        tool = self.agent.tools.get("Bash")
        if tool is None:
            self.tui.warn("The Bash tool is off.")
            return
        res = tool.run({"command": cmd}, self.agent.ctx)
        for l in res.text.split("\n")[:60]:
            self.tui.w("  " + S.muted(l))
        self.session.add({"role": "user", "content": "I ran this in my shell:\n$ %s\n%s" % (cmd, util.truncate_middle(res.text, 6000, "characters")), "_synthetic": "shell"})

    def note_line(self, note):
        """# text saves a note to the project's YARDCODE.md (## for your own, all projects)."""
        scope = "user" if note.startswith("#") else "project"
        text = note.lstrip("#").strip()
        if not text:
            return
        path, added = memory.add_note(text, scope, self.agent.ctx.cwd)
        self.agent.refresh_prompt()
        self.tui.info(("Saved to %s" if added else "Already in %s") % util.shorten_path(path))

    def handle(self, text):
        """One line from the user. Returns False to quit."""
        from . import slash
        text = text.rstrip()
        if not text.strip():
            return True
        if text.startswith("/") and not text.startswith("//"):
            name, _, arg = text[1:].partition(" ")
            return slash.dispatch(self, name.strip().lower(), arg.strip())
        if text.startswith("!") and not text.startswith("!!"):
            cmd = text[1:].strip()
            if not cmd:
                self.tui.info("Type ! on an empty prompt for shell mode, or !command to run one command (e.g. !git status).")
            elif cmd in ("exit", "quit", "logout") and self.editor is not None and self.editor.mode == "shell":
                self.editor.mode = ""
                self.tui.w(self.tui.style.muted("  Back to chat."))
            else:
                self.shell_line(cmd)
            return True
        if text.startswith("#") and len(text) > 1 and not text.startswith("#!"):
            self.note_line(text[1:])
            return True
        self.send(text)
        return True

    def send(self, text, plain=False):
        if not plain:
            text = self.expand_mentions(text)
        self.ensure_ready()
        self.agent.run(text)
        self.tui.w()
        sync.push_in_background(self)

    def ensure_ready(self):
        """If the model is still loading, wait for it instead of failing."""
        if not self.client.base_url:
            return
        state = self.client.health()
        if state != "loading":
            return
        S = self.tui.style
        self.tui.w(S.muted("  The model is still loading; waiting for it (Ctrl-C to give up)..."))
        t0 = time.time()
        try:
            self.client.wait_ready(timeout=1800, tick=lambda st, left: self.tui.spinner.begin("Waiting for the model to load · %s" % util.human_duration(time.time() - t0), ""))
        finally:
            self.tui.spinner.end()

    # ---- the loop -------------------------------------------------------------------------------------------------------
    def repl(self):
        from . import slash
        S = self.tui.style
        self.commands = ["/" + c for c in slash.names(self)]
        editor = LineEditor(S, self.tui.out, self.history_file + ".jsonl", commands=lambda: slash.menu_items(self), files=default_files,
                            footer=self.footer_line, shell_prompt=self.shell_prompt)
        use_editor = editor.available() and os.environ.get("YARDCODE_PLAIN_INPUT") != "1"
        self.editor = editor if use_editor else None
        if not use_editor:
            setup_readline(self.history_file, self.commands)
        prompt = S.accent(S.g("arrow") + " ")
        rl_prompt = prompt
        if S.enabled:
            rl_prompt = "\001" + S._code("accent") + "\002" + S.g("arrow") + " \001" + ui.RESET + "\002"
        while True:
            try:
                if not use_editor:
                    self.tui.w(self.status_line())
                text = editor.read(prompt) if use_editor else read_prompt(rl_prompt, S)
            except EOFError:
                self.tui.w()
                break
            except KeyboardInterrupt:
                self.tui.w()
                if time.time() - self.last_interrupt < 2:
                    break
                self.last_interrupt = time.time()
                self.tui.w(S.muted("  Press Ctrl-C again (or Ctrl-D) to exit."))
                continue
            try:
                if not self.handle(text):
                    break
            except KeyboardInterrupt:
                self.tui.spinner.end()
                self.tui.w(S.warn("\n  Interrupted."))
            except APIError as e:
                self.tui.error(str(e))
        if not use_editor:
            save_history(self.history_file)
        self.shutdown()

    def shutdown(self):
        from . import hooks
        try:
            hooks.run_hooks(self.settings, "SessionEnd", {}, self.agent.ctx.cwd, session_id=self.session.id)
        except Exception:
            pass
        t = getattr(self, "_sync_thread", None)
        if t is not None:
            t.join(timeout=4)               # let the last turn reach the server
        for s in self.mcp_servers:
            s.stop()
        for sh in list(self.agent.ctx.shells.values()):
            sh.kill()
        if self.agent.ctx.python is not None:
            self.agent.ctx.python.stop()
        for t in list(getattr(self.agent.ctx, "terms", {}).values()):
            t.stop()


def git_diff_text(cwd, staged=False):
    try:
        return subprocess.run(["git", "-C", cwd, "diff"] + (["--staged"] if staged else []), capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


_ = (compact, sys)
