# claude-status-hero

**A statusline that never jitters, and a fleet dashboard that's actually fun.**

Run many Claude Code windows at once? This gives every window a tiny animal
hero, and gives *you* one glanceable board that shows which window is working,
which one **needs you**, and how hard each is leaning on its context window —
plus a statusline that packs 5h/7d budgets, context, git, and cost into three
rock-solid lines.

Two single-file, zero-dependency Python scripts:

| | what | where it runs |
|---|---|---|
| `hero_line.py` | the **gauge** — a fixed 3-line statusline | inside every Claude Code window |
| `hero_board.py` | the **game** — an animated fleet dashboard | its own terminal pane |

![hero_board](preview/hero-board.svg)

![hero_line](preview/hero-line.svg)

## Why two pieces?

v1 of this project tried to render a whole game *inside* the statusline.
Physics said no ([docs/DESIGN.md](docs/DESIGN.md) has the autopsy):

- Claude Code refreshes the statusline on **events, debounced 300 ms** — and
  not at all while idle. Smooth animation in a statusline is impossible.
- If your statusline's **line count varies**, the prompt box jumps up and down.
- Emoji and even common block glyphs have **terminal-dependent widths**;
  naïve centering math drifts columns.

So v2 splits it: the statusline is a *gauge* — exactly 3 lines, every line
padded to an exact display width, alignment computed with ANSI-aware
East-Asian-Width rules, missing data rendered as dim placeholders instead of
dropped rows. The *game* — pixel-art heroes, pillars, blinking beacons, real
frame rate — lives in `hero_board.py`, a proper alt-screen TUI where 4+ fps is
trivial.

## The gauge (statusline)

```
sightlab [Fable 5] ⎇ main·2              xhigh · $14.79 · 1h36m
5h ░░░░░░░░░░.░░░░░░░🦊░⭐░░░░░⭐░░░░░░🏁  55% ↻2h11m
ctx ████████░░░░ 64% 128/200k · 7d █████░░░░░ 48% ↻3d0h · ⚡2 ❗1
```

- **Line 1** — project, model, git branch (+dirty count), effort, session
  cost, elapsed time.
- **Line 2** — your hero walks the **5-hour rate-limit track**. ⭐ coins mark
  each 20% of budget (collected as you pass), 🏁 is the limit. Colors go
  green → yellow → red.
- **Line 3** — **context** meter (the thing that triggers compaction),
  **7-day** meter, and a **fleet summary**: ⚡ how many other windows are
  working, ❗ how many need you — visible from *any* window.
- No Pro/Max rate data (API billing)? The track dims and shows `--%`.
  Rows are never dropped; the box never moves.

## The board (fleet dashboard)

Open a spare pane, run `python3 hero_board.py`, keep it in the corner:

- one **pixel-art hero per live session** — big enough to actually see
- each stands on a **pillar = its context %** (red pillar ≈ compaction soon)
- state beacons: ⚡ working (hero trots) · ❗ **NEEDS YOU** (blinks, name
  inverted, macOS notification) · 💤 idle · 🌀 compacting · 👻 stale
- last activity per session: `you: fix the tests…`, `Bash: pytest -q…` —
  so you know *which* window to jump to and *why*
- header: account-wide 5h/7d meters, Σ cost, live count
- `m` toggles a dense list mode; `q` quits; `--fps 8` if you want it smoother

![hero_board list](preview/hero-board-list.svg)

## Install

Requires Python ≥3.9 (macOS system Python is fine) and any modern terminal
(iTerm2, Windows Terminal, WezTerm, Kitty…). No pip packages.

```bash
git clone https://github.com/hhchuan89/claude-status-hero
cd claude-status-hero
python3 hero_line.py --install     # statusline + hooks → ~/.claude/settings.json
python3 hero_board.py --demo       # try the board right now, with fake data
```

`--install` backs up your `settings.json` first and prints the rollback
command; `--uninstall` removes exactly our entries and nothing else. The hooks
(9 events, 5 s timeout, always exit 0) are what feed per-session state to the
board — without them you still get metrics, just not working/needs-you.

Try before installing:

```bash
python3 hero_line.py --demo        # three sample statusline states
python3 hero_line.py --simulate    # animated fake session (GIF material)
python3 hero_line.py --doctor      # alignment + color diagnostics
```

## If something misaligns

Run `--doctor` — if its test rows don't line up, your terminal disagrees
about some glyph's width:

- `STATUS_HERO_ASCII=1` — pure ASCII everywhere (old PowerShell, plain TTYs)
- `STATUS_HERO_AMBIG_WIDE=1` — for CJK terminals that render
  ambiguous-width glyphs as double width (implies ASCII bars)
- `NO_COLOR=1` — monochrome

The glyph palette is deliberately conservative: bars use `█ ░`, emoji only
from the always-two-columns set (dedicated pictographs like 🦊 ⭐ 🏁 ⚡ ❗),
never width-trap symbols like ⚠ ⏱ ✔ that depend on variation selectors.

## How state flows

```
Claude Code ──stdin JSON──► hero_line.py ──┐ metrics, every render
Claude Code ──9 hooks────► hero_line.py ──┤ state transitions
                                           ▼
                     ~/.claude/status-hero/sessions/<session_id>.json
                                           ▲
hero_board.py ──reads all, 4 fps──────────┘
```

States: `SessionStart/UserPromptSubmit/PostToolUse → working`,
`Notification(permission|idle) → needs_you`, `Stop → idle`,
`Pre/PostCompact → compacting`, `SessionEnd → file deleted`. A session whose
heartbeat is stale decays to idle, then ghost 👻, then is buried after 24 h.
Everything is local files — no network, ever. (Prompt snippets are stored in
that local state dir; they never leave your machine.)

## Tests

```bash
python3 tests/test_render.py   # 1200+ checks: geometry, hooks, installer, board
```

The suite hammers the invariants: always 3 lines, exact display width for
every payload × terminal width × glyph mode, ANSI-injection sanitization,
installer idempotence, foreign-hook preservation.

## Prior art / thanks

[ccstatusline](https://github.com/sirmalloc/ccstatusline) ·
[claude-powerline](https://github.com/Owloops/claude-powerline) ·
[ccusage](https://github.com/ryoppippi/ccusage) ·
[claude-squad](https://github.com/smtg-ai/claude-squad) ·
[chafa](https://hpjansson.org/chafa/) — and Anthropic's own Agent View.
The niche this fills: the fun layer and the fleet layer, merged.

## License

MIT — see [LICENSE](LICENSE).
