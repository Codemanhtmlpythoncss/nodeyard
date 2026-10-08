"""The Settings and Doctor pages' back end, and the custom background picture.

Everything that changes the machine goes through nodeyard itself (never a
shell): secrets are passed on stdin, and changes to the dashboard's own
service are handed to systemd so the restart doesn't cut this request off.
The page's own preferences (background, blur, dim) live in --state-dir.
"""
import base64
import ipaddress
import json
import os
import re
import subprocess
import threading
import time

IMAGE_MAX = 8 * 1024 * 1024                     # the picture itself
BODY_MAX = IMAGE_MAX * 4 // 3 + 64 * 1024       # ... sent as base64 JSON
KINDS = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
LISTEN_RE = re.compile(r"^[a-z0-9.:]+(,[a-z0-9.:]+)*$")  # each item is then checked on its own
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "NODEYARD_COLOR": "never"}

DEMO_DOCTOR = {"ok": True, "issues": 1, "warnings": 2, "fixed": 0, "checks": [
    {"id": "deps", "category": "nodeyard", "title": "nodeyard's required tools are installed", "status": "ok", "detail": None, "fix": None},
    {"id": "k3s-active", "category": "k3s", "title": "k3s service is running", "status": "ok", "detail": None, "fix": "restart the k3s service"},
    {"id": "time-sync", "category": "host", "title": "clock is synchronised", "status": "warn", "detail": "systemd-timesyncd is not running", "fix": "enable time sync"},
    {"id": "disk-space", "category": "host", "title": "enough free disk space", "status": "warn", "detail": "/ is 91% full on yard-3", "fix": None},
    {"id": "nodes-ready", "category": "cluster", "title": "every node is Ready", "status": "fail", "detail": "yard-4 is NotReady", "fix": None},
    {"id": "ip-forward", "category": "host", "title": "IP forwarding enabled", "status": "ok", "detail": None, "fix": "set net.ipv4.ip_forward=1 (persistently)"},
]}


def image_kind(data):
    """The picture's type from its first bytes (never from its name)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class SettingsError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Settings:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.bin = getattr(args, "nodeyard_bin", "") or ""
        self.password_file = getattr(args, "password_file", "") or ""
        self.args = args
        d = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        if self.demo:
            d = os.path.join("/tmp", "nodeyard-demo-dashboard-%d" % os.getuid())
        self.dir = d
        self.lock = threading.Lock()
        self.doctor_cache = None
        self.public_cache = None
        self.commands_cache = None
        self.weak_cache = None

    # -- the page's own preferences ---------------------------------------------

    def _prefs_path(self):
        return os.path.join(self.dir, "prefs.json")

    def prefs(self):
        try:
            with open(self._prefs_path(), "r", encoding="utf-8") as f:
                p = json.load(f)
            return p if isinstance(p, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_prefs(self, p):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        tmp = self._prefs_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f)
        os.replace(tmp, self._prefs_path())

    def view(self, token):
        st = (self.ctx.store.snapshot()["state"] or {})
        split = (st.get("ai") or {}).get("split") or {}
        a = self.args
        p = self.prefs()
        return {
            "listen": getattr(a, "listen", ""), "port": getattr(a, "port", 0), "interval": self.ctx.store.interval,
            "cluster_name": getattr(a, "cluster_name", "") or "",
            "auth": self.ctx.auth is not None, "sessions": self.ctx.auth.count() if self.ctx.auth else 0,
            "demo": self.demo, "can_run": self.demo or bool(self.bin and os.access(self.bin, os.X_OK)),
            "hf_token": self._secret_exists("hf-token"), "model_key": self._secret_exists("ai-split-api-key"),
            "agents": bool((st.get("agents") or {}).get("installed")),
            "gate": split.get("gate"), "split": bool(split),
            "background": p.get("bg"),
            "public": self.public_state(),
            "weak_password": self.weak_ok(),
            "nodes": [{"name": n["name"], "disk_limit": n.get("disk_limit")} for n in st.get("nodes", [])],
        }

    def _secret_exists(self, name):
        if self.demo:
            return name != "hf-token"
        try:
            return os.path.getsize(os.path.join("/etc/nodeyard/secrets", name)) > 0
        except OSError:
            return False

    # -- running nodeyard ---------------------------------------------------------

    def _run(self, argv, stdin="", timeout=60):
        if self.demo:
            return 0, "(demo: nothing was changed)"
        if not self.bin or not os.access(self.bin, os.X_OK):
            raise SettingsError("This dashboard can't run nodeyard commands (nodeyard wasn't found).", 501)
        try:
            r = subprocess.run([self.bin, "--no-color"] + argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=ENV)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SettingsError("nodeyard didn't finish: %s" % e, 502)
        out = (r.stdout + r.stderr).strip()
        return r.returncode, out

    @staticmethod
    def _last_error(out):
        lines = [x for x in out.splitlines() if x.strip()]
        bad = [x for x in lines if "Error" in x or x.startswith("✗")]
        return (bad or lines or ["It didn't work."])[-1].replace("✗ Error: ", "").strip()[:300]

    # -- sign-in --------------------------------------------------------------------

    def set_password(self, body, token):
        if self.ctx.auth is None:
            raise SettingsError("This dashboard has no sign-in (it only answers on this machine).")
        if body.get("random") is True:
            rc, out = self._run(["dashboard", "password", "--random", "--no-restart"])
            if rc:
                raise SettingsError(self._last_error(out), 500)
            if self.demo:
                pw = "DEMO-0000-0000-0000-0000-0000"
            else:
                with open(self.password_file, "r", encoding="utf-8") as f:
                    pw = f.read().strip()
        else:
            pw = str(body.get("password", ""))
            least = 1 if self.weak_ok() else 6
            if len(pw) < least or len(pw) > 200 or any(ord(c) < 32 for c in pw):
                raise SettingsError("Use %d to 200 characters (no control characters)." % least)
            if pw != str(body.get("again", "")):
                raise SettingsError("The two passwords don't match.")
            rc, out = self._run(["dashboard", "password", "--stdin", "--no-restart"], stdin=pw + "\n")
            if rc:
                raise SettingsError(self._last_error(out), 500)
        self.ctx.auth.set_password(pw, keep=token, min_length=1 if self.weak_ok() else 6)
        return {"password": pw} if body.get("random") is True else {}

    def weak_ok(self):
        """`dashboard.weak-password` in the cluster config (cached for 30 s)."""
        if self.demo:
            return bool(getattr(self, "_demo_weak", False))
        with self.lock:
            if self.weak_cache and time.time() - self.weak_cache[0] < 30:
                return self.weak_cache[1]
        try:
            rc, out = self._run(["config", "get", "dashboard.weak-password"], timeout=20)
            val = rc == 0 and out.strip().splitlines()[-1:] == ["true"]
        except SettingsError:
            val = False
        with self.lock:
            self.weak_cache = (time.time(), val)
        return val

    def set_weak(self, body):
        on = body.get("on") is True
        if self.demo:
            self._demo_weak = on
        else:
            rc, out = self._run(["config", "set", "dashboard.weak-password", "true"] if on else ["config", "unset", "dashboard.weak-password"])
            if rc:
                raise SettingsError(self._last_error(out), 500)
        with self.lock:
            self.weak_cache = None
        return {"weak_password": self.weak_ok()}

    def signout_all(self):
        if self.ctx.auth is None:
            raise SettingsError("This dashboard has no sign-in.")
        return {"signed_out": self.ctx.auth.signout_all()}

    # -- secrets --------------------------------------------------------------------------

    def set_model_key(self, body):
        key = str(body.get("key", "")).strip()
        if not re.match(r"^[A-Za-z0-9._~+/=-]{16,200}$", key):
            raise SettingsError("The key needs 16 to 200 characters: letters, digits and . _ ~ + / = -")
        rc, out = self._run(["ai", "split", "key", "--stdin", "--yes"], stdin=key + "\n", timeout=120)
        if rc:
            raise SettingsError(self._last_error(out), 500)
        return {}

    def adopt_model_key(self):
        rc, out = self._run(["ai", "key", "--adopt-model", "--yes"], timeout=60)
        if rc:
            raise SettingsError(self._last_error(out), 500)
        return {}

    def set_hf_token(self, body):
        if body.get("remove") is True:
            rc, out = self._run(["ai", "hf", "token", "--remove"])
        else:
            tok = str(body.get("token", "")).strip()
            if not re.match(r"^hf_[A-Za-z0-9]{20,100}$", tok):
                raise SettingsError("That doesn't look like a Hugging Face token (they start with hf_).")
            rc, out = self._run(["ai", "hf", "token", "--stdin"], stdin=tok + "\n")
        if rc:
            raise SettingsError(self._last_error(out), 500)
        hf = getattr(self.ctx.ai, "hf", None)
        if hf is not None and hasattr(hf, "reload_token"):
            hf.reload_token()
        return {}

    # -- the dashboard service --------------------------------------------------------------

    def apply_service(self, body):
        try:
            port = int(body.get("port"))
            interval = int(body.get("interval"))
        except (TypeError, ValueError):
            raise SettingsError("The port and refresh interval are whole numbers.")
        listen = str(body.get("listen", "")).strip().lower().replace(" ", "")
        if not 1024 <= port <= 65535:
            raise SettingsError("Pick a port from 1024 to 65535.")
        if not 1 <= interval <= 300:
            raise SettingsError("The refresh interval is 1 to 300 seconds.")
        if not LISTEN_RE.match(listen):
            raise SettingsError("Listen on: auto, local, tailscale, all, or IP addresses separated by commas.")
        for item in listen.split(","):
            if item not in ("auto", "local", "tailscale", "all"):
                try:
                    ipaddress.ip_address(item)
                except ValueError:
                    raise SettingsError("%s isn't an IP address." % item)
        if self.demo:
            return {"restarting": False}
        # systemd runs it outside this service, so restarting the dashboard
        # doesn't kill the command halfway through.
        unit = "nodeyard-dashboard-apply-%d" % int(time.time())
        try:
            subprocess.run(["systemd-run", "--quiet", "--collect", "--unit", unit, "--on-active=2", self.bin, "--no-color",
                            "dashboard", "start", "--port", str(port), "--listen", listen, "--interval", str(interval), "--yes"],
                           capture_output=True, text=True, timeout=20, env=ENV, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise SettingsError("Couldn't hand the change to systemd: %s" % e, 500)
        return {"restarting": True, "port": port}

    # -- public access (Tailscale Funnel) -----------------------------------------------------

    def public_state(self):
        """{'name', 'dashboard': url|None, 'api': url|None} from `nodeyard public status --json` (30 s cache)."""
        if self.demo:
            return {"name": "yard-1.tail1234.ts.net", "dashboard": None, "api": "https://yard-1.tail1234.ts.net:10000/v1", "available": True}
        with self.lock:
            if self.public_cache and time.time() - self.public_cache[0] < 30:
                return self.public_cache[1]
        try:
            rc, out = self._run(["public", "status", "--json"], timeout=20)
            data = json.loads([x for x in out.splitlines() if x.startswith("{")][-1]) if rc == 0 else None
        except (SettingsError, ValueError, IndexError):
            data = None
        st = {"name": data.get("name"), "dashboard": data.get("dashboard"), "api": data.get("api"), "available": True} if isinstance(data, dict) \
            else {"name": None, "dashboard": None, "api": None, "available": False}
        with self.lock:
            self.public_cache = (time.time(), st)
        return st

    def forget_public(self):
        with self.lock:
            self.public_cache = None

    # -- every command (the Commands page) -------------------------------------------------------

    def commands(self):
        """`nodeyard commands --json`, cached for 10 minutes."""
        with self.lock:
            if self.commands_cache and time.time() - self.commands_cache[0] < 600:
                return self.commands_cache[1]
        argv = ["commands", "--json"]
        if self.demo:
            demo_bin = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "bin", "nodeyard"))
            try:
                r = subprocess.run(["bash", demo_bin, "--demo", "--no-color"] + argv, capture_output=True, text=True, timeout=120,
                                   env=dict(ENV, NODEYARD_DEMO_DIR=os.path.join("/tmp", "nodeyard-demo-%d" % os.getuid())))
                rc, out = r.returncode, r.stdout
            except (OSError, subprocess.TimeoutExpired) as e:
                raise SettingsError("Couldn't list the commands: %s" % e, 502)
        else:
            rc, out = self._run(argv, timeout=120)
        try:
            data = json.JSONDecoder().raw_decode(out[out.index('{"ok"'):])[0]["commands"]
        except (ValueError, IndexError, KeyError, TypeError):
            raise SettingsError("Couldn't list the commands.", 502)
        with self.lock:
            self.commands_cache = (time.time(), data)
        return data

    def command_paths(self):
        try:
            return {c["path"] for c in self.commands()}
        except SettingsError:
            return set()

    # -- doctor --------------------------------------------------------------------------------

    def doctor(self, fresh):
        if self.demo:
            return DEMO_DOCTOR
        with self.lock:
            if not fresh and self.doctor_cache and time.time() - self.doctor_cache[0] < 20:
                return self.doctor_cache[1]
        rc, out = self._run(["doctor", "--json"], timeout=180)
        try:
            data = json.loads([x for x in out.splitlines() if x.startswith("{")][-1])
        except (ValueError, IndexError):
            raise SettingsError("doctor didn't answer in JSON: " + self._last_error(out), 502)
        with self.lock:
            self.doctor_cache = (time.time(), data)
        return data

    # -- background picture -----------------------------------------------------------------------

    def _bg_path(self, kind):
        return os.path.join(self.dir, "background." + kind)

    def set_background(self, body):
        raw = str(body.get("image", ""))
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError:
            raise SettingsError("That isn't a picture.")
        if not data:
            raise SettingsError("The picture is empty.")
        if len(data) > IMAGE_MAX:
            raise SettingsError("The picture is too big (8 MB at most).", 413)
        kind = image_kind(data)
        if kind is None:
            raise SettingsError("Use a PNG, JPEG or WebP picture.")
        with self.lock:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            for k in KINDS:
                if k != kind:
                    try:
                        os.remove(self._bg_path(k))
                    except OSError:
                        pass
            tmp = self._bg_path(kind) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._bg_path(kind))
            p = self.prefs()
            old = p.get("bg") or {}
            p["bg"] = {"kind": kind, "v": int(time.time()), "blur": old.get("blur", 0), "dim": old.get("dim", 55)}
            self._save_prefs(p)
            return p["bg"]

    def background_style(self, body):
        try:
            blur = max(0, min(40, int(body.get("blur", 0))))
            dim = max(0, min(95, int(body.get("dim", 45))))
        except (TypeError, ValueError):
            raise SettingsError("Blur and dim are numbers.")
        with self.lock:
            p = self.prefs()
            if not p.get("bg"):
                raise SettingsError("Upload a picture first.")
            p["bg"].update({"blur": blur, "dim": dim})
            self._save_prefs(p)
            return p["bg"]

    def remove_background(self):
        with self.lock:
            for k in KINDS:
                try:
                    os.remove(self._bg_path(k))
                except OSError:
                    pass
            p = self.prefs()
            p.pop("bg", None)
            self._save_prefs(p)
        return {}

    def background_file(self):
        bg = self.prefs().get("bg") or {}
        kind = bg.get("kind")
        if kind not in KINDS:
            return None, None
        try:
            with open(self._bg_path(kind), "rb") as f:
                return f.read(), KINDS[kind]
        except OSError:
            return None, None


def register(ctx, args):
    s = Settings(ctx, args)
    ctx.settings = s

    def wrap(fn):
        def route(h, arg):
            try:
                h._json(dict(fn(h, arg) or {}, ok=True))
            except SettingsError as e:
                h._json({"ok": False, "error": str(e)}, e.code)
            except (OSError, ValueError) as e:
                h._json({"ok": False, "error": str(e)}, 500)
        return route

    def background(h, q):
        data, ctype = s.background_file()
        if data is None:
            return h._send(404, "No background", "text/plain; charset=utf-8")
        h._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})

    ctx.get_routes.update({
        "/api/settings": wrap(lambda h, q: s.view(h._token())),
        "/api/doctor": wrap(lambda h, q: {"doctor": s.doctor(bool(q.get("fresh")))}),
        "/api/background": background,
        "/api/commands": wrap(lambda h, q: {"commands": s.commands(), "public": h._forwarded() is not None}),
    })
    ctx.post_routes.update({
        "/api/settings/password": wrap(lambda h, b: s.set_password(b, h._token())),
        "/api/settings/signout-all": wrap(lambda h, b: s.signout_all()),
        "/api/settings/weak-password": wrap(lambda h, b: s.set_weak(b)),
        "/api/settings/model-key": wrap(lambda h, b: s.set_model_key(b)),
        "/api/settings/adopt-model-key": wrap(lambda h, b: s.adopt_model_key()),
        "/api/settings/hf-token": wrap(lambda h, b: s.set_hf_token(b)),
        "/api/settings/service": wrap(lambda h, b: s.apply_service(b)),
        "/api/settings/background": wrap(lambda h, b: {"background": s.set_background(b)}),
        "/api/settings/background-style": wrap(lambda h, b: {"background": s.background_style(b)}),
        "/api/settings/background-remove": wrap(lambda h, b: s.remove_background()),
        "/api/settings/public-refresh": wrap(lambda h, b: (s.forget_public(), {"public": s.public_state()})[1]),
    })
    ctx.post_limits["/api/settings/background"] = BODY_MAX
    try:
        import aiapi
        aiapi.COMMAND_PATHS = s.command_paths
    except ImportError:
        pass
