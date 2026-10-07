"""Terminal drawing: colours, markdown, code highlighting, diffs, boxes and the spinner.

Everything here is pure presentation. Colours switch off when output isn't a terminal, NO_COLOR is set,
or the theme is "none"; glyphs fall back to plain ASCII on terminals that aren't UTF-8.
"""
import difflib
import locale
import os
import re
import shutil
import sys
import threading
import time
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RESET = "\x1b[0m"

PALETTES = {
    "dark": {"accent": "#7aa2f7", "ok": "#9ece6a", "warn": "#e0af68", "err": "#f7768e", "muted": "#7f869e", "code": "#c0caf5",
             "heading": "#bb9af7", "link": "#7dcfff", "user": "#e0af68", "add_bg": "#16301f", "del_bg": "#3a1a22", "think": "#6b7390"},
    "light": {"accent": "#2a56d6", "ok": "#1b7a2e", "warn": "#9a5b00", "err": "#c0262d", "muted": "#6b7280", "code": "#1f2937",
              "heading": "#6d28d9", "link": "#0369a1", "user": "#9a5b00", "add_bg": "#d9f2de", "del_bg": "#fadadd", "think": "#8a8f9c"},
}


def _hex(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to256(r, g, b):
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return round((r - 8) / 247 * 24) + 232
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def vwidth(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s):
    return sum(vwidth(c) for c in ANSI_RE.sub("", s))


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def terminal_width(default=100):
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except OSError:
        return default


def utf8_ok():
    enc = (getattr(sys.stdout, "encoding", "") or locale.getpreferredencoding(False) or "").lower()
    return "utf" in enc and os.environ.get("TERM") != "linux"


class Style:
    """Colour helpers: S.accent("text"), S.bold(...), S.fg("ok", ...). Everything is plain text when colour is off."""

    def __init__(self, enabled=True, theme="dark", truecolor=None):
        self.enabled = enabled and theme != "none"
        self.theme = theme if theme in PALETTES else "dark"
        if truecolor is None:
            truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        self.truecolor = truecolor
        self.pal = PALETTES[self.theme]
        self.unicode = utf8_ok()

    def _code(self, name, bg=False):
        r, g, b = _hex(self.pal[name])
        if self.truecolor:
            return "\x1b[%d;2;%d;%d;%dm" % (48 if bg else 38, r, g, b)
        return "\x1b[%d;5;%dm" % (48 if bg else 38, _to256(r, g, b))

    def wrap(self, codes, text):
        if not self.enabled or text == "":
            return text
        return codes + text + RESET

    def fg(self, name, text):
        return self.wrap(self._code(name), text)

    def bg(self, name, text, fg=None):
        return self.wrap((self._code(fg) if fg else "") + self._code(name, bg=True), text)

    def bold(self, t):
        return self.wrap("\x1b[1m", t)

    def dim(self, t):
        return self.wrap("\x1b[2m", t)

    def italic(self, t):
        return self.wrap("\x1b[3m", t)

    def underline(self, t):
        return self.wrap("\x1b[4m", t)

    def strike(self, t):
        return self.wrap("\x1b[9m", t)

    def accent(self, t):
        return self.fg("accent", t)

    def ok(self, t):
        return self.fg("ok", t)

    def warn(self, t):
        return self.fg("warn", t)

    def err(self, t):
        return self.fg("err", t)

    def muted(self, t):
        return self.fg("muted", t)

    def code(self, t):
        return self.fg("code", t)

    def g(self, name):
        """A glyph, or its ASCII stand-in."""
        u = {"dot": "●", "arrow": "❯", "elbow": "⎿", "bar": "│", "bullet": "•", "check": "✓", "cross": "✗", "todo": "☐", "done": "☒",
             "prog": "◐", "hr": "─", "corner_tl": "╭", "corner_tr": "╮", "corner_bl": "╰", "corner_br": "╯", "vbar": "│", "ell": "…",
             "up": "↑", "down": "↓", "lt": "▎", "tri": "▸", "warn": "⚠"}
        a = {"dot": "*", "arrow": ">", "elbow": "`-", "bar": "|", "bullet": "-", "check": "+", "cross": "x", "todo": "[ ]", "done": "[x]",
             "prog": "[~]", "hr": "-", "corner_tl": "+", "corner_tr": "+", "corner_bl": "+", "corner_br": "+", "vbar": "|", "ell": "...",
             "up": "^", "down": "v", "lt": "|", "tri": ">", "warn": "!"}
        return (u if self.unicode else a)[name]


def detect_style(theme="auto", stream=None):
    stream = stream or sys.stdout
    tty = hasattr(stream, "isatty") and stream.isatty()
    if os.environ.get("NO_COLOR") or theme == "none" or not tty or os.environ.get("TERM") == "dumb":
        return Style(False, "dark")
    if theme == "auto":
        bgvar = os.environ.get("COLORFGBG", "")
        theme = "light" if bgvar.split(";")[-1] in ("15", "7") else "dark"
    return Style(True, theme)


# ---- wrapping ---------------------------------------------------------------------------------

def wrap_ansi(text, width, first="", rest=""):
    """Word-wrap a string that contains colour codes; colours carry over onto the continuation lines."""
    width = max(20, width)
    lines, cur, curlen = [], first, vlen(first)
    active = ""
    pend = ""

    def note(tok):
        nonlocal active
        for m in ANSI_RE.finditer(tok):
            active = "" if m.group(0) == RESET else active + m.group(0)

    for tok in re.split(r"( +)", text):
        if tok == "":
            continue
        w = vlen(tok)
        if tok.isspace():
            if curlen + w < width and curlen > vlen(first if not lines else rest):
                pend = tok
            continue
        gap = vlen(pend)
        if curlen + gap + w > width and curlen > vlen(first if not lines else rest):
            lines.append(cur + (RESET if active else ""))
            cur, curlen = rest + active, vlen(rest)
            pend, gap = "", 0
        cur += pend + tok
        curlen += gap + w
        pend = ""
        note(tok)
    lines.append(cur)
    return lines


# ---- syntax highlighting -------------------------------------------------------------------------

KEYWORDS = {
    "py": "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self",
    "js": "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements",
    "c": "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class namespace template typename using new delete public private protected virtual bool true false nullptr",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
    "rs": "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
    "sh": "if then else elif fi for while until do done case esac in function select time return exit local export readonly declare unset shift break continue echo cd test",
    "sql": "SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE DROP ALTER ADD JOIN LEFT RIGHT INNER OUTER ON GROUP BY ORDER LIMIT HAVING AS AND OR NOT NULL DISTINCT UNION select from where insert into values update set delete create table drop alter add join left right inner outer on group by order limit having as and or not null distinct union",
    "json": "true false null",
    "yaml": "true false null yes no on off",
}
LANG_FAMILY = {"python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "js", "ts": "js", "tsx": "js", "jsx": "js", "java": "c", "c": "c", "cpp": "c",
               "c++": "c", "cs": "c", "csharp": "c", "kotlin": "c", "swift": "c", "php": "c", "go": "go", "golang": "go", "rust": "rs", "rs": "rs",
               "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh", "console": "sh", "sql": "sql", "json": "json", "jsonc": "json", "yaml": "yaml",
               "yml": "yaml", "toml": "yaml", "ruby": "py", "dockerfile": "sh", "makefile": "sh"}
COMMENT = {"py": "#", "sh": "#", "yaml": "#", "js": "//", "c": "//", "go": "//", "rs": "//", "sql": "--", "json": None}
TOKEN_RE = re.compile(r"""(?P<str>"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)|(?P<num>\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\b)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)""")


def highlight(line, lang, S):
    fam = LANG_FAMILY.get((lang or "").lower())
    if not S.enabled or not fam:
        return line
    kw = set(KEYWORDS.get(fam, "").split())
    cm = COMMENT.get(fam)
    code, comment = line, ""
    if cm:
        # a comment marker outside quotes
        quote, i = "", 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "\"'`":
                quote = ch
            elif line.startswith(cm, i) and not (cm == "#" and i > 0 and line[i - 1] == "$") and not (cm == "//" and i > 0 and line[i - 1] == ":"):
                code, comment = line[:i], line[i:]
                break
            i += 1
    out, pos = [], 0
    for m in TOKEN_RE.finditer(code):
        out.append(code[pos:m.start()])
        t = m.group(0)
        if m.group("str"):
            out.append(S.fg("ok", t))
        elif m.group("num"):
            out.append(S.fg("warn", t))
        elif t in kw:
            out.append(S.fg("heading", t))
        else:
            out.append(t)
        pos = m.end()
    out.append(code[pos:])
    if comment:
        out.append(S.fg("muted", comment))
    return "".join(out)


# ---- inline markdown ---------------------------------------------------------------------------

INLINE_RE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|(?<![\w*])\*(?P<it>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<it2>[^_\s][^_]*?)_(?![\w_])"
    r"|~~(?P<st>.+?)~~"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\)"
    r"|(?P<url>https?://[^\s<>)\]]+)")


def inline(text, S):
    if not S.enabled:
        text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", text)
        return re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", text)
    out, pos = [], 0
    for m in INLINE_RE.finditer(text):
        out.append(text[pos:m.start()])
        if m.group("codetext") is not None:
            out.append(S.code(m.group("codetext")))
        elif m.group("bold") or m.group("bold2"):
            out.append(S.bold(inline(m.group("bold") or m.group("bold2"), S)))
        elif m.group("it") or m.group("it2"):
            out.append(S.italic(inline(m.group("it") or m.group("it2"), S)))
        elif m.group("st"):
            out.append(S.strike(m.group("st")))
        elif m.group("lurl") and m.group("ltext"):
            out.append(S.underline(S.fg("link", m.group("ltext"))) + S.dim(" (" + m.group("lurl") + ")"))
        elif m.group("url"):
            out.append(S.underline(S.fg("link", m.group("url"))))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def render_table(rows, S, width):
    """rows: list of lists of cell strings (the separator row already removed; first row is the header)."""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    cells = [[inline(c.strip(), S) for c in r] for r in rows]
    widths = [max(vlen(c[i]) for c in cells) for i in range(ncol)]
    total = sum(widths) + 3 * ncol + 1
    if total > width:  # shrink the widest columns
        over = total - width
        while over > 0 and max(widths) > 8:
            i = widths.index(max(widths))
            widths[i] -= 1
            over -= 1
    v = S.g("vbar")

    def row(r, head=False):
        wrapped = [wrap_ansi(c, w) if vlen(c) > w else [c] for c, w in zip(r, widths)]
        height = max(len(x) for x in wrapped)
        out = []
        for k in range(height):
            parts = []
            for x, w in zip(wrapped, widths):
                cell = x[k] if k < len(x) else ""
                if head:
                    cell = S.bold(cell)
                parts.append(cell + " " * max(0, w - vlen(cell)))
            out.append(S.muted(v) + " " + (" " + S.muted(v) + " ").join(parts) + " " + S.muted(v))
        return out

    line = lambda l, m, r: S.muted(l + m.join(S.g("hr") * (w + 2) for w in widths) + r)
    out = [line(S.g("corner_tl"), S.g("hr"), S.g("corner_tr"))] if S.unicode else []
    out += row(cells[0], True)
    out.append(line("├", "┼", "┤") if S.unicode else S.muted("+" + "+".join("-" * (w + 2) for w in widths) + "+"))
    for r in cells[1:]:
        out += row(r)
    if S.unicode:
        out.append(line(S.g("corner_bl"), "┴", S.g("corner_br")))
    return out


class Markdown:
    """Turns streamed markdown into styled terminal lines, one finished line at a time."""

    def __init__(self, S, width=None):
        self.S = S
        self.width = width or terminal_width()
        self.in_code = False
        self.fence = ""
        self.lang = ""
        self.table = []
        self.blank_run = 0

    def lines(self, raw):
        """Render one complete source line to zero or more output lines."""
        S, w = self.S, self.width
        line = raw.rstrip("\r\n")
        m = re.match(r"^(\s*)(```+|~~~+)\s*([\w+#.-]*)", line)
        if self.in_code:
            if m and m.group(2).startswith(self.fence[:3]) and not line.strip().strip("`~"):
                self.in_code = False
                return [S.muted("  " + S.g("corner_bl") + S.g("hr") * 3)] if S.unicode else []
            return [S.muted("  " + S.g("vbar") + " ") + highlight(line, self.lang, S)]
        out = self._flush_table_if(line)
        if m and (m.group(2) or "").startswith(("```", "~~~")):
            self.in_code, self.fence, self.lang = True, m.group(2), m.group(3) or ""
            label = self.lang or "code"
            out.append(S.muted("  " + S.g("corner_tl") + S.g("hr") + " ") + S.dim(label))
            return out
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            self.table.append(line)
            return out
        if not line.strip():
            self.blank_run += 1
            return out + ([""] if self.blank_run == 1 else [])
        self.blank_run = 0
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
        if h:
            level = len(h.group(1))
            text = inline(h.group(2), S)
            if level == 1:
                return out + ["", S.bold(S.fg("heading", strip_ansi(text))), S.muted(S.g("hr") * min(w - 2, max(8, vlen(text))))]
            return out + ["", S.bold(S.fg("heading", ("#" * level + " " if level > 3 else "") + strip_ansi(text)))]
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            return out + [S.muted(S.g("hr") * min(w - 2, 60))]
        b = re.match(r"^(\s*)([-*+])\s+(?:\[([ xX])\]\s+)?(.*)$", line)
        if b:
            depth = len(b.group(1).replace("\t", "    ")) // 2
            mark = S.g("bullet") if b.group(3) is None else (S.g("done") if b.group(3) != " " else S.g("todo"))
            first = "  " * (depth + 1) + S.accent(mark) + " "
            return out + wrap_ansi(inline(b.group(4), S), w, first, "  " * (depth + 1) + "  ")
        n = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if n:
            depth = len(n.group(1).replace("\t", "    ")) // 2
            first = "  " * (depth + 1) + S.accent(n.group(2) + ".") + " "
            return out + wrap_ansi(inline(n.group(3), S), w, first, "  " * (depth + 1) + " " * (len(n.group(2)) + 2))
        q = re.match(r"^\s*>\s?(.*)$", line)
        if q:
            bar = S.muted(S.g("lt") + " ")
            return out + wrap_ansi(S.italic(inline(q.group(1), S)), w, bar, bar)
        return out + wrap_ansi(inline(line, S), w, "", "")

    def _flush_table_if(self, line):
        if self.table and not (line.lstrip().startswith("|") and line.count("|") >= 2):
            return self.end_table()
        return []

    def end_table(self):
        if not self.table:
            return []
        rows = []
        for l in self.table:
            if TABLE_SEP_RE.match(l):
                continue
            cells = l.strip().strip("|").split("|")
            rows.append(cells)
        self.table = []
        return render_table(rows, self.S, self.width) if rows else []

    def finish(self):
        return self.end_table()


def render_markdown(text, S, width=None):
    md = Markdown(S, width)
    out = []
    for line in text.split("\n"):
        out += md.lines(line)
    out += md.finish()
    return "\n".join(out)


# ---- diffs -------------------------------------------------------------------------------------

def render_diff(old, new, S, path="", context=3, max_lines=60):
    """A coloured unified diff with line numbers (what an edit changes), as a list of lines."""
    a, b = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out, shown = [], 0
    adds = dels = 0
    for group in sm.get_grouped_opcodes(context):
        if out:
            out.append(S.muted("   ..."))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i1, i2):
                    out.append(S.muted("%4d " % (k + 1)) + "  " + a[k])
            else:
                for k in range(i1, i2):
                    dels += 1
                    out.append(S.bg("del_bg", "%4d - %s" % (k + 1, a[k])) if S.enabled else "%4d - %s" % (k + 1, a[k]))
                for k in range(j1, j2):
                    adds += 1
                    out.append(S.bg("add_bg", "%4d + %s" % (k + 1, b[k])) if S.enabled else "%4d + %s" % (k + 1, b[k]))
        shown = len(out)
        if shown > max_lines:
            out = out[:max_lines] + [S.muted("   ... %d more lines of changes" % (shown - max_lines))]
            break
    return out, adds, dels


def box(title, lines, S, width=None, color="accent"):
    width = min(width or terminal_width(), 110)
    inner = width - 4
    tl, tr, bl, br, h, v = S.g("corner_tl"), S.g("corner_tr"), S.g("corner_bl"), S.g("corner_br"), S.g("hr"), S.g("vbar")
    c = lambda t: S.fg(color, t)
    head = (" " + title + " ") if title else ""
    out = [c(tl + h) + S.bold(head) + c(h * max(0, width - 3 - vlen(head)) + tr)]
    for ln in lines:
        for piece in (wrap_ansi(ln, inner) if vlen(ln) > inner else [ln]):
            out.append(c(v) + " " + piece + " " * max(0, inner - vlen(piece)) + " " + c(v))
    out.append(c(bl + h * (width - 2) + br))
    return out


# ---- the spinner ---------------------------------------------------------------------------------

class Spinner:
    """A one-line "working" indicator that cleans up after itself; does nothing when output isn't a terminal."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, S, out=None):
        self.S = S
        self.out = out or sys.stdout
        self.active = False
        self.label = ""
        self.tokens = 0
        self.start = 0.0
        self.hint = "esc to interrupt"
        self._thread = None
        self._lock = threading.Lock()
        self.live = bool(S.enabled and hasattr(self.out, "isatty") and self.out.isatty())

    def begin(self, label="Working", hint="esc to interrupt"):
        with self._lock:
            if self.active:
                self.label = label
                return
            self.active, self.label, self.tokens, self.start, self.hint = True, label, 0, time.time(), hint
        if self.live:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def update(self, label=None, tokens=None):
        if label is not None:
            self.label = label
        if tokens is not None:
            self.tokens = tokens

    def end(self):
        with self._lock:
            was = self.active
            self.active = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if was and self.live:
            self.out.write("\r\x1b[2K")
            self.out.flush()

    def _run(self):
        i = 0
        while True:
            with self._lock:
                if not self.active:
                    return
                label, tokens, start, hint = self.label, self.tokens, self.start, self.hint
            frames = self.FRAMES if self.S.unicode else "|/-\\"
            el = time.time() - start
            parts = ["%ds" % el]
            if tokens:
                parts.append("%s tokens" % tokens)
            if hint:
                parts.append(hint)
            line = "%s %s %s" % (self.S.accent(frames[i % len(frames)]), self.S.fg("accent", label + self.S.g("ell")),
                                 self.S.muted("(" + " · ".join(parts) + ")"))
            width = terminal_width()
            if vlen(line) > width - 1:
                line = line[:width * 2]
            self.out.write("\r\x1b[2K" + line)
            self.out.flush()
            i += 1
            time.sleep(0.1)
