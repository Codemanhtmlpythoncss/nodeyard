"""A small terminal emulator: feed it the bytes a program wrote and read back what the screen shows (used by the Terminal tool and the tests).

Handles text with wrapping, cursor movement and addressing, erasing and scrolling in a fixed-size window; colours are ignored.
"""
import re
import unicodedata

CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")


def width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    def __init__(self, cols=80, rows=1000, scroll=False):
        self.cols, self.rows = cols, rows
        self.scroll = scroll       # True: a window of ROWS lines that scrolls (a real terminal); False: keep every line
        self.lines = [[]]      # each line is a list of characters (one per cell; wide chars take two cells with a "" filler)
        self.y = 0
        self.x = 0
        self.pending_wrap = False

    def _line(self, y):
        if self.scroll:
            while y >= self.rows:                 # scroll up
                del self.lines[0]
                y -= 1
                self.y -= 1
        while len(self.lines) <= y:
            self.lines.append([])
        return self.lines[y]

    def put(self, ch):
        w = width(ch)
        if w == 0:
            return
        if self.pending_wrap or self.x + w > self.cols:
            self.y += 1
            self.x = 0
            self.pending_wrap = False
        line = self._line(self.y)
        while len(line) < self.x:
            line.append(" ")
        if len(line) > self.x:
            line[self.x] = ch
        else:
            line.append(ch)
        if w == 2:
            if len(line) > self.x + 1:
                line[self.x + 1] = ""
            else:
                line.append("")
        self.x += w
        if self.x >= self.cols:
            self.x = self.cols
            self.pending_wrap = True

    def feed(self, text):
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                m = CSI.match(text, i)
                if m:
                    self.csi(m.group(1), m.group(3))
                    i = m.end()
                    continue
                i += 2 if text[i + 1:i + 2] in "()]" else 1
                continue
            if ch == "\n":
                self.y += 1
                self.x = 0
                self.pending_wrap = False
            elif ch == "\r":
                self.x = 0
                self.pending_wrap = False
            elif ch == "\b":
                self.x = max(0, self.x - 1)
            elif ch == "\x07" or ch == "\x00" or ch == "\x01" or ch == "\x02":
                pass
            elif ch == "\t":
                self.x = (self.x // 8 + 1) * 8
            else:
                self.put(ch)
            i += 1

    def csi(self, args, final):
        n = [int(a) if a.isdigit() else 0 for a in args.replace("?", "").split(";")] if args else []
        a0 = n[0] if n else 0
        if final == "A":
            self.y = max(0, self.y - (a0 or 1))
            self.pending_wrap = False
        elif final == "B":
            self.y += a0 or 1
        elif final == "C":
            self.x = min(self.cols - 1, self.x + (a0 or 1))
        elif final == "D":
            self.x = max(0, self.x - (a0 or 1))
        elif final == "K":
            line = self._line(self.y)
            if a0 in (0,):
                del line[self.x:]
            elif a0 == 2:
                line.clear()
            elif a0 == 1:
                for k in range(min(self.x + 1, len(line))):
                    line[k] = " "
            self.pending_wrap = False
        elif final == "J":
            if a0 == 2 or a0 == 3:
                self.lines, self.y, self.x = [[]], (self.y if not self.scroll else 0), (self.x if not self.scroll else 0)
            elif a0 == 1:
                for k in range(self.y):
                    if k < len(self.lines):
                        self.lines[k] = []
            else:
                line = self._line(self.y)
                del line[self.x:]
                del self.lines[self.y + 1:]
        elif final == "G":
            self.x = max(0, (a0 or 1) - 1)
        elif final in ("H", "f"):
            self.y = max(0, (n[0] if n and n[0] else 1) - 1)
            self.x = max(0, (n[1] if len(n) > 1 and n[1] else 1) - 1)
            self.pending_wrap = False
        elif final == "d":
            self.y = max(0, (a0 or 1) - 1)
        elif final == "P":                          # delete characters
            line = self._line(self.y)
            del line[self.x:self.x + (a0 or 1)]
        elif final in ("h", "l") and "1049" in args:  # the alternate screen: start clean
            self.lines, self.y, self.x = [[]], 0, 0
        # colours (m), modes (h/l) and the rest do nothing here

    def text(self):
        return "\n".join("".join(l).rstrip() for l in self.lines).rstrip("\n")


def render(data, cols=80):
    s = Screen(cols)
    s.feed(data if isinstance(data, str) else data.decode("utf-8", "replace"))
    return s.text()
