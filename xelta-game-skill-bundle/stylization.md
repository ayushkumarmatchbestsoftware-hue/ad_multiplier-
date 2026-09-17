# Stylization — STYLE FORMULA & Asset Prompt Assembly

> Open this file before any asset generation call. The STYLE FORMULA must come
> from this file — never invented ad-hoc. Every `generate_image` prompt must
> embed the STYLE FORMULA byte-identical.

---

## 1. What is the STYLE FORMULA?

A single compact string that encodes the game's complete visual identity.
Every asset prompt ends with it — unchanged, character-for-character.
This keeps sprites, backgrounds, UI, and effects visually consistent across
all `generate_image` calls even when jobs run in parallel.

**Format:**
```
[ART_STYLE] | [PALETTE] | [LINE_WEIGHT] | [LIGHTING] | [MOOD] | game asset, transparent background
```

**Example:**
```
pixel art, 16-bit SNES era | warm earth tones, deep navy, gold accents | crisp 2px outline | soft side lighting | adventurous and cozy | game asset, transparent background
```

---

## 2. Deriving the STYLE FORMULA

Work through these four steps in reasoning, then post the result:

### Step 1 — Art style
Pick one anchor style that fits the game genre and brief:

| Genre fit | Art style options |
|---|---|
| Retro / arcade | `pixel art, 8-bit` · `pixel art, 16-bit SNES era` · `pixel art, 32-bit` |
| Modern 2D | `flat vector illustration` · `hand-drawn cartoon` · `clean line art, cel-shaded` |
| Dark / moody | `dark fantasy illustration` · `noir ink wash` · `gritty comic book` |
| Cute / casual | `kawaii chibi style` · `pastel watercolor` · `soft rounded cartoon` |
| Sci-fi | `neon cyberpunk` · `isometric sci-fi` · `clean UI illustration, HUD-style` |
| Abstract / puzzle | `geometric flat design` · `minimalist vector` · `bold graphic` |

### Step 2 — Palette
3–5 colors max. Name them explicitly.

Examples:
- `warm earth tones, deep navy, gold accents`
- `neon cyan, hot pink, dark purple, black`
- `pastel mint, lavender, cream white, soft coral`
- `forest green, rust orange, cream, charcoal`

### Step 3 — Line weight + lighting
- Line weight: `no outline` · `1px crisp outline` · `2px bold outline` · `thick ink outline`
- Lighting: `flat lighting` · `soft side lighting` · `top-down lighting` · `dramatic rim light`

### Step 4 — Mood
One or two words: `adventurous` · `cozy` · `tense` · `playful` · `mysterious` · `epic` · `serene`

---

## 3. Posting the STYLE FORMULA

After deriving it, post to chat in this format:

```
Style: [ART_STYLE] | [PALETTE] | [LINE_WEIGHT] | [LIGHTING] | [MOOD] | game asset, transparent background
```

If style was explicit in the brief → post and continue.
If style was not explicit → post and wait for approval before generating any assets.

---

## 4. Assembling asset prompts

Every `generate_image` call follows this structure:

```
[SUBJECT DESCRIPTION], [SIZE/COMPOSITION NOTES], [STYLE FORMULA]
```

**Subject description** — what the asset is, its pose/state, key details:
- Sprite: "hero character, idle standing pose, facing right, full body"
- Background: "forest clearing with ancient ruins, wide establishing shot, no characters"
- UI element: "health bar frame, ornate border, empty interior, horizontal"
- Tile: "stone floor tile, seamless-tileable, top-down view"
- Enemy: "slime enemy, round blob shape, two eyes, small size"

**Size / composition notes:**
- Sprites: "centered in frame, generous padding, full body visible"
- Backgrounds: "16:9 landscape, horizon at 1/3 height"
- Icons/UI: "square composition, centered"
- Tiles: "square tile, seamless edge matching"

**Example full prompt:**
```
hero character, idle standing pose, facing right, full body, centered in frame with padding, pixel art, 16-bit SNES era | warm earth tones, deep navy, gold accents | 2px bold outline | soft side lighting | adventurous and cozy | game asset, transparent background
```

---

## 5. Asset manifest (assets.csv)

Write this file to `design/assets.csv` inside the game folder.

Columns:
```
id, role, description, size, style_line, source
```

- `id` — unique snake_case name, e.g. `hero_idle`, `bg_forest`, `ui_healthbar`
- `role` — how the game uses it: `player_sprite` · `background` · `enemy_sprite` · `ui_element` · `tile` · `collectible` · `projectile` · `effect`
- `description` — the subject description for the prompt
- `size` — `1:1` · `16:9` · `4:3` · or pixel dimensions hint
- `style_line` — paste the full STYLE FORMULA here (same for every row)
- `source` — always `generate_image` for Xelta-generated assets

Example rows:
```
hero_idle, player_sprite, "hero character idle standing facing right full body centered", 1:1, [FORMULA], generate_image
bg_forest, background, "dense forest path with dappled light wide establishing shot", 16:9, [FORMULA], generate_image
ui_health, ui_element, "health bar frame ornate border empty interior horizontal", 4:1, [FORMULA], generate_image
```

---

## 6. Xelta generate_image params

| Param | Value |
|---|---|
| `prompt` | Full assembled prompt (subject + STYLE FORMULA) |
| `model_id` | **Always pass `flux-dev-workflow`** — omitting it returns a model list instead of generating |
| `size` | `1080p` for sprites/UI · `2K` for backgrounds · `4K` for hero art only |
| `optimize_prompt` | `true` (default) — Xelta enhances the prompt automatically |

Run all manifest jobs in parallel — do not wait for one before starting the next.

---

## 7. Mixboard for exploration

If the brief is vague and the user wants to explore styles before committing:

1. Call `create_mixboard` with a creative prompt: `"[genre] game asset mood board, [style direction]"`
2. Show the user the grid results
3. Have them pick a direction
4. Derive the STYLE FORMULA from the chosen direction

`create_mixboard` is exploration only — it does not produce individual downloadable assets. After choosing a style, generate real assets with `generate_image`.
