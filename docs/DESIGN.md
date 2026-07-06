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
| SessionEnd | file deleted |

Liveness: SessionEnd is primary cleanup; a recorded parent PID is checked as
backup; files stale >2 h render as dim ghosts 👻 before removal. Idle sessions
legitimately stop writing (no statusline refresh while idle) — staleness alone
is **not** death.

Hero identity: stable `hash(session_id)` into the 8-animal roster, collision
bumped to the next free slot. Lanes ordered by `started_at` — no reshuffling.

## Non-goals

- No Sixel/Kitty image protocols (Claude Code re-renders statusline output;
  raw escapes get corrupted — confirmed in v1).
- No Nintendo IP. The roster is IP-clean animals (a decision v1 already made).
- No network calls, no dependencies: Python ≥3.9 stdlib only, single files.

## Distribution

`git clone` + `python3 hero_line.py --install` (backs up settings.json, writes
statusLine + hooks entries, idempotent; `--uninstall` restores). Board is just
`python3 hero_board.py` in any spare pane.
