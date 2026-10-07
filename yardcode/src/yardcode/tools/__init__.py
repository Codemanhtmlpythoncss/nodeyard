"""The built-in tools, as a list the agent turns into its tool box."""
from .files import FILE_TOOLS
from .misc import MISC_TOOLS
from .shell import SHELL_TOOLS
from .web import WEB_TOOLS

# The names a person sees in /tools, grouped (the order they are shown to the model).
GROUPS = [
    ("Files", ["Read", "Write", "Edit", "MultiEdit", "LS", "Glob", "Grep"]),
    ("Shell", ["Bash", "BashOutput", "KillShell", "Python"]),
    ("Web", ["WebSearch", "WebFetch", "Wikipedia", "Arxiv", "Weather"]),
    ("Thinking", ["TodoWrite", "Calculator", "FileSearch", "Memory", "AskUserQuestion", "ExitPlanMode", "Task"]),
    ("Cluster", ["Model"]),
]


def builtin(settings, with_cluster=False):
    disabled = set(settings.get("tools.disabled") or [])
    tools = []
    for cls in FILE_TOOLS + SHELL_TOOLS + WEB_TOOLS + MISC_TOOLS:
        if cls.name in disabled:
            continue
        if cls.name == "Model" and not with_cluster:
            continue
        tools.append(cls())
    return tools
