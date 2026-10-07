"""Instructions and memory: YARDCODE.md files that are read at the start of every conversation.

Looked for (and all of them used, general first): ~/.config/yardcode/YARDCODE.md, then in every directory from the
filesystem root down to the working directory: YARDCODE.md, AGENTS.md or CLAUDE.md (whichever exist), and
.yardcode/YARDCODE.md. Inside a file, a line "@path/to/other.md" pulls that file in.
"""
import os
import re

from . import util

NAMES = ("YARDCODE.md", "AGENTS.md", "CLAUDE.md")
NOTE_HEADING = "## Notes saved by the agent"


def user_file():
    return os.path.join(util.config_dir(), "YARDCODE.md")


def _expand_imports(text, base, seen, depth=0):
    def repl(m):
        path = os.path.normpath(os.path.join(base, os.path.expanduser(m.group(1))))
        if path in seen or depth >= 4 or not os.path.isfile(path):
            return m.group(0)
        seen.add(path)
        return _expand_imports(util.read_text(path), os.path.dirname(path), seen, depth + 1)
    return re.sub(r"^@(\S+\.md)\s*$", repl, text, flags=re.M)


def find_files(cwd):
    """[(path, scope)] in the order they should be shown to the model."""
    found = []
    uf = user_file()
    if os.path.isfile(uf):
        found.append((uf, "user"))
    chain, d = [], os.path.abspath(cwd)
    while True:
        chain.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for d in reversed(chain):
        for name in NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                found.append((p, "project" if d == os.path.abspath(cwd) else "parent"))
        p = os.path.join(d, ".yardcode", "YARDCODE.md")
        if os.path.isfile(p):
            found.append((p, "project"))
    return found


def load(cwd, limit=24000):
    """(text for the system prompt, [(path, scope, chars)])"""
    parts, info = [], []
    for path, scope in find_files(cwd):
        text = _expand_imports(util.read_text(path), os.path.dirname(path), {path}).strip()
        if text:
            parts.append("Contents of %s (%s instructions):\n\n%s" % (path, scope, text))
            info.append((path, scope, len(text)))
    return util.truncate_middle("\n\n".join(parts), limit), info


def add_note(text, scope, cwd):
    """Append a remembered fact to the user's or the project's file."""
    path = user_file() if scope == "user" else os.path.join(os.path.abspath(cwd), "YARDCODE.md")
    old = util.read_text(path)
    line = "- " + " ".join(text.strip().split())
    if line in old:
        return path, False
    if NOTE_HEADING not in old:
        old = (old.rstrip("\n") + "\n\n" if old.strip() else "") + NOTE_HEADING + "\n"
    util.atomic_write(path, old.rstrip("\n") + "\n" + line + "\n", 0o644)
    return path, True


def notes(scope, cwd):
    path = user_file() if scope == "user" else os.path.join(os.path.abspath(cwd), "YARDCODE.md")
    text = util.read_text(path)
    if NOTE_HEADING not in text:
        return path, []
    block = text.split(NOTE_HEADING, 1)[1]
    return path, [l[2:] for l in block.split("\n") if l.startswith("- ")]


def remove_note(match, scope, cwd):
    path, items = notes(scope, cwd)
    hits = [i for i in items if match.lower() in i.lower()]
    if len(hits) != 1:
        return path, len(hits)
    text = util.read_text(path)
    util.atomic_write(path, text.replace("- " + hits[0] + "\n", "", 1), 0o644)
    return path, 1
