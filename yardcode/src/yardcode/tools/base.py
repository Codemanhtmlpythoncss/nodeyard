"""What every tool is: a name, a description the model reads, a JSON schema for its arguments, and a run()."""
import json
import os

from .. import util


class ToolError(Exception):
    """The tool couldn't do it; the message goes back to the model so it can try something else."""


class Result:
    def __init__(self, text="", error=False, summary="", preview=None, diff=None, meta=None):
        self.text = text            # what the model sees
        self.error = error
        self.summary = summary      # one line for the screen ("Read 120 lines")
        self.preview = preview or []  # a few lines of output for the screen
        self.diff = diff            # (lines, adds, dels) for edits
        self.meta = meta or {}


class Context:
    """What tools may touch: the working directory, what has been read, background shells, the todo list..."""

    def __init__(self, settings, cwd=None, frontend=None):
        self.settings = settings
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.frontend = frontend
        self.read_state = {}        # path -> mtime when the model last read it (edits need a fresh read)
        self.shells = {}            # background shells
        self.todos = []
        self.checkpoint = None      # set by the session: callable(path) saving a file's old content before it changes
        self.extra_dirs = [os.path.abspath(os.path.expanduser(d)) for d in settings.get("additional_dirs", []) or []]
        self.python = None          # the persistent Python worker
        self.agent = None           # the running Agent (the Task tool starts sub-agents through it)
        self.plan_mode_exit = None
        self.modelapi = None
        self.depth = 0              # 0 = the main agent, 1+ = a sub-agent
        self.abort = None           # threading.Event set when the user interrupts

    def resolve(self, path):
        path = os.path.expanduser(str(path or ""))
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)

    def inside(self, path):
        """True when PATH is in the working directory or an added directory."""
        p = os.path.realpath(path)
        for root in [self.cwd] + self.extra_dirs:
            r = os.path.realpath(root)
            if p == r or p.startswith(r + os.sep):
                return True
        return False

    def interrupted(self):
        return bool(self.abort is not None and self.abort.is_set())

    def limit(self):
        return int(self.settings.get("tool_output_limit", 16000) or 16000)


def slim(node, limit=52):
    """The schema without long parameter descriptions: every token here is paid on every request, and small models read short hints fine."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                if len(v) > limit:
                    v = v.split(". ")[0].split(" (")[0]
                    if len(v) > limit:
                        continue
                out[k] = v
            else:
                out[k] = slim(v, limit)
        return out
    if isinstance(node, list):
        return [slim(x, limit) for x in node]
    return node


class Tool:
    name = ""
    description = ""
    parameters = {"type": "object", "properties": {}}
    kind = "read"          # read | edit | exec | net | other (what it may do, for permissions)
    read_only = True       # allowed in plan mode and for read-only sub-agents
    enabled = True

    def schema(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": slim(self.parameters)}}

    def specifier(self, args, ctx):
        """What a permission rule is matched against (a command, a path, a domain), or None."""
        return None

    def summary(self, args, ctx):
        s = self.specifier(args, ctx)
        s = util.shorten_path(s, ctx.cwd) if (s and self.kind in ("read", "edit") and os.path.isabs(str(s))) else s
        s = (s or "")
        s = s.replace("\n", " ")
        if len(s) > 100:
            s = s[:97] + "..."
        return "%s(%s)" % (self.name, s)

    def preview_diff(self, args, ctx):
        """For edits: (diff lines, adds, dels) shown when asking permission. None when there is no diff."""
        return None

    def run(self, args, ctx):  # pragma: no cover - overridden
        raise NotImplementedError


def need(args, key, typ=str, default=None):
    v = args.get(key, default)
    if v is None:
        raise ToolError("Missing required argument: %s" % key)
    if typ is str and not isinstance(v, str):
        v = str(v)
    if typ is int:
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ToolError("%s must be a whole number" % key)
    return v


def parse_args(raw):
    """Tool arguments from the model: usually a JSON string, sometimes already a dict, sometimes broken."""
    if isinstance(raw, dict):
        return raw
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        pass
    # common slips: trailing commas, single quotes, a missing closing brace
    import re
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)
    for candidate in (fixed, fixed + "}", fixed + '"}'):
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    raise ToolError("The tool arguments weren't valid JSON: %s" % raw[:200])
