#!/usr/bin/env python3
"""claude-status-hero test suite — stdlib only, run: python3 tests/test_render.py

The contracts under test:
  1. hero_line prints EXACTLY 3 lines, each EXACTLY W display columns,
     for any payload (including garbage) and any COLUMNS.
  2. No control chars leak except SGR color sequences.
  3. The hook state machine transitions correctly; SessionEnd tombstones
     (never deletes: an in-flight statusline render must not resurrect the
     session; the board buries tombstones on sight).
  4. --install / --uninstall are idempotent and preserve foreign entries.
  5. hero_board --once renders every line at exactly the same width, and
     never crashes across sizes/modes.

Width is measured with the same East-Asian-Width rules the scripts use, so
these tests prove internal consistency (padding/cropping/accounting bugs);
cross-terminal truth is checked by eye via `hero_line.py --doctor`.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LINE = os.path.join(ROOT, "hero_line.py")
BOARD = os.path.join(ROOT, "hero_board.py")
PY = sys.executable or "python3"

ANSI = re.compile(r"\x1b\[[0-9;:]*m")
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        FAILS.append(name)
        print("FAIL  %s  %s" % (name, detail))


def ch_width(ch, ambig_wide=False):
    o = ord(ch)
    if o == 0xFE0F or unicodedata.combining(ch):
        return 0
    for lo, hi in ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF)):
        if lo <= o <= hi:
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    ea = unicodedata.east_asian_width(ch)
    if ea in ("W", "F"):
        return 2
    if ea == "A":
        return 2 if ambig_wide else 1
    return 1


def disp_width(s, ambig_wide=False):
    return sum(ch_width(c, ambig_wide) for c in ANSI.sub("", s))


def run(script, args=(), stdin="", env_extra=None, cols="100", lines="30"):
    env = dict(os.environ)
    env.pop("STATUS_HERO_ASCII", None)
    env.pop("STATUS_HERO_AMBIG_WIDE", None)
    env.pop("NO_COLOR", None)
    env["STATUS_HERO_DIR"] = env_extra.pop("_dir") if env_extra and "_dir" in env_extra \
        else os.path.join(tempfile.mkdtemp(prefix="sh-test-"), "state")
    if cols is not None:
        env["COLUMNS"] = cols
    else:
        env.pop("COLUMNS", None)
    env["LINES"] = lines
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY, script] + list(args), input=stdin,
                       capture_output=True, text=True, timeout=30, env=env)
    return p


def payload(**over):
    base = {
        "session_id": "t-abc", "cwd": "/tmp/proj",
        "workspace": {"current_dir": "/tmp/proj"},
        "model": {"display_name": "Fable 5"},
        "effort": {"level": "xhigh"},
        "cost": {"total_cost_usd": 12.34, "total_duration_ms": 3600000},
        "context_window": {"used_percentage": 42.0, "context_window_size": 200000,
                           "total_input_tokens": 84000},
        "rate_limits": {"five_hour": {"used_percentage": 34.0,
                                      "resets_at": time.time() + 8000},
                        "seven_day": {"used_percentage": 55.0,
                                      "resets_at": time.time() + 200000}},
    }
    base.update(over)
    return json.dumps(base)


# ------------------------------------------------- 1+2: geometry invariants

print("== hero_line geometry ==")
PAYLOADS = {
    "full": payload(),
    "empty": "{}",
    "not-a-dict": "[1,2,3]",
    "broken-json": "{oops",
    "no-rate-limits": payload(rate_limits=None),
    "null-ctx": payload(context_window={"used_percentage": None}),
    "cjk-dir": payload(workspace={"current_dir": "/tmp/深度研究项目"}),
    "ansi-inject": payload(workspace={"current_dir": "/tmp/\x1b[31mevil\r\n[8;1H"}),
    "huge": payload(cost={"total_cost_usd": 987654.32,
                          "total_duration_ms": 9.9e12}),
    "negative": payload(rate_limits={"five_hour": {"used_percentage": -5,
                                                   "resets_at": 0}}),
    "over-100": payload(rate_limits={"five_hour": {"used_percentage": 140,
                                                   "resets_at": "bogus"}}),
    "nan-inf": payload(cost={"total_cost_usd": float("nan"),
                             "total_duration_ms": float("inf")},
                       context_window={"used_percentage": float("nan")}),
}
ENVS = {
    "default": {},
    "ascii": {"STATUS_HERO_ASCII": "1"},
    "ambig-wide": {"STATUS_HERO_AMBIG_WIDE": "1"},
    "no-color": {"NO_COLOR": "1"},
}
for cols in ("40", "60", "80", "120", "200", "garbage", None):
    exp_w = None
    if cols and cols.isdigit():
        exp_w = max(40, min(int(cols) - 2, 100))
    for pname, pl in PAYLOADS.items():
        for ename, ev in ENVS.items():
            tag = "%s/%s/cols=%s" % (pname, ename, cols)
            p = run(LINE, stdin=pl, env_extra=dict(ev), cols=cols)
            lines = p.stdout.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            check(tag + " exit0", p.returncode == 0, p.stderr[:200])
            check(tag + " 3 lines", len(lines) == 3, "got %d" % len(lines))
            if exp_w is not None:
                aw = ename == "ambig-wide"
                widths = [disp_width(ln, aw) for ln in lines]
                check(tag + " width==%d" % exp_w, widths == [exp_w] * 3,
                      "got %s" % widths)
            plain = ANSI.sub("", p.stdout)
            bad = [c for c in plain if ord(c) < 32 and c != "\n"]
            check(tag + " no ctrl leak", not bad, repr(bad[:5]))

# --------------------------------------------------- 3: hook state machine

print("== hook state machine ==")
state_dir = os.path.join(tempfile.mkdtemp(prefix="sh-hooks-"), "state")
sess_file = os.path.join(state_dir, "sessions", "t-hooks.json")


def hook(event, extra=None):
    body = {"session_id": "t-hooks", "cwd": "/tmp/proj"}
    body.update(extra or {})
    return run(LINE, ["--hook", event], stdin=json.dumps(body),
               env_extra={"_dir": state_dir})


def state():
    with open(sess_file) as f:
        return json.load(f)


hook("SessionStart")
check("SessionStart→working", state()["state"] == "working")
check("started_at recorded", "started_at" in state())
first_start = state()["started_at"]
hook("UserPromptSubmit", {"prompt": "fix the \x1b[31mtests\r\n please"})
s = state()
check("prompt→working+activity", s["state"] == "working"
      and s["activity"].startswith("you: fix the tests"), s.get("activity"))
check("prompt ANSI sanitized", "\x1b" not in s["activity"] and "\r" not in s["activity"])
hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})
check("tool→activity", state()["activity"] == "Bash: pytest -q", state().get("activity"))
hook("NeedsYouPermission")
check("permission→needs_you", state()["state"] == "needs_you")
hook("Stop")
check("Stop→idle", state()["state"] == "idle")
hook("PreCompact")
check("PreCompact→compacting", state()["state"] == "compacting")
hook("PostCompact")
check("PostCompact→working", state()["state"] == "working")
check("started_at stable", state()["started_at"] == first_start)
hook("SessionEnd")
s = state()
check("SessionEnd writes tombstone", s.get("ended") is True and "state" not in s)
p = hook("UnknownEvent")
check("unknown hook exits 0", p.returncode == 0)
check("unknown hook leaves tombstone alone", state().get("ended") is True)
# an in-flight statusline render (metrics-only write) must NOT resurrect it
p = run(LINE, stdin=payload(session_id="t-hooks"), env_extra={"_dir": state_dir})
check("statusline can't resurrect tombstone", p.returncode == 0
      and state().get("ended") is True and "state" not in state())
# ...but a real hook may (resumed session reuses its session_id)
hook("SessionStart")
s = state()
check("SessionStart resurrects tombstone",
      s.get("state") == "working" and not s.get("ended"))

# ----------------------------------------------- 4: installer idempotence

print("== installer ==")
tmp = tempfile.mkdtemp(prefix="sh-install-")
settings = os.path.join(tmp, "settings.json")
fixture = {
    "model": "claude-fable-5",
    "statusLine": {"type": "command", "command": "python3 /old/statusline.py"},
    "hooks": {"Stop": [{"hooks": [{"type": "command",
                                   "command": "echo foreign-hook"}]}]},
}
with open(settings, "w") as f:
    json.dump(fixture, f)

run(LINE, ["--install", "--settings", settings])
d = json.load(open(settings))
check("statusLine points at hero_line", "hero_line.py" in d["statusLine"]["command"])
check("model key preserved", d["model"] == "claude-fable-5")
check("foreign Stop hook preserved",
      any("foreign-hook" in json.dumps(g) for g in d["hooks"]["Stop"]))
check("our Stop hook added",
      any("hero_line.py --hook Stop" in json.dumps(g) for g in d["hooks"]["Stop"]))
check("Notification matchers", sorted(g.get("matcher", "") for g in
      d["hooks"]["Notification"]) == ["idle_prompt", "permission_prompt"],
      json.dumps(d["hooks"].get("Notification")))

run(LINE, ["--install", "--settings", settings])       # second install
d2 = json.load(open(settings))
ours_in_stop = [g for g in d2["hooks"]["Stop"] if "hero_line.py" in json.dumps(g)]
check("re-install idempotent", len(ours_in_stop) == 1, "got %d" % len(ours_in_stop))
backups = [f for f in os.listdir(tmp) if "status-hero-backup" in f]
check("backups written", len(backups) >= 2, "got %d" % len(backups))

run(LINE, ["--uninstall", "--settings", settings])
d3 = json.load(open(settings))
check("uninstall removes statusLine", "statusLine" not in d3)
check("uninstall keeps foreign hook",
      any("foreign-hook" in json.dumps(g) for g in d3.get("hooks", {}).get("Stop", [])))
check("uninstall removes our hooks", "hero_line.py" not in json.dumps(d3))

# ------------------------------- 4b: malformed settings (review regressions)

print("== malformed settings ==")
tmp2 = tempfile.mkdtemp(prefix="sh-mal-")
for name, fixture in (
        ("hooks-null", {"hooks": None}),
        ("hooks-str", {"hooks": "oops"}),
        ("hooks-list", {"hooks": [1, 2]}),
        ("event-str", {"hooks": {"Stop": "foo"}}),
        ("event-null", {"hooks": {"Stop": None}})):
    sp = os.path.join(tmp2, name + ".json")
    with open(sp, "w") as f:
        json.dump(fixture, f)
    p = run(LINE, ["--install", "--settings", sp])
    check("install %s exit0" % name, p.returncode == 0, p.stderr[:200])
    d = json.load(open(sp))
    check("install %s statusLine set" % name,
          "hero_line.py" in d.get("statusLine", {}).get("command", ""))
    stop = d.get("hooks", {}).get("Stop", [])
    check("install %s Stop sane" % name, isinstance(stop, list)
          and all(isinstance(g, dict) for g in stop), json.dumps(stop)[:120])
    p = run(LINE, ["--uninstall", "--settings", sp])
    check("uninstall %s exit0" % name, p.returncode == 0, p.stderr[:200])

sp = os.path.join(tmp2, "un-null-event.json")
with open(sp, "w") as f:
    json.dump({"hooks": {"Stop": None}}, f)
p = run(LINE, ["--uninstall", "--settings", sp])
check("uninstall event-null exit0", p.returncode == 0, p.stderr[:200])
check("uninstall event-null untouched",
      json.load(open(sp))["hooks"]["Stop"] is None)

# shell quoting of metacharacters (review regression)
p = subprocess.run([PY, "-c", (
    "import importlib.util,sys;"
    "spec=importlib.util.spec_from_file_location('hl', %r);"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
    "out=m.shell_cmd('/usr/bin/python3','/tmp/a$(rm -rf x)/b.py','--hook','X');"
    "print(out)") % LINE], capture_output=True, text=True, timeout=15)
check("shell_cmd quotes metachars",
      "'/tmp/a$(rm -rf x)/b.py'" in p.stdout, p.stdout.strip())

# ------------------------------------------------------- 5: board geometry

print("== hero_board ==")
for cols, rows in (("60", "15"), ("100", "30"), ("200", "50")):
    for mode in ([], ["--list"], ["--office"]):
        for ev in ({}, {"STATUS_HERO_ASCII": "1"}):
            tag = "board cols=%s rows=%s %s%s" % (
                cols, rows, mode[0][2:] if mode else "scene",
                " ascii" if ev else "")
            p = run(BOARD, ["--demo", "--once"] + mode, env_extra=dict(ev),
                    cols=cols, lines=rows)
            check(tag + " exit0", p.returncode == 0, p.stderr[:200])
            out = [ln for ln in p.stdout.split("\n") if ln != ""]
            widths = {disp_width(ln) for ln in out}
            check(tag + " uniform width", len(widths) == 1
                  and widths == {int(cols) - 1}, "got %s" % sorted(widths))
            check(tag + " fits height", len(out) <= int(rows) + 22,
                  "got %d lines" % len(out))

# board reads real session files: stale cleanup + ordering
state_dir2 = os.path.join(tempfile.mkdtemp(prefix="sh-fleet-"), "state")
sess2 = os.path.join(state_dir2, "sessions")
os.makedirs(sess2)
now = time.time()
mk = lambda sid, started, ts, st="working": json.dump(
    {"sid": sid, "dir": sid, "hero": "fox", "state": st, "state_ts": ts,
     "ts": ts, "started_at": started, "ctx": 50, "cost": 1.0},
    open(os.path.join(sess2, sid + ".json"), "w"))
mk("younger", now - 10, now)
mk("older", now - 500, now)
mk("corpse", now - 90000, now - 90000)   # >24h: should be buried
json.dump({"sid": "gone", "ended": True, "ts": now},       # fresh tombstone
          open(os.path.join(sess2, "gone.json"), "w"))
json.dump({"sid": "gone2", "ended": True, "ts": now - 120},  # aged tombstone
          open(os.path.join(sess2, "gone2.json"), "w"))
p = run(BOARD, ["--once", "--list"], env_extra={"_dir": state_dir2},
        cols="120", lines="30")
out = ANSI.sub("", p.stdout)
check("board hides day-old corpse", "corpse" not in out)
check("corpse file removed", not os.path.exists(os.path.join(sess2, "corpse.json")))
check("board hides fresh tombstone", "gone" not in out)
check("fresh tombstone KEPT on disk",      # burying it early would re-open
      os.path.exists(os.path.join(sess2, "gone.json")))   # the delete race
check("aged tombstone buried",
      not os.path.exists(os.path.join(sess2, "gone2.json")))
check("lane order by started_at",
      out.find("older") != -1 and out.find("older") < out.find("younger"))

# hostile session files must never crash the board (NaN/inf pass json.load;
# non-string state/hero/sid would TypeError on dict keying)
state_dirH = os.path.join(tempfile.mkdtemp(prefix="sh-hostile-"), "state")
sessH = os.path.join(state_dirH, "sessions")
os.makedirs(sessH)
open(os.path.join(sessH, "nan.json"), "w").write(
    '{"sid":"nan","dir":"nan","hero":"fox","state":"working",'
    '"state_ts":%d,"ts":%d,"started_at":1,"ctx":NaN,"cost":Infinity,'
    '"five":NaN,"five_reset":NaN}' % (now, now))
json.dump({"sid": "types", "dir": "types", "hero": ["cat"], "state": {"x": 1},
           "state_ts": now, "ts": now, "started_at": 2, "ctx": 50},
          open(os.path.join(sessH, "types.json"), "w"))
json.dump({"sid": ["evil"], "dir": "evil", "ts": now},   # unhashable sid
          open(os.path.join(sessH, "badsid.json"), "w"))
for mode in ([], ["--list"], ["--office"]):
    mname = mode[0][2:] if mode else "scene"
    p = run(BOARD, ["--once"] + mode, env_extra={"_dir": state_dirH},
            cols="100", lines="30")
    outl = [ln for ln in p.stdout.split("\n") if ln != ""]
    check("board hostile files %s exit0" % mname, p.returncode == 0,
          p.stderr[:300])
    check("board hostile files %s uniform width" % mname,
          {disp_width(ln) for ln in outl} == {99},
          "got %s" % sorted({disp_width(ln) for ln in outl}))

# CJK names/activity must not shear scene lanes (review regression)
state_dir3 = os.path.join(tempfile.mkdtemp(prefix="sh-cjk-"), "state")
sess3 = os.path.join(state_dir3, "sessions")
os.makedirs(sess3)
for i, (sid, d_, act) in enumerate((
        ("cjk1", "深度研究项目", "you: 修复测试并推送"),
        ("asc1", "plain-project", "Bash: pytest -q"),
        ("cjk2", "招股书分析", "Read: 财报.pdf"))):
    json.dump({"sid": sid, "dir": d_, "hero": ["fox", "cat", "frog"][i],
               "state": "working", "state_ts": now, "ts": now,
               "started_at": now - i, "ctx": 40 + i * 20, "cost": 5.0,
               "activity": act},
              open(os.path.join(sess3, sid + ".json"), "w"))
for mode in ([], ["--list"], ["--office"]):
    mname = mode[0][2:] if mode else "scene"
    p = run(BOARD, ["--once"] + mode, env_extra={"_dir": state_dir3},
            cols="100", lines="30")
    outl = [ln for ln in p.stdout.split("\n") if ln != ""]
    widths = {disp_width(ln) for ln in outl}
    check("board CJK %s uniform width" % mname,
          widths == {99}, "got %s" % sorted(widths))
    check("board CJK %s shows name" % mname,
          "深度研" in ANSI.sub("", p.stdout))

# office v3: emoji actors (identical glyph to the statusline), not big
# half-block sprites — and cost + activity (speech bubble) ARE on the floor
# plan now; ctx/usage meters are NOT (those stay in the statusline/list).
p = run(BOARD, ["--demo", "--once", "--office"], cols="100", lines="30")
plain = ANSI.sub("", p.stdout)
check("office draws the room", "┌" in plain and "└" in plain and "│" in plain)
check("office has a break room (茶水间)", "茶水间" in plain, plain[:400])
check("office shows a desk name plate", "sightlab" in plain, plain[:400])
check("office actor is the statusline hero glyph", "🦊" in p.stdout, p.stdout[:400])
check("office shows a state beacon", "⚡" in p.stdout, p.stdout[:400])
check("office shows per-desk cost", "$31.26" in plain, plain[:400])
check("office has no ctx%/usage on the floor plan",
      "ctx" not in plain and "%" not in plain, plain[:400])
check("office speech bubble carries the activity string",
      "(" in plain and "pytest" in plain, plain[:400])
check("office header drops the 5h/7d usage block", "5h" not in plain,
      plain[:400])
check("office has no half-block sprites left", "▀" not in plain, plain[:400])
check("office has furniture (potted plant)", "🪴" in p.stdout, p.stdout[:400])
door_rows = [ln for ln in plain.split("\n")
             if ln.startswith(" ") and ln.rstrip().endswith("│")]
check("office has a door gap in the left wall", len(door_rows) >= 3,
      "got %d" % len(door_rows))

# scene mode keeps the full header (5h/7d) — only office trims it; a wide
# terminal is needed since the 5h/7d block is dropped when it wouldn't fit
p = run(BOARD, ["--demo", "--once"], cols="200", lines="30")
check("scene header keeps the 5h/7d usage block",
      "5h" in ANSI.sub("", p.stdout), p.stdout[:400])
p = run(BOARD, ["--demo", "--once", "--office"], cols="100", lines="15")
check("office short terminal falls back (no room)",
      "┌" not in ANSI.sub("", p.stdout))
state_dir9 = os.path.join(tempfile.mkdtemp(prefix="sh-nine-"), "state")
sess9 = os.path.join(state_dir9, "sessions")
os.makedirs(sess9)
for i in range(9):
    json.dump({"sid": "s%d" % i, "dir": "proj%d" % i, "hero": "fox",
               "state": "working", "state_ts": now, "ts": now,
               "started_at": now - i, "ctx": 10, "cost": 1.0},
              open(os.path.join(sess9, "s%d.json" % i), "w"))
p = run(BOARD, ["--once", "--office"], env_extra={"_dir": state_dir9},
        cols="200", lines="40")
plain9 = ANSI.sub("", p.stdout)
check("office >8 sessions falls back to list",
      "┌" not in plain9 and "proj8" in plain9)

# office walk animation (floor-plan): idle walks DOWN its column to the
# break room and back UP when working; new sids enter from the door and
# ended sids leave; needs_you snaps to its desk (attention > choreography)
anim_code = """
import sys; sys.path.insert(0, %r)
import hero_board as hb
def G(ss):                       # geo is recomputed per frame, like render
    return hb.office_geo(len(ss), 96, 20)
ss = [{'sid':'a','hero':'fox','_state':'working'},
      {'sid':'b','hero':'cat','_state':'working'}]
st = {}
hb.office_move(st, ss, G(ss))                     # init: seat the fleet
placed, w = hb.office_move(st, ss, G(ss))
assert w == [] and all(not p[3] for p in placed.values()), placed  # all seated
geo = G(ss); seat_y = geo['seats'][0][1]; lounge_y = geo['lounges'][0][1]
assert placed['a'][1] == seat_y and placed['a'][4] is True
# 'a' goes idle -> must WALK DOWN its column toward the break room
ss[0]['_state'] = 'idle'
ys = []
for _ in range(60):
    placed, w = hb.office_move(st, ss, G(ss))
    ys.append(placed['a'][1])
    if not placed['a'][3]: break
assert ys == sorted(ys) and ys[-1] == lounge_y, ys      # monotone downward
assert placed['a'][4] is False                          # now on break
# new session enters from the door (x climbs from 0), then seats
ss.append({'sid':'c','hero':'frog','_state':'working'})
xs = []
for _ in range(80):
    placed, w = hb.office_move(st, ss, G(ss))
    xs.append(placed['c'][0])
    if not placed['c'][3]: break
assert xs[0] < xs[-1] and xs == sorted(xs), xs
# ended session walks out and is dropped
ss.pop()
seen = False
for _ in range(200):
    placed, w = hb.office_move(st, ss, G(ss))
    if w: seen = True
    if not w and seen: break
assert seen and not st['leaving'], st
# needs_you snaps straight to its desk, no walk
ss.append({'sid':'d','hero':'owl','_state':'needs_you'})
placed, w = hb.office_move(st, ss, G(ss))
assert placed['d'][3] is False and placed['d'][1] == G(ss)['seats'][2][1], placed
# the LAST session's walk-out must play in an otherwise-empty room
st2 = {}
hb.render_office([{'sid':'z','hero':'duck','_state':'working'}],
                 99, 29, 0, True, st2)
lines = hb.render_office([], 99, 29, 1, True, st2)
assert st2['leaving'], st2
assert not any('no live sessions' in l for l in lines), 'room vanished'
print('anim-ok')
""" % ROOT
p = subprocess.run([PY, "-c", anim_code], capture_output=True, text=True,
                   timeout=30)
check("office walk anim deterministic + terminates",
      "anim-ok" in p.stdout, (p.stderr or p.stdout)[:400])

# narrow list mode must not truncate the cost column (review regression)
p = run(BOARD, ["--demo", "--once", "--list"], cols="60", lines="20")
plain = ANSI.sub("", p.stdout)
check("board narrow list cost intact",
      len(re.findall(r"\$\s*\d+\.\d\d", plain)) >= 5, plain[:200])

# ASCII mode board must be pure ASCII (review regression)
for mode in ([], ["--office"]):
    p = run(BOARD, ["--demo", "--once"] + mode,
            env_extra={"STATUS_HERO_ASCII": "1"}, cols="100", lines="30")
    plain = ANSI.sub("", p.stdout)
    nonascii = sorted({c for c in plain if ord(c) > 127})
    check("board ASCII %s pure ascii" % (mode[0][2:] if mode else "scene"),
          not nonascii, repr(nonascii[:10]))
p = run(LINE, stdin=payload(), env_extra={"STATUS_HERO_ASCII": "1"}, cols="100")
plain = ANSI.sub("", p.stdout)
nonascii = sorted({c for c in plain if ord(c) > 127})
check("line ASCII mode pure ascii", not nonascii, repr(nonascii[:10]))

# -------------------------------------------------- 6: fleet statusline style

print("== fleet style ==")


def fleet_dir(with_sessions=True):
    d = os.path.join(tempfile.mkdtemp(prefix="sh-flt-"), "state")
    os.makedirs(os.path.join(d, "sessions"))
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({"style": "fleet"}, f)
    if with_sessions:
        t = time.time()
        for i, (sid, dn, hero, st) in enumerate((
                ("s0", "sightlab", "fox", "working"),
                ("s1", "深度研究项目", "cat", "needs_you"),
                ("s2", "daily-news", "frog", "idle"))):
            with open(os.path.join(d, "sessions", sid + ".json"), "w") as f:
                json.dump({"sid": sid, "dir": dn, "hero": hero, "state": st,
                           "state_ts": t, "ts": t, "started_at": t - 100 + i,
                           "ctx": 30 + i * 30, "cost": 5.0}, f)
    return d


for cols in ("60", "100", "200"):
    for pl_name, pl in (("full", payload(session_id="s0")), ("empty", "{}")):
        for ename, ev in (("default", {}), ("ascii", {"STATUS_HERO_ASCII": "1"})):
            tag = "fleet %s/%s/cols=%s" % (pl_name, ename, cols)
            env = dict(ev)
            env["_dir"] = fleet_dir()
            p = run(LINE, stdin=pl, env_extra=env, cols=cols)
            lines = [ln for ln in p.stdout.split("\n") if ln != ""]
            check(tag + " exit0", p.returncode == 0, p.stderr[:200])
            check(tag + " 10 lines", len(lines) == 10, "got %d" % len(lines))
            exp_w = max(40, min(int(cols) - 2, 100))
            aw = False
            widths = [disp_width(ln, aw) for ln in lines]
            check(tag + " width==%d" % exp_w, widths == [exp_w] * 10,
                  "got %s" % sorted(set(widths)))

p = run(LINE, stdin="{}", env_extra={"_dir": fleet_dir(with_sessions=False)},
        cols="100")
lines = [ln for ln in p.stdout.split("\n") if ln != ""]
check("fleet empty fleet 10 lines", len(lines) == 10, "got %d" % len(lines))
check("fleet empty fleet message", "no fleet yet" in ANSI.sub("", p.stdout))

# list style: 7 lines, own session forced visible, overflow marker
print("== list style ==")


def list_dir(n_sessions):
    d = os.path.join(tempfile.mkdtemp(prefix="sh-lst-"), "state")
    os.makedirs(os.path.join(d, "sessions"))
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({"style": "list"}, f)
    t = time.time()
    heroes = ["fox", "cat", "frog", "owl", "penguin", "rabbit"]
    for i in range(n_sessions):
        sid = "ls%d" % i
        dn = "深度研究项目" if i == 1 else "proj-%d" % i
        with open(os.path.join(d, "sessions", sid + ".json"), "w") as f:
            json.dump({"sid": sid, "dir": dn, "hero": heroes[i % 6],
                       "state": "working", "state_ts": t, "ts": t,
                       "started_at": t - 100 + i, "ctx": 20 + i * 10,
                       "cost": 2.0, "activity": "Bash: task %d" % i}, f)
    return d


for cols in ("60", "100", "200"):
    for n in (0, 2, 6):
        for ename, ev in (("default", {}), ("ascii", {"STATUS_HERO_ASCII": "1"})):
            tag = "list n=%d/%s/cols=%s" % (n, ename, cols)
            env = dict(ev)
            env["_dir"] = list_dir(n)
            p = run(LINE, stdin=payload(session_id="ls5",
                                        workspace={"current_dir": "/tmp/own-proj"}),
                    env_extra=env, cols=cols)
            lines = [ln for ln in p.stdout.split("\n") if ln != ""]
            check(tag + " exit0", p.returncode == 0, p.stderr[:200])
            check(tag + " 7 lines", len(lines) == 7, "got %d" % len(lines))
            exp_w = max(40, min(int(cols) - 2, 100))
            widths = [disp_width(ln) for ln in lines]
            check(tag + " width==%d" % exp_w, widths == [exp_w] * 7,
                  "got %s" % sorted(set(widths)))
            plain = ANSI.sub("", p.stdout)
            if n == 6:
                # ls5 (own, started last of 6 live) must still be visible;
                # the render persists its dir as own-proj from the payload
                check(tag + " own visible", "own-proj" in plain)
                check(tag + " overflow marker", re.search(r"\+\d", plain),
                      plain[:120])

# --style CLI wiring
tmp3 = tempfile.mkdtemp(prefix="sh-style-")
sp3 = os.path.join(tmp3, "settings.json")
d3 = os.path.join(tmp3, "state")
run(LINE, ["--install", "--settings", sp3], env_extra={"_dir": d3})
p = run(LINE, ["--style", "fleet", "--settings", sp3], env_extra={"_dir": d3})
check("--style fleet exit0", p.returncode == 0, p.stderr[:200])
check("--style fleet config", json.load(open(os.path.join(d3, "config.json")))
      ["style"] == "fleet")
check("--style fleet refreshInterval",
      json.load(open(sp3))["statusLine"].get("refreshInterval") == 2)
p = run(LINE, ["--style", "gauge", "--settings", sp3], env_extra={"_dir": d3})
check("--style gauge exit0", p.returncode == 0)
check("--style gauge no refreshInterval",
      "refreshInterval" not in json.load(open(sp3))["statusLine"])
p = run(LINE, ["--style", "bogus", "--settings", sp3], env_extra={"_dir": d3})
check("--style bogus rejected", p.returncode == 1)

# ------------------------------------------------------------------ result

print()
if FAILS:
    print("%d FAILURES:" % len(FAILS))
    for f in FAILS[:20]:
        print("  - " + f)
    sys.exit(1)
n = "all green"
print(n)
