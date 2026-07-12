#!/usr/bin/env python3
"""claude-status-hero · hero_line.py — the gauge.

A Claude Code statusline that is fun but NEVER jitters:
  • always exactly 3 lines, each padded/cropped to an exact display width
  • a hero emoji walks the 5-hour rate-limit track (coins at budget quarters)
  • context + 7-day meters, git, cost, and a cross-window fleet summary
  • doubles as the hook handler + state writer for hero_board.py

Modes (first argument):
  (none)          statusline: read Claude Code JSON on stdin, print the lines
  --hook EVENT    hook handler: update this session's state file, print nothing
  --install       register statusline + hooks in ~/.claude/settings.json
  --uninstall     remove our entries (a timestamped backup is written either way)
  --style X       statusline style, persisted in config.json:
                    gauge  3 lines: HUD + 5h track + meters (default)
                    list   7 lines: gauge + one compact fleet row per session
                           (emoji hero, state, activity, ctx bar, cost)
                    fleet  10 lines: HUD + 5h track + static pixel-art scene
                           (needs hero_board.py next to this file)
                  list/fleet set statusLine.refreshInterval=2 so other
                  windows' states stay fresh; gauge removes it
  --demo          print sample renders (no Claude Code needed)
  --simulate      animate a fake session in your terminal (for GIF capture)
  --doctor        print alignment/color diagnostics for your terminal

Env: STATUS_HERO_ASCII=1 (pure ASCII), STATUS_HERO_AMBIG_WIDE=1 (treat
Unicode ambiguous-width as 2 columns AND swap block glyphs for ASCII),
STATUS_HERO_DIR (state dir override), NO_COLOR.

Python ≥3.9, stdlib only. MIT.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata

# ---------------------------------------------------------------- environment

ASCII = os.environ.get("STATUS_HERO_ASCII", "") not in ("", "0")
AMBIG_WIDE = os.environ.get("STATUS_HERO_AMBIG_WIDE", "") not in ("", "0")
if AMBIG_WIDE:
    ASCII = True  # ambiguous-wide terminals can't render block bars safely
NO_COLOR = os.environ.get("NO_COLOR") is not None
TRUECOLOR = (os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
             or bool(os.environ.get("WT_SESSION")))  # WT never sets COLORTERM

STATE_DIR = os.environ.get("STATUS_HERO_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "status-hero"
)
SESS_DIR = os.path.join(STATE_DIR, "sessions")

# Force UTF-8 I/O; newline="" keeps Windows Python from emitting \r\n.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", newline="")
    except Exception:
        pass

# ------------------------------------------------------------------- palette

RESET = "" if NO_COLOR else "\x1b[0m"
BOLD = "" if NO_COLOR else "\x1b[1m"
DIM = "" if NO_COLOR else "\x1b[2m"
INVERT = "" if NO_COLOR else "\x1b[7m"


def fg(rgb, idx):
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
GOLD = fg((245, 200, 66), 221)


def zone(pct):
    """Zone color for a 0-100 percentage; gray when unknown."""
    if pct is None:
        return GRAY
    if pct >= 90:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


# ---------------------------------------------------------------- glyph sets

# Only emoji with Emoji_Presentation=Yes + East_Asian_Width=Wide (always 2
# columns in spec-compliant terminals). Never ⚠ ⏱ ✔ ❤ (VS16 width traps).
HERO_ROSTER = [
    ("fox", "🦊", (255, 140, 66)),
    ("cat", "🐱", (250, 208, 120)),
    ("frog", "🐸", (110, 205, 90)),
    ("owl", "🦉", (188, 140, 100)),
    ("penguin", "🐧", (140, 170, 230)),
    ("rabbit", "🐰", (235, 200, 210)),
    ("bear", "🐻", (196, 138, 84)),
    ("duck", "🦆", (120, 190, 120)),
]

if ASCII:
    G_COIN, G_FLAG, G_WORK, G_NEED, G_IDLE = "o", ">", "W", "!", "z"
    G_TRACK, G_FILL, G_EMPTY = "-", "#", "."
    G_BRANCH, G_RESET_AT, G_SEP = "git:", "R ", " | "
else:
    G_COIN, G_FLAG, G_WORK, G_NEED, G_IDLE = "⭐", "🏁", "⚡", "❗", "💤"
    G_TRACK, G_FILL, G_EMPTY = "░", "█", "░"
    G_BRANCH, G_RESET_AT, G_SEP = "⎇ ", "↻", " · "


def hero_glyph(name):
    if ASCII:
        return "@"
    for n, g, _rgb in HERO_ROSTER:
        if n == name:
            return g
    return "🦊"


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
            # dedicated pictographs render 2 cols; narrow symbols in these
            # ranges (e.g. ✂ U+2702 without VS16) are avoided by our glyph set
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    ea = unicodedata.east_asian_width(ch)
    if ea in ("W", "F"):
        return 2
    if ea == "A":
        return 2 if AMBIG_WIDE else 1
    return 1


def disp_width(s):
    return sum(ch_width(ch) for ch in ANSI_TOKEN.sub("", s))


def crop_pad(s, width):
    """Crop s to exactly `width` display columns (ANSI-aware), pad w/ spaces."""
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
    return "".join(out) + RESET + " " * (width - w)


def sanitize(s, limit=120):
    """External strings (dirnames, prompts, tool args) → safe printable text.
    Strips ANSI, C0/C1 controls, and invisible format chars (bidi overrides)."""
    if not isinstance(s, str):
        return ""
    s = s[:4096]        # bound the scans: session files can be hostile/huge
    s = CTRL_STRIP.sub("", ANSI_TOKEN.sub("", s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf").strip()
    if len(s) > limit:
        s = s[: limit - 2] + ".."
    return s


# ------------------------------------------------------------------- helpers

def get(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # json admits bare NaN/Infinity; int()/round() on them crashes renders
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


def fmt_dur(ms):
    n = num(ms)
    if n is None:
        return "--"
    mins = int(n // 60000)
    if mins >= 60:
        return "%dh%02dm" % (mins // 60, mins % 60)
    return "%dm" % mins


def bar(pct, width):
    p = num(pct)
    filled = 0 if p is None else max(0, min(width, int(round(p * width / 100.0))))
    col = zone(p)
    return col + G_FILL * filled + GRAY + DIM + G_EMPTY * (width - filled) + RESET


def pct_txt(pct):
    p = num(pct)
    return "--%" if p is None else "%d%%" % int(round(p))


# --------------------------------------------------------------- session I/O

def _ensure_dirs():
    try:
        os.makedirs(SESS_DIR, exist_ok=True)
    except Exception:
        pass


def sess_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(sid))[:80] or "unknown"
    return os.path.join(SESS_DIR, safe + ".json")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _open_excl(path):
    """Create-only open: never follows a pre-planted symlink, private perms."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def _tmp_name(path):
    return "%s.tmp.%d.%d" % (path, os.getpid(), time.time_ns() % 1000000)


def atomic_write(path, data):
    tmp = _tmp_name(path)
    try:
        with _open_excl(tmp) as f:
            json.dump(data, f)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                # Windows: replace fails while a reader (the board, 4 fps)
                # holds the file open — sub-ms window, one retry wins.
                if os.name != "nt" or attempt == 2:
                    raise
                time.sleep(0.01)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def update_session(sid, updates):
    if not sid:
        return
    _ensure_dirs()
    path = sess_path(sid)
    s = load_json(path) or {}
    if s.get("ended") and "state" not in updates:
        return          # tombstone: only a live hook may resurrect a session
    if "state" in updates:
        s.pop("ended", None)
    s.update(updates)
    s["sid"] = str(sid)[:80]
    s["ts"] = time.time()
    if "hero" not in s:
        s["hero"] = pick_hero(sid)
    if "started_at" not in s:
        s["started_at"] = s["ts"]  # first sighting = stable lane order
    if "state" not in updates:
        # metrics-only write (statusline): a hook may have advanced the state
        # between our load and now — re-check so we never roll it back.
        fresh = load_json(path)
        if fresh:
            if fresh.get("ended"):
                return
            if (num(fresh.get("state_ts")) or 0) > (num(s.get("state_ts")) or 0):
                for k in ("state", "state_ts", "need", "activity", "activity_ts"):
                    if k in fresh:
                        s[k] = fresh[k]
    atomic_write(path, s)


def load_config():
    return load_json(os.path.join(STATE_DIR, "config.json")) or {}


def save_config(updates):
    _ensure_dirs()
    cfg = load_config()
    cfg.update(updates)
    atomic_write(os.path.join(STATE_DIR, "config.json"), cfg)


def all_sessions():
    out = []
    try:
        names = sorted(os.listdir(SESS_DIR))[:64]  # sanity cap
    except Exception:
        return out
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(SESS_DIR, n)
        s = load_json(path)
        if not s or not s.get("sid"):
            continue
        if s.get("ended"):
            if now - (num(s.get("ts")) or 0) > 600:
                try:
                    os.remove(path)   # stale tombstone: board isn't running
                except Exception:
                    pass
            continue
        out.append(s)
    return out


def pick_hero(sid):
    claimed = {s.get("hero") for s in all_sessions() if s.get("sid") != str(sid)}
    names = [n for n, _g, _c in HERO_ROSTER]
    idx = int(hashlib.sha1(str(sid).encode()).hexdigest(), 16) % len(names)
    for i in range(len(names)):
        cand = names[(idx + i) % len(names)]
        if cand not in claimed:
            return cand
    return names[idx]


def effective_state(s, now=None):
    """A session's display state; 'working' decays if its heartbeat is stale."""
    now = now or time.time()
    st = s.get("state") or "working"
    ts = num(s.get("state_ts")) or num(s.get("ts")) or 0
    if st in ("working", "compacting") and now - ts > 15 * 60:
        return "idle"
    return st


# ------------------------------------------------------------------ git info

def git_branch(cwd):
    """Branch + dirty count, cached 5 s per repo (statusline runs often)."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    _ensure_dirs()
    key = hashlib.sha1(cwd.encode()).hexdigest()[:16]
    cache = os.path.join(STATE_DIR, "git-" + key)
    try:
        if os.path.isfile(cache) and time.time() - os.path.getmtime(cache) < 5:
            with open(cache, encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    info = ""
    try:
        def run(*args):
            return subprocess.run(
                ["git", "-C", cwd, "--no-optional-locks"] + list(args),
                capture_output=True, text=True, timeout=1.5,
            ).stdout
        branch = run("branch", "--show-current").strip()
        if branch:
            dirty = sum(1 for ln in run("status", "--porcelain").splitlines() if ln.strip())
            info = branch + ("·%d" % dirty if dirty else "")
    except Exception:
        info = ""
    try:
        tmp = _tmp_name(cache)          # atomic: two windows share this cache
        with _open_excl(tmp) as f:
            f.write(info)
        os.replace(tmp, cache)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return info


# ------------------------------------------------------------------ renderer

def term_width():
    try:
        return int(os.environ.get("COLUMNS", ""))
    except ValueError:
        pass
    try:
        import shutil
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def render(data, width=None):
    """The contract: a CONSTANT number of lines (3 gauge / 10 fleet), each
    EXACTLY W columns wide."""
    W = max(40, min((width or term_width()) - 2, 100))

    dirname = sanitize(os.path.basename(
        get(data, "workspace", "current_dir") or get(data, "cwd") or "") or "?", 24)
    model = sanitize(get(data, "model", "display_name") or "", 20)
    agent = sanitize(get(data, "agent", "name") or "", 16)
    effort = sanitize(get(data, "effort", "level") or "", 8)
    cost = num(get(data, "cost", "total_cost_usd"))
    dur = get(data, "cost", "total_duration_ms")
    ctx = num(get(data, "context_window", "used_percentage"))
    ctx_used = num(get(data, "context_window", "total_input_tokens"))
    ctx_size = num(get(data, "context_window", "context_window_size"))
    five = num(get(data, "rate_limits", "five_hour", "used_percentage"))
    five_rs = get(data, "rate_limits", "five_hour", "resets_at")
    seven = num(get(data, "rate_limits", "seven_day", "used_percentage"))
    seven_rs = get(data, "rate_limits", "seven_day", "resets_at")
    sid = get(data, "session_id")

    branch = sanitize(git_branch(get(data, "workspace", "current_dir")
                                 or get(data, "cwd")), 40)

    # persist metrics FIRST so our own lane is in the fleet we render
    if sid:
        update_session(sid, {
            "dir": dirname, "cwd": sanitize(get(data, "workspace", "current_dir") or "", 200),
            "model": model, "ctx": ctx,
            "ctx_used_k": None if ctx_used is None else int(ctx_used / 1000),
            "ctx_size_k": None if ctx_size is None else int(ctx_size / 1000),
            "cost": cost, "dur_ms": num(dur),
            "five": five, "five_reset": num(five_rs),
            "seven": seven, "seven_reset": num(seven_rs),
            "branch": branch, "ppid": os.getppid(),
        })
    sessions = all_sessions()
    me = next((s for s in sessions if s.get("sid") == str(sid)), None)
    hero = (me or {}).get("hero") or (pick_hero(sid) if sid else "fox")

    # ---- L1: identity ----
    left = BOLD + dirname + RESET
    if model:
        left += " " + DIM + "[" + model + "]" + RESET
    if agent:
        left += " " + MAGENTA + agent + RESET
    if branch:
        left += " " + CYAN + G_BRANCH + branch + RESET
    right = []
    if effort:
        right.append(MAGENTA + effort + RESET)
    right.append(DIM + "$" + ("%.2f" % cost if cost is not None else "--") + RESET)
    right.append(DIM + fmt_dur(dur) + RESET)
    right_s = (DIM + G_SEP + RESET).join(right)
    gap = W - disp_width(left) - disp_width(right_s)
    if gap < 1:
        line1 = crop_pad(left + " " + right_s, W)
    else:
        line1 = crop_pad(left + " " * gap + right_s, W)

    # ---- L2: the 5h track ----
    label = zone(five) + BOLD + "5h" + RESET + " "
    tail = " " + zone(five) + "%4s" % pct_txt(five) + RESET + \
           " " + DIM + G_RESET_AT + "%-6s" % fmt_reset(five_rs) + RESET
    track_w = W - disp_width(label) - disp_width(tail)
    line2 = crop_pad(label + track(five, track_w, hero) + tail, W)

    # ---- fleet style: full static fleet scene instead of L3 ----
    style = load_config().get("style")
    if style == "fleet":
        return "\n".join([line1, line2] + fleet_lines(sessions, sid, W))

    # ---- L3: meters + fleet ----
    fleet = fleet_summary(sessions, sid)
    used_k = "" if ctx_used is None or ctx_size is None else \
        " %d/%dk" % (int(ctx_used / 1000), int(ctx_size / 1000))
    for attempt in range(3):
        bw_c, bw_7 = (12, 10) if attempt == 0 else (8, 6)
        seg = [DIM + "ctx " + RESET + bar(ctx, bw_c) + " " + zone(ctx) + pct_txt(ctx) + RESET
               + (DIM + used_k + RESET if attempt == 0 else "")]
        seg.append(DIM + "7d " + RESET + bar(seven, bw_7) + " " + zone(seven) + pct_txt(seven)
                   + RESET + (" " + DIM + G_RESET_AT + fmt_reset(seven_rs) + RESET
                              if attempt == 0 else ""))
        if fleet and attempt < 2:
            seg.append(fleet)
        cand = (DIM + G_SEP + RESET).join(seg)
        if disp_width(cand) <= W:
            break
    line3 = crop_pad(cand, W)

    # ---- list style: gauge + LIST_LANES compact fleet rows in between ----
    if style == "list":
        return "\n".join([line1, line2] + list_lines(sessions, sid, W)
                         + [line3])

    return "\n".join((line1, line2, line3))


def track(five, width, hero_name):
    """The 5h world strip: hero walks toward 🏁, ⭐ at 20/40/60/80 % budget."""
    if width < 8:
        return GRAY + DIM + G_TRACK * max(0, width) + RESET
    known = num(five) is not None
    p = max(0.0, min(100.0, num(five) or 0.0))
    col = zone(p if known else None)

    slots = [None] * width          # None = base track char
    flag_at = width - 2
    hero_w = 1 if ASCII else 2
    hero_at = int(round(p / 100.0 * (flag_at - hero_w)))

    def place(i, glyph, style, w):
        if i < 0 or i + w > width:
            return
        for j in range(i, i + w):
            if isinstance(slots[j], tuple) or slots[j] == "skip":
                return
        slots[i] = (glyph, style)
        for j in range(i + 1, i + w):
            slots[j] = "skip"

    place(flag_at, G_FLAG, ("" if known and p >= 99.5 else DIM), 1 if ASCII else 2)
    coin_w = 1 if ASCII else 2
    for q in (20, 40, 60, 80):
        c = int(round(q / 100.0 * (flag_at - coin_w)))
        if known and p >= q:
            place(c, ".", GRAY + DIM, 1)          # collected
        else:
            place(c, G_COIN, GOLD if known else GRAY + DIM, coin_w)
    # hero wins collisions: clear then place
    for j in range(hero_at, min(hero_at + hero_w, width)):
        slots[j] = None
    nxt = hero_at + hero_w
    if nxt < width and slots[nxt] == "skip":      # half-orphaned wide glyph
        slots[nxt] = None
    place(hero_at, hero_glyph(hero_name), "", hero_w)

    out, passed_style = [], (col + DIM if known else GRAY + DIM)
    i = 0
    while i < width:
        s = slots[i]
        if s == "skip":
            i += 1
            continue
        if isinstance(s, tuple):
            out.append(s[1] + s[0] + RESET)
        else:
            out.append((passed_style if i < hero_at else GRAY + DIM) + G_TRACK + RESET)
        i += 1
    return "".join(out)


# ------------------------------------------------------------- fleet style

FLEET_ROWS = 8   # beacons + 4 sprite rows + ctx bar + names + info


def _board_mod():
    """Load hero_board.py (sprites live there) from our own directory."""
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hero_board.py")
        spec = importlib.util.spec_from_file_location("status_hero_board", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


def center_to(s, width):
    w = disp_width(s)
    if w >= width:
        return crop_pad(s, width)
    lpad = (width - w) // 2
    return " " * lpad + s + RESET + " " * (width - w - lpad)


STATE_GLYPHS = {
    "working": (G_WORK, CYAN, "working"),
    "needs_you": (G_NEED, RED, "NEEDS YOU"),
    "idle": (G_IDLE, GRAY, "idle"),
    "compacting": ("~" if ASCII else "🌀", MAGENTA, "compacting"),
    "ghost": ("?" if ASCII else "👻", GRAY, "stale"),
}


def fleet_lines(sessions, own_sid, W):
    """The board's scene, statically, in EXACTLY FLEET_ROWS statusline rows.
    No blink/trot (statusline refresh is event-driven) — beacons are steady."""
    now = time.time()
    live = [s for s in sessions if now - (num(s.get("ts")) or 0) < 2 * 3600]
    live.sort(key=lambda s: num(s.get("started_at")) or num(s.get("ts")) or 0)
    if not live:
        msg = ["", "", "  no fleet yet — open more Claude Code windows", "",
               "", "", "", ""]
        return [crop_pad(DIM + m + RESET, W) for m in msg]

    k = max(1, min(len(live), W // 14))
    lanes, extra = live[:k], len(live) - k
    lane_w = min(24, W // k)
    board = _board_mod()

    cells = []   # per lane: list of FLEET_ROWS strings, each lane_w wide
    for s in lanes:
        st = effective_state(s, now)
        glyph, col, label = STATE_GLYPHS.get(st, STATE_GLYPHS["idle"])
        ctx = num(s.get("ctx"))
        rows = [center_to(col + glyph + RESET, lane_w)]
        if board is not None:
            spr = board.sprite_lines(s.get("hero") or "fox", 0, st)
        else:   # hero_board.py missing next to us — emoji stand-in
            spr = ["", hero_glyph(s.get("hero") or "fox"), "", ""]
        for r in spr:
            rows.append(center_to(r, lane_w))
        rows.append(center_to(bar(ctx, min(10, lane_w - 4)) + " " + zone(ctx)
                              + pct_txt(ctx) + RESET, lane_w))
        name = sanitize(s.get("dir") or "?", lane_w - 2)
        if st == "needs_you":
            rows.append(center_to(RED + BOLD + INVERT + " " + name + " " + RESET, lane_w))
        elif s.get("sid") == str(own_sid):
            rows.append(center_to(CYAN + BOLD + name + RESET, lane_w))
        else:
            rows.append(center_to(BOLD + name + RESET, lane_w))
        info = col + label + RESET + DIM + " $%.0f" % (num(s.get("cost")) or 0) + RESET
        rows.append(center_to(info, lane_w))
        cells.append(rows)

    out = []
    for r in range(FLEET_ROWS):
        line = "".join(c[r] for c in cells)
        if r == 0 and extra > 0:
            line += DIM + " +%d" % extra + RESET
        out.append(crop_pad(line, W))
    return out


# ------------------------------------------------------------- list style

LIST_LANES = 4


def list_lines(sessions, own_sid, W):
    """Compact fleet: EXACTLY LIST_LANES rows, one session per row.
    Own session is always visible; blank rows keep the height constant."""
    now = time.time()
    live = [s for s in sessions if now - (num(s.get("ts")) or 0) < 2 * 3600]
    live.sort(key=lambda s: num(s.get("started_at")) or num(s.get("ts")) or 0)
    me = next((s for s in live if s.get("sid") == str(own_sid)), None)
    lanes = live[:LIST_LANES]
    if me is not None and me not in lanes:
        lanes[-1] = me
    extra = max(0, len(live) - len(lanes))

    rows = []
    for i, s in enumerate(lanes):
        if extra and i == len(lanes) - 1:
            suffix = " +%d" % extra
            row = list_row(s, own_sid, W - len(suffix)) + DIM + suffix + RESET
        else:
            row = list_row(s, own_sid, W)
        rows.append(crop_pad(row, W))
    if not rows:
        rows.append(crop_pad(DIM + "  no fleet yet — open more Claude Code"
                             " windows" + RESET, W))
    while len(rows) < LIST_LANES:
        rows.append(crop_pad("", W))
    return rows


def list_row(s, own_sid, W):
    st = effective_state(s)
    glyph, col, label = STATE_GLYPHS.get(st, STATE_GLYPHS["idle"])
    ctx = num(s.get("ctx"))
    name_w = 14 if W >= 80 else 11
    bar_w = 6 if W >= 80 else 4
    own = s.get("sid") == str(own_sid)

    name = sanitize(s.get("dir") or "?", name_w)
    name_cell = crop_pad((CYAN if own else "") + BOLD + name + RESET, name_w)
    state_cell = crop_pad(col + (BOLD if st == "needs_you" else "")
                          + label + RESET, 10)
    right = (bar(ctx, bar_w) + " " + zone(ctx) + "%4s" % pct_txt(ctx) + RESET
             + DIM + " $%4.0f" % (num(s.get("cost")) or 0) + RESET)
    prefix = (hero_glyph(s.get("hero") or "fox") + " " + name_cell + " "
              + col + glyph + RESET + " " + state_cell + " ")
    act_w = W - disp_width(prefix) - disp_width(right) - 1
    if act_w >= 6:
        act = crop_pad(GRAY + sanitize(s.get("activity") or "", act_w)
                       + RESET, act_w)
        return prefix + act + " " + right
    return prefix + right


def fleet_summary(sessions, own_sid):
    now = time.time()
    live = [s for s in sessions if now - (num(s.get("ts")) or 0) < 2 * 3600]
    if len(live) < 2:
        return ""
    counts = {"working": 0, "needs_you": 0, "idle": 0}
    for s in live:
        st = effective_state(s, now)
        counts[st if st in counts else "idle"] += 1
    parts = []
    if counts["working"]:
        parts.append(CYAN + G_WORK + "%d" % counts["working"] + RESET)
    if counts["needs_you"]:
        parts.append(RED + BOLD + G_NEED + "%d" % counts["needs_you"] + RESET)
    if counts["idle"]:
        parts.append(GRAY + G_IDLE + "%d" % counts["idle"] + RESET)
    return " ".join(parts)


# ---------------------------------------------------------------- hook mode

def handle_hook(event):
    """Update session state. Must NEVER block or fail (exit 0 always)."""
    raw, total = [], 0
    try:
        while True:                     # read to EOF: a PostToolUse payload
            chunk = sys.stdin.read(1 << 20)   # embeds whole tool_input/response
            if not chunk:
                break
            total += len(chunk)
            if total <= (1 << 23):      # keep <=8 MB, drain the rest
                raw.append(chunk)
    except Exception:
        pass
    raw = "".join(raw)
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        # oversized/truncated payload: salvage the sid (it leads the JSON)
        # so the STATE flip still lands; only the activity detail is lost.
        m = re.search(r'"session_id"\s*:\s*"([A-Za-z0-9_-]{1,80})"', raw[:4096])
        payload = {"session_id": m.group(1)} if m else {}
    sid = payload.get("session_id")
    if not sid:
        return
    now = time.time()

    if event == "SessionEnd":
        # tombstone, not delete: an in-flight statusline render (git can take
        # seconds) would otherwise resurrect the file after os.remove and the
        # board would show a zombie lane. The board buries tombstones on sight.
        atomic_write(sess_path(sid), {"sid": str(sid)[:80], "ended": True,
                                      "ts": now})
        return

    upd = {}
    if event == "SessionStart":
        upd = {"state": "working", "state_ts": now, "started_at": now,
               "cwd": sanitize(payload.get("cwd") or "", 200),
               "dir": sanitize(os.path.basename(payload.get("cwd") or "") or "?", 24)}
    elif event == "UserPromptSubmit":
        snippet = sanitize(payload.get("prompt") or "", 60)
        upd = {"state": "working", "state_ts": now,
               "activity": ("you: " + snippet) if snippet else "prompt",
               "activity_ts": now}
    elif event == "PostToolUse":
        tool = sanitize(payload.get("tool_name") or "tool", 24)
        ti = payload.get("tool_input") or {}
        hint = ""
        if isinstance(ti, dict):
            hint = sanitize(ti.get("command") or ti.get("file_path")
                            or ti.get("description") or ti.get("prompt") or "", 40)
        upd = {"state": "working", "state_ts": now,
               "activity": tool + (": " + hint if hint else ""), "activity_ts": now}
    elif event == "NeedsYouPermission":
        upd = {"state": "needs_you", "state_ts": now, "need": "permission"}
    elif event == "NeedsYouIdle":
        upd = {"state": "needs_you", "state_ts": now, "need": "waiting for you"}
    elif event == "Stop":
        upd = {"state": "idle", "state_ts": now}
    elif event == "PreCompact":
        upd = {"state": "compacting", "state_ts": now}
    elif event == "PostCompact":
        upd = {"state": "working", "state_ts": now}
    else:
        return
    update_session(sid, upd)


# ------------------------------------------------------------ install logic

HOOK_EVENTS = [
    ("SessionStart", None, "SessionStart"),
    ("UserPromptSubmit", None, "UserPromptSubmit"),
    ("PostToolUse", None, "PostToolUse"),
    ("Stop", None, "Stop"),
    ("Notification", "permission_prompt", "NeedsYouPermission"),
    ("Notification", "idle_prompt", "NeedsYouIdle"),
    ("PreCompact", None, "PreCompact"),
    ("PostCompact", None, "PostCompact"),
    ("SessionEnd", None, "SessionEnd"),
]

MARK = "hero_line.py"  # our entries are recognized by this substring


def shell_cmd(*parts):
    """Join command parts with real shell quoting (spaces AND metacharacters)."""
    out = []
    for p in parts:
        if os.name == "nt":
            # Claude Code runs statusLine/hook commands through a POSIX shell
            # even on Windows, where an unquoted backslash is an escape char
            # ("C:\\Python\\python.exe" -> "C:Pythonpython.exe": not found).
            # Forward slashes work as path separators in that shell and cmd.exe.
            p = p.replace("\\", "/")
            if not re.fullmatch(r"[A-Za-z0-9_\-.:/]+", p):
                p = '"%s"' % p.replace('"', '""')
        else:
            import shlex
            p = shlex.quote(p)
        out.append(p)
    return " ".join(out)


def settings_file(argv):
    if "--settings" in argv:
        return argv[argv.index("--settings") + 1]
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def backup_settings(path):
    if not os.path.isfile(path):
        return None
    base = path + ".status-hero-backup-" + time.strftime("%Y%m%d-%H%M%S")
    backup, n = base, 1
    while True:
        try:
            with open(path, encoding="utf-8") as f, _open_excl(backup) as b:
                b.write(f.read())
            return backup
        except FileExistsError:
            backup = "%s.%d" % (base, n)
            n += 1


def install(argv):
    path = settings_file(argv)
    me = os.path.abspath(__file__)
    py = sys.executable or "python3"
    data = load_json(path) or {}
    backup = backup_settings(path)

    prev = data.get("statusLine")
    data["statusLine"] = {"type": "command", "command": shell_cmd(py, me), "padding": 0}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):   # null/str/list → start clean (backup exists)
        hooks = {}
        data["hooks"] = hooks
    events = {e for e, _m, _a in HOOK_EVENTS}
    for event in events:  # sweep our stale entries once per event, keep others
        cur = hooks.get(event, [])
        if not isinstance(cur, list):
            cur = []                  # malformed per-event value → reset
        hooks[event] = [g for g in cur if not _is_ours(g)]
    for event, matcher, arg in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command",
                            "command": shell_cmd(py, me, "--hook", arg),
                            "timeout": 5}]}
        if matcher:
            entry["matcher"] = matcher
        hooks[event].append(entry)

    atomic_write_pretty(path, data)
    print("installed → %s" % path)
    print("  statusline : %s" % shell_cmd(py, me))
    print("  hooks      : %d events (state for hero_board.py)" % len(HOOK_EVENTS))
    if prev and MARK not in json.dumps(prev):
        print("  replaced   : %s" % json.dumps(prev))
    print("  rollback   : cp '%s' '%s'  (or --uninstall)" % (backup, path))
    print("takes effect on the next statusline refresh; open a new Claude Code"
          " window (or send a message) to see it.")


def uninstall(argv):
    path = settings_file(argv)
    data = load_json(path) or {}
    backup = backup_settings(path)
    sl = data.get("statusLine")
    if isinstance(sl, dict) and MARK in (sl.get("command") or ""):
        del data["statusLine"]
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            if not isinstance(hooks[event], list):
                continue              # not ours to touch
            kept = [g for g in hooks[event] if not _is_ours(g)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            data.pop("hooks", None)
    atomic_write_pretty(path, data)
    print("uninstalled (backup: %s)" % backup)


def _is_ours(group):
    try:
        return any(MARK in (h.get("command") or "") for h in group.get("hooks", []))
    except Exception:
        return False


def atomic_write_pretty(path, data):
    tmp = _tmp_name(path)
    try:
        with _open_excl(tmp) as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


# ----------------------------------------------------------- demo / doctor

def fake_payload(five, ctx, seven, cost, mins, dirname="status-hero"):
    now = time.time()
    return {
        "session_id": "demo", "cwd": "/tmp/" + dirname,
        "workspace": {"current_dir": "/tmp/" + dirname},
        "model": {"display_name": "Fable 5"},
        "effort": {"level": "xhigh"},
        "cost": {"total_cost_usd": cost, "total_duration_ms": mins * 60000},
        "context_window": {
            "used_percentage": ctx, "context_window_size": 200000,
            "total_input_tokens": int(ctx * 2000),
        },
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": now + 7900},
            "seven_day": {"used_percentage": seven, "resets_at": now + 260000},
        },
    }


def demo():
    for name, f, c, s, cost, m in (
            ("fresh morning", 8, 12, 22, 1.4, 9),
            ("deep in it", 55, 64, 48, 14.79, 96),
            ("red zone", 93, 91, 78, 41.20, 260)):
        print(DIM + "── %s " % name + "─" * 30 + RESET)
        print(render(fake_payload(f, c, s, cost, m)))
    print(DIM + "── no rate limits (API billing) " + "─" * 14 + RESET)
    p = fake_payload(0, 33, 0, 3.10, 25)
    del p["rate_limits"]
    print(render(p))


def simulate():
    sys.stdout.write("\x1b[?25l")
    try:
        for t in range(0, 101, 1):
            payload = fake_payload(t, min(98, t * 1.15), 30 + t // 4,
                                   t * 0.45, t * 3)
            out = render(payload)
            sys.stdout.write(out + "\n\x1b[3A")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\x1b[3B\n")
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def doctor():
    print("claude-status-hero doctor")
    print("  COLUMNS env : %r" % os.environ.get("COLUMNS"))
    print("  COLORTERM   : %r  → %s" % (os.environ.get("COLORTERM"),
          "truecolor" if TRUECOLOR else "256-color"))
    print("  ASCII mode  : %s   ambiguous-wide: %s" % (ASCII, AMBIG_WIDE))
    print("  state dir   : %s" % STATE_DIR)
    print("alignment check — all rows below must end at the same column:")
    w = 40
    print("  |" + "-" * w + "|")
    print("  |" + crop_pad("ascii " + "x" * 50, w) + "|")
    print("  |" + crop_pad("blocks █████░░░░░ boxes ────", w) + "|")
    print("  |" + crop_pad("emoji 🦊⭐🏁⚡❗💤 wide 中文字符", w) + "|")
    print("  |" + crop_pad(bar(64, 20) + " " + GOLD + "⭐" + RESET, w) + "|")
    print("if a row overshoots: your terminal renders some glyph wider than")
    print("computed — set STATUS_HERO_ASCII=1 (or AMBIG_WIDE=1 for CJK-wide).")


# --------------------------------------------------------------------- main

def main(argv):
    if "--hook" in argv:
        try:
            handle_hook(argv[argv.index("--hook") + 1])
        except Exception:
            pass
        return 0
    if "--style" in argv:
        i = argv.index("--style")
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if val not in ("fleet", "gauge", "list"):
            print("usage: --style gauge|list|fleet")
            return 1
        save_config({"style": val})
        path = settings_file(argv)
        data = load_json(path) or {}
        sl = data.get("statusLine")
        if isinstance(sl, dict) and MARK in (sl.get("command") or ""):
            if val in ("fleet", "list"):
                sl["refreshInterval"] = 2   # keep other windows' states fresh
            else:
                sl.pop("refreshInterval", None)
            atomic_write_pretty(path, data)
            print("statusLine.refreshInterval %s in %s"
                  % ("→ 2s" if val in ("fleet", "list") else "removed", path))
        desc = {"gauge": "3 lines: HUD + 5h track + meters",
                "list": "7 lines: gauge + one compact row per session",
                "fleet": "10 lines: HUD + 5h track + static fleet scene"}
        print("style → %s  (%s)" % (val, desc[val]))
        print("takes effect on the next statusline refresh.")
        return 0
    if "--install" in argv:
        install(argv)
        return 0
    if "--uninstall" in argv:
        uninstall(argv)
        return 0
    if "--demo" in argv:
        demo()
        return 0
    if "--simulate" in argv:
        simulate()
        return 0
    if "--doctor" in argv:
        doctor()
        return 0

    # statusline mode: never crash, always 3 lines
    try:
        data = json.loads(sys.stdin.read(1000000) or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    try:
        sys.stdout.write(render(data) + "\n")
    except Exception:
        try:
            n = {"fleet": 10, "list": 7}.get(load_config().get("style"), 3)
        except Exception:
            n = 3
        sys.stdout.write("status-hero\n(render error — run --doctor)\n"
                         + " \n" * (n - 2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
