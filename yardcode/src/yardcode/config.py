"""Settings: defaults < user file < project file < project-local file < environment < command line.

  ~/.config/yardcode/settings.json        yours, for every project (the API key lives in credentials.json beside it)
  .yardcode/settings.json                 shared with the project (commit it)
  .yardcode/settings.local.json           yours, only here (keep it out of git)
"""
import copy
import json
import os

from . import util

DEFAULTS = {
    "api_base": "",              # e.g. http://100.64.0.10:31435/v1
    "model": "",                 # empty = whatever the server has loaded
    "control_url": "",           # the nodeyard dashboard, for loading models remotely (default: same host, port 9092)
    "context_window": 0,         # 0 = ask the server
    "max_tokens": 0,             # 0 = no limit (the model stops by itself or when the context is full)
    "temperature": 0.2,
    "permission_mode": "default",  # default | acceptEdits | plan | bypassPermissions
    "permissions": {"allow": [], "deny": [], "ask": []},
    "additional_dirs": [],
    "tool_mode": "auto",         # auto | native | text (text: for servers without tool calling)
    "tool_output_limit": 16000,  # characters of one tool result sent to the model
    "tools": {"disabled": [], "profile": "auto"},   # profile: auto (lean when the context is small) | full | lean
    "thinking": "show",          # live (as it is written) | show (a short summary afterwards) | hide
    "theme": "auto",             # auto | dark | light | none
    "vim": False,
    "compact": {"auto": True, "threshold": 0.8, "keep_turns": 3, "prune_after": 6, "prune_chars": 600},
    "search": {"engine": "auto", "searxng_url": "", "brave_key": "", "tavily_key": ""},
    "web": {"allow_private": False, "timeout": 20, "via": "auto"},   # via: auto (the server when it can) | server | local (this computer)
    "hooks": {},
    "mcpServers": {},
    "env": {},
    "system_prompt_extra": "",
    "check_updates": False,
    "sync_chats": True,          # share finished chats with the nodeyard dashboard (needs the server API key)
    "trusted_projects": [],
}

# A repository you cloned can contain .yardcode/ files. Until you trust the project, only these are honoured from it;
# hooks, MCP servers, environment variables and "always allow" rules could otherwise run commands before you've seen them.
UNTRUSTED_DROP = ("hooks", "mcpServers", "env", "permission_mode", "system_prompt_extra", "additional_dirs", "trusted_projects", "tool_mode", "web", "search")

ENV_MAP = {
    "YARDCODE_API_BASE": "api_base", "OPENAI_BASE_URL": "api_base", "OPENAI_API_BASE": "api_base",
    "YARDCODE_MODEL": "model", "YARDCODE_CONTROL_URL": "control_url",
    "YARDCODE_CONTEXT": "context_window", "YARDCODE_MAX_TOKENS": "max_tokens",
    "YARDCODE_PERMISSION_MODE": "permission_mode", "YARDCODE_THEME": "theme",
}
KEY_ENV = ("YARDCODE_API_KEY", "OPENAI_API_KEY")


def merge(base, extra):
    """Deep-merge EXTRA into a copy of BASE (lists are replaced, except permissions, which are joined)."""
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k == "permissions" and isinstance(v, dict) and isinstance(out.get(k), dict):
            for kind in ("allow", "deny", "ask"):
                out[k][kind] = list(dict.fromkeys(list(out[k].get(kind, [])) + list(v.get(kind, []))))
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _coerce(key, value):
    ref = DEFAULTS.get(key)
    if isinstance(ref, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(ref, int) and not isinstance(ref, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return ref
    if isinstance(ref, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return ref
    return value


class Settings:
    def __init__(self, cwd=None, overrides=None, environ=None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.environ = os.environ if environ is None else environ
        self.overrides = dict(overrides or {})
        self.user_path = os.path.join(util.config_dir(), "settings.json")
        self.cred_path = os.path.join(util.config_dir(), "credentials.json")
        self.project_dir = self.find_project_dir()
        self.trusted = True
        self.pending_trust = False
        self.reload()

    def find_project_dir(self):
        """The nearest .yardcode folder at or above the working directory (else the working directory's own)."""
        d = self.cwd
        while True:
            if os.path.isdir(os.path.join(d, ".yardcode")):
                return os.path.join(d, ".yardcode")
            parent = os.path.dirname(d)
            if parent == d:
                return os.path.join(self.cwd, ".yardcode")
            d = parent

    @property
    def project_path(self):
        return os.path.join(self.project_dir, "settings.json")

    @property
    def local_path(self):
        return os.path.join(self.project_dir, "settings.local.json")

    def reload(self):
        data = copy.deepcopy(DEFAULTS)
        self.layers = {}
        user_layer = util.read_json(self.user_path)
        root = os.path.dirname(self.project_dir)
        self.trusted = bool(self.environ.get("YARDCODE_TRUST") == "1" or root in (user_layer.get("trusted_projects") or [])
                            or os.path.abspath(os.path.expanduser("~")) == root)
        self.pending_trust = False
        for name, path in (("user", self.user_path), ("project", self.project_path), ("local", self.local_path)):
            layer = user_layer if name == "user" else util.read_json(path)
            if name != "user":  # a shared file must never carry (or redirect) your credentials
                layer = {k: v for k, v in layer.items() if k not in ("api_key", "api_base", "control_url", "trusted_projects")}
                if not self.trusted:
                    risky = [k for k in UNTRUSTED_DROP if k in layer] + (["permissions.allow"] if (layer.get("permissions") or {}).get("allow") else [])
                    if risky:
                        self.pending_trust = True
                    layer = {k: v for k, v in layer.items() if k not in UNTRUSTED_DROP}
                    if "permissions" in layer:
                        layer["permissions"] = {k: v for k, v in layer["permissions"].items() if k != "allow"}
            self.layers[name] = layer
            data = merge(data, layer)
        for var, key in ENV_MAP.items():
            if self.environ.get(var):
                data[key] = _coerce(key, self.environ[var])
        cred = util.read_json(self.cred_path)
        data["api_key"] = cred.get("api_key", "")
        for var in KEY_ENV:
            if self.environ.get(var):
                data["api_key"] = self.environ[var]
                break
        for k, v in self.overrides.items():
            if v is not None:
                data[k] = v
        self.data = data

    def get(self, dotted, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted, value, scope="user"):
        """Change one setting in a settings file (scope: user | project | local)."""
        path = {"user": self.user_path, "project": self.project_path, "local": self.local_path}[scope]
        data = util.read_json(path)
        cur = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        util.write_json(path, data, 0o600 if scope == "user" else 0o644)
        self.reload()

    def trust_project(self):
        """Honour this project's hooks, MCP servers and allow rules from now on."""
        root = os.path.dirname(self.project_dir)
        data = util.read_json(self.user_path)
        lst = data.setdefault("trusted_projects", [])
        if root not in lst:
            lst.append(root)
        util.ensure_dir(os.path.dirname(self.user_path), 0o700)
        util.write_json(self.user_path, data, 0o600)
        self.reload()

    def save_key(self, api_key):
        data = util.read_json(self.cred_path)
        if api_key:
            data["api_key"] = api_key
        else:
            data.pop("api_key", None)
        util.ensure_dir(os.path.dirname(self.cred_path), 0o700)
        util.write_json(self.cred_path, data, 0o600)
        self.reload()

    def add_permission(self, kind, rule, scope="local"):
        path = {"user": self.user_path, "project": self.project_path, "local": self.local_path}[scope]
        data = util.read_json(path)
        perms = data.setdefault("permissions", {})
        rules = perms.setdefault(kind, [])
        if rule not in rules:
            rules.append(rule)
        util.write_json(path, data, 0o600 if scope == "user" else 0o644)
        self.reload()

    def remove_permission(self, kind, rule):
        for scope, path in (("user", self.user_path), ("project", self.project_path), ("local", self.local_path)):
            data = util.read_json(path)
            rules = (data.get("permissions") or {}).get(kind, [])
            if rule in rules:
                rules.remove(rule)
                util.write_json(path, data, 0o600 if scope == "user" else 0o644)
        self.reload()

    # ---- derived values -------------------------------------------------------------------
    @property
    def api_base(self):
        base = (self.data.get("api_base") or "").strip().rstrip("/")
        return base

    def control_url(self):
        """Where the nodeyard dashboard is: set explicitly, else the API's host on port 9092."""
        explicit = (self.data.get("control_url") or "").strip().rstrip("/")
        if explicit:
            return explicit
        base = self.api_base
        if not base:
            return ""
        from urllib.parse import urlparse
        u = urlparse(base)
        if not u.hostname:
            return ""
        host = "[%s]" % u.hostname if ":" in u.hostname else u.hostname
        return "%s://%s:9092" % ("http" if u.scheme != "https" else "https", host)

    def dump(self, hide_key=True):
        d = copy.deepcopy(self.data)
        if hide_key and d.get("api_key"):
            k = d["api_key"]
            d["api_key"] = (k[:4] + "…" + k[-2:]) if len(k) > 8 else "set"
        return d

    def to_json(self):
        return json.dumps(self.dump(), indent=2, sort_keys=True)
