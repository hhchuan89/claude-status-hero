#!/usr/bin/env python3
"""claude-status-hero · hero_pixel.py — the pixel office.

A rich pixel-art office floor plan for the whole fleet, blitted into
iTerm2 via a hand-rolled sixel encoder. This is the fancy sibling of
hero_board.py's stdlib TUI office mode — same data, same zero-dependency
promise, richer picture. See docs/DESIGN.md and the commissioning task for
the full design rationale (LOCKED SPEC): walled zones, teleport model (no
walking/animation — position = f(state)), a manager room queue sorted
longest-wait-first with long-wait escalation, a HUD power panel with
time-to-limit gauges, and a live system clock.

Everything here is 100% stdlib, zero bundled assets:
  - heroes are the SAME 10x8 sprite grids hero_board.py already draws
    (SPRITES dict), reused via the same importlib pattern hero_line.py
    uses to load hero_board.py from disk;
  - furniture/status props are ~10 hand-authored in-palette pixel icons
    (PROPS below);
  - text uses a self-authored 5x7 bitmap font (FONT5X7), uppercase +
    digits + the punctuation this design actually uses — no font files,
    no emoji, no CC-BY notices to carry in an MIT repo;
  - the framebuffer is a bytearray of palette indices (0..N-1, N<=256),
    encoded to PNG (truecolor, stdlib zlib) for the integrator to eyeball
    and to sixel (hand-rolled DCS encoder) for iTerm2.

No animation loop: this module draws ONE static frame from a state
snapshot, event-driven (see run()). Sessions TELEPORT between rooms by
state; there is no walking.

Usage:
  python3 hero_pixel.py --demo --once --png /path/to/out.png
  python3 hero_pixel.py --once                  # real fleet, one frame
  python3 hero_pixel.py                         # event-driven loop (iTerm2)
  python3 hero_pixel.py --scale N               # force NxN size (else auto-fit)
  STATUS_HERO_SIXEL=1|0                          # force capability on/off

The office is a fixed 1040x600 bitmap; on a big/Retina window that looks
small, so by default it is integer-upscaled (nearest-neighbor, stays crisp)
to fill the terminal's reported pixel size. --scale N overrides the auto-fit.

Normally invoked via `hero_board.py --pixel`, which detects sixel support
and falls back to the existing TUI office mode when unavailable.

Python >=3.9, stdlib only. MIT.
"""

import itertools
import math
import os
import re
import select
import struct
import sys
import time
import zlib

# --------------------------------------------------------------- constants

MANAGER_LABEL = "YOU"   # fallback desk marker when the OS gives us no user name


def _user_display_name():
    """Who the fleet is waiting on — the manager desk marker. Prefer the OS
    account's real name (macOS GECOS), then the login name, then MANAGER_LABEL.
    Uppercased; the full name if it fits the narrow plate (<=12 chars), else
    just the first token. Never raises (a monitor must not die because a name
    lookup hiccuped)."""
    name = ""
    try:
        import pwd
        name = (pwd.getpwuid(os.getuid()).pw_gecos or "").split(",")[0].strip()
    except Exception:
        name = ""
    if not name:
        try:
            import getpass
            name = getpass.getuser() or ""
        except Exception:
            name = ""
    full = " ".join(name.split()).upper()    # collapse internal whitespace
    if not full:
        return MANAGER_LABEL
    if len(full) <= 12:
        return full                           # a short full name fits the plate
    return full.split()[0][:12]               # long name -> first token only

ESCALATE_AMBER_MIN = 15   # long-wait escalation tier 1 (amber)
ESCALATE_ALARM_MIN = 30   # long-wait escalation tier 2 (vermillion + "!!")

HERE = os.path.dirname(os.path.abspath(__file__))

STATE_DIR = os.environ.get("STATUS_HERO_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "status-hero"
)
SESS_DIR = os.path.join(STATE_DIR, "sessions")


# ------------------------------------------------------------- hero_board
# Loaded from disk by path (not `import hero_board`) so this module never
# creates a circular import with hero_board.py's own --pixel branch, and so
# it inherits fleet()/effective_state()/sanitize()/num()/SPRITES verbatim —
# tombstone burial, corpse cleanup, hostile-file hardening, ghost decay.

_hb_mod = None


def _board():
    global _hb_mod
    if _hb_mod is None:
        import importlib.util
        p = os.path.join(HERE, "hero_board.py")
        spec = importlib.util.spec_from_file_location("status_hero_board_px", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _hb_mod = m
    return _hb_mod


_hb = _board()
SPRITES = _hb.SPRITES

# ------------------------------------------------------------------ palette
# ~19 flat UI tokens, no gradients. Hex comments are the source of truth.
# Wong colorblind-safe state colors: one alarm channel only (vermillion),
# for needs_you/error — never any other red anywhere in the scene.
PAL = {
    "bg_void":      (18, 20, 28),     # #12141C
    "hud_plate":    (26, 22, 32),     # #1A1620 — HUD band fill + every text plate
    "hud_border":   (58, 54, 74),     # #3A364A
    "wall_face":    (58, 54, 74),     # #3A364A
    "wall_shadow":  (44, 41, 58),     # #2C2939
    "floor_base":   (107, 93, 79),    # #6B5D4F
    "floor_alt":    (94, 82, 69),     # #5E5245
    "rug_pantry":   (78, 106, 97),    # #4E6A61 — decorative only, never status
    "wood":         (138, 98, 66),    # #8A6242
    "wood_dark":    (95, 69, 48),     # #5F4530 — also the mandatory desk outline
    "ink":          (242, 236, 224),  # #F2ECE0 — 15.1:1 on hud_plate
    "ink_dim":      (168, 162, 176),  # #A8A2B0 — 7.2:1 on hud_plate
    "ink_inverse":  (26, 22, 32),     # text on accent (alarm/warn) plates
    "working":      (79, 168, 224),   # #4FA8E0 blue
    "alarm":        (232, 85, 43),    # #E8552B vermillion — needs_you/error, ONLY red
    "idle":         (138, 132, 120),  # #8A8478 warm-gray
    "compacting":   (176, 114, 208),  # #B072D0 purple
    "ghost":        (106, 116, 132),  # #6A7484 blue-gray
    "warn":         (230, 159, 0),    # #E69F00 amber — gauge warnings + escalation tier 1
}
PAL_ORDER = ["bg_void", "hud_plate", "hud_border", "wall_face", "wall_shadow",
             "floor_base", "floor_alt", "rug_pantry", "wood", "wood_dark",
             "ink", "ink_dim", "ink_inverse", "working", "alarm", "idle",
             "compacting", "ghost", "warn"]

STATE_COLOR = {"working": "working", "needs_you": "alarm", "error": "alarm",
               "idle": "idle", "compacting": "compacting", "ghost": "ghost"}


def _mix(a, b, t):
    return tuple(int(round(x * (1 - t) + y * t)) for x, y in zip(a, b))


PALETTE_RGB = []      # index -> (r,g,b), the sixel/PNG palette (<=256 entries)
PALETTE_IDX = {}       # UI token name -> index
for _name in PAL_ORDER:
    PALETTE_IDX[_name] = len(PALETTE_RGB)
    PALETTE_RGB.append(PAL[_name])
PALETTE_IDX["dim_wood"] = len(PALETTE_RGB)          # never-occupied desk slot
PALETTE_RGB.append(_mix(PAL["wood"], PAL["floor_base"], 0.5))

HERO_IDX = {}         # (hero_name, sprite_char) -> palette index
HERO_GHOST_IDX = {}   # same, dimmed+desaturated (mirrors hero_board.sprite_cells)
for _hero, _sp in SPRITES.items():
    for _ch, _rgb in _sp["pal"].items():
        HERO_IDX[(_hero, _ch)] = len(PALETTE_RGB)
        PALETTE_RGB.append(_rgb)
        _dim = tuple(int(c * 0.45 + 25) for c in _rgb)
        _gray = int(sum(_dim) / 3)
        HERO_GHOST_IDX[(_hero, _ch)] = len(PALETTE_RGB)
        PALETTE_RGB.append((_gray, _gray, _gray))

assert len(PALETTE_RGB) <= 256, \
    "sixel register overflow: %d colors" % len(PALETTE_RGB)


def safe_hero(hero):
    """A session's `hero` field, hardened for hostile files: hero_board's
    fleet() already forces non-str heroes to None, but this module reads
    raw session dicts directly in a couple of code paths (tests, future
    callers), and dict membership on an unhashable value (e.g. a hostile
    file's hero: ["cat"]) raises TypeError before we ever get to draw
    anything — so every hero lookup in this file goes through here first."""
    return hero if isinstance(hero, str) and hero in SPRITES else "fox"


def _hero_pal_map(hero, ghost=False):
    hero = safe_hero(hero)
    sp = SPRITES[hero]
    table = HERO_GHOST_IDX if ghost else HERO_IDX
    return {ch: table[(hero, ch)] for ch in sp["pal"]}


# ------------------------------------------------------- contrast audit ----
def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(c1, c2):
    l1, l2 = _lum(c1), _lum(c2)
    l1, l2 = max(l1, l2), min(l1, l2)
    return (l1 + 0.05) / (l2 + 0.05)


def audit_contrast(verbose=False):
    """Every text/mark pair this file actually draws, checked against the
    WCAG floor the design claims. Raises if one regresses. Pure function
    over the RGB table (no drawing) so tests can call it directly."""
    checks = [
        ("ink on hud_plate (primary text)", PAL["ink"], PAL["hud_plate"], 7.0),
        ("ink_dim on hud_plate (secondary text)", PAL["ink_dim"], PAL["hud_plate"], 4.5),
        ("ink_inverse on alarm (front wait chip)", PAL["ink_inverse"], PAL["alarm"], 4.5),
        ("ink_inverse on warn (gauge danger text)", PAL["ink_inverse"], PAL["warn"], 4.5),
        ("working tick on hud_plate", PAL["working"], PAL["hud_plate"], 3.0),
        ("alarm tick on hud_plate", PAL["alarm"], PAL["hud_plate"], 3.0),
        ("idle tick on hud_plate", PAL["idle"], PAL["hud_plate"], 3.0),
        ("compacting tick on hud_plate", PAL["compacting"], PAL["hud_plate"], 3.0),
        ("ghost tick on hud_plate", PAL["ghost"], PAL["hud_plate"], 3.0),
        ("wood_dark outline on floor_base", PAL["wood_dark"], PAL["floor_base"], 1.1),
    ]
    ok = True
    for name, c1, c2, floor in checks:
        ratio = _contrast(c1, c2)
        passed = ratio >= floor
        ok = ok and passed
        if verbose:
            print("[contrast] %-42s %5.2f:1  (floor %.1f)  %s" %
                  (name, ratio, floor, "ok" if passed else "FAIL"))
        assert passed, "contrast regression: %s = %.2f:1 < %.1f" % (name, ratio, floor)
    return ok


# ------------------------------------------------------------- 5x7 font ----
# Self-authored, uppercase + digits + the punctuation this design uses:
# ! $ % · . : / + ~ # - … space. '#'=lit '.'=blank, 5 cols x 7 rows.
FONT5X7 = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".####", "#....", "#....", "#....", "#....", "#....", ".####"],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".####", "#....", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "X": ["#...#", ".#.#.", "..#..", "..#..", "..#..", ".#.#.", "#...#"],
    "Y": ["#...#", ".#.#.", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": [".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    "!": ["..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."],
    "$": ["..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."],
    "%": ["#...#", "#..#.", "...#.", "..#..", ".#...", ".#..#", "#...#"],
    "·": [".....", ".....", ".....", "..#..", ".....", ".....", "....."],
    ".": [".....", ".....", ".....", ".....", ".....", ".....", "..#.."],
    ":": [".....", "..#..", ".....", ".....", "..#..", ".....", "....."],
    "/": ["....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    "~": [".....", ".....", ".#...", "#.#.#", "...#.", ".....", "....."],
    "#": [".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    "…": [".....", ".....", ".....", ".....", ".....", ".....", "#.#.#"],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
}
for _ch, _rows in FONT5X7.items():
    assert len(_rows) == 7 and all(len(r) == 5 for r in _rows), \
        "FONT5X7[%r] must be 5x7" % _ch


def measure_text(text, scale=1):
    text = text.upper()
    n = len(text)
    if n == 0:
        return 0, 7 * scale
    return (n * 6 - 1) * scale, 7 * scale


def draw_text(fb, x, y, text, color, scale=1):
    """Bare text — NEVER call this directly in scene code; use text_plate/
    icon_chip so text always sits on a solid plate (the legibility rule)."""
    cx = int(x)
    y = int(y)
    for ch in text.upper():
        glyph = FONT5X7.get(ch, FONT5X7[" "])
        for r, row in enumerate(glyph):
            for c, bit in enumerate(row):
                if bit == "#":
                    fb.fill_rect(cx + c * scale, y + r * scale,
                                 cx + c * scale + scale, y + r * scale + scale, color)
        cx += 6 * scale
    return cx


# ---------------------------------------------------------------- geometry
W, H = 1040, 600
OW = 16   # outer wall thickness
IW = 8    # interior wall thickness
ZONES = {
    "hud": (0, 0, 1040, 64),
    "work": (16, 64, 728, 460),          # OFFICE floor (left, the big room)
    "reception": (736, 64, 1024, 460),   # MANAGER ROOM (right)
    "pantry": (16, 468, 1024, 584),      # pantry (bottom)
}
VWALL = dict(x0=728, x1=728 + IW, y0=64, y1=460, gap=(210, 272))    # office | manager
HWALL = dict(x0=16, x1=1024, y0=460, y1=460 + IW, gap=(488, 552))   # rooms | pantry

SLOT_W, SLOT_H = 178, 148
SLOT_COLS = 4
SLOT_ROWS = [98, 268]
assert SLOT_ROWS[-1] + SLOT_H <= ZONES["work"][3], "desk grid overflows the work floor"
assert ZONES["work"][0] + SLOT_COLS * SLOT_W == ZONES["work"][2], "desk grid overflows the work floor"


def slot_xy(i):
    row, col = divmod(i, SLOT_COLS)
    return ZONES["work"][0] + col * SLOT_W, SLOT_ROWS[row]


# --------------------------------------------------------------- gauge/time
def gauge_calc(pct, reset_epoch, window_sec, now):
    pct = 0.0 if pct is None else pct
    remaining = max(reset_epoch - now, 1.0)
    elapsed = max(window_sec - remaining, 60.0)
    pace = pct / elapsed
    eta = (100.0 - pct) / pace if pace > 0 else float("inf")
    if eta >= remaining:
        level = 0
    elif eta < 1800:
        level = 2
    else:
        level = 1
    return eta, remaining, level


def fmt_dur(sec):
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return "%dH%02dM" % (h, m) if h > 0 else "%dM" % m


def fmt_clock(epoch, with_day=False):
    lt = time.localtime(epoch)
    return time.strftime("%a %H:%M" if with_day else "%H:%M", lt).upper()


# ------------------------------------------------------------- framebuffer
class Framebuffer:
    """A W*H bytearray of palette indices — no alpha, no gradients. Text/AA
    stay OFF by construction: every primitive writes a solid index."""

    def __init__(self, w, h, bg=0):
        self.w = w
        self.h = h
        self.buf = bytearray([bg]) * (w * h)

    def set(self, x, y, c):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.w + x] = c

    def get(self, x, y):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.buf[y * self.w + x]
        return None

    def fill_rect(self, x0, y0, x1, y1, c):
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes([c]) * (x1 - x0)
        w = self.w
        buf = self.buf
        for y in range(y0, y1):
            base = y * w
            buf[base + x0:base + x1] = row

    def outline_rect(self, x0, y0, x1, y1, c, width=1):
        self.fill_rect(x0, y0, x1, y0 + width, c)
        self.fill_rect(x0, y1 - width, x1, y1, c)
        self.fill_rect(x0, y0, x0 + width, y1, c)
        self.fill_rect(x1 - width, y0, x1, y1, c)

    def rounded_rect(self, x0, y0, x1, y1, radius, color):
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
        if r == 0:
            self.fill_rect(x0, y0, x1, y1, color)
            return
        self.fill_rect(x0, y0 + r, x1, y1 - r, color)
        self.fill_rect(x0 + r, y0, x1 - r, y0 + r, color)
        self.fill_rect(x0 + r, y1 - r, x1 - r, y1, color)
        r2 = r * r
        for cx, cy in ((x0 + r, y0 + r), (x1 - r - 1, y0 + r),
                       (x0 + r, y1 - r - 1), (x1 - r - 1, y1 - r - 1)):
            for yy in range(max(0, cy - r), min(self.h, cy + r + 1)):
                dy = yy - cy
                for xx in range(max(0, cx - r), min(self.w, cx + r + 1)):
                    dx = xx - cx
                    if dx * dx + dy * dy <= r2:
                        self.set(xx, yy, color)

    def dashed_rect(self, x0, y0, x1, y1, color, dash=4, gap=3):
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        for yy in (y0, y1 - 1):
            x = x0
            while x < x1:
                self.fill_rect(x, yy, min(x + dash, x1), yy + 1, color)
                x += dash + gap
        for xx in (x0, x1 - 1):
            y = y0
            while y < y1:
                self.fill_rect(xx, y, xx + 1, min(y + dash, y1), color)
                y += dash + gap

    def line(self, x0, y0, x1, y1, color, width=1):
        x0, y0 = int(round(x0)), int(round(y0))
        x1, y1 = int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        x, y = x0, y0
        hw = width // 2
        while True:
            if width <= 1:
                self.set(x, y, color)
            else:
                self.fill_rect(x - hw, y - hw, x - hw + width, y - hw + width, color)
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def circle(self, cx, cy, r, color, fill=True, width=1):
        cx, cy, r = int(cx), int(cy), int(r)
        if fill:
            r2 = r * r
            for yy in range(cy - r, cy + r + 1):
                dy = yy - cy
                for xx in range(cx - r, cx + r + 1):
                    dx = xx - cx
                    if dx * dx + dy * dy <= r2:
                        self.set(xx, yy, color)
        else:
            outer2, inner2 = r * r, max(0, r - width) ** 2
            for yy in range(cy - r, cy + r + 1):
                dy = yy - cy
                for xx in range(cx - r, cx + r + 1):
                    dx = xx - cx
                    d2 = dx * dx + dy * dy
                    if inner2 <= d2 <= outer2:
                        self.set(xx, yy, color)

    def blit_sprite(self, x, y, px_rows, pal_map, scale=1):
        x, y = int(x), int(y)
        for r, row in enumerate(px_rows):
            for c, ch in enumerate(row):
                if ch == ".":
                    continue
                color = pal_map.get(ch)
                if color is None:
                    continue
                self.fill_rect(x + c * scale, y + r * scale,
                               x + c * scale + scale, y + r * scale + scale, color)


# -------------------------------------------------------------- text plates
# THE RULE: text never touches the scene bare. Every text/tick/prop call in
# this file goes through icon_chip (directly or via text_plate/room_sign) —
# it always paints a solid plate first.
def icon_chip(fb, x, y, parts, *, ink, plate, anchor="lt", pad=(6, 4),
              scale=1, gap=4, dashed=False, radius=3):
    """parts: list of ('text', s) | ('tick', color, w) | ('prop', name, scale).
    Measures the row, paints one solid plate, draws parts left-to-right.
    Returns the plate rect (x0,y0,x1,y1)."""
    padx, pady = pad
    widths, height = [], 0
    for part in parts:
        kind = part[0]
        if kind == "text":
            w, h = measure_text(part[1], scale)
            widths.append(w)
            height = max(height, h)
        elif kind == "tick":
            widths.append(part[2])
            height = max(height, 6 * scale)
        elif kind == "prop":
            pw, ph = prop_size(part[1], part[2])
            widths.append(pw)
            height = max(height, ph)
        else:
            widths.append(0)
    n = len(parts)
    content_w = sum(widths) + gap * max(n - 1, 0)
    if anchor == "rt":
        px0, py0 = x - (content_w + 2 * padx), y
    elif anchor == "mt":
        px0, py0 = x - (content_w + 2 * padx) // 2, y
    elif anchor == "mm":
        px0, py0 = x - (content_w + 2 * padx) // 2, y - (height + 2 * pady) // 2
    else:
        px0, py0 = x, y
    px0, py0 = int(px0), int(py0)
    px1, py1 = px0 + content_w + 2 * padx, py0 + height + 2 * pady
    fb.rounded_rect(px0, py0, px1, py1, radius, plate)
    if dashed:
        fb.dashed_rect(px0, py0, px1, py1, PALETTE_IDX["ghost"])
    cx = px0 + padx
    cy_mid = (py0 + py1) // 2
    for part, w in zip(parts, widths):
        kind = part[0]
        if kind == "text":
            ty = cy_mid - (measure_text(part[1], scale)[1]) // 2
            draw_text(fb, cx, ty, part[1], ink, scale)
        elif kind == "tick":
            _, color, tw = part
            fb.rounded_rect(cx, py0 + 3, cx + tw, py1 - 3, 2, color)
        elif kind == "prop":
            _, name, sc = part
            ph = prop_size(name, sc)[1]
            blit_prop(fb, cx, cy_mid - ph // 2, name, sc)
        cx += w + gap
    return (px0, py0, px1, py1)


def text_plate(fb, x, y, s, *, ink=None, plate=None, anchor="lt", pad=(6, 4),
               scale=1, dashed=False):
    ink = PALETTE_IDX["ink"] if ink is None else ink
    plate = PALETTE_IDX["hud_plate"] if plate is None else plate
    return icon_chip(fb, x, y, [("text", s)], ink=ink, plate=plate,
                      anchor=anchor, pad=pad, scale=scale, dashed=dashed)


def room_sign(fb, x, y, name, anchor="mt"):
    return icon_chip(fb, x, y, [("tick", PALETTE_IDX["hud_border"], 5), ("text", name)],
                      ink=PALETTE_IDX["ink"], plate=PALETTE_IDX["hud_plate"],
                      anchor=anchor, pad=(9, 5), scale=1)


# --------------------------------------------------------------- HUD parts
def draw_gauge(fb, x, y, label, pct, reset_epoch, window_sec, now, with_day):
    pct = 0.0 if pct is None else pct
    reset_epoch = (now + window_sec) if reset_epoch is None else reset_epoch
    eta, remaining, level = gauge_calc(pct, reset_epoch, window_sec, now)
    col = (PALETTE_IDX["ink"] if level == 0
           else (PALETTE_IDX["warn"] if level == 1 else PALETTE_IDX["alarm"]))
    l1 = "%s %3.0f%%" % (label, pct)
    r1 = text_plate(fb, x, y, l1, ink=col, pad=(4, 2))
    bar_x, bar_y, bar_w, bar_h = x, r1[3] + 3, 120, 10
    fb.fill_rect(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h, PALETTE_IDX["wall_shadow"])
    fill_w = int(bar_w * min(pct, 100) / 100)
    fb.fill_rect(bar_x, bar_y, bar_x + fill_w, bar_y + bar_h, PALETTE_IDX["ink"])
    if level > 0 and fill_w >= 3:
        fb.fill_rect(bar_x + fill_w - 3, bar_y, bar_x + fill_w, bar_y + bar_h, col)
    proj_y = bar_y + bar_h + 3
    if level == 0:
        proj = "RESETS %s SAFE" % fmt_clock(reset_epoch, with_day)
        proj_ink = PALETTE_IDX["ink_dim"]
    else:
        proj = "~%s LEFT" % fmt_dur(eta)
        proj_ink = col
    r2 = text_plate(fb, x, proj_y, proj, ink=proj_ink, pad=(4, 1))
    return (x, y, max(r1[2], bar_x + bar_w, r2[2]), r2[3])


def draw_clock(fb, x, y, now):
    r = 11
    cxc, cyc = x + r, y + r
    fb.circle(cxc, cyc, r, PALETTE_IDX["hud_plate"], fill=True)
    fb.circle(cxc, cyc, r, PALETTE_IDX["ink_dim"], fill=False, width=2)
    lt = time.localtime(now)
    mm = lt.tm_min
    hh = (lt.tm_hour % 12) + lt.tm_min / 60.0
    ma, ha = math.radians(mm * 6 - 90), math.radians(hh * 30 - 90)
    fb.line(cxc, cyc, cxc + math.cos(ma) * (r - 3), cyc + math.sin(ma) * (r - 3),
            PALETTE_IDX["ink"], width=1)
    fb.line(cxc, cyc, cxc + math.cos(ha) * (r - 6), cyc + math.sin(ha) * (r - 6),
            PALETTE_IDX["ink"], width=2)
    txt = time.strftime("%a %H:%M", lt).upper()
    return text_plate(fb, x + 2 * r + 8, y, txt, ink=PALETTE_IDX["ink_dim"], pad=(4, 2))


# ------------------------------------------------------------------ props
# Hand-authored, in-palette (reuse UI tokens, no new colors): door, laptop,
# chair, bell, printer, plant, couch, coffee, water_cooler, paper_stack.
PROPS = {
    "door": {"pal": {"F": "wood_dark", "o": "warn"},
             "px": ["FFFFF", "F...F", "F...F", "F..oF", "F...F", "FFFFF"]},
    "laptop": {"pal": {"F": "ink_dim", "o": "working"},
               "px": [".FFFFF.", ".FoooF.", ".FFFFF.", "FFFFFFF"]},
    "chair": {"pal": {"F": "wood_dark"},
              "px": ["FF.FF", "FFFFF", "F...F", "F...F"]},
    "bell": {"pal": {"F": "warn"},
             "px": [".FFF.", "FFFFF", "FFFFF", "..F.."]},
    "printer": {"pal": {"F": "wall_shadow"},
                "px": ["FFFFFFF", "F.....F", "FFFFFFF", ".FF.FF."]},
    "plant": {"pal": {"F": "rug_pantry", "o": "wood_dark"},
              "px": ["..F..", ".FFF.", "FFFFF", "..o..", ".ooo."]},
    "couch": {"pal": {"F": "rug_pantry", "o": "wood_dark"},
              "px": ["FFFFFFFF", "F......F", "FFFFFFFF", "oo....oo"]},
    "coffee": {"pal": {"F": "wood"},
               "px": [".FFF.", "F...F", "F...F", ".FFF."]},
    "water_cooler": {"pal": {"F": "working", "o": "wall_face"},
                      "px": [".FFF.", "FFFFF", "F...F", "F...F", "ooooo"]},
    "paper_stack": {"pal": {"F": "ink_dim"},
                    "px": ["FFFF", "F..F", "FFFF", "F..F", "FFFF"]},
}


def prop_size(name, scale):
    px = PROPS[name]["px"]
    return len(px[0]) * scale, len(px) * scale


def blit_prop(fb, x, y, name, scale):
    grid = PROPS[name]
    pal_map = {ch: PALETTE_IDX[key] for ch, key in grid["pal"].items()}
    fb.blit_sprite(x, y, grid["px"], pal_map, scale)


# ------------------------------------------------------------------ scene
def floor_room(fb, rect):
    x0, y0, x1, y1 = rect
    fb.fill_rect(x0, y0, x1, y1, PALETTE_IDX["floor_base"])
    cell = 40
    for ty in range(y0, y1, cell):
        for tx in range(x0, x1, cell):
            if ((tx // cell) + (ty // cell)) % 2:
                fb.fill_rect(tx, ty, min(tx + cell, x1), min(ty + cell, y1),
                             PALETTE_IDX["floor_alt"])


def draw_walls(fb, queue_nonempty):
    wf, ws = PALETTE_IDX["wall_face"], PALETTE_IDX["wall_shadow"]
    fb.fill_rect(0, 64, OW, H, wf)
    fb.fill_rect(OW - 2, 64, OW, H, ws)
    door_y0, door_y1 = 192, 252
    fb.fill_rect(0, door_y0, OW, door_y1, ws)
    dw, dh = prop_size("door", 3)
    blit_prop(fb, 1, door_y0 + max(0, (door_y1 - door_y0 - dh) // 2), "door", 3)
    fb.fill_rect(W - OW, 64, W, H, wf)
    fb.fill_rect(W - OW, 64, W - OW + 2, H, ws)
    fb.fill_rect(0, H - OW, W, H, wf)
    fb.fill_rect(0, H - OW, W, H - OW + 2, ws)
    vx0, vx1, vy0, vy1 = VWALL["x0"], VWALL["x1"], VWALL["y0"], VWALL["y1"]
    gy0, gy1 = VWALL["gap"]
    fb.fill_rect(vx0, vy0, vx1, gy0, wf)
    fb.fill_rect(vx0, gy1, vx1, vy1, wf)
    hx0, hx1, hy0, hy1 = HWALL["x0"], HWALL["x1"], HWALL["y0"], HWALL["y1"]
    gx0, gx1 = HWALL["gap"]
    fb.fill_rect(hx0, hy0, gx0, hy1, wf)
    fb.fill_rect(gx1, hy0, hx1, hy1, wf)
    fb.fill_rect(hx0, hy1 - 2, gx0, hy1, ws)
    fb.fill_rect(gx1, hy1 - 2, hx1, hy1, ws)
    if queue_nonempty:
        mx0, _, mx1, _ = ZONES["reception"]
        fb.fill_rect(mx0, 64, mx1, 68, PALETTE_IDX["alarm"])


def draw_desk(fb, slot_x, slot_y, s):
    cx = slot_x + SLOT_W // 2
    if s is None:
        fb.rounded_rect(slot_x + 21, slot_y + 88, slot_x + 161, slot_y + 116, 4,
                         PALETTE_IDX["dim_wood"])
        return
    state = s.get("_state") if isinstance(s.get("_state"), str) else "working"
    hero = safe_hero(s.get("hero"))
    tick_idx = PALETTE_IDX.get(STATE_COLOR.get(state, "working"), PALETTE_IDX["working"])
    fb.rounded_rect(slot_x + 21, slot_y + 88, slot_x + 161, slot_y + 116, 4, PALETTE_IDX["wood"])
    fb.outline_rect(slot_x + 21, slot_y + 88, slot_x + 161, slot_y + 116, PALETTE_IDX["wood_dark"], 2)
    blit_prop(fb, cx - 10, slot_y + 58, "laptop", 3)
    if state in ("working", "compacting"):
        sp = SPRITES[hero]
        fb.blit_sprite(cx - 25, slot_y + 8, sp["px"], _hero_pal_map(hero), scale=5)
    elif state == "ghost":
        sp = SPRITES[hero]
        fb.blit_sprite(cx - 25, slot_y + 8, sp["px"], _hero_pal_map(hero, ghost=True), scale=5)
    else:   # needs_you/idle: occupant teleported to manager room/pantry
        blit_prop(fb, cx - 12, slot_y + 16, "chair", 4)
    ctx = _hb.num(s.get("ctx"))
    if ctx is not None and ctx >= 85:
        blit_prop(fb, slot_x + 148, slot_y + 78, "paper_stack", 3)
    name = _crop(_hb.sanitize(s.get("dir") or "?", 20), 12)
    icon_chip(fb, cx, slot_y + 120, [("tick", tick_idx, 6), ("text", name)],
              ink=PALETTE_IDX["ink"], plate=PALETTE_IDX["hud_plate"], anchor="mt",
              dashed=(state == "ghost"))


def draw_decor_work(fb):
    blit_prop(fb, ZONES["work"][2] - 50, 90, "printer", 4)
    blit_prop(fb, ZONES["work"][2] - 45, 340, "plant", 5)


def _escalation_style(tier):
    """(plate_idx, ink_idx, prefix) for a non-front queue row's wait chip."""
    if tier >= 2:
        return PALETTE_IDX["alarm"], PALETTE_IDX["ink_inverse"], "!! "
    if tier >= 1:
        return PALETTE_IDX["warn"], PALETTE_IDX["ink_inverse"], ""
    return PALETTE_IDX["hud_plate"], PALETTE_IDX["ink_dim"], ""


def draw_reception(fb, model):
    x0, y0, x1, y1 = ZONES["reception"]
    cx = (x0 + x1) // 2
    fb.rounded_rect(x0 + 40, y0 + 16, x1 - 40, y0 + 40, 4, PALETTE_IDX["wood"])
    fb.outline_rect(x0 + 40, y0 + 16, x1 - 40, y0 + 40, PALETTE_IDX["wood_dark"], 2)
    bw, bh = prop_size("bell", 3)
    blit_prop(fb, cx - bw // 2, y0 + 2, "bell", 3)
    text_plate(fb, cx, (y0 + 16 + y0 + 40) // 2, model["manager_label"],
               ink=PALETTE_IDX["ink"], plate=PALETTE_IDX["hud_plate"], anchor="mm")
    room_sign(fb, cx, y0 + 46, "MANAGER ROOM", anchor="mt")
    queue = model["queue"]
    if not queue:
        return
    front = queue[0]
    fy = 134
    hero = safe_hero(front.get("hero"))
    sp = SPRITES[hero]
    fb.blit_sprite(cx - 35, fy, sp["px"], _hero_pal_map(hero), scale=7)
    bang = "!! " if front["tier"] >= 2 else ""
    icon_chip(fb, cx, fy + 68, [("text", "%s%dM WAIT" % (bang, front["wait_min"]))],
              ink=PALETTE_IDX["ink_inverse"], plate=PALETTE_IDX["alarm"], anchor="mt")
    ask = front.get("ask")
    if ask:
        icon_chip(fb, cx, fy + 100, [("tick", PALETTE_IDX["alarm"], 5), ("text", ask)],
                  ink=PALETTE_IDX["ink"], plate=PALETTE_IDX["hud_plate"], anchor="mt")
    # compact list of everyone else (mirrors the emoji backend): small hero +
    # name + escalation wait, sorted longest-wait-first, so all 8 windows show.
    rest = queue[1:]
    list_top, pitch = 262, 25
    slots = max(1, (y1 - 8 - list_top) // pitch)
    shown = rest if len(rest) <= slots else rest[:slots - 1]
    extra = len(rest) - len(shown)
    ry = list_top
    for s in shown:
        hero = safe_hero(s.get("hero"))
        sp = SPRITES[hero]
        fb.blit_sprite(x0 + 14, ry - 2, sp["px"], _hero_pal_map(hero), scale=3)
        name = _crop(_hb.sanitize(s.get("dir") or "?", 20), 10)
        text_plate(fb, x0 + 46, ry, name, ink=PALETTE_IDX["ink_dim"],
                   plate=PALETTE_IDX["hud_plate"], anchor="lt")
        plate_idx, ink_idx, prefix = _escalation_style(s["tier"])
        icon_chip(fb, x1 - 16, ry, [("text", "%s%dM" % (prefix, s["wait_min"]))],
                  ink=ink_idx, plate=plate_idx, anchor="rt")
        ry += pitch
    if extra > 0:
        text_plate(fb, cx, ry, "+%d MORE" % extra, ink=PALETTE_IDX["ink_dim"], anchor="mt")


def draw_pantry(fb, model):
    x0, y0, x1, y1 = ZONES["pantry"]
    fb.rounded_rect(x0 + 8, y0 + 8, x0 + 500, y1 - 8, 16, PALETTE_IDX["rug_pantry"])
    blit_prop(fb, x0 + 16, y0 + 20, "couch", 6)
    blit_prop(fb, x0 + 260, y0 + 30, "coffee", 5)
    blit_prop(fb, x1 - 150, y0 + 24, "water_cooler", 6)
    blit_prop(fb, x0 + 380, y0 + 40, "plant", 6)
    blit_prop(fb, x1 - 60, y0 + 50, "plant", 6)
    rx = x0 + 100
    for s in model["resting"]:
        hero = safe_hero(s.get("hero"))
        sp = SPRITES[hero]
        fb.blit_sprite(rx, y0 + 44, sp["px"], _hero_pal_map(hero), scale=4)
        text_plate(fb, rx + 42, y0 + 28, "Z", ink=PALETTE_IDX["ink_dim"], pad=(2, 1), scale=2)
        rx += 84
    gx0, gx1 = HWALL["gap"]
    dxc = (gx0 + gx1) // 2
    done = model["done"]
    dx = dxc - (len(done) * 60) // 2
    for s in done:
        hero = safe_hero(s.get("hero"))
        sp = SPRITES[hero]
        fb.blit_sprite(dx, y0 + 6, sp["px"], _hero_pal_map(hero), scale=4)
        icon_chip(fb, dx + 22, y0 + 52, [("text", "OK %dM RUN" % s["run_min"])],
                  ink=PALETTE_IDX["ink_dim"], plate=PALETTE_IDX["hud_plate"], anchor="mt")
        dx += 60


def draw_hud(fb, model, now):
    fb.fill_rect(0, 0, W, 64, PALETTE_IDX["hud_plate"])
    fb.fill_rect(0, 62, W, 64, PALETTE_IDX["hud_border"])
    x = 16
    if model["queue_nonempty"]:
        rect = icon_chip(fb, x, 18, [("text", model["hud_text"])],
                          ink=PALETTE_IDX["ink_inverse"], plate=PALETTE_IDX["alarm"], scale=2)
    else:
        rect = icon_chip(fb, x, 18, [("text", model["hud_text"])],
                          ink=PALETTE_IDX["ink_dim"], plate=PALETTE_IDX["hud_plate"], scale=2)
    xcur = rect[2] + 40
    gauges = model["gauges"]
    if gauges:
        r5 = draw_gauge(fb, xcur, 6, "5H", gauges["five"], gauges["five_reset"],
                         5 * 3600, now, with_day=False)
        xcur = r5[2] + 36
        rwk = draw_gauge(fb, xcur, 6, "WK", gauges["seven"], gauges["seven_reset"],
                          7 * 86400, now, with_day=True)
        xcur = rwk[2] + 46
    else:
        r5 = text_plate(fb, xcur, 6, "5H --", ink=PALETTE_IDX["ink_dim"], pad=(4, 2))
        xcur = r5[2] + 16
        rwk = text_plate(fb, xcur, 6, "WK --", ink=PALETTE_IDX["ink_dim"], pad=(4, 2))
        xcur = rwk[2] + 30
    draw_clock(fb, xcur, 20, now)
    text_plate(fb, W - 16, 10, "TOTAL $%.2f" % model["payroll_total"],
               ink=PALETTE_IDX["ink_dim"], anchor="rt", pad=(4, 2))
    text_plate(fb, W - 16, 34, "$%.1f/HR" % model["payroll_rate"],
               ink=PALETTE_IDX["ink_dim"], anchor="rt", pad=(4, 2))


def draw_office(model, now):
    """Draws one static frame at the locked 1040x600 canvas from a model
    produced by build_office_model(). Pure function: no I/O, no globals
    mutated besides the returned Framebuffer."""
    fb = Framebuffer(W, H, PALETTE_IDX["bg_void"])
    floor_room(fb, ZONES["reception"])
    floor_room(fb, ZONES["work"])
    floor_room(fb, ZONES["pantry"])
    draw_walls(fb, model["queue_nonempty"])
    draw_decor_work(fb)
    draw_reception(fb, model)
    for i in range(8):
        sx, sy = slot_xy(i)
        s = model["desks"][i] if i < len(model["desks"]) else None
        draw_desk(fb, sx, sy, s)
    if model["overflow"] > 0:
        text_plate(fb, ZONES["work"][2] - 10, ZONES["work"][3] - 26,
                   "+%d MORE" % model["overflow"], ink=PALETTE_IDX["ink_dim"], anchor="rt")
    draw_pantry(fb, model)
    wx0, _, _, _ = ZONES["work"]
    room_sign(fb, wx0 + 14, 72, "OFFICE", anchor="lt")
    px0, py0, _, _ = ZONES["pantry"]
    room_sign(fb, px0 + 14, py0 + 8, "PANTRY", anchor="lt")
    draw_hud(fb, model, now)
    return fb


# --------------------------------------------------------------- data layer
def escalation_tier(wait_min):
    if wait_min >= ESCALATE_ALARM_MIN:
        return 2
    if wait_min >= ESCALATE_AMBER_MIN:
        return 1
    return 0


_ASK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*: .+$")


def _derive_ask(s):
    """WHAT the front/queued agent needs. `need` is read ONLY here — for
    needs_you members — because hero_line.py never clears it after the
    state flips back to working (confirmed on a live session file); trusting
    it outside the queue would fabricate an ask for a working session."""
    need = s.get("need")
    if need == "permission":
        activity = s.get("activity")
        if isinstance(activity, str) and _ASK_RE.match(activity):
            return "permission: " + activity
        return "permission needed"
    if need == "waiting for you":
        return "waiting: your review"
    return "waiting: your review"   # needs_you with no/garbled need: honest fallback


def _crop(s, n):
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def build_office_model(sessions, now, manager_label=None):
    """Pure data-mapping layer: sessions (as produced by hero_board.fleet(),
    each already carrying `_state`) -> everything draw_office() needs. No
    drawing, no I/O — directly unit-testable."""
    manager_label = _user_display_name() if manager_label is None else manager_label
    live = [s for s in sessions if s.get("_state") != "ghost"]

    queue = []
    for s in sessions:
        if s.get("_state") != "needs_you":
            continue
        ts = _hb.num(s.get("state_ts")) or _hb.num(s.get("ts")) or now
        wait_min = max(0, int((now - ts) / 60))
        row = dict(s)
        row["wait_min"] = wait_min
        row["tier"] = escalation_tier(wait_min)
        row["ask"] = _crop(_derive_ask(s), 26)
        queue.append(row)
    queue.sort(key=lambda r: (False, -r["wait_min"]))   # structured for a future error state

    desks = list(sessions[:8])
    overflow = max(0, len(sessions) - 8)

    resting, done = [], []
    for s in sessions:
        if s.get("_state") != "idle":
            continue
        ts = _hb.num(s.get("state_ts")) or _hb.num(s.get("ts")) or now
        age = now - ts
        row = dict(s)
        if age < 600:
            row["run_min"] = int((_hb.num(s.get("dur_ms")) or 0) / 60000)
            done.append(row)
        else:
            resting.append(row)

    # Account-wide 5h/7d, de-flickered across sessions (see hero_board.account_rl).
    gauges = _hb.account_rl(live)

    total = sum(_hb.num(s.get("cost")) or 0 for s in live)
    max_ms = max((_hb.num(s.get("dur_ms")) or 0 for s in live), default=0)
    max_hr = max_ms / 3.6e6
    rate = total / max_hr if max_hr > 0 else 0.0

    if queue:
        front = queue[0]
        dirname = _hb.sanitize(front.get("dir") or "?", 20)
        hud_text = "%d WAITING %s %dM" % (len(queue), dirname.upper()[:10], front["wait_min"])
    else:
        hud_text = "ALL CLEAR %d LIVE" % len(live)

    return {
        "now": now, "queue": queue, "desks": desks, "overflow": overflow,
        "resting": resting, "done": done, "gauges": gauges,
        "payroll_total": total, "payroll_rate": rate,
        "hud_text": hud_text, "queue_nonempty": bool(queue), "live_count": len(live),
        "manager_label": manager_label,
    }


# ---------------------------------------------------------------- encoders
def encode_png(fb, palette=None):
    """Truecolor (colortype 2), filter-0 scanlines, stdlib zlib — no PLTE
    chunk, no external image library. The framebuffer IS the palette-index
    twin of what encode_sixel() draws in the terminal.

    `palette` defaults to the module's own PALETTE_RGB (the stdlib sprite
    renderer's fixed token table); the emoji backend passes its own
    quantize()-derived 256-color table instead (see build_and_draw() in
    run()) — fb.buf is then palette INDICES into THAT table, not this
    module's PALETTE_RGB. Everything else about the encoding is unchanged."""
    w, h = fb.w, fb.h
    palette_rgb = PALETTE_RGB if palette is None else palette
    raw = bytearray()
    buf = fb.buf
    for y in range(h):
        raw.append(0)   # filter type 0: None
        base = y * w
        for x in range(w):
            raw += bytes(palette_rgb[buf[base + x]])
    compressed = zlib.compress(bytes(raw), 6)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def _rle_row(mask_bytes):
    out = []
    for val, group in itertools.groupby(mask_bytes):
        run = sum(1 for _ in group)
        ch = chr(0x3F + val)
        if run >= 4:
            out.append("!%d%s" % (run, ch))
        else:
            out.append(ch * run)
    return "".join(out)


def encode_sixel(fb, palette=None):
    """DCS sixel encoder: single-pass per 6-row band building
    {color -> bytearray(W) of 6-bit masks}, then per-color RLE emission.
    Palette defined once (rescaled to 0-100 per DEC sixel convention),
    `$` between colors in a band, `-` between bands, ST terminator.

    `palette` defaults to PALETTE_RGB; see encode_png()'s docstring — the
    emoji backend passes a quantize()-derived table here instead."""
    w, h = fb.w, fb.h
    buf = fb.buf
    palette_rgb = PALETTE_RGB if palette is None else palette
    parts = ["\x1bP0;1;0q", "\"1;1;%d;%d" % (w, h)]
    for i, (r, g, b) in enumerate(palette_rgb):
        parts.append("#%d;2;%d;%d;%d" % (i, (r * 100 + 127) // 255,
                                          (g * 100 + 127) // 255, (b * 100 + 127) // 255))
    band_strs = []
    y = 0
    while y < h:
        band_h = min(6, h - y)
        masks = {}
        for by in range(band_h):
            row = y + by
            base = row * w
            row_slice = buf[base:base + w]
            bitval = 1 << by
            for x in range(w):
                c = row_slice[x]
                m = masks.get(c)
                if m is None:
                    m = bytearray(w)
                    masks[c] = m
                m[x] |= bitval
        color_strs = ["#%d%s" % (c, _rle_row(masks[c])) for c in sorted(masks)]
        band_strs.append("$".join(color_strs))
        y += band_h
    parts.append("-".join(band_strs))
    parts.append("\x1b\\")
    return "".join(parts).encode("ascii")


# ------------------------------------------------------- capability detect
def _win_read_reply(query, terminators, timeout=0.3):
    """Windows: write an escape-sequence query to the terminal and read its
    reply from the console input buffer via msvcrt (there is no termios/select
    here). Reads until any terminator char or timeout; returns the reply (may
    be empty). Best-effort — never raises."""
    try:
        import msvcrt
    except Exception:
        return ""
    try:
        sys.stdout.write(query)
        sys.stdout.flush()
    except Exception:
        return ""
    reply = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            reply += ch
            if ch in terminators:
                break
        else:
            time.sleep(0.005)
    return reply


def detect_sixel():
    """Decision ladder, first hit wins: env override -> isatty gate -> DA1
    probe (bounded, termios always restored) -> iTerm2 env heuristic."""
    override = os.environ.get("STATUS_HERO_SIXEL")
    if override == "1":
        return True
    if override == "0":
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        # No termios here. Ask via DA1 through the console (msvcrt); if the
        # terminal reports sixel (param 4) trust it, otherwise fall back to
        # "are we in Windows Terminal?" — WT has shipped sixel since v1.22.
        reply = _win_read_reply("\x1b[c", ("c",))
        m = re.search(r"\x1b\[\?([0-9;]+)c", reply)
        if m:
            return "4" in m.group(1).split(";")
        return bool(os.environ.get("WT_SESSION"))
    try:
        import termios
        import tty
    except ImportError:
        termios = tty = None
    if termios is not None:
        try:
            fd = sys.stdin.fileno()
        except Exception:
            fd = None
        if fd is not None:
            old = None
            try:
                old = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                sys.stdout.write("\x1b[c")
                sys.stdout.flush()
                resp = ""
                deadline = time.monotonic() + 0.25
                while time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    r, _, _ = select.select([sys.stdin], [], [], remaining)
                    if not r:
                        break
                    resp += sys.stdin.read(1)
                    if resp.endswith("c"):
                        break
                m = re.search(r"\x1b\[\?([0-9;]+)c", resp)
                if m:
                    return "4" in m.group(1).split(";")
            except Exception:
                pass
            finally:
                if old is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    except Exception:
                        pass
    if os.environ.get("LC_TERMINAL") == "iTerm2" or os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return True
    return False


# --------------------------------------------------------- emoji backend --
# Progressive enhancement (the "B / hybrid emoji" look): when Pillow AND a
# system emoji font are both available, render real color-emoji heroes/props
# via hero_pixel_emoji.py instead of this file's stdlib palette sprites.
# hero_pixel_emoji is ONLY ever imported from inside _emoji_module()/below
# (never at module scope) so this file keeps working, byte-identically,
# with zero third-party dependencies whenever Pillow (or an emoji font) is
# missing. Import is cached in sys.modules by Python itself, so every caller
# here just imports it fresh each time rather than threading a cached
# reference through — no shared mutable state, no call-order dependency
# between emoji_available() and _emoji_framebuffer().
def _emoji_module():
    import hero_pixel_emoji as _hpe
    return _hpe


def emoji_available():
    """Pillow importable AND hero_pixel_emoji found a system emoji font AND
    the env override doesn't force it off. STATUS_HERO_EMOJI=1 doesn't do
    anything beyond NOT being "0" — it can't force the backend on if the
    dependencies aren't actually there."""
    if os.environ.get("STATUS_HERO_EMOJI") == "0":
        return False
    try:
        import PIL   # noqa: F401
    except Exception:
        return False
    try:
        return bool(_emoji_module().font_available())
    except Exception:
        return False


class _QuantizedFramebuffer:
    """Just enough of the Framebuffer interface (.w, .h, .buf) for
    encode_sixel()/encode_png() to draw a quantized PIL image — those
    functions only ever read these three attributes."""
    __slots__ = ("w", "h", "buf")

    def __init__(self, w, h, buf):
        self.w, self.h, self.buf = w, h, buf


def _emoji_framebuffer(model, now):
    """Renders the emoji backend's PIL image and quantizes it to a <=256
    color palette (proven visually lossless for this scene — see the
    commissioning task), returning (fb-like, palette) ready for
    encode_sixel()/encode_png()."""
    from PIL import Image
    img = _emoji_module().render(model, now)
    q = img.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.NONE)
    w, h = q.size
    buf = bytearray(q.tobytes())
    raw_pal = (q.getpalette() or [])
    raw_pal = (raw_pal + [0] * 768)[:768]   # always exactly 256 RGB triples
    palette = [tuple(raw_pal[i:i + 3]) for i in range(0, 768, 3)]
    return _QuantizedFramebuffer(w, h, buf), palette


# ------------------------------------------------------------------- demo
def demo_fleet(now=None):
    """A deterministic 8-session fleet exercising every visual feature:
    working+paper-stack, needs_you at each escalation tier, idle-done,
    idle-resting, compacting, ghost."""
    t = now if now is not None else time.time()

    def mk(i, d, hero, state, **extra):
        base = {
            "sid": "demo%d" % i, "dir": d, "hero": hero, "state": state, "_state": state,
            "state_ts": t, "ts": t, "started_at": t - (400 - i * 20) * 60,
            "ctx": 30, "cost": 5.0, "dur_ms": 30 * 60000, "model": "Fable 5",
            "five": 68.0, "five_reset": t + 5200, "seven": 41.0, "seven_reset": t + 3.1 * 86400,
            "branch": "main", "ppid": os.getpid(),
        }
        base.update(extra)
        return base

    return [
        mk(0, "acme-api", "fox", "working", ctx=91, cost=31.26, dur_ms=95 * 60000,
           activity="Bash: pytest -q tests/"),
        mk(1, "web-app", "cat", "needs_you", cost=4.10, dur_ms=40 * 60000,
           state_ts=t - 12 * 60, need="permission", activity="Edit: main.py"),
        mk(2, "data-sync", "rabbit", "needs_you", cost=6.75, dur_ms=22 * 60000,
           state_ts=t - 18 * 60, need="waiting for you"),
        mk(3, "api-gateway", "bear", "needs_you", cost=1.20, dur_ms=15 * 60000,
           state_ts=t - 35 * 60, need="permission", activity="Bash: rm -rf build"),
        mk(4, "auth-service", "frog", "idle", cost=12.55, dur_ms=38 * 60000, state_ts=t - 120),
        mk(5, "search-index", "owl", "idle", cost=3.30, dur_ms=50 * 60000, state_ts=t - 1800),
        mk(6, "billing-svc", "penguin", "compacting", cost=22.40, dur_ms=70 * 60000),
        mk(7, "mobile-app", "duck", "ghost", cost=15.90, dur_ms=200 * 60000),
    ]


# --------------------------------------------------------------- run loop
def _fs_signature():
    try:
        names = sorted(os.listdir(SESS_DIR))
    except Exception:
        return ()
    sig = []
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(SESS_DIR, n)
        try:
            st = os.stat(p)
            sig.append((n, st.st_mtime_ns, st.st_size))
        except Exception:
            pass
    return tuple(sig)


def _default_png_path():
    return os.path.join(STATE_DIR, "pixel-office.png")


def _dump_png(fb, path, palette=None):
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(encode_png(fb, palette))
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


_WIN_PIXEL_SIZE = None   # memoized Windows terminal pixel size (see _term_pixel_size)


def _term_pixel_size():
    """(xpixel, ypixel) of the terminal's content area, or (0, 0) if unknown.
    iTerm2 and most modern terminals report this via TIOCGWINSZ; the values
    are physical pixels, so on a Retina display they're large — which is
    exactly what we want to scale the fixed-size office up to fill."""
    if os.name == "nt":
        # No TIOCGWINSZ. Ask the terminal for its window size in pixels via
        # CSI 14 t -> reply "ESC [ 4 ; <height> ; <width> t". Unknown -> (0,0),
        # which just means scale factor 1 (base-size office, still renders).
        #
        # CACHED: this probe reads the console input queue (msvcrt), the SAME
        # queue run()'s keyboard loop polls for `q`. _scale_factor() calls us
        # once per rendered frame; probing every frame would swallow keystrokes
        # typed during the 0.3 s read window (q lost) and add up to 0.3 s of
        # latency per frame. So probe ONCE and memoize — the office won't
        # rescale on a Windows resize, but the quit key stays responsive.
        # (Unix below is a free ioctl, so it stays per-frame and resize-adaptive.)
        global _WIN_PIXEL_SIZE
        if _WIN_PIXEL_SIZE is None:
            _WIN_PIXEL_SIZE = (0, 0)
            reply = _win_read_reply("\x1b[14t", ("t",))
            m = re.search(r"\x1b\[4;([0-9]+);([0-9]+)t", reply)
            if m:
                ypix, xpix = int(m.group(1)), int(m.group(2))
                if xpix > 0 and ypix > 0:
                    _WIN_PIXEL_SIZE = (xpix, ypix)
        return _WIN_PIXEL_SIZE
    try:
        import fcntl
        import struct
        import termios
    except Exception:
        return 0, 0
    for stream in (sys.stdout, sys.stdin):
        try:
            fd = stream.fileno()
            packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
            _rows, _cols, xpix, ypix = struct.unpack("HHHH", packed)
            if xpix > 0 and ypix > 0:
                return xpix, ypix
        except Exception:
            continue
    return 0, 0


def _scale_factor(argv, w=1040, h=600):
    """Integer upscale so the fixed WxH office fills the terminal window.
    `--scale N` forces it; otherwise auto-fit from the terminal's pixel size.
    Nearest-neighbor (see _upscale) keeps the pixel art crisp. Clamped [1,4]
    so a huge window can't produce an absurdly heavy frame."""
    if "--scale" in argv:
        i = argv.index("--scale")
        if i + 1 < len(argv):
            try:
                return max(1, min(4, int(argv[i + 1])))
            except ValueError:
                pass
    xpix, ypix = _term_pixel_size()
    if xpix > 0 and ypix > 0:
        return max(1, min(4, min(xpix // w, ypix // h)))
    return 1


def _upscale(fb, n):
    """Nearest-neighbor integer upscale of a palette-index framebuffer — each
    pixel becomes an n*n block. Crisp for pixel art; returns an fb-like the
    encoders can read (.w/.h/.buf). n<=1 is a no-op passthrough."""
    if n <= 1:
        return fb
    w, h, src = fb.w, fb.h, fb.buf
    nw = w * n
    out = bytearray(nw * h * n)
    for y in range(h):
        row = bytearray(nw)
        base = y * w
        for x in range(w):
            row[x * n:x * n + n] = bytes((src[base + x],)) * n
        line = bytes(row)
        for ry in range(n):
            dst = (y * n + ry) * nw
            out[dst:dst + nw] = line
    return _QuantizedFramebuffer(nw, h * n, out)


def run(argv):
    argv = list(argv)
    once = "--once" in argv
    demo = "--demo" in argv
    png_path = _default_png_path()
    if "--png" in argv:
        idx = argv.index("--png")
        if idx + 1 < len(argv):
            png_path = argv[idx + 1]

    sixel_ok = detect_sixel()
    # Progressive enhancement, out loud: if we're about to draw the office but
    # Pillow (the emoji backend) is missing, say so once and how to upgrade —
    # never install anything behind the user's back.
    if sixel_ok and not emoji_available():
        sys.stderr.write("\x1b[2m[status-hero] drawing the stdlib pixel office; "
                         "`pip install pillow` for the Apple-emoji one.\x1b[0m\n")

    def build_and_draw():
        """Returns (fb, palette). `palette` is None for the stdlib sprite
        path (encode_sixel/encode_png then use the module's own
        PALETTE_RGB); the emoji backend returns its own quantized table
        instead. Any failure in the emoji path (missing font race, a bad
        Pillow install, whatever) falls back to the stdlib render for that
        frame rather than crashing the whole run — same "must still work
        with Pillow absent" guarantee, just checked per-frame too."""
        now = time.time()
        sessions = demo_fleet(now) if demo else _hb.fleet(now)
        model = build_office_model(sessions, now)
        if emoji_available():
            try:
                return _emoji_framebuffer(model, now)
            except Exception:
                pass
        return draw_office(model, now), None

    def emit_sixel(fb, palette=None):
        if not sixel_ok:
            return
        try:
            n = _scale_factor(argv)   # per-frame: adapts to window resize
            data = (b"\x1b[?2026h\x1b[H"
                    + encode_sixel(_upscale(fb, n), palette)
                    + b"\x1b[?2026l")
            if os.name == "nt":
                # Windows console: sys.stdout.buffer is UTF-8-only and does not
                # reliably pass raw DCS bytes; the text layer (WriteConsoleW)
                # does. Sixel is pure ASCII, so latin-1 is a 1:1 byte->char map.
                sys.stdout.write(data.decode("latin-1"))
                sys.stdout.flush()
            else:
                out = sys.stdout.buffer
                out.write(data)
                out.flush()
        except Exception:
            pass

    if once:
        fb, palette = build_and_draw()
        _dump_png(fb, png_path, palette)
        try:
            is_tty = sys.stdout.isatty()
        except Exception:
            is_tty = False
        if sixel_ok and is_tty:
            emit_sixel(fb, palette)
        return 0

    try:
        import termios
        import tty
    except ImportError:
        termios = tty = None
    msvcrt = None
    if termios is None and os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            pass
    fd, old = None, None
    if termios is not None:
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            old = None

    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()
    last_render = 0.0
    dirty = True
    last_sig, last_minute = None, None
    try:
        while True:
            sig = _fs_signature()
            minute = int(time.time() // 60)
            if sig != last_sig or minute != last_minute:
                dirty = True
                last_sig, last_minute = sig, minute
            now_m = time.monotonic()
            if dirty and (now_m - last_render) >= 1.0:
                fb, palette = build_and_draw()
                _dump_png(fb, png_path, palette)
                emit_sixel(fb, palette)
                last_render = now_m
                dirty = False
            ch = None
            if termios is not None and old is not None:
                r, _, _ = select.select([sys.stdin], [], [], 0.5)
                if r:
                    ch = sys.stdin.read(1)
            elif msvcrt is not None:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        break
                    time.sleep(0.02)
            else:
                time.sleep(0.5)
            if ch in ("q", "Q", "\x03"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if termios is not None and old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    audit_contrast()
    sys.exit(run(sys.argv[1:]))
