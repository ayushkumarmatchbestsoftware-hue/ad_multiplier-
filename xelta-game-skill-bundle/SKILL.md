---
version: 0.1.0
name: xelta-game
description: |
  Create and deploy playable browser games using Xelta AI tools.
  Builds complete games from a brief — puzzle, platformer, arcade, card,
  top-down RPG, word game — with AI-generated visual assets, then deploys
  to a live Vercel URL via Xelta.
  Use when: "make me a game", "build a browser game", "create a playable game",
  "make a puzzle game", "make an arcade game", "make a platformer",
  "build a card game", "create a top-down game", "make a word game",
  "deploy my game", "create game assets", "make sprites for a game",
  "generate a background for my game", "make a game I can share",
  "build a multiplayer game", "make a shooting game", "create a runner game".
  NOT for: standalone image generation only, website creation without gameplay,
  video reels only, street ads, marketing materials only.
argument-hint: "[game brief]"
allowed-tools: Bash
---

# Xelta Game

Build and deploy playable browser games. Uses Xelta MCP tools for asset
generation (`generate_image`, `create_mixboard`) and deployment
(`build_website`). Uses Bash for local testing and file operations.

**Local-first rule:** Every game is built and verified locally before any
Xelta deploy call. No exceptions.

## Step 0 — Prerequisite check

Before any work:
1. Call `xelta_connection_status` — if not authenticated, stop and tell the user.
2. Call `check_balance` — surface credits. If below 20 credits, warn before starting.

## Routing

| Request type | Route |
|---|---|
| **Full game from a brief** | Full pipeline — PLAN → BUILD → TEST → DEPLOY |
| **Game assets only** (sprites, backgrounds, UI) | Asset-only — `references/stylization.md`, then `generate_image` |
| **Design/concept only** (GDD, mechanics, no build) | PLAN only — no code, no assets |
| **Deploy an existing local game** | Skip to DEPLOY — user has a working `index.html` |
| **Mood board / concept art** | `create_mixboard` — not the full pipeline |
| **Game trailer / promo reel** | `create_reel` — not the full pipeline |

## Pipeline

Four phases, always in this order. No game code before PLAN closes. No deploy before TEST passes.

### PLAN

1. Read `references/game-design.md` in full before anything else.
2. Think through the game profile (time, space, agency, conflict, players, session) in reasoning only.
3. Open `references/stylization.md` — derive the **STYLE FORMULA**. Post it to chat. If style is not explicit in the brief, wait for approval before continuing.
4. Write `design/assets.csv` inside the game folder — every visual asset needed (columns: `id`, `role`, `description`, `size`, `style_line`).
5. Confirm: `build_website` available + credits sufficient.

PLAN produces: `design/assets.csv` + approved STYLE FORMULA.

### BUILD

1. Read `references/build-game.md` in full before writing any game code.
2. Submit all `generate_image` asset jobs in parallel — each prompt embeds the STYLE FORMULA byte-identical. **Always pass `model_id: 'flux-dev-workflow'`** — omitting it returns a model list, not an image.
3. Write the game as a **single self-contained `index.html`** — all CSS and JS inline, no external dependencies. Start from the skeleton in `references/build-game.md`.
4. Embed each `generate_image` result URL directly into the HTML/JS as assets land. **Do not skip image generation** — assets.csv is meaningless if the images are never generated. Procedural fallback only if `generate_image` returns a 401/auth error.
5. Save to `test-games/<game-name>/index.html` inside the `xelta games` folder.

BUILD produces: `test-games/<game-name>/index.html` (fully self-contained).

### TEST

1. Start a local server: `python -m http.server 8080` in the game folder.
2. Verify checklist (from `references/build-game.md` §5):
   - Core loop runs: start → play → win/lose → restart
   - All assets load (no broken images, no 404s)
   - Controls work: keyboard + touch
   - Game runs at smooth framerate
   - No JS errors in console
3. Fix any issues before proceeding. Do not deploy a broken game.

TEST is a hard gate. A game that fails local testing does not get deployed.

### DEPLOY

Only after TEST passes:
1. Call `build_website` with:
   - `prompt`: full `index.html` content, prefixed with: `"Serve this browser game exactly as provided — do not modify the HTML, CSS, or JS: "` followed by the complete source
   - `industry`: `"gaming"`
   - `pages`: `"Game"`
   - `palette`: palette string from the STYLE FORMULA
   - `deploy`: `true`
2. Deliver the returned live Vercel URL to the user.
3. Offer to call `create_reel` with a one-sentence game description for a shareable trailer.

## UX rules

1. Never expose pipeline internals — no phase names, stage numbers, tool names, or §-references in user-facing messages.
2. One hard stop only: STYLE FORMULA approval when style is not explicit in the brief.
3. After each phase: 1–3 plain sentences about the game in natural language — not the process.
4. Deliver the live URL. Never paste asset UUIDs, job IDs, or tool output internals.
5. Balance check is silent unless credits are low — then surface it clearly before any generation.
6. If TEST fails: describe the problem in game terms, fix it, retest. Do not skip ahead.

## Xelta tools reference

| Tool | When to use |
|---|---|
| `xelta_connection_status` | Step 0 — verify auth |
| `check_balance` | Step 0 + before heavy asset runs |
| `generate_image` | All visual assets: sprites, backgrounds, UI, cards, tiles, icons |
| `create_mixboard` | Mood boards, concept art grids, style exploration |
| `build_website` | DEPLOY — generates and deploys the game to Vercel |
| `create_reel` | Post-deploy: shareable game trailer from a description |
| `get_website_history` | Retrieve URLs for previously deployed games |

## Asset references

| Topic | Reference |
|---|---|
| STYLE FORMULA + prompt assembly rules | `references/stylization.md` |
| Game code skeleton + client rules + deploy flow | `references/build-game.md` |
| Design framework (profile, laws, concept, system) | `references/game-design.md` |

## Key rules — do not violate

- **STYLE FORMULA comes from `references/stylization.md`** — never invent ad-hoc style.
- **No game code before PLAN closes** — `assets.csv` written and FORMULA set first.
- **Single self-contained `index.html`** — all CSS and JS inline; no `<script src="...">` to external hosts; asset URLs are `https://` from `generate_image` results only.
- **`<script data-cfasync="false">`** on the game's single inline script tag, always — the production hosting CDN runs Cloudflare Rocket Loader, which rewrites undecorated script tags to defer execution and can silently break input handling while leaving the render loop looking fine.
- **Keyboard bound to `event.code`**, never `event.key` — letter bindings break on non-Latin layouts.
- **Local test before deploy** — TEST phase is a hard gate, not optional.
- **Never call Xelta APIs directly** — use MCP tools only.
- **`build_website` returns the Vercel URL** — never hand-construct the live link.
- **Save `get_website_history`** to retrieve URLs for games if the user asks for a previous deploy.
- **`generate_image` requires `model_id: 'flux-dev-workflow'`** — omitting it returns a model list, not an image.
- **Never skip image generation** — generate all assets from `assets.csv` and embed the URLs. Procedural fallback is only acceptable if `generate_image` returns an auth error.
- **Every game needs a reward system** — immediate feedback, session summary (stars/position), and persistent best (`localStorage`). A game with no scoring has no reason to replay.
- **Racing game physics must be calibrated** — `maxSpeed = trackLength / targetLapSeconds`. Read `references/build-game.md §9` before writing any racing game code.
