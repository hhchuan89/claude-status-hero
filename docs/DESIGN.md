# claude-status-hero v2 — Design

> v1 (single-file `statusline-game.py`, see git history) tried to render a full
> game scene *inside* the Claude Code status line. It failed for structural
> reasons, not implementation bugs. v2 splits the product in two.

## Why v1 could not work

| Symptom | Root cause (confirmed) |
|---|---|
| Animation janky / frozen | Statusline refresh is **event-driven + 300 ms debounce**; zero refreshes while idle (`refreshInterval` min is 1 s). Smooth animation in the statusline is physically impossible. |
| Input box jumped up/down | Line count varied between renders: jump pose inserted a blank row (`statusline-game.py:436-440`), missing data dropped whole rows. |
| Fleet pillars misaligned | Centering math counted ANSI escapes as visible chars (`:943-946`); emoji heroes have terminal-dependent width. |
| Sprites/coins illegible | A statusline row is ~14 px tall. 8-px sprites are at the edge of legibility; a `•` coin is invisible. |

## v2 architecture: two pieces, one data plane

```
┌────────────────────────────┐        ┌─────────────────────────────┐
│ hero_line.py (per window)  │ writes │ ~/.claude/status-hero/      │
│ • fixed 3-line HUD         │──────► │   sessions/<session_id>.json│
│ • never changes line count │        │   {metrics, state, activity}│
│ • hooks update state       │        └──────────────┬──────────────┘
└────────────────────────────┘                       │ reads (all files)
                                                     ▼
                                      ┌─────────────────────────────┐
                                      │ hero_board.py (own pane)    │
                                      │ • real TUI, alt-screen      │
                                      │ • smooth animation (4 fps+) │
                                      │ • one hero per session      │
                                      │ • NEEDS-YOU beacon + notify │
                                      └─────────────────────────────┘
```

**The statusline is a gauge. The board is the game.** Fun that needs motion
lives where motion is possible.

## hero_line.py — the gauge (per window)

Three lines, **always exactly three**, each padded/cropped to an exact display
width `W = clamp(40, COLUMNS-2, 100)`. Missing data renders as dim `--`
placeholders — a row is never dropped.

```
sightlab [Fable 5] ⎇ main·2        xhigh · $14.79 · 46m
5h ░░░░░🦊░⭐░░░░⭐░░░░░🏁 34% ↻2h13m
ctx ████████░░░░ 42% 84/200k · 7d ███░░░░░░░ 31% ↻2d4h · ⚡2 ❗1
```

- **L1 identity**: dir, model, git branch (+dirty count), effort, session cost,
  elapsed. Left/right groups joined by a padding spacer.
- **L2 the 5h track**: hero emoji walks left→right with the 5-hour window.
  ⭐ coins sit at 20/40/60/80 % (budget quarter-marks — decorative *and*
  functional); 🏁 at 100 %. Track/percent zone-coloured green→yellow→red.
  No rate limits (API billing) → dim track, hero at start, `--%`.
- **L3 meters**: context bar (input-token %, the compaction driver), 7-day bar,
  and a **fleet summary** (⚡ working / ❗ needs-you counts across all live
  sessions) so any window shows whether *another* window wants you.
- Every render also merges this session's metrics into its session file.

### Rendering discipline (the anti-jitter contract)

1. Line count is a constant. Period.
2. Display width is computed by an ANSI-aware, stdlib-only `disp_width()`:
   strip SGR sequences; `east_asian_width in (W,F)` → 2; combining → 0;
   safe-emoji override table → 2; ambiguous → 1 (env `STATUS_HERO_AMBIG_WIDE=1`
   switches to 2 **and** swaps block glyphs for ASCII).
3. Glyph budget: bars `█ ░` (+ ASCII fallback `# .`), track `░`,
   emoji only from the always-wide set (`🦊🐱🐸🦉🐧🐰🐻🦆 ⭐ 🏁 ⚡ ❗ 💤 ✅`).
   Never `⚠ ⏱ ✔ ❤` (VS16 width traps).
4. All external strings (dirnames, git branch, prompt snippets) are stripped of
   control chars before rendering — ANSI injection from data is impossible.
5. `STATUS_HERO_ASCII=1` → pure ASCII everywhere (PowerShell-proof).

## hero_board.py — the game (own pane)

A real terminal app: alt-screen, hidden cursor, 4 fps default, `q` quits.
Two modes:

- **scene** (default): each live session is a pixel-art animal (half-block,
  ~10×8 px — actually legible at this size) standing on a **pillar whose height
  is its context %**, state beacon above (⚡ bobbing = working, ❗ blinking =
  needs you, 💤 = idle, 🌀 = compacting, ✅ = done), name/ctx/cost below.
  Header HUD: account-wide 5h + 7d bars, Σ cost, live-session count.
- **list**: dense one-row-per-session mode (name, state, last activity,
  ctx bar, cost) for >8 sessions or narrow panes.

**Last activity** comes from hooks: last prompt snippet (UserPromptSubmit) or
last tool (`PostToolUse` → "Bash: pytest -q…"). That answers "which window is
doing what" at a glance. `--notify` (default on, macOS) fires a notification
when any session flips to needs-you.

## State machine (hooks → session file)

| Hook | State |
|---|---|
| SessionStart | `working` (records started_at, claims a hero) |
| UserPromptSubmit | `working` + prompt snippet |
| PostToolUse | `working` + tool name (doubles as heartbeat) |
| Notification `permission_prompt` | `needs_you` (reason: permission) |
| Notification `idle_prompt` | `needs_you` (reason: waiting) |
| Stop | `idle` |
| PreCompact / PostCompact | `compacting` / restore `working` |
| SessionEnd | tombstone `{"ended": true}` — see concurrency notes |

Liveness: SessionEnd is primary cleanup; a recorded parent PID is checked as
backup; files stale >2 h render as dim ghosts 👻 before removal. Idle sessions
legitimately stop writing (no statusline refresh while idle) — staleness alone
is **not** death.

Hero identity: stable `hash(session_id)` into the 8-animal roster, collision
bumped to the next free slot. Lanes ordered by `started_at` — no reshuffling.

### Concurrency (the session file is shared by 2+ processes)

A hook process and a statusline render can update the SAME session file
simultaneously — both are load → merge → `os.replace`. Two guards keep the
state machine honest:

- **No state rollback.** A metrics-only write (statusline) re-loads the file
  just before replacing it; if the on-disk `state_ts` advanced meanwhile (a
  hook fired), the fresher `state/activity` keys are re-merged. Without this,
  a ~ms race could roll `needs_you` back to `working` — and stick, because a
  blocked Claude fires no further hooks to correct it.
- **No resurrection.** `SessionEnd` writes a tombstone `{"ended": true}`
  instead of deleting: a statusline render already in flight (git subprocess
  can take seconds) would otherwise recreate the file and leave a zombie lane
  on the board for hours. Metrics writes refuse to touch a tombstone; a real
  hook (resumed session, same session_id) clears it. The board hides
  tombstones immediately but only DELETES them after 60 s — deleting on
  sight would remove the guard while the in-flight render is still coming
  (`load_json or {}` on a missing file skips the ended check), re-opening
  the exact race the tombstone exists to stop. `all_sessions()` sweeps
  stragglers after 10 min for statusline-only setups.

Known cosmetic race, accepted: two sessions starting in the same instant can
claim the same hero (`pick_hero` scan-then-write has no mutex, ~1/8 chance on
a hash collision). Harmless; a mutex isn't worth the complexity.

## Office mode (`--office`)

The board's third mode: a floor plan, ≤ 8 sessions in `started_at` order.
**Each session owns a vertical column** — its **desk** at the top, its
**break-room (茶水间) spot** at the bottom, an open floor between, a door in
the left wall. State picks which end of the column the hero belongs at, and
it *walks* there:

| state | where the hero is |
|---|---|
| working / compacting | at its **desk** (top of its column) |
| idle | walks **down its column** to the **茶水间** (bottom) |
| needs_you | **snaps** to its desk, blinks — no walk (attention first) |
| new session (`SessionStart`) | walks in through the **door** → its desk |
| ended (`SessionEnd`) | walks to the **door** → out, then dropped |

### v3: emoji actors, not sprites

The moving actor is a **single emoji — the exact same `HERO_EMOJI` glyph
`hero_line.py`'s statusline already shows for that window** (`HERO_EMOJI ==
hero_line.HERO_ROSTER`, one-to-one). That identity link is the whole point of
the redesign: glance at the office, glance at a terminal tab, same fox. Emoji
beat v2's 10×8 half-block sprites here on every axis that matters for this
project's terminals (iTerm2 + tmux + Windows Terminal) — see the Sixel
rejection under Non-goals.

An emoji is width-2/height-1 in the `Stage` compositor (vs. the sprite's
10×4), which is what makes the room dense instead of empty:

- **Desk pod** (drawn for *every* live session, whether or not its actor is
  currently there — the desk is the session's card): empty chair `🪑` +
  laptop `💻` (occupant cell gets overwritten by the hero when seated — same
  x, same width, clean replace), a desk-front bar (`▄`/ASCII `▄`→`=`, GRAY —
  **RED, not dim, under `needs_you`**, the only desk colouring), a **name
  plate** (width-cropped via `wcrop`, a new display-width-aware crop that
  appends `..` — CJK names must never overflow), and a **status row** = state
  beacon + `cost so far` (`⚡ $31.26`). That is the entire per-agent data
  surface: **name + state + cost. No ctx %, no usage meters, no pillar** —
  those already live in the statusline and in list mode; a `%` character
  never appears anywhere in office output.
- **Speech bubble**: for a *seated, not-walking* actor whose state is
  `working`/`compacting`/`needs_you` and has a non-empty `activity`, a
  `(parens)` balloon + `▼` tail pops up over its desk carrying that activity
  string (also `wcrop`-truncated). Never shown while walking, never in the
  break room, never for idle/ghost — an idle agent isn't doing anything, `💤`
  already says that. Bubbles are clamped into their own desk's column span so
  they can never collide with a neighbour's.
- **Ghost** sessions (stale > 2 h): the actor is `👻` instead of the hero;
  name and cost render dim.
- **Furniture** (fixed position, no randomness — `--once` stays byte-stable):
  a door `🚪` beside the wall gap, a wall clock `🕐`, potted plants `🪴`, a
  water cooler `🚰` in the break room, a two-row dim rug, and the 茶水间
  divider — still just a two-word label, not a scene (the old ASCII
  coffee-machine box is gone). All furniture is `not ASCII`-gated.

Design invariants:
- **Animation state lives ONLY in the renderer** (`ostate` = `{ag, leaving}`
  keyed by sid); session files are never touched. `--once` passes
  `ostate=None` → everyone at their home spot, no motion, so single frames are
  deterministic for tests. `office_move`'s signature, semantics, and the
  `seats`/`lounges`/`door_y`/`IW` geo keys are untouched by v3 — only the
  geometry *values* feeding it changed.
- **First interactive frame seats the existing fleet** (no walk-in replay on
  startup). Only sids appearing *after* that snapshot walk in; a vanished sid
  walks out; a sid that reappears mid-exit re-enters from the door. Homes are
  recomputed every frame from the current fleet, so a resize just re-targets —
  a walker can't be stranded (`_toward` clamps, arrival is a tolerance check).
- **Walk speed** = `max(2, IW//40)` cols/frame, moving x and y together; every
  walk settles in a few seconds at any pane size.
- **The Stage compositor** (`Stage`) is a 2D cell buffer. Backgrounds (the rug,
  the desk-front bar, the 茶水间 divider) are painted **per-cell**
  (`bar`/`glyph`) so a walker crossing them composites cleanly; only atomic
  labels (names, beacons, bubbles, furniture glyphs) use wide **runs**
  (`text`) — a run advances the render cursor past its whole span, so a run
  drawn *under* a later paint would swallow it. Draw order is backdrop → desks
  → actors → bubbles, so bubbles legitimately win any overlap (e.g. over the
  wall clock); a partial run-over-run overlap degrades to a blank cell, never
  to a width shear — `Stage.rows()` always emits exactly `IW` columns because
  every column index is visited exactly once regardless of how the grid was
  overwritten.
- **Geometry spreads instead of hugging the left, and caps its height**:
  `col_w = min(22, IW//n)`, columns centred (`left = (IW - n·col_w)//2`), each
  desk pod centred within its column (`pods[i] = left + i·col_w + (col_w-12)//2`);
  room height `rh = min(IH, 18)` — rows below `rh` are simply not emitted, so
  a big terminal gets a dense room, not a stretched-empty one. Fallback rule
  is unchanged: needs `IH ≥ 15` and `n·12 ≤ IW`; doesn't fit / > 8 sessions →
  falls back to the dense list, same as scene's overflow rule.
- **Columns are `started_at`-ordered and recomputed each frame**, so a session
  leaving mid-row shifts the others (they walk to the new column) and a board
  restart re-derives the order. NOT sticky across membership changes — don't
  assert cross-frame column order in interactive tests.
- `--demo` schedules a visitor purely off the frame counter (present frames
  12–59 of every 90-frame cycle, absent at frame 0) — interactive demo shows
  one entrance + one exit per cycle for GIFs; `--demo --once` stays byte-stable.

## Mouse (future — designed, not built)

Terminals do support mouse via ANSI: enable SGR mouse reporting
(`\x1b[?1000h` click, `\x1b[?1002h` drag/button-event, always with `\x1b[?1006h`
for clean extended coordinates) and the terminal delivers events on **stdin**
as `\x1b[<b;x;y;M` (press) / `…m` (release) — parsed in the same place the
board already reads `q`/`m`. iTerm2 and Windows Terminal both support it;
tmux needs `set -g mouse on` and can fight the app over drags. Two real costs,
both must be handled: (1) the modes **must be disabled on exit** or the
terminal is left unusable; (2) while mouse reporting is on, the terminal's
native text-selection/copy is suppressed (iTerm2: hold ⌥ to bypass) — a real
tax on a glance-at monitor.

Worth building, ranked by payoff:
- **Click a hero → raise its Claude Code window** (the point of the whole
  project: see who needs you, jump straight there). The click is trivial; the
  hard part is mapping a session → its OS window/tab — likely via matching the
  session `cwd`/title, or having the hook stamp the window id. Needs a
  feasibility spike before promising it.
- **Click a hero → detail popover** (full activity / ctx / cost / branch).
  Pure-local, no window mapping — the easy, high-value first step.
- **Hover a hero → highlight + tooltip** (cheap once reporting is on).
- **Drag to rearrange desks:** possible (`?1002h`), but low value for a
  monitor that auto-seats — explicitly *not* recommended.

Design stance: gate mouse behind a flag (`--mouse`, default off) so the
copy-selection tax is opt-in, and make click-to-focus the headline once the
session→window mapping is proven.

## Non-goals

- No Sixel/Kitty image protocols (Claude Code re-renders statusline output;
  raw escapes get corrupted — confirmed in v1). Re-litigated for office v3's
  emoji-vs-Sixel actor choice and rejected again: the board also runs inside
  tmux, and default tmux builds (incl. Homebrew's) compile **without**
  `--enable-sixel` — tmux would strip or garbage the escapes on that surface,
  and even hand-rebuilt tmux has documented Sixel bugs in exactly the
  animated-redraw case (ghost pixels, failed cell overwrites). Emoji work
  identically on iTerm2 + tmux + Windows Terminal, and — decisive for office
  v3 — are the pixel-identical glyph the statusline already shows
  (`HERO_EMOJI == hero_line.HERO_ROSTER`), which a bespoke Sixel sprite would
  break.
- No Nintendo IP. The roster is IP-clean animals (a decision v1 already made).
- No network calls, no dependencies: Python ≥3.9 stdlib only, single files.

## Distribution

`git clone` + `python3 hero_line.py --install` (backs up settings.json, writes
statusLine + hooks entries, idempotent; `--uninstall` restores). Board is just
`python3 hero_board.py` in any spare pane.
