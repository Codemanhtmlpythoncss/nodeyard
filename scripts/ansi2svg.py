#!/usr/bin/env python3
"""Render terminal output with ANSI colours as an SVG "terminal window".

Usage: ansi2svg.py TITLE < output.txt > picture.svg

Only what nodeyard prints is supported: SGR codes for bold, dim, the eight
basic colours and reset. Every character is placed on a fixed grid, so the
result looks the same whatever monospace font the viewer has.
"""
import html
import re
import sys

COLORS = {31: "#ff7b72", 32: "#7ee787", 33: "#e3b341", 34: "#79c0ff",
          35: "#d2a8ff", 36: "#56d4dd", 37: "#e6edf3"}
FG, DIM_FG, BG, BAR = "#e6edf3", "#8b949e", "#0d1117", "#161b22"
CHAR_W, LINE_H, FONT = 8.4, 18, 14
PAD_X, PAD_TOP, PAD_BOTTOM = 16, 44, 16
SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Cursor and erase sequences (anything but SGR "m"), and carriage returns.
OTHER_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\r")
MAX_COLS = 100


def parse(line):
    """Yield (text, style) runs for one line."""
    style = {"fg": None, "bold": False, "dim": False}
    pos = 0
    for m in SGR.finditer(line):
        if m.start() > pos:
            yield line[pos:m.start()], dict(style)
        codes = [int(c) for c in m.group(1).split(";") if c] or [0]
        for c in codes:
            if c == 0:
                style = {"fg": None, "bold": False, "dim": False}
            elif c == 1:
                style["bold"] = True
            elif c == 2:
                style["dim"] = True
            elif c in COLORS:
                style["fg"] = c
        pos = m.end()
    if pos < len(line):
        yield line[pos:], dict(style)


def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "nodeyard"
    raw = sys.stdin.read().expandtabs(8)
    lines = [OTHER_ESC.sub("", l) for l in raw.rstrip("\n").split("\n")]
    width_chars = min(max([len(SGR.sub("", l)) for l in lines] + [60]), MAX_COLS)
    width = int(PAD_X * 2 + width_chars * CHAR_W)
    height = PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        f'<rect width="{width}" height="{height}" rx="8" fill="{BG}"/>',
        f'<rect width="{width}" height="30" rx="8" fill="{BAR}"/>',
        f'<rect y="22" width="{width}" height="8" fill="{BAR}"/>',
        '<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/>',
        '<circle cx="36" cy="15" r="5.5" fill="#febc2e"/>',
        '<circle cx="54" cy="15" r="5.5" fill="#28c840"/>',
        f'<text x="{width / 2}" y="19.5" fill="{DIM_FG}" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="12" text-anchor="middle">{html.escape(title)}</text>',
        f'<g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, \'Liberation Mono\', monospace" '
        f'font-size="{FONT}" xml:space="preserve">',
    ]
    for i, line in enumerate(lines):
        y = PAD_TOP + i * LINE_H + FONT
        col = 0
        for text, st in parse(line):
            if not text or col >= MAX_COLS:
                continue
            if col + len(text) > MAX_COLS:
                text = text[: MAX_COLS - col - 1] + "\u2026"
            color = COLORS.get(st["fg"], FG)
            if st["dim"] and st["fg"] is None:
                color = DIM_FG
            attrs = f'fill="{color}"'
            if st["bold"]:
                attrs += ' font-weight="bold"'
            if st["dim"]:
                attrs += ' opacity="0.8"'
            x = PAD_X + col * CHAR_W
            # Non-breaking spaces: renderers may collapse ordinary ones.
            shown = html.escape(text).replace(" ", "&#160;")
            out.append(f'<text x="{x:.1f}" y="{y}" {attrs}>{shown}</text>')
            col += len(text)
    out.append("</g></svg>")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
