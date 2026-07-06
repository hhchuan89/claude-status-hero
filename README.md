# claude-status-hero

A tiny **pixel-art game scene rendered live inside the Claude Code status line** — a hero climbs a mountain whose height is your context window, walks a track set by your 5-hour usage, and hops toward a flag, all in true-colour terminal graphics. Pure Python, single file, Windows-Terminal-friendly.

> It's not ASCII art and it's not an image protocol — it's real per-pixel colour packed into ordinary Unicode block characters, so it survives the Claude Code status-line pipe on any terminal that supports true-colour.

<!-- Add a screenshot/GIF here — the colour is the whole point. e.g. ![screenshot](preview/screenshot.png) -->

## Scenes & looks

| Toggle | Options | What it changes |
|---|---|---|
| `--set-style` | `flat` · `summit` | Side-scroller track, or a mountain climb |
| `--set-theme` | `day` · `cyber` | Painted daylight sky, or a black-bg neon **synthwave** look |
| `--set` (hero) | `mario` `pika` `ghost` `goomba` `slime` `avatar` | Which character |
| `--set-pixels` | `half` · `sext` | Pixel density (sextant = 3× finer, needs a modern font) |
| `--set-size` | `small` · `medium` · `large` | Scene height |

Everything below the scene is real data: **5h / 7d rate-limit windows**, **context %**, session time, cost, and a coin tally — colour-coded green → yellow → red.

## Install

Requires **Python 3** and a terminal with **true-colour** support (Windows Terminal, iTerm2, most modern terminals). For the fine `sext` pixels you need a font with *Symbols for Legacy Computing* glyphs — e.g. **Cascadia Code ≥ 2404.23** (ships with Windows Terminal). If sextants show as boxes, run `--test-glyphs` and fall back to `--set-pixels half`.

```bash
# 1. copy the script into your Claude config dir
cp statusline-game.py ~/.claude/statusline-game.py

# 2. point Claude Code at it — in ~/.claude/settings.json:
#    "statusLine": { "type": "command",
#                    "command": "python ~/.claude/statusline-game.py",
#                    "refreshInterval": 1 }
#    (refreshInterval:1 drives the 1 fps jump; omit it for a static scene / lower cost)

# 3. pick your look (writes tiny config files next to the script)
python ~/.claude/statusline-game.py --list          # see everything + current settings
python ~/.claude/statusline-game.py --set-theme cyber
python ~/.claude/statusline-game.py --demo           # preview right now, in colour
```

On Windows PowerShell use `$HOME\.claude\statusline-game.py` (the shell doesn't expand `~` for `python`).

## How it works (the teaching bit)

Claude Code pipes [JSON session data](https://code.claude.com/docs/en/statusline) to the script on stdin. It reads the fields it needs and prints coloured text; Claude Code shows whatever it prints.

**Rendering.** A terminal cell can't show an image, but a single character *can* carry two independent 24-bit colours (foreground + background via `\033[38;2;r;g;b` / `48;2;...`). By choosing the right block glyph you pack multiple sub-pixels into one cell:

| Glyph set | Sub-pixels/cell | Notes |
|---|---|---|
| Half-block `▀ ▄` | 1×2 | universal, the safe baseline |
| Sextant `🬀–🬻` (U+1FB00) | 2×3 | 3× finer, needs a modern font |

The scene is painted into a pixel canvas, then each terminal row packs 2 (half) or 3 (sext) pixel-rows into one line of glyphs. **Image protocols (Sixel / Kitty) are deliberately not used** — Claude Code captures and re-renders the status-line output, which corrupts raw image escape sequences; block glyphs are just printable text, so they always survive.

**Fields read:** `model.display_name`, `workspace.current_dir`, `context_window.used_percentage`, `cost.total_cost_usd` / `total_duration_ms`, `rate_limits.five_hour` / `seven_day` (`used_percentage`, `resets_at`). Every field is guarded — missing → that element is dropped, never an error. (Rate limits are Pro/Max-only and appear after the first API response; on API/Console billing the hero falls back to standing on the context bar.)

**Windows-hardened.** Forces UTF-8 on stdin/stdout and sets `newline=""` so Windows Python doesn't turn `\n` into `\r\n` and corrupt the multi-row output — a bug the official docs don't mention. Git branch is cached ~10 s so `refreshInterval:1` doesn't spawn `git` every second.

## Prior art / thanks

Part of a small, recent cluster of Claude-Code pixel-art projects — worth a look:
[claude-code-mascot-statusline](https://github.com/TeXmeijin/claude-code-mascot-statusline) (half-block mascot in the status line),
[pixtuoid](https://github.com/IvanWng97/pixtuoid) (animated half-block "office" of agent sessions),
and [chafa](https://hpjansson.org/chafa/) (the reference for terminal image→glyph rendering).

## License

MIT — see [LICENSE](LICENSE).
