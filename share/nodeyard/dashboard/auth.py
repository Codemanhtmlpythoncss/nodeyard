"""Sign-in for the dashboard: one password, short-lived session cookies, lock-out after repeated failures.

The password lives in a root-only file (see `nodeyard dashboard password`). Everything here is
standard library; comparisons are constant-time.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

MIN_LENGTH = 6
# nodeyard's own generated passwords (XXXX-XXXX-...): dashes, spaces and case don't matter when typing them.
GENERATED = re.compile(r"^[0-9A-F]{4}(-[0-9A-F]{4}){5}$")

SESSION_SECONDS = 12 * 3600
WINDOW = 300.0          # failures are counted over this many seconds
PER_IP_LIMIT = 5        # failures from one address before it is locked out
GLOBAL_LIMIT = 30       # failures from anywhere before everyone waits
COOKIE = "nd_session"


def normalise(text):
    """A generated password as typed: dashes, spaces and case don't matter."""
    return "".join(c for c in (text or "") if c.isalnum()).upper()


class Auth:
    def __init__(self, password):
        # Any stored password is used as it is (a minimum length only applies when one is
        # set, and `dashboard.weak-password` can turn that off), so the service always starts.
        password = (password or "").strip()
        if not password:
            raise ValueError("the dashboard password is empty")
        # A password you chose is compared exactly; a generated one forgives typing slips.
        self.lenient = bool(GENERATED.match(password))
        self.key = self._canon(password)
        self.lock = threading.Lock()
        self.sessions = {}   # token -> expiry
        self.failures = {}   # ip -> [timestamps]
        self.all_failures = []

    def _canon(self, text):
        return normalise(text) if self.lenient else (text or "").strip()

    def _prune(self, now):
        for ip in list(self.failures):
            self.failures[ip] = [t for t in self.failures[ip] if now - t < WINDOW]
            if not self.failures[ip]:
                del self.failures[ip]
        self.all_failures = [t for t in self.all_failures if now - t < WINDOW]
        for tok in [t for t, exp in self.sessions.items() if exp < now]:
            del self.sessions[tok]

    def retry_after(self, ip, now=None, public=False):
        """Seconds this address must wait before trying again (0 = go ahead). The limit for
        everyone together only applies to sign-ins from the internet (public=True), so
        strangers can't lock you out on your own network or Tailscale."""
        now = now or time.time()
        with self.lock:
            self._prune(now)
            wait = 0.0
            mine = self.failures.get(ip, [])
            if len(mine) >= PER_IP_LIMIT:
                wait = max(wait, WINDOW - (now - mine[0]))
            if public and len(self.all_failures) >= GLOBAL_LIMIT:
                wait = max(wait, WINDOW - (now - self.all_failures[0]))
            return int(wait) + 1 if wait > 0 else 0

    def login(self, ip, supplied, now=None, public=False):
        """Returns a new session token, or None if the password is wrong."""
        now = now or time.time()
        ok = hmac.compare_digest(self._canon(supplied).encode(), self.key.encode())
        with self.lock:
            self._prune(now)
            if not ok:
                self.failures.setdefault(ip, []).append(now)
                if public:
                    self.all_failures.append(now)
                return None
            self.failures.pop(ip, None)
            token = secrets.token_hex(32)
            self.sessions[token] = now + SESSION_SECONDS
            return token

    def valid(self, token, now=None):
        if not token:
            return False
        now = now or time.time()
        with self.lock:
            exp = self.sessions.get(token)
            if exp is None:
                return False
            if exp < now:
                del self.sessions[token]
                return False
            return True

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def set_password(self, password, keep=None, min_length=MIN_LENGTH):
        """Use a new password from now on. Every session except KEEP (the
        browser that changed it) has to sign in again."""
        password = (password or "").strip()
        if len(password) < max(1, min_length):
            raise ValueError("the dashboard password is too short (%d characters at least)" % max(1, min_length))
        with self.lock:
            self.lenient = bool(GENERATED.match(password))
            self.key = self._canon(password)
            self.sessions = {t: e for t, e in self.sessions.items() if t == keep}

    def signout_all(self):
        """Every browser, this one included, has to sign in again."""
        with self.lock:
            n = len(self.sessions)
            self.sessions = {}
            return n

    def count(self):
        with self.lock:
            self._prune(time.time())
            return len(self.sessions)


# -- "remember me this month" -------------------------------------------------
# After a correct password, a browser is remembered for a month so a dashboard update (which drops
# the in-memory sessions) doesn't make you sign in again. Recognition is by a long-lived cookie, or,
# on your own network, by the exact IP + MAC the browser signed in from. The record of those
# addresses is the "log" kept here. This never applies to sign-ins coming in over public access.
REMEMBER_COOKIE = "nd_remember"
REMEMBER_SECONDS = 31 * 24 * 3600     # about a month
TOUCH_SECONDS = 300                    # don't rewrite the store more often than this per device
MAX_LOG = 8                            # addresses kept per device


def _read_arp():
    """IPv4 -> MAC for the host's current neighbours, from the kernel's ARP table (Linux only)."""
    out = {}
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            next(f, None)   # header row
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] and parts[3] != "00:00:00:00:00:00" and parts[2] != "0x0":
                    out[parts[0]] = parts[3].lower()
    except OSError:
        pass
    return out


class Remember:
    """Devices that signed in with the password, kept for a month in a root-only JSON file."""

    def __init__(self, path, enabled=True):
        self.path = path
        self.lock = threading.Lock()
        self.devices = {}        # id -> {ip, mac, first, last, label, log:[{ip, mac, at}]}
        self.enabled = enabled
        self._arp = ({}, 0.0)
        self._load()

    # -- storage --------------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            devs = data.get("devices")
            if isinstance(devs, dict):
                self.devices = {k: v for k, v in devs.items() if isinstance(v, dict)}
            if "enabled" in data:
                self.enabled = bool(data.get("enabled"))

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled, "devices": self.devices}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- MAC lookups ----------------------------------------------------------
    def mac_for(self, ip, now=None):
        """The MAC the host sees for a direct-neighbour IPv4 address, or "" (loopback, Tailscale, remote)."""
        if not ip or ":" in ip:
            return ""
        now = now or time.time()
        table, at = self._arp
        if now - at >= 2.0:
            table = _read_arp()
            self._arp = (table, now)
        return table.get(ip, "")

    @staticmethod
    def _id(token):
        # the file holds a hash, never the cookie value, so a leaked file can't be used to sign in
        return hashlib.sha256(("ny-remember:" + (token or "")).encode()).hexdigest()[:24]

    def _prune(self, now):
        dead = [k for k, d in self.devices.items() if now - float(d.get("first") or 0) > REMEMBER_SECONDS]
        for k in dead:
            del self.devices[k]
        return bool(dead)

    # -- the month-long remember ---------------------------------------------
    def remember(self, ip, mac, label="", now=None):
        """Record a browser that just signed in; return the token to put in its cookie ("" if off)."""
        if not self.enabled:
            return ""
        now = now or time.time()
        token = secrets.token_hex(32)
        with self.lock:
            self._prune(now)
            self.devices[self._id(token)] = {
                "ip": ip or "", "mac": mac or "", "first": now, "last": now,
                "label": (label or "")[:60], "log": [{"ip": ip or "", "mac": mac or "", "at": now}]}
            self._save()
        return token

    def recognised(self, token, ip, mac, now=None, public=False):
        """True when a non-public request comes from a device remembered within the month: by its
        cookie, or (no usable cookie) by the exact IP+MAC it last signed in from on your network."""
        if public or not self.enabled:
            return False
        now = now or time.time()
        with self.lock:
            changed = self._prune(now)
            dev = None
            did = self._id(token) if token else ""
            if did and did in self.devices:
                dev = self.devices[did]
            elif ip and mac:
                for d in self.devices.values():
                    seen = [d] + list(d.get("log") or [])
                    if any(e.get("ip") == ip and e.get("mac") == mac for e in seen):
                        dev = d
                        break
            ok = dev is not None and now - float(dev.get("first") or 0) <= REMEMBER_SECONDS
            if ok and self._touch(dev, ip, mac, now):
                changed = True
            if changed:
                self._save()
            return ok

    def _touch(self, dev, ip, mac, now):
        """Keep the device's last-seen time and address log current, but write rarely."""
        changed = False
        new_addr = bool(ip) and (dev.get("ip") != ip or (mac and dev.get("mac") != mac))
        if new_addr:
            dev["ip"] = ip
            if mac:
                dev["mac"] = mac
            log = dev.setdefault("log", [])
            if not any(e.get("ip") == ip and (e.get("mac") == mac or not mac) for e in log):
                log.insert(0, {"ip": ip, "mac": mac or "", "at": now})
                del log[MAX_LOG:]
            dev["last"] = now
            changed = True
        elif now - float(dev.get("last") or 0) >= TOUCH_SECONDS:
            dev["last"] = now
            changed = True
        return changed

    # -- management -----------------------------------------------------------
    def set_enabled(self, on):
        with self.lock:
            self.enabled = bool(on)
            if not self.enabled:
                self.devices = {}       # off means: ask for the password again everywhere
            self._save()
        return self.enabled

    def forget(self, device_id):
        with self.lock:
            if device_id in self.devices:
                del self.devices[device_id]
                self._save()
                return True
        return False

    def forget_token(self, token):
        return self.forget(self._id(token)) if token else False

    def forget_all(self):
        with self.lock:
            n = len(self.devices)
            self.devices = {}
            self._save()
            return n

    def count(self, now=None):
        now = now or time.time()
        with self.lock:
            self._prune(now)
            return len(self.devices)

    def view(self, now=None):
        """For the Sign-in page: each remembered device, newest first, without any secret."""
        now = now or time.time()
        with self.lock:
            self._prune(now)
            out = []
            for did, d in self.devices.items():
                out.append({"id": did, "ip": d.get("ip", ""), "mac": d.get("mac", ""),
                            "label": d.get("label", ""), "first": d.get("first"), "last": d.get("last"),
                            "expires": float(d.get("first") or 0) + REMEMBER_SECONDS,
                            "history": [{"ip": e.get("ip", ""), "mac": e.get("mac", ""), "at": e.get("at")}
                                        for e in (d.get("log") or [])][:MAX_LOG]})
            out.sort(key=lambda x: x.get("last") or 0, reverse=True)
            return {"enabled": self.enabled, "devices": out}
