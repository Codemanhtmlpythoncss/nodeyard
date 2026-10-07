"""The prompt: a small line editor with a live menu for /commands and @files (pure standard library, macOS and Linux).

The input sits between two rules with the permission mode and model under it. Type "/" and the commands appear above it, filtering as
you type; Up/Down choose, Tab or Enter completes. "@" does the same for files. "!" on an empty prompt switches to shell mode (a
shell prompt: Tab completes files, backspace or Esc on an empty line goes back). Also: arrow keys, Home/End, word moves (alt+b / alt+f), ctrl+u / ctrl+k / ctrl+w, history (Up/Down), a line ending in \\
or alt+Enter (ctrl+J) continues on a new line, pasted text arrives whole, ctrl+L clears the screen, ctrl+D leaves, and shift+tab
cycles the permission mode.
"""
import codecs
import json
import os
import re
import select
import shutil
import sys

from . import ui

try:
    import termios
    import tty
except ImportError:  # pragma: no cover  (Windows)
    termios = tty = None

CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
MENU_ROWS = 8
SPECIAL_MODE = "/mode"


class LineEditor:
    def __init__(self, S, out=None, history_path="", commands=None, files=None, stdin=None, footer=None, shell_prompt=None):
        self.S = S
        self.footer = footer or (lambda: ("", "muted"))        # -> (text, tone) shown under the input
        self.shell_prompt = shell_prompt                      # -> the prompt text while in shell mode
        self.mode = ""                                        # "" or "shell"
        self.base_prompt = ""
        self._tab_open = False
        self.out = out or sys.stdout
        self.stdin = stdin or sys.stdin
        self.history_path = history_path
        self.commands = commands or (lambda: [])      # -> [(name, help)]
        self.files = files or default_files
        self.history = self._load()
        self.buf = ""
        self.pos = 0
        self.sel = 0
        self.hist_i = None
        self.draft = ""
        self._cur_row = 0
        self._menu = ("", [])      # (kind, [(label, help, insert, replace_from)])

    # ---- environment ----
    def available(self):
        try:
            return bool(termios and self.stdin.isatty() and self.out.isatty())
        except (AttributeError, ValueError):
            return False

    # ---- history ----
    def _load(self):
        out = []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        t = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(t, str) and t.strip():
                        out.append(t)
        except OSError:
            pass
        return out[-2000:]

    def _remember(self, text):
        if not text.strip() or (self.history and self.history[-1] == text):
            return
        self.history.append(text)
        try:
            os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(text, ensure_ascii=False) + "\n")
            os.chmod(self.history_path, 0o600)
        except OSError:
            pass

    # ---- menu ----
    def _token_at_cursor(self):
        i = self.pos
        while i > 0 and self.buf[i - 1] not in " \t\n":
            i -= 1
        return i, self.buf[i:self.pos]

    def _update_menu(self):
        buf = self.buf
        items, kind = [], ""
        if self.mode == "shell":
            if not self._tab_open:
                self._menu = ("", [])
                self.sel = 0
            return
        if buf.startswith("/") and "\n" not in buf and " " not in buf and self.pos == len(buf):
            pre = buf[1:].lower()
            items = [(("/" + n), h, "/" + n + " ", 0) for n, h in self.commands() if n.lower().startswith(pre)]
            kind = "slash"
        else:
            start, tok = self._token_at_cursor()
            if tok.startswith("@") and len(tok) >= 1:
                for path, isdir in self.files(tok[1:])[:40]:
                    items.append(("@" + path + ("/" if isdir else ""), "folder" if isdir else "", "@" + path + ("/" if isdir else " "), start))
                kind = "file"
        self._menu = (kind, items)
        self.sel = max(0, min(self.sel, len(items) - 1)) if items else 0

    # ---- drawing ----
    def _width(self):
        return max(20, shutil.get_terminal_size((100, 24)).columns)

    def current_prompt(self):
        if self.mode == "shell":
            where = self.shell_prompt() if self.shell_prompt else "shell"
            return self.S.bold(self.S.ok(where)) + " " + self.S.bold(self.S.accent("$")) + " "
        return self.base_prompt

    def _footer_text(self):
        if self.mode == "shell":
            return "! shell mode · Tab completes files · Esc, backspace on an empty line, or exit goes back to chat", "ok"
        try:
            text, tone = self.footer()
        except Exception:
            return "", "muted"
        return text, tone

    def _menu_lines(self, width):
        kind, items = self._menu
        if not items:
            return []
        S = self.S
        n = len(items)
        top = max(0, min(self.sel - MENU_ROWS + 1, n - MENU_ROWS)) if n > MENU_ROWS else 0
        show = items[top:top + MENU_ROWS]
        label_w = min(30, max(ui.vlen(x[0]) for x in show))
        lines = []
        for k, (label, help_, _ins, _from) in enumerate(show):
            on = (top + k) == self.sel
            room = max(0, width - label_w - 6)
            h = help_ if len(help_) <= room else help_[:max(0, room - 1)] + S.g("ell")
            plain = "  " + label[:label_w].ljust(label_w) + "  " + h
            if on:
                lines.append(S.bg("add_bg", plain) if S.enabled else "> " + plain[2:])
            else:
                lines.append("  " + S.accent(label[:label_w].ljust(label_w)) + "  " + S.muted(h))
        if n > len(show):
            lines.insert(0, S.muted(("  %d-%d of %d · up/down to browse, keep typing to narrow it down" % (top + 1, top + len(show), n))[:width - 1]))
        return lines

    def _render(self, prompt, final=False):
        S, out, width = self.S, self.out, self._width()
        prompt = self.current_prompt()
        parts = self.buf.split("\n")
        cont = S.muted("… ")
        rows, texts = [], []
        for i, ln in enumerate(parts):
            t = (prompt if i == 0 else cont) + ln
            texts.append(t)
            v = ui.vlen(t)
            rows.append(max(1, (v - 1) // width + 1) if v else 1)
        # where the cursor goes, in rows from the top of the input
        before = self.buf[:self.pos].split("\n")
        li = len(before) - 1
        col_v = ui.vlen(prompt if li == 0 else cont) + ui.vlen(before[-1])
        cur_row = sum(rows[:li]) + col_v // width
        cur_col = col_v % width
        total = sum(rows)
        # back to the top of what was drawn last time, and wipe it
        out.write("\r" + ("\x1b[%dA" % self._cur_row if self._cur_row else "") + "\x1b[J")
        if final:                       # leave just the line that was typed in the scroll-back
            out.write("\r\n".join(texts) + "\r\n")
            out.flush()
            self._cur_row = 0
            return
        menu = self._menu_lines(width)
        rule = (S.accent if self.mode == "shell" else S.muted)(S.g("hr") * (width - 1))
        foot_text, tone = self._footer_text()
        foot = ("  " + getattr(S, tone, S.muted)(foot_text[:max(1, width - 4)])) if foot_text else ""
        lines = menu + [rule, "\r\n".join(texts), rule] + ([foot] if foot else [])
        out.write("\r\n".join(lines))
        above = len(menu)
        last = above + 1 + total + (1 if foot else 0)          # the row the drawing ends on
        cursor = above + 1 + cur_row
        up = last - cursor
        if up > 0:
            out.write("\x1b[%dA" % up)
        out.write("\r")
        if cur_col:
            out.write("\x1b[%dC" % cur_col)
        self._cur_row = cursor
        out.flush()

    # ---- editing ----
    def _insert(self, text):
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def _word_left(self):
        i = self.pos
        while i > 0 and self.buf[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self):
        i = self.pos
        while i < len(self.buf) and self.buf[i].isspace():
            i += 1
        while i < len(self.buf) and not self.buf[i].isspace():
            i += 1
        return i

    def _line_start(self):
        return self.buf.rfind("\n", 0, self.pos) + 1

    def _line_end(self):
        j = self.buf.find("\n", self.pos)
        return len(self.buf) if j < 0 else j

    def _accept_menu(self):
        kind, items = self._menu
        label, _h, ins, frm = items[self.sel]
        self.buf = self.buf[:frm] + ins + self.buf[self.pos:]
        self.pos = frm + len(ins)
        self.sel = 0
        return label

    def read(self, prompt):
        """One message from the keyboard. Raises EOFError (ctrl+D on an empty line) or KeyboardInterrupt (ctrl+C on an empty line)."""
        fd = self.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self.base_prompt = prompt
        self.buf, self.pos, self.sel, self.hist_i, self._cur_row = "", 0, 0, None, 0
        self._menu = ("", [])
        self._tab_open = False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        self.out.write("\x1b[?2004h")             # bracketed paste
        try:
            tty.setraw(fd)
            self._render(prompt)
            while True:
                if not pending:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        raise EOFError
                    pending = decoder.decode(chunk)
                    if pending == "\x1b":          # a lone Esc: wait a moment to be sure no sequence follows
                        if select.select([fd], [], [], 0.03)[0]:
                            pending += decoder.decode(os.read(fd, 4096))
                key, pending = self._take(pending, fd, decoder)
                if key is None:
                    continue
                done = self._handle(key, prompt)
                if done is not None:
                    return done
                self._update_menu()
                self._render(prompt)
        finally:
            try:
                self._menu = ("", [])
                self._render(prompt, final=True)
            except (OSError, ValueError):
                pass
            self.out.write("\x1b[?2004l")
            self.out.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def _take(self, text, fd, decoder):
        """The next key from TEXT: (key name or ("text", str), rest)."""
        if text.startswith("\x1b[200~"):          # pasted text, up to the closing mark
            end = text.find("\x1b[201~")
            while end < 0:
                if not select.select([fd], [], [], 1.0)[0]:
                    break
                text += decoder.decode(os.read(fd, 65536))
                end = text.find("\x1b[201~")
            body, rest = (text[6:end], text[end + 6:]) if end >= 0 else (text[6:], "")
            return ("text", body.replace("\r\n", "\n").replace("\r", "\n")), rest
        if text.startswith("\x1b"):
            m = CSI_RE.match(text)
            if m:
                arg, final = m.group(1), m.group(3)
                names = {("", "A"): "up", ("", "B"): "down", ("", "C"): "right", ("", "D"): "left", ("", "H"): "home", ("", "F"): "end", ("1", "~"): "home", ("7", "~"): "home",
                         ("4", "~"): "end", ("8", "~"): "end", ("3", "~"): "delete", ("", "Z"): "shifttab", ("1;5", "C"): "wordright", ("1;5", "D"): "wordleft",
                         ("1;3", "C"): "wordright", ("1;3", "D"): "wordleft", ("1;2", "C"): "wordright", ("1;2", "D"): "wordleft"}
                return names.get((arg, final), "ignore"), text[m.end():]
            if len(text) >= 2:
                c = text[1]
                alt = {"b": "wordleft", "f": "wordright", "\x7f": "delword", "\r": "newline", "\n": "newline", "d": "delwordfwd"}
                return alt.get(c, "ignore"), text[2:]
            return "esc", ""
        ch = text[0]
        rest = text[1:]
        if ch.isprintable() or ch == "\t":
            # take the whole run of printable characters (typing fast or pasting without bracketed paste support)
            i = 1
            while i < len(text) and text[i].isprintable():
                i += 1
            return ("text", text[:i]) if ch != "\t" else "tab", text[i:] if ch != "\t" else rest
        codes = {"\r": "enter", "\n": "newline", "\x03": "ctrl-c", "\x04": "ctrl-d", "\x01": "home", "\x05": "end", "\x02": "left", "\x06": "right", "\x0b": "killeol",
                 "\x15": "killbol", "\x17": "delword", "\x7f": "backspace", "\x08": "backspace", "\x0c": "clear", "\x09": "tab"}
        return codes.get(ch, "ignore"), rest

    def _handle(self, key, prompt):
        """Apply one key. Returns the finished text, or None to keep editing."""
        kind, items = self._menu
        if key not in ("up", "down", "tab", "enter"):
            self._tab_open = False
        if isinstance(key, tuple):
            text = key[1]
            if self.mode == "" and self.buf == "" and text.startswith("!"):       # "!" on an empty prompt: shell mode
                self.mode, self.hist_i = "shell", None
                text = text[1:]
            if text:
                self._insert(text)
            self.sel = 0
            self.hist_i = None
            return None
        if key == "enter":
            if items and kind == "path":
                self._accept_menu()
                self._tab_open = False
                return None
            if items and kind == "slash":
                label = items[self.sel][0]
                if self.buf.strip() != label:             # a half-typed command: complete it first
                    self._accept_menu()
                    return None
            elif items and kind == "file":
                self._accept_menu()
                return None
            if self.buf.endswith("\\") and not self.buf.endswith("\\\\"):
                self.buf = self.buf[:-1] + "\n"
                self.pos = len(self.buf)
                return None
            text = self.buf
            if self.mode == "shell":
                self._remember("!" + text)
                return "!" + text if text.strip() else ""
            self._remember(text)
            return text
        if key == "tab":
            if items:
                self._accept_menu()
                self._tab_open = False
            elif self.mode == "shell":
                self._shell_complete()
            return None
        if key == "shifttab":
            return None if self.mode == "shell" else SPECIAL_MODE
        if key == "ctrl-c":
            if self.buf:
                self.buf, self.pos = "", 0
                self.sel = 0
                return None
            if self.mode == "shell":
                self.mode, self.hist_i = "", None
                return None
            raise KeyboardInterrupt
        if key == "ctrl-d":
            if not self.buf:
                if self.mode == "shell":
                    self.mode, self.hist_i = "", None
                    return None
                raise EOFError
            if self.pos < len(self.buf):
                self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
            return None
        if key == "esc":
            self._menu = ("", [])
            self._tab_open = False
            self.sel = 0
            if not items:
                if self.buf:
                    self.buf, self.pos = "", 0
                elif self.mode == "shell":
                    self.mode, self.hist_i = "", None
            return None
        if key in ("up", "down"):
            if items:
                self.sel = (self.sel + (1 if key == "down" else -1)) % len(items)
                return None
            return self._history(key)
        if key == "left":
            self.pos = max(0, self.pos - 1)
        elif key == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif key == "home":
            self.pos = self._line_start()
        elif key == "end":
            self.pos = self._line_end()
        elif key == "wordleft":
            self.pos = self._word_left()
        elif key == "wordright":
            self.pos = self._word_right()
        elif key == "backspace":
            if self.pos:
                self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                self.pos -= 1
            elif self.mode == "shell" and not self.buf:
                self.mode, self.hist_i = "", None
        elif key == "delete":
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif key == "delword":
            i = self._word_left()
            self.buf, self.pos = self.buf[:i] + self.buf[self.pos:], i
        elif key == "delwordfwd":
            j = self._word_right()
            self.buf = self.buf[:self.pos] + self.buf[j:]
        elif key == "killeol":
            self.buf = self.buf[:self.pos] + self.buf[self._line_end():]
        elif key == "killbol":
            i = self._line_start()
            self.buf, self.pos = self.buf[:i] + self.buf[self.pos:], i
        elif key == "newline":
            self._insert("\n")
        elif key == "clear":
            self.out.write("\x1b[2J\x1b[H")
            self._cur_row = 0
        return None

    def _hist_items(self):
        """Shell mode walks back through earlier shell lines; the chat prompt through everything."""
        if self.mode == "shell":
            return [h[1:] for h in self.history if h.startswith("!")]
        return self.history

    def _history(self, key):
        hist = self._hist_items()
        if not hist:
            return None
        if key == "up":
            if self.hist_i is None:
                self.draft = self.buf
                self.hist_i = len(hist)
            if self.hist_i > 0:
                self.hist_i -= 1
        else:
            if self.hist_i is None:
                return None
            self.hist_i += 1
        if self.hist_i is not None and self.hist_i >= len(hist):
            self.hist_i = None
            self.buf = self.draft
        else:
            self.buf = hist[self.hist_i]
        self.pos = len(self.buf)
        return None

    def _shell_complete(self):
        """Tab in shell mode: finish the file name under the cursor, or list the choices."""
        start, tok = self._token_at_cursor()
        if not tok:
            return
        cands = self.files(tok)
        if not cands:
            return
        names = [p + ("/" if d else "") for p, d in cands]
        if len(names) == 1:
            self.buf = self.buf[:start] + names[0] + ("" if names[0].endswith("/") else " ") + self.buf[self.pos:]
            self.pos = start + len(names[0]) + (0 if names[0].endswith("/") else 1)
            return
        common = os.path.commonprefix(names)
        if len(common) > len(tok):
            self.buf = self.buf[:start] + common + self.buf[self.pos:]
            self.pos = start + len(common)
        items = [(n, "folder" if n.endswith("/") else "", n, start) for n in names[:60]]
        self._menu = ("path", items)
        self._tab_open = True
        self.sel = 0


def default_files(prefix):
    """[(path, is_dir)] for what @prefix could mean."""
    base = os.path.expanduser(prefix)
    d, part = os.path.split(base)
    out = []
    try:
        for n in sorted(os.listdir(d or ".")):
            if n.startswith(part) and (part or not n.startswith(".")):
                p = os.path.join(d, n)
                shown = (os.path.join(os.path.split(prefix)[0], n)) if os.path.split(prefix)[0] else n
                out.append((shown, os.path.isdir(p)))
    except OSError:
        pass
    out.sort(key=lambda x: (not x[1], x[0].lower()))
    return out
