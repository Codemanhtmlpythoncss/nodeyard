"""The Settings and Doctor pages' back end, and the custom background picture.

Everything that changes the machine goes through nodeyard itself (never a
shell): secrets are passed on stdin, and changes to the dashboard's own
service are handed to systemd so the restart doesn't cut this request off.
The page's own preferences (background, blur, dim) live in --state-dir.
"""
import base64
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

import tsserve

IMAGE_MAX = 8 * 1024 * 1024                     # the picture itself
BODY_MAX = IMAGE_MAX * 4 // 3 + 64 * 1024       # ... sent as base64 JSON
KINDS = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "mp4": "video/mp4", "webm": "video/webm"}
VIDEO_KINDS = ("mp4", "webm")
VIDEO_MAX = 80 * 1024 * 1024                    # a background video, uploaded in pieces
CHUNK_MAX = 3 * 1024 * 1024                     # one piece of an upload
UPLOAD_RE = re.compile(r"^[0-9a-f]{16,32}$")
LIB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
LIB_MAX_ITEMS = 30                              # uploaded pictures and videos kept to pick again
LIB_MAX_BYTES = 1024 * 1024 * 1024
# Built-in backgrounds (drawn by the page with CSS, see style.css .bgp-*). A new dashboard starts on the first.
PRESETS = ("aurora", "sunset", "ocean", "nebula", "forest", "graphite", "galaxy", "warp", "constellation")   # (the last three move)
DEFAULT_BG = {"kind": "preset", "preset": PRESETS[0], "v": 0, "blur": 0, "dim": 0, "default": True}
LISTEN_RE = re.compile(r"^[a-z0-9.:]+(,[a-z0-9.:]+)*$")  # each item is then checked on its own
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "NODEYARD_COLOR": "never"}
CHAT_DEFAULTS = {"system": "", "temperature": 0.7, "max_tokens": 1024, "compress": "auto", "web": False,
                 "skills_auto": True, "autofix": False, "files": "ask", "context_length": 8192}

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


def media_kind(data):
    """A background's type from its first bytes: a picture, or an MP4 / WebM video (not QuickTime .mov)."""
    kind = image_kind(data)
    if kind:
        return kind
    if data[4:8] == b"ftyp" and data[8:10] != b"qt":
        return "mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
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

    @staticmethod
    def _clean_chat_defaults(value):
        out = dict(CHAT_DEFAULTS)
        if not isinstance(value, dict):
            return out
        system = value.get("system", out["system"])
        if isinstance(system, str) and len(system) <= 12000:
            out["system"] = system
        try:
            temperature = float(value.get("temperature", out["temperature"]))
            if math.isfinite(temperature) and 0 <= temperature <= 2:
                out["temperature"] = temperature
        except (TypeError, ValueError):
            pass
        for key, low, high in (("max_tokens", 0, 65536), ("context_length", 512, 131072)):
            raw = value.get(key, out[key])
            if isinstance(raw, bool):
                continue
            try:
                number = int(raw)
                if str(number) == str(raw).strip() and low <= number <= high and (key != "max_tokens" or number == 0 or number >= 16):
                    out[key] = number
            except (TypeError, ValueError):
                pass
        if value.get("compress") in ("auto", "off"):
            out["compress"] = value["compress"]
        if value.get("files") in ("ask", "always", "never"):
            out["files"] = value["files"]
        for key in ("web", "skills_auto", "autofix"):
            if isinstance(value.get(key), bool):
                out[key] = value[key]
        return out

    def chat_defaults(self, prefs=None):
        p = self.prefs() if prefs is None else prefs
        return self._clean_chat_defaults(p.get("ai_chat_defaults"))

    def set_chat_defaults(self, body):
        value = body.get("defaults", body)
        if not isinstance(value, dict):
            raise SettingsError("Chat defaults must be an object.")
        candidate = dict(self.chat_defaults())
        candidate.update({k: v for k, v in value.items() if k in CHAT_DEFAULTS})
        if "system" in value and (not isinstance(value["system"], str) or len(value["system"]) > 12000):
            raise SettingsError("The system prompt must be text under 12,000 characters.")
        for key, low, high in (("temperature", 0, 2), ("max_tokens", 0, 65536), ("context_length", 512, 131072)):
            if key not in value:
                continue
            raw = value[key]
            if isinstance(raw, bool):
                raise SettingsError("%s must be a number." % key.replace("_", " ").capitalize())
            try:
                number = float(raw) if key == "temperature" else int(raw)
            except (TypeError, ValueError):
                raise SettingsError("%s must be a number." % key.replace("_", " ").capitalize())
            if not math.isfinite(number) or not low <= number <= high:
                raise SettingsError("%s must be between %s and %s." % (key.replace("_", " ").capitalize(), low, high))
            if key == "max_tokens" and number != 0 and number < 16:
                raise SettingsError("Reply length must be 0 (no limit) or between 16 and 65,536 tokens.")
            if key != "temperature" and str(number) != str(raw).strip():
                raise SettingsError("%s must be a whole number." % key.replace("_", " ").capitalize())
            candidate[key] = number
        for key, allowed in (("compress", ("auto", "off")), ("files", ("ask", "always", "never"))):
            if key in value:
                if value[key] not in allowed:
                    raise SettingsError("Choose a supported %s option." % key)
                candidate[key] = value[key]
        for key in ("web", "skills_auto", "autofix"):
            if key in value:
                if not isinstance(value[key], bool):
                    raise SettingsError("%s must be on or off." % key.replace("_", " ").capitalize())
                candidate[key] = value[key]
        with self.lock:
            prefs = self.prefs()
            prefs["ai_chat_defaults"] = candidate
            self._save_prefs(prefs)
        return {"chat_defaults": candidate}

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
            "background": self._adopt_legacy() or DEFAULT_BG,
            "background_library": [dict(e, video=e.get("kind") in VIDEO_KINDS) for e in self.prefs().get("bg_library") or []],
            "background_presets": list(PRESETS),
            "chat_defaults": self.chat_defaults(p),
            "public": self.public_state(),
            "https": {"url": tsserve.https_url(getattr(self.args, "port", 9092) or 9092, max_age=10), "tailscale": bool(shutil.which("tailscale"))},
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
                p["bg"] = {k: v for k, v in DEFAULT_BG.items() if k != "default"}
            if p["bg"].get("kind") == "none":
                raise SettingsError("Pick a background first.")
            p["bg"].update({"blur": blur, "dim": dim})
            self._save_prefs(p)
            return p["bg"]

    def remove_background(self):
        """No background (your uploaded ones stay in the library)."""
        self._adopt_legacy()
        with self.lock:
            p = self.prefs()
            p["bg"] = {"kind": "none", "v": int(time.time())}
            self._save_prefs(p)
        return {"background": p["bg"]}

    def _drop_files(self):
        for k in KINDS:
            try:
                os.remove(self._bg_path(k))
            except OSError:
                pass

    # Uploaded pictures and videos stay in a library (state dir/backgrounds/ID.KIND) to pick again later.
    def _lib_path(self, eid, kind):
        return os.path.join(self.dir, "backgrounds", "%s.%s" % (eid, kind))

    def _adopt_legacy(self):
        """The current background from before the library (background.KIND) moves into it. Returns the current one."""
        with self.lock:
            p = self.prefs()
            bg = p.get("bg")
            if not bg or bg.get("kind") not in KINDS or bg.get("id"):
                return bg
            old = self._bg_path(bg["kind"])
            if not os.path.exists(old):
                return bg
            eid = secrets.token_hex(6)
            os.makedirs(os.path.dirname(self._lib_path(eid, bg["kind"])), mode=0o700, exist_ok=True)
            os.replace(old, self._lib_path(eid, bg["kind"]))
            lib = p.get("bg_library") or []
            lib.insert(0, {"id": eid, "kind": bg["kind"], "size": os.path.getsize(self._lib_path(eid, bg["kind"])), "added": int(time.time())})
            p["bg_library"] = lib
            bg["id"] = eid
            p["bg"] = bg
            self._save_prefs(p)
            return bg

    def pick_background(self, body):
        eid = str(body.get("id", ""))
        self._adopt_legacy()
        with self.lock:
            p = self.prefs()
            entry = next((e for e in p.get("bg_library") or [] if e.get("id") == eid), None)
            if entry is None or not os.path.exists(self._lib_path(eid, entry["kind"])):
                raise SettingsError("That picture or video isn't in the library any more.", 404)
            old = p.get("bg") or {}
            custom = old.get("kind") in KINDS
            p["bg"] = {"kind": entry["kind"], "id": eid, "v": int(time.time()), "blur": old.get("blur", 0) if custom else 0,
                       "dim": old.get("dim", 45) if custom else 45, "video": entry["kind"] in VIDEO_KINDS}
            self._save_prefs(p)
            return p["bg"]

    def delete_background(self, body):
        eid = str(body.get("id", ""))
        if not LIB_ID_RE.match(eid):
            raise SettingsError("Unknown picture or video.")
        self._adopt_legacy()
        with self.lock:
            p = self.prefs()
            lib = p.get("bg_library") or []
            entry = next((e for e in lib if e.get("id") == eid), None)
            if entry is None:
                raise SettingsError("That picture or video isn't in the library.", 404)
            try:
                os.remove(self._lib_path(eid, entry["kind"]))
            except OSError:
                pass
            p["bg_library"] = [e for e in lib if e.get("id") != eid]
            if (p.get("bg") or {}).get("id") == eid:   # it was the background: back to the default
                p["bg"] = {k: v for k, v in DEFAULT_BG.items() if k != "default"}
            self._save_prefs(p)
            return {"background": p.get("bg") or DEFAULT_BG}

    def set_preset(self, body):
        name = str(body.get("name", ""))
        if name not in PRESETS:
            raise SettingsError("There's no built-in background called that.")
        self._adopt_legacy()   # (your own pictures stay in the library)
        with self.lock:
            p = self.prefs()
            p["bg"] = {"kind": "preset", "preset": name, "v": int(time.time()), "blur": 0, "dim": 0}
            self._save_prefs(p)
            return p["bg"]

    def upload_background(self, body):
        """One piece of a picture or video, in order; the last one ("done") checks the file and makes it the background."""
        uid = str(body.get("id", ""))
        if not UPLOAD_RE.match(uid):
            raise SettingsError("Bad upload.")
        try:
            data = base64.b64decode(str(body.get("data", "")), validate=True)
            offset = int(body.get("offset", 0))
        except (ValueError, TypeError):
            raise SettingsError("Bad upload data.")
        if len(data) > CHUNK_MAX:
            raise SettingsError("Upload pieces are 3 MB at most.", 413)
        tmp = os.path.join(self.dir, "background.upload-" + uid)
        with self.lock:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            if offset == 0:   # forget uploads nobody finished
                for n in os.listdir(self.dir):
                    f = os.path.join(self.dir, n)
                    if n.startswith("background.upload-") and time.time() - os.path.getmtime(f) > 3600:
                        os.remove(f)
            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if offset != size:
                raise SettingsError("The upload got out of step. Try again.", 409)
            if size + len(data) > VIDEO_MAX:
                os.remove(tmp)
                raise SettingsError("That file is too big (80 MB at most).", 413)
            with open(tmp, "ab") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            size += len(data)
            if not body.get("done"):
                return {"received": size}
            with open(tmp, "rb") as f:
                kind = media_kind(f.read(64))
            if kind is None or (kind not in VIDEO_KINDS and size > IMAGE_MAX):
                os.remove(tmp)
                raise SettingsError("Use a PNG, JPEG or WebP picture (8 MB at most) or an MP4 or WebM video (80 MB at most)." if kind is None
                                    else "The picture is too big (8 MB at most).", 400 if kind is None else 413)
            lib = self.prefs().get("bg_library") or []
            if len(lib) >= LIB_MAX_ITEMS or sum(int(e.get("size") or 0) for e in lib) + size > LIB_MAX_BYTES:
                os.remove(tmp)
                raise SettingsError("Your pictures and videos are full (%d, or 1 GB): delete some first." % LIB_MAX_ITEMS, 413)
        self._adopt_legacy()
        with self.lock:
            eid = secrets.token_hex(6)
            os.makedirs(os.path.dirname(self._lib_path(eid, kind)), mode=0o700, exist_ok=True)
            os.replace(tmp, self._lib_path(eid, kind))
            os.chmod(self._lib_path(eid, kind), 0o600)
            p = self.prefs()
            p["bg_library"] = [{"id": eid, "kind": kind, "size": size, "added": int(time.time()), "name": str(body.get("name", ""))[:80]}] + (p.get("bg_library") or [])
            old = p.get("bg") or {}
            custom = old.get("kind") in KINDS
            p["bg"] = {"kind": kind, "id": eid, "v": int(time.time()), "blur": old.get("blur", 0) if custom else 0,
                       "dim": old.get("dim", 45) if custom else 45, "video": kind in VIDEO_KINDS}
            self._save_prefs(p)
            return p["bg"]

    def background_file(self):
        """(bytes, content type) of the uploaded background, or (None, None)."""
        path, ctype, _ = self.background_media()
        if path is None:
            return None, None
        with open(path, "rb") as f:
            return f.read(), ctype

    def background_media(self, eid=""):
        """(path, content type, size) of the current uploaded background, or of library item eid; else (None, None, 0)."""
        p = self.prefs()
        if eid:
            entry = next((e for e in p.get("bg_library") or [] if e.get("id") == eid and LIB_ID_RE.match(eid)), None)
            if entry is None:
                return None, None, 0
            kind, path = entry["kind"], self._lib_path(eid, entry["kind"])
        else:
            bg = p.get("bg") or {}
            kind = bg.get("kind")
            if kind not in KINDS:
                return None, None, 0
            path = self._lib_path(bg["id"], kind) if bg.get("id") else self._bg_path(kind)
        try:
            return path, KINDS[kind], os.path.getsize(path)
        except OSError:
            return None, None, 0


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
        path, ctype, size = s.background_media(str((q.get("id") or [""])[0])[:12])
        if path is None:
            return h._send(404, "No background", "text/plain; charset=utf-8")
        cache = {"Cache-Control": "private, max-age=86400", "Accept-Ranges": "bytes"}
        m = re.match(r"^bytes=(\d*)-(\d*)$", h.headers.get("Range", "").strip())
        if m and size:   # videos are read in ranges (Safari won't play one otherwise)
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            else:
                start, end = max(0, size - int(m.group(2) or 0)), size - 1
            end = min(end, size - 1, start + 4 * 1024 * 1024 - 1)
            if start >= size or start > end:
                return h._send(416, "", "text/plain; charset=utf-8", {"Content-Range": "bytes */%d" % size})
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            return h._send(206, data, ctype, dict(cache, **{"Content-Range": "bytes %d-%d/%d" % (start, end, size)}))
        h.send_response(200)
        for k, v in dict(cache, **{"Content-Type": ctype, "Content-Length": str(size), "X-Content-Type-Options": "nosniff"}).items():
            h.send_header(k, v)
        h.end_headers()
        if h.command == "HEAD":
            return None
        with open(path, "rb") as f:
            while True:
                piece = f.read(1024 * 1024)
                if not piece:
                    break
                h.wfile.write(piece)

    def update_all(h, body):
        """Update nodeyard (dashboard + its deps), sc-pick, and the audio/download tools, then restart the dashboard.
        Runs outside the dashboard's sandbox via a detached systemd unit, like the other system-changing actions."""
        if getattr(args, "demo", False):
            return h._json({"ok": True, "message": "(demo) nothing was updated."})
        script = (
            "echo '== nodeyard =='; curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/install.sh | bash -s -- --yes --force; "
            "echo '== sc-pick =='; curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/sc-pick/main/sc-pick -o /usr/local/bin/sc-pick && chmod +x /usr/local/bin/sc-pick; "
            "echo '== audio/download deps =='; "
            "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y ffmpeg pulseaudio-utils; "
            "elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm ffmpeg libpulse; "
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y ffmpeg pulseaudio-utils; fi; "
            "python3 -m pip install --upgrade --quiet yt-dlp 2>/dev/null || true; "
            "echo '== restart =='; nodeyard dashboard start --yes; echo '== done =='"
        )
        try:
            subprocess.Popen(["systemd-run", "--unit", "nodeyard-update", "--collect", "--setenv=HOME=/root", "/bin/sh", "-c", script],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return h._json({"ok": False, "error": "Couldn't start the update: %s" % e}, 500)
        return h._json({"ok": True, "message": "Update started — nodeyard, sc-pick and the audio/download tools. The dashboard will restart on the new version. Watch it in Terminal: journalctl -u nodeyard-update -f"})

    ctx.get_routes.update({
        "/api/settings": wrap(lambda h, q: s.view(h._token())),
        "/api/doctor": wrap(lambda h, q: {"doctor": s.doctor(bool(q.get("fresh")))}),
        "/api/background": background,
        "/api/commands": wrap(lambda h, q: {"commands": s.commands(), "public": h._forwarded() is not None}),
    })
    ctx.post_routes.update({
        "/api/update/run": update_all,
        "/api/settings/password": wrap(lambda h, b: s.set_password(b, h._token())),
        "/api/settings/chat-defaults": wrap(lambda h, b: s.set_chat_defaults(b)),
        "/api/settings/signout-all": wrap(lambda h, b: s.signout_all()),
        "/api/settings/weak-password": wrap(lambda h, b: s.set_weak(b)),
        "/api/settings/model-key": wrap(lambda h, b: s.set_model_key(b)),
        "/api/settings/adopt-model-key": wrap(lambda h, b: s.adopt_model_key()),
        "/api/settings/hf-token": wrap(lambda h, b: s.set_hf_token(b)),
        "/api/settings/service": wrap(lambda h, b: s.apply_service(b)),
        "/api/settings/background": wrap(lambda h, b: {"background": s.set_background(b)}),
        "/api/settings/background-style": wrap(lambda h, b: {"background": s.background_style(b)}),
        "/api/settings/background-remove": wrap(lambda h, b: s.remove_background()),
        "/api/settings/background-preset": wrap(lambda h, b: {"background": s.set_preset(b)}),
        "/api/settings/background-pick": wrap(lambda h, b: {"background": s.pick_background(b)}),
        "/api/settings/background-delete": wrap(lambda h, b: s.delete_background(b)),
        "/api/settings/background-upload": wrap(lambda h, b: (lambda r: {"background": r} if "kind" in r else r)(s.upload_background(b))),
        "/api/settings/public-refresh": wrap(lambda h, b: (s.forget_public(), tsserve.forget(), {"public": s.public_state()})[1]),
    })
    ctx.post_limits["/api/settings/background"] = BODY_MAX
    ctx.post_limits["/api/settings/background-upload"] = CHUNK_MAX * 4 // 3 + 64 * 1024
    try:
        import aiapi
        aiapi.COMMAND_PATHS = s.command_paths
    except ImportError:
        pass
