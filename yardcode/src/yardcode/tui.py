"""The terminal screen: streams the answer, draws tool calls and results, asks permissions, reads your input."""
import os
import re
import shutil
import sys
import threading
import time

from . import ui, util
from .frontend import Frontend

try:
    import readline  # noqa: F401  (line editing and history for input())
except ImportError:  # pragma: no cover
    readline = None

try:
    import termios
    import tty
except ImportError:  # pragma: no cover  (Windows)
    termios = tty = None


class LiveMarkdown:
    """Renders streamed markdown line by line. The line being written shows as plain text and is swapped for its styled
    version when it is complete."""

    def __init__(self, S, out):
        self.S = S
        self.out = out
        self.md = ui.Markdown(S)
        self.buf = ""
        self.rows = 0
        self.live = bool(S.enabled and out.isatty())
        self.any = False

    def _erase(self):
        if self.rows >= 0 and self.live and self.buf_shown:
            self.out.write("\r" + ("\x1b[%dA" % self.rows if self.rows else "") + "\x1b[J")
        self.buf_shown = False
        self.rows = 0

    buf_shown = False

    def feed(self, text):
        self.buf += text
        wrote = False
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._erase()
            for l in self.md.lines(line):
                self.out.write(l + "\n")
            wrote = True
            self.any = True
        if self.buf and self.live:
            self._erase()
            width = ui.terminal_width(100)
            shown = self.buf if not self.md.in_code else "  " + self.S.g("vbar") + " " + self.buf
            self.out.write(shown)
            self.rows = max(0, (ui.vlen(shown) - 1) // width)
            self.buf_shown = True
            wrote = True
        if wrote:
            self.out.flush()

    def finish(self):
        if self.buf:
            self._erase()
            for l in self.md.lines(self.buf):
                self.out.write(l + "\n")
            self.buf = ""
        for l in self.md.finish():
            self.out.write(l + "\n")
        self.out.flush()


class EscWatcher:
    """While the model works, pressing Esc interrupts it (Ctrl-C does too). Only on a real terminal."""

    def __init__(self, on_esc):
        self.on_esc = on_esc
        self.thread = None
        self.running = False
        self.saved = None

    def start(self):
        if termios is None or not sys.stdin.isatty():
            return
        try:
            self.saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except (termios.error, OSError):
            self.saved = None
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        import select
        while self.running:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.15)
                if not r:
                    continue
                ch = os.read(sys.stdin.fileno(), 32)
            except (OSError, ValueError):
                return
            if ch == b"\x1b":
                self.on_esc()
                return

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
            self.thread = None
        if self.saved is not None:
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)
            except (termios.error, OSError):
                pass
            self.saved = None


class TUI(Frontend):
    interactive = True

    def __init__(self, settings, S=None, out=None):
        self.settings = settings
        self.out = out or sys.stdout
        self.style = S or ui.detect_style(settings.get("theme", "auto"), self.out)
        self.spinner = ui.Spinner(self.style, self.out)
        self.live = None
        self.agent = None
        self.esc = None
        self.turn_start = 0.0
        self.turn_tokens = 0
        self.turn_prompt = 0
        self.turn_seconds = 0.0
        self.turn_gen_seconds = 0.0
        self.thinking_on = False
        self.think_live = False
        self.turn_thought = False
        self.hinted = False
        self.think_buf = ""
        self.quiet = False
        self.verbose = False

    # ---- helpers ----
    def w(self, text="", end="\n"):
        self.out.write(text + end)
        self.out.flush()

    def _calm(self):
        """Stop the spinner (and finish a half-written answer) before anything else is printed."""
        self.spinner.end()
        if self.thinking_on:
            self.thinking_on = False
            self._flush_think()

    def _flush_think(self):
        if self.think_live:               # it was streamed on screen already: just end the line
            self.think_live = False
            self.think_buf = ""
            self.w()
            return
        if self.think_buf.strip():
            S = self.style
            text = self.think_buf.strip()
            lines = text.split("\n")
            show = lines if len(lines) <= 6 else lines[:3] + ["..."] + lines[-2:]
            self.w(S.fg("think", S.italic("  " + S.g("tri") + " thinking (%d lines)" % len(lines))))
            for l in show:
                self.w(S.fg("think", "    " + l[:ui.terminal_width() - 8]))
        self.think_buf = ""

    # ---- turn ----
    def begin_turn(self, user_text):
        self.turn_start = time.time()
        self.turn_tokens = self.turn_prompt = 0
        self.turn_seconds = self.turn_gen_seconds = 0.0
        self.turn_thought = False
        if self.esc is None and self.agent is not None:
            self.esc = EscWatcher(self.agent.interrupt)
        if self.esc:
            self.esc.start()

    def waiting(self, label, tokens=0):
        self._prompt_t0 = None
        if self.live is None:
            self.spinner.begin(label, "esc to interrupt")
        self.spinner.update(label)

    _prompt_t0 = None

    def progress(self, done, total, cached=0):
        """The model is reading the prompt: show how far it is and about how long is left."""
        now = time.time()
        todo = max(0, total - cached)
        if self._prompt_t0 is None:
            self._prompt_t0 = (now, done)
        t0, d0 = self._prompt_t0
        rate = (done - d0) / (now - t0) if now - t0 > 3 and done > d0 else 0
        left = ((total - done) / rate) if rate else None
        label = "Reading the prompt: %s of %s tokens" % (util.human_tokens(done), util.human_tokens(total))
        if left is not None and total > done:
            label += " · about %s left" % util.human_duration(left)
        elif cached and not todo:
            label = "Reading the prompt (already cached)"
        self.spinner.update(label)

    def on_thinking(self, delta):
        S = self.style
        mode = self.settings.get("thinking", "show")
        self.turn_thought = True
        if mode == "hide":
            self.spinner.update("Thinking", self.spinner.tokens + 1)
            return
        if mode == "live":              # the reasoning appears as the model writes it
            if not self.think_live:
                self.spinner.end()
                self.thinking_on = True
                self.think_live = True
                self.w(S.fg("think", S.italic("  " + S.g("tri") + " thinking")))
                self.out.write("    ")
            self.out.write(S.fg("think", S.italic(delta.replace("\n", "\n    "))))
            self.out.flush()
            return
        self.thinking_on = True
        self.think_buf += delta
        self.spinner.update("Thinking", self.spinner.tokens + 1)

    def on_text(self, delta):
        if self.live is None:
            self._calm()
            self.live = LiveMarkdown(self.style, self.out)
            self.w()
        self.live.feed(delta)

    def end_text(self):
        self.spinner.end()
        if self.thinking_on:
            self.thinking_on = False
            self._flush_think()
        if self.live is not None:
            self.live.finish()
            self.live = None

    def end_turn(self, final_text):
        self.end_text()
        if self.esc:
            self.esc.stop()
        S = self.style
        if self.settings.get("thinking", "show") == "live" and not self.turn_thought and not self.hinted:
            self.hinted = True
            self.w(S.muted("  (No reasoning came from this model. A llama.cpp model only thinks when it is started with thinking on: nodeyard ai split deploy --think on)"))
        secs = time.time() - self.turn_start
        parts = [util.human_duration(secs) if secs >= 1 else "%.1fs" % secs]
        if self.turn_tokens:
            parts.append("%s %s tokens" % (S.g("down"), util.human_tokens(self.turn_tokens)))
        if self.turn_prompt:
            parts.append("%s %s" % (S.g("up"), util.human_tokens(self.turn_prompt)))
        if self.turn_gen_seconds > 0.5 and self.turn_tokens:
            parts.append("%.1f tok/s" % (self.turn_tokens / self.turn_gen_seconds))
        self.w(S.muted("  " + " · ".join(parts)))

    def usage(self, comp, session, context):
        self.turn_tokens += comp.completion_tokens
        self.turn_prompt = max(self.turn_prompt, comp.prompt_tokens)
        self.turn_seconds += comp.seconds
        if comp.completion_tokens and comp.seconds:
            self.turn_gen_seconds += max(0.0, comp.seconds - (comp.first_token or 0))

    # ---- tools ----
    def tool_use(self, call_id, name, summary, args, depth=0):
        self.end_text()
        S = self.style
        pad = "  " * depth
        head = S.bold(summary.split("(", 1)[0]) + ("(" + summary.split("(", 1)[1] if "(" in summary else "")
        self.w("%s%s %s" % (pad, S.accent(S.g("dot")), head))
        self.spinner.begin("Running %s" % name.split("__")[-1], "esc to interrupt")

    def tool_progress(self, call_id, line):
        if line:
            self.spinner.update("Running · " + line[:60])

    def tool_result(self, call_id, name, result, depth=0):
        self.spinner.end()
        S = self.style
        pad = "  " * depth
        elbow = S.muted(S.g("elbow"))
        color = S.err if result.error else (lambda t: t)
        if result.summary:
            self.w("%s  %s  %s" % (pad, elbow, color(result.summary)))
        if result.diff and result.diff[0]:
            for l in result.diff[0][:40]:
                self.w("%s     %s" % (pad, l))
        elif result.preview:
            for l in result.preview[:6]:
                self.w("%s     %s" % (pad, S.muted(l[:ui.terminal_width() - 8 - 2 * depth])))
            extra = result.meta.get("lines", 0) - len(result.preview)
            if extra > 0:
                self.w("%s     %s" % (pad, S.muted("… +%d more line%s" % (extra, "" if extra == 1 else "s"))))
        if not result.summary and result.error:
            self.w("%s  %s  %s" % (pad, elbow, S.err(result.text.split("\n")[0][:150])))
        elif result.error and result.text and result.summary in ("Denied", "Failed", "Bad arguments", "Blocked", "Unknown tool", "Crashed"):
            self.w("%s     %s" % (pad, S.muted(result.text.split("\n")[0][:ui.terminal_width() - 10])))

    def request_permission(self, tool, args, decision, summary):
        self._calm()
        if self.esc:
            self.esc.stop()
        S = self.style
        lines = []
        spec = tool.specifier(args, self.agent.ctx) if self.agent else ""
        if tool.name == "Bash":
            lines += [S.bold(l) for l in (args.get("command", "") or "").split("\n")[:12]]
            if args.get("description"):
                lines.append(S.muted(args["description"]))
        elif tool.kind == "edit":
            lines.append(S.bold(util.shorten_path(str(spec), self.agent.ctx.cwd if self.agent else None)))
            d = tool.preview_diff(args, self.agent.ctx) if self.agent else None
            if d and d[0]:
                lines += [""] + d[0][:30]
                lines.append(S.muted("%d addition%s, %d removal%s" % (d[1], "" if d[1] == 1 else "s", d[2], "" if d[2] == 1 else "s")))
        elif tool.name == "Python":
            lines += (args.get("code", "") or "").split("\n")[:20]
        else:
            lines.append(S.bold(summary))
        if decision.risk:
            lines += ["", S.warn(S.g("warn") + " This " + decision.risk + ".")]
        title = "%s: %s" % (tool.name, decision.reason) if decision.reason else tool.name
        for l in ui.box(title, lines, S, color="warn" if decision.risk else "accent"):
            self.w(l)
        opts = ["Yes"]
        if decision.suggest and not decision.risk:
            hint = decision.scope_hint or decision.suggest
            opts += ["Yes, and don't ask again this session for %s" % hint, "Yes, and always for this project (%s)" % decision.suggest]
        opts.append("No, and tell the model what to do instead")
        for i, o in enumerate(opts, 1):
            self.w("  %s %s" % (S.accent("%d." % i), o))
        while True:
            try:
                ans = input(S.accent("  Choose [1-%d] " % len(opts))).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.w()
                return "deny", "once", "the user cancelled"
            if ans in ("", "1", "y", "yes"):
                n = 1
            elif ans in ("n", "no", "esc"):
                n = len(opts)
            elif ans.isdigit() and 1 <= int(ans) <= len(opts):
                n = int(ans)
            else:
                continue
            break
        if self.esc and self.agent:
            self.esc.start()
        if n == len(opts):
            try:
                fb = input(S.muted("  What should it do instead? (Enter to skip) ")).strip()
            except (EOFError, KeyboardInterrupt):
                fb = ""
            self.w()
            return "deny", "once", fb
        scope = "once" if n == 1 else ("session" if n == 2 else "project")
        return "allow", scope, ""

    def todos(self, items):
        self._calm()
        S = self.style
        self.w(S.muted("  " + S.g("elbow") + " ") + S.bold("Tasks"))
        for t in items:
            if t["status"] == "completed":
                self.w("     " + S.ok(S.g("done")) + " " + S.strike(S.muted(t["content"])))
            elif t["status"] == "in_progress":
                self.w("     " + S.accent(S.g("prog")) + " " + S.bold(t.get("activeForm") or t["content"]))
            else:
                self.w("     " + S.muted(S.g("todo")) + " " + t["content"])

    def ask_user(self, questions):
        self._calm()
        if self.esc:
            self.esc.stop()
        S = self.style
        answers = {}
        for q in questions:
            self.w()
            self.w(S.bold(S.accent("? ")) + S.bold(q.get("question", "")))
            opts = q.get("options") or []
            for i, o in enumerate(opts, 1):
                self.w("  %s %s%s" % (S.accent("%d." % i), o.get("label", ""), S.muted(" - " + o["description"]) if o.get("description") else ""))
            self.w("  %s %s" % (S.accent("%d." % (len(opts) + 1)), "Something else (type your own)"))
            multi = bool(q.get("multiSelect"))
            try:
                raw = input(S.accent("  Your answer%s: " % (" (numbers separated by commas)" if multi else ""))).strip()
            except (EOFError, KeyboardInterrupt):
                self.w()
                return None
            picks = []
            for tok in re.split(r"[,\s]+", raw):
                if tok.isdigit() and 1 <= int(tok) <= len(opts):
                    picks.append(opts[int(tok) - 1]["label"])
                elif tok.isdigit() and int(tok) == len(opts) + 1:
                    try:
                        picks.append(input(S.muted("  Type your answer: ")).strip())
                    except (EOFError, KeyboardInterrupt):
                        return None
            answers[q.get("question", "")] = ", ".join(picks) if picks else raw
        if self.esc and self.agent:
            self.esc.start()
        return answers

    def approve_plan(self, plan):
        self._calm()
        if self.esc:
            self.esc.stop()
        S = self.style
        for l in ui.box("Plan", ui.render_markdown(plan, S, 96).split("\n"), S, color="accent"):
            self.w(l)
        self.w("  %s Yes, and accept edits automatically" % S.accent("1."))
        self.w("  %s Yes, and ask before each change" % S.accent("2."))
        self.w("  %s No, keep planning" % S.accent("3."))
        while True:
            try:
                ans = input(S.accent("  Choose [1-3] ")).strip()
            except (EOFError, KeyboardInterrupt):
                self.w()
                return "no"
            if ans in ("1", "y", "yes", ""):
                res = "auto"
            elif ans == "2":
                res = "ask"
            elif ans in ("3", "n", "no"):
                res = "no"
            else:
                continue
            break
        if self.esc and self.agent:
            self.esc.start()
        return res

    # ---- housekeeping ----
    def info(self, text):
        self._calm()
        self.w(self.style.muted("  " + text))

    def warn(self, text):
        self._calm()
        self.w(self.style.warn("  " + self.style.g("warn") + " " + text))

    def error(self, text):
        self._calm()
        self.w(self.style.err("  " + self.style.g("cross") + " " + text))

    def compacting(self, label):
        if self.live is not None:
            self.end_text()
        self.spinner.begin(label, "")
        self.spinner.update(label)

    def compacted(self, before, after, summary):
        self.spinner.end()
        S = self.style
        self.w(S.accent("  ✂ " if S.unicode else "  ") + "Compressed the conversation: %s -> %s tokens" % (util.human_tokens(before), util.human_tokens(after)))

    def mode_changed(self, mode):
        self.w(self.style.muted("  mode: " + mode))


# ---- input -------------------------------------------------------------------------------------------------

def setup_readline(history_file, commands):
    """History, tab completion for /commands and @files, and shift+tab to change the permission mode."""
    if readline is None:
        return
    try:
        readline.set_history_length(2000)
        if os.path.exists(history_file):
            readline.read_history_file(history_file)
    except OSError:
        pass
    doc = readline.__doc__ or ""

    def complete(text, state):
        line = readline.get_line_buffer()
        opts = []
        if line.startswith("/") and " " not in line:
            opts = [c + " " for c in commands if c.startswith(line)]
        elif text.startswith("@"):
            base = os.path.expanduser(text[1:])
            d, prefix = os.path.split(base)
            try:
                for n in sorted(os.listdir(d or ".")):
                    if n.startswith(prefix) and (prefix or not n.startswith(".")):
                        p = os.path.join(d, n)
                        opts.append("@" + p + ("/" if os.path.isdir(p) else " "))
            except OSError:
                pass
        return opts[state] if state < len(opts) else None

    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n")
    try:
        if "libedit" in doc:
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
            readline.parse_and_bind(r'"\e[Z": "\C-a\C-k/mode\C-m"')
            readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass


def save_history(history_file):
    if readline is None:
        return
    try:
        util.ensure_dir(os.path.dirname(history_file), 0o700)
        readline.write_history_file(history_file)
    except OSError:
        pass


def read_prompt(prompt, S):
    """One message from the keyboard. A line ending in \\ continues on the next line, and a paste arrives whole."""
    import select
    lines = []
    cont = False
    while True:
        text = input(prompt if not cont else S.muted("  … "))
        if text.endswith("\\") and not text.endswith("\\\\"):
            lines.append(text[:-1])
            cont = True
            continue
        lines.append(text)
        # a paste without bracketed-paste support delivers its lines at once: take them all
        try:
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.02)[0]:
                more = sys.stdin.readline()
                while more:
                    lines.append(more.rstrip("\n"))
                    if not select.select([sys.stdin], [], [], 0.02)[0]:
                        break
                    more = sys.stdin.readline()
        except (OSError, ValueError):
            pass
        break
    return "\n".join(lines)


_ = shutil
