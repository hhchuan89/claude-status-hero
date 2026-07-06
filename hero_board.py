#!/usr/bin/env python3
"""claude-status-hero · hero_board.py — the game.

A real terminal dashboard for ALL your Claude Code windows. Run it in its own
iTerm2/tmux pane and glance at the whole fleet:

  • one pixel-art animal hero per live session (assigned by hero_line.py)
  • standing on a pillar whose height = that session's context %
  • state beacon: ⚡ working (bobs) · ❗ NEEDS YOU (blinks) · 💤 idle
                  🌀 compacting · 👻 stale/ghost
  • last activity per session ("you: fix the tests…", "Bash: pytest -q…")
  • account-wide 5h / 7d meters + Σ cost in the header
  • macOS notification when any session flips to NEEDS YOU

Usage:
  python3 hero_board.py            # scene mode, 4 fps
  python3 hero_board.py --list     # dense one-row-per-session mode
  python3 hero_board.py --demo     # fake fleet (no Claude Code needed)
  python3 hero_board.py --once     # print a single frame and exit
  --fps N (1-30) · --no-notify · keys: q quit, m toggle mode

Data comes from ~/.claude/status-hero/sessions/*.json, written by
hero_line.py (statusline + hooks). Install that first.

Python ≥3.9, stdlib only. MIT.
"""

import json
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
TRUECOLOR = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")

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


def sanitize(s, limit=120):
    if not isinstance(s, str):
        return ""
    s = CTRL_STRIP.sub("", ANSI_TOKEN.sub("", s)).strip()
    if len(s) > limit:
        s = s[: limit - 2] + ".."
    return s


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

G_SUM = "sum " if ASCII else "Σ "
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


def sprite_lines(hero, frame, mood):
    """Render a hero as 4 terminal rows of half-blocks, exactly SPRITE_W wide."""
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
                cells.append("#" if (top or bot) else " ")
            elif top and bot:
                cells.append(px_fg(top) + px_bg(bot) + "▀" + RESET)
            elif top:
                cells.append(px_fg(top) + "▀" + RESET)
            elif bot:
                cells.append(px_fg(bot) + "▄" + RESET)
            else:
                cells.append(" ")
        rows.append("".join(cells))
    return rows


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
        if not s or not s.get("sid"):
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
    return [
        mk(0, "sightlab", "working", 52, 31.26, "Bash: pytest -q tests/"),
        mk(1, "invest-sight", "needs_you", 18, 4.10, "permission: Edit main.py"),
        mk(2, "daily-news", "idle", 91, 12.55, "you: push the brief"),
        mk(3, "onchain-sight", "working", 34, 8.02, "Read: abi/router.json"),
        mk(4, "radar", "compacting", 96, 22.40, "compacting context"),
    ]


# ----------------------------------------------------------------- renderer

def header(sessions, W, demo):
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


# -------------------------------------------------------------- notification

_notified = {}


def notify(sessions, enabled):
    if not enabled or sys.platform != "darwin":
        return
    now = time.time()
    for s in sessions:
        sid = s.get("sid")
        if s["_state"] == "needs_you":
            if now - _notified.get(sid, 0) > 120:
                _notified[sid] = now
                # whitelist chars: no quote/backslash escaping games in osascript
                safe = re.sub(r"[^\w .$/-]", "", str(s.get("dir") or sid))[:40]
                try:
                    subprocess.Popen(
                        ["osascript", "-e",
                         'display notification "%s needs you"'
                         ' with title "claude-status-hero"' % safe],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        else:
            _notified.pop(sid, None)


# --------------------------------------------------------------------- main

def draw(lines, H):
    out = ["\x1b[H"]
    for ln in lines[:H]:
        out.append(ln + "\x1b[K\n")
    out.append("\x1b[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def main(argv):
    demo = "--demo" in argv
    once = "--once" in argv
    mode = "list" if "--list" in argv else "scene"
    enabled_notify = "--no-notify" not in argv
    fps = 4.0
    if "--fps" in argv:
        try:
            fps = max(1.0, min(30.0, float(argv[argv.index("--fps") + 1])))
        except (ValueError, IndexError):
            pass

    frame = 0

    def one_frame():
        size = shutil.get_terminal_size((100, 30))
        W, H = max(60, size.columns), max(12, size.lines)
        sessions = demo_fleet(frame) if demo else fleet()
        if not demo:
            notify(sessions, enabled_notify)
        rend = render_scene if mode == "scene" else render_list
        lines = rend(sessions, W - 1, H - 1, frame, demo)
        hint = DIM + "  q quit%sm mode%s%s" % (SEP, SEP,
               "scene" if mode == "scene" else "list") + RESET
        lines.append(crop_pad(hint, W - 1))
        return lines, H

    if once:
        lines, H = one_frame()
        sys.stdout.write("\n".join(lines) + "\n")
        return 0

    # interactive: alt screen + cbreak keyboard (keyboard optional on Windows)
    try:
        import termios
        import tty
    except ImportError:
        termios = tty = None
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
            if termios and old_attrs is not None:
                r, _, _ = select.select([sys.stdin], [], [], 1.0 / fps)
                if r:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q", "\x03"):
                        break
                    if ch in ("m", "M"):
                        mode = "list" if mode == "scene" else "scene"
            else:
                time.sleep(1.0 / fps)   # no raw keyboard: Ctrl-C to quit
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
