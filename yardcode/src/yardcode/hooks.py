"""Hooks: your own commands that run at set moments (same layout as Claude Code's settings).

  "hooks": {
    "PreToolUse":  [{"matcher": "Bash",       "hooks": [{"type": "command", "command": "./check.sh", "timeout": 30}]}],
    "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format ."}]}],
    "UserPromptSubmit": [...], "Stop": [...], "SessionStart": [...], "SessionEnd": [...], "PreCompact": [...]
  }

A hook receives JSON on standard input (event, session, cwd, tool name and input...). Exit code 0 means fine (for
UserPromptSubmit and SessionStart, what it prints is added to the conversation); exit code 2 blocks (PreToolUse: the
call is refused and its stderr goes to the model; Stop: the model is told to keep going); other codes are shown as warnings.
"""
import json
import os
import re
import subprocess

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "SessionEnd", "PreCompact")


class HookResult:
    def __init__(self):
        self.blocked = False
        self.message = ""      # why it was blocked / what to tell the model
        self.context = []      # text to add to the conversation
        self.warnings = []


def run_hooks(settings, event, payload, cwd, match_value=None, session_id=""):
    res = HookResult()
    groups = (settings.get("hooks") or {}).get(event) or []
    body = dict(payload, hook_event_name=event, session_id=session_id, cwd=cwd)
    for g in groups:
        if not isinstance(g, dict):
            continue
        matcher = g.get("matcher")
        if matcher and match_value is not None:
            try:
                if not re.fullmatch(matcher, match_value):
                    continue
            except re.error:
                continue
        for h in g.get("hooks") or []:
            if not isinstance(h, dict) or h.get("type", "command") != "command" or not h.get("command"):
                continue
            timeout = int(h.get("timeout") or 30)
            try:
                p = subprocess.run(h["command"], shell=True, input=json.dumps(body), capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                   env=dict(os.environ, YARDCODE_HOOK=event, YARDCODE_PROJECT_DIR=cwd))
            except subprocess.TimeoutExpired:
                res.warnings.append("hook %r timed out after %ds" % (h["command"][:60], timeout))
                continue
            except OSError as e:
                res.warnings.append("hook %r failed: %s" % (h["command"][:60], e))
                continue
            out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
            if p.returncode == 2:
                res.blocked = True
                res.message = err or out or "blocked by a hook"
                return res
            if p.returncode != 0:
                res.warnings.append("hook %r exited %d%s" % (h["command"][:60], p.returncode, (": " + err[:200]) if err else ""))
                continue
            if out:
                try:
                    j = json.loads(out)
                    if isinstance(j, dict):
                        if j.get("decision") == "block" or j.get("continue") is False:
                            res.blocked = True
                            res.message = j.get("reason") or j.get("stopReason") or "blocked by a hook"
                            return res
                        out = j.get("additionalContext") or j.get("context") or ""
                except ValueError:
                    pass
                if out and event in ("UserPromptSubmit", "SessionStart"):
                    res.context.append(out)
    return res
