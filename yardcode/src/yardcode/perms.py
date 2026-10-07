"""Who decides whether a tool call may run: modes, allow/deny/ask rules, read-only commands, risky commands.

Modes:  default (asks before changing anything or running commands), acceptEdits (edits inside the project need no asking),
        plan (read-only; the model plans, then asks to leave plan mode), bypassPermissions (no questions, except deny rules).
Rules (in settings): "Bash(git status:*)", "Edit(src/**)", "Read(~/notes/**)", "WebFetch(domain:docs.python.org)", "mcp__server__tool", "Python".
"""
import fnmatch
import os
import re
import shlex
from urllib.parse import urlsplit

from .tools.files import glob_to_regex

MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
MODE_LABEL = {"default": "ask before changes", "acceptEdits": "accept edits", "plan": "plan mode (read-only)", "bypassPermissions": "no questions asked"}
RULE_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*(?:\((.*)\))?\s*$", re.S)
PATH_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "FileSearch")
PLAN_OK = {"ExitPlanMode", "AskUserQuestion", "TodoWrite"}

# commands that only read, whatever their arguments (as long as nothing is redirected into a file)
READONLY_ANY = set("""ls cat head tail wc pwd echo printf date whoami id uname hostname which type file stat du df free uptime ps tree grep egrep fgrep rg ag sort uniq
cut tr diff cmp md5sum sha1sum sha256sum sha512sum basename dirname realpath readlink jq column nl tac rev fold expand od hexdump xxd strings lscpu lsblk nproc arch
locale true false test [ seq sleep sensors nvidia-smi vcgencmd cd pushd popd dir vdir getconf lsusb lspci lsmod ping traceroute dig nslookup host whereis
tput clear groups last w who lsof pstree pgrep top vmstat iostat mpstat sar tty""".split())

# commands with read-only subcommands
READONLY_SUB = {
    "git": None,   # handled by _git_readonly
    "kubectl": {"get", "describe", "logs", "top", "version", "cluster-info", "api-resources", "api-versions", "explain", "events"},
    "docker": {"ps", "images", "logs", "inspect", "version", "info", "top", "port", "history", "search", "stats", "events"},
    "podman": {"ps", "images", "logs", "inspect", "version", "info", "top", "port", "history", "search", "stats"},
    "helm": {"list", "ls", "status", "get", "history", "show", "search", "version", "env"},
    "npm": {"ls", "list", "view", "outdated", "-v", "--version", "root", "prefix"},
    "pip": {"list", "show", "freeze", "check", "--version", "-V"}, "pip3": {"list", "show", "freeze", "check", "--version", "-V"},
    "cargo": {"--version", "tree", "metadata"}, "go": {"version", "env", "list"},
    "tailscale": {"status", "ip", "version", "netcheck", "whois", "ping"}, "ufw": {"status"}, "apt": {"list", "show", "search", "policy"},
    "dpkg": {"-l", "-L", "-s", "--list", "--status", "-S"}, "rpm": {"-q", "-qa", "-qi", "-ql"}, "pacman": {"-Q", "-Qi", "-Ql", "-Ss", "-Si"},
    "python": {"-V", "--version"}, "python3": {"-V", "--version"}, "node": {"-v", "--version"}, "java": {"-version", "--version"},
    "loginctl": {"list-sessions", "list-users", "show-session", "show-user", "status"},
}
# read-only commands whose arguments can still change things (the forbidden ones are listed)
READONLY_BUT = {
    "ip": ("add", "del", "delete", "set", "flush", "replace", "change", "up", "down", "append", "exec"),
    "journalctl": ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish", "--setup-keys", "--update-catalog", "--smart-relinquish"),
    "dmesg": ("-c", "-C", "--clear", "--read-clear", "-n", "--console-level", "-D", "-E"),
    "ss": ("-K", "--kill"), "netstat": (), "apt-cache": (),
}
GIT_RO = {"status", "diff", "log", "show", "blame", "rev-parse", "rev-list", "ls-files", "ls-tree", "describe", "shortlog", "grep", "cat-file", "merge-base",
          "name-rev", "for-each-ref", "count-objects", "whatchanged", "reflog", "help", "version", "show-ref", "diff-tree", "diff-index", "ls-remote", "cherry"}
GIT_BRANCH_WRITE = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "--unset-upstream", "--edit-description", "-u", "-f", "--force"}
NODEYARD_TOP_RO = {"status", "nodes", "info", "doctor", "version", "commands", "changes", "nettest", "worker-info"}
ACTION_FLAGS = {"--fix", "--yes", "-y", "--apply", "--force"}

RISKY = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|\*|\.\s|\.$)", "deletes files recursively"),
    (r"\brm\s+-[a-zA-Z]*r", "deletes folders recursively"),
    (r"\bmkfs|\bwipefs|\bfdisk\b|\bparted\b", "changes disk partitions or formats a disk"),
    (r"\bdd\b[^|;]*\bof=/dev/", "writes straight to a disk"),
    (r">\s*/dev/(sd|nvme|mmcblk|disk)", "writes straight to a disk"),
    (r":\(\)\s*\{", "looks like a fork bomb"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\b|\bchown\s+-R\b", "changes permissions recursively"),
    (r"(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", "runs a script straight from the internet"),
    (r"\bsudo\b|\bdoas\b", "runs with administrator rights"),
    (r"\bgit\s+push\b[^;&|]*(--force|-f\b)", "force-pushes"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|restore\s)", "throws away uncommitted work"),
    (r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b", "shuts the machine down"),
    (r"\bkubectl\s+(delete|drain|cordon|taint|replace)\b|\bk3s\s+kubectl\s+(delete|drain)", "changes or deletes cluster resources"),
    (r"\b(drop|truncate)\s+(table|database)\b", "deletes database content"),
    (r"\b(systemctl|service)\s+(stop|disable|mask|restart)\b", "stops or restarts a service"),
    (r"\bnodeyard\s+[^;&|]*(remove|undeploy|purge|reset|uninstall|clean|\brm\b|\bstop\b)", "removes things from the cluster"),
    (r">\s*~?/?\.?(bashrc|zshrc|profile)|>\s*~?/?\.ssh/|>\s*/etc/", "overwrites a system or shell configuration file"),
]
SENSITIVE_PATHS = [r"(^|/)\.git(/|$)", r"(^|/)\.ssh(/|$)", r"(^|/)\.env(\.|$)", r"(^|/)\.(bash|zsh)rc$", r"(^|/)\.(bash_|zsh|)profile$", r"^/etc/", r"^/boot/",
                   r"(^|/)\.yardcode/settings", r"(^|/)\.config/yardcode/", r"(^|/)\.gnupg(/|$)", r"(^|/)\.aws(/|$)", r"(^|/)id_(rsa|ed25519|ecdsa)"]
SENSITIVE_READ = [r"\.ssh/", r"(^|[\s/])\.env(\.|\b)", r"\.aws/", r"\.gnupg", r"id_(rsa|ed25519|ecdsa)", r"/etc/(shadow|sudoers)", r"\.netrc", r"\.npmrc",
                  r"\.pypirc", r"\.kube/config", r"k3s\.yaml", r"/etc/nodeyard/secrets", r"credentials\.json", r"(^|/)secrets?/"]


def parse_rule(text):
    m = RULE_RE.match(text or "")
    if not m:
        return None, None
    return m.group(1), (m.group(2) if m.group(2) is not None else None)


# ---- splitting shell commands ------------------------------------------------------------------------------------

def split_command(cmd):
    """The simple commands in a shell line, split on ; && || | & and newlines, respecting quotes. -> (list, uses_substitution)"""
    parts, cur, quote, i, subst = [], [], "", 0, False
    s = cmd or ""
    while i < len(s):
        ch = s[i]
        nxt = s[i + 1:i + 2]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and nxt:
                cur.append(nxt)
                i += 1
            elif ch == quote:
                quote = ""
            elif quote == '"' and (ch == "`" or (ch == "$" and nxt == "(")):
                subst = True
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "\\" and nxt:
            cur.append(ch + nxt)
            i += 1
        elif ch == "`" or (ch in "$<>" and nxt == "("):
            subst = True
            cur.append(ch)
        elif ch in ";\n" or ch == "|" or (ch == "&" and nxt != ">" and (i == 0 or s[i - 1] not in ">&")):
            if ch in "&|" and nxt == ch:
                i += 1
            piece = "".join(cur).strip()
            if piece:
                parts.append(piece)
            cur = []
        else:
            cur.append(ch)
        i += 1
    piece = "".join(cur).strip()
    if piece:
        parts.append(piece)
    return parts, subst


def _tokens(cmd):
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        return None
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks = toks[1:]
    return toks


def has_file_redirect(cmd):
    """True when the command writes into a file with > or >> (not just /dev/null or another file descriptor)."""
    s = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", cmd)
    s = re.sub(r"\d*>&\d+|&>\s*/dev/null|\d*>>?\s*/dev/null|<<-?\s*\S+|<\s*\S+", " ", s)
    return ">" in s


def is_readonly_command(cmd):
    toks = _tokens(cmd)
    if not toks or has_file_redirect(cmd):
        return False
    if any(re.search(p, cmd) for p in SENSITIVE_READ):
        return False
    base, args = os.path.basename(toks[0]), toks[1:]
    if base == "find":
        return not any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls", "-fprintf", "-fprint0") for a in args)
    if base == "sed":
        joined = " ".join(args)
        return not any(a.startswith("-i") or a == "--in-place" for a in args) and not re.search(r"(^|[;{\s])[wWe]\s", joined)
    if base == "awk":
        return not re.search(r"system\s*\(|\|\s*getline|print[^;]*>", " ".join(args))
    if base == "sort":
        return not any(a == "-o" or a.startswith("-o") or a.startswith("--output") for a in args)
    if base == "git":
        return _git_readonly(args)
    if base == "k3s":
        return bool(args) and args[0] == "kubectl" and _sub_readonly("kubectl", args[1:])
    if base == "systemctl":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub.startswith(("status", "is-", "list-", "show", "cat"))
    if base == "nodeyard":
        return _nodeyard_readonly(args)
    if base in READONLY_BUT:
        return not any(a == bad or (bad.startswith("-") and a.startswith(bad)) for a in args for bad in READONLY_BUT[base])
    if base in READONLY_SUB:
        return _sub_readonly(base, args)
    return base in READONLY_ANY


def _sub_readonly(base, args):
    allowed = READONLY_SUB.get(base) or set()
    if base in ("python", "python3", "node", "java", "cargo", "go", "pip", "pip3", "npm"):
        sub = args[0] if args else ""
    else:
        sub = next((a for a in args if not a.startswith("-")), "")
    return sub in allowed


def _git_readonly(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-c":         # -c can set options that run programs (core.fsmonitor, core.pager...)
            return False
        i += 2 if args[i] == "-C" else 1
    if i >= len(args):
        return True
    sub, rest = args[i], args[i + 1:]
    if sub in GIT_RO:
        return not any(a.startswith("--output") for a in rest)
    positional = [a for a in rest if not a.startswith("-")]
    if sub == "branch":
        listing = any(a in ("-l", "--list") for a in rest)
        return not any(a in GIT_BRANCH_WRITE for a in rest) and (listing or not positional)
    if sub == "tag":
        return any(a in ("-l", "--list", "-n") for a in rest) or not rest
    if sub == "remote":
        return not rest or rest[0] in ("-v", "--verbose", "show", "get-url")
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "config":
        return bool(rest) and rest[0] in ("--get", "--list", "-l", "--get-all", "--get-regexp")
    if sub == "worktree":
        return bool(rest) and rest[0] == "list"
    return False


def _nodeyard_readonly(args):
    if any(a in ACTION_FLAGS for a in args):
        return False
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return True
    top = words[0]
    sub = words[1] if len(words) > 1 else ""
    sub2 = words[2] if len(words) > 2 else ""
    if top in NODEYARD_TOP_RO:
        return True
    if top == "hw":
        return sub == "show"
    if top == "ai":
        if sub in ("status", "nodes"):
            return True
        if sub in ("split", "gate", "gpu"):
            return sub2 in ("status", "plan", "models")
        return False
    if top in ("dashboard", "public"):
        return sub == "status"
    if top == "config":
        return sub in ("get", "list")
    return False


def risk_note(cmd):
    for pat, why in RISKY:
        if re.search(pat, cmd):
            return why
    return ""


def command_rule_prefix(cmd):
    """The "git commit" in "git commit -m x": what an "always allow" rule for this command should cover."""
    toks = _tokens(cmd) or []
    if not toks:
        return ""
    base = os.path.basename(toks[0])
    two = {"git", "npm", "yarn", "pnpm", "docker", "podman", "kubectl", "systemctl", "apt", "apt-get", "pip", "pip3", "cargo", "go", "make", "nodeyard", "helm",
           "brew", "journalctl", "k3s", "gh", "python", "python3", "node", "npx", "tailscale", "ufw", "snap", "flatpak"}
    if base in two and len(toks) > 1 and not toks[1].startswith("-"):
        if base in ("nodeyard", "k3s") and len(toks) > 2 and not toks[2].startswith("-"):
            return "%s %s %s" % (base, toks[1], toks[2])
        return "%s %s" % (base, toks[1])
    return base


# ---- the decision ----------------------------------------------------------------------------------------------

class Decision:
    def __init__(self, action, reason="", suggest="", scope_hint="", risk=""):
        self.action = action          # allow | ask | deny
        self.reason = reason
        self.suggest = suggest        # a rule to offer for "always allow"
        self.scope_hint = scope_hint  # what "always" would cover, in words
        self.risk = risk


class Permissions:
    def __init__(self, settings, ctx, interactive=True):
        self.settings = settings
        self.ctx = ctx
        self.mode = settings.get("permission_mode", "default")
        if self.mode not in MODES:
            self.mode = "default"
        self.session_allow = []
        self.session_deny = []
        self.interactive = interactive
        self.known_urls = set()      # addresses the user typed or a search returned: fetching them needs no question
        self.extra_allow = []        # from --allowedTools
        self.extra_deny = []         # from --disallowedTools

    def _rules(self, kind):
        base = list((self.settings.get("permissions") or {}).get(kind, []))
        if kind == "allow":
            base += self.session_allow + self.extra_allow
        if kind == "deny":
            base += self.session_deny + self.extra_deny
        return base

    def allow_session(self, rule):
        if rule and rule not in self.session_allow:
            self.session_allow.append(rule)

    # -- matching --
    def _rules_for(self, kind, name):
        out = []
        for raw in self._rules(kind):
            n, spec = parse_rule(raw)
            if not n:
                continue
            if n == name or (name.startswith("mcp__") and name.startswith(n + "__")):
                out.append((spec, raw))
        return out

    def _match(self, kind, tool, args):
        """The first rule of this kind that covers the call, or None. A Bash line is covered by an allow rule only when
        every command in it is."""
        mine = self._rules_for(kind, tool.name)
        if not mine:
            return None
        spec = tool.specifier(args, self.ctx)
        if tool.name == "Bash":
            parts, subst = split_command(spec or "")
            if kind == "allow" and (subst or not parts):
                return None
            first = None
            for p in parts:
                hit = next((raw for s, raw in mine if self._bash_match(s, p)), None)
                if hit is None and kind == "allow":
                    return None
                first = first or hit
            return first
        for s, raw in mine:
            if self._spec_match(tool.name, s, spec):
                return raw
        return None

    @staticmethod
    def _bash_match(spec, cmd):
        if spec is None:
            return True
        cmd, spec = " ".join(cmd.split()), " ".join(spec.split())
        if spec.endswith(":*"):
            pre = spec[:-2]
            return cmd == pre or cmd.startswith(pre + " ")
        if "*" in spec:
            return fnmatch.fnmatchcase(cmd, spec)
        return cmd == spec

    def _spec_match(self, name, spec, value):
        if spec is None:
            return True
        value = value or ""
        if name == "WebFetch":
            if spec.startswith("domain:"):
                dom = spec[7:].lower().lstrip(".")
                host = value[7:].lower() if value.startswith("domain:") else ""
                return host == dom or host.endswith("." + dom)
            return fnmatch.fnmatchcase(value, spec)
        if name in PATH_TOOLS and os.path.isabs(value):
            sp = spec[1:] if spec.startswith("//") else os.path.expanduser(spec)
            if not os.path.isabs(sp):
                sp = os.path.join(self.ctx.cwd, sp[2:] if sp.startswith("./") else sp)
            sp = os.path.normpath(sp)
            if value == sp or value.startswith(sp + os.sep):   # a rule naming a folder covers everything below it
                return True
            return bool(glob_to_regex(sp).match(value)) or bool(glob_to_regex(sp + "/**").match(value))
        return fnmatch.fnmatchcase(value, spec)

    # -- the decision --
    def decide(self, tool, args):
        name = tool.name
        spec = tool.specifier(args, self.ctx)
        deny = self._match("deny", tool, args)
        if deny:
            return Decision("deny", "blocked by your rule %s" % deny)
        if self.mode == "plan" and not tool.read_only and name not in PLAN_OK:
            return Decision("deny", "plan mode is read-only. Finish the plan and present it with ExitPlanMode; then changes can be made.")
        risk = ""
        if name == "Bash":
            parts, _ = split_command(spec or "")
            risk = next((risk_note(p) for p in parts + [spec or ""] if risk_note(p)), "")
        if self.mode == "bypassPermissions":
            return Decision("allow", "no questions asked")
        suggest = self.suggest_rule(tool, args)
        ask = self._match("ask", tool, args)
        if ask:
            return Decision("ask", "your rule %s asks first" % ask, suggest, risk=risk)
        allow = self._match("allow", tool, args)
        if allow and not (risk and name == "Bash"):
            return Decision("allow", "allowed by %s" % allow)
        return self._default(tool, args, spec, suggest, risk)

    def suggest_rule(self, tool, args):
        name = tool.name
        spec = tool.specifier(args, self.ctx)
        if name == "Bash":
            parts, subst = split_command(spec or "")
            if len(parts) == 1 and not subst:
                pre = command_rule_prefix(parts[0])
                return "Bash(%s:*)" % pre if pre else ""
            return ""
        if name == "WebFetch":
            return "WebFetch(%s)" % spec
        if name in ("Edit", "Write", "MultiEdit"):
            return "Edit"
        return name

    def _default(self, tool, args, spec, suggest, risk):
        name, kind, ctx = tool.name, tool.kind, self.ctx
        p = str(spec or "")
        if kind == "read":
            if p and os.path.isabs(p):
                if any(re.search(x, p) for x in SENSITIVE_READ):
                    return Decision("ask", "it may hold secrets", "")
                if not ctx.inside(p) and not self._trusted_outside(p):
                    rule = "%s(%s/**)" % (name, os.path.dirname(p).rstrip("/")) if name in ("Read", "LS", "Glob", "Grep") else ""
                    return Decision("ask", "it is outside the project folders", rule, "this folder")
            return Decision("allow", "read-only")
        if kind == "net":
            if name != "WebFetch":
                return Decision("allow", "web lookups are allowed")
            url = str(args.get("url", ""))
            host = (urlsplit(url if "//" in url else "//" + url).hostname or "").lower()
            if any(url.rstrip("/") == k.rstrip("/") for k in self.known_urls):
                return Decision("allow", "this address came from your request or a search")
            return Decision("ask", "it fetches a page the conversation didn't point to", suggest if host else "", "pages on %s" % host)
        if kind == "edit":
            if name == "Memory":
                return Decision("allow" if self.mode == "acceptEdits" else "ask", "it saves to a notes file", suggest)
            sensitive = any(re.search(x, p) for x in SENSITIVE_PATHS)
            if self.mode == "acceptEdits" and ctx.inside(p) and not sensitive:
                return Decision("allow", "edits are accepted")
            why = "it is a sensitive file" if sensitive else ("it is outside the project folders" if not ctx.inside(p) else "it changes a file")
            return Decision("ask", why, "" if sensitive else suggest, "" if sensitive else "file edits")
        if kind == "exec":
            if name == "Bash":
                parts, subst = split_command(spec or "")
                if parts and not subst and all(is_readonly_command(x) for x in parts):
                    return Decision("allow", "read-only command")
                hint = "commands starting with %r" % command_rule_prefix(parts[0]) if len(parts) == 1 and parts else ""
                return Decision("ask", "it runs a command", suggest, hint, risk)
            if name == "KillShell":
                return Decision("allow", "stops a shell this session started")
            return Decision("ask", "it runs code", suggest)
        if kind == "cluster":
            if args.get("action") in ("status", "list", "search", "files"):
                return Decision("allow", "read-only")
            return Decision("ask", "it changes the model running on your cluster", suggest)
        if tool.read_only and kind == "other":
            return Decision("allow", "read-only tool")
        return Decision("ask", "it is a plugin tool", suggest)

    @staticmethod
    def _trusted_outside(path):
        from . import util
        for root in (util.config_dir(), util.data_dir(), "/tmp", "/private/tmp", "/usr/share", "/usr/include", "/usr/local/lib/nodeyard", "/usr/lib/python3",
                     "/proc/cpuinfo", "/proc/meminfo", "/etc/os-release", "/var/log"):
            if path == root or path.startswith(root.rstrip("/") + "/"):
                return True
        return False

    def remember(self, rule, scope):
        """After "always allow": for this session only, or saved to a settings file (project -> your local file)."""
        if not rule:
            return
        self.allow_session(rule)
        if scope in ("project", "user", "local"):
            self.settings.add_permission("allow", rule, "local" if scope == "project" else scope)

    def cycle_mode(self):
        order = ["default", "acceptEdits", "plan"]
        i = order.index(self.mode) if self.mode in order else -1
        self.mode = order[(i + 1) % len(order)]
        return self.mode
