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
import collections
import hashlib
import http.cookies
import importlib.util
import itertools
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
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
sys.path.insert(0, HERE)

import auth as authmod  # noqa: E402
import tsserve  # noqa: E402
import kube  # noqa: E402
from analysis import alerts, sample, totals  # noqa: E402
from httpcache import COMPRESSIBLE, GZIP_MIN, STATIC, gzip_bytes  # noqa: E402

HISTORY_POINTS = 720  # an hour at the default 5 s
LOOPBACK = ("127.0.0.1", "::1", "localhost")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
MAX_BODY = 1024 * 1024
MAX_STREAMS = 24
STARTED = time.time()
# <script src>/<link href> in the two HTML pages, rewritten to file.js?v=CONTENT-HASH: browsers then keep them for a
# year without asking again, and an update changes the hash, so the new file loads at once.
ASSET_REF = re.compile(r'(\b(?:src|href)=")([A-Za-z0-9_.-]+\.(?:js|css|webmanifest|png))(")')
IMMUTABLE = "private, max-age=31536000, immutable"
# Needed to show the sign-in page; everything else waits for a session.
PUBLIC_FILES = ("/login", "/login.html", "/login.js", "/style.css", "/theme.js", "/manifest.webmanifest", "/icon-180.png", "/icon-512.png")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; frame-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Voice chat needs the microphone; nothing here needs the rest.
PERMISSIONS = "camera=(), geolocation=(), payment=(), usb=(), serial=(), hid=(), bluetooth=(), microphone=(self), fullscreen=(self)"




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
        self.history = collections.deque(maxlen=HISTORY_POINTS)
        self.cond = threading.Condition()
        self.version = 0
        self.alert_hooks = []    # other features' alerts (the service monitor, disk forecasts): fn() -> [alert]
        if hasattr(source, "seed_history"):
            self.history.extend(source.seed_history(HISTORY_POINTS, interval))

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
                for hook in self.alert_hooks:
                    try:
                        al.extend(hook())
                    except Exception:  # one feature's alerts never block the cluster's
                        pass
                order = {"critical": 0, "warning": 1, "info": 2}
                al.sort(key=lambda a: order.get(a.get("level"), 3))
            except Exception as e:  # keep serving the last good snapshot
                with self.lock:
                    self.error = str(e)
                return
            now = time.time()
            with self.lock:
                self.state, self.totals, self.alerts, self.error, self.updated = state, tot, al, None, now
                self.history.append(sample(state, now))
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
            n = len(self.history)
            return list(itertools.islice(self.history, max(0, n - points), n))


# --------------------------------------------------------------------------- http

def state_response(srv, with_agents=False):
    snap = srv.store.snapshot()
    snap.update({"ok": snap["state"] is not None, "version": srv.nd_version, "mode": srv.mode, "auth": srv.auth is not None,
                 "interval": srv.store.interval, "now": time.time(), "host": socket.gethostname()})
    if srv.mode != "demo" and getattr(srv, "port", None):
        snap["https_url"] = tsserve.https_url(srv.port)
    hl = getattr(srv, "homelab", None)
    if hl is not None:   # the service monitor's counts, for the Overview and the Services badge
        snap["monitor"] = hl.summary()
    su = getattr(srv, "selfupdate", None)
    if su is not None:   # the page reloads itself when this changes, and shows a pending update
        snap.update({"build": su.build, "update_pending": su.pending_since is not None})
    if with_agents and snap["state"] is not None:
        agents = srv.store.agents
        snap["agents_full"] = {"ok": True, "installed": bool(snap["state"].get("agents", {}).get("installed")),
                               "nodes": agents.payload(snap["state"]) if agents is not None else {}}
    return snap


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"), check_circular=False)


_PAGES = {}
_PAGES_LOCK = threading.Lock()


def versioned_page(full, page):
    """index.html / login.html with each script and stylesheet as name?v=HASH, rebuilt when the page or any of them changes."""
    refs = ASSET_REF.findall(page["body"].decode("utf-8", "replace"))
    tags = []
    for _a, name, _b in refs:
        try:
            tags.append((name, STATIC.get(os.path.join(WEB, name))["tag"]))
        except OSError:
            tags.append((name, ""))
    key = (page["etag"], tuple(tags))
    with _PAGES_LOCK:
        hit = _PAGES.get(full)
    if hit and hit[0] == key:
        return hit[1]
    lookup = dict(tags)
    text = ASSET_REF.sub(lambda m: m.group(1) + m.group(2) + ("?v=" + lookup[m.group(2)] if lookup.get(m.group(2)) else "") + m.group(3),
                         page["body"].decode("utf-8", "replace"))
    body = text.encode("utf-8")
    tag = hashlib.blake2b(body, digest_size=8).hexdigest()
    entry = {"body": body, "gzip": gzip_bytes(body, 9), "ctype": page["ctype"], "etag": '"%s"' % tag, "tag": tag}
    with _PAGES_LOCK:
        _PAGES[full] = (key, entry)
    return entry


def encoded_snapshot(srv, version, want_agents):
    """The snapshot as JSON, built once per cluster read and shared by every page: each section of the state on its own
    (so a live update can carry only the sections that changed) and the rest. Pods go without their uid (the
    node agents' process names need it, server-side only)."""
    key = (version, want_agents)
    with srv.lock:
        hit = srv.encoded.get(key)
    if hit is not None:
        return hit
    snap = state_response(srv, want_agents)
    state = snap.pop("state", None)
    snap.pop("now", None)
    sections = None
    if state is not None:
        sections = {}
        for k, v in state.items():
            if k == "pods":
                v = [{f: x for f, x in p.items() if f != "uid"} for p in v]
            sections[k] = _dumps(v)
    hit = {"version": version, "sections": sections, "rest": _dumps(snap)}
    with srv.lock:
        srv.encoded = {k: v for k, v in srv.encoded.items() if k[0] >= version - 1}   # this read and the one before
        srv.encoded[key] = hit
    return hit


def snapshot_json(enc, now, only=None):
    """{"state": {...}, ...rest, "now": t} from encoded sections; only = the section names to send (a partial update)."""
    if enc["sections"] is None:
        state = "null"
    else:
        names = enc["sections"] if only is None else only
        state = "{" + ",".join('"%s":%s' % (k, enc["sections"][k]) for k in names) + "}"
    head = '{"state":%s,%s"now":%.3f,' % (state, '"partial":true,' if only is not None else "", now)
    return head + enc["rest"][1:]


class Handler(BaseHTTPRequestHandler):
    server_version = "nodeyard-dashboard"
    # Small writes (a streamed token, a terminal echo, an SSE event) go out at once instead of waiting
    # for the previous packet's ACK (Nagle + delayed ACK can hold each one for up to 40-200 ms).
    disable_nagle_algorithm = True
    sys_version = ""   # (don't announce the Python version to every client)
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ------------------------------------------------------------

    @property
    def ctx(self):
        return self.server.ctx

    def _gzip_ok(self):
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def _send(self, code, body, ctype, extra=None, gz=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        frameable = self.path.split("?", 1)[0] == "/g3/embed"
        extra = dict(extra or {})
        if ctype.startswith(COMPRESSIBLE) and "Content-Encoding" not in extra:
            extra["Vary"] = "Accept-Encoding"
            if self._gzip_ok():
                if gz is None and len(body) >= GZIP_MIN:
                    gz = gzip_bytes(body, 5)
                if gz is not None and len(gz) < len(body):
                    body, extra["Content-Encoding"] = gz, "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if frameable else "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", PERMISSIONS)
        self.send_header("Content-Security-Policy", CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'") if frameable else CSP)
        if "Cache-Control" not in extra:   # (a caller's own caching must not be cancelled by a second, stricter header)
            self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "no-cache")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, separators=(",", ":")), "application/json; charset=utf-8", extra)

    def _https_redirect(self, path, q):
        """Browsers opening a page on the plain http:// address (like http://TAILSCALE-IP:9092) go to the tailnet's
        https:// one when it's on (voice chat needs it): never the API, the Mac app's embedded pages, this machine
        itself (SSH tunnels), Tailscale Serve's own requests, or with ?http=1."""
        if self.command != "GET" or path not in ("/", "/index.html", "/login", "/login.html") or q.get("embed") or q.get("http"):
            return ""
        if self.ctx.mode == "demo" or not getattr(self.ctx, "port", None):
            return ""
        peer = self.client_address[0]
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
        if peer in ("127.0.0.1", "::1") or host.endswith(".ts.net") or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return ""
        url = tsserve.https_url(self.ctx.port)
        if not url or peer == tsserve.proxy_source():
            return ""
        return url.rstrip("/") + ("/login" if path.startswith("/login") else "/")

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
                # (public: for uptime monitors, so it says how fresh the data is but nothing about the cluster)
                age = time.time() - srv.store.updated if srv.store.updated else None
                return self._json({"ok": True, "uptime": int(time.time() - STARTED), "data_age": round(age, 1) if age is not None else None,
                                   "fresh": age is not None and age < max(30, srv.store.interval * 6)})
            if path == "/api/auth":
                return self._json({"ok": True, "enabled": srv.auth is not None, "authenticated": self._authed()})
            if path == "/api/state":
                if q.get("fresh") and time.time() - srv.store.updated > 2:
                    srv.store.poll()
                enc = encoded_snapshot(srv, srv.store.version, False)
                return self._send(200, snapshot_json(enc, time.time()), "application/json; charset=utf-8")
            if path == "/api/stream":
                return self._stream(q)
            if path == "/api/history":
                try:
                    n = max(2, min(HISTORY_POINTS, int(q.get("points", ["360"])[0])))
                except ValueError:
                    n = 360
                key = (srv.store.version, n)
                with srv.lock:
                    body = srv.history_cache.get(key)
                if body is None:
                    body = _dumps({"ok": True, "interval": srv.store.interval, "points": srv.store.history_slice(n)})
                    with srv.lock:
                        srv.history_cache = {key: body}
                return self._send(200, body, "application/json; charset=utf-8")
            if path == "/api/logs":
                return self._logs(q)
            if path == "/api/export":
                snap = srv.store.snapshot()
                snap["exported"] = time.time()
                return self._json(snap, extra={"Content-Disposition": 'attachment; filename="nodeyard-snapshot.json"'})
            for route, fn in srv.get_routes.items():
                if path == route:
                    return fn(self, q)
            for prefix, fn in srv.prefix_routes.items():
                if path.startswith(prefix):
                    return fn(self, path, q)
            go = self._https_redirect(path, q)
            if go:
                return self._redirect(go)
            if path == "/login":
                path = "/login.html"
            if srv.auth is not None and self._authed() and path == "/login.html":
                return self._redirect("/")
            return self._static(path, q)
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
        delta = bool(q.get("delta"))     # the page merges partial updates; other readers get whole snapshots
        z = zlib.compressobj(5, zlib.DEFLATED, 31) if self._gzip_ok() else None

        def write(data):
            # a compressed stream is flushed at every event, so each one arrives whole and at once
            self.wfile.write(z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH) if z is not None else data)
            self.wfile.flush()
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                         ("X-Accel-Buffering", "no"), ("Connection", "close"), ("Content-Security-Policy", CSP), ("Vary", "Accept-Encoding")):
                self.send_header(k, v)
            if z is not None:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.close_connection = True
            store, last, sent = srv.store, -1, None
            write(b"retry: 2000\n\n")
            while not store.stop.is_set():
                with store.cond:
                    store.cond.wait_for(lambda: store.version != last or store.stop.is_set(), timeout=15)
                    version = store.version
                if store.stop.is_set():
                    break
                if not self._authed():
                    write(b"event: auth\ndata: {}\n\n")
                    break
                if version != last:
                    last = version
                    enc = encoded_snapshot(srv, version, want_agents)
                    only = None
                    if delta and sent is not None and sent["sections"] is not None and enc["sections"] is not None and \
                            set(sent["sections"]) == set(enc["sections"]):
                        only = [k for k, v in enc["sections"].items() if sent["sections"][k] is not v and sent["sections"][k] != v]
                    write(("event: state\ndata: %s\n\n" % snapshot_json(enc, time.time(), only)).encode("utf-8"))
                    sent = enc
                else:
                    write(b": ping\n\n")
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

    def _static(self, path, q=None):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(WEB, path.lstrip("/")))
        if not (full == WEB or full.startswith(WEB + os.sep)) or not os.path.isfile(full):
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        f = STATIC.get(full)
        if full.endswith(".html"):
            f = versioned_page(full, f)
        # a URL carrying this file's own hash never changes: the browser keeps it without asking again
        cache = IMMUTABLE if (q or {}).get("v", [""])[0] == f["tag"] else "no-cache"
        if f["etag"] in [t.strip() for t in self.headers.get("If-None-Match", "").split(",")]:
            # unchanged since the browser's copy: no body (the files are re-checked on every load, so updates show at once)
            self.send_response(304)
            for k, v in (("ETag", f["etag"]), ("Cache-Control", cache), ("Vary", "Accept-Encoding")):
                self.send_header(k, v)
            self.end_headers()
            return None
        headers = {"ETag": f["etag"], "Cache-Control": cache}
        if path.endswith(".sh"):
            headers["Content-Disposition"] = 'attachment; filename="' + os.path.basename(path) + '"'
        self._send(200, f["body"], f["ctype"], headers, gz=f["gzip"])

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
        self.prefix_routes = {}   # path prefix -> fn(handler, path, query), for folders of files
        self.lock = threading.Lock()
        self.streams = 0
        self.encoded = {}        # (store version, with agents) -> the snapshot's JSON, shared by every page
        self.history_cache = {}
        self.__dict__.update(kw)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # A page load opens 20+ connections at once; the default backlog of 5 drops some SYNs on a busy
    # moment, and each dropped one costs a 1 s retransmit.
    request_queue_size = 128


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
    ctx = Context(store=store, source=source, mode="demo" if a.demo else "live", nd_version=a.nodeyard_version, auth=auth,
                  port=a.port, listen_addrs=addrs)

    def agents_route(h, q):
        snap = store.snapshot()
        if not snap["state"]:
            return h._json({"ok": False, "error": "Not ready yet."}, 503)
        node = q.get("node", [""])[0]
        h._json({"ok": True, "installed": bool(snap["state"].get("agents", {}).get("installed")), "nodes": agents.payload(snap["state"], node or None)})
    ctx.get_routes["/api/agents"] = agents_route
    import turns         # chat answers that survive a dropped connection (the page reconnects and carries on)
    turns.register(ctx, a)
    import aisearch      # whether the AI may search the web (enforced by every AI route)
    aisearch.register(ctx, a)
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
    import connections
    connections.register(ctx, a)
    if getattr(ctx, "ai", None) is not None:
        import lifecycle
        lifecycle.register(ctx, a)
        import modelrecs     # AI > Recommended: the model for each kind of work, for these machines and overall
        modelrecs.register(ctx, a)
        import research
        research.register(ctx, a)
    import terminal
    terminal.register(ctx, a)
    import apiregistry   # the user's own self-hosted APIs (search + REST), as AI tools and dashboard UI
    apiregistry.register(ctx, a)
    import music         # optional: a folder music player and host-audio forwarding, both streamed server-side
    music.register(ctx, a)
    import video         # optional: upload and watch movies and series, streamed server-side with resume
    video.register(ctx, a)
    import djvoice       # the DJ's natural voice: Piper text-to-speech on this server, downloaded when asked for
    djvoice.register(ctx, a)
    import diskusage     # what's filling each node's disk, and how to free it
    diskusage.register(ctx, a)
    import tune          # per-device AI settings, and CPU/GPU power, clock and overclock controls
    tune.register(ctx, a)
    import memfree       # processes a node doesn't need, moved to swap or stopped (Processes > Free up memory)
    memfree.register(ctx, a)
    import pwnforge      # optional: host PwnForge (cybersecurity training) from this machine; off unless turned on
    pwnforge.register(ctx, a)
    import temps         # every machine's sensors, kept for a day (the node page's temperature charts)
    temps.register(ctx, a)
    import hwcare        # Hardware > Maintenance, Cooling and Lid: a guide per machine, idle fan settings, the lid action
    hwcare.register(ctx, a)
    import cleanup       # pods nobody has used for a while, removed by a policy you control (off by default)
    cleanup.register(ctx, a)
    import quotes        # the Desk's quotes: built-in (checked against their sources) and your own
    quotes.register(ctx, a)
    if getattr(ctx, "ai", None) is not None:
        import aiapi as _aiapi
        _aiapi.EXTRA_SPLIT_ARGS = ctx.tuning.run_args
    import homelab       # service monitor, notifications, Wake-on-LAN, network tools, Prometheus metrics
    homelab.register(ctx, a)
    import galaxy3d      # the 3D galaxy screens (/g3/), fed from k3s
    galaxy3d.register(ctx, a)
    import selfupdate    # restarts on the new program after an update (the page files are already new)
    selfupdate.register(ctx, a)
    def refresh_all(h, body):
        """Full refresh: every module forgets what it remembered (cluster lists, connections, model lists, the disk
        inventory, Tailscale and CLI answers, page files, the music library, services), the cluster is read again, and
        the model is really asked to answer (repaired when it can't). The page reloads itself afterwards."""
        t0 = time.time()
        cleared = []
        for name, obj in (("cluster", source), ("node agents", agents), ("models", getattr(ctx, "ai", None)), ("settings", getattr(ctx, "settings", None)),
                          ("music library", getattr(ctx, "music", None)), ("services", getattr(ctx, "homelab", None))):
            fn = getattr(obj, "reset", None)
            if fn is None:
                continue
            try:
                fn()
                cleared.append(name)
            except Exception as e:  # noqa: BLE001 -- one module's cache never stops the rest
                cleared.append("%s (failed: %s)" % (name, str(e)[:80]))
        tsserve.forget()
        with STATIC.lock:
            STATIC.files.clear()
        with _PAGES_LOCK:
            _PAGES.clear()
        with ctx.lock:
            ctx.encoded, ctx.history_cache = {}, {}
        cleared += ["Tailscale address", "page files", "live-update encodings"]
        before = store.version
        store.poll()
        with store.cond:     # (a read already under way counts: wait for the next one to land)
            store.cond.wait_for(lambda: store.version > before or store.stop.is_set(), timeout=25)
        snap = store.snapshot()
        st = snap["state"] or {}
        report = {"ok": True, "cleared": cleared, "cluster": {"read": store.version > before, "error": snap["error"],
                  "nodes": len(st.get("nodes", [])), "ready": sum(1 for n in st.get("nodes", []) if n.get("ready"))}}
        backend, lc = getattr(ctx, "ai", None), getattr(ctx, "lifecycle", None)
        if backend is not None and hasattr(backend, "check_split"):
            model = backend.check_split()
            if model["state"] in ("stale", "broken") and lc is not None:
                try:
                    job = lc.force_reload("Full refresh found it not answering (%s)" % model["detail"].rstrip("."))
                    model["action"] = "reloading" if job else "already reloading"
                except Exception as e:  # noqa: BLE001
                    model["action"] = "couldn't reload: %s" % e
            report["model"] = model
        report["seconds"] = round(time.time() - t0, 1)
        return h._json(report)

    ctx.post_routes["/api/refresh-all"] = refresh_all
    import plugins       # last: it wraps every route above with on/off switches and the audit log
    plugins.register(ctx, a)

    servers = []
    try:
        port = a.port
        for addr in addrs:
            srv = make_listener(addr, port, ctx, a.verbose)
            port = srv.server_address[1]  # --port 0: every listener uses the first one's port
            servers.append(srv)
        ctx.port = port
    except OSError as e:
        sys.exit("Cannot listen on %s:%d: %s" % (addr, port, e.strerror or e))

    threading.Thread(target=store.loop, daemon=True).start()
    # read and compress the page files now, and ask Tailscale for the https address, so the first visitor doesn't wait
    threading.Thread(target=lambda: (STATIC.warm(WEB), tsserve.https_url(port) if a.port else None), daemon=True).start()

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
