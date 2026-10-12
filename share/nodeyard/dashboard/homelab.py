"""Homelab tools: a service monitor, notifications, Wake-on-LAN, network diagnostics and Prometheus metrics.

* Service monitor: HTTP(S), TCP and ping checks you add yourself, run from this server on their own interval. Each
  keeps 24 hours of results (uptime, latency) across restarts, and HTTPS checks track when the certificate expires.
  A service that fails twice in a row, or a certificate close to expiry, becomes an alert on the Alerts page.
* Notifications: when an alert appears (and has lasted a minute, so short flaps stay quiet) or clears, a message goes
  to your own webhook: ntfy, Discord, Slack/Mattermost, Gotify or plain JSON. Nothing is sent until you set a URL.
* Wake-on-LAN: saved machines get a magic packet from this server's network.
* Network tools: ping, DNS lookup, TCP port and HTTP checks, and traceroute, run from this server.
* /api/v1/metrics: Prometheus text format, with the server API key as a bearer token (or a signed-in page).

Only a signed-in user can change any of it. Checks reach the addresses you type, including private ones: that is
the point of a homelab monitor, and the AI has no access to these routes.
"""
import http.client
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2})(?:[:-]?([0-9A-Fa-f]{2})){5}$")
KINDS = ("http", "tcp", "ping")
NOTIFY_KINDS = ("ntfy", "discord", "slack", "gotify", "json")
HISTORY_SECONDS = 24 * 3600
HISTORY_MAX = 3000            # per service: a 30 s check for 24 h is 2880
MAX_SERVICES = 200
MAX_WOL = 100
CERT_WARN_DAYS = 14
ALERT_HOLD = 60               # an alert must last this long before it's sent
REPEAT_QUIET = 15 * 60        # an alert that clears and comes back within this is not sent again
BODY_PEEK = 256 * 1024        # how much of a page a keyword check reads
USER_AGENT = "nodeyard-monitor/1"


class HomelabError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _host(value):
    """A host name or IP address, never anything a command line could take as an option."""
    v = str(value or "").strip().strip("[]")
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        pass
    if not HOST_RE.match(v) or v.startswith("-"):
        raise HomelabError("That isn't a host name or IP address.")
    return v


def _port(value, default=None):
    if value in (None, "") and default is not None:
        return default
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise HomelabError("The port must be a number from 1 to 65535.")
    if not 1 <= p <= 65535:
        raise HomelabError("The port must be a number from 1 to 65535.")
    return p


def _url(value):
    u = urllib.parse.urlparse(str(value or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HomelabError("Use a full http:// or https:// address.")
    _host(u.hostname)
    return u.geturl()


def _mac(value):
    m = str(value or "").strip()
    if not MAC_RE.match(m):
        raise HomelabError("A MAC address looks like aa:bb:cc:dd:ee:ff.")
    return ":".join(re.findall(r"[0-9a-f]{2}", m.lower().replace(":", "").replace("-", "")))


def _clean_text(value, limit, default=""):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value if value is not None else default)).strip()[:limit]


def _cert_days(sock):
    try:
        cert = sock.getpeercert()
        if cert and cert.get("notAfter"):
            return (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400.0
    except (ValueError, OSError, AttributeError):
        pass
    return None


# --------------------------------------------------------------------------- checks

def check_http(url, timeout=10, verify=True, expect="", keyword="", method="GET"):
    """One request, following no redirects (a redirect is an answer). Returns a result dict."""
    u = urllib.parse.urlparse(url)
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    t0 = time.monotonic()
    conn = None
    try:
        if https:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
        conn.request(method, path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        r = conn.getresponse()
        first = time.monotonic()
        body = r.read(BODY_PEEK) if keyword and method != "HEAD" else b""
        ms = (first - t0) * 1000.0
        cert = _cert_days(conn.sock) if https and verify and conn.sock else None
        ok = _status_ok(r.status, expect)
        error = "" if ok else "Answered HTTP %d%s." % (r.status, (" (expected %s)" % expect) if expect else "")
        if ok and keyword and keyword.encode("utf-8", "replace") not in body:
            ok, error = False, "The page didn't contain \u201c%s\u201d." % keyword
        return {"ok": ok, "ms": round(ms, 1), "code": r.status, "error": error, "cert_days": cert,
                "location": r.getheader("Location", "") or "", "server": r.getheader("Server", "") or ""}
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "ms": None, "error": "Certificate problem: %s. Turn off certificate checking for self-signed services." % (e.verify_message or e)}
    except (OSError, http.client.HTTPException, ValueError) as e:
        return {"ok": False, "ms": None, "error": _net_error(e)}
    finally:
        if conn is not None:
            conn.close()


def _status_ok(code, expect):
    expect = (expect or "").strip()
    if not expect:
        return 200 <= code < 400
    for part in expect.replace(" ", "").split(","):
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit() and int(lo) <= code <= int(hi):
                return True
        elif part.isdigit() and int(part) == code:
            return True
        elif len(part) == 3 and part[0].isdigit() and part[1:].lower() == "xx" and code // 100 == int(part[0]):
            return True
    return False


def _net_error(e):
    if isinstance(e, socket.timeout) or "timed out" in str(e):
        return "No answer in time."
    if isinstance(e, socket.gaierror):
        return "The name doesn't resolve (DNS)."
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused: nothing is listening on that port."
    if isinstance(e, OSError) and e.errno in (101, 113, 65, 51):
        return "No route to that address."
    return (str(e) or e.__class__.__name__)[:200]


def check_tcp(host, port, timeout=5):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000.0, 1), "error": ""}
    except OSError as e:
        return {"ok": False, "ms": None, "error": _net_error(e)}


def _ping_argv(host, count, wait):
    exe = shutil.which("ping")
    if not exe:
        return None
    six = ":" in host
    if platform.system() == "Darwin":
        return [shutil.which("ping6") or exe, "-c", str(count), host] if six else [exe, "-c", str(count), "-t", str(max(1, wait * count)), host]
    return [exe] + (["-6"] if six else []) + ["-c", str(count), "-W", str(wait), "-i", "0.3" if count > 1 else "1", host]


def check_ping(host, count=1, wait=2):
    argv = _ping_argv(host, count, wait)
    if argv is None:
        return {"ok": False, "ms": None, "error": "ping isn't installed on this server. Use a TCP check instead.", "text": ""}
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=wait * count + 5)
        text = p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "ms": None, "error": "No answer in time.", "text": ""}
    except OSError as e:
        return {"ok": False, "ms": None, "error": str(e), "text": ""}
    times = [float(x) for x in re.findall(r"time[=<]([\d.]+)\s*ms", text)]
    if p.returncode == 0 and times:
        return {"ok": True, "ms": round(sum(times) / len(times), 1), "error": "", "text": text, "replies": len(times)}
    if "permitted" in text or "permission" in text.lower():
        err = "This server may not send pings (permission). Use a TCP check instead."
    elif "unknown host" in text.lower() or "resolve" in text.lower() or "not known" in text.lower():
        err = "The name doesn't resolve (DNS)."
    else:
        err = "No reply."
    return {"ok": False, "ms": None, "error": err, "text": text}


def run_check(svc):
    kind, target = svc["kind"], svc["target"]
    if kind == "http":
        return check_http(target, timeout=svc.get("timeout") or 10, verify=svc.get("verify", True), expect=svc.get("expect", ""), keyword=svc.get("keyword", ""))
    if kind == "tcp":
        host, _, port = target.rpartition(":")
        return check_tcp(host.strip("[]"), int(port), timeout=svc.get("timeout") or 5)
    return check_ping(target)


# --------------------------------------------------------------------------- notifications

def build_message(kind, url, title, body, level):
    """(url, data bytes, headers) for one webhook flavour."""
    if kind == "ntfy":
        headers = {"Title": title.encode("ascii", "replace").decode("ascii"), "Priority": {"critical": "high", "warning": "default"}.get(level, "low"),
                   "Tags": {"critical": "rotating_light", "warning": "warning", "resolved": "white_check_mark"}.get(level, "information_source")}
        return url, body.encode("utf-8"), dict(headers, **{"Content-Type": "text/plain; charset=utf-8"})
    if kind == "discord":
        return url, json.dumps({"content": ("**%s**\n%s" % (title, body))[:1900], "username": "nodeyard"}).encode("utf-8"), {"Content-Type": "application/json"}
    if kind == "slack":
        return url, json.dumps({"text": "*%s*\n%s" % (title, body)}).encode("utf-8"), {"Content-Type": "application/json"}
    if kind == "gotify":
        return url, json.dumps({"title": title, "message": body, "priority": {"critical": 8, "warning": 5}.get(level, 2)}).encode("utf-8"), {"Content-Type": "application/json"}
    return url, json.dumps({"source": "nodeyard", "title": title, "message": body, "level": level, "time": int(time.time())}).encode("utf-8"), {"Content-Type": "application/json"}


def send_webhook(kind, url, title, body, level, timeout=10):
    target, data, headers = build_message(kind, url, title, body, level)
    req = urllib.request.Request(target, data=data, method="POST", headers=dict(headers, **{"User-Agent": USER_AGENT}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return True, "Delivered (HTTP %d)." % r.status
    except urllib.error.HTTPError as e:
        return False, "The webhook answered HTTP %d." % e.code
    except (OSError, ValueError) as e:
        return False, _net_error(e)


def alert_key(a):
    """Alerts whose titles carry live numbers (\u201c91% used\u201d) keep one identity."""
    return "%s|%s|%s|%s" % (a.get("kind", ""), a.get("ref", ""), a.get("level", ""), re.sub(r"[\d.,]+", "#", a.get("title", "")))


# --------------------------------------------------------------------------- the store

class Homelab:
    def __init__(self, state_dir, store=None, demo=False):
        self.dir = state_dir
        self.path = os.path.join(state_dir, "homelab.json")
        self.hist_path = os.path.join(state_dir, "homelab-history.json")
        self.store = store
        self.demo = demo
        self.lock = threading.RLock()
        self.cfg = self._load(self.path, {})
        self.cfg.setdefault("services", [])
        self.cfg.setdefault("wol", [])
        self.cfg.setdefault("notify", {})
        hist = self._load(self.hist_path, {})
        self.hist = {k: v for k, v in hist.items() if isinstance(v, list)}
        self.live = {}            # service id -> {"fails": n, "last": result, "next": t, "running": bool, "down_since": t}
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="homelab-check")
        self.tools = threading.BoundedSemaphore(3)
        self.stop = threading.Event()
        self.dirty_at = 0.0
        self.seen = {}            # alert key -> first seen
        self.sent = {}            # alert key -> {"title", "level", "at"}
        self.cleared = {}         # alert key -> when it cleared (for flap quiet)
        self.notify_log = []      # the last deliveries, newest first
        self.first_round = True

    # -- persistence ----------------------------------------------------------

    @staticmethod
    def _load(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, type(default)) else default
        except (OSError, ValueError):
            return default

    def _write(self, path, obj):
        os.makedirs(self.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def save(self):
        with self.lock:
            self._write(self.path, self.cfg)

    def save_history(self, force=False):
        with self.lock:
            if not force and (not self.dirty_at or time.time() - self.dirty_at < 300):
                return
            self.dirty_at = 0.0
            data = {k: v for k, v in self.hist.items() if any(s["id"] == k for s in self.cfg["services"])}
        try:
            self._write(self.hist_path, data)
        except OSError:
            pass

    # -- services -------------------------------------------------------------

    def clean_service(self, body, old=None):
        kind = str(body.get("kind") or (old or {}).get("kind") or "http")
        if kind not in KINDS:
            raise HomelabError("The check type must be http, tcp or ping.")
        name = _clean_text(body.get("name"), 60) or (old or {}).get("name") or ""
        target = str(body.get("target") if body.get("target") is not None else (old or {}).get("target", "")).strip()
        if kind == "http":
            if target and "://" not in target:
                target = "http://" + target
            target = _url(target)
        elif kind == "tcp":
            host, sep, port = target.rpartition(":")
            if not sep:
                raise HomelabError("A TCP check needs host:port, like 192.168.1.10:22.")
            target = "%s:%d" % (("[%s]" % _host(host)) if ":" in _host(host) else _host(host), _port(port))
        else:
            target = _host(target)
        try:
            interval = int(body.get("interval") or (old or {}).get("interval") or 60)
        except (TypeError, ValueError):
            interval = 60
        svc = {
            "id": (old or {}).get("id") or secrets.token_hex(5),
            "name": name or target.split("://")[-1][:60],
            "kind": kind, "target": target,
            "interval": max(15, min(3600, interval)),
            "group": _clean_text(body.get("group", (old or {}).get("group", "")), 40),
            "link": "",
            "expect": _clean_text(body.get("expect", (old or {}).get("expect", "")), 40) if kind == "http" else "",
            "keyword": _clean_text(body.get("keyword", (old or {}).get("keyword", "")), 120) if kind == "http" else "",
            "verify": bool(body.get("verify", (old or {}).get("verify", True))) if kind == "http" else True,
            "paused": bool(body.get("paused", (old or {}).get("paused", False))),
            "created": (old or {}).get("created") or int(time.time()),
        }
        link = str(body.get("link", (old or {}).get("link", "")) or "").strip()
        svc["link"] = _url(link) if link else (target if kind == "http" else "")
        if svc["expect"] and not re.match(r"^[\dxX,\- ]+$", svc["expect"]):
            raise HomelabError("Expected status codes look like 200, 200-299 or 2xx.")
        return svc

    def services(self):
        now = time.time()
        out = []
        with self.lock:
            for s in self.cfg["services"]:
                h = [x for x in self.hist.get(s["id"], []) if x[0] >= now - HISTORY_SECONDS]
                live = self.live.get(s["id"], {})
                last = live.get("last") or (({"ok": bool(h[-1][1]), "ms": h[-1][2], "error": h[-1][3] if len(h[-1]) > 3 else ""}) if h else None)
                ups = sum(1 for x in h if x[1])
                lat = [x[2] for x in h if x[1] and x[2] is not None]
                out.append(dict(s, status=self._status(s, live, last), last=last, checked=(live.get("at") or (h[-1][0] if h else None)),
                                uptime=round(100.0 * ups / len(h), 2) if h else None, checks=len(h),
                                avg_ms=round(sum(lat) / len(lat), 1) if lat else None,
                                down_since=live.get("down_since"), cert_days=(live.get("last") or {}).get("cert_days"),
                                bars=self._bars(h, now), spark=[x[2] if x[1] else None for x in h[-60:]]))
        return out

    @staticmethod
    def _status(s, live, last):
        if s.get("paused"):
            return "paused"
        if not last:
            return "pending"
        if last.get("ok"):
            return "up"
        return "down" if live.get("fails", 0) >= 2 or not live else "degraded"

    @staticmethod
    def _bars(h, now, n=48):
        """48 half-hour buckets over 24 h: None (no data), or the share of good checks."""
        span = HISTORY_SECONDS / n
        good, total = [0] * n, [0] * n
        for x in h:
            i = int((x[0] - (now - HISTORY_SECONDS)) // span)
            if 0 <= i < n:
                total[i] += 1
                good[i] += 1 if x[1] else 0
        return [round(good[i] / total[i], 3) if total[i] else None for i in range(n)]

    def add_service(self, body):
        with self.lock:
            if len(self.cfg["services"]) >= MAX_SERVICES:
                raise HomelabError("That's the most services one dashboard watches (%d)." % MAX_SERVICES)
            svc = self.clean_service(body)
            self.cfg["services"].append(svc)
            self.save()
            self.live[svc["id"]] = {"next": 0}
        return svc

    def update_service(self, body):
        with self.lock:
            sid = str(body.get("id") or "")
            for i, s in enumerate(self.cfg["services"]):
                if s["id"] == sid:
                    new = self.clean_service(body, s)
                    if new["target"] != s["target"] or new["kind"] != s["kind"]:
                        self.hist.pop(sid, None)
                        self.live[sid] = {"next": 0}
                    self.cfg["services"][i] = new
                    self.save()
                    return new
        raise HomelabError("That service isn't in the list any more.", 404)

    def delete_service(self, sid):
        with self.lock:
            before = len(self.cfg["services"])
            self.cfg["services"] = [s for s in self.cfg["services"] if s["id"] != sid]
            if len(self.cfg["services"]) == before:
                raise HomelabError("That service isn't in the list any more.", 404)
            self.hist.pop(sid, None)
            self.live.pop(sid, None)
            self.save()
            self.dirty_at = self.dirty_at or time.time()

    def check_now(self, sid):
        with self.lock:
            svc = next((s for s in self.cfg["services"] if s["id"] == sid), None)
            if svc is None:
                raise HomelabError("That service isn't in the list any more.", 404)
        self._run(svc)
        return next(s for s in self.services() if s["id"] == sid)

    def _run(self, svc):
        with self.lock:
            live = self.live.setdefault(svc["id"], {})
            if live.get("running"):
                return
            live["running"] = True
        try:
            res = self._demo_result(svc) if self.demo and not svc["target"].startswith(("http://127.", "http://localhost")) else run_check(svc)
        except Exception as e:  # a check must never take the loop down
            res = {"ok": False, "ms": None, "error": str(e)[:200]}
        res.pop("text", None)
        now = time.time()
        with self.lock:
            live = self.live.setdefault(svc["id"], {})
            live["running"] = False
            live["at"] = now
            live["last"] = res
            live["fails"] = 0 if res["ok"] else live.get("fails", 0) + 1
            if res["ok"]:
                live.pop("down_since", None)
            elif live["fails"] >= 2 and not live.get("down_since"):
                live["down_since"] = now
            live["next"] = now + svc["interval"]
            h = self.hist.setdefault(svc["id"], [])
            h.append([round(now, 1), 1 if res["ok"] else 0, res.get("ms"), (res.get("error") or "")[:120]])
            cut = now - HISTORY_SECONDS
            while h and (h[0][0] < cut or len(h) > HISTORY_MAX):
                h.pop(0)
            self.dirty_at = self.dirty_at or now

    def _demo_result(self, svc):
        """The demo has no network to watch: a steady, mostly-up answer per service."""
        seed = sum(ord(c) for c in svc["id"])
        t = int(time.time() / 60)
        down = (t + seed) % 97 == 0 or "down" in svc["name"].lower()
        if down:
            return {"ok": False, "ms": None, "error": "Connection refused: nothing is listening on that port."}
        return {"ok": True, "ms": round(8 + (seed % 40) + 6 * math.sin(time.time() / 50 + seed), 1), "error": "", "code": 200,
                "cert_days": 41.0 if svc["target"].startswith("https") else None}

    def due(self):
        now = time.time()
        with self.lock:
            out = []
            for s in self.cfg["services"]:
                live = self.live.setdefault(s["id"], {})
                if s.get("paused") or live.get("running"):
                    continue
                if live.get("next") is None:
                    # first round after a start: spread the checks so they don't all fire at once
                    h = self.hist.get(s["id"])
                    live["next"] = (h[-1][0] + s["interval"]) if h else now + (len(out) % 10)
                if live["next"] <= now:
                    live["next"] = now + s["interval"]
                    out.append(s)
            return out

    def alerts(self):
        """Alerts for the Alerts page: services that are down, and certificates near expiry."""
        out = []
        for s in self.services():
            ref = s["id"]
            if s["status"] == "down":
                mins = int((time.time() - (s.get("down_since") or time.time())) // 60)
                out.append({"level": "critical", "title": "%s is down" % s["name"],
                            "detail": "%s %s%s" % (s["target"], (s["last"] or {}).get("error") or "", (" Down for %d min." % mins) if mins else ""),
                            "kind": "monitor", "ref": ref})
            cd = s.get("cert_days")
            if cd is not None and cd < CERT_WARN_DAYS and s["status"] != "paused":
                out.append({"level": "critical" if cd < 3 else "warning",
                            "title": "%s's certificate %s" % (s["name"], "has expired" if cd < 0 else "expires in %d day%s" % (int(cd), "" if int(cd) == 1 else "s")),
                            "detail": "%s: renew it before browsers refuse the site." % s["target"], "kind": "monitor", "ref": ref})
        out += self.forecast_alerts()
        return out

    def forecast_alerts(self):
        """A disk that keeps filling at its recent rate and would be full within a day."""
        if self.store is None:
            return []
        hist = self.store.history_slice(720)
        if len(hist) < 30:
            return []
        snap = self.store.snapshot()["state"] or {}
        out = []
        for n in snap.get("nodes", []):
            total = n.get("disk_total")
            pts = [(h["t"], h["nodes"][n["name"]][9]) for h in hist
                   if n["name"] in h.get("nodes", {}) and len(h["nodes"][n["name"]]) > 9 and h["nodes"][n["name"]][9] is not None]
            if not total or len(pts) < 30:
                continue
            span = pts[-1][0] - pts[0][0]
            if span < 600:
                continue
            mt = sum(p[0] for p in pts) / len(pts)
            mv = sum(p[1] for p in pts) / len(pts)
            den = sum((p[0] - mt) ** 2 for p in pts)
            if not den:
                continue
            rate = sum((p[0] - mt) * (p[1] - mv) for p in pts) / den   # bytes per second
            if rate * span < 256 * 1048576:   # under 256 MiB over the window: noise, logs rotating, a pull
                continue
            left = (total - pts[-1][1]) / rate
            if 0 < left < 24 * 3600:
                out.append({"level": "critical" if left < 3 * 3600 else "warning",
                            "title": "%s's disk will be full in about %s" % (n["name"], ("%d h" % (left // 3600)) if left >= 3600 else ("%d min" % (left // 60))),
                            "detail": "It grew %.1f GiB in the last %d minutes. Find what is writing (Processes, Storage) before it fills."
                                      % (rate * span / 1073741824.0, span // 60), "kind": "node", "ref": n["name"]})
        return out

    # -- notifications -----------------------------------------------------------

    def notify_cfg(self):
        n = dict(self.cfg.get("notify") or {})
        return {"enabled": bool(n.get("enabled")), "kind": n.get("kind") if n.get("kind") in NOTIFY_KINDS else "ntfy", "url": n.get("url", ""),
                "level": n.get("level") if n.get("level") in ("critical", "warning") else "critical", "resolved": n.get("resolved", True) is not False}

    def set_notify(self, body):
        n = self.notify_cfg()
        if "kind" in body:
            if body["kind"] not in NOTIFY_KINDS:
                raise HomelabError("Pick ntfy, Discord, Slack, Gotify or JSON.")
            n["kind"] = body["kind"]
        if "url" in body:
            n["url"] = _url(body["url"]) if str(body["url"] or "").strip() else ""
        if "level" in body:
            n["level"] = "warning" if body["level"] == "warning" else "critical"
        if "resolved" in body:
            n["resolved"] = bool(body["resolved"])
        if "enabled" in body:
            n["enabled"] = bool(body["enabled"])
        if n["enabled"] and not n["url"]:
            raise HomelabError("Add the webhook address first.")
        with self.lock:
            self.cfg["notify"] = n
            self.save()
        return self.notify_view()

    def notify_view(self):
        n = self.notify_cfg()
        u = urllib.parse.urlparse(n["url"]) if n["url"] else None
        # the address can hold a token (Discord, Gotify): the page shows where it goes, not the whole thing
        n["url_hint"] = ("%s://%s/…" % (u.scheme, u.netloc)) if u else ""
        n["has_url"] = bool(n.pop("url"))
        n["log"] = self.notify_log[:12]
        return n

    def _deliver(self, title, body, level):
        n = self.notify_cfg()
        if not n["url"]:
            return False, "No webhook address is set."
        if self.demo:
            ok, msg = True, "(demo) would send to %s" % urllib.parse.urlparse(n["url"]).netloc
        else:
            ok, msg = send_webhook(n["kind"], n["url"], title, body, level)
        with self.lock:
            self.notify_log.insert(0, {"at": time.time(), "title": title, "level": level, "ok": ok, "result": msg})
            del self.notify_log[30:]
        return ok, msg

    def test_notify(self):
        ok, msg = self._deliver("nodeyard test", "Notifications from your homelab dashboard work.", "info")
        if not ok:
            raise HomelabError(msg)
        return {"message": msg, "notify": self.notify_view()}

    def watch_alerts(self, current):
        """Sends new alerts that lasted ALERT_HOLD, and their clearing. The first round after a start only learns."""
        n = self.notify_cfg()
        now = time.time()
        levels = ("critical",) if n["level"] == "critical" else ("critical", "warning")
        wanted = {alert_key(a): a for a in current if a.get("level") in levels}
        for k in list(self.seen):
            if k not in wanted:
                self.seen.pop(k)
        for k in wanted:
            self.seen.setdefault(k, now)
        if self.first_round:
            self.first_round = False
            for k, a in wanted.items():   # (it may have been sent before the restart: its clearing is still news)
                self.sent[k] = {"title": a["title"], "level": a["level"], "at": now, "notified": True}
            return
        fresh = [(k, wanted[k]) for k, t in self.seen.items() if k not in self.sent and now - t >= ALERT_HOLD]
        gone = [(k, v) for k, v in self.sent.items() if k not in wanted]
        for k, v in gone:
            self.sent.pop(k)
            self.cleared[k] = now
        for k in [k for k, t in self.cleared.items() if now - t > REPEAT_QUIET]:
            self.cleared.pop(k)
        sendable = []
        for k, a in fresh:
            quiet = k in self.cleared     # it came back soon after clearing: a flap, stay quiet
            self.sent[k] = {"title": a["title"], "level": a["level"], "at": self.seen[k], "notified": not quiet}
            if not quiet:
                sendable.append(a)
        if not (n["enabled"] and n["url"]):
            return
        if len(sendable) > 4:
            crit = any(a["level"] == "critical" for a in sendable)
            self._deliver("%d new alerts" % len(sendable), "\n".join("\u2022 " + a["title"] for a in sendable[:15]), "critical" if crit else "warning")
        else:
            for a in sendable:
                self._deliver(a["title"], a.get("detail") or "", a["level"])
        if n["resolved"]:
            done = [v for k, v in gone if v.get("notified")]
            if len(done) > 4:
                self._deliver("%d alerts cleared" % len(done), "\n".join("\u2022 " + v["title"] for v in done[:15]), "resolved")
            else:
                for v in done:
                    mins = int((now - v["at"]) // 60)
                    self._deliver("Resolved: " + v["title"], "Cleared after %s." % ("%d min" % mins if mins else "under a minute"), "resolved")

    # -- Wake-on-LAN -------------------------------------------------------------

    def wol_add(self, body):
        dev = {"id": secrets.token_hex(5), "name": _clean_text(body.get("name"), 60) or "Machine", "mac": _mac(body.get("mac")),
               "broadcast": str(ipaddress.IPv4Address(str(body.get("broadcast") or "255.255.255.255").strip())), "port": _port(body.get("port"), 9)}
        with self.lock:
            if len(self.cfg["wol"]) >= MAX_WOL:
                raise HomelabError("That's the most machines this list holds.")
            self.cfg["wol"].append(dev)
            self.save()
        return self.cfg["wol"]

    def wol_delete(self, did):
        with self.lock:
            self.cfg["wol"] = [d for d in self.cfg["wol"] if d["id"] != did]
            self.save()
        return self.cfg["wol"]

    def wake(self, body):
        if body.get("id"):
            dev = next((d for d in self.cfg["wol"] if d["id"] == body["id"]), None)
            if dev is None:
                raise HomelabError("That machine isn't in the list any more.", 404)
        else:
            dev = {"name": "that machine", "mac": _mac(body.get("mac")), "broadcast": str(ipaddress.IPv4Address(str(body.get("broadcast") or "255.255.255.255").strip())),
                   "port": _port(body.get("port"), 9)}
        packet = b"\xff" * 6 + bytes.fromhex(dev["mac"].replace(":", "")) * 16
        if self.demo:
            return {"message": "(demo) would send a magic packet to %s." % dev["mac"]}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for _ in range(3):
                    s.sendto(packet, (dev["broadcast"], dev["port"]))
        except OSError as e:
            raise HomelabError("Couldn't send the packet: %s" % (e.strerror or e))
        return {"message": "Sent a wake-up packet to %s (%s). A machine takes a minute or so to boot." % (dev["name"], dev["mac"])}

    # -- network tools -------------------------------------------------------------

    def tool(self, body):
        tool = str(body.get("tool") or "")
        if not self.tools.acquire(blocking=False):
            raise HomelabError("Three tools are already running. Wait for one to finish.", 429)
        try:
            t0 = time.monotonic()
            out = self._tool(tool, body)
            out["seconds"] = round(time.monotonic() - t0, 2)
            return out
        finally:
            self.tools.release()

    def _tool(self, tool, body):
        if tool == "ping":
            host = _host(body.get("target"))
            r = check_ping(host, count=4, wait=2) if not self.demo else {"ok": True, "ms": 1.2, "error": "", "text": "(demo) 4 packets transmitted, 4 received, time=1.2 ms"}
            return {"ok": r["ok"], "summary": ("%d of 4 replies, average %.1f ms" % (r.get("replies", 4), r["ms"])) if r["ok"] else r["error"], "text": r.get("text", "")}
        if tool == "dns":
            host = _host(body.get("target"))
            lines = []
            try:
                ipaddress.ip_address(host)
                try:
                    name, aliases, _ = socket.gethostbyaddr(host)
                    lines.append("%s \u2192 %s" % (host, name))
                    lines += ["alias: %s" % a for a in aliases]
                except OSError:
                    lines.append("No reverse name (PTR) for %s." % host)
            except ValueError:
                try:
                    seen = []
                    for fam, _, _, _, addr in socket.getaddrinfo(host, None):
                        a = addr[0]
                        if a not in seen:
                            seen.append(a)
                            lines.append("%s  %s" % ("AAAA" if fam == socket.AF_INET6 else "A   ", a))
                except socket.gaierror as e:
                    return {"ok": False, "summary": "%s doesn't resolve: %s" % (host, e.strerror or e), "text": ""}
            return {"ok": True, "summary": "%d answer%s" % (len(lines), "" if len(lines) == 1 else "s"), "text": "\n".join(lines)}
        if tool == "port":
            host, port = _host(body.get("target")), _port(body.get("port"))
            r = check_tcp(host, port)
            return {"ok": r["ok"], "summary": ("%s:%d is open (%.1f ms)" % (host, port, r["ms"])) if r["ok"] else "%s:%d: %s" % (host, port, r["error"]), "text": ""}
        if tool == "http":
            url = str(body.get("target") or "").strip()
            url = _url(url if "://" in url else "http://" + url)
            r = check_http(url, verify=body.get("verify", True) is not False, expect="100-599")
            if not r.get("code"):
                return {"ok": False, "summary": r["error"], "text": ""}
            lines = [("HTTP %d in %.0f ms" if r["ms"] >= 10 else "HTTP %d in %.1f ms") % (r["code"], r["ms"])]
            if r.get("location"):
                lines.append("Redirects to: %s" % r["location"])
            if r.get("server"):
                lines.append("Server: %s" % r["server"])
            if r.get("cert_days") is not None:
                lines.append("Certificate valid for %d more days" % int(r["cert_days"]))
            return {"ok": r["code"] < 400, "summary": lines[0], "text": "\n".join(lines)}
        if tool == "trace":
            host = _host(body.get("target"))
            exe = shutil.which("tracepath") or shutil.which("traceroute")
            if not exe:
                return {"ok": False, "summary": "Neither tracepath nor traceroute is installed on this server.", "text": ""}
            argv = [exe, "-m", "20", host] if exe.endswith("tracepath") else [exe, "-m", "20", "-w", "2", "-q", "1", host]
            if exe.endswith("tracepath") and ":" in host:
                argv.insert(1, "-6")
            try:
                p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                text = p.stdout.decode("utf-8", "replace")
            except subprocess.TimeoutExpired as e:
                text = (e.output or b"").decode("utf-8", "replace") + "\n(stopped after 60 seconds)"
            hops = len([ln for ln in text.splitlines() if re.match(r"^\s*\d+[:\s]", ln)])
            return {"ok": True, "summary": "%d hop line%s" % (hops, "" if hops == 1 else "s"), "text": text[-8000:]}
        raise HomelabError("Pick ping, dns, port, http or trace.")

    # -- discovery and backup ---------------------------------------------------------

    def suggestions(self):
        """Services the cluster already exposes, ready to add."""
        snap = (self.store.snapshot()["state"] if self.store else None) or {}
        have = {s["target"] for s in self.cfg["services"]}
        control = next((n for n in snap.get("nodes", []) if any(r in ("control-plane", "master") for r in n.get("roles", []))), None)
        host = (control or {}).get("internal_ip") or ""
        out = []
        for ing in snap.get("ingresses", []):
            for h in ing.get("hosts", []) or []:
                url = "%s://%s/" % ("https" if ing.get("tls") else "http", h)
                if url not in have:
                    out.append({"name": ing.get("name") or h, "kind": "http", "target": url, "from": "Ingress %s/%s" % (ing.get("namespace", ""), ing.get("name", ""))})
        for s in snap.get("services", []):
            if s.get("namespace") in ("kube-system",):
                continue
            for p in s.get("node_ports") or []:
                if not host:
                    break
                t = "%s:%d" % (host, p)
                if t not in have:
                    out.append({"name": s["name"], "kind": "tcp", "target": t, "from": "NodePort %s/%s" % (s["namespace"], s["name"])})
            for ip in s.get("external_ips") or []:
                for port in s.get("ports") or []:
                    num = str(port).split("/")[0]
                    if num.isdigit():
                        t = "%s:%s" % (ip, num)
                        if t not in have:
                            out.append({"name": s["name"], "kind": "tcp", "target": t, "from": "LoadBalancer %s/%s" % (s["namespace"], s["name"])})
        return out[:60]

    def export(self):
        with self.lock:
            cfg = json.loads(json.dumps(self.cfg))
        return {"nodeyard": "homelab", "version": 1, "exported": int(time.time()), "services": cfg["services"], "wol": cfg["wol"],
                "notify": {k: v for k, v in self.notify_cfg().items()}}

    def import_(self, body):
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        if data.get("nodeyard") != "homelab":
            raise HomelabError("That isn't a nodeyard homelab backup.")
        replace = bool(body.get("replace"))
        added = 0
        with self.lock:
            services = [] if replace else list(self.cfg["services"])
            have = {(s["kind"], s["target"]) for s in services}
            for s in (data.get("services") or [])[:MAX_SERVICES]:
                try:
                    clean = self.clean_service(s if isinstance(s, dict) else {})
                except HomelabError:
                    continue
                if (clean["kind"], clean["target"]) not in have and len(services) < MAX_SERVICES:
                    services.append(clean)
                    have.add((clean["kind"], clean["target"]))
                    added += 1
            wol = [] if replace else list(self.cfg["wol"])
            macs = {d["mac"] for d in wol}
            for d in (data.get("wol") or [])[:MAX_WOL]:
                try:
                    dev = {"id": secrets.token_hex(5), "name": _clean_text(d.get("name"), 60) or "Machine", "mac": _mac(d.get("mac")),
                           "broadcast": str(ipaddress.IPv4Address(str(d.get("broadcast") or "255.255.255.255"))), "port": _port(d.get("port"), 9)}
                except (HomelabError, ValueError, AttributeError):
                    continue
                if dev["mac"] not in macs:
                    wol.append(dev)
                    macs.add(dev["mac"])
            self.cfg["services"], self.cfg["wol"] = services, wol
            if isinstance(data.get("notify"), dict) and (replace or not self.notify_cfg()["url"]):
                try:
                    n = data["notify"]
                    self.cfg["notify"] = {"enabled": bool(n.get("enabled")) and bool(n.get("url")), "kind": n.get("kind") if n.get("kind") in NOTIFY_KINDS else "ntfy",
                                          "url": _url(n["url"]) if n.get("url") else "", "level": "warning" if n.get("level") == "warning" else "critical",
                                          "resolved": n.get("resolved", True) is not False}
                except HomelabError:
                    pass
            self.save()
            for s in self.cfg["services"]:
                self.live.setdefault(s["id"], {"next": 0})
        return {"message": "Added %d service%s." % (added, "" if added == 1 else "s")}

    # -- metrics -------------------------------------------------------------------

    def metrics(self):
        snap = self.store.snapshot() if self.store else {"state": None, "alerts": []}
        st = snap["state"] or {}
        lines = []

        def esc(v):
            return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

        def metric(name, help_, kind, rows):
            lines.append("# HELP nodeyard_%s %s" % (name, help_))
            lines.append("# TYPE nodeyard_%s %s" % (name, kind))
            for labels, value in rows:
                if value is None:
                    continue
                lab = ",".join('%s="%s"' % (k, esc(v)) for k, v in labels.items())
                lines.append("nodeyard_%s%s %s" % (name, "{%s}" % lab if lab else "", repr(float(value)) if isinstance(value, float) else value))

        nodes = st.get("nodes", [])
        metric("up", "1 when the dashboard has fresh cluster data.", "gauge", [({}, 1 if st else 0)])
        metric("node_ready", "1 when Kubernetes reports the node Ready.", "gauge", [({"node": n["name"]}, 1 if n.get("ready") else 0) for n in nodes])
        metric("node_cpu_used_cores", "CPU in use, in cores.", "gauge", [({"node": n["name"]}, n.get("cpu_used")) for n in nodes])
        metric("node_cpu_cores", "CPU cores.", "gauge", [({"node": n["name"]}, n.get("cpu_cores")) for n in nodes])
        metric("node_memory_used_bytes", "Memory in use.", "gauge", [({"node": n["name"]}, n.get("mem_used")) for n in nodes])
        metric("node_memory_total_bytes", "Memory installed.", "gauge", [({"node": n["name"]}, n.get("mem_total")) for n in nodes])
        metric("node_disk_used_bytes", "Disk in use.", "gauge", [({"node": n["name"]}, n.get("disk_used")) for n in nodes])
        metric("node_disk_total_bytes", "Disk size.", "gauge", [({"node": n["name"]}, n.get("disk_total")) for n in nodes])
        metric("node_temperature_celsius", "Hottest sensor (node agent).", "gauge", [({"node": n["name"]}, (n.get("hw") or {}).get("temp_c")) for n in nodes])
        metric("node_network_receive_bytes_per_second", "Network receive rate.", "gauge", [({"node": n["name"]}, n.get("net_rx_rate")) for n in nodes])
        metric("node_network_transmit_bytes_per_second", "Network transmit rate.", "gauge", [({"node": n["name"]}, n.get("net_tx_rate")) for n in nodes])
        counts = {}
        for p in st.get("pods", []):
            counts[p.get("status", "")] = counts.get(p.get("status", ""), 0) + 1
        metric("pods", "Pods by status.", "gauge", [({"status": k}, v) for k, v in sorted(counts.items())])
        metric("workload_ready", "Ready replicas.", "gauge", [({"namespace": w["namespace"], "kind": w["kind"], "name": w["name"]}, w.get("ready")) for w in st.get("workloads", [])
                                                           if w["kind"] in ("Deployment", "StatefulSet", "DaemonSet")])
        metric("workload_desired", "Desired replicas.", "gauge", [({"namespace": w["namespace"], "kind": w["kind"], "name": w["name"]}, w.get("desired")) for w in st.get("workloads", [])
                                                               if w["kind"] in ("Deployment", "StatefulSet", "DaemonSet")])
        levels = {"critical": 0, "warning": 0, "info": 0}
        for a in snap.get("alerts") or []:
            levels[a.get("level", "info")] = levels.get(a.get("level", "info"), 0) + 1
        metric("alerts", "Open alerts by level.", "gauge", [({"level": k}, v) for k, v in levels.items()])
        svcs = self.services()
        metric("service_up", "1 when the service's last check passed.", "gauge", [({"service": s["name"], "kind": s["kind"]}, 1 if s["status"] == "up" else 0)
                                                                               for s in svcs if s["status"] not in ("paused", "pending")])
        metric("service_latency_ms", "Latency of the last good check.", "gauge", [({"service": s["name"]}, (s["last"] or {}).get("ms")) for s in svcs if s["status"] == "up"])
        metric("service_uptime_ratio", "Share of good checks over 24 h.", "gauge", [({"service": s["name"]}, s["uptime"] / 100.0 if s["uptime"] is not None else None) for s in svcs])
        metric("service_certificate_days", "Days until the HTTPS certificate expires.", "gauge", [({"service": s["name"]}, round(s["cert_days"], 2) if s["cert_days"] is not None else None) for s in svcs])
        return "\n".join(lines) + "\n"

    # -- the loop --------------------------------------------------------------------

    def loop(self):
        last_alerts = 0.0
        while not self.stop.wait(2):
            for svc in self.due():
                self.pool.submit(self._run, svc)
            now = time.time()
            if self.store is not None and now - last_alerts >= 5:
                last_alerts = now
                try:
                    self.watch_alerts(self.store.snapshot()["alerts"] or [])
                except Exception:   # notifications must never stop the checks
                    pass
            self.save_history()


def register(ctx, args):
    state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    demo = bool(getattr(args, "demo", False))
    hl = Homelab(state_dir, getattr(ctx, "store", None), demo=demo)
    if demo and not hl.cfg["services"] and not os.path.exists(hl.path):
        for s in ({"name": "Home Assistant", "kind": "http", "target": "https://ha.home.example/", "group": "Home"},
                  {"name": "Jellyfin", "kind": "http", "target": "http://192.168.1.20:8096/", "group": "Media"},
                  {"name": "NAS SSH", "kind": "tcp", "target": "192.168.1.30:22", "group": "Storage"},
                  {"name": "Router", "kind": "ping", "target": "192.168.1.1", "group": "Network"},
                  {"name": "Old backup box (down)", "kind": "tcp", "target": "192.168.1.40:22", "group": "Storage"}):
            hl.cfg["services"].append(hl.clean_service(s))
        hl.cfg["wol"].append({"id": secrets.token_hex(5), "name": "Gaming PC", "mac": "aa:bb:cc:dd:ee:01", "broadcast": "255.255.255.255", "port": 9})
    ctx.homelab = hl
    store = getattr(ctx, "store", None)
    if store is not None and hasattr(store, "alert_hooks"):
        store.alert_hooks.append(hl.alerts)

    def wrap(fn):
        def run(h, arg):
            try:
                r = fn(h, arg)
            except HomelabError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            if isinstance(r, dict) and "ok" not in r:
                r = dict(r, ok=True)
            return h._json(r)
        return run

    def metrics(h, q):
        body = hl.metrics().encode("utf-8")
        h._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

    ctx.get_routes.update({
        "/api/homelab/services": wrap(lambda h, q: {"services": hl.services(), "notify": hl.notify_view(), "wol": hl.cfg["wol"]}),
        "/api/homelab/suggestions": wrap(lambda h, q: {"suggestions": hl.suggestions()}),
        "/api/homelab/export": lambda h, q: h._json(hl.export(), extra={"Content-Disposition": 'attachment; filename="nodeyard-homelab.json"'}),
        "/api/v1/metrics": metrics,
    })
    ctx.post_routes.update({
        "/api/homelab/service": wrap(lambda h, b: {"service": hl.update_service(b) if b.get("id") else hl.add_service(b)}),
        "/api/homelab/service-delete": wrap(lambda h, b: (hl.delete_service(str(b.get("id") or "")), {})[1]),
        "/api/homelab/check": wrap(lambda h, b: {"service": hl.check_now(str(b.get("id") or ""))}),
        "/api/homelab/notify": wrap(lambda h, b: {"notify": hl.set_notify(b)}),
        "/api/homelab/notify-test": wrap(lambda h, b: hl.test_notify()),
        "/api/homelab/wol": wrap(lambda h, b: {"wol": hl.wol_delete(str(b.get("id"))) if b.get("action") == "delete" else hl.wol_add(b)}),
        "/api/homelab/wake": wrap(lambda h, b: hl.wake(b)),
        "/api/homelab/tool": wrap(lambda h, b: hl.tool(b)),
        "/api/homelab/import": wrap(lambda h, b: hl.import_(b)),
    })
    ctx.post_limits["/api/homelab/import"] = 2 * 1024 * 1024
    threading.Thread(target=hl.loop, daemon=True, name="homelab").start()
