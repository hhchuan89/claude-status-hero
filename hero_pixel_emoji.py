#!/usr/bin/env python3
"""claude-status-hero · hero_pixel_emoji.py — the emoji office (Pillow backend).

Same locked layout as hero_pixel.py's pixel office (walled zones, teleport
model, manager-room queue, HUD gauges + clock, pantry) but drawn with real
color emoji (Apple Color Emoji / Noto Color Emoji baked via Pillow) instead
of the hand-authored palette-index sprites. This is the "B / hybrid emoji"
look — the one the design review liked — offered as a progressive
enhancement: hero_pixel.py only imports this module, and only uses it, when
Pillow AND an emoji font are both present (see emoji_available() there).
Every other environment gets the 100%-stdlib sprite renderer unchanged.

Ported from the prototype at (scratchpad) office_px.py, which is the
validated design source of truth for this look. Deltas from the prototype:

  - Font/asset discovery is now optional and multi-platform instead of a
    single hard-coded macOS path: EMOJI_FONT tries Apple Color Emoji, then
    a handful of common Linux Noto Color Emoji install paths, and is None
    (backend unavailable, see font_available()) if none exist. The text
    font similarly tries a short list of system monospace fonts and falls
    back to Pillow's built-in bitmap font — no bundled font assets, no
    proprietary-font dev warnings, matching this repo's "zero bundled
    assets" rule (only hero_pixel.py's stdlib path is truly zero-dependency;
    this backend's one extra dependency is a system-installed emoji font).

  - INPUT SHAPE: render(model, now) takes the `model` dict produced by
    hero_pixel.build_office_model(sessions, now) — the SAME model
    hero_pixel.draw_office(model, now) draws — not a raw session list like
    the prototype's draw_office(sessions). This was the "least glue" choice:
    build_office_model() already does every bit of data-mapping this scene
    needs (queue sort + wait_min + long-wait escalation tier + ask
    derivation, desks/overflow, done-vs-resting split, gauge selection,
    payroll, hud_text, hostile/NaN hardening) and is unit-tested in
    tests/test_pixel.py. Re-deriving any of that here from a raw session
    list would duplicate — and risk drifting from — logic that already has
    a passing test suite. So this module is a pure rendering layer over
    that same model, and the two backends are interchangeable at the call
    site: `draw_office(model, now)` vs `render(model, now)`.

  - ADDED (features hero_pixel.py's stdlib renderer has that the prototype
    lacked): long-wait escalation styling across the WHOLE reception queue
    (not just the front of line) via _escalation_style(tier) — dim below
    ESCALATE_AMBER_MIN, amber at/above it, vermillion + "!!" at/above
    ESCALATE_ALARM_MIN (the tier value itself comes pre-computed on every
    queue row by build_office_model(), so this module only needs to style
    it) — and the MANAGER_LABEL ("YOU") desk marker in the manager room.
    Also ported hero_pixel.py's None-safe gauge handling (no rate-limit
    data yet -> "5H --"/"WK --" placeholders instead of a crash).

Event-driven, one static frame per call — no animation loop lives here
(see hero_pixel.py's run()). Needs Pillow; import this module inside a
try/except ImportError (hero_pixel.py's emoji_available() does this).
"""

import math
import os
import time

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------- hero_board loader
# Loaded from disk by path (own module name, distinct from hero_pixel.py's
# own "status_hero_board_px" load) so this file never depends on hero_pixel
# or creates any import-order surprise when hero_pixel.py lazily imports
# this module mid-run — it only ever needs hero_board.py's HERO_EMOJI map
# (the SAME glyphs the statusline/TUI office already show), sanitize(), and
# num().

_hb_mod = None


def _board():
    global _hb_mod
    if _hb_mod is None:
        import importlib.util
        p = os.path.join(HERE, "hero_board.py")
        spec = importlib.util.spec_from_file_location("status_hero_board_px_emoji", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _hb_mod = m
    return _hb_mod


_hb = _board()


def safe_hero(hero):
    """A session's `hero` field, hardened for hostile files: dict membership
    on an unhashable value (e.g. a hostile file's hero: ["cat"]) raises
    TypeError before we ever draw anything, so every hero lookup goes
    through here first (mirrors hero_pixel.safe_hero())."""
    return hero if isinstance(hero, str) and hero in _hb.HERO_EMOJI else "fox"


def _crop(s, n):
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ fonts --
# No bundled font files: the emoji glyphs come from a system-installed color
# emoji font (discovered below; EMOJI_FONT is None, and this backend is
# unavailable, if none is found), and body text uses a system monospace font
# if one exists, else Pillow's built-in bitmap font. Either way, nothing here
# is shipped/redistributed by this repo.
_EMOJI_FONT_CANDIDATES = [
    "/System/Library/Fonts/Apple Color Emoji.ttc",             # macOS
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",       # Debian/Ubuntu
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",   # Fedora
    "/usr/local/share/fonts/NotoColorEmoji.ttf",
]

_TEXT_FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",                                   # macOS
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",               # Debian/Ubuntu
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",   # Fedora/RHEL
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",                           # Arch
]


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


EMOJI_FONT = _first_existing(_EMOJI_FONT_CANDIDATES)
TEXT_FONT = _first_existing(_TEXT_FONT_CANDIDATES)


def font_available():
    """True iff an emoji font was found at import time. hero_pixel.
    emoji_available() ANDs this with 'Pillow importable' and the
    STATUS_HERO_EMOJI env override to decide which backend renders."""
    return EMOJI_FONT is not None


def _default_font(size):
    try:
        return ImageFont.load_default(size=size)   # Pillow >=10.1
    except TypeError:
        return ImageFont.load_default()             # older Pillow: fixed size


_fc = {}


def _font(size):
    if size not in _fc:
        if TEXT_FONT is not None:
            try:
                _fc[size] = ImageFont.truetype(TEXT_FONT, size)
            except Exception:
                _fc[size] = _default_font(size)
        else:
            _fc[size] = _default_font(size)
    return _fc[size]


# Three names kept (matching the prototype's call sites) though they
# currently resolve to the same face — no bold/mono variant is discovered
# separately since nothing is bundled; weight is carried by plate color,
# not by glyph, everywhere in this design.
def F_reg(size=16):
    return _font(size)


def F_bold(size=16):
    return _font(size)


def F_mono(size=16):
    return _font(size)


_ef = {}


def ef(strike):
    if EMOJI_FONT is None:
        raise RuntimeError("hero_pixel_emoji: no emoji font available")
    if strike not in _ef:
        _ef[strike] = ImageFont.truetype(EMOJI_FONT, strike)
    return _ef[strike]


def emoji(img, x, y, ch, sz, alpha=255):
    """Bake a real color emoji glyph as pixels and blit at (x,y), sz square.
    alpha<255 dims it (used for the ghost/stale treatment)."""
    strike = 96 if sz > 64 else 48
    f = ef(strike)
    t = Image.new("RGBA", (strike + 8, strike + 8), (0, 0, 0, 0))
    ImageDraw.Draw(t).text((0, 0), ch, font=f, embedded_color=True)
    if sz != strike:
        t = t.resize((sz, sz), Image.LANCZOS)
    if alpha < 255:
        r, g, b, a = t.split()
        a = a.point(lambda v: v * alpha // 255)
        t = Image.merge("RGBA", (r, g, b, a))
    img.alpha_composite(t, (int(x), int(y)))


# ---------------------------------------------------------------- palette --
# ~18 flat tokens, no gradients — identical values to hero_pixel.PAL (that
# module's source-of-truth hex comments apply here too). Kept as its own
# copy (not imported from hero_pixel) so this module has zero dependency on
# hero_pixel.py — see the module docstring.
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

STATE_COLOR = {
    "working": PAL["working"], "needs_you": PAL["alarm"], "error": PAL["alarm"],
    "idle": PAL["idle"], "compacting": PAL["compacting"], "ghost": PAL["ghost"],
}

# ------------------------------------------------------------------ grid ---
W, H = 1040, 600
OW = 16   # outer wall thickness
IW = 8    # interior wall thickness
ZONES = {
    "hud": (0, 0, 1040, 64),
    "work": (16, 64, 728, 460),          # OFFICE floor (left, the big room)
    "reception": (736, 64, 1024, 460),   # MANAGER ROOM (right)
    "pantry": (16, 468, 1024, 584),       # pantry (bottom)
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


def audit_contrast(verbose=True):
    """Every text/mark pair this file actually draws, checked against the
    WCAG floor the design claims. Raises if one regresses (same checks as
    hero_pixel.audit_contrast(), over this module's own PAL copy)."""
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


# ------------------------------------------------ text/emoji primitives ----
# THE RULE: text never touches the scene bare. icon_chip() always paints a
# solid plate first; every text/emoji call in this file goes through it
# (directly, or via text_plate()).
def dashed_rect(d, rect, color, dash=4, gap=3):
    x0, y0, x1, y1 = rect
    for yy in (y0, y1 - 1):
        x = x0
        while x < x1:
            d.line((x, yy, min(x + dash, x1), yy), fill=color)
            x += dash + gap
    for xx in (x0, x1 - 1):
        y = y0
        while y < y1:
            d.line((xx, y, xx, min(y + dash, y1)), fill=color)
            y += dash + gap


def icon_chip(img, d, x, y, parts, *, font, ink, plate, anchor="lt",
              pad=(6, 4), radius=3, gap=4, dashed=False):
    """parts: list of ('text', s) | ('emoji', ch, size) | ('tick', color, w).
    Measures the row, paints one solid rounded plate, then draws parts
    left-to-right. Returns the plate rect (x0,y0,x1,y1)."""
    padx, pady = pad
    widths, height = [], 0
    for part in parts:
        kind = part[0]
        if kind == "emoji":
            _, ch, size = part
            widths.append(size)
            height = max(height, size)
        elif kind == "tick":
            _, color, w = part
            widths.append(w)
            height = max(height, 16)
        else:
            _, s = part
            bbox = d.textbbox((0, 0), s, font=font)
            widths.append(bbox[2] - bbox[0])
            height = max(height, bbox[3] - bbox[1])
    n = len(parts)
    content_w = sum(widths) + gap * max(n - 1, 0)
    if anchor == "rt":
        px0, py0 = x - (content_w + 2 * padx), y
    elif anchor == "mt":
        px0, py0 = x - (content_w + 2 * padx) / 2, y
    elif anchor == "mm":
        px0, py0 = x - (content_w + 2 * padx) / 2, y - (height + 2 * pady) / 2
    else:  # "lt"
        px0, py0 = x, y
    px1, py1 = px0 + content_w + 2 * padx, py0 + height + 2 * pady
    d.rounded_rectangle((px0, py0, px1, py1), radius, fill=plate)
    if dashed:
        dashed_rect(d, (px0, py0, px1, py1), PAL["ghost"])
    cx = px0 + padx
    cy_mid = (py0 + py1) / 2
    for part, w in zip(parts, widths):
        kind = part[0]
        if kind == "emoji":
            _, ch, size = part
            emoji(img, cx, cy_mid - size / 2, ch, size)
        elif kind == "tick":
            _, color, tw = part
            d.rounded_rectangle((cx, py0 + 3, cx + tw, py1 - 3), 2, fill=color)
        else:
            _, s = part
            bbox = d.textbbox((0, 0), s, font=font)
            ty = cy_mid - (bbox[3] - bbox[1]) / 2 - bbox[1]
            d.text((cx - bbox[0], ty), s, font=font, fill=ink)
        cx += w + gap
    return (px0, py0, px1, py1)


def text_plate(img, d, x, y, s, *, font, ink=None, plate=None,
               anchor="lt", pad=(6, 4), radius=3, dashed=False):
    ink = PAL["ink"] if ink is None else ink
    plate = PAL["hud_plate"] if plate is None else plate
    return icon_chip(img, d, x, y, [("text", s)], font=font, ink=ink,
                      plate=plate, anchor=anchor, pad=pad, radius=radius,
                      dashed=dashed)


def room_sign(img, d, x, y, name, *, anchor="mt"):
    return icon_chip(img, d, x, y, [("tick", PAL["hud_border"], 5), ("text", name)],
                      font=F_bold(18), ink=PAL["ink"], plate=PAL["hud_plate"],
                      anchor=anchor, pad=(9, 5), radius=3)


# --------------------------------------------------------- gauge/time ------
def gauge_calc(pct, reset_epoch, window_sec, now):
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


def draw_gauge(img, d, x, y, label, pct, reset_epoch, window_sec, now, with_day):
    # None-safe (ported from hero_pixel.draw_gauge): a fresh install with no
    # rate-limit data yet must not crash the HUD.
    pct = 0.0 if pct is None else pct
    reset_epoch = (now + window_sec) if reset_epoch is None else reset_epoch
    eta, remaining, level = gauge_calc(pct, reset_epoch, window_sec, now)
    col = PAL["ink"] if level == 0 else (PAL["warn"] if level == 1 else PAL["alarm"])
    l1 = "%s %3.0f%%" % (label, pct)
    r1 = text_plate(img, d, x, y, l1, font=F_mono(16), ink=col, pad=(0, 2))
    bar_x, bar_y, bar_w, bar_h = x, r1[3] + 3, 120, 10
    d.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), fill=PAL["wall_shadow"])
    fill_w = int(bar_w * min(pct, 100) / 100)
    d.rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), fill=PAL["ink"])
    if level > 0 and fill_w >= 3:
        d.rectangle((bar_x + fill_w - 3, bar_y, bar_x + fill_w, bar_y + bar_h), fill=col)
    proj_y = bar_y + bar_h + 3
    if level == 0:
        # neutral ink already signals safe; drop the verbose "· SAFE" so the
        # HUD row stays narrow enough for the clock + payroll to both fit
        proj = "RESETS %s" % fmt_clock(reset_epoch, with_day)
        proj_ink = PAL["ink_dim"]
    else:
        proj = "~%s LEFT @ PACE" % fmt_dur(eta)
        proj_ink = col
    r2 = text_plate(img, d, x, proj_y, proj, font=F_mono(16), ink=proj_ink, pad=(0, 1))
    return (x, y, max(r1[2], bar_x + bar_w, r2[2]), r2[3])


def draw_clock(img, d, x, y, now):
    """Live system clock: a small analog face (hands from local time) + a
    digital day/HH:MM chip."""
    lt = time.localtime(now)
    r = 11
    cxc, cyc = x + r, y + r
    d.ellipse((cxc - r, cyc - r, cxc + r, cyc + r),
              outline=PAL["ink_dim"], width=2, fill=PAL["hud_plate"])
    mm, hh = lt.tm_min, (lt.tm_hour % 12) + lt.tm_min / 60.0
    ma, ha = math.radians(mm * 6 - 90), math.radians(hh * 30 - 90)
    d.line((cxc, cyc, cxc + math.cos(ma) * (r - 3), cyc + math.sin(ma) * (r - 3)),
           fill=PAL["ink"], width=1)
    d.line((cxc, cyc, cxc + math.cos(ha) * (r - 6), cyc + math.sin(ha) * (r - 6)),
           fill=PAL["ink"], width=2)
    txt = time.strftime("%a %H:%M", lt).upper()
    return text_plate(img, d, x + 2 * r + 8, y, txt, font=F_mono(16),
                      ink=PAL["ink_dim"], pad=(0, 2))


# ------------------------------------------------------------ wall/room ----
def draw_walls(img, d, queue_nonempty):
    wf, ws = PAL["wall_face"], PAL["wall_shadow"]
    d.rectangle((0, 64, OW, H), fill=wf)
    d.rectangle((OW - 2, 64, OW, H), fill=ws)
    door_y0, door_y1 = 192, 252
    d.rectangle((0, door_y0, OW, door_y1), fill=ws)
    emoji(img, 0, door_y0 + 4, "🚪", 44)
    d.rectangle((W - OW, 64, W, H), fill=wf)
    d.rectangle((W - OW, 64, W - OW + 2, H), fill=ws)
    d.rectangle((0, H - OW, W, H), fill=wf)
    d.rectangle((0, H - OW, W, H - OW + 2), fill=ws)
    vx0, vx1, vy0, vy1 = VWALL["x0"], VWALL["x1"], VWALL["y0"], VWALL["y1"]
    gy0, gy1 = VWALL["gap"]
    d.rectangle((vx0, vy0, vx1, gy0), fill=wf)
    d.rectangle((vx0, gy1, vx1, vy1), fill=wf)
    hx0, hx1, hy0, hy1 = HWALL["x0"], HWALL["x1"], HWALL["y0"], HWALL["y1"]
    gx0, gx1 = HWALL["gap"]
    d.rectangle((hx0, hy0, gx0, hy1), fill=wf)
    d.rectangle((gx1, hy0, hx1, hy1), fill=wf)
    d.rectangle((hx0, hy1 - 2, gx0, hy1), fill=ws)
    d.rectangle((gx1, hy1 - 2, hx1, hy1), fill=ws)
    if queue_nonempty:
        mx0, _, mx1, _ = ZONES["reception"]
        d.rectangle((mx0, 64, mx1, 68), fill=PAL["alarm"])


def floor_room(d, rect):
    x0, y0, x1, y1 = rect
    d.rectangle(rect, fill=PAL["floor_base"])
    cell = 40
    for ty in range(y0, y1, cell):
        for tx in range(x0, x1, cell):
            if ((tx // cell) + (ty // cell)) % 2:
                d.rectangle((tx, ty, min(tx + cell, x1), min(ty + cell, y1)),
                            fill=PAL["floor_alt"])


def draw_decor_work(img):
    emoji(img, ZONES["work"][2] - 40, 90, "🖨️", 40)
    emoji(img, ZONES["work"][2] - 42, 340, "🪴", 44)


# ------------------------------------------------------------- desks -------
def draw_desk(img, d, slot_x, slot_y, s):
    cx = slot_x + SLOT_W // 2
    if s is None:
        dim_wood = tuple((a + b) // 2 for a, b in zip(PAL["wood"], PAL["floor_base"]))
        d.rounded_rectangle((slot_x + 21, slot_y + 88, slot_x + 161, slot_y + 116),
                             4, fill=dim_wood)
        return
    state = s.get("_state") if isinstance(s.get("_state"), str) else "working"
    hero = safe_hero(s.get("hero"))
    tick_color = STATE_COLOR.get(state, PAL["working"])
    d.rounded_rectangle((slot_x + 21, slot_y + 88, slot_x + 161, slot_y + 116),
                         4, outline=PAL["wood_dark"], width=2, fill=PAL["wood"])
    emoji(img, cx - 17, slot_y + 58, "💻", 34)
    if state == "error":
        for dx, dy, r in [(0, 0, 7), (-4, -10, 6), (5, -18, 5)]:
            ex, ey = cx + dx, slot_y + 76 + dy
            d.ellipse((ex - r, ey - r, ex + r, ey + r), fill=PAL["wall_shadow"])
    if state in ("working", "compacting"):
        emoji(img, cx - 28, slot_y + 8, _hb.HERO_EMOJI[hero], 56)
    elif state == "ghost":
        emoji(img, cx - 28, slot_y + 8, _hb.HERO_EMOJI[hero], 56, alpha=128)
    else:   # needs_you/idle: occupant teleported to manager room/pantry
        emoji(img, cx - 22, slot_y + 16, "🪑", 44)
    ctx = _hb.num(s.get("ctx"))
    if ctx is not None and ctx >= 85:
        emoji(img, slot_x + 148, slot_y + 78, "🗂️", 22)
    name = _crop(_hb.sanitize(s.get("dir") or "?", 20), 12)
    icon_chip(img, d, cx, slot_y + 120,
              [("tick", tick_color, 6), ("text", name)],
              font=F_reg(16), ink=PAL["ink"], plate=PAL["hud_plate"], anchor="mt",
              dashed=(state == "ghost"))


# ------------------------------------------------------------ reception ----
def _escalation_style(tier):
    """(plate_rgb, ink_rgb, prefix) for a non-front queue row's wait chip —
    mirrors hero_pixel._escalation_style(): the long-wait escalation the
    original office_px prototype only applied to the front of the queue."""
    if tier >= 2:
        return PAL["alarm"], PAL["ink_inverse"], "!! "
    if tier >= 1:
        return PAL["warn"], PAL["ink_inverse"], ""
    return PAL["hud_plate"], PAL["ink_dim"], ""


def draw_reception(img, d, model):
    x0, y0, x1, y1 = ZONES["reception"]
    cx = (x0 + x1) // 2
    d.rounded_rectangle((x0 + 40, y0 + 16, x1 - 40, y0 + 40), 4,
                         outline=PAL["wood_dark"], width=2, fill=PAL["wood"])
    emoji(img, cx - 14, y0 + 2, "🔔", 28)
    # the manager's desk marker — added vs. the prototype, which never
    # labelled the counter at all.
    text_plate(img, d, cx, (y0 + 16 + y0 + 40) // 2, model["manager_label"],
               font=F_reg(16), ink=PAL["ink"], plate=PAL["hud_plate"], anchor="mm")
    room_sign(img, d, cx, y0 + 46, "MANAGER ROOM", anchor="mt")
    queue = model["queue"]
    if not queue:
        return
    front, rest = queue[0], queue[1:3]
    overflow = len(queue) - 1 - len(rest)
    fy = 134
    hero = safe_hero(front.get("hero"))
    emoji(img, cx - 32, fy, _hb.HERO_EMOJI[hero], 64)
    bang = "!! " if front["tier"] >= 2 else ""
    icon_chip(img, d, cx, fy + 68,
              [("text", "%s%dM WAIT" % (bang, front["wait_min"]))],
              font=F_bold(20), ink=PAL["ink_inverse"], plate=PAL["alarm"], anchor="mt")
    ask = front.get("ask")
    if ask:
        icon_chip(img, d, cx, fy + 100,
                  [("tick", PAL["alarm"], 5), ("text", ask)],
                  font=F_mono(16), ink=PAL["ink"], plate=PAL["hud_plate"], anchor="mt")
    ry = 268
    for s in rest:
        hero = safe_hero(s.get("hero"))
        emoji(img, cx - 24, ry, _hb.HERO_EMOJI[hero], 48)
        plate, ink, prefix = _escalation_style(s["tier"])
        icon_chip(img, d, cx, ry + 52,
                  [("text", "%s%dM" % (prefix, s["wait_min"]))],
                  font=F_mono(16), ink=ink, plate=plate, anchor="mt")
        ry += 82
    if overflow > 0:
        text_plate(img, d, cx, ry, "+%d MORE" % overflow, font=F_mono(16),
                   ink=PAL["ink_dim"], anchor="mt")


# --------------------------------------------------------------- pantry ----
def draw_pantry(img, d, model):
    x0, y0, x1, y1 = ZONES["pantry"]
    d.rounded_rectangle((x0 + 8, y0 + 8, x0 + 500, y1 - 8), 16, fill=PAL["rug_pantry"])
    emoji(img, x0 + 16, y0 + 36, "🛋️", 64)
    emoji(img, x0 + 260, y0 + 46, "☕", 34)
    emoji(img, x1 - 150, y0 + 40, "🚰", 48)
    emoji(img, x0 + 380, y0 + 60, "🪴", 40)
    emoji(img, x1 - 60, y0 + 70, "🪴", 40)
    rx = x0 + 100
    for s in model["resting"]:
        hero = safe_hero(s.get("hero"))
        emoji(img, rx, y0 + 44, _hb.HERO_EMOJI[hero], 48)
        emoji(img, rx + 30, y0 + 30, "💤", 26)
        rx += 84
    gx0, gx1 = HWALL["gap"]
    dxc = (gx0 + gx1) // 2
    done = model["done"]
    dx = dxc - (len(done) * 60) // 2
    for s in done:
        hero = safe_hero(s.get("hero"))
        emoji(img, dx, y0 + 6, _hb.HERO_EMOJI[hero], 44)
        icon_chip(img, d, dx + 22, y0 + 52,
                  [("emoji", "✅", 16), ("text", "%dM RUN" % s["run_min"])],
                  font=F_mono(16), ink=PAL["ink_dim"], plate=PAL["hud_plate"], anchor="mt")
        dx += 60


# ------------------------------------------------------------------ HUD ----
def draw_hud(img, d, model, now):
    d.rectangle((0, 0, W, 64), fill=PAL["hud_plate"])
    d.rectangle((0, 62, W, 64), fill=PAL["hud_border"])
    # payroll first (far right) so the clock can be tucked in just LEFT of it
    # and can never overlap it, whatever the gauge text width is this frame
    pr1 = text_plate(img, d, W - 16, 10, "TOTAL $%.2f" % model["payroll_total"],
                     font=F_mono(16), ink=PAL["ink_dim"], anchor="rt", pad=(4, 2))
    pr2 = text_plate(img, d, W - 16, 34, "$%.1f/HR" % model["payroll_rate"],
                     font=F_mono(16), ink=PAL["ink_dim"], anchor="rt", pad=(4, 2))
    payroll_left = min(pr1[0], pr2[0])

    x = 16
    if model["queue_nonempty"]:
        rect = icon_chip(img, d, x, 20, [("emoji", "❗", 20), ("text", model["hud_text"])],
                          font=F_bold(16), ink=PAL["ink_inverse"], plate=PAL["alarm"])
    else:
        rect = icon_chip(img, d, x, 20, [("emoji", "✅", 20), ("text", model["hud_text"])],
                          font=F_bold(16), ink=PAL["ink_dim"], plate=PAL["hud_plate"])
    xcur = rect[2] + 40

    gauges = model["gauges"]
    if gauges:
        r5 = draw_gauge(img, d, xcur, 6, "5H", gauges["five"], gauges["five_reset"],
                         5 * 3600, now, with_day=False)
        xcur = r5[2] + 36
        rwk = draw_gauge(img, d, xcur, 6, "WK", gauges["seven"], gauges["seven_reset"],
                          7 * 86400, now, with_day=True)
        xcur = rwk[2] + 30
    else:
        r5 = text_plate(img, d, xcur, 6, "5H --", font=F_mono(16), ink=PAL["ink_dim"], pad=(4, 2))
        xcur = r5[2] + 16
        rwk = text_plate(img, d, xcur, 6, "WK --", font=F_mono(16), ink=PAL["ink_dim"], pad=(4, 2))
        xcur = rwk[2] + 30

    # clock only if it fits between the gauges and the payroll block
    ctxt = time.strftime("%a %H:%M", time.localtime(now)).upper()
    clock_w = 2 * 11 + 8 + int(d.textlength(ctxt, font=F_mono(16))) + 8
    if xcur + clock_w <= payroll_left - 12:
        draw_clock(img, d, xcur, 20, now)


# --------------------------------------------------------------- office ----
def render(model, now):
    """Draws one static frame at the locked 1040x600 canvas from a model
    produced by hero_pixel.build_office_model(sessions, now) — the SAME
    model hero_pixel.draw_office(model, now) draws (see module docstring for
    why this backend takes the model dict rather than a raw session list).
    Pure function: no I/O besides font/emoji glyph caches. Returns a PIL RGB
    Image, 1040x600."""
    if EMOJI_FONT is None:
        raise RuntimeError("hero_pixel_emoji: no emoji font found on this system")

    img = Image.new("RGBA", (W, H), PAL["bg_void"])
    d = ImageDraw.Draw(img)
    d.fontmode = "1"   # AA off — glyph pixels pure on/off, no sixel-quantized fringe

    floor_room(d, ZONES["reception"])
    floor_room(d, ZONES["work"])
    floor_room(d, ZONES["pantry"])

    draw_walls(img, d, model["queue_nonempty"])
    draw_decor_work(img)
    draw_reception(img, d, model)

    for i in range(8):
        sx, sy = slot_xy(i)
        s = model["desks"][i] if i < len(model["desks"]) else None
        draw_desk(img, d, sx, sy, s)

    if model["overflow"] > 0:
        text_plate(img, d, ZONES["work"][2] - 10, ZONES["work"][3] - 26,
                   "+%d MORE" % model["overflow"], font=F_mono(16),
                   ink=PAL["ink_dim"], anchor="rt")

    draw_pantry(img, d, model)

    wx0, _, _, _ = ZONES["work"]
    room_sign(img, d, wx0 + 14, 72, "OFFICE", anchor="lt")
    px0, py0, _, _ = ZONES["pantry"]
    room_sign(img, d, px0 + 14, py0 + 8, "PANTRY", anchor="lt")

    draw_hud(img, d, model, now)
    return img.convert("RGB")


if __name__ == "__main__":
    # Manual sanity check only (not part of the test gate): renders the demo
    # fleet with this backend and saves a PNG. Imports hero_pixel here, not
    # at module scope — this file is self-contained for every OTHER caller
    # (in particular, hero_pixel.py importing *this* module while it is
    # itself running as __main__), and only needs hero_pixel when *this*
    # file is the one being run directly.
    import sys
    audit_contrast()
    if not font_available():
        print("no emoji font found on this system — nothing to render", file=sys.stderr)
        sys.exit(1)
    sys.path.insert(0, HERE)
    import hero_pixel as hp
    now = time.time()
    sessions = hp.demo_fleet(now)
    model = hp.build_office_model(sessions, now)
    render(model, now).save("office_emoji_demo.png")
    print("saved office_emoji_demo.png")
