#!/usr/bin/env python3
"""Dev tool: pipe ANSI terminal output in, get a GitHub-renderable SVG out.

    python3 hero_board.py --demo --once | python3 tools/ansi2svg.py > preview/board.svg

Understands SGR: 0 reset · 1 bold · 2 dim · 7 invert · 38/48;2;r;g;b · 38/48;5;n.
Column positions are computed with the same width rules the renderers use, so
the SVG shows the same alignment a spec-compliant terminal would.
Python ≥3.9 stdlib only.
"""

import re
import sys
import unicodedata

CW, CH, FS = 8.4, 19.0, 14          # cell width/height px, font size
PAD = 14
BG = "#0d1117"
FG_DEFAULT = "#c9d1d9"

ANSI = re.compile(r"\x1b\[([0-9;:]*)m")

CUBE = [0, 95, 135, 175, 215, 255]


def color256(n):
    if n < 16:
        base = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc",
                "#11a8cd", "#e5e5e5", "#666666", "#f14c4c", "#23d18b", "#f5f543",
                "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
        return base[n]
    if n < 232:
        n -= 16
        r, g, b = CUBE[n // 36], CUBE[(n % 36) // 6], CUBE[n % 6]
        return "#%02x%02x%02x" % (r, g, b)
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def ch_width(ch):
    o = ord(ch)
    if o == 0xFE0F or unicodedata.combining(ch):
        return 0
    for lo, hi in ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF)):
        if lo <= o <= hi:
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class State:
    def __init__(self):
        self.fg = None
        self.bg = None
        self.bold = False
        self.dim = False
        self.invert = False

    def apply(self, params):
        codes = [int(x) if x else 0 for x in params.split(";")] if params else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.__init__()
            elif c == 1:
                self.bold = True
            elif c == 2:
                self.dim = True
            elif c == 7:
                self.invert = True
            elif c in (38, 48) and i + 1 < len(codes):
                if codes[i + 1] == 2 and i + 4 < len(codes):
                    col = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    if c == 38:
                        self.fg = col
                    else:
                        self.bg = col
                    i += 4
                elif codes[i + 1] == 5 and i + 2 < len(codes):
                    col = color256(codes[i + 2])
                    if c == 38:
                        self.fg = col
                    else:
                        self.bg = col
                    i += 2
            i += 1


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    text = sys.stdin.read()
    lines = text.split("\n")
    while lines and not lines[-1].strip("\x1b[0m \t"):
        lines.pop()

    rects, texts = [], []
    max_col = 0
    for row, line in enumerate(lines):
        st = State()
        col = 0
        parts = ANSI.split(line)
        # split -> [text, params, text, params, ...]
        for i, part in enumerate(parts):
            if i % 2 == 1:
                st.apply(part)
                continue
            if not part:
                continue
            run_fg = st.fg or FG_DEFAULT
            run_bg = st.bg
            if st.invert:
                run_fg, run_bg = (run_bg or BG), (st.fg or FG_DEFAULT)
            w = sum(ch_width(c) for c in part)
            x = PAD + col * CW
            y = PAD + row * CH
            if run_bg:
                rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                             % (x, y, w * CW, CH, run_bg))
            style = ""
            if st.bold:
                style += "font-weight:bold;"
            opacity = ' opacity="0.55"' if st.dim else ""
            if part.strip():
                texts.append('<text x="%.1f" y="%.1f" fill="%s"%s style="%s" '
                             'textLength="%.1f" lengthAdjust="spacingAndGlyphs" '
                             'xml:space="preserve">%s</text>'
                             % (x, y + CH - 5, run_fg, opacity, style,
                                w * CW, esc(part)))
            col += w
        max_col = max(max_col, col)

    W = PAD * 2 + max_col * CW
    H = PAD * 2 + len(lines) * CH
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
           'font-family="Menlo,Consolas,DejaVu Sans Mono,monospace" font-size="%d">' % (W, H, FS),
           '<rect width="100%%" height="100%%" fill="%s" rx="8"/>' % BG]
    out += rects + texts + ["</svg>"]
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
