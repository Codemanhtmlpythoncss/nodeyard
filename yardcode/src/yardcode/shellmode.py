"""Shell mode: your own shell, inside yardcode.

Type ! on an empty prompt (or run /shell) and the prompt turns into a shell prompt. Every line runs in your real shell ($SHELL, so your
aliases and PATH work) with the real terminal attached: vim, ssh, top, python and sudo all work, colours and Ctrl-C included. Directory
changes (cd) and exported variables carry over from line to line. What a command printed is added to the conversation, so you can say
"fix that" next.

POSIX only (macOS and Linux); anywhere else the line runs through the Bash tool instead.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import tty
    POSIX = True
except ImportError:  # pragma: no cover  (Windows)
    POSIX = False

from . import ui, util

CAPTURE_LIMIT = 400 * 1024
ALT_SCREEN = re.compile(rb"\x1b\[\?(?:1049|1047|47)h")
SKIP_ENV = {"_", "SHLVL", "PWD", "OLDPWD", "COLUMNS", "LINES"}


def available(stdin=None, stdout=None):
    try:
        return POSIX and (stdin or sys.stdin).isatty() and (stdout or sys.stdout).isatty()
    except (AttributeError, ValueError):
        return False


def shell_program(environ=None):
    sh = (environ or os.environ).get("SHELL") or ""
    if sh and os.path.isfile(sh) and os.access(sh, os.X_OK):
        return sh
    return shutil.which("bash") or "/bin/sh"


def clean_output(raw):
    """What the command printed, as plain text: no colour codes, progress bars reduced to their last state, no alternate-screen noise."""
    if ALT_SCREEN.search(raw):
        return "(an interactive full-screen program ran; its screen is not captured)"
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    text = ui.strip_ansi(text)
    lines = []
    for ln in text.split("\n"):
        if "\r" in ln:
            segs = [x for x in ln.split("\r") if x.strip()]
            ln = segs[-1] if segs else ""
        lines.append(ln.rstrip())
    return "\n".join(lines).strip("\n")


def _winsize(fd):
    cols, rows = shutil.get_terminal_size((80, 24))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def relay(argv, cwd, env, capture=True):
    """Run ARGV with this terminal attached through a pty. Returns (exit status, captured output bytes)."""
    master, slave = pty.openpty()
    _winsize(slave)
    got = bytearray()
    old_tty = termios.tcgetattr(0)
    old_winch = signal.getsignal(signal.SIGWINCH)
    resized = []
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
                                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except OSError:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    signal.signal(signal.SIGWINCH, lambda *a: resized.append(1))
    try:
        tty.setraw(0)
        watch_stdin = True
        gone_at = None
        while True:
            if resized:
                del resized[:]
                _winsize(master)
            try:
                ready = select.select([0, master] if watch_stdin else [master], [], [], 0.1)[0]
            except (OSError, ValueError):
                ready = []
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                _write_all(1, data)
                if capture and len(got) < CAPTURE_LIMIT:
                    got += data
            if 0 in ready:
                try:
                    data = os.read(0, 4096)
                except OSError:
                    data = b""
                if data:
                    _write_all(master, data)
                else:
                    watch_stdin = False
            if proc.poll() is not None:
                gone_at = gone_at or time.time()
                if master not in ready and time.time() - gone_at > 0.15:
                    break          # the shell has gone and nothing more is coming (a background job may still hold the pty open)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old_tty)
        signal.signal(signal.SIGWINCH, old_winch)
        try:
            os.close(master)
        except OSError:
            pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except OSError:
                pass
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    return code, bytes(got)


def _write_all(fd, data):
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            select.select([], [fd], [], 0.2)
            continue
        data = data[n:]


class Shell:
    """A shell session that remembers its folder and exported variables from one command to the next."""

    def __init__(self, cwd, environ=None, rc=True):
        self.cwd = cwd
        self.env = dict(environ if environ is not None else os.environ)
        self.rc = rc                         # run interactively so ~/.zshrc / ~/.bashrc (aliases, PATH) apply
        self.status = 0

    def _argv(self, script):
        sh = shell_program(self.env)
        name = os.path.basename(sh)
        interactive = self.rc and name in ("bash", "zsh", "fish", "ksh")
        return ([sh, "-i", "-c", script] if interactive else [sh, "-c", script]), name

    def run(self, command, capture=True):
        """Run one line. Returns (exit status, printed text). The folder and exports it leaves behind are kept."""
        state = tempfile.mkdtemp(prefix="yardcode-shell-")
        cwd_file, env_file = os.path.join(state, "cwd"), os.path.join(state, "env")
        _, name = self._argv("")
        if name == "fish":
            tail = '\nset -l __yc_rc $status\npwd -P > "%s" 2>/dev/null\nenv -0 > "%s" 2>/dev/null\nexit $__yc_rc\n' % (cwd_file, env_file)
        else:
            tail = '\n__yc_rc=$?\npwd -P > "%s" 2>/dev/null\nenv -0 > "%s" 2>/dev/null\nexit $__yc_rc\n' % (cwd_file, env_file)
        argv, _ = self._argv(command + tail)
        env = dict(self.env)
        env["YARDCODE_SHELL"] = "1"
        env.setdefault("TERM", "xterm-256color")
        try:
            code, raw = relay(argv, self.cwd, env, capture=capture)
            self._absorb(cwd_file, env_file)
        finally:
            shutil.rmtree(state, ignore_errors=True)
        self.status = code
        return code, clean_output(raw) if capture else ""

    def interactive(self):
        """Hand the terminal to a full interactive shell until it exits."""
        env = dict(self.env)
        env["YARDCODE_SHELL"] = "1"
        env.setdefault("TERM", "xterm-256color")
        code, _ = relay([shell_program(self.env), "-i"], self.cwd, env, capture=False)
        return code

    def _absorb(self, cwd_file, env_file):
        try:
            with open(cwd_file, "r", encoding="utf-8") as f:
                new = f.read().strip()
            if new and os.path.isdir(new):
                self.cwd = new
        except OSError:
            pass
        try:
            with open(env_file, "rb") as f:
                blob = f.read()
        except OSError:
            return
        env = {}
        for item in blob.split(b"\0"):
            k, sep, v = item.decode("utf-8", "replace").partition("=")
            if sep and k and k not in SKIP_ENV and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                env[k] = v
        if env:
            self.env = env

    def prompt_text(self):
        home = os.path.expanduser("~")
        if self.cwd == home:
            return "~"
        return "~" + self.cwd[len(home):] if self.cwd.startswith(home + os.sep) else self.cwd


def run_line(app, command):
    """A !command line: runs in the shell session, shows its output, and tells the model what happened."""
    S = app.tui.style
    shell = app.shell_session()
    t0 = time.time()
    try:
        code, text = shell.run(command)
    except OSError as e:
        app.tui.error("Couldn't start the shell: %s" % e)
        return
    took = time.time() - t0
    if code:
        app.tui.w(S.muted("  exit status %d" % code) + ("" if took < 2 else S.muted(" · %s" % util.human_duration(took))))
    note = "I ran this in my shell (folder %s):\n$ %s\n%s" % (shell.cwd, command, util.truncate_middle(text, 6000, "characters") or "(no output)")
    if code:
        note += "\n(exit status %d)" % code
    app.session.add({"role": "user", "content": note, "_synthetic": "shell"})
