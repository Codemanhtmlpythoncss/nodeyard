"""Terminal: a real interactive terminal the AI can drive (start a program, type, read the screen).

Bash runs one command and has no keyboard; this is for everything that needs one: REPLs (python, node, sqlite3), ssh and sudo prompts,
installers that ask questions, top, less, vim. The screen is rendered the way a terminal would show it, so the AI sees what you would.
"""
import atexit
import fcntl
import os
import pty
import re
import select
import signal
import struct
import termios
import threading
import time

from ..screen import Screen
from .base import Result, Tool, ToolError, need

KEYS = {"enter": "\r", "return": "\r", "tab": "\t", "esc": "\x1b", "escape": "\x1b", "space": " ", "backspace": "\x7f", "delete": "\x1b[3~",
        "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D", "home": "\x1b[H", "end": "\x1b[F", "pageup": "\x1b[5~", "pagedown": "\x1b[6~"}
KEY_RE = re.compile(r"\{([A-Za-z0-9-]+)\}")
MAX_SESSIONS = 4


def translate(keys):
    """Text with {enter}, {tab}, {ctrl-c}, {up}... in it, as the bytes a keyboard would send. A newline counts as Enter."""
    def one(m):
        name = m.group(1).lower()
        if name in KEYS:
            return KEYS[name]
        c = re.fullmatch(r"ctrl-([a-z])", name)
        if c:
            return chr(ord(c.group(1)) - 96)
        return m.group(0)
    return KEY_RE.sub(one, keys).replace("\r\n", "\r").replace("\n", "\r")


class PtySession:
    def __init__(self, command, cwd, rows, cols, env):
        self.command = command
        self.screen = Screen(cols, rows, scroll=True)
        self.lock = threading.Lock()
        self.exit_code = None
        self.started = time.time()
        self.last_read = 0
        self.total = 0
        argv = ["/bin/sh", "-c", command] if command else [os.environ.get("SHELL") or "/bin/bash", "-i"]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        threading.Thread(target=self._read, daemon=True).start()
        atexit.register(self.stop)

    def _read(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if not r:
                    if self.exit_code is None and self._reap():
                        break
                    continue
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                self._reap(wait=True)
                break
            with self.lock:
                self.screen.feed(data.decode("utf-8", "replace"))
                self.total += len(data)

    def _reap(self, wait=False):
        try:
            pid, status = os.waitpid(self.pid, 0 if wait else os.WNOHANG)
        except ChildProcessError:
            self.exit_code = self.exit_code if self.exit_code is not None else 0
            return True
        if pid == 0:
            return False
        self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -(os.WTERMSIG(status) if os.WIFSIGNALED(status) else 1)
        return True

    @property
    def alive(self):
        return self.exit_code is None

    def send(self, keys):
        if not self.alive:
            raise ToolError("That terminal has finished (exit code %s). Start a new one." % self.exit_code)
        try:
            os.write(self.fd, translate(keys).encode("utf-8"))
        except OSError:
            raise ToolError("That terminal has closed.")

    def wait_quiet(self, seconds):
        """Wait up to SECONDS for output, returning early once nothing new has come for a short while."""
        end = time.time() + seconds
        last, quiet_since = self.total, time.time()
        while time.time() < end and self.alive:
            time.sleep(0.05)
            if self.total != last:
                last, quiet_since = self.total, time.time()
            elif time.time() - quiet_since > 0.35 and self.total > self.last_read:
                break
        self.last_read = self.total

    def text(self, max_lines=40):
        with self.lock:
            lines = ["".join(l).rstrip() for l in self.screen.lines]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines[-max_lines:])

    def stop(self):
        if self.alive:
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.kill(self.pid, sig)
                except OSError:
                    break
                time.sleep(0.1)
                if self._reap():
                    break
        try:
            os.close(self.fd)
        except OSError:
            pass


class Terminal(Tool):
    name = "Terminal"
    kind = "exec"
    read_only = False
    description = ("An interactive terminal (a real pty) for what Bash can't do: start a program, send keys, read the screen (REPLs, ssh, installers that ask, top, less). "
                   "action: start (command), send (id, keys like 'ls{enter}', {ctrl-c}, {tab}, {up}), read (id), stop (id), list.")
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["start", "send", "read", "stop", "list"]},
        "id": {"type": "string", "description": "From start (term_1...)"},
        "command": {"type": "string", "description": "start: program to run (default: your shell)"},
        "keys": {"type": "string", "description": "send: text; {enter} {tab} {esc} {up} {ctrl-c}..."},
        "wait": {"type": "number", "description": "Seconds to wait for output (default 2)"}}, "required": ["action"]}

    def specifier(self, args, ctx):
        return str(args.get("command") or args.get("keys") or args.get("action") or "")

    def summary(self, args, ctx):
        a = args.get("action", "")
        detail = args.get("command") or args.get("keys") or args.get("id") or ""
        return "Terminal(%s%s)" % (a, (": " + str(detail)[:80]) if detail else "")

    def _sessions(self, ctx):
        if not hasattr(ctx, "terms"):
            ctx.terms = {}
        return ctx.terms

    def run(self, args, ctx):
        action = need(args, "action")
        terms = self._sessions(ctx)
        wait = max(0.2, min(float(args.get("wait") or 2), 30))
        if action == "list":
            return Result("\n".join("%s  %s  (%s)" % (i, t.command or "shell", "running" if t.alive else "exit %s" % t.exit_code) for i, t in terms.items()) or "(no terminals)",
                          summary="%d terminal%s" % (len(terms), "" if len(terms) == 1 else "s"))
        if action == "start":
            if sum(1 for t in terms.values() if t.alive) >= MAX_SESSIONS:
                raise ToolError("%d terminals are open already. Stop one first." % MAX_SESSIONS)
            env = dict(os.environ, TERM="xterm-256color", PAGER="cat", LANG=os.environ.get("LANG", "C.UTF-8"))
            sess = PtySession(str(args.get("command") or ""), getattr(ctx, "shell_cwd", None) or ctx.cwd, 24, 100, env)
            tid = "term_%d" % (len(terms) + 1)
            terms[tid] = sess
            sess.wait_quiet(wait)
            return self._result(tid, sess, "Started")
        tid = need(args, "id")
        sess = terms.get(tid)
        if sess is None:
            raise ToolError("No terminal %s. Open: %s" % (tid, ", ".join(terms) or "none"))
        if action == "send":
            sess.send(need(args, "keys"))
            sess.wait_quiet(wait)
            return self._result(tid, sess, "Sent")
        if action == "read":
            sess.wait_quiet(min(wait, 1.0))
            return self._result(tid, sess, "Read")
        if action == "stop":
            sess.stop()
            terms.pop(tid, None)
            return Result("Stopped %s." % tid, summary="Stopped")
        raise ToolError("action must be start, send, read, stop or list.")

    def _result(self, tid, sess, verb):
        screen = sess.text()
        state = "running" if sess.alive else "finished (exit code %s)" % sess.exit_code
        return Result("[%s: %s]\n%s" % (tid, state, screen or "(blank screen)"), summary="%s %s · %s" % (verb, tid, state), preview=screen.split("\n")[-5:] if screen else [])
