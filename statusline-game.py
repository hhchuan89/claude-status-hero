#!/usr/bin/env python
# Claude Code statusline — "Claude Quest" edition.
# A tiny 1-fps retro side-scroller rendered as your statusline: a colored pixel
# hero (Mario, Pikachu, and friends) walks a track whose length is your Claude
# Pro/Max 5-hour usage. It bobs once a second, hustles in the yellow zone, and
# JUMPS in the red zone. Below it: the 7-day window + context, and a HUD row.
#
# ---- CHOOSE YOUR HERO ----
#   python ~/.claude/statusline-game.py --list          # see all heroes
#   python ~/.claude/statusline-game.py --set pika       # pick one (writes a config file)
#   python ~/.claude/statusline-game.py --current        # show the current pick
#   python ~/.claude/statusline-game.py --demo goomba    # preview a hero in your terminal
# Selection priority:  CLAUDE_SL_SPRITE env var  >  statusline-hero.txt  >  default (mario).
# So one file serves everyone: each person runs --set once, no code editing, no extra files.
#
# Honest animation model (see code.claude.com/docs/en/statusline):
#   The statusline re-runs on events (each assistant message, debounced 300ms)
#   PLUS an optional refreshInterval timer whose MINIMUM is 1 second. So the
#   fastest animation possible is ~1 frame/second. Position (usage) carries the
#   meaning; the 1-fps bob is just "I'm alive". Set "refreshInterval": 1 in
#   settings.json to keep the bob ticking while the session is idle.
#
# Windows-hardened: forces UTF-8 stdio + newline="" so Windows Python doesn't
# turn \n into \r\n and corrupt the multi-row output.

import sys
import os
import re
import json
import time
import subprocess

# ---- CONFIG (the knobs) ------------------------------------------------------
DEFAULT_HERO = "mario"
MOTION = (os.environ.get("CLAUDE_SL_MOTION") or "usage").strip().lower()
#   "usage" = stand at your 5h % (meaningful) ·  "march" = run left->right on the clock (fun)
TRACK_MIN, TRACK_MAX = 22, 40      # ground-track width clamps (chars)
SHOW_SKY = True                    # flat style: coins + goal-flag decoration row
YELLOW_AT, RED_AT = 70, 90         # zone thresholds (percent)
SCENE_PX = 12                      # summit style: scene height in px (even; 12 = 6 rows)
MIN_PEAK = 2                       # summit style: min mountain height so it's never flat
HERO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-hero.txt")
STYLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-style.txt")
STYLES = ("flat", "summit", "fleet")   # flat=track · summit=mountain · fleet=one hero per window
FLEET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet")
FLEET_MAX = 8                      # max windows shown
FLEET_ALIVE = 120                  # seconds; stale files dropped (open windows refresh every ~1s)
HERO_SLOTS = ("mario", "pika", "ghost", "goomba", "slime", "avatar", "robot", "alien")
PIXELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-pixels.txt")
PIXELS = ("half", "sext")          # half = 1x2 px/cell (safe) · sext = 2x3 px/cell (finer, prettier)
SIZE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-size.txt")
SIZES = {"small": 12, "medium": 16, "large": 20}   # summit scene height in px (rows = px/2)
THEME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-theme.txt")
THEMES = ("day", "cyber")          # day = painted sky · cyber = black bg, white mountain, neon
# ------------------------------------------------------------------------------

try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="")
except Exception:
    pass

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def fg(n):
    return "\033[38;5;%dm" % n


# ---- hero roster (7 wide x 8 tall pixel grids). '.' = transparent. -----------
PAL = {
    "R": 196, "S": 223, "B": 94, "o": 20, "w": 231, "k": 16,
    "Y": 226, "C": 211, "G": 46, "c": 51, "g": 40, "m": 121,
    "e": 250, "p": 141,
}

HEROES = {
    "mario": {
        "label": "🍄 red-hat plumber",
        "a": ["..RRR..", ".RRRRR.", ".BSSSk.", ".BSSSS.", ".RRRRR.", "wRRRRRw", ".ooooo.", "..B.B.."],
        "b": ["..RRR..", ".RRRRR.", ".BSSSk.", ".BSSSS.", ".RRRRR.", "wRRRRRw", ".ooooo.", ".B...B."],
        "jump": ["w.RRR.w", ".RRRRR.", ".BSSSk.", ".BSSSS.", ".RRRRR.", ".ooooo.", "..B.B..", "......."],
    },
    "pika": {
        "label": "⚡ electric mouse",
        "a": ["k.....k", "Yk...kY", ".YYYYY.", ".YkYkY.", "CYYYYYC", ".YYYYY.", ".Y...Y.", "..B.B.."],
        "b": ["k.....k", "Yk...kY", ".YYYYY.", ".YkYkY.", "CYYYYYC", ".YYYYY.", ".Y...Y.", ".B...B."],
        "jump": ["k.....k", "Yk.k.kY", ".YYYYY.", ".YkYkY.", "CYYYYYC", ".YYYYY.", "..YYY..", "......."],
    },
    "ghost": {
        "label": "👻 cyan spook",
        "a": ["..ccc..", ".ccccc.", "cckcckc", "ccccccc", "ccccccc", "ccccccc", "c.c.c.c", "......."],
        "b": ["..ccc..", ".ccccc.", "cckcckc", "ccccccc", "ccccccc", "ccccccc", ".c.c.c.", "......."],
        "jump": ["..ccc..", ".ccccc.", "cckcckc", "ccccccc", "ccccccc", "ccccccc", "ccccccc", "......."],
    },
    "goomba": {
        "label": "🟤 angry mushroom",
        "a": ["..BBB..", ".BBBBB.", "BBBBBBB", "BkBBBkB", "BBBBBBB", ".BBBBB.", "..w.w..", "......."],
        "b": ["..BBB..", ".BBBBB.", "BBBBBBB", "BkBBBkB", "BBBBBBB", ".BBBBB.", ".w...w.", "......."],
        "jump": ["..BBB..", ".BBBBB.", "BBBBBBB", "BkBBBkB", "BBBBBBB", ".BBBBB.", "..www..", "......."],
    },
    "slime": {
        "label": "🟢 rpg blob",
        "a": [".......", "..ggg..", ".ggggg.", "ggggggg", "gkgggkg", "ggggggg", "ggggggg", "......."],
        "b": [".......", ".......", "..ggg..", ".ggggg.", "gkgggkg", "ggggggg", "ggggggg", "......."],
        "jump": ["..ggg..", ".ggggg.", "ggggggg", "gkgggkg", "ggggggg", "ggggggg", ".ggggg.", "......."],
    },
    "avatar": {
        "label": "🟩 your mint-green pixel face",
        "a": [".mmmmm.", "mmmmmmm", "mkmmmkm", "mmmmmmm", "mmwwwmm", "mmmmmmm", ".mmmmm.", "......."],
        "b": [".mmmmm.", "mmmmmmm", "mkmmmkm", "mmmmmmm", "mmmwmmm", "mmmmmmm", ".mmmmm.", "......."],
        "jump": ["m.....m", ".mmmmm.", "mkmmmkm", "mmmmmmm", "mmwwwmm", "mmmmmmm", ".mmmmm.", "......."],
    },
    "robot": {
        "label": "🤖 steel bot",
        "a": ["...c...", "...e...", ".eeeee.", ".ekeke.", ".eeeee.", "eeeeeee", "e.e.e.e", ".e...e."],
        "b": ["...c...", "...e...", ".eeeee.", ".ekeke.", ".eeeee.", "eeeeeee", "e.e.e.e", "e.....e"],
        "jump": ["c..c..c", "...e...", ".eeeee.", ".ekeke.", ".eeeee.", "eeeeeee", ".e...e.", "......."],
    },
    "alien": {
        "label": "👾 violet alien",
        "a": ["..ppp..", ".ppppp.", "pkpppkp", ".ppppp.", ".ppppp.", "..ppp..", ".p...p.", ".p...p."],
        "b": ["..ppp..", ".ppppp.", "pkpppkp", ".ppppp.", ".ppppp.", "..ppp..", ".p...p.", "p.....p"],
        "jump": ["p.ppp.p", ".ppppp.", "pkpppkp", ".ppppp.", ".ppppp.", "..ppp..", ".p...p.", "......."],
    },
}

# ---- detailed 12x16 hero for the summit scene (recognisable + a jump pose) ----
# Only the plumber for now; other heroes fall back to their 7x8 grid.
HERO_BIG = {
    "mario": {
        # Silhouette-first: hard red-cap / red-shirt vs blue-overalls split,
        # one eye dot, no facial noise (readability > detail at this size).
        "stand": [
            "....RRRR....",
            "...RRRRRRR..",
            "..RRRRRRRRR.",
            "..RSSSSSSR..",
            "..SSSkSSSS..",
            "..SSSSSSSS..",
            "...RRRRRR...",
            "..RRRRRRRR..",
            ".wRRRRRRRRw.",
            ".wRRRRRRRRw.",
            "..oooooooo..",
            "..oYooooYo..",
            "..oooooooo..",
            "..oooooooo..",
            "..oo....oo..",
            ".BBB....BBB.",
        ],
        "jump": [
            "....RRRR....",
            "...RRRRRRR..",
            "..RRRRRRRRR.",
            "..RSSSSSSR..",
            "..SSSkSSSS..",
            "..SSSSSSSS..",
            "w..RRRRRR..w",
            "wRRRRRRRRRRw",
            "..RRRRRRRR..",
            "..oooooooo..",
            "..oYooooYo..",
            "..oooooooo..",
            "..oooooooo..",
            "...oooooo...",
            "...oooooo...",
            "..BBBBBBBB..",
        ],
    },
}

def resolve_hero():
    """env var  >  config file  >  default; unknown names fall back to default."""
    env = (os.environ.get("CLAUDE_SL_SPRITE") or "").strip().lower()
    if env in HEROES:
        return env
    try:
        with open(HERO_FILE, encoding="utf-8") as f:
            name = f.read().strip().lower()
            if name in HEROES:
                return name
    except Exception:
        pass
    return DEFAULT_HERO


def resolve_style():
    """env var  >  config file  >  default (flat)."""
    env = (os.environ.get("CLAUDE_SL_STYLE") or "").strip().lower()
    if env in STYLES:
        return env
    try:
        with open(STYLE_FILE, encoding="utf-8") as f:
            s = f.read().strip().lower()
            if s in STYLES:
                return s
    except Exception:
        pass
    return "flat"


def resolve_pixels():
    """env var  >  config file  >  default (half = safest). Summit only."""
    env = (os.environ.get("CLAUDE_SL_PIXELS") or "").strip().lower()
    if env in PIXELS:
        return env
    try:
        with open(PIXELS_FILE, encoding="utf-8") as f:
            p = f.read().strip().lower()
            if p in PIXELS:
                return p
    except Exception:
        pass
    return "half"


def resolve_size():
    """env var  >  config file  >  default (medium). Returns scene height in px."""
    env = (os.environ.get("CLAUDE_SL_SIZE") or "").strip().lower()
    if env in SIZES:
        return SIZES[env]
    try:
        with open(SIZE_FILE, encoding="utf-8") as f:
            s = f.read().strip().lower()
            if s in SIZES:
                return SIZES[s]
    except Exception:
        pass
    return SIZES["medium"]


def _size_name():
    px = resolve_size()
    for k, v in SIZES.items():
        if v == px:
            return k
    return str(px)


def resolve_theme():
    """env var  >  config file  >  default (cyber)."""
    env = (os.environ.get("CLAUDE_SL_THEME") or "").strip().lower()
    if env in THEMES:
        return env
    try:
        with open(THEME_FILE, encoding="utf-8") as f:
            t = f.read().strip().lower()
            if t in THEMES:
                return t
    except Exception:
        pass
    return "cyber"


# ---- helpers -----------------------------------------------------------------
def get(data, *path):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def zone(v):
    n = to_num(v)
    if n is None:
        return ("dim", 244)
    if n >= RED_AT:
        return ("red", 196)
    if n >= YELLOW_AT:
        return ("yellow", 214)
    return ("green", 46)


def fmt_reset(ts):
    n = to_num(ts)
    if n is None:
        return ""
    diff = max(0, int(n - time.time()))
    h, m = diff // 3600, (diff % 3600) // 60
    if h >= 24:
        return "%dd %dh" % (h // 24, h % 24)
    return "%dh %dm" % (h, m)


def fmt_dur(ms):
    n = to_num(ms)
    if n is None:
        return ""
    s = int(n // 1000)
    h, m = s // 3600, (s % 3600) // 60
    return ("%dh %dm" % (h, m)) if h else ("%dm" % m)


def sprite_rows(grid, left_pad):
    """Render an 8px-tall grid to 4 terminal rows using half-blocks."""
    pad = " " * max(0, left_pad)
    out = []
    for t in range(0, len(grid), 2):
        top = grid[t]
        bot = grid[t + 1] if t + 1 < len(grid) else "." * len(top)
        row = []
        for x in range(len(top)):
            ct = PAL.get(top[x])
            cb = PAL.get(bot[x])
            if ct is None and cb is None:
                row.append(" ")
            elif ct is not None and cb is not None:
                row.append("\033[38;5;%d;48;5;%dm▀%s" % (ct, cb, RESET))
            elif ct is not None:
                row.append("%s▀%s" % (fg(ct), RESET))
            else:
                row.append("%s▄%s" % (fg(cb), RESET))
        out.append(pad + "".join(row))
    return out


def git_branch(cwd):
    """Current branch, cached ~10s in TEMP. Without this, refreshInterval:1 would
    spawn a git process every second per session — slow in a big repo tree, and
    slow renders get cancelled mid-run (a blank statusline). Cache once, read fast."""
    if not (cwd and os.path.isdir(cwd)):
        return ""
    cache_dir = os.path.join(os.environ.get("TEMP") or "/tmp", ".claude-sl-branch")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        pass
    cf = os.path.join(cache_dir, re.sub(r"[^A-Za-z0-9]", "_", cwd))
    try:
        if os.path.isfile(cf) and (time.time() - os.path.getmtime(cf)) < 10:
            with open(cf, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    try:
        b = subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        b = ""
    try:
        with open(cf, "w", encoding="utf-8") as f:
            f.write(b)
    except Exception:
        pass
    return b


def track(used, width):
    _, col = zone(used)
    n = to_num(used) or 0.0
    filled = max(0, min(width, int(round(n * width / 100))))
    return "%s%s%s%s" % (fg(col), "▓" * filled, DIM + fg(238), "░" * (width - filled)) + RESET


def lerp(a, b, t):
    return a + (b - a) * t


def _hud(data):
    """Shared top row: dir [model] ⎇branch  ⏱session-time  $cost."""
    model = get(data, "model", "display_name")
    cwd = get(data, "workspace", "current_dir") or get(data, "cwd")
    dirname = os.path.basename(cwd) if cwd else "?"
    hud = "%s%s%s" % (BOLD, dirname, RESET)
    if model:
        hud += " %s[%s]%s" % (DIM, model, RESET)
    branch = git_branch(cwd)
    if branch:
        hud += " %s⎇ %s%s" % (fg(45), branch, RESET)
    d = fmt_dur(get(data, "cost", "total_duration_ms"))
    if d:
        hud += "  %s⏱ %s%s" % (DIM, d, RESET)
    c = to_num(get(data, "cost", "total_cost_usd"))
    if c is not None:
        hud += "  %s$%.2f%s" % (DIM, c, RESET)
    return hud


# ---- the statusline ----------------------------------------------------------
def render_statusline(data, hero):
    model = get(data, "model", "display_name")
    cwd = get(data, "workspace", "current_dir") or get(data, "cwd")
    ctx = get(data, "context_window", "used_percentage")
    cost = get(data, "cost", "total_cost_usd")
    dur = get(data, "cost", "total_duration_ms")
    five = get(data, "rate_limits", "five_hour", "used_percentage")
    five_r = get(data, "rate_limits", "five_hour", "resets_at")
    week = get(data, "rate_limits", "seven_day", "used_percentage")
    week_r = get(data, "rate_limits", "seven_day", "resets_at")

    dirname = os.path.basename(cwd) if cwd else "?"
    branch = git_branch(cwd)

    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except Exception:
        cols = 0
    track_w = max(TRACK_MIN, min(TRACK_MAX, (cols or 80) - 22))

    try:
        frame = int(os.environ.get("CLAUDE_SL_FRAME", int(time.time())))
    except Exception:
        frame = int(time.time())
    step = frame % 2

    run_pct = to_num(five)
    if run_pct is None:
        run_pct = to_num(ctx)      # fall back to context on API/Console billing
    if run_pct is None:
        run_pct = 0.0
    jumping = run_pct >= RED_AT
    hero_grids = HEROES.get(hero, HEROES[DEFAULT_HERO])
    span = max(0, track_w - 7)
    if MOTION == "march" and not jumping:
        pos = frame % (span + 1)                    # run left->right, 1 step/sec, wraps
    else:
        pos = int(run_pct / 100.0 * span)           # stand at your 5h usage %
    if jumping:
        grid = hero_grids["jump"]
    else:
        grid = hero_grids["a"] if step == 0 else hero_grids["b"]
        if step == 1:
            grid = ["......."] + list(grid[:-1])     # 1px hop so the bob is actually visible

    out = []

    # HUD
    hud = "%s%s%s" % (BOLD, dirname, RESET)
    if model:
        hud += " %s[%s]%s" % (DIM, model, RESET)
    if branch:
        hud += " %s⎇ %s%s" % (fg(45), branch, RESET)
    d = fmt_dur(dur)
    if d:
        hud += "  %s⏱ %s%s" % (DIM, d, RESET)
    if to_num(cost) is not None:
        hud += "  %s$%.2f%s" % (DIM, to_num(cost), RESET)
    out.append(hud)

    # sky: coins + goal flag
    if SHOW_SKY:
        cells = []
        for i in range(track_w):
            cells.append("%s•%s" % (fg(226), fg(220)) if (6 <= i < track_w - 2 and i % 7 == 0) else " ")
        out.append(fg(220) + "".join(cells) + RESET + " 🚩")

    # the runner (jump = float up a row)
    sr = sprite_rows(grid, pos)
    if jumping:
        out.append("")
        out.extend(sr[:3])
    else:
        out.extend(sr)

    # ground track = 5h
    _, col5 = zone(five)
    five_txt = ("%d%%" % round(to_num(five))) if to_num(five) is not None else "--"
    r5 = fmt_reset(five_r)
    ground = "%s5h%s %s %s%s%s" % (BOLD + fg(col5), RESET, track(five, track_w), fg(col5), five_txt, RESET)
    if r5:
        ground += " %s↻ %s%s" % (DIM, r5, RESET)
    out.append(ground)

    # 7-day + context
    line = []
    if to_num(week) is not None:
        _, c7 = zone(week)
        seg = "%s7d%s %s %s%d%%%s" % (BOLD + fg(c7), RESET, track(week, 12), fg(c7), round(to_num(week)), RESET)
        r7 = fmt_reset(week_r)
        if r7:
            seg += " %s↻ %s%s" % (DIM, r7, RESET)
        line.append(seg)
    if to_num(ctx) is not None:
        _, cc = zone(ctx)
        line.append("%sctx%s %s %s%d%%%s" % (DIM, RESET, track(ctx, 10), fg(cc), round(to_num(ctx)), RESET))
    if line:
        out.append(("  %s|%s  " % (DIM, RESET)).join(line))

    return "\n".join(out) + "\n"


# ---- SUMMIT style: a calm, STATIC, true-colour mountain scene ----------------
TCPAL = {
    "R": (229, 49, 43), "S": (244, 201, 154), "B": (138, 90, 18), "o": (35, 64, 200),
    "w": (245, 247, 251), "k": (24, 24, 28), "Y": (245, 197, 24), "C": (255, 143, 176),
    "c": (31, 212, 212), "g": (55, 194, 90), "m": (116, 224, 190),
    "e": (159, 176, 200), "p": (180, 124, 255),
}


def lerp3(a, b, t):
    t = max(0.0, min(1.0, t))
    return (int(round(a[0] + (b[0] - a[0]) * t)),
            int(round(a[1] + (b[1] - a[1]) * t)),
            int(round(a[2] + (b[2] - a[2]) * t)))


def render_canvas_tc(canvas):
    """Half-block render of a TRUE-COLOUR canvas ((r,g,b) tuples or None per cell)."""
    H = len(canvas)
    W = len(canvas[0]) if H else 0
    rows = []
    for t in range(0, H, 2):
        top = canvas[t]
        bot = canvas[t + 1] if t + 1 < H else [None] * W
        cells = []
        for x in range(W):
            ct, cb = top[x], bot[x]
            if ct is None and cb is None:
                cells.append(" ")
            elif ct is not None and cb is not None:
                cells.append("\033[38;2;%d;%d;%d;48;2;%d;%d;%dm▀\033[0m" % (ct + cb))
            elif ct is not None:
                cells.append("\033[38;2;%d;%d;%dm▀\033[0m" % ct)
            else:
                cells.append("\033[38;2;%d;%d;%dm▄\033[0m" % cb)
        rows.append("".join(cells))
    return rows


# Sextant glyphs (U+1FB00..) pack a 2-wide x 3-tall pixel grid into one cell.
# Bit weights:  TL=1 TR=2 / ML=4 MR=8 / BL=16 BR=32.  Two colours per cell.
def _sextant_char(v):
    if v == 0:
        return None
    if v == 63:
        return "█"          # full block
    if v == 21:
        return "▌"          # left half
    if v == 42:
        return "▐"          # right half
    off = v - 1
    if v > 21:
        off -= 1
    if v > 42:
        off -= 1
    return chr(0x1FB00 + off)


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _d2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def render_sextant(canvas):
    """Render a true-colour canvas (PH rows x PW cols, PH%3==0, PW%2==0) with
    sextant glyphs: 2 colours per cell, so each cell holds a 2x3 pixel pattern."""
    PH = len(canvas)
    PW = len(canvas[0]) if PH else 0
    wts = (1, 2, 4, 8, 16, 32)
    offs = ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1))
    rows = []
    for cy in range(0, PH, 3):
        line = []
        for cx in range(0, PW, 2):
            six = []
            for dy, dx in offs:
                yy, xx = cy + dy, cx + dx
                six.append(canvas[yy][xx] if (yy < PH and xx < PW) else None)
            opaque = [c for c in six if c is not None]
            if not opaque:                           # all transparent -> terminal bg
                line.append(" ")
                continue
            if len(opaque) < 6:                      # some transparent: fg = lit pixels, bg shows through
                mask = 0
                fs = [0, 0, 0]
                n = 0
                for i, c in enumerate(six):
                    if c is not None:
                        mask |= wts[i]
                        fs[0] += c[0]; fs[1] += c[1]; fs[2] += c[2]; n += 1
                fg = (fs[0] // n, fs[1] // n, fs[2] // n)
                ch = _sextant_char(mask)
                if ch == "█" or ch is None:
                    line.append("\033[38;2;%d;%d;%dm█\033[0m" % fg)
                else:
                    line.append("\033[38;2;%d;%d;%dm%s\033[0m" % (fg + (ch,)))
                continue
            lums = [_lum(c) for c in six]
            hi = six[lums.index(max(lums))]
            lo = six[lums.index(min(lums))]
            if _d2(hi, lo) < 500:                    # near-uniform cell -> solid block
                line.append("\033[38;2;%d;%d;%dm█\033[0m" % six[0])
                continue
            mask = 0
            fs = [0, 0, 0]
            fn = 0
            bs = [0, 0, 0]
            bn = 0
            for i, c in enumerate(six):
                if _d2(c, hi) <= _d2(c, lo):
                    mask |= wts[i]
                    fs[0] += c[0]; fs[1] += c[1]; fs[2] += c[2]; fn += 1
                else:
                    bs[0] += c[0]; bs[1] += c[1]; bs[2] += c[2]; bn += 1
            fg = (fs[0] // fn, fs[1] // fn, fs[2] // fn) if fn else hi
            bg = (bs[0] // bn, bs[1] // bn, bs[2] // bn) if bn else lo
            ch = _sextant_char(mask)
            if ch is None:
                line.append("\033[38;2;%d;%d;%dm█\033[0m" % bg)
            else:
                line.append("\033[38;2;%d;%d;%d;48;2;%d;%d;%dm%s\033[0m" % (fg + bg + (ch,)))
        rows.append("".join(line))
    return rows


def _build_scene(fivev, ctxv, weekv, hero, PW, PH, theme, frame):
    """Paint the scene into a PW x PH true-colour canvas (None = transparent black).
    theme 'day' = painted sky · 'cyber' = black bg, white mountain, neon.
    frame drives a 1 fps jump. Returns (canvas, coins_collected, coins_total)."""
    baseY = PH - 1
    peakCol = max(1, int(PW * 0.90))
    peakH = int(round((0.14 + 0.60 * ctxv / 100.0) * (PH - 1)))
    peakH = max(2, min(PH - 2, peakH))

    def tt(x):
        xx = min(max(0, x), peakCol)
        return baseY - int(round(peakH * xx / peakCol))

    # hero grid + jump pose (frame % 2 -> a 1 fps hop)
    jumping = (frame % 2 == 1)
    big = HERO_BIG.get(hero)
    if big:
        grid = big["jump"] if jumping else big["stand"]
    else:
        grid = HEROES.get(hero, HEROES[DEFAULT_HERO])["a"]
    gh, gw = len(grid), len(grid[0])
    hero_scale = max(1, int(round(PH * 0.60 / gh)))
    hw_px = gw * hero_scale
    heroCol = max(0, min(PW - hw_px, int(round(fivev / 100.0 * (peakCol - hw_px)))))
    lift = (PH // 5) if jumping else 0
    coin_fx = (0.14, 0.28, 0.42, 0.56, 0.70, 0.85)
    cr = max(1, PH // 9)

    if theme == "cyber":
        cv = [[None] * PW for _ in range(PH)]
        # synthwave retro-sun: amber top -> hot-magenta bottom, horizontal scan-gaps
        # across the lower half, wrapped in a dim neon halo (fake glow into the black).
        sunx, suny = int(PW * 0.62), int(PH * 0.36)
        sunr = max(3, int(PH * 0.32))
        halo = max(1, sunr // 3)
        gap = max(2, sunr // 3)
        for dy in range(-sunr - halo, sunr + halo + 1):
            for dx in range(-sunr - halo, sunr + halo + 1):
                d2 = dx * dx + dy * dy
                yy, xx = suny + dy, sunx + dx
                if not (0 <= yy < PH and 0 <= xx < PW):
                    continue
                t = max(0.0, min(1.0, (dy + sunr) / (2.0 * sunr)))
                if d2 <= sunr * sunr:
                    if dy > sunr * 0.15 and int(dy - sunr * 0.15) % gap == 0:
                        continue                                   # transparent scan slit
                    cv[yy][xx] = lerp3((255, 214, 92), (255, 44, 150), t)
                elif d2 <= (sunr + halo) * (sunr + halo) and cv[yy][xx] is None:
                    cv[yy][xx] = lerp3((26, 6, 28), (58, 12, 52), t)   # dim halo glow
        # white mountain with a neon-cyan crest rim + faint crest glow above it
        for x in range(PW):
            tp = tt(x)
            gy = tp - 1
            if 0 <= gy < PH and cv[gy][x] is None:
                cv[gy][x] = (12, 66, 74)                            # cyan glow above the ridge
            for y in range(tp, baseY):
                if y == tp:
                    cv[y][x] = (120, 246, 255)                     # bright neon crest
                else:
                    v = 236 - int(44 * (y - tp) / max(1, peakH))
                    cv[y][x] = (v, v, min(255, v + 14))
        # neon grid ground (cyan) + faint verticals
        for x in range(PW):
            cv[baseY][x] = (40, 235, 235)
        for gx in range(0, PW, max(3, PW // 12)):
            for gy in (baseY - 2, baseY - 1):
                if 0 <= gy < PH and cv[gy][gx] is None:
                    cv[gy][gx] = (18, 120, 120)
        coin_col, flag_col, pole_col = (250, 215, 50), (255, 60, 90), (210, 225, 255)
    else:
        wk = weekv / 100.0
        sky_top = lerp3((116, 180, 214), (9, 13, 34), wk)
        sky_bot = lerp3((186, 214, 236), (26, 40, 70), wk)
        cv = [[lerp3(sky_top, sky_bot, min(1.0, y / float(max(1, baseY - 1)))) for _ in range(PW)]
              for y in range(PH)]
        sunx = int(round(lerp(PW * 0.06, PW * 0.86, wk)))
        suny = int(round(lerp(PH * 0.14, PH * 0.5, wk)))
        sun_col = (232, 238, 252) if weekv > 78 else lerp3((255, 238, 150), (255, 138, 70), min(1.0, wk * 1.4))
        sunr = max(1, PH // 7)
        for dy in range(-2 * sunr, 2 * sunr + 1):
            for dx in range(-2 * sunr, 2 * sunr + 1):
                yy, xx = suny + dy, sunx + dx
                if not (0 <= yy < PH and 0 <= xx < PW):
                    continue
                d = dx * dx + dy * dy
                if d <= sunr * sunr:
                    cv[yy][xx] = sun_col
                elif d <= 4 * sunr * sunr:
                    cv[yy][xx] = lerp3(cv[yy][xx], sun_col, 0.30)
        if weekv > 66:
            for fx, fyf in ((0.07, 0.14), (0.19, 0.05), (0.34, 0.22), (0.52, 0.09), (0.68, 0.03), (0.82, 0.18)):
                xx, yy = int(fx * PW), int(fyf * PH)
                if 0 <= xx < PW and 0 <= yy < PH:
                    cv[yy][xx] = (236, 238, 245)
        elif weekv < 55:
            for cxf, cyf, cwf in ((0.24, 0.17, 0.11), (0.62, 0.10, 0.15)):
                ccx, ccy, cw = int(cxf * PW), int(cyf * PH), max(2, int(cwf * PW))
                ch = max(1, cw // 3)
                for dx in range(-cw, cw + 1):
                    for dy in range(-ch, ch + 1):
                        if (dx * dx) / float(cw * cw) + (dy * dy) / float(ch * ch) <= 1.0:
                            yy, xx = ccy + dy, ccx + dx
                            if 0 <= yy < PH and 0 <= xx < PW:
                                cv[yy][xx] = lerp3(cv[yy][xx], (248, 250, 253), 0.8)
        for x in range(PW):
            tp = tt(x)
            for y in range(tp, baseY):
                hf = (baseY - y) / float(max(1, peakH))
                if hf > 0.72:
                    snow = (232, 150, 150) if ctxv >= RED_AT else (242, 245, 250)
                    col = lerp3((214, 224, 236), snow, (hf - 0.72) / 0.28)
                elif hf > 0.36:
                    col = lerp3((104, 74, 42), (150, 104, 58), (hf - 0.36) / 0.36)
                else:
                    col = lerp3((44, 104, 60), (72, 150, 88), hf / 0.36)
                cv[y][x] = col
        for x in range(PW):
            cv[baseY][x] = (40, 33, 24)
        coin_col, flag_col, pole_col = (245, 200, 60), (229, 72, 77), (222, 216, 202)

    # coins (shared) — collected once the hero passes
    coins_got = 0
    for fx in coin_fx:
        cx = int(fx * PW)
        if cx <= heroCol + hw_px:
            coins_got += 1
            continue
        cyc = tt(cx) - cr - 2
        for dy in range(-cr, cr + 1):
            for dx in range(-cr, cr + 1):
                if dx * dx + dy * dy <= cr * cr:
                    yy, xx = cyc + dy, cx + dx
                    if 0 <= yy < baseY and 0 <= xx < PW:
                        cv[yy][xx] = coin_col

    # flag (shared) — pole + triangle at the summit
    fpx = peakCol
    fyb = tt(fpx)
    fph = max(3, PH // 3)
    for i in range(fph):
        yy = fyb - 1 - i
        if 0 <= yy < PH and 0 <= fpx < PW:
            cv[yy][fpx] = pole_col
    tw = max(2, PW // 16)
    for i in range(tw):
        for j in range(tw - i):
            yy, xx = fyb - fph + i, fpx + 1 + j
            if 0 <= yy < PH and 0 <= xx < PW:
                cv[yy][xx] = flag_col

    # hero (shared) — lifts on the jump frame
    groundY = tt(heroCol + hw_px // 2)
    topY = max(0, groundY - gh * hero_scale - lift)
    for ry in range(gh):
        for rx in range(gw):
            c = TCPAL.get(grid[ry][rx])
            if c is None:
                continue
            for sy in range(hero_scale):
                for sx in range(hero_scale):
                    yy, xx = topY + ry * hero_scale + sy, heroCol + rx * hero_scale + sx
                    if 0 <= yy < PH and 0 <= xx < PW:
                        cv[yy][xx] = c

    return cv, coins_got, len(coin_fx)


def render_summit(data, hero):
    ctx = get(data, "context_window", "used_percentage")
    five = get(data, "rate_limits", "five_hour", "used_percentage")
    five_r = get(data, "rate_limits", "five_hour", "resets_at")
    week = get(data, "rate_limits", "seven_day", "used_percentage")
    week_r = get(data, "rate_limits", "seven_day", "resets_at")

    fivev = to_num(five)
    if fivev is None:
        fivev = to_num(ctx)                 # API/Console billing: climb by context instead
    if fivev is None:
        fivev = 0.0
    ctxv = to_num(ctx) or 0.0
    weekv = to_num(week) or 0.0

    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except Exception:
        cols = 0
    try:
        frame = int(os.environ.get("CLAUDE_SL_FRAME", int(time.time())))
    except Exception:
        frame = int(time.time())
    theme = resolve_theme()
    W = max(30, min(46, (cols or 80) - 8))
    rows_scene = max(4, resolve_size() // 2)               # terminal rows for the scene
    if resolve_pixels() == "sext":
        canvas, coins_got, coin_total = _build_scene(fivev, ctxv, weekv, hero, W * 2, rows_scene * 3, theme, frame)
        scene = render_sextant(canvas)
    else:
        canvas, coins_got, coin_total = _build_scene(fivev, ctxv, weekv, hero, W, rows_scene * 2, theme, frame)
        scene = render_canvas_tc(canvas)
    out = [_hud(data)] + scene

    # stats row (exact numbers, never lost)
    _, c5 = zone(five)
    five_txt = ("%d%%" % round(to_num(five))) if to_num(five) is not None else "--"
    segs = ["%s5h%s %s%s%s" % (BOLD + fg(c5), RESET, fg(c5), five_txt, RESET)]
    r5 = fmt_reset(five_r)
    if r5:
        segs[0] += " %s↻%s%s" % (DIM, r5, RESET)
    if to_num(week) is not None:
        _, c7 = zone(week)
        s7 = "%s7d%s %s%d%%%s" % (BOLD + fg(c7), RESET, fg(c7), round(to_num(week)), RESET)
        r7 = fmt_reset(week_r)
        if r7:
            s7 += " %s↻%s%s" % (DIM, r7, RESET)
        segs.append(s7)
    if to_num(ctx) is not None:
        _, cc = zone(ctx)
        segs.append("%sctx%s %s%d%%%s" % (DIM, RESET, fg(cc), round(to_num(ctx)), RESET))
    segs.append("%s🪙 %d/%d%s" % (fg(220), coins_got, coin_total, RESET))
    out.append(("  %s·%s  " % (DIM, RESET)).join(segs))

    return "\n".join(out) + "\n"


# ---- FLEET style: one hero per Claude Code window ----------------------------
# Each session writes ~/.claude/fleet/<session_id>.json every render (+ on hooks
# for state). Every window reads the whole folder and draws the full row, so any
# window shows the entire fleet. State (working/idle/needsyou/error) comes from
# hooks; metrics (ctx/cost/5h/7d) from the render. Dead sessions age out (TTL).
def _fleet_dir():
    try:
        os.makedirs(FLEET_DIR, exist_ok=True)
    except Exception:
        pass
    return FLEET_DIR


def _fleet_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "unknown")[:80]
    return os.path.join(_fleet_dir(), safe + ".json")


def _fleet_load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else None
    except Exception:
        return None


def _fleet_all():
    out = []
    try:
        files = os.listdir(_fleet_dir())
    except Exception:
        files = []
    for fn in files:
        if fn.endswith(".json"):
            p = os.path.join(FLEET_DIR, fn)
            s = _fleet_load(p)
            if s:
                out.append((p, s))
    return out


def _pick_hero():
    claimed = set(s.get("hero") for _, s in _fleet_all())
    for h in HERO_SLOTS:
        if h not in claimed:
            return h
    return HERO_SLOTS[0]


def _fleet_write(sid, updates, state=None):
    """Merge updates into this session's file. state (if given) is set by a hook."""
    path = _fleet_path(sid)
    s = _fleet_load(path) or {}
    now = int(time.time())
    if not s:
        s = {"session_id": sid, "first_ts": now, "hero": _pick_hero(), "state": "idle", "state_ts": now}
    s.update(updates)
    s["ts"] = now
    if state is not None:
        s["state"] = state
        s["state_ts"] = now
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass
    return s


def _fleet_read():
    """Alive sessions, sorted by open order, capped; drops dead files."""
    now = int(time.time())
    ships = []
    for p, s in _fleet_all():
        sid = s.get("session_id")
        stale = now - s.get("ts", 0) > FLEET_ALIVE
        # drop demo/unknown files and stale ones — but keep a pending needs-you
        # visible (its own window's status line is hidden while the prompt is up)
        if not sid or sid == "unknown" or (stale and s.get("state") != "needsyou"):
            try:
                os.remove(p)
            except Exception:
                pass
            continue
        ships.append(s)
    ships.sort(key=lambda s: s.get("first_ts", 0))
    return ships[:FLEET_MAX]


def render_fleet(data):
    sid = get(data, "session_id") or "unknown"
    ctx = to_num(get(data, "context_window", "used_percentage")) or 0.0
    cost = to_num(get(data, "cost", "total_cost_usd")) or 0.0
    five = to_num(get(data, "rate_limits", "five_hour", "used_percentage"))
    seven = to_num(get(data, "rate_limits", "seven_day", "used_percentage"))
    cwd = get(data, "workspace", "current_dir") or get(data, "cwd")
    if sid and sid != "unknown":        # don't let --demo / missing-id pollute the real fleet
        _fleet_write(sid, {"ctx": ctx, "cost": cost, "five": five, "seven": seven,
                           "dir": os.path.basename(cwd) if cwd else "?"})
    ships = _fleet_read()
    if not ships:
        ships = [{"session_id": sid, "hero": "mario", "ctx": ctx, "cost": cost, "state": "idle", "first_ts": 0}]
    n = len(ships)

    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except Exception:
        cols = 0
    cols = min(max(cols or 100, 40), 120)
    try:
        frame = int(os.environ.get("CLAUDE_SL_FRAME", int(time.time())))
    except Exception:
        frame = int(time.time())
    step = frame % 2
    # Fleet always renders HALF-BLOCK, ignoring --set-pixels: at tiny fleet-hero
    # sizes, sextant's 2-colours-per-cell merges a sprite's body+legs into a
    # smear. Half-block keeps every pixel COLUMN its own colour → crisp sprites.
    rows_scene = 8
    PW = cols
    PH = rows_scene * 2
    baseY = PH - 1
    laneW = max(8, PW // n)
    maxP = max(3, PH - 9)           # pillar height range (leaves room for the hero on top)

    def dim(c, f=0.4):
        return (int(c[0] * f), int(c[1] * f), int(c[2] * f))

    def zc(v):                          # distinct hues so pillars never blend into the ground
        return (238, 70, 74) if v >= 90 else (240, 185, 55) if v >= 70 else (58, 200, 100)

    canvas = [[None] * PW for _ in range(PH)]
    for i, s in enumerate(ships):
        cx = i * laneW + laneW // 2
        cv = to_num(s.get("ctx")) or 0.0
        st = s.get("state", "idle")
        ph = max(3, int(cv / 100.0 * maxP))
        pcol = zc(cv)
        pw = max(5, min(9, laneW - 2))
        # pillar = neon-outlined column (bright top + sides, visible dim fill)
        px0 = cx - pw // 2
        for yy in range(baseY - ph, baseY):
            for xx in range(px0, px0 + pw):
                if not (0 <= yy < PH and 0 <= xx < PW):
                    continue
                edge = yy == baseY - ph or xx == px0 or xx == px0 + pw - 1
                canvas[yy][xx] = pcol if edge else dim(pcol, 0.5)
        # hero on top of the pillar
        grid = HEROES.get(s.get("hero"), HEROES[DEFAULT_HERO])["a"]
        gh, gw = len(grid), len(grid[0])
        hs = 1
        bob = 2 if (st == "working" and step == 0) else 0
        bright = st != "idle"
        topY = baseY - ph - gh * hs - bob
        topY -= topY % 2            # align to half-block cell so the sprite doesn't smear
        x0 = cx - (gw * hs) // 2
        for ry in range(gh):
            for rx in range(gw):
                col = TCPAL.get(grid[ry][rx])
                if col is None:
                    continue
                if not bright:
                    col = dim(col)
                for sy in range(hs):
                    for sx in range(hs):
                        yy, xx = topY + ry * hs + sy, x0 + rx * hs + sx
                        if 0 <= yy < PH and 0 <= xx < PW:
                            canvas[yy][xx] = col
        # state beacon above the hero
        by = max(1, topY - 2)

        def put(ox, oy, col):
            yy, xx = by + oy, cx + ox
            if 0 <= yy < PH and 0 <= xx < PW:
                canvas[yy][xx] = col
        if st == "needsyou":
            if step == 0:                                  # blinking amber "!"
                put(0, -2, (255, 200, 50)); put(0, -1, (255, 200, 50)); put(0, 1, (255, 200, 50))
        elif st == "working":
            put(-1, 0, (60, 240, 255)); put(0, -1, (60, 240, 255)); put(1, 0, (60, 240, 255))
        elif st == "error":
            for ox, oy in ((-1, -1), (1, 1), (1, -1), (-1, 1), (0, 0)):
                put(ox, oy, (255, 74, 77))
        else:                                              # idle dot
            put(0, 0, (120, 130, 155))
        # YOU marker on the ground under this session
        if s.get("session_id") == sid:
            for xx in range(cx - 1, cx + 2):
                if 0 <= xx < PW:
                    canvas[baseY][xx] = (33, 240, 180)
    for xx in range(PW):                                   # dim neutral ground line (recedes)
        if canvas[baseY][xx] is None:
            canvas[baseY][xx] = (26, 40, 60)

    scene = render_canvas_tc(canvas)

    # HUD summary
    w = sum(1 for s in ships if s.get("state") == "working")
    ny = sum(1 for s in ships if s.get("state") == "needsyou")
    idl = sum(1 for s in ships if s.get("state") == "idle")
    tot = sum((to_num(s.get("cost")) or 0.0) for s in ships)
    hud = "%sFLEET%s %d/%d  %s⚡%d%s %s❗%d%s %s💤%d%s  %sΣ$%.0f%s" % (
        BOLD, RESET, n, FLEET_MAX, fg(45), w, RESET, fg(214), ny, RESET, DIM, idl, RESET, DIM, tot, RESET)
    if five is not None:
        hud += "  %s5h%s %s%d%%%s" % (DIM, RESET, fg(zone(five)[1]), round(five), RESET)
    if seven is not None:
        hud += " %s· 7d %d%%%s" % (DIM, round(seven), RESET)

    # per-lane metrics row (aligned to lanes): ctx% + $cost when there's room
    term_w = PW
    fw = max(4, term_w // n)
    cells = []
    for s in ships:
        cv = round(to_num(s.get("ctx")) or 0.0)
        _, zcol = zone(cv)
        lab = "%d%%" % cv
        if fw >= 8:
            lab += " $%d" % round(to_num(s.get("cost")) or 0.0)
        pad = fw - len(lab)
        left = max(0, pad // 2)
        cells.append(" " * left + fg(zcol) + lab + RESET + " " * max(0, pad - left))
    metrics = "".join(cells)

    return "\n".join([hud] + scene + [metrics]) + "\n"


# ---- CLI (hero picker) vs statusline (stdin) mode ----------------------------
def _mock():
    now = int(time.time())
    return {
        "model": {"display_name": "Opus 4.8"},
        "workspace": {"current_dir": os.getcwd()},
        "context_window": {"used_percentage": 42},
        "cost": {"total_cost_usd": 1.42, "total_duration_ms": 5000000},
        "rate_limits": {
            "five_hour": {"used_percentage": 45, "resets_at": now + 8100},
            "seven_day": {"used_percentage": 79, "resets_at": now + 39600},
        },
    }


def _test_glyphs():
    print("Does your terminal render these cleanly? Solid shapes = good; boxes/?/blank = unsupported.\n")
    print("  half-block |\033[38;2;90;170;220m▀▄▀▄▀▄▀▄\033[0m|  (always works)")
    print("  quadrants  |▘▝▖▗▚▞▙▟▛▜▀▄▌▐█|  (almost always)")
    print("  sextants   |" + "".join(chr(0x1FB00 + i) for i in range(26)) + "|  (finest — needs a modern font)")
    print("\nIf the sextants row shows solid shapes, turn on fine pixels:")
    print("  python %s --set-pixels sext" % os.path.basename(__file__))
    print("If it shows boxes, keep the safe size:  --set-pixels half")


def _cli_hook(arg):
    """Called by Claude Code hooks: read the hook JSON on stdin, update this
    session's fleet file. arg is a target state, or 'start' / 'end'."""
    try:
        d = json.loads(sys.stdin.read())
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    sid = d.get("session_id") or "unknown"
    cwd = d.get("cwd")
    upd = {"dir": os.path.basename(cwd)} if cwd else {}
    if arg == "end":
        try:
            os.remove(_fleet_path(sid))
        except Exception:
            pass
    elif arg == "start":
        _fleet_write(sid, upd)
    elif arg in ("working", "idle", "needsyou", "error"):
        _fleet_write(sid, upd, state=arg)
    return 0


def _cli(argv):
    cmd = argv[1].lstrip("-").lower()
    if cmd == "hook":
        return _cli_hook(argv[2] if len(argv) > 2 else "")
    if cmd in ("list", "heroes"):
        cur = resolve_hero()
        print("Heroes (current: %s):\n" % cur)
        for name, h in HEROES.items():
            mark = "→" if name == cur else " "
            print("  %s %-8s %s" % (mark, name, h["label"]))
        base = os.path.basename(__file__)
        print("\nPick a hero:  python %s --set <name>" % base)
        print("Pick a scene: python %s --set-style <flat|summit|fleet>  (current: %s)" % (base, resolve_style()))
        print("Theme:        python %s --set-theme <day|cyber>      (current: %s)" % (base, resolve_theme()))
        print("Pixel size:   python %s --set-pixels <half|sext>    (current: %s)" % (base, resolve_pixels()))
        print("Scene size:   python %s --set-size <small|medium|large>  (current: %s)" % (base, _size_name()))
        print("Test glyphs:  python %s --test-glyphs" % base)
    elif cmd == "current":
        print(resolve_hero())
    elif cmd == "set":
        if len(argv) < 3 or argv[2].lower() not in HEROES:
            print("Usage: --set <name>   where <name> is one of: %s" % ", ".join(HEROES))
            return 1
        name = argv[2].lower()
        with open(HERO_FILE, "w", encoding="utf-8") as f:
            f.write(name)
        print("Hero set to '%s' %s  (%s)" % (name, HEROES[name]["label"], HERO_FILE))
    elif cmd == "style":
        print(resolve_style())
    elif cmd in ("set-style", "setstyle"):
        if len(argv) < 3 or argv[2].lower() not in STYLES:
            print("Usage: --set-style <flat|summit>")
            return 1
        s = argv[2].lower()
        with open(STYLE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        print("Scene set to '%s'  (%s)" % (s, STYLE_FILE))
    elif cmd in ("test-glyphs", "testglyphs"):
        _test_glyphs()
    elif cmd in ("set-pixels", "setpixels"):
        if len(argv) < 3 or argv[2].lower() not in PIXELS:
            print("Usage: --set-pixels <half|sext>")
            return 1
        p = argv[2].lower()
        with open(PIXELS_FILE, "w", encoding="utf-8") as f:
            f.write(p)
        print("Pixel size set to '%s'  (%s)" % (p, PIXELS_FILE))
    elif cmd in ("set-size", "setsize"):
        if len(argv) < 3 or argv[2].lower() not in SIZES:
            print("Usage: --set-size <%s>" % "|".join(SIZES))
            return 1
        s = argv[2].lower()
        with open(SIZE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        print("Scene size set to '%s' (%d px)  (%s)" % (s, SIZES[s], SIZE_FILE))
    elif cmd in ("set-theme", "settheme"):
        if len(argv) < 3 or argv[2].lower() not in THEMES:
            print("Usage: --set-theme <day|cyber>")
            return 1
        t = argv[2].lower()
        with open(THEME_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        print("Theme set to '%s'  (%s)" % (t, THEME_FILE))
    elif cmd == "demo":
        name = argv[2].lower() if len(argv) > 2 and argv[2].lower() in HEROES else resolve_hero()
        style = resolve_style()
        if style == "fleet":
            sys.stdout.write(render_fleet(_mock()))
        else:
            sys.stdout.write((render_summit if style == "summit" else render_statusline)(_mock(), name))
    else:
        print(__doc__.split("Honest animation")[0].strip())
    return 0


def main():
    if len(sys.argv) > 1:
        sys.exit(_cli(sys.argv))
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    style = resolve_style()
    if style == "fleet":
        sys.stdout.write(render_fleet(data))
    else:
        render = render_summit if style == "summit" else render_statusline
        sys.stdout.write(render(data, resolve_hero()))


main()
