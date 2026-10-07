"""A shell on the server, in the dashboard (the Terminal page).

Safety rules, all enforced here (not in the page):
  - only over Tailscale or your own network: requests that came in through Tailscale
    Funnel (the public internet) are always refused;
  - the dashboard password is asked again to open a terminal (a stolen session
    cookie alone isn't enough), and wrong ones count towards the sign-in lockout;
  - the shell runs as an ordinary user (--terminal-user, set by `nodeyard dashboard
    start` to whoever ran it with sudo), never as root, and outside the dashboard's
    own sandbox, through systemd;
  - a terminal belongs to the sign-in session that opened it; idle ones close after
    30 minutes, and at most 4 are open at once.

Output goes to the page as server-sent events; keystrokes come back as small POSTs.
"""
import base64
import fcntl
import json
import os
import pty
import pwd
import re
import secrets
import select
import signal
import struct
import subprocess
import termios
import threading
import time

MAX_SESSIONS = 4
IDLE_SECONDS = 30 * 60
KEEP_BYTES = 256 * 1024
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class TermError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Session:
    def __init__(self, sid, owner, argv, cols, rows):
        self.id, self.owner = sid, owner
        self.buf = bytearray()
        self.base = 0          # offset of buf[0] in the whole output
        self.cond = threading.Condition()
        self.alive = True
        self.last = time.time()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: become the shell
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        self.resize(cols, rows)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    continue
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.cond:
                self.buf += data
                if len(self.buf) > KEEP_BYTES:
                    cut = len(self.buf) - KEEP_BYTES
                    del self.buf[:cut]
                    self.base += cut
                self.cond.notify_all()
        with self.cond:
            self.alive = False
            self.cond.notify_all()
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def write(self, data):
        self.last = time.time()
        if not self.alive:
            raise TermError("That terminal has closed.", 410)
        try:
            os.write(self.fd, data)
        except OSError:
            raise TermError("That terminal has closed.", 410)

    def resize(self, cols, rows):
        cols, rows = max(10, min(500, int(cols))), max(4, min(200, int(rows)))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def read_from(self, offset):
        """(bytes since offset, new offset, alive)"""
        with self.cond:
            start = max(offset, self.base)
            return bytes(self.buf[start - self.base:]), self.base + len(self.buf), self.alive

    def close(self):
        self.alive = False
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass


class Terminals:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.user = getattr(args, "terminal_user", "") or ""
        self.lock = threading.Lock()
        self.sessions = {}
        threading.Thread(target=self._reaper, daemon=True).start()

    def available(self):
        if self.demo:
            return True, ""
        if not self.user or not USER_RE.match(self.user):
            return False, "No terminal user is set. Restart the dashboard with sudo from your own account: sudo nodeyard dashboard start"
        try:
            pwd.getpwnam(self.user)
        except KeyError:
            return False, "The terminal user %s doesn't exist on this machine." % self.user
        return True, ""

    def _argv(self):
        if self.demo:  # never a real shell in the demo
            return ["/bin/sh", "-c", 'printf "\\033[1;36mDemo terminal\\033[0m: nothing you type is run.\\r\\n"; '
                    'while IFS= read -r l; do printf "\\033[33m(demo)\\033[0m would run: %s\\r\\n" "$l"; done']
        u = pwd.getpwnam(self.user)
        shell = u.pw_shell if u.pw_shell and os.path.exists(u.pw_shell) and not u.pw_shell.endswith(("nologin", "false")) else "/bin/bash"
        # systemd starts it outside the dashboard's sandbox, as that user, in their home
        return ["systemd-run", "--quiet", "--collect", "--pty", "--uid=" + self.user, "--setenv=TERM=xterm-256color",
                "-p", "WorkingDirectory=~", "--description=nodeyard dashboard terminal", shell, "-l"]

    def _reaper(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                for sid, s in list(self.sessions.items()):
                    if not s.alive or now - s.last > IDLE_SECONDS:
                        s.close()
                        if not s.alive:
                            self.sessions.pop(sid, None)

    def open(self, handler, body):
        ok, why = self.available()
        if not ok:
            raise TermError(why, 503)
        if handler._forwarded() is not None:
            raise TermError("The terminal only works over Tailscale or your own network, never through public access.", 403)
        auth = self.ctx.auth
        if auth is not None:
            ip = handler.client_address[0]
            wait = auth.retry_after(ip)
            if wait:
                raise TermError("Too many wrong passwords. Try again in %d seconds." % wait, 429)
            if auth.login(ip, str(body.get("password", ""))[:200]) is None:
                time.sleep(0.4)
                raise TermError("That isn't the right password.", 401)
        with self.lock:
            mine = [s for s in self.sessions.values() if s.alive]
            if len(mine) >= MAX_SESSIONS:
                raise TermError("%d terminals are open already. Close one first." % MAX_SESSIONS, 429)
            sid = secrets.token_hex(12)
            s = Session(sid, handler._token() or "local", self._argv(), body.get("cols", 100), body.get("rows", 30))
            self.sessions[sid] = s
        return {"id": sid, "user": self.user or os.environ.get("USER", "")}

    def get(self, handler, sid):
        with self.lock:
            s = self.sessions.get(str(sid))
        if s is None or s.owner != (handler._token() or "local"):
            raise TermError("No such terminal.", 404)
        if handler._forwarded() is not None:
            raise TermError("The terminal only works over Tailscale or your own network.", 403)
        return s

    def stream(self, handler, q):
        try:
            s = self.get(handler, q.get("id", [""])[0])
            offset = int(q.get("from", ["0"])[0] or 0)
        except (TermError, ValueError) as e:
            return handler._json({"ok": False, "error": str(e)}, getattr(e, "code", 400))
        handler.send_response(200)
        for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                     ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
            handler.send_header(k, v)
        handler.end_headers()
        handler.close_connection = True
        try:
            handler.wfile.write(b"retry: 1000\n\n")
            while True:
                with s.cond:
                    s.cond.wait_for(lambda: s.base + len(s.buf) > offset or not s.alive, timeout=15)
                data, offset, alive = s.read_from(offset)
                if data:
                    msg = json.dumps({"d": base64.b64encode(data).decode(), "o": offset})
                    handler.wfile.write(("event: out\ndata: %s\n\n" % msg).encode())
                elif alive:
                    handler.wfile.write(b": ping\n\n")
                if not alive and not data:
                    handler.wfile.write(b"event: exit\ndata: {}\n\n")
                    handler.wfile.flush()
                    return None
                handler.wfile.flush()
                if self.ctx.auth is not None and not handler._authed():
                    return None
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None

    def input(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        data = body.get("data", "")
        if not isinstance(data, str) or len(data) > 65536:
            raise TermError("Bad input.")
        s.write(data.encode("utf-8", "surrogateescape"))
        return {}

    def resize(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        try:
            s.resize(body.get("cols", 80), body.get("rows", 24))
        except (TypeError, ValueError):
            raise TermError("Bad size.")
        return {}

    def close(self, handler, body):
        s = self.get(handler, body.get("id", ""))
        s.close()
        return {}

    def info(self, handler):
        ok, why = self.available()
        return {"available": ok, "why": why, "user": self.user, "public": handler._forwarded() is not None,
                "open": sum(1 for s in self.sessions.values() if s.alive and s.owner == (handler._token() or "local"))}


def register(ctx, args):
    t = Terminals(ctx, args)

    def wrap(fn):
        def route(h, arg):
            try:
                h._json(dict(fn(h, arg) or {}, ok=True))
            except TermError as e:
                h._json({"ok": False, "error": str(e)}, e.code)
        return route

    ctx.get_routes.update({
        "/api/term/info": wrap(lambda h, q: t.info(h)),
        "/api/term/stream": lambda h, q: t.stream(h, q),
    })
    ctx.post_routes.update({
        "/api/term/open": wrap(t.open),
        "/api/term/input": wrap(t.input),
        "/api/term/resize": wrap(t.resize),
        "/api/term/close": wrap(t.close),
    })
    _ = subprocess  # (systemd-run is started through pty.fork + exec)
