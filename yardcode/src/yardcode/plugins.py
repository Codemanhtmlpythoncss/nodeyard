"""Plugins: add your own tools for the model. Two ways, no packaging needed.

1. A Python file in ~/.config/yardcode/plugins/ (or .yardcode/plugins/ in a trusted project):

       TOOLS = [{
           "name": "Dice",
           "description": "Roll dice. Use for random numbers.",
           "parameters": {"type": "object", "properties": {"sides": {"type": "integer"}}, "required": []},
           "run": lambda args, ctx: str(__import__("random").randint(1, int(args.get("sides", 6)))),
           "read_only": True,          # optional: allowed in plan mode without asking
           "kind": "read",             # optional: read | net | exec | edit | other (default other)
       }]

   (or define register(api) and call api.add_tool(...)).

2. A JSON file next to a script: describe the tool and the command to run. The model's arguments arrive as JSON on
   standard input and in $YC_ARGS; whatever the command prints is the result.

       {"name": "DiskFree", "description": "Show free disk space", "command": ["df", "-h"], "read_only": true,
        "parameters": {"type": "object", "properties": {}}}
"""
import importlib.util
import json
import os
import subprocess

from . import util
from .tools.base import Result, Tool, ToolError


class PluginTool(Tool):
    kind = "other"

    def __init__(self, spec, source):
        self.name = str(spec["name"])
        self.description = str(spec.get("description", self.name))
        self.parameters = spec.get("parameters") or {"type": "object", "properties": {}}
        self.read_only = bool(spec.get("read_only"))
        self.kind = spec.get("kind") if spec.get("kind") in ("read", "net", "exec", "edit", "other") else "other"
        self._run = spec.get("run")
        self._command = spec.get("command")
        self._timeout = int(spec.get("timeout", 60))
        self.source = source

    def specifier(self, args, ctx):
        return json.dumps(args, sort_keys=True)[:200]

    def summary(self, args, ctx):
        return "%s(%s)" % (self.name, json.dumps(args)[:90] if args else "")

    def run(self, args, ctx):
        if self._run:
            try:
                out = self._run(args, ctx)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError("%s failed: %s" % (self.name, e))
            if isinstance(out, Result):
                return out
            return Result(str(out) if out is not None else "(no output)", summary="%s done" % self.name)
        cmd = self._command
        if isinstance(cmd, str):
            cmd = ["/bin/sh", "-c", cmd]
        try:
            p = subprocess.run(cmd, input=json.dumps(args), capture_output=True, text=True, timeout=self._timeout, cwd=ctx.cwd,
                               env=dict(os.environ, YC_ARGS=json.dumps(args)))
        except subprocess.TimeoutExpired:
            raise ToolError("%s timed out after %d s." % (self.name, self._timeout))
        except OSError as e:
            raise ToolError("%s can't run: %s" % (self.name, e))
        out = util.truncate_middle((p.stdout or "") + (("\n" + p.stderr) if p.stderr and p.returncode else ""), ctx.limit(), "characters").strip()
        return Result(out or "(no output)", error=p.returncode != 0, summary="%s: exit %d" % (self.name, p.returncode))


class _API:
    def __init__(self, source):
        self.source = source
        self.tools = []

    def add_tool(self, name, description, run, parameters=None, read_only=False, kind="other"):
        self.tools.append(PluginTool({"name": name, "description": description, "run": run, "parameters": parameters, "read_only": read_only, "kind": kind}, self.source))


def plugin_dirs(settings):
    dirs = [os.path.join(util.config_dir(), "plugins")]
    if settings.trusted:
        dirs.append(os.path.join(settings.project_dir, "plugins"))
    return dirs


def load_plugins(settings, on_error=None):
    """Every plugin tool found. A broken plugin is reported and skipped, never fatal."""
    tools = []
    for d in plugin_dirs(settings):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            path = os.path.join(d, fn)
            try:
                if fn.endswith(".py"):
                    spec = importlib.util.spec_from_file_location("yc_plugin_" + fn[:-3], path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    for t in getattr(mod, "TOOLS", []) or []:
                        tools.append(PluginTool(t, path))
                    if hasattr(mod, "register"):
                        api = _API(path)
                        mod.register(api)
                        tools += api.tools
                elif fn.endswith(".json"):
                    data = util.read_json(path)
                    for t in (data if isinstance(data, list) else [data]):
                        if isinstance(t, dict) and t.get("name") and t.get("command"):
                            tools.append(PluginTool(t, path))
            except Exception as e:  # a plugin may do anything wrong
                if on_error:
                    on_error(path, str(e))
    return tools
