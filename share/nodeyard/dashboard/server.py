#!/usr/bin/env python3
"""The nodeyard dashboard: a web page about your cluster.

    server.py [--port 9092] [--listen local,100.64.0.1] [--password-file FILE]
              [--kubeconfig FILE | --demo] [--interval 5]

Standard library only. By default it listens on this machine only (127.0.0.1)
and needs no sign-in. To listen on another address (for example your
Tailscale address) it requires --password-file: every request must then carry a
session from the sign-in page. It looks at the cluster through its API and only
ever reads from it; the AI features (chat, running models) are the exception
and need that sign-in.
"""
import argparse
import http.cookies
import importlib.util
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
sys.path.insert(0, HERE)

import auth as authmod  # noqa: E402
import kube  # noqa: E402
from analysis import alerts, sample, totals  # noqa: E402

HISTORY_POINTS = 720  # an hour at the default 5 s
LOOPBACK = ("127.0.0.1", "::1", "localhost")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
MAX_BODY = 1024 * 1024
MAX_STREAMS = 24
MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".json": "application/json", ".png": "image/png", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".webp": "image/webp",
}
# Needed to show the sign-in page; everything else waits for a session.
PUBLIC_FILES = ("/login", "/login.html", "/login.js", "/style.css", "/theme.js")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, source, interval, agents=None):
        self.source = source
        self.agents = agents
        self.interval = interval
        self.lock = threading.Lock()
        self.poll_lock = threading.Lock()
        self.stop = threading.Event()
        self.state = None
        self.totals = None
        self.alerts = []
        self.error = None
        self.updated = 0.0
        self.history = []
        self.cond = threading.Condition()
        self.version = 0
        if hasattr(source, "seed_history"):
            self.history = source.seed_history(HISTORY_POINTS, interval)

    def poll(self):
        if not self.poll_lock.acquire(blocking=False):
            return
        try:
            try:
                state = self.source.collect()
                if self.agents is not None:
                    self.agents.annotate(state)
                # (after the agents: the GPU's share of a loading model is read on the card)
                kube.gpu_progress(state)
                if hasattr(self.source, "load_eta"):
                    self.source.load_eta(state)
                tot, al = totals(state), alerts(state)
            except Exception as e:  # keep serving the last good snapshot
                with self.lock:
                    self.error = str(e)
                return
            now = time.time()
            with self.lock:
                self.state, self.totals, self.alerts, self.error, self.updated = state, tot, al, None, now
                self.history.append(sample(state, now))
                del self.history[:-HISTORY_POINTS]
            with self.cond:
                self.version += 1
                self.cond.notify_all()
        finally:
            self.poll_lock.release()

    def loop(self):
        while not self.stop.is_set():
            started = time.time()
            self.poll()
            self.stop.wait(max(0.2, self.interval - (time.time() - started)))
        with self.cond:
            self.cond.notify_all()

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "totals": self.totals, "alerts": self.alerts, "error": self.error, "updated": self.updated}

    def history_slice(self, points):
        with self.lock:
            return self.history[-points:]


# --------------------------------------------------------------------------- http

def state_response(srv, with_agents=False):
    snap = srv.store.snapshot()
    snap.update({"ok": snap["state"] is not None, "version": srv.nd_version, "mode": srv.mode, "auth": srv.auth is not None,
                 "interval": srv.store.interval, "now": time.time(), "host": socket.gethostname()})
    if with_agents and snap["state"] is not None:
        agents = srv.store.agents
        snap["agents_full"] = {"ok": True, "installed": bool(snap["state"].get("agents", {}).get("installed")),
                               "nodes": agents.payload(snap["state"]) if agents is not None else {}}
    return snap


class Handler(BaseHTTPRequestHandler):
    server_version = "nodeyard-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ------------------------------------------------------------

    @property
    def ctx(self):
        return self.server.ctx

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, separators=(",", ":")), "application/json; charset=utf-8", extra)

    def _redirect(self, where):
        self._send(302, "", "text/plain; charset=utf-8", {"Location": where})

    # Without a sign-in, only answer requests addressed to this machine: stops a web page
    # on the internet from using your browser to talk to a loopback-only dashboard (DNS rebinding).
    def _host_ok(self):
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
        return name in LOOPBACK

    # Tailscale Funnel (`nodeyard public on`) hands internet requests to us from 127.0.0.1
    # and says who really sent them. Only a loopback peer is believed, and only for
    # counting failed sign-ins and building links, never for deciding who is signed in.
    def _forwarded(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return None
        fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return fwd or None

    def _https(self):
        return self._forwarded() is not None and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            c = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        m = c.get(authmod.COOKIE)
        return m.value if m else None

    def _authed(self):
        a = self.ctx.auth
        return a is None or a.valid(self._token())

    def _gate(self, public=False):
        """True if the request may go on; otherwise the answer has been sent."""
        v1 = getattr(self.ctx, "v1_gate", None)
        if v1 is not None and self.path.startswith("/api/v1/"):
            return v1(self)    # the control API: the server API key (or a signed-in session)
        if self.ctx.auth is None:
            if not self._host_ok():
                self._json({"ok": False, "error": "This dashboard only answers on localhost."}, 403)
                return False
            return True
        if public or self._authed():
            return True
        if self.path.startswith("/api/"):
            self._json({"ok": False, "error": "Sign in first.", "auth": True}, 401)
        else:
            self._redirect("/login")
        return False

    def _body(self, limit=MAX_BODY):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            self._json({"ok": False, "error": "Request too large."}, 413)
            return None
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "Not valid JSON."}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"ok": False, "error": "Expected a JSON object."}, 400)
            return None
        return data

    # A web page on another site can't send these (it would need permission we never give).
    def _csrf_ok(self):
        if self.headers.get("X-Nodeyard") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json({"ok": False, "error": "Missing request headers."}, 403)
            return False
        origin = self.headers.get("Origin")
        hosts = {self.headers.get("Host", "")}
        if self._forwarded() is not None and self.headers.get("X-Forwarded-Host"):
            hosts.add(self.headers.get("X-Forwarded-Host"))  # the public name, through Tailscale Funnel
        if origin and urllib.parse.urlparse(origin).netloc not in hosts:
            self._json({"ok": False, "error": "Cross-site request refused."}, 403)
            return False
        return True

    # -- GET -----------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if not self._gate(public=path in PUBLIC_FILES or path in ("/api/health", "/api/auth")):
            return
        srv = self.ctx
        try:
            if path == "/api/health":
                return self._json({"ok": True})
            if path == "/api/auth":
                return self._json({"ok": True, "enabled": srv.auth is not None, "authenticated": self._authed()})
            if path == "/api/state":
                if q.get("fresh") and time.time() - srv.store.updated > 2:
                    srv.store.poll()
                return self._json(state_response(srv))
            if path == "/api/stream":
                return self._stream(q)
            if path == "/api/history":
                try:
                    n = max(2, min(HISTORY_POINTS, int(q.get("points", ["360"])[0])))
                except ValueError:
                    n = 360
                return self._json({"ok": True, "interval": srv.store.interval, "points": srv.store.history_slice(n)})
            if path == "/api/logs":
                return self._logs(q)
            if path == "/api/export":
                snap = srv.store.snapshot()
                snap["exported"] = time.time()
                return self._json(snap, extra={"Content-Disposition": 'attachment; filename="nodeyard-snapshot.json"'})
            for route, fn in srv.get_routes.items():
                if path == route:
                    return fn(self, q)
            if path == "/login":
                path = "/login.html"
            if srv.auth is not None and self._authed() and path == "/login.html":
                return self._redirect("/")
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:  # never leak a traceback to the page
            return self._json({"ok": False, "error": str(e)}, 500)

    def _stream(self, q):
        """Server-sent events: the whole snapshot each time the dashboard reads the cluster."""
        srv = self.ctx
        with srv.lock:
            if srv.streams >= MAX_STREAMS:
                return self._json({"ok": False, "error": "Too many dashboards are open."}, 429)
            srv.streams += 1
        want_agents = bool(q.get("agents"))
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                         ("X-Accel-Buffering", "no"), ("Connection", "close"), ("Content-Security-Policy", CSP)):
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            store, last = srv.store, -1
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not store.stop.is_set():
                with store.cond:
                    store.cond.wait_for(lambda: store.version != last or store.stop.is_set(), timeout=15)
                    version = store.version
                if store.stop.is_set():
                    break
                if not self._authed():
                    self.wfile.write(b"event: auth\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                if version != last:
                    last = version
                    body = json.dumps(state_response(srv, want_agents), separators=(",", ":"))
                    self.wfile.write(("event: state\ndata: %s\n\n" % body).encode("utf-8"))
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with srv.lock:
                srv.streams -= 1

    def _logs(self, q):
        ns, pod = q.get("ns", [""])[0], q.get("pod", [""])[0]
        container = q.get("container", [""])[0]
        if not (NAME_RE.match(ns) and NAME_RE.match(pod) and (not container or NAME_RE.match(container))):
            return self._json({"ok": False, "error": "Bad namespace, pod or container name."}, 400)
        try:
            lines = max(1, min(2000, int(q.get("lines", ["200"])[0])))
        except ValueError:
            lines = 200
        try:
            text = self.ctx.source.logs(ns, pod, container, lines)
        except kube.KubeError as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        return self._json({"ok": True, "text": text})

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(WEB, path.lstrip("/")))
        if not (full == WEB or full.startswith(WEB + os.sep)) or not os.path.isfile(full):
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    # -- POST ----------------------------------------------------------------

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        srv = self.ctx
        if path == "/api/login":
            return self._login()
        if not self._gate():
            return
        if path == "/api/logout":
            if not self._csrf_ok():
                return
            if srv.auth is not None:
                srv.auth.logout(self._token())
            return self._json({"ok": True}, extra={"Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % authmod.COOKIE})
        fn = srv.post_routes.get(path)
        if fn is None:
            return self._json({"ok": False, "error": "Nothing here accepts that kind of request."}, 405, {"Allow": "GET, HEAD"})
        if not (getattr(self, "v1_bearer", False) or self._csrf_ok()):   # (a key in the header can't be forged by another site)
            return
        body = self._body(srv.post_limits.get(path, MAX_BODY))
        if body is None:
            return
        try:
            return fn(self, body)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def _login(self):
        srv = self.ctx
        if srv.auth is None:
            return self._json({"ok": True, "enabled": False})
        if not self._csrf_ok():
            return
        fwd = self._forwarded()
        ip = ("internet:" + fwd) if fwd else self.client_address[0]
        wait = srv.auth.retry_after(ip, public=fwd is not None)
        if wait:
            return self._json({"ok": False, "error": "Too many wrong passwords. Try again in %d seconds." % wait, "wait": wait}, 429, {"Retry-After": str(wait)})
        body = self._body()
        if body is None:
            return
        token = srv.auth.login(ip, str(body.get("password", ""))[:200], public=fwd is not None)
        if token is None:
            sys.stderr.write("dashboard: wrong password from %s\n" % ip)
            sys.stderr.flush()
            time.sleep(0.4)
            return self._json({"ok": False, "error": "That isn't the right password."}, 401)
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (authmod.COOKIE, token, authmod.SESSION_SECONDS, "; Secure" if self._https() else "")
        return self._json({"ok": True}, extra={"Set-Cookie": cookie})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._json(
        {"ok": False, "error": "Nothing here accepts that kind of request."}, 405, {"Allow": "GET, HEAD, POST"})


class Context:
    """What every listener shares."""

    def __init__(self, **kw):
        self.get_routes, self.post_routes, self.post_limits = {}, {}, {}
        self.lock = threading.Lock()
        self.streams = 0
        self.__dict__.update(kw)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def make_listener(addr, port, ctx, verbose):
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET

    class S(Server):
        address_family = family

    srv = S((addr, port), Handler, bind_and_activate=False)
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if addr not in ("127.0.0.1", "::1") and family == socket.AF_INET:
        # Lets the service start before the Tailscale interface is up.
        try:
            srv.socket.setsockopt(socket.IPPROTO_IP, 15, 1)  # IP_FREEBIND
        except OSError:
            pass
    srv.server_bind()
    srv.server_activate()
    srv.ctx, srv.verbose = ctx, verbose
    return srv


def parse_listen(spec):
    """'local,100.64.0.1' -> ['127.0.0.1', '100.64.0.1']; 'all' -> ['0.0.0.0']."""
    out = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        if item == "local":
            out.append("127.0.0.1")
        elif item == "all":
            out.append("0.0.0.0")
        else:
            try:
                out.append(str(ipaddress.ip_address(item)))
            except ValueError:
                sys.exit("--listen: '%s' isn't local, all or an IP address." % item)
    return list(dict.fromkeys(out)) or ["127.0.0.1"]


def main():
    ap = argparse.ArgumentParser(description="nodeyard dashboard")
    ap.add_argument("--port", type=int, default=9092)
    ap.add_argument("--listen", default="local", help="comma list of: local, all, or IP addresses")
    ap.add_argument("--password-file", default="", help="sign-in password; required to listen on anything but this machine")
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"))
    ap.add_argument("--demo", action="store_true", help="show a simulated cluster")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between cluster reads")
    ap.add_argument("--cluster-name", default="")
    ap.add_argument("--agent-token-file", default="", help="token the node agents expect (processes, CPU clocks, temperatures)")
    ap.add_argument("--ai-key-file", default="", help="the server API key file (chat uses it; it never reaches the browser)")
    ap.add_argument("--nodeyard-bin", default="", help="nodeyard itself, so signed-in users can run and remove models from the page")
    ap.add_argument("--nodeyard-version", default="")
    ap.add_argument("--terminal-user", default="", help="the account the Terminal page's shell runs as (never root); empty = no terminal")
    ap.add_argument("--state-dir", default="/var/lib/nodeyard/dashboard", help="where the page's own settings and background picture live")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if not 0 <= a.port <= 65535:
        sys.exit("The port must be between 0 and 65535.")
    a.interval = max(1.0, min(300.0, a.interval))
    addrs = parse_listen(a.listen)
    remote = [x for x in addrs if x not in ("127.0.0.1", "::1")]

    auth = None
    if a.password_file:
        try:
            with open(a.password_file, "r", encoding="utf-8") as f:
                auth = authmod.Auth(f.read())
        except (OSError, ValueError) as e:
            sys.exit("Cannot use the dashboard password in %s: %s" % (a.password_file, getattr(e, "strerror", None) or e))
    if remote and auth is None:
        sys.exit("Listening on %s needs a sign-in password (--password-file): without one anybody who can reach "
                 "the address could read your cluster. Use `nodeyard dashboard start`, which sets one up." % ", ".join(remote))
    if remote and a.demo:
        sys.exit("The demo has no sign-in, so it only listens on this machine.")

    if a.demo:
        import demo
        source = demo.DemoSource()
    else:
        try:
            source = kube.KubeSource(a.kubeconfig, a.cluster_name, a.interval)
        except kube.KubeError as e:
            sys.exit("Cannot connect to the cluster: %s" % e)

    if a.demo:
        agents = source.agents
    else:
        import agents as agentsmod
        agents = agentsmod.AgentPoller(a.agent_token_file)
    store = Store(source, a.interval, agents)
    ctx = Context(store=store, source=source, mode="demo" if a.demo else "live", nd_version=a.nodeyard_version, auth=auth)

    def agents_route(h, q):
        snap = store.snapshot()
        if not snap["state"]:
            return h._json({"ok": False, "error": "Not ready yet."}, 503)
        node = q.get("node", [""])[0]
        h._json({"ok": True, "installed": bool(snap["state"].get("agents", {}).get("installed")), "nodes": agents.payload(snap["state"], node or None)})
    ctx.get_routes["/api/agents"] = agents_route
    if importlib.util.find_spec("aiapi"):
        import aiapi
        aiapi.register(ctx, a)
        import controlapi
        controlapi.register(ctx, a)
        import agentapi
        agentapi.register(ctx, a)
        import chatsapi
        chatsapi.register(ctx, a)
        import updateapi
        updateapi.register(ctx, a)
    import settings
    settings.register(ctx, a)
    if getattr(ctx, "ai", None) is not None:
        import lifecycle
        lifecycle.register(ctx, a)
        import research
        research.register(ctx, a)
    import terminal
    terminal.register(ctx, a)

    servers = []
    try:
        port = a.port
        for addr in addrs:
            srv = make_listener(addr, port, ctx, a.verbose)
            port = srv.server_address[1]  # --port 0: every listener uses the first one's port
            servers.append(srv)
    except OSError as e:
        sys.exit("Cannot listen on %s:%d: %s" % (addr, port, e.strerror or e))

    threading.Thread(target=store.loop, daemon=True).start()

    def shutdown(*_):
        store.stop.set()
        with store.cond:
            store.cond.notify_all()
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for s in servers:
        host = s.server_address[0]
        print("nodeyard dashboard listening on http://%s:%d (%s data, %s, refreshing every %gs)" % (
            "[%s]" % host if ":" in host else host, s.server_address[1], ctx.mode,
            "sign-in required" if auth else "no sign-in, this machine only", a.interval), flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    for s in servers:
        s.server_close()


if __name__ == "__main__":
    main()
