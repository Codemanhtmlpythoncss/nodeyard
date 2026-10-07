"""Sign-in for the dashboard: one password, short-lived session cookies, lock-out after repeated failures.

The password lives in a root-only file (see `nodeyard dashboard password`). Everything here is
standard library; comparisons are constant-time.
"""
import hmac
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
