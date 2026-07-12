#!/usr/bin/env python3
"""claude-status-hero · hero_board.py — the game.

A real terminal dashboard for ALL your Claude Code windows. Run it in its own
iTerm2/tmux pane and glance at the whole fleet:

  • one pixel-art animal hero per live session (assigned by hero_line.py)
  • standing on a pillar whose height = that session's context %
  • state beacon: ⚡ working (bobs) · ❗ NEEDS YOU (blinks) · 💤 idle
                  🌀 compacting · 👻 stale/ghost
  • last activity per session ("you: fix the tests…", "Bash: pytest -q…")
  • account-wide 5h / 7d meters + total cost in the header
  • macOS notification when any session flips to NEEDS YOU

Usage:
  python3 hero_board.py            # scene mode, 4 fps
  python3 hero_board.py --office   # office mode: emoji desk pods + walk-in/out
  python3 hero_board.py --list     # dense one-row-per-session mode
  python3 hero_board.py --demo     # fake fleet (no Claude Code needed)
  python3 hero_board.py --once     # print a single frame and exit
  python3 hero_board.py --autostart on   # auto-open --office when Claude Code starts
                                         #   (add --list/--scene to pick another mode)
  --fps N (1-30) · --no-notify · keys: q quit, m cycle modes

Data comes from ~/.claude/status-hero/sessions/*.json, written by
hero_line.py (statusline + hooks). Install that first.

Python ≥3.9, stdlib only. MIT.
"""

import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import time
import unicodedata

# ---------------------------------------------------------------- environment

ASCII = os.environ.get("STATUS_HERO_ASCII", "") not in ("", "0")
AMBIG_WIDE = os.environ.get("STATUS_HERO_AMBIG_WIDE", "") not in ("", "0")
if AMBIG_WIDE:
    ASCII = True  # ambiguous-wide terminals can't render block art safely
NO_COLOR = os.environ.get("NO_COLOR") is not None
TRUECOLOR = (os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
             or bool(os.environ.get("WT_SESSION")))  # WT never sets COLORTERM

STATE_DIR = os.environ.get("STATUS_HERO_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "status-hero"
)
SESS_DIR = os.path.join(STATE_DIR, "sessions")

RESET = "" if NO_COLOR else "\x1b[0m"
BOLD = "" if NO_COLOR else "\x1b[1m"
DIM = "" if NO_COLOR else "\x1b[2m"
INVERT = "" if NO_COLOR else "\x1b[7m"


def fg(rgb, idx=15):
    if NO_COLOR:
        return ""
    if TRUECOLOR:
        return "\x1b[38;2;%d;%d;%dm" % rgb
    return "\x1b[38;5;%dm" % idx


GREEN = fg((80, 210, 110), 41)
YELLOW = fg((235, 185, 50), 220)
RED = fg((239, 90, 90), 196)
GRAY = fg((120, 126, 138), 245)
CYAN = fg((90, 200, 215), 44)
MAGENTA = fg((200, 140, 255), 177)


def zone(pct):
    if pct is None:
        return GRAY
    if pct >= 90:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


# ------------------------------------------------------------- display width

ANSI_TOKEN = re.compile(r"(\x1b\[[0-9;:]*m)")
CTRL_STRIP = re.compile(r"[\x00-\x1f\x7f]")
_WIDE_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF))


def ch_width(ch):
    o = ord(ch)
    if o == 0xFE0F or unicodedata.combining(ch):
        return 0
    for lo, hi in _WIDE_EMOJI_RANGES:
        if lo <= o <= hi:
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    ea = unicodedata.east_asian_width(ch)
    if ea in ("W", "F"):
        return 2
    if ea == "A":
        return 2 if AMBIG_WIDE else 1
    return 1


def disp_width(s):
    return sum(ch_width(ch) for ch in ANSI_TOKEN.sub("", s))


def crop_pad(s, width, fill=" "):
    out, w = [], 0
    for part in ANSI_TOKEN.split(s):
        if not part:
            continue
        if part.startswith("\x1b"):
            out.append(part)
            continue
        if w >= width:
            continue
        for ch in part:
            cw = ch_width(ch)
            if w + cw > width:
                if cw == 2 and width - w == 1:
                    out.append(" ")
                    w += 1
                break
            out.append(ch)
            w += cw
    return "".join(out) + RESET + fill * (width - w)


def wcrop(s, w):
    """Crop `s` to display width `w` (measuring via ch_width, not len() — a
    CJK string can hit its display-width limit long before its char count
    does). Truncation is marked with '..' so an overflowing bubble/name is
    visibly cut rather than silently clipped."""
    if disp_width(s) <= w:
        return s
    budget = max(0, w - 2)
    out, width = [], 0
    for ch in s:
        cw = ch_width(ch)
        if width + cw > budget:
            break
        out.append(ch)
        width += cw
    return "".join(out) + ".."


def sanitize(s, limit=120):
    if not isinstance(s, str):
        return ""
    s = s[:4096]        # bound the regex scans: hostile files can be huge
    s = CTRL_STRIP.sub("", ANSI_TOKEN.sub("", s)).strip()
    if len(s) > limit:
        s = s[: limit - 2] + ".."
    return s


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # json.load admits bare NaN/Infinity; int(round(nan)) would then crash
    # every render mode, and the poisoned file crash-loops the board
    return f if math.isfinite(f) else None


def fmt_reset(epoch_s):
    n = num(epoch_s)
    if n is None:
        return "--"
    diff = max(0, int(n - time.time()))
    h, m = diff // 3600, (diff % 3600) // 60
    if h >= 24:
        return "%dd%dh" % (h // 24, h % 24)
    if h:
        return "%dh%02dm" % (h, m)
    return "%dm" % m


def bar(pct, width):
    p = num(pct)
    filled = 0 if p is None else max(0, min(width, int(round(p * width / 100.0))))
    fill_ch, empty_ch = ("#", ".") if ASCII else ("█", "░")
    return zone(p) + fill_ch * filled + GRAY + DIM + empty_ch * (width - filled) + RESET


def pct_txt(pct):
    p = num(pct)
    return "--%" if p is None else "%d%%" % int(round(p))


# ----------------------------------------------------------------- sprites

# 10x8 pixel grids. '.'=transparent M=main D=dark W=white K=eye/ink A=accent
SPRITES = {
    "fox": {
        "pal": {"M": (255, 140, 66), "D": (200, 95, 40), "W": (255, 244, 230),
                "K": (30, 26, 24), "A": (255, 200, 160)},
        "px": ["MM......MM",
               "MMM....MMM",
               "MMMMMMMMMM",
               "MKWMMMMWKM",
               "MMMMMMMMMM",
               ".MWWWWWWM.",
               "..MMMMMM..",
               ".M......M."],
    },
    "cat": {
        "pal": {"M": (250, 208, 120), "D": (205, 160, 80), "W": (255, 250, 240),
                "K": (35, 30, 26), "A": (250, 150, 150)},
        "px": ["M........M",
               "MM......MM",
               "MMMMMMMMMM",
               "MKMMMMMMKM",
               "MMAMMMMAMM",
               ".MMMWWMMM.",
               "..MMMMMM..",
               ".M......M."],
    },
    "frog": {
        "pal": {"M": (110, 205, 90), "D": (70, 160, 60), "W": (250, 255, 245),
                "K": (25, 35, 25), "A": (240, 120, 120)},
        "px": [".WW....WW.",
               ".WK....KW.",
               "MMMMMMMMMM",
               "MMMMMMMMMM",
               "MMAAAAAAMM",
               "MMMMMMMMMM",
               ".M.MMMM.M.",
               "M........M"],
    },
    "owl": {
        "pal": {"M": (188, 140, 100), "D": (140, 100, 70), "W": (255, 248, 235),
                "K": (35, 28, 22), "A": (240, 170, 60)},
        "px": ["MM......MM",
               "MMMMMMMMMM",
               "MWWMMMMWWM",
               "MWKMMMMKWM",
               "MMMMAAMMMM",
               "MDMMMMMMDM",
               ".MMMMMMMM.",
               "..M....M.."],
    },
    "penguin": {
        "pal": {"M": (52, 60, 76), "D": (30, 36, 48), "W": (245, 248, 252),
                "K": (240, 200, 80), "A": (255, 170, 60)},
        "px": ["...MMMM...",
               "..MMMMMM..",
               ".MMWMMWMM.",
               ".MMMKKMMM.",
               ".MWWWWWWM.",
               ".MWWWWWWM.",
               "..MWWWWM..",
               "..A....A.."],
    },
    "rabbit": {
        "pal": {"M": (235, 210, 215), "D": (200, 160, 170), "W": (255, 252, 252),
                "K": (40, 32, 34), "A": (245, 140, 160)},
        "px": ["..M....M..",
               "..MA..AM..",
               "..MM..MM..",
               ".MMMMMMMM.",
               ".MKMMMMKM.",
               ".MMMAAMMM.",
               "..MMMMMM..",
               "..M....M.."],
    },
    "bear": {
        "pal": {"M": (196, 138, 84), "D": (150, 100, 60), "W": (240, 225, 205),
                "K": (35, 28, 22), "A": (170, 110, 70)},
        "px": ["MM......MM",
               "MMMMMMMMMM",
               "MMMMMMMMMM",
               "MKMMMMMMKM",
               "MMMWWWWMMM",
               "MMMWKWWMMM",
               ".MMMMMMMM.",
               ".M......M."],
    },
    "duck": {
        "pal": {"M": (240, 210, 90), "D": (200, 165, 60), "W": (255, 250, 235),
                "K": (35, 30, 24), "A": (255, 150, 60)},
        "px": ["...MMMM...",
               "..MMMMMM..",
               "..MKMMKM..",
               ".MAAAAAAM.",
               "MMMMMMMMMM",
               "MMMMMMMMMM",
               ".MMMMMMMM.",
               "..A....A.."],
    },
}
SPRITE_W, SPRITE_H = 10, 8   # pixels → 10 cols x 4 terminal rows

HERO_EMOJI = {"fox": "🦊", "cat": "🐱", "frog": "🐸", "owl": "🦉",
              "penguin": "🐧", "rabbit": "🐰", "bear": "🐻", "duck": "🦆"}

G_SUM = "cost "
G_RESET_AT = "R " if ASCII else "↻"
SEP = " - " if ASCII else " · "

STATES = {
    "working":    {"glyph": "⚡", "ascii": "*", "label": "working",    "col": CYAN},
    "needs_you":  {"glyph": "❗", "ascii": "!", "label": "NEEDS YOU",  "col": RED},
    "idle":       {"glyph": "💤", "ascii": "z", "label": "idle",       "col": GRAY},
    "compacting": {"glyph": "🌀", "ascii": "~", "label": "compacting", "col": MAGENTA},
    "ghost":      {"glyph": "👻", "ascii": "?", "label": "stale",      "col": GRAY},
}


def idx256(rgb):
    """Nearest xterm-256 color-cube index for an RGB triple."""
    r, g, b = (min(5, max(0, int(round(c / 51.0)))) for c in rgb)
    return 16 + 36 * r + 6 * g + b


def px_fg(rgb):
    if NO_COLOR:
        return ""
    return "\x1b[38;2;%d;%d;%dm" % rgb if TRUECOLOR else "\x1b[38;5;%dm" % idx256(rgb)


def px_bg(rgb):
    if NO_COLOR:
        return ""
    return "\x1b[48;2;%d;%d;%dm" % rgb if TRUECOLOR else "\x1b[48;5;%dm" % idx256(rgb)


def sprite_cells(hero, frame, mood):
    """A hero as 4 rows × SPRITE_W one-column cells (None = transparent).

    Cell form (vs joined lines) lets the office corridor composite several
    walking sprites onto one row at arbitrary x offsets.
    """
    sp = SPRITES.get(hero) or SPRITES["fox"]
    px = list(sp["px"])
    if mood == "working" and frame % 2:
        px = px[:-1] + [px[-1][::-1]]          # feet swap = trot
    if mood == "idle":
        px = px[1:] + ["." * SPRITE_W]         # slumped: sink 1px

    def color(chv):
        rgb = sp["pal"].get(chv)
        if rgb is None:
            return None
        if mood in ("idle", "ghost"):
            rgb = tuple(int(c * 0.45 + 25) for c in rgb)
        if mood == "ghost":
            g = int(sum(rgb) / 3)
            rgb = (g, g, g)
        return rgb

    rows = []
    for r in range(0, SPRITE_H, 2):
        cells = []
        for cix in range(SPRITE_W):
            top, bot = color(px[r][cix]), color(px[r + 1][cix])
            if ASCII:
                cells.append("#" if (top or bot) else None)
            elif top and bot:
                cells.append(px_fg(top) + px_bg(bot) + "▀" + RESET)
            elif top:
                cells.append(px_fg(top) + "▀" + RESET)
            elif bot:
                cells.append(px_fg(bot) + "▄" + RESET)
            else:
                cells.append(None)
        rows.append(cells)
    return rows


def sprite_lines(hero, frame, mood):
    """Render a hero as 4 terminal rows of half-blocks, exactly SPRITE_W wide."""
    return ["".join(c or " " for c in row)
            for row in sprite_cells(hero, frame, mood)]


# --------------------------------------------------------------- fleet data

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def pid_alive(pid):
    p = num(pid)
    if not p or os.name == "nt":   # os.kill(pid,0) semantics unreliable on nt
        return None
    try:
        os.kill(int(p), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return None


def effective_state(s, now=None):
    now = now or time.time()
    ts = num(s.get("state_ts")) or num(s.get("ts")) or 0
    age = now - (num(s.get("ts")) or 0)
    if age > 2 * 3600:
        return "ghost"
    st = s.get("state") or "working"
    if st == "needs_you" and now - ts > 15 * 60 \
            and pid_alive(s.get("ppid")) is False:
        # killed while waiting: without this it would blink (and re-notify
        # every 120 s) for up to 2 h. A LIVE waiting session stays needs_you
        # no matter how long you're away — only a dead pid demotes it.
        return "ghost"
    if st in ("working", "compacting") and now - ts > 15 * 60:
        # no heartbeat: interrupted or forgotten — check the recorded pid
        if pid_alive(s.get("ppid")) is False:
            return "ghost"
        return "idle"
    return st if st in STATES else "idle"


def fleet(now=None):
    now = now or time.time()
    out = []
    try:
        names = sorted(os.listdir(SESS_DIR))
    except Exception:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(SESS_DIR, n)
        s = load_json(path)
        if not s or not isinstance(s.get("sid"), str) or not s.get("sid"):
            continue
        if not isinstance(s.get("state"), str):
            s["state"] = None       # hostile types would crash `in STATES`
        if not isinstance(s.get("hero"), str):
            s["hero"] = None
        if s.get("ended"):
            # bury tombstones only after they've outlived any in-flight
            # statusline render (git can stall ~3 s) — removing them on
            # sight would re-open the resurrection race they exist to stop
            if now - (num(s.get("ts")) or 0) > 60:
                try:
                    os.remove(path)
                except Exception:
                    pass
            continue
        if now - (num(s.get("ts")) or 0) > 24 * 3600:
            try:
                os.remove(path)               # day-old corpse: bury it
            except Exception:
                pass
            continue
        s["_state"] = effective_state(s, now)
        out.append(s)
    out.sort(key=lambda s: num(s.get("started_at")) or num(s.get("ts")) or 0)
    return out


def demo_fleet(frame):
    t = time.time()
    mk = lambda i, d, st, ctx, cost, act, five=76: {
        "sid": "demo%d" % i, "dir": d, "hero": list(SPRITES)[i], "state": st,
        "_state": st, "state_ts": t, "ts": t, "started_at": t - i * 100,
        "ctx": ctx, "cost": cost, "activity": act, "model": "Fable 5",
        "five": five, "five_reset": t + 7900, "seven": 31, "seven_reset": t + 260000,
        "branch": "main",
    }
    out = [
        mk(0, "sightlab", "working", 52, 31.26, "Bash: pytest -q tests/"),
        mk(1, "invest-sight", "needs_you", 18, 4.10, "permission: Edit main.py"),
        mk(2, "daily-news", "idle", 91, 12.55, "you: push the brief"),
        mk(3, "onchain-sight", "working", 34, 8.02, "Read: abi/router.json"),
        mk(4, "radar", "compacting", 96, 22.40, "compacting context"),
    ]
    # a scheduled visitor exercises the office walk-in/out (pure in frame:
    # absent at frame 0, so --once and the geometry tests stay byte-stable)
    if 12 <= frame % 90 < 60:
        out.append(mk(5, "visitor", "working", 8, 0.42, "Bash: git clone .."))
    return out


# ----------------------------------------------------------------- renderer

def header(sessions, W, demo, usage=True):
    live = [s for s in sessions if s["_state"] != "ghost"]
    counts = {}
    for s in live:
        counts[s["_state"]] = counts.get(s["_state"], 0) + 1
    parts = [BOLD + ("STATUS-HERO FLEET" if not ASCII else "STATUS-HERO") + RESET,
             DIM + "%d live" % len(live) + RESET]
    for st in ("working", "needs_you", "idle", "compacting"):
        if counts.get(st):
            m = STATES[st]
            parts.append(m["col"] + (m["ascii"] if ASCII else m["glyph"])
                         + "%d" % counts[st] + RESET)
    cost = sum(num(s.get("cost")) or 0 for s in live)
    parts.append(DIM + G_SUM + "$%.2f" % cost + RESET)
    if demo:
        parts.append(MAGENTA + "demo data" + RESET)
    left = "  ".join(parts)

    if not usage:
        # office mode: the statusline in every window already shows 5h/7d —
        # the floor plan is the calm view, so the usage meters are dropped
        return [crop_pad(left, W), GRAY + DIM + ("─" if not ASCII else "-") * W + RESET]

    rl = None
    for s in sorted(live, key=lambda x: -(num(x.get("ts")) or 0)):
        if num(s.get("five")) is not None:
            rl = s
            break
    if rl:
        right = ("5h " + bar(rl.get("five"), 8) + " " + zone(num(rl.get("five")))
                 + pct_txt(rl.get("five")) + RESET + DIM + " " + G_RESET_AT
                 + fmt_reset(rl.get("five_reset"))
                 + RESET + "   7d " + bar(rl.get("seven"), 6) + " "
                 + zone(num(rl.get("seven"))) + pct_txt(rl.get("seven")) + RESET)
    else:
        right = DIM + "5h --  7d --  (no Pro/Max rate data yet)" + RESET
    gap = W - disp_width(left) - disp_width(right)
    line = left + " " * max(1, gap) + right if gap >= 1 else left
    return [crop_pad(line, W), GRAY + DIM + ("─" if not ASCII else "-") * W + RESET]


def render_scene(sessions, W, H, frame, demo):
    lines = header(sessions, W, demo)
    if not sessions:
        return lines + empty_state(W)
    n = len(sessions)
    lane_w = max(SPRITE_W + 4, min(22, (W - 2) // n)) if n else W
    if lane_w * n > W:                       # too many lanes for this width
        return lines + render_list(sessions, W, H, frame, demo, header_done=True)

    pillar_max = max(3, min(10, H - 13))
    beacon_r, sprite_r = 1, 4
    grid_h = beacon_r + sprite_r + pillar_max
    lanes = [lane_render(s, lane_w, pillar_max, frame) for s in sessions]
    for r in range(grid_h):
        lines.append(crop_pad("  " + "".join(l[r] for l in lanes), W))
    lines.append(GRAY + DIM + ("─" if not ASCII else "-") * W + RESET)
    for r in range(grid_h, grid_h + 4):
        lines.append(crop_pad("  " + "".join(l[r] for l in lanes), W))
    return lines


def lane_render(s, lane_w, pillar_max, frame):
    """One session → beacon+sprite+pillar rows, then 4 text rows. Fixed widths."""
    st = s["_state"]
    meta = STATES[st]
    ctx = num(s.get("ctx"))
    pillar_h = 0 if ctx is None else max(1, int(round(ctx / 100.0 * pillar_max)))
    rows = []

    # beacon (blinks for needs_you)
    beacon = meta["ascii"] if ASCII else meta["glyph"]
    if st == "needs_you" and frame % 2:
        beacon = " "
    rows.append(center(meta["col"] + beacon + RESET, lane_w))

    # air gap above sprite so it "stands" on its pillar (bob = 1-row hop)
    air = pillar_max - pillar_h
    spr = sprite_lines(s.get("hero") or "fox", frame, st)
    bob = 1 if (st == "working" and frame % 2 == 0) else 0
    col_px = zone(ctx)
    top = max(0, air - bob)
    for r in range(4 + pillar_max):
        if top <= r < top + 4:
            rows.append(center(spr[r - top], lane_w))
        elif r >= air + 4:
            fill_ch = "#" if ASCII else "█"
            pil = col_px + (DIM if st in ("idle", "ghost") else "") + fill_ch * 4 + RESET
            rows.append(center(pil, lane_w))
        else:
            rows.append(" " * lane_w)

    # text rows (center() measures real display width — CJK names stay aligned)
    name = sanitize(s.get("dir") or "?", lane_w - 2)
    if st == "needs_you":
        rows.append(center(RED + BOLD + INVERT + " " + name + " " + RESET, lane_w))
    else:
        rows.append(center(BOLD + name + RESET, lane_w))
    rows.append(center(meta["col"] + meta["label"] + RESET, lane_w))
    m = "ctx " + pct_txt(ctx) + "  $" + ("%.0f" % (num(s.get("cost")) or 0))
    rows.append(center(DIM + m + RESET, lane_w))
    act = sanitize(s.get("activity") or "", lane_w - 2)
    rows.append(center(GRAY + act + RESET, lane_w))
    return rows


def center(s, width):
    w = disp_width(s)   # always measured — char counts lie for CJK/emoji
    if w >= width:
        return crop_pad(s, width)
    lpad = (width - w) // 2
    return " " * lpad + s + RESET + " " * (width - w - lpad)


def render_list(sessions, W, H, frame, demo, header_done=False):
    lines = [] if header_done else header(sessions, W, demo)
    if not sessions:
        return lines + empty_state(W)
    name_w = 16 if W >= 90 else 12
    state_w = 12 if W >= 80 else 10
    for s in sessions[: max(1, H - 5)]:
        st = s["_state"]
        meta = STATES[st]
        glyph = HERO_EMOJI.get(s.get("hero"), "🦊") if not ASCII else "@"
        beacon = meta["ascii"] if ASCII else meta["glyph"]
        if st == "needs_you" and frame % 2:
            beacon = " " if ASCII else "  "
        name = crop_pad(BOLD + sanitize(s.get("dir") or "?", name_w) + RESET, name_w)
        state = crop_pad(meta["col"] + (BOLD if st == "needs_you" else "")
                         + meta["label"] + RESET, state_w)
        ctx = num(s.get("ctx"))
        bar_w = 8 if W >= 90 else 5
        right = (bar(ctx, bar_w) + " " + zone(ctx) + "%4s" % pct_txt(ctx) + RESET
                 + DIM + "  $%6.2f" % (num(s.get("cost")) or 0) + RESET)
        # activity gets what's left; when nothing is left it's dropped whole,
        # never squeezing the cost column into truncation
        act_w = W - 2 - 2 - 1 - name_w - 1 - 2 - 1 - state_w - 1 - disp_width(right) - 1
        act = crop_pad(GRAY + sanitize(s.get("activity") or "", max(0, act_w))
                       + RESET, max(0, act_w)) if act_w >= 6 else ""
        sepa = " " if act else ""
        lines.append(crop_pad("  " + glyph + " " + name + " "
                              + meta["col"] + beacon + RESET + " " + state + " "
                              + act + sepa + right, W))
    if len(sessions) > max(1, H - 5):
        lines.append(crop_pad(DIM + "  +%d more" % (len(sessions) - max(1, H - 5))
                              + RESET, W))
    return lines


def empty_state(W):
    msg = ["", "  no live sessions.",
           "  open a Claude Code window (with hero_line.py installed),",
           "  or try:  python3 hero_board.py --demo", ""]
    return [crop_pad(DIM + m + RESET, W) for m in msg]


# ------------------------------------------------------------------- office
# A floor plan, not a lineup. Each session owns a COLUMN: its desk sits at
# the top (where it works) and its break-room spot sits at the bottom (the
# 茶水间). So a session going idle literally walks DOWN its column to the
# break room, and walks back UP to its desk when it resumes. New sessions
# walk in through the door in the left wall; ended sessions walk back out.
# The actor is a single emoji — the SAME hero glyph the statusline shows for
# that window — so a glance links a PowerShell/iTerm window to its desk.
# A desk pod is drawn for every LIVE session (name plate + state beacon +
# cost, no ctx/usage — that lives in the statusline already); a speech
# bubble over a seated, busy actor carries its current activity string.
# Animation state lives ONLY in the renderer (ostate); --once passes
# ostate=None → everyone drawn at their home spot (deterministic for tests).

DESK_W = 12                 # cols per desk pod (name/status/bubble budget)
OFFICE_MIN_H = 15           # interior rows below which we fall back to list
COL_MAX = 22                # a column never spreads wider than this
ROOM_H_MAX = 18             # room height caps out here instead of stretching


class Stage:
    """A 2D cell buffer that renders to fixed-width ANSI rows.

    A cell is None (floor), a 1-column styled glyph, ("run", text, width)
    for a multi-column styled label, or "skip" for the columns a run covers.
    Later paints win, so draw order is furniture → sprites → labels."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.g = [[None] * w for _ in range(h)]

    def glyph(self, x, y, styled):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.g[y][x] = styled

    def sprite(self, x, y, cells):
        for r, row in enumerate(cells):
            for c, cell in enumerate(row):
                if cell is not None:
                    self.glyph(x + c, y + r, cell)

    def bar(self, x, y, ch_styled, width):
        """A horizontal run drawn per-cell (unlike text(): a walker crossing
        it composits cleanly instead of being swallowed by a wide run)."""
        for i in range(width):
            self.glyph(x + i, y, ch_styled)

    def text(self, x, y, styled, plain):
        """Place a styled run; `plain` (unstyled) measures its column width."""
        w = disp_width(plain)
        if not (0 <= y < self.h) or x < 0 or x + w > self.w or w <= 0:
            return
        self.g[y][x] = ("run", styled, w)
        for i in range(1, w):
            self.g[y][x + i] = "skip"

    def rows(self):
        out = []
        for y in range(self.h):
            cells, i, row = [], 0, self.g[y]
            while i < self.w:
                cell = row[i]
                if isinstance(cell, tuple):     # ("run", text, width)
                    cells.append(cell[1]); i += cell[2]
                elif isinstance(cell, str) and cell != "skip":
                    cells.append(cell); i += 1
                else:                            # None (floor) or stray skip
                    cells.append(" "); i += 1
            out.append("".join(cells))
        return out


def office_geo(n, IW, IH):
    """Column layout, or None when the room can't fit → caller uses list.

    Desks SPREAD across the full width instead of hugging the left, and the
    room height caps at ROOM_H_MAX instead of stretching into a mostly-empty
    tall room on a big terminal."""
    if IH < OFFICE_MIN_H or n * DESK_W > IW or IW < DESK_W:
        return None
    rh = min(IH, ROOM_H_MAX)
    col_w = min(COL_MAX, IW // n)
    left = (IW - n * col_w) // 2
    pods = [left + i * col_w + (col_w - DESK_W) // 2 for i in range(n)]
    seats = [(pods[i] + 4, 2) for i in range(n)]                # actor anchor
    lounges = [(seats[i][0], rh - 2) for i in range(n)]          # 茶水间
    divider_y = rh - 3
    door_y = max(6, min(rh - 5, (6 + (rh - 4)) // 2))
    return {"seats": seats, "lounges": lounges, "divider_y": divider_y,
            "door_y": door_y, "IW": IW, "IH": IH, "pods": pods,
            "col_w": col_w, "left": left, "rh": rh}


def _toward(v, t, step):
    if v < t:
        return min(v + step, float(t))
    if v > t:
        return max(v - step, float(t))
    return v


def office_move(ostate, sessions, geo):
    """Return placed {sid:(x,y,session,walking,at_desk)} and leaving walkers
    [(x,y,hero)]. Mutates ostate (renderer-local; session files untouched)."""
    seats, lounges, door_y = geo["seats"], geo["lounges"], geo["door_y"]
    cur = {s["sid"]: s for s in sessions}
    def home(i, s):     # working/needs_you/compacting up top; idle on break
        return lounges[i] if s["_state"] == "idle" else seats[i]
    homes = {s["sid"]: home(i, s) for i, s in enumerate(sessions)}
    desk = {s["sid"]: s["_state"] != "idle" for s in sessions}

    if ostate is None:              # --once: everyone at home, no animation
        placed = {sid: (homes[sid][0], homes[sid][1], cur[sid], False,
                        desk[sid]) for sid in cur}
        return placed, []

    if not ostate.get("init"):      # first frame: seat the fleet, no walk-in
        ostate.update(init=True, ag={}, leaving={})
        for sid in cur:
            hx, hy = homes[sid]
            ostate["ag"][sid] = {"x": float(hx), "y": float(hy),
                                 "hero": cur[sid].get("hero") or "fox"}
    ag, leaving = ostate["ag"], ostate["leaving"]
    step = max(2, geo["IW"] // 40)
    for sid in cur:                                 # arrivals: in from door
        if sid not in ag:
            ag[sid] = {"x": 0.0, "y": float(door_y),
                       "hero": cur[sid].get("hero") or "fox"}
            leaving.pop(sid, None)                  # rejoined while leaving
    for sid in list(ag):                            # departures: out the door
        if sid not in cur:
            a = ag.pop(sid)
            leaving[sid] = {"x": a["x"], "y": a["y"], "hero": a["hero"]}
    placed = {}
    for sid, s in cur.items():
        a = ag[sid]
        tx, ty = homes[sid]
        if s["_state"] == "needs_you":              # attention: snap to desk
            a["x"], a["y"] = float(tx), float(ty)
        else:
            a["x"], a["y"] = _toward(a["x"], tx, step), _toward(a["y"], ty, step)
        walking = abs(a["x"] - tx) > 0.5 or abs(a["y"] - ty) > 0.5
        placed[sid] = (int(round(a["x"])), int(round(a["y"])), s, walking,
                       desk[sid])
    walkers = []
    for sid in list(leaving):
        lv = leaving[sid]
        lv["x"], lv["y"] = _toward(lv["x"], 0, step), _toward(lv["y"], door_y, step)
        if lv["x"] <= 0 and abs(lv["y"] - door_y) < 1:
            del leaving[sid]                        # out the door
        else:
            walkers.append((int(round(lv["x"])), int(round(lv["y"])), lv["hero"]))
    return placed, walkers


def office_backdrop(stage, geo, n):
    """Draw the fixtures — rug, wall clock, plants, door marker, the 茶水间
    divider + water cooler. Desks/actors/bubbles paint over all of this."""
    IW, rh = geo["IW"], geo["rh"]
    door_y, dy = geo["door_y"], geo["divider_y"]

    rug_w = min(28, IW - 8)                          # per-cell: walkers cross it
    if rug_w > 0:
        dot = "." if ASCII else "·"
        rug_y = (6 + (rh - 4)) // 2
        rx = (IW - rug_w) // 2
        stage.bar(rx, rug_y, GRAY + DIM + dot + RESET, rug_w)
        stage.bar(rx, rug_y + 1, GRAY + DIM + dot + RESET, rug_w)

    rule = "-" if ASCII else "─"
    label = "PANTRY" if ASCII else "  ☕ 茶水间  "
    # per-cell (NOT one wide run): a full-width run would swallow the label
    # painted on top of it — Stage.rows() jumps a run's whole span.
    for cx in range(IW):
        stage.glyph(cx, dy, GRAY + DIM + rule + RESET)
    lx = max(0, (IW - disp_width(label)) // 2)
    stage.text(lx, dy, GRAY + label + RESET, label)

    if ASCII:
        return                                       # decor is emoji-only
    stage.text(0, door_y, "🚪", "🚪")
    stage.text((IW - 2) // 2, 0, "🕐", "🕐")
    stage.text(1, 0, "🪴", "🪴")
    if IW - 3 > 1:
        stage.text(IW - 3, 0, "🪴", "🪴")
    stage.text(1, rh - 2, "🪴", "🪴")
    if IW - 6 >= 0:
        stage.text(IW - 6, rh - 2, "🚰", "🚰")


def draw_desk(stage, geo, i, s, frame):
    """The static desk pod for session i — drawn for EVERY live session
    regardless of where its actor currently stands (desk/break/walking).
    This is the entire per-agent data surface: name + state + cost."""
    sx, sy = geo["seats"][i]
    pod = geo["pods"][i]
    st = s["_state"]
    meta = STATES[st]
    bw = 1 if ASCII else 2

    if not ASCII:                            # empty chair; the hero overwrites
        stage.text(sx, sy, GRAY + DIM + "🪑" + RESET, "🪑")   # this same-x run
    laptop = "[]" if ASCII else "💻"
    stage.text(sx + bw, sy, GRAY + laptop + RESET, laptop)

    fill = "=" if ASCII else "▄"
    dcol = RED if st == "needs_you" else (GRAY + DIM)
    stage.bar(pod, sy + 1, dcol + fill + RESET, DESK_W)

    name = wcrop(sanitize(s.get("dir") or "?", 40), DESK_W)
    nx = pod + max(0, (DESK_W - disp_width(name)) // 2)
    if st == "needs_you":
        stage.text(nx, sy + 2, RED + BOLD + INVERT + name + RESET, name)
    elif st == "ghost":
        stage.text(nx, sy + 2, DIM + name + RESET, name)
    else:
        stage.text(nx, sy + 2, BOLD + name + RESET, name)

    beacon = meta["ascii"] if ASCII else meta["glyph"]
    if st == "needs_you" and frame % 2:
        beacon = " " if ASCII else "  "              # same-width blink
    cost = " $%.2f" % (num(s.get("cost")) or 0)
    plain = beacon + cost
    styled = meta["col"] + beacon + RESET + DIM + cost + RESET
    bx = pod + max(0, (DESK_W - disp_width(plain)) // 2)
    stage.text(bx, sy + 3, styled, plain)


def draw_actor(stage, x, y, s, walking, at_desk):
    """The moving actor: the session's hero glyph, identical to the
    statusline — the window↔desk link the whole redesign is for."""
    st = s["_state"]
    hero = s.get("hero") or "fox"
    if ASCII:
        glyph, plain = "@", "@"
    else:
        glyph = "👻" if st == "ghost" else HERO_EMOJI.get(hero, "🦊")
        plain = "xx"
    stage.text(x, y, glyph, plain)
    if walking or at_desk:
        return                                        # bare hero mid-motion
    sleep = "z" if ASCII else "💤"                     # on break: idle statement
    stage.text(x + (1 if ASCII else 2), y, DIM + sleep + RESET, sleep)


def draw_walker(stage, x, y, hero):
    if ASCII:
        stage.text(x, y, "@", "@")
    else:
        stage.text(x, y, HERO_EMOJI.get(hero, "🦊"), "xx")


def draw_bubble(stage, geo, i, s):
    """Speech bubble over a seated, busy actor — the session's `activity`.
    Never for idle/ghost, never mid-walk, never in the break room."""
    st = s["_state"]
    act = s.get("activity")
    if st not in ("working", "compacting", "needs_you") or not act:
        return
    pod, col_w, left = geo["pods"][i], geo["col_w"], geo["left"]
    seat_x = geo["seats"][i][0]
    text = wcrop(sanitize(act, 80), max(0, col_w - 4))
    balloon = "(" + text + ")"
    bw = disp_width(balloon)
    bx = pod + (DESK_W - bw) // 2
    lo, hi = left + i * col_w, left + (i + 1) * col_w - bw     # own column only
    bx = max(lo, min(max(lo, hi), bx))
    bx = max(0, min(geo["IW"] - bw, bx))                       # never off-stage
    if st == "needs_you":
        styled = RED + BOLD + INVERT + balloon + RESET
    else:
        styled = INVERT + balloon + RESET
    stage.text(bx, 0, styled, balloon)
    tail = "v" if ASCII else "▼"
    stage.text(seat_x + (0 if ASCII else 1), 1, STATES[st]["col"] + tail + RESET,
               tail)


def render_office(sessions, W, H, frame, demo, ostate=None):
    lines = header(sessions, W, demo, usage=False)
    n = len(sessions)
    IW, IH = W - 2, H - 4
    geo = office_geo(n, IW, IH) if n else office_geo(1, IW, IH)
    if n > 8 or geo is None:
        return lines + render_list(sessions, W, H, frame, demo,
                                   header_done=True)
    # move BEFORE the empty check: the last session's walk-out must still
    # play in an otherwise-empty room
    placed, walkers = office_move(ostate, sessions, geo)
    if not sessions and not walkers:
        return lines + empty_state(W)
    stage = Stage(IW, geo["rh"])
    office_backdrop(stage, geo, n)
    for i, s in enumerate(sessions):                  # every desk, always
        draw_desk(stage, geo, i, s, frame)
    for (x, y, s, walking, at_desk) in placed.values():
        draw_actor(stage, x, y, s, walking, at_desk)
    for (x, y, hero) in walkers:
        draw_walker(stage, x, y, hero)
    for i, s in enumerate(sessions):                  # bubbles paint last
        p = placed.get(s.get("sid"))
        if p is not None and not p[3] and p[4]:        # seated, not walking
            draw_bubble(stage, geo, i, s)

    hwall, vwall = ("-", "|") if ASCII else ("─", "│")
    tl, tr, bl, br = ("+",) * 4 if ASCII else ("┌", "┐", "└", "┘")
    wc = GRAY + DIM
    lines.append(crop_pad(wc + tl + hwall * IW + tr + RESET, W))
    for y, row in enumerate(stage.rows()):
        left = " " if abs(y - geo["door_y"]) <= 1 else vwall   # doorway gap
        lines.append(crop_pad(wc + left + RESET + row + wc + vwall + RESET, W))
    lines.append(crop_pad(wc + bl + hwall * IW + br + RESET, W))
    return lines


# -------------------------------------------------------------- notification

_notified = {}


def _alert(dirname):
    if sys.platform == "darwin":
        # whitelist chars: no quote/backslash escaping games in osascript
        safe = re.sub(r"[^\w .$/-]", "", str(dirname))[:40]
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 'display notification "%s needs you"'
                 ' with title "claude-status-hero"' % safe],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    elif os.name == "nt":
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass


def notify(sessions, enabled):
    if not enabled or (sys.platform != "darwin" and os.name != "nt"):
        return
    now = time.time()
    live = {s.get("sid") for s in sessions}
    for k in list(_notified):       # prune departed sids: unbounded otherwise
        if k not in live:
            _notified.pop(k, None)
    for s in sessions:
        sid = s.get("sid")
        if s["_state"] == "needs_you":
            if now - _notified.get(sid, 0) > 120:
                _notified[sid] = now
                _alert(s.get("dir") or sid)
        else:
            _notified.pop(sid, None)


# --------------------------------------------------------------------- main

def enable_vt():
    """Windows: turn on ANSI/VT processing for stdout (no-op elsewhere).

    PowerShell inside Windows Terminal already has it; legacy conhost and
    some launchers don't — without this the board prints raw escapes there.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)               # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VT_PROCESSING
    except Exception:
        pass


def draw(lines, H):
    # absolute row addressing: a trailing \n on the bottom row would scroll
    # the alt screen every frame on short terminals. ?2026 = synchronized
    # output (flicker guard on WT 1.25+/iTerm2; ignored where unsupported).
    out = ["\x1b[?2026h"]
    for i, ln in enumerate(lines[:H]):
        out.append("\x1b[%d;1H" % (i + 1) + ln + "\x1b[K")
    out.append("\x1b[0J\x1b[?2026l")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


# --------------------------------------------------- auto-launch (opt-in) ---
# One board window that opens itself when you start Claude Code. `--autostart
# on` installs a SessionStart hook that runs `hero_board.py --ensure`; --ensure
# opens a NEW terminal window with the board, but ONLY if one isn't already
# running (singleton, so sessions never stack windows). macOS/iTerm2 for now.
# Fully opt-in — the statusline + board behave exactly as before until you
# turn it on. hero_line.py's own hooks are never touched.

def _shq(s):
    import shlex
    return shlex.quote(s)


def _settings_path():
    return os.environ.get("STATUS_HERO_SETTINGS") or \
        os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _board_already_open():
    """True if a real board process is already running (ignoring the
    short-lived --ensure/--autostart helpers) — the singleton guard."""
    try:
        out = subprocess.run(["pgrep", "-fl", "hero_board.py"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    me = os.getpid()
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if pid == me or "--ensure" in cmd or "--autostart" in cmd:
            continue
        if "hero_board.py" in cmd:
            return True
    return False


def ensure_board(argv):
    """Open the board in a new terminal window if one isn't already up. A safe
    no-op on any failure — it must never block or fail the SessionStart hook."""
    if _board_already_open():
        return 0
    board = os.path.abspath(__file__)
    py = sys.executable or "python3"
    extra = " ".join(a for a in argv if a != "--ensure")
    cmd = ("%s %s %s" % (_shq(py), _shq(board), extra)).strip()
    if sys.platform != "darwin":
        sys.stderr.write("auto-launch is macOS-only for now — run: %s\n" % cmd)
        return 0
    esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        script = ('tell application "iTerm" to create window with default '
                  'profile command "%s"' % esc)
    else:
        script = 'tell application "Terminal" to do script "%s"' % esc
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return 0


def _is_our_autostart(group):
    s = json.dumps(group)
    return "--ensure" in s and "hero_board.py" in s


def autostart(state, mode="--office"):
    """Install / remove the SessionStart hook that auto-opens the board in a
    given mode (default --office). Backs up settings.json, preserves every
    other hook, idempotent."""
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw) if raw.strip() else {}   # empty file == {}
    except FileNotFoundError:
        data = {}
    except Exception as e:
        # genuinely malformed JSON — refuse rather than clobber the user's file
        sys.stderr.write("autostart: cannot parse %s (%s); leaving it untouched\n" % (path, e))
        return 1
    if not isinstance(data, dict):
        sys.stderr.write("autostart: %s is not a JSON object\n" % path)
        return 1
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    ss = hooks.get("SessionStart")
    if not isinstance(ss, list):
        ss = []
    if state == "status":
        print("auto-launch: %s" % ("ON" if any(_is_our_autostart(g) for g in ss) else "off"))
        return 0
    ss = [g for g in ss if not _is_our_autostart(g)]   # idempotent
    if state == "on":
        modeflag = "" if mode == "--scene" else " " + mode   # scene is bare default
        cmd = "%s %s --ensure%s" % (_shq(sys.executable or "python3"),
                                    _shq(os.path.abspath(__file__)), modeflag)
        ss.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    hooks["SessionStart"] = ss
    data["hooks"] = hooks
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(path):
            shutil.copy2(path, path + ".autostart-backup")
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        sys.stderr.write("autostart: write failed: %s\n" % e)
        return 1
    if state == "on":
        print("auto-launch enabled — a board window opens when you start Claude "
              "Code. (Disable: python3 hero_board.py --autostart off)")
    else:
        print("auto-launch disabled.")
    return 0


def main(argv):
    if "--ensure" in argv:
        return ensure_board(argv)
    if "--autostart" in argv:
        i = argv.index("--autostart")
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        state = nxt if nxt in ("on", "off", "status") else "status"
        mode = next((m for m in ("--office", "--list", "--scene") if m in argv), "--office")
        return autostart(state, mode)

    demo = "--demo" in argv
    once = "--once" in argv
    mode = ("office" if "--office" in argv
            else "list" if "--list" in argv else "scene")
    enabled_notify = "--no-notify" not in argv
    fps = 4.0
    if "--fps" in argv:
        try:
            fps = max(1.0, min(30.0, float(argv[argv.index("--fps") + 1])))
        except (ValueError, IndexError):
            pass

    frame = 0
    ostate = {}          # office walk animation state (renderer-local only)

    def one_frame():
        size = shutil.get_terminal_size((100, 30))
        W, H = max(60, size.columns), max(12, size.lines)
        sessions = demo_fleet(frame) if demo else fleet()
        if not demo:
            notify(sessions, enabled_notify)
        if mode == "office":
            lines = render_office(sessions, W - 1, H - 1, frame, demo,
                                  None if once else ostate)
        else:
            rend = render_scene if mode == "scene" else render_list
            lines = rend(sessions, W - 1, H - 1, frame, demo)
        hint = DIM + "  q quit%sm mode%s%s" % (SEP, SEP, mode) + RESET
        lines.append(crop_pad(hint, W - 1))
        return lines, H

    enable_vt()
    if once:
        lines, H = one_frame()
        sys.stdout.write("\n".join(lines) + "\n")
        return 0

    # interactive: alt screen + cbreak keyboard (msvcrt keyboard on Windows)
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
    fd = sys.stdin.fileno() if termios else None
    old_attrs = None
    if termios:
        try:
            old_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            old_attrs = None
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        while True:
            lines, H = one_frame()
            draw(lines, H)
            frame += 1
            ch = None
            if termios and old_attrs is not None:
                r, _, _ = select.select([sys.stdin], [], [], 1.0 / fps)
                if r:
                    ch = sys.stdin.read(1)
            elif msvcrt:
                deadline = time.monotonic() + 1.0 / fps
                while time.monotonic() < deadline:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        break
                    time.sleep(0.02)
            else:
                time.sleep(1.0 / fps)   # no raw keyboard: Ctrl-C to quit
            if ch in ("q", "Q", "\x03"):
                break
            if ch in ("m", "M"):
                mode = {"scene": "list", "list": "office",
                        "office": "scene"}[mode]
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if termios and old_attrs is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
