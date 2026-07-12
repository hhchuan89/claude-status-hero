#!/usr/bin/env python3
"""claude-status-hero pixel-office test suite — stdlib only.

Run: python3 tests/test_pixel.py
(alongside tests/test_render.py, which this file does not modify or import)

Contracts under test:
  1. encode_sixel is byte-exact: DCS intro, raster attrs, palette dump with
     the documented 0-255->0-100 rescale, RLE for runs >=4, `$` between
     colors in a band, `-` between bands, ST terminator, partial-band math
     when H is not a multiple of 6.
  2. encode_png round-trips: signature, IHDR (packed W/H + colortype 2),
     per-chunk CRC32, and zlib.decompress(IDAT) reproduces the exact
     filter-0 scanlines the framebuffer encodes.
  3. build_office_model is a pure, crash-proof data-mapping layer: queue
     sort/wait_min math, `need` read ONLY for queue members (the stale-need
     rule), escalation tier boundaries, done-vs-resting split, gauge
     newest-with-data selection, >8 desks overflow, hostile/NaN inputs.
  4. hero_board.py --pixel falls back to the TUI office (byte-identical,
     zero DCS bytes) whenever sixel is unavailable, and still writes the
     PNG when forced on but piped.
  5. Palette stays within the sixel register cap and passes the contrast
     audit.
"""

import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
BOARD = os.path.join(ROOT, "hero_board.py")
PIXEL = os.path.join(ROOT, "hero_pixel.py")
PY = sys.executable or "python3"

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        FAILS.append(name)
        print("FAIL  %s  %s" % (name, detail))


def run(script, args=(), env_extra=None, timeout=30):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY, script] + list(args), capture_output=True,
                       text=False, timeout=timeout, env=env)
    return p


import hero_pixel as hp  # noqa: E402  (after sys.path insert)

# ------------------------------------------------------------- 1: sixel ----

print("== sixel encoder ==")

fb = hp.Framebuffer(12, 8, bg=0)
fb.fill_rect(0, 0, 6, 8, 1)   # left half color1, right half color0 (bg); H=8 -> band0=6 rows, band1=2 rows (partial)
data = hp.encode_sixel(fb)

check("sixel DCS intro", data.startswith(b"\x1bP0;1;0q"))
check("sixel raster attrs", b'"1;1;12;8' in data)
r, g, b = hp.PALETTE_RGB[0]
expect_pal0 = ("#0;2;%d;%d;%d" % ((r * 100 + 127) // 255, (g * 100 + 127) // 255,
                                  (b * 100 + 127) // 255)).encode()
check("sixel palette rescale entry #0", expect_pal0 in data, data[:200])
check("sixel ST terminator", data.endswith(b"\x1b\\"))
check("sixel no raw newline/control leak", b"\n" not in data and b"\r" not in data)

body = data.decode("ascii")
# band0: color1 fills x0-5 (6 rows -> full 6-bit mask = value 63 -> char 0x3F+63='~');
# color0 fills x6-11 same way. band1 only 2 rows -> mask value 3 -> char 0x3F+3='B'.
check("sixel RLE run>=4 uses !n<ch>", "!6~" in body and "!6?" in body, body[-200:])
check("sixel partial band (H%6!=0) uses smaller mask value", "!6B" in body, body[-200:])
check("sixel colors separated by $ within a band", "$" in body)
check("sixel bands separated by -", "-" in body.split("\x1b\\")[0])

# a run <4 must NOT be RLE-compressed (raw repeated chars instead)
fb2 = hp.Framebuffer(3, 6, bg=0)
fb2.set(0, 0, 1)
d2 = hp.encode_sixel(fb2).decode("ascii")
check("sixel short run (<4) not RLE-compressed", "!1" not in d2 and "!2" not in d2 and "!3" not in d2, d2)

check("sixel demo scene under 1s", True)  # timing asserted below
sessions = hp.demo_fleet(time.time())
model = hp.build_office_model(sessions, time.time())
big_fb = hp.draw_office(model, time.time())
t0 = time.time()
hp.encode_sixel(big_fb)
t1 = time.time()
check("full 1040x600 encode_sixel < 1s", (t1 - t0) < 1.0, "%.3fs" % (t1 - t0))
t0 = time.time()
hp.encode_png(big_fb)
t1 = time.time()
check("full 1040x600 encode_png < 1s", (t1 - t0) < 1.0, "%.3fs" % (t1 - t0))

# ---------------------------------------------------------------- 2: png --

print("== png encoder ==")

png = hp.encode_png(fb)
check("png signature", png.startswith(b"\x89PNG\r\n\x1a\n"))
ihdr_len = struct.unpack(">I", png[8:12])[0]
check("png IHDR chunk tag", png[12:16] == b"IHDR")
w, h, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", png[16:16 + ihdr_len])
check("png IHDR dims", (w, h) == (12, 8), (w, h))
check("png IHDR colortype 2 (truecolor)", ctype == 2, ctype)
check("png IHDR bitdepth 8", depth == 8, depth)

ihdr_crc = struct.unpack(">I", png[16 + ihdr_len:20 + ihdr_len])[0]
check("png IHDR crc32 matches", zlib.crc32(png[12:16 + ihdr_len]) & 0xffffffff == ihdr_crc)

idat_off = 20 + ihdr_len
idat_len = struct.unpack(">I", png[idat_off:idat_off + 4])[0]
idat_data = png[idat_off + 8:idat_off + 8 + idat_len]
idat_crc = struct.unpack(">I", png[idat_off + 8 + idat_len:idat_off + 12 + idat_len])[0]
check("png IDAT crc32 matches",
      zlib.crc32(png[idat_off + 4:idat_off + 8 + idat_len]) & 0xffffffff == idat_crc)

raw = zlib.decompress(idat_data)
check("png raw scanline count", len(raw) == 8 * (1 + 12 * 3), "got %d" % len(raw))
# reconstruct pixel (0,0) and (6,0): filter byte 0, then RGB triples
row0 = raw[0:1 + 12 * 3]
check("png filter byte is 0 (None) every row", all(raw[y * (1 + 36)] == 0 for y in range(8)))
px00 = tuple(row0[1:4])
px60 = tuple(row0[1 + 6 * 3:1 + 6 * 3 + 3])
check("png pixel(0,0) matches palette[1]", px00 == hp.PALETTE_RGB[1], px00)
check("png pixel(6,0) matches palette[0] (bg)", px60 == hp.PALETTE_RGB[0], px60)
check("png IEND present", png.endswith(b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)))

tmpdir = tempfile.mkdtemp(prefix="sh-px-")
png_out = os.path.join(tmpdir, "demo.png")
p = run(PIXEL, ["--demo", "--once", "--png", png_out])
check("hero_pixel --demo --once exit0", p.returncode == 0, p.stderr[:300])
check("hero_pixel writes PNG file", os.path.exists(png_out))
with open(png_out, "rb") as f:
    full_png = f.read()
check("full-canvas PNG signature", full_png.startswith(b"\x89PNG\r\n\x1a\n"))
fw, fh = struct.unpack(">II", full_png[16:24])
check("full-canvas PNG is 1040x600", (fw, fh) == (1040, 600), (fw, fh))
check("full-canvas PNG has IEND", full_png.endswith(b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)))

# ------------------------------------------------------- 3: model mapping --

print("== build_office_model ==")


def mk(sid, state, **extra):
    now = extra.pop("_now", time.time())
    base = {"sid": sid, "dir": sid, "hero": "fox", "state": state, "_state": state,
            "state_ts": now, "ts": now, "started_at": now, "ctx": 20, "cost": 1.0,
            "dur_ms": 60000, "five": None, "seven": None}
    base.update(extra)
    return base


now = time.time()

# queue sort + wait_min math
sessions = [
    mk("a", "needs_you", state_ts=now - 5 * 60),
    mk("b", "needs_you", state_ts=now - 40 * 60),
    mk("c", "needs_you", state_ts=now - 20 * 60),
]
m = hp.build_office_model(sessions, now)
order = [r["sid"] for r in m["queue"]]
check("queue sorted longest-wait-first", order == ["b", "c", "a"], order)
check("wait_min math (5m)", m["queue"][2]["wait_min"] == 5, m["queue"])
check("wait_min math (40m)", m["queue"][0]["wait_min"] == 40, m["queue"])

# ask derivation: permission + matching "Tool: target" activity
s = mk("p1", "needs_you", need="permission", activity="Edit: main.py")
row = hp.build_office_model([s], now)["queue"][0]
check("ask: permission + matching activity", row["ask"] == "permission: Edit: main.py", row["ask"])

# ask derivation: permission + non-matching activity -> generic fallback
s = mk("p2", "needs_you", need="permission", activity="no colon here")
row = hp.build_office_model([s], now)["queue"][0]
check("ask: permission + non-matching activity -> fallback", row["ask"] == "permission needed", row["ask"])

# ask derivation: waiting for you
s = mk("p3", "needs_you", need="waiting for you")
row = hp.build_office_model([s], now)["queue"][0]
check("ask: waiting for you", row["ask"] == "waiting: your review", row["ask"])

# stale `need`: a session back in `working` with a leftover need must NOT
# surface in the queue at all (need is only ever read for queue members)
s = mk("stale", "working", need="waiting for you", activity="Bash: pytest -q")
m = hp.build_office_model([s], now)
check("stale need outside needs_you: not in queue", m["queue"] == [], m["queue"])

# ask crop to 26 chars with ellipsis
s = mk("long", "needs_you", need="permission",
       activity="SomeReallyLongToolName: a very long target path/here.py")
row = hp.build_office_model([s], now)["queue"][0]
check("ask cropped to <=26 chars", len(row["ask"]) <= 26, row["ask"])
check("ask crop ends with ellipsis", row["ask"].endswith("…"), row["ask"])

# escalation tier boundaries
check("tier 14m == 0", hp.escalation_tier(14) == 0)
check("tier 15m == 1", hp.escalation_tier(15) == 1)
check("tier 29m == 1", hp.escalation_tier(29) == 1)
check("tier 30m == 2", hp.escalation_tier(30) == 2)

# gauges: pick newest-by-ts session with five is not None
sessions = [
    mk("newer", "working", ts=now, five=None),
    mk("older", "working", ts=now - 100, five=42.0, seven=10.0,
       five_reset=now + 100, seven_reset=now + 200),
]
m = hp.build_office_model(sessions, now)
check("gauges skip newer session with five=None", m["gauges"] is not None and m["gauges"]["five"] == 42.0,
      m["gauges"])

# gauges: no session has five -> no-data, no crash
sessions = [mk("x", "working", five=None), mk("y", "working", five=None)]
m = hp.build_office_model(sessions, now)
check("gauges None when no session has rate data", m["gauges"] is None)
fb3 = hp.draw_office(m, now)   # must not crash rendering the no-data HUD state
check("draw_office renders with no gauge data (no crash)", isinstance(fb3, hp.Framebuffer))

# done-vs-resting split at the 600s boundary
sessions = [
    mk("done", "idle", state_ts=now - 599, dur_ms=120000),
    mk("resting", "idle", state_ts=now - 600),
    mk("resting2", "idle", state_ts=now - 900),
]
m = hp.build_office_model(sessions, now)
check("done bucket (age<600s)", [r["sid"] for r in m["done"]] == ["done"], m["done"])
check("resting bucket (age>=600s)", sorted(r["sid"] for r in m["resting"]) == ["resting", "resting2"], m["resting"])
check("done run_min derived from dur_ms", m["done"][0]["run_min"] == 2, m["done"])

# >8 sessions -> 8 desks + overflow count
sessions = [mk("s%d" % i, "working") for i in range(11)]
m = hp.build_office_model(sessions, now)
check(">8 sessions: exactly 8 desks", len(m["desks"]) == 8, len(m["desks"]))
check(">8 sessions: overflow count", m["overflow"] == 3, m["overflow"])

# payroll excludes ghosts
sessions = [
    mk("live1", "working", cost=10.0, dur_ms=3600000),
    mk("ghost1", "ghost", cost=999.0, dur_ms=999 * 3600000),
]
m = hp.build_office_model(sessions, now)
check("payroll total excludes ghost cost", m["payroll_total"] == 10.0, m["payroll_total"])
check("payroll rate excludes ghost duration", abs(m["payroll_rate"] - 10.0) < 1e-9, m["payroll_rate"])

# hostile/NaN inputs must never crash the model or the renderer
hostile = [
    {"sid": "nan1", "dir": "nan1", "hero": "fox", "state": "needs_you", "_state": "needs_you",
     "state_ts": float("nan"), "ts": float("nan"), "started_at": 1, "ctx": float("nan"),
     "cost": float("inf"), "five": float("nan"), "dur_ms": float("nan")},
    {"sid": "bad2", "dir": "bad2", "hero": ["cat"], "state": {"x": 1}, "_state": "idle",
     "state_ts": now, "ts": now, "started_at": 2, "ctx": 50, "cost": None},
]
try:
    m = hp.build_office_model(hostile, now)
    fb4 = hp.draw_office(m, now)
    check("hostile/NaN sessions: model+render no crash", isinstance(fb4, hp.Framebuffer))
except Exception as e:
    check("hostile/NaN sessions: model+render no crash", False, repr(e))

# --------------------------------------------------- 4: capability fallback

print("== capability fallback ==")

p = run(BOARD, ["--pixel", "--demo", "--once", "--office"], env_extra={"STATUS_HERO_SIXEL": "0"})
out = p.stdout
check("fallback (sixel=0) exit0", p.returncode == 0, p.stderr[:300])
check("fallback (sixel=0) shows office TUI frame", b"\xe2\x94\x8c" in out, out[:200])   # UTF-8 for ┌
check("fallback (sixel=0) has zero DCS bytes", b"\x1bP" not in out)
check("fallback (sixel=0) stderr notice", b"no sixel support detected" in p.stderr)

# forced on but piped (not a tty): still no DCS bytes, PNG still written
png_out2 = os.path.join(tmpdir, "live_forced.png")
p2 = run(BOARD, ["--pixel", "--demo", "--once", "--png", png_out2],
         env_extra={"STATUS_HERO_SIXEL": "1"})
check("forced-on but piped: exit0", p2.returncode == 0, p2.stderr[:300])
check("forced-on but piped: zero DCS bytes (isatty gate)", b"\x1bP" not in p2.stdout)
check("forced-on but piped: PNG still written", os.path.exists(png_out2))

# --office without --pixel is untouched (byte-identical TUI path)
p3 = run(BOARD, ["--demo", "--once", "--office"])
p4 = run(BOARD, ["--pixel", "--demo", "--once", "--office"], env_extra={"STATUS_HERO_SIXEL": "0"})
check("plain --office output unaffected by --pixel wiring",
      p3.stdout == p4.stdout, (len(p3.stdout), len(p4.stdout)))

# ----------------------------------------------------------- 5: palette ---

print("== palette / contrast ==")

check("palette <= 256 sixel registers", len(hp.PALETTE_RGB) <= 256, len(hp.PALETTE_RGB))
try:
    ok = hp.audit_contrast()
    check("audit_contrast passes", ok is True)
except AssertionError as e:
    check("audit_contrast passes", False, str(e))

# ------------------------------------------------------- 6: emoji guard-gating

print("== emoji backend guard-gating ==")

try:
    import PIL as _pil_probe   # noqa: F401  (probe only, in THIS interpreter)
    _pil_here = True
except ImportError:
    _pil_here = False

if _pil_here:
    # This gate is meant to run under a system python3 with no Pillow (see
    # tests/README or the commissioning task); if some other interpreter
    # happens to have Pillow installed, the False-without-Pillow assertion
    # doesn't apply to it, but the stdlib-path checks below still do.
    print("  skip  emoji_available() False-without-Pillow (this interpreter has Pillow)")
else:
    check("emoji_available() False without Pillow", hp.emoji_available() is False,
          hp.emoji_available())

# The whole point of progressive enhancement: with Pillow (or an emoji font)
# absent, the tool must still work EXACTLY as before — stdlib model+render+
# encode, end to end, both as a library call and through the real CLI.
sessions = hp.demo_fleet(time.time())
model = hp.build_office_model(sessions, time.time())
fb_stdlib = hp.draw_office(model, time.time())
check("stdlib draw_office still renders without Pillow", isinstance(fb_stdlib, hp.Framebuffer))
png_stdlib = hp.encode_png(fb_stdlib)
check("stdlib encode_png still produces a valid PNG", png_stdlib.startswith(b"\x89PNG\r\n\x1a\n"))

png_out3 = os.path.join(tmpdir, "guard.png")
p = run(PIXEL, ["--demo", "--once", "--png", png_out3])
check("hero_pixel --demo --once (guard-gating) exit0", p.returncode == 0, p.stderr[:300])
check("hero_pixel --demo --once (guard-gating) writes PNG", os.path.exists(png_out3))

# ---- window-fit scaling (fixes "office too small in a big Retina window") ----
class _TinyFB:
    w, h = 2, 2
    buf = bytearray([1, 2, 3, 4])
u = hp._upscale(_TinyFB(), 2)
check("upscale x2 doubles dimensions", u.w == 4 and u.h == 4)
check("upscale x2 is exact nearest-neighbor",
      list(u.buf) == [1, 1, 2, 2, 1, 1, 2, 2, 3, 3, 4, 4, 3, 3, 4, 4], list(u.buf))
check("upscale n<=1 is a no-op passthrough", hp._upscale(_TinyFB(), 1).w == 2)
check("_scale_factor honors --scale override", hp._scale_factor(["--scale", "3"]) == 3)
check("_scale_factor clamps to [1,4]", hp._scale_factor(["--scale", "99"]) == 4
      and hp._scale_factor(["--scale", "0"]) == 1)
check("_scale_factor defaults to 1 with no tty/size", hp._scale_factor([]) == 1)
fb_big = hp._upscale(hp.draw_office(hp.build_office_model(hp.demo_fleet(time.time()),
                                                          time.time()), time.time()), 2)
check("upscaled framebuffer still encodes to valid sixel",
      hp.encode_sixel(fb_big).startswith(b"\x1bP"))

# ------------------------------------------------------------------ result

print()
if FAILS:
    print("%d FAILURES:" % len(FAILS))
    for f in FAILS[:30]:
        print("  - " + f)
    sys.exit(1)
print("all green")
