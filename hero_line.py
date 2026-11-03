#!/usr/bin/env python3
"""claude-status-hero · hero_line.py — the gauge.

A Claude Code statusline that is fun but NEVER jitters:
  • fixed height per style: 3 gauge / 7 list / 10 fleet lines, exact display width
  • a hero emoji walks the 5-hour rate-limit track (coins at 20% budget marks)
  • context + 7-day meters, git, cost, and a cross-window fleet summary
  • doubles as the hook handler + state writer for hero_board.py

Modes (first argument):
  (none)          statusline: read Claude Code JSON on stdin, print the lines
  --hook EVENT    hook handler: update this session's state file, print nothing
  --install       register statusline + hooks in ~/.claude/settings.json
  --uninstall     remove our entries (a timestamped backup is written either way)
  --snippets X    on|off: store redacted prompt/tool snippets (default on)
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

from contextlib import contextmanager
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
    width = max(0, width)
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
    s = ANSI_TOKEN.sub("", s[:4096])
    s = "".join(ch for ch in s if unicodedata.category(ch)
                not in ("Cc", "Cf", "Cs", "Zl", "Zp")).strip()
    if ASCII:
        s = s.encode("ascii", "replace").decode("ascii")
    if len(s) > limit:
        s = s[: limit - 2] + ".."
    return s


_SECRETS = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]+|(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]+"
    r"|xox[abpr]-[A-Za-z0-9-]+|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
    r"|\bBearer\s+[^\s\"'`,;]+"
    r"|\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?<=://)[^\s/@:]+:[^\s/]*@"  # user has no ':', so one split: linear time
    r"|\b(?:[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s;,&]+)", re.I)


def redact(s):
    """Mask recognizable credentials before truncation or persistence."""
    return _SECRETS.sub("[REDACTED]", s) if isinstance(s, str) else ""


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
    except (TypeError, ValueError, OverflowError):
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
    filled = 0 if p is None else max(0, min(width, int(round(max(0.0, min(100.0, p)) * width / 100.0))))
    col = zone(p)
    return col + G_FILL * filled + GRAY + DIM + G_EMPTY * (width - filled) + RESET


def pct_txt(pct):
    p = num(pct)
    return "--%" if p is None else "%d%%" % int(round(p))


# --------------------------------------------------------------- session I/O

def _ensure_dirs():
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.makedirs(SESS_DIR, mode=0o700, exist_ok=True)
    except Exception:
        pass


def session_key(sid):
    raw = str(sid)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:80] or "unknown"
    if safe != raw:
        # Sanitizing/truncating alone aliases distinct untrusted identifiers.
        digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        safe = safe[:48] + "-" + digest
    return safe


def sess_path(sid):
    return os.path.join(SESS_DIR, session_key(sid) + ".json")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ---- Fable weekly (model-scoped) usage -------------------------------------
# CC forwards ONLY five_hour/seven_day into the statusline stdin, so a
# model-scoped weekly window (e.g. Fable) is read from CC's own on-disk cache
# of GET /api/oauth/usage (~/.claude.json → .cachedUsageUtilization) — read
# only, no network/tokens; CC refreshes it on its own schedule. Same number as
# /usage's "Current week (Fable)" bar; mirrors the ai-repo statusline plugin
# v1.5.0 jq extraction (.utilization.limits[] where kind=="weekly_scoped" and
# scope.model.display_name in ("Fable", "Fable 5")).
USAGE_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".claude.json")
FABLE_STALE_S = 12 * 3600  # '*' suffix when the cached usage snapshot is older


def read_fable(now=None):
    """(pct, stale) for the Fable weekly window, or None when there's no such
    window / no cache — the segment then hides itself (matches the plugin)."""
    cache = get(load_json(USAGE_CACHE_FILE), "cachedUsageUtilization")
    limits = get(cache, "utilization", "limits")
    if not isinstance(limits, list):
        return None
    pct = None
    for lim in limits:
        if (isinstance(lim, dict) and lim.get("kind") == "weekly_scoped"
                and get(lim, "scope", "model", "display_name") in ("Fable", "Fable 5")):
            pct = num(lim.get("percent"))
            break
    if pct is None:
        return None
    fetched_ms = num(get(cache, "fetchedAtMs")) or 0
    stale = (now or time.time()) - fetched_ms / 1000.0 > FABLE_STALE_S
    return pct, stale


def _open_excl(path):
    """Create-only open: never follows a pre-planted symlink, private perms."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def _tmp_name(path):
    return "%s.tmp.%d.%d" % (path, os.getpid(), time.time_ns() % 1000000)


def atomic_write(path, data, strict=False):
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
        if strict:
            raise


@contextmanager
def file_lock(path, timeout=1.0):
    """Bounded advisory lock, released by the OS even if a hook is killed.
    Keep the lock inode: unlinking it could let two writers lock different files.
    """
    import stat
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            if not os.fstat(fd).st_size:
                os.write(fd, b"0")
        else:
            import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("status-hero lock timed out")
                time.sleep(.005)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def session_lock(timeout=1.0):
    _ensure_dirs()
    return file_lock(os.path.join(STATE_DIR, "sessions.lock"), timeout=timeout)


@contextmanager
def hook_session_lock():
    # Hooks have a 5s timeout. After 3s, retain the original last-writer-wins
    # behavior rather than silently losing a state change (including SessionEnd).
    from contextlib import ExitStack
    with ExitStack() as stack:
        try:
            stack.enter_context(session_lock(timeout=3.0))
        except TimeoutError:
            pass
        yield


def update_session(sid, updates, resume=False, hook=False):
    if not sid:
        return
    sid = session_key(sid)
    try:
        with hook_session_lock() if hook else session_lock():
            path = sess_path(sid)
            s = load_json(path) or {}
            if s.get("ended") and not resume:
                return  # only SessionStart may resume; late hooks stay buried
            if resume:
                s.pop("ended", None)
            s.update(updates)
            if load_config().get("snippets", "on") == "off":
                s.pop("activity", None)
                s.pop("activity_ts", None)
            s["sid"] = str(sid)[:80]
            s["ts"] = time.time()
            if "hero" not in s:
                s["hero"] = pick_hero(sid)
            if "started_at" not in s:
                s["started_at"] = s["ts"]
            atomic_write(path, s)
    except OSError:
        pass  # state persistence must not suppress the gauge


def load_config():
    return load_json(os.path.join(STATE_DIR, "config.json")) or {}


def save_config(updates):
    _ensure_dirs()
    cfg = load_config()
    cfg.update(updates)
    atomic_write(os.path.join(STATE_DIR, "config.json"), cfg, strict=True)


def prune_session(path, snapshot):
    """Do not unlink a session a concurrent hook has just refreshed."""
    with session_lock():
        if load_json(path) == snapshot:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def pid_alive(pid):
    p = num(pid)
    if not p or p <= 0 or os.name == "nt":
        return None
    try:
        os.kill(int(p), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return None


def all_sessions(cleanup=True):
    out = []
    try:
        names = sorted(n for n in os.listdir(SESS_DIR) if n.endswith(".json"))
    except Exception:
        return out
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(SESS_DIR, n)
        s = load_json(path)
        if not s or not isinstance(s.get("sid"), str) or not s.get("sid"):
            continue
        age = now - (num(s.get("ts")) or 0)
        expired = (age > 24 * 3600 and pid_alive(s.get("ppid")) is False
                   or age > 7 * 24 * 3600 and not s.get("ppid"))
        if s.get("ended") or expired:
            if cleanup and (expired or (s.get("ended") and age > 600)):
                try:
                    prune_session(path, s)
                except OSError:
                    pass
            continue
        if not isinstance(s.get("state"), str):
            s["state"] = None
        if not isinstance(s.get("hero"), str):
            s["hero"] = None
        out.append(s)
    return out


def pick_hero(sid):
    claimed = {s.get("hero") for s in all_sessions(cleanup=False) if s.get("sid") != str(sid)}
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
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return ""
    _ensure_dirs()
    key = hashlib.sha1(cwd.encode()).hexdigest()[:16]
    cache = os.path.join(STATE_DIR, "git-" + key)
    try:
        if os.path.isfile(cache) and 0 <= time.time() - os.path.getmtime(cache) < 5:
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


def render_width(width=None):
    return max(1, min((term_width() if width is None else width) - 2, 100))


def render(data, width=None):
    """The contract: a CONSTANT number of lines (3 gauge / 10 fleet), each
    EXACTLY W columns wide."""
    W = render_width(width)

    cwd = get(data, "workspace", "current_dir") or get(data, "cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""
    dirname = sanitize(os.path.basename(cwd) or "?", 24)
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
    fable = read_fable()  # model-scoped weekly; None (segment hidden) if absent
    sid = get(data, "session_id")
    sid = session_key(sid) if sid else None

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
    have_fable = fable is not None
    # Fable is the new/optional meter, so it degrades out FIRST when width is
    # tight (fleet drops last) — leaving the no-Fable layout byte-identical.
    for attempt in range(4 if have_fable else 3):
        bw_c, bw_7 = (12, 10) if attempt == 0 else (8, 6)
        seg = [DIM + "ctx " + RESET + bar(ctx, bw_c) + " " + zone(ctx) + pct_txt(ctx) + RESET
               + (DIM + used_k + RESET if attempt == 0 else "")]
        seg.append(DIM + "7d " + RESET + bar(seven, bw_7) + " " + zone(seven) + pct_txt(seven)
                   + RESET + (" " + DIM + G_RESET_AT + fmt_reset(seven_rs) + RESET
                              if attempt == 0 else ""))
        if have_fable and attempt < 2:
            fpct, fstale = fable
            seg.append(DIM + "Fable " + RESET + bar(fpct, bw_7) + " " + zone(fpct)
                       + pct_txt(fpct) + (DIM + "*" if fstale else "") + RESET)
        if fleet and attempt < (3 if have_fable else 2):
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
    """Update session state with a bounded lock wait (CLI always exits 0)."""
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
    if not isinstance(payload, dict):
        return
    sid = payload.get("session_id")
    if not sid:
        return
    sid = session_key(sid)
    now = time.time()

    if event == "SessionEnd":
        # tombstone, not delete: an in-flight statusline render (git can take
        # seconds) would otherwise resurrect the file after os.remove and the
        # board would show a zombie lane. The board hides tombstones at once and prunes them after a grace period.
        with hook_session_lock():
            atomic_write(sess_path(sid), {"sid": str(sid)[:80], "ended": True,
                                         "ts": now})
        return

    upd = {}
    if event == "SessionStart":
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else ""
        upd = {"state": "working", "state_ts": now, "started_at": now,
               "cwd": sanitize(payload.get("cwd") or "", 200),
               "dir": sanitize(os.path.basename(cwd) or "?", 24)}
    elif event == "UserPromptSubmit":
        snippet = sanitize(redact(payload.get("prompt")), 60)
        upd = {"state": "working", "state_ts": now,
               "activity": ("you: " + snippet) if snippet else "prompt",
               "activity_ts": now}
    elif event == "PostToolUse":
        tool = sanitize(redact(payload.get("tool_name") or "tool"), 24)
        ti = payload.get("tool_input") or {}
        hint = ""
        if isinstance(ti, dict):
            hint = sanitize(redact(ti.get("command") or ti.get("file_path")
                            or ti.get("description") or ti.get("prompt") or ""), 40)
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
    update_session(sid, upd, resume=(event == "SessionStart"), hook=True)


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


def shell_cmd(*parts):
    """Commands run in a POSIX shell on every platform, including Windows."""
    import shlex
    return " ".join(shlex.quote(p.replace("\\", "/") if os.name == "nt" else p)
                    for p in parts)


def settings_file(argv):
    if "--settings" in argv:
        return argv[argv.index("--settings") + 1]
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def read_settings(path):
    """Missing settings are new; unreadable/invalid settings must never be reset."""
    if os.path.islink(path):
        raise ValueError("settings is a symlink; edit its target explicitly: %s" % path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object: %s" % path)
    return data


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


def settings_lock(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    return file_lock(path + ".status-hero.lock", timeout=4)


def settings_transaction(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(argv):
        with settings_lock(settings_file(argv)):
            return fn(argv)
    return wrapped


@settings_transaction
def install(argv):
    path = settings_file(argv)
    me = os.path.abspath(__file__)
    py = sys.executable or "python3"
    data = read_settings(path)
    backup = backup_settings(path)

    prev = data.get("statusLine")
    prev_owned = isinstance(prev, dict) and owned_command(prev.get("command"))
    stale = [prev] if prev_owned and owned_command(prev.get("command"), stale_only=True) else []
    if isinstance(prev, dict) and not prev_owned:
        save_config({"replaced_statusline": prev})
    sl = dict(prev) if prev_owned else {}
    sl.update(type="command", command=shell_cmd(py, me), padding=0)
    if load_config().get("style") in ("fleet", "list"):
        sl["refreshInterval"] = 2
    else:
        sl.pop("refreshInterval", None)
    data["statusLine"] = sl

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):   # null/str/list → start clean (backup exists)
        hooks = {}
        data["hooks"] = hooks
    events = {e for e, _m, _a in HOOK_EVENTS}
    for event in events:  # sweep this installation's entries, keep others
        cur = hooks.get(event, [])
        if not isinstance(cur, list):
            cur = []                  # malformed per-event value → reset
        hooks[event] = remove_owned_hooks(cur, stale_entries=stale)
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
    print("  hooks      : %d entries across %d events (state for hero_board.py)"
          % (len(HOOK_EVENTS), len(events)))
    print("  stale entries swept: %d" % len(stale))
    if prev and not prev_owned:
        print("  replaced   : previous statusline (saved in config and backup)")
    print("  rollback   : %s" % (shell_cmd("cp", backup, path) if backup else "remove the newly created settings file"))
    print("takes effect on the next statusline refresh; open a new Claude Code"
          " window (or send a message) to see it.")


@settings_transaction
def uninstall(argv):
    path = settings_file(argv)
    data = read_settings(path)
    backup = backup_settings(path)
    sl = data.get("statusLine")
    stale = []
    restored = False
    if isinstance(sl, dict) and owned_command(sl.get("command")):
        if owned_command(sl.get("command"), stale_only=True):
            stale.append(sl)
        replaced = load_config().get("replaced_statusline")
        if isinstance(replaced, dict):
            data["statusLine"] = replaced
            restored = True
        else:
            del data["statusLine"]
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            if not isinstance(hooks[event], list):
                continue              # not ours to touch
            kept = remove_owned_hooks(hooks[event], stale_entries=stale)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            data.pop("hooks", None)
    atomic_write_pretty(path, data)
    print("uninstalled (backup: %s)" % backup)
    print("  stale entries swept: %d" % len(stale))
    if restored:
        print("  restored previous statusline")


def owned_command(command, script="hero_line.py", mode=None, stale_only=False):
    """Recognize our Python invocation, not arbitrary mentions in other hooks."""
    if not isinstance(command, str):
        return False
    import ntpath
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 2:
        return False
    expected = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    normalize = ntpath.normcase if os.name == "nt" else os.path.normpath
    stale = False
    if script == "hero_line.py" and os.path.isabs(parts[1]) and ntpath.basename(parts[1]) == script:
        try:
            os.stat(parts[1])
        except FileNotFoundError:
            stale = True
        except OSError:
            pass  # inaccessible is not proof that another tool is gone
    if (stale_only and not stale) or (normalize(parts[1]) != normalize(expected) and not stale):
        return False
    if not re.fullmatch(r"(?:python(?:[0-9]+(?:\.[0-9]+)*)?|py)(?:\.exe)?",
                        ntpath.basename(parts[0]), re.I):
        return False
    if mode is not None:
        return len(parts) in (3, 4) and parts[2] == mode
    return len(parts) == 2 or (len(parts) == 4 and parts[2] == "--hook"
                              and parts[3] in {a for _e, _m, a in HOOK_EVENTS})


def remove_owned_hooks(groups, script="hero_line.py", mode=None, stale_entries=None):
    """Remove only owned commands inside a group; keep its other metadata."""
    out = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            out.append(group)
            continue
        kept = []
        for h in group["hooks"]:
            if (isinstance(h, dict) and h.get("type") == "command"
                    and owned_command(h.get("command"), script, mode)):
                if stale_entries is not None and owned_command(h.get("command"), script, mode, stale_only=True):
                    stale_entries.append(h)
            else:
                kept.append(h)
        if kept == group["hooks"]:
            out.append(group)
        elif kept:
            out.append(dict(group, hooks=kept))
    return out


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
    if "--snippets" in argv:
        i = argv.index("--snippets")
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if val not in ("on", "off"):
            print("usage: --snippets on|off")
            return 1
        with session_lock():
            save_config({"snippets": val})
            if val == "off":
                for name in os.listdir(SESS_DIR):
                    if not name.endswith(".json"):
                        continue
                    path = os.path.join(SESS_DIR, name)
                    s = load_json(path)
                    if s is not None and ("activity" in s or "activity_ts" in s):
                        s.pop("activity", None)
                        s.pop("activity_ts", None)
                        atomic_write(path, s, strict=True)
        print("snippets → %s" % val)
        return 0
    if "--style" in argv:
        i = argv.index("--style")
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if val not in ("fleet", "gauge", "list"):
            print("usage: --style gauge|list|fleet")
            return 1
        path = settings_file(argv)
        with settings_lock(path):
            data = read_settings(path)
            sl = data.get("statusLine")
            if isinstance(sl, dict) and owned_command(sl.get("command")):
                if val in ("fleet", "list"):
                    sl["refreshInterval"] = 2
                else:
                    sl.pop("refreshInterval", None)
                backup_settings(path)
                atomic_write_pretty(path, data)
                print("statusLine.refreshInterval %s in %s"
                      % ("→ 2s" if val in ("fleet", "list") else "removed", path))
            save_config({"style": val})
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

    # statusline mode: preserve the configured height, including on errors
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
        W = render_width()
        rows = ["status-hero", "(render error — run --doctor)"] + [""] * (n - 2)
        sys.stdout.write("\n".join(crop_pad(row, W) for row in rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
