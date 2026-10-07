"""Reading, writing, editing and searching files: Read, Write, Edit, MultiEdit, LS, Glob, Grep."""
import difflib
import fnmatch
import os
import re
import shutil
import subprocess

from .. import ui, util
from .base import Result, Tool, ToolError, need

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox", "dist-packages", ".idea", ".gradle", "target"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".svgz"}
MAX_FILE = 4 * 1024 * 1024


def numbered(lines, start=1):
    return "\n".join("%6d\t%s" % (i, l) for i, l in enumerate(lines, start))


def read_lines(path):
    """The lines of a text file (without line endings), and the line-ending style it uses."""
    with open(path, "rb") as f:
        data = f.read(MAX_FILE + 1)
    if util.is_binary(data):
        raise ToolError("%s looks like a binary file, so it can't be shown as text." % os.path.basename(path))
    text = data.decode("utf-8", "replace")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n").split("\n"), crlf, len(data) > MAX_FILE


def similar_paths(path):
    d, base = os.path.dirname(path) or ".", os.path.basename(path)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return [os.path.join(d, n) for n in difflib.get_close_matches(base, names, n=3, cutoff=0.5)]


def check_fresh(ctx, path):
    """Editing a file you haven't read, or one that changed since, is how edits go wrong."""
    seen = ctx.read_state.get(path)
    if seen is None:
        raise ToolError("Read %s first (use the Read tool) so the edit matches what is really in it." % util.shorten_path(path, ctx.cwd))
    try:
        if os.path.getmtime(path) > seen + 0.001:
            raise ToolError("%s changed since it was read. Read it again before editing." % util.shorten_path(path, ctx.cwd))
    except OSError:
        pass


def snippet(new_text, line_no, around=3):
    lines = new_text.split("\n")
    a, b = max(0, line_no - 1 - around), min(len(lines), line_no + around + 1)
    return numbered(lines[a:b], a + 1)


def apply_edit(text, old, new, replace_all=False):
    """(new text, number of replacements). Raises ToolError with a helpful message when OLD isn't found once."""
    if old == new:
        raise ToolError("old_string and new_string are the same, so nothing would change.")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    if "\r\n" in old:
        old = old.replace("\r\n", "\n")
    if "\r\n" in new:
        new = new.replace("\r\n", "\n")
    n = text.count(old) if old else 0
    if n == 0:
        # forgive trailing whitespace and indentation differences when exactly one place still matches
        loose = _loose_replace(text, old, new)
        if loose is not None:
            return (loose.replace("\n", "\r\n") if crlf else loose), 1
        first = next((l for l in old.split("\n") if l.strip()), "")
        close = difflib.get_close_matches(first.strip(), [l.strip() for l in text.split("\n")], n=2, cutoff=0.6)
        hint = (" Closest lines in the file: " + " | ".join(repr(c[:80]) for c in close)) if close else ""
        raise ToolError("old_string wasn't found in the file. It must match the file's text exactly, including spaces and indentation." + hint)
    if n > 1 and not replace_all:
        raise ToolError("old_string appears %d times. Add more surrounding lines to make it unique, or set replace_all to true." % n)
    out = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return (out.replace("\n", "\r\n") if crlf else out), (n if replace_all else 1)


def _loose_replace(text, old, new):
    """Match OLD ignoring trailing whitespace and indentation style, as long as exactly one place matches."""
    tl, ol = text.split("\n"), old.split("\n")
    while ol and not ol[-1].strip():
        ol.pop()
    if not ol:
        return None
    key = lambda s: re.sub(r"\s+", " ", s.strip())
    ok = [key(l) for l in ol]
    hits = []
    for i in range(len(tl) - len(ol) + 1):
        if all(key(tl[i + j]) == ok[j] for j in range(len(ol))):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    indent_old = re.match(r"\s*", ol[0]).group(0)
    indent_file = re.match(r"\s*", tl[i]).group(0)
    nl = new.split("\n")
    if indent_old != indent_file:  # keep the file's own indentation for the new text
        nl = [(indent_file + l[len(indent_old):]) if l.startswith(indent_old) else l for l in nl]
    return "\n".join(tl[:i] + nl + tl[i + len(ol):])


class Read(Tool):
    name = "Read"
    kind = "read"
    description = "Read a text file with line numbers (offset/limit for big files). Read before editing."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File to read (absolute or relative to the working directory)"},
        "offset": {"type": "integer", "description": "First line to show (1-based)"},
        "limit": {"type": "integer", "description": "How many lines to show"}}, "required": ["path"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path", args.get("file_path", "")))

    def run(self, args, ctx):
        raw = args.get("path", args.get("file_path"))
        path = ctx.resolve(need({"path": raw}, "path"))
        if os.path.isdir(path):
            raise ToolError("%s is a directory. Use LS to list it." % util.shorten_path(path, ctx.cwd))
        if not os.path.exists(path):
            sim = similar_paths(path)
            raise ToolError("No such file: %s.%s" % (util.shorten_path(path, ctx.cwd), (" Did you mean: " + ", ".join(sim)) if sim else ""))
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXT:
            return Result("(%s is an image, %s. It can't be shown to this model.)" % (os.path.basename(path), util.human_bytes(os.path.getsize(path))),
                          summary="Image file")
        if ext == ".pdf":
            if shutil.which("pdftotext"):
                out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=60).stdout
                ctx.read_state[path] = os.path.getmtime(path)
                lines = out.split("\n")
            else:
                raise ToolError("This is a PDF and pdftotext isn't installed (poppler-utils), so it can't be read.")
            crlf, big = False, False
        else:
            lines, crlf, big = read_lines(path)
        ctx.read_state[path] = os.path.getmtime(path)
        total = len(lines) - (1 if lines and lines[-1] == "" else 0)
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or 2000)
        limit = max(1, min(limit, 4000))
        if total == 0:
            return Result("(the file is empty)", summary="Read 0 lines")
        chunk = lines[offset - 1:offset - 1 + limit]
        chunk = [l if len(l) <= 2000 else l[:2000] + " ...[line cut]" for l in chunk]
        text = numbered(chunk, offset)
        more = offset - 1 + len(chunk) < total
        if more:
            text += "\n\n[showing lines %d-%d of %d; use offset=%d to continue]" % (offset, offset + len(chunk) - 1, total, offset + len(chunk))
        if big:
            text += "\n[the file is larger than %s; only the start was loaded]" % util.human_bytes(MAX_FILE)
        return Result(text, summary="Read %d line%s%s" % (len(chunk), "" if len(chunk) == 1 else "s", " (of %d)" % total if more or offset > 1 else ""))


class Write(Tool):
    name = "Write"
    kind = "edit"
    read_only = False
    description = "Create or overwrite a file. Prefer Edit for existing files."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File to write"},
        "content": {"type": "string", "description": "The whole new content"}}, "required": ["path", "content"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path", args.get("file_path", "")))

    def preview_diff(self, args, ctx):
        path = ctx.resolve(args.get("path", args.get("file_path", "")))
        new = args.get("content", "")
        old = ""
        if os.path.exists(path):
            try:
                old = "\n".join(read_lines(path)[0])
            except (ToolError, OSError):
                return None
        return diff_lines(old, new, ctx)

    def run(self, args, ctx):
        path = ctx.resolve(need({"path": args.get("path", args.get("file_path"))}, "path"))
        content = need(args, "content")
        exists = os.path.exists(path)
        if os.path.isdir(path):
            raise ToolError("%s is a directory." % path)
        old = ""
        if exists:
            check_fresh(ctx, path)
            try:
                old = "\n".join(read_lines(path)[0])
            except ToolError:
                old = ""
        if ctx.checkpoint:
            ctx.checkpoint(path)
        util.atomic_write(path, content)
        ctx.read_state[path] = os.path.getmtime(path)
        n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        lines, adds, dels = diff_lines(old, content, ctx)
        return Result("%s %s (%d line%s)." % ("Updated" if exists else "Created", util.shorten_path(path, ctx.cwd), n, "" if n == 1 else "s"),
                      summary=("Updated %s with %d addition%s and %d removal%s" % (util.shorten_path(path, ctx.cwd), adds, "" if adds == 1 else "s", dels, "" if dels == 1 else "s"))
                      if exists else "Created %s (%d line%s)" % (util.shorten_path(path, ctx.cwd), n, "" if n == 1 else "s"), diff=(lines, adds, dels))


def diff_lines(old, new, ctx):
    S = getattr(ctx.frontend, "style", None) or ui.Style(False)
    return ui.render_diff(old, new, S)


class Edit(Tool):
    name = "Edit"
    kind = "edit"
    read_only = False
    description = "Replace exact text in a file; old_string must be unique (or set replace_all)."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File to edit"},
        "old_string": {"type": "string", "description": "The exact text to replace"},
        "new_string": {"type": "string", "description": "What to put there instead"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default: exactly one must match)"}},
        "required": ["path", "old_string", "new_string"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path", args.get("file_path", "")))

    def _new_text(self, args, ctx):
        path = ctx.resolve(need({"path": args.get("path", args.get("file_path"))}, "path"))
        old, new = need(args, "old_string"), need(args, "new_string")
        if not os.path.exists(path):
            if old == "":
                return path, "", new, 0
            raise ToolError("No such file: %s" % util.shorten_path(path, ctx.cwd))
        lines, crlf, _ = read_lines(path)
        text = "\n".join(lines)
        if crlf:
            text = text.replace("\n", "\r\n")
        if old == "" and text.strip():
            raise ToolError("old_string is empty but the file already has content. Give the text to replace.")
        out, n = apply_edit(text, old, new, bool(args.get("replace_all")))
        return path, text, out, n

    def preview_diff(self, args, ctx):
        try:
            _, old, new, _ = self._new_text(args, ctx)
        except (ToolError, OSError):
            return None
        return diff_lines(old.replace("\r\n", "\n"), new.replace("\r\n", "\n"), ctx)

    def run(self, args, ctx):
        path = ctx.resolve(need({"path": args.get("path", args.get("file_path"))}, "path"))
        if os.path.exists(path):
            check_fresh(ctx, path)
        path, old_text, new_text, n = self._new_text(args, ctx)
        if ctx.checkpoint:
            ctx.checkpoint(path)
        util.atomic_write(path, new_text)
        ctx.read_state[path] = os.path.getmtime(path)
        first = new_text.replace("\r\n", "\n").find(need(args, "new_string").replace("\r\n", "\n")[:200]) if args.get("new_string") else 0
        line_no = new_text[:max(0, first)].count("\n") + 1
        lines, adds, dels = diff_lines(old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n"), ctx)
        return Result("Edited %s (%d replacement%s). Now around line %d:\n%s" % (util.shorten_path(path, ctx.cwd), n, "" if n == 1 else "s", line_no,
                                                                               snippet(new_text.replace("\r\n", "\n"), line_no)),
                      summary="Updated %s with %d addition%s and %d removal%s" % (util.shorten_path(path, ctx.cwd), adds, "" if adds == 1 else "s", dels, "" if dels == 1 else "s"),
                      diff=(lines, adds, dels))


class MultiEdit(Edit):
    name = "MultiEdit"
    description = "Several Edit replacements to one file, in order, all or nothing."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File to edit"},
        "edits": {"type": "array", "description": "Replacements, applied one after another", "items": {"type": "object", "properties": {
            "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
            "required": ["old_string", "new_string"]}}}, "required": ["path", "edits"]}

    def _new_text(self, args, ctx):
        path = ctx.resolve(need({"path": args.get("path", args.get("file_path"))}, "path"))
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ToolError("edits must be a non-empty list.")
        if os.path.exists(path):
            lines, crlf, _ = read_lines(path)
            text = "\n".join(lines)
            if crlf:
                text = text.replace("\n", "\r\n")
        elif edits[0].get("old_string", "x") == "":
            text = ""
        else:
            raise ToolError("No such file: %s" % util.shorten_path(path, ctx.cwd))
        orig, total = text, 0
        for i, e in enumerate(edits, 1):
            old_s, new_s = e.get("old_string", ""), e.get("new_string", "")
            try:
                if old_s == "" and not text.strip():
                    text, n = new_s, 1
                else:
                    text, n = apply_edit(text, old_s, new_s, bool(e.get("replace_all")))
            except ToolError as err:
                raise ToolError("Edit %d of %d failed, so nothing was changed: %s" % (i, len(edits), err))
            total += n
        return path, orig, text, total

    def run(self, args, ctx):
        path = ctx.resolve(need({"path": args.get("path", args.get("file_path"))}, "path"))
        if os.path.exists(path):
            check_fresh(ctx, path)
        path, old_text, new_text, n = self._new_text(args, ctx)
        if ctx.checkpoint:
            ctx.checkpoint(path)
        util.atomic_write(path, new_text)
        ctx.read_state[path] = os.path.getmtime(path)
        lines, adds, dels = diff_lines(old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n"), ctx)
        return Result("Edited %s with %d replacement%s." % (util.shorten_path(path, ctx.cwd), n, "" if n == 1 else "s"),
                      summary="Updated %s with %d addition%s and %d removal%s" % (util.shorten_path(path, ctx.cwd), adds, "" if adds == 1 else "s", dels, "" if dels == 1 else "s"),
                      diff=(lines, adds, dels))


class LS(Tool):
    name = "LS"
    kind = "read"
    description = "List a directory (two levels)."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "Directory (default: the working directory)"},
        "ignore": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to leave out"}}}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path") or ".")

    def run(self, args, ctx):
        root = ctx.resolve(args.get("path") or ".")
        if not os.path.isdir(root):
            raise ToolError("%s isn't a directory." % util.shorten_path(root, ctx.cwd))
        ignore = [g for g in (args.get("ignore") or []) if isinstance(g, str)]
        out, count = [], [0]

        def walk(d, depth):
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError as e:
                out.append("  " * depth + "(can't open: %s)" % e.strerror)
                return
            for e in entries:
                if e.name in SKIP_DIRS or any(fnmatch.fnmatch(e.name, g) for g in ignore):
                    continue
                if count[0] >= 300:
                    return
                count[0] += 1
                isdir = e.is_dir(follow_symlinks=False)
                out.append("  " * depth + "- " + e.name + ("/" if isdir else ""))
                if isdir and depth < 1:
                    walk(e.path, depth + 1)

        walk(root, 0)
        if count[0] >= 300:
            out.append("... (only the first 300 entries are shown)")
        head = "%s/" % util.shorten_path(root, ctx.cwd)
        return Result(head + "\n" + "\n".join(out) if out else head + "\n(empty)", summary="Listed %d entries" % count[0])


def glob_to_regex(pat):
    i, n, out = 0, len(pat), ""
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "{":
            j = pat.find("}", i)
            if j > i:
                out += "(?:" + "|".join(re.escape(x) for x in pat[i + 1:j].split(",")) + ")"
                i = j + 1
                continue
            out += re.escape(c)
        elif c == "[":
            j = pat.find("]", i)
            if j > i:
                out += pat[i:j + 1]
                i = j + 1
                continue
            out += re.escape(c)
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def walk_files(root, skip=SKIP_DIRS):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in skip)
        for f in sorted(files):
            yield os.path.join(d, f)


class Glob(Tool):
    name = "Glob"
    kind = "read"
    description = "Find files by pattern, newest first (e.g. **/*.py)."
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Glob pattern"},
        "path": {"type": "string", "description": "Directory to search (default: the working directory)"}}, "required": ["pattern"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path") or ".")

    def run(self, args, ctx):
        pat = need(args, "pattern")
        root = ctx.resolve(args.get("path") or ".")
        if not os.path.isdir(root):
            raise ToolError("%s isn't a directory." % util.shorten_path(root, ctx.cwd))
        rx = glob_to_regex(pat if "/" in pat or pat.startswith("**") else "**/" + pat)
        hits = []
        for p in walk_files(root):
            if rx.match(os.path.relpath(p, root).replace(os.sep, "/")):
                try:
                    hits.append((os.path.getmtime(p), p))
                except OSError:
                    pass
        hits.sort(reverse=True)
        shown = hits[:200]
        if not shown:
            return Result("No files match %s in %s." % (pat, util.shorten_path(root, ctx.cwd)), summary="No matches")
        text = "\n".join(util.shorten_path(p, ctx.cwd) for _, p in shown)
        if len(hits) > len(shown):
            text += "\n... and %d more (narrow the pattern)" % (len(hits) - len(shown))
        return Result(text, summary="Found %d file%s" % (len(hits), "" if len(hits) == 1 else "s"), preview=text.split("\n")[:5])


TYPE_EXT = {"py": [".py"], "js": [".js", ".jsx", ".mjs", ".cjs"], "ts": [".ts", ".tsx"], "go": [".go"], "rust": [".rs"], "java": [".java"], "c": [".c", ".h"],
            "cpp": [".cpp", ".cc", ".hpp", ".h"], "md": [".md"], "json": [".json"], "yaml": [".yml", ".yaml"], "sh": [".sh", ".bash"], "html": [".html"],
            "css": [".css"], "rb": [".rb"], "php": [".php"], "toml": [".toml"]}


class Grep(Tool):
    name = "Grep"
    kind = "read"
    description = "Regex search of file contents. output_mode: content, files_with_matches, count."
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Regular expression"},
        "path": {"type": "string", "description": "File or folder (default: here)"},
        "glob": {"type": "string", "description": "Only files matching, e.g. *.py"},
        "type": {"type": "string", "description": "py, js, ts, go, rust, java, c, md, json, yaml, sh"},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
        "-i": {"type": "boolean", "description": "Ignore case"},
        "-C": {"type": "integer", "description": "Context lines"},
        "head_limit": {"type": "integer", "description": "Max result lines (200)"}}, "required": ["pattern"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path") or ".")

    def run(self, args, ctx):
        pat = need(args, "pattern")
        root = ctx.resolve(args.get("path") or ".")
        if not os.path.exists(root):
            raise ToolError("No such path: %s" % util.shorten_path(root, ctx.cwd))
        mode = args.get("output_mode") or "content"
        flags = re.I if args.get("-i") else 0
        if args.get("multiline"):
            flags |= re.S | re.M
        try:
            rx = re.compile(pat, flags | (0 if args.get("multiline") else re.M))
        except re.error as e:
            raise ToolError("Bad regular expression: %s" % e)
        ctx_a = int(args.get("-C") or args.get("-A") or 0)
        ctx_b = int(args.get("-C") or args.get("-B") or 0)
        limit = int(args.get("head_limit") or 200)
        gl = args.get("glob")
        grx = glob_to_regex(gl if "/" in gl else "**/" + gl) if gl else None
        exts = TYPE_EXT.get(args.get("type") or "", None)
        files = [root] if os.path.isfile(root) else walk_files(root)
        out, nfiles, total = [], 0, 0
        for p in files:
            rel = os.path.relpath(p, root if os.path.isdir(root) else os.path.dirname(root)).replace(os.sep, "/")
            if grx and not grx.match(rel):
                continue
            if exts and os.path.splitext(p)[1].lower() not in exts:
                continue
            try:
                if os.path.getsize(p) > MAX_FILE:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if util.is_binary(data):
                continue
            text = data.decode("utf-8", "replace")
            shown = util.shorten_path(p, ctx.cwd)
            if args.get("multiline"):
                ms = list(rx.finditer(text))
                if not ms:
                    continue
                nfiles += 1
                total += len(ms)
                if mode == "content":
                    for m in ms:
                        ln = text.count("\n", 0, m.start()) + 1
                        out.append("%s:%d:%s" % (shown, ln, m.group(0).replace("\n", "\\n")[:300]))
                elif mode == "files_with_matches":
                    out.append(shown)
                else:
                    out.append("%s:%d" % (shown, len(ms)))
                continue
            lines = text.split("\n")
            hit = [i for i, l in enumerate(lines) if rx.search(l)]
            if not hit:
                continue
            nfiles += 1
            total += len(hit)
            if mode == "files_with_matches":
                out.append(shown)
            elif mode == "count":
                out.append("%s:%d" % (shown, len(hit)))
            else:
                last = -1
                for i in hit:
                    a, b = max(0, i - ctx_b), min(len(lines) - 1, i + ctx_a)
                    if last >= 0 and a > last + 1 and (ctx_a or ctx_b):
                        out.append("--")
                    for k in range(max(a, last + 1), b + 1):
                        sep = ":" if k == i or rx.search(lines[k]) else "-"
                        out.append("%s%s%d%s%s" % (shown, sep, k + 1, sep, lines[k][:400]))
                    last = max(last, b)
            if len(out) > limit * 3:
                break
        if not out:
            return Result("No matches for %s." % pat, summary="No matches")
        more = len(out) > limit
        text = "\n".join(out[:limit]) + ("\n... (%d more lines; narrow the search or raise head_limit)" % (len(out) - limit) if more else "")
        return Result(text, summary="Found %d match%s in %d file%s" % (total, "" if total == 1 else "es", nfiles, "" if nfiles == 1 else "s"), preview=out[:5])


FILE_TOOLS = [Read, Write, Edit, MultiEdit, LS, Glob, Grep]
_ = subprocess
