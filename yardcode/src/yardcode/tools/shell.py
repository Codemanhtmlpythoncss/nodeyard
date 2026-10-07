"""Running things: Bash (with background shells), BashOutput, KillShell and a persistent Python interpreter."""
import atexit
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .. import util
from .base import Result, Tool, ToolError, need


def _env(ctx):
    env = dict(os.environ)
    env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive", "NO_COLOR": "1",
                "TERM": "dumb", "YARDCODE": "1"})
    for k, v in (ctx.settings.get("env") or {}).items():
        env[str(k)] = str(v)
    return env


def kill_group(proc, sig=signal.SIGTERM):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


class Shell:
    """One running command whose output is collected by a reader thread."""

    def __init__(self, ident, command, ctx, cwd):
        self.id, self.command = ident, command
        self.lines = []
        self.pos = 0
        self.lock = threading.Lock()
        self.started = time.time()
        self.rc = None
        fd, self.cwdfile = tempfile.mkstemp(prefix="yc-cwd-")
        os.close(fd)
        script = 'trap \'pwd -P >"$YC_CWDFILE" 2>/dev/null\' EXIT\n' + command + "\n"
        env = _env(ctx)
        env["YC_CWDFILE"] = self.cwdfile
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        self.proc = subprocess.Popen([shell, "-c", script], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        buf = b""
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            *whole, buf = buf.split(b"\n")
            if whole:
                with self.lock:
                    self.lines.extend(l.decode("utf-8", "replace").rstrip("\r") for l in whole)
        if buf:
            with self.lock:
                self.lines.append(buf.decode("utf-8", "replace"))
        self.rc = self.proc.wait()
        try:
            self.proc.stdout.close()
        except OSError:
            pass

    @property
    def done(self):
        return self.rc is not None and not self.thread.is_alive()

    def new_output(self):
        with self.lock:
            out = self.lines[self.pos:]
            self.pos = len(self.lines)
        return out

    def all_output(self):
        with self.lock:
            return list(self.lines)

    def last_line(self):
        with self.lock:
            for l in reversed(self.lines):
                if l.strip():
                    return l.strip()
        return ""

    def final_cwd(self):
        try:
            with open(self.cwdfile) as f:
                return f.read().strip()
        except OSError:
            return ""
        finally:
            try:
                os.unlink(self.cwdfile)
            except OSError:
                pass

    def kill(self):
        if self.rc is None:
            kill_group(self.proc, signal.SIGTERM)
            for _ in range(20):
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            if self.proc.poll() is None:
                kill_group(self.proc, signal.SIGKILL)
        self.thread.join(timeout=1)


def _cleanup(ctx):
    for sh in list(ctx.shells.values()):
        sh.kill()


class Bash(Tool):
    name = "Bash"
    kind = "exec"
    read_only = False
    description = "Run a bash command; the working directory persists; no keyboard input; 120 s timeout (max 900), run_in_background for servers."
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "description": "The command to run"},
        "description": {"type": "string", "description": "A few words on what it does"},
        "timeout": {"type": "integer", "description": "Seconds before it is stopped (default 120, max 900)"},
        "run_in_background": {"type": "boolean", "description": "Start it and return at once; read output with BashOutput"}}, "required": ["command"]}

    def specifier(self, args, ctx):
        return args.get("command", "")

    def summary(self, args, ctx):
        cmd = (args.get("command", "") or "").strip().split("\n")[0]
        return "Bash(%s)" % (cmd if len(cmd) <= 110 else cmd[:107] + "...")

    def run(self, args, ctx):
        command = need(args, "command")
        if not command.strip():
            raise ToolError("The command is empty.")
        cwd = getattr(ctx, "shell_cwd", None) or ctx.cwd
        if not os.path.isdir(cwd):
            cwd = ctx.cwd
        shells = ctx.shells
        sid = "bash_%d" % (len(shells) + 1)
        if not hasattr(ctx, "shell_cwd"):
            ctx.shell_cwd = cwd
        if args.get("run_in_background"):
            sh = Shell(sid, command, ctx, cwd)
            shells[sid] = sh
            atexit.register(sh.kill)
            return Result("Started in the background as %s. Read its output with BashOutput (bash_id=%s); stop it with KillShell." % (sid, sid),
                          summary="Running in the background (%s)" % sid)
        timeout = max(1, min(int(args.get("timeout") or ctx.settings.get("bash_timeout", 120) or 120), 900))
        sh = Shell(sid, command, ctx, cwd)
        shells[sid] = sh
        t0 = time.time()
        timed_out = interrupted = False
        last_line = None
        try:
            while not sh.done:
                time.sleep(0.05)
                if getattr(ctx, "progress", None):
                    line = sh.last_line()
                    if line != last_line:
                        last_line = line
                        ctx.progress(line)
                if time.time() - t0 > timeout:
                    timed_out = True
                    break
                if ctx.interrupted():
                    interrupted = True
                    break
        except KeyboardInterrupt:
            sh.kill()
            shells.pop(sid, None)
            raise
        if timed_out or interrupted:
            sh.kill()
        else:
            sh.thread.join(timeout=2)
        lines = sh.all_output()
        new_cwd = sh.final_cwd()
        shells.pop(sid, None)
        note = ""
        if new_cwd and os.path.isdir(new_cwd) and new_cwd != cwd:
            if ctx.inside(new_cwd):
                ctx.shell_cwd = new_cwd
                note = "\n[working directory is now %s]" % util.shorten_path(new_cwd, ctx.cwd)
            else:
                ctx.shell_cwd = ctx.cwd
                note = "\n[the shell moved outside the project; the next command starts in %s again]" % ctx.cwd
        text = "\n".join(lines).rstrip("\n")
        text = util.truncate_middle(text, ctx.limit(), "characters")
        rc = sh.rc if sh.rc is not None else -1
        took = time.time() - t0
        if timed_out:
            text += "\n[stopped: it ran longer than %d s. For long jobs use run_in_background.]" % timeout
        elif interrupted:
            text += "\n[interrupted by the user]"
        elif rc != 0:
            text += "\n[exit code %d]" % rc
        text = (text + note).strip() or "(no output)"
        shown = [l for l in lines if l.strip()]
        summ = "Ran for %s" % (("%.1fs" % took) if took < 60 else util.human_duration(took))
        if rc != 0 and not (timed_out or interrupted):
            summ += " · exit code %d" % rc
        return Result(text, error=(rc != 0 or timed_out), summary=summ, preview=shown[:6] if rc == 0 else shown[-6:], meta={"lines": len(shown)})


class BashOutput(Tool):
    name = "BashOutput"
    kind = "read"
    description = "New output of a background shell."
    parameters = {"type": "object", "properties": {
        "bash_id": {"type": "string", "description": "The id Bash returned, e.g. bash_1"},
        "filter": {"type": "string", "description": "Only lines matching this regular expression"}}, "required": ["bash_id"]}

    def run(self, args, ctx):
        sid = need(args, "bash_id")
        sh = ctx.shells.get(sid)
        if not sh:
            raise ToolError("No background shell %s. Running: %s" % (sid, ", ".join(ctx.shells) or "none"))
        lines = sh.new_output()
        if args.get("filter"):
            try:
                rx = re.compile(args["filter"])
            except re.error as e:
                raise ToolError("Bad filter: %s" % e)
            lines = [l for l in lines if rx.search(l)]
        state = "finished (exit code %s)" % sh.rc if sh.done else "still running"
        text = util.truncate_middle("\n".join(lines), ctx.limit(), "characters")
        return Result("[%s: %s]\n%s" % (sid, state, text or "(no new output)"), summary="%d new line%s, %s" % (len(lines), "" if len(lines) == 1 else "s", state))


class KillShell(Tool):
    name = "KillShell"
    kind = "exec"
    read_only = False
    description = "Stop a background shell."
    parameters = {"type": "object", "properties": {"shell_id": {"type": "string", "description": "The id Bash returned, e.g. bash_1"}}, "required": ["shell_id"]}

    def specifier(self, args, ctx):
        return args.get("shell_id", "")

    def run(self, args, ctx):
        sid = need(args, "shell_id")
        sh = ctx.shells.get(sid)
        if not sh:
            raise ToolError("No background shell %s." % sid)
        sh.kill()
        ctx.shells.pop(sid, None)
        return Result("Stopped %s." % sid, summary="Stopped %s" % sid)


# ---- Python: a persistent interpreter ("code interpreter") ---------------------------------------------

WORKER = r'''
import ast, contextlib, io, json, os, sys, traceback
proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
g = {"__name__": "__main__"}
def run(code):
    out = io.StringIO()
    try:
        tree = ast.parse(code, "<cell>", "exec")
        last = ast.Expression(tree.body.pop().value) if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(tree, "<cell>", "exec"), g)
            if last is not None:
                v = eval(compile(last, "<cell>", "eval"), g)
                if v is not None:
                    print(repr(v))
        return {"out": out.getvalue(), "error": ""}
    except SystemExit as e:
        return {"out": out.getvalue(), "error": "SystemExit(%s)" % (e.code,)}
    except BaseException:
        tb = traceback.format_exc().split("\n")
        tb = [l for l in tb if "yc_worker" not in l and "<cell>" in l or not l.startswith("  File")]
        return {"out": out.getvalue(), "error": "\n".join(tb).strip()}
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    proto.write("\x00YC" + json.dumps(run(req.get("code", ""))) + "\n")
    proto.flush()
'''


class PythonWorker:
    def __init__(self, ctx):
        env = _env(ctx)
        env["MPLBACKEND"] = "Agg"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen([sys.executable, "-c", WORKER], cwd=getattr(ctx, "shell_cwd", None) or ctx.cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        atexit.register(self.stop)

    def stop(self):
        if self.proc.poll() is None:
            kill_group(self.proc, signal.SIGKILL)

    def run(self, code, timeout, ctx):
        try:
            self.proc.stdin.write((json.dumps({"code": code}) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            return None, "restart"
        fd = self.proc.stdout.fileno()
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            if ctx.interrupted():
                self.stop()
                return None, "interrupted"
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None, "died"
                buf += chunk
                if b"\n" in buf:
                    line = buf.split(b"\n", 1)[0]
                    i = line.find(b"\x00YC")
                    try:
                        return json.loads(line[i + 3:].decode("utf-8", "replace")), ""
                    except ValueError:
                        return None, "garbled"
        self.stop()
        return None, "timeout"


class Python(Tool):
    name = "Python"
    kind = "exec"
    read_only = False
    description = "Run Python 3 in a persistent interpreter (state kept between calls, like a notebook); the last expression is printed."
    parameters = {"type": "object", "properties": {
        "code": {"type": "string", "description": "Python code to run"},
        "timeout": {"type": "integer", "description": "Seconds before it is stopped (default 60, max 600)"}}, "required": ["code"]}

    def specifier(self, args, ctx):
        return args.get("code", "")

    def summary(self, args, ctx):
        first = (args.get("code", "") or "").strip().split("\n")[0]
        return "Python(%s)" % (first if len(first) <= 100 else first[:97] + "...")

    def run(self, args, ctx):
        code = need(args, "code")
        timeout = max(1, min(int(args.get("timeout") or 60), 600))
        t0 = time.time()
        if ctx.python is None or ctx.python.proc.poll() is not None:
            ctx.python = PythonWorker(ctx)
        res, why = ctx.python.run(code, timeout, ctx)
        if res is None:
            ctx.python = None
            msg = {"timeout": "The code ran longer than %d s and was stopped. The interpreter restarted, so earlier variables are gone." % timeout,
                   "interrupted": "Interrupted by the user. The interpreter restarted, so earlier variables are gone."}.get(
                       why, "The interpreter stopped (%s). It will restart on the next call; earlier variables are gone." % why)
            return Result(msg, error=True, summary="Stopped")
        out = util.truncate_middle(res.get("out", "").rstrip("\n"), ctx.limit(), "characters")
        err = res.get("error", "")
        text = out
        if err:
            text = (out + "\n" if out else "") + err
        lines = [l for l in text.split("\n") if l.strip()]
        return Result(text or "(no output)", error=bool(err), summary="Ran for %.1fs%s" % (time.time() - t0, " · error" if err else ""),
                      preview=lines[-6:] if err else lines[:6])


SHELL_TOOLS = [Bash, BashOutput, KillShell, Python]
_ = json
