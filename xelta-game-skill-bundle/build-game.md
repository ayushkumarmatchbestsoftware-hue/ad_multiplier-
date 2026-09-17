# Game Assembly, Testing & Delivery

> **Read order:** Read this LAST — after `game-design.md` and all asset references,
> but before writing any game code. The skeletons below must shape the code, not
> be retrofitted to it.

---

## 1. Output format — single self-contained HTML file

Every Xelta game is one file: `index.html`.

Rules:
- All CSS is inline in `<style>` tags.
- All JS is inline in `<script>` tags.
- No `<script src="...">` pointing to external CDNs or local files.
- No `<link rel="stylesheet">` to external hosts.
- Asset images are `https://` URLs from `generate_image` results — embedded directly in JS/CSS.
- The file must work when opened directly in a browser AND when served under a subpath.

Save to: `test-games/<game-name>/index.html` inside the `xelta games` folder.

---

## 2. Client rules (non-negotiable)

- **Canvas-based** — game renders on `<canvas>`. No DOM game objects.
- **Relative paths only** — if referencing any co-located file, use `./filename`. No `/` root-relative paths.
- **Keyboard bindings** — always `event.code` (physical key), never `event.key` (typed character). `'KeyW'` not `'w'`.
- **Touch support from day one** — left/right/action zones mapped on the canvas. No hover-only interactions.
- **Responsive canvas** — resize on `window` `resize` and `orientationchange`. DPR capped at 1.5.
- **Fixed-timestep loop** — game logic runs at 60 ticks/sec regardless of display framerate.
- **Pause on blur** — game pauses when the tab loses focus; resumes on focus.
- **Dev overlay** — FPS + frame time shown when `?dev=1` is in the URL; hidden by default.
- **Seeded RNG** — any randomness uses a seedable function, not `Math.random()` directly, so runs are reproducible.

---

## 3. Solo game skeleton

Replace `update()` and `render()` and the `BIND` table with the actual game:

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
  <title>Game Title</title>
  <style>
    html, body { margin: 0; height: 100%; overflow: hidden; background: #0f0f1a; }
    canvas { display: block; }
    #dev { position: fixed; top: 6px; left: 6px; color: #0f0; font: 11px monospace;
           background: rgba(0,0,0,.5); padding: 2px 6px; display: none; pointer-events: none; }
  </style>
</head>
<body>
<canvas id="c"></canvas>
<div id="dev"></div>
<script data-cfasync="false">
// ── CONFIG ─────────────────────────────────────────────────────────────────
const CFG = {
  gravity:    0.5,
  jumpForce:  -12,
  moveSpeed:  4,
  // all balance numbers live here — tune one at a time
};

// ── ASSETS ─────────────────────────────────────────────────────────────────
// Populate with https:// URLs from generate_image results
const IMG = {};
function loadImage(key, url) {
  const img = new Image(); img.src = url;
  IMG[key] = img;
}
// loadImage('hero',    'https://...');
// loadImage('bg',      'https://...');
// loadImage('enemy',   'https://...');

// ── INPUT ──────────────────────────────────────────────────────────────────
// event.code (physical key), never event.key (typed character)
const BIND = {
  KeyW: 'up',    ArrowUp: 'up',
  KeyS: 'down',  ArrowDown: 'down',
  KeyA: 'left',  ArrowLeft: 'left',
  KeyD: 'right', ArrowRight: 'right',
  Space: 'action', KeyZ: 'action', KeyX: 'action2',
  Escape: 'pause',
};
const held = new Set();
addEventListener('keydown', e => { const c = BIND[e.code]; if (c) { held.add(c); e.preventDefault(); }});
addEventListener('keyup',   e => { const c = BIND[e.code]; if (c) held.delete(c); });

// Touch zones: left 40% = move, right 40% = action, middle = both
const touch = new Set();
addEventListener('touchstart', e => {
  for (const t of e.changedTouches) {
    const x = t.clientX / innerWidth;
    if (x < 0.4) touch.add('left');
    else if (x > 0.6) touch.add('action');
  }
  e.preventDefault();
}, { passive: false });
addEventListener('touchend', e => { touch.clear(); e.preventDefault(); }, { passive: false });

const cmds = () => new Set([...held, ...touch]);

// ── CANVAS ─────────────────────────────────────────────────────────────────
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const DPR_CAP = 1.5;
let W, H;
function resize() {
  const dpr = Math.min(devicePixelRatio || 1, DPR_CAP);
  W = innerWidth; H = innerHeight;
  canvas.width  = W * dpr; canvas.height = H * dpr;
  canvas.style.width  = W + 'px'; canvas.style.height = H + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener('resize', resize);
addEventListener('orientationchange', resize);
resize();

// ── SEEDED RNG ─────────────────────────────────────────────────────────────
function makeRng(seed) {
  let s = seed;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 0xffffffff; };
}
let rng = makeRng(42);

// ── GAME STATE ─────────────────────────────────────────────────────────────
let state = {}; // replace with actual initial state

function initState() {
  rng = makeRng(Date.now() | 0);
  state = {
    phase: 'playing', // 'playing' | 'gameover' | 'win'
    score: 0,
    // ... game-specific state
  };
}

// ── SIMULATION ─────────────────────────────────────────────────────────────
function update(dt, input) {
  if (state.phase !== 'playing') {
    if (input.has('action')) initState();
    return;
  }
  // game logic here — deterministic, uses rng(), never Math.random()
}

// ── RENDERING ──────────────────────────────────────────────────────────────
function render(alpha) {
  ctx.clearRect(0, 0, W, H);
  // draw background, entities, UI
  // alpha is the interpolation factor (0–1) for smooth motion between ticks
}

// ── FIXED-TIMESTEP LOOP ────────────────────────────────────────────────────
const STEP = 1000 / 60;
let acc = 0, last = performance.now(), paused = false;
let frames = 0, fpsAt = performance.now(), fps = 0, frameMs = 0;

addEventListener('blur',  () => { paused = true; });
addEventListener('focus', () => { paused = false; last = performance.now(); });

const devEl = document.getElementById('dev');
const showDev = new URLSearchParams(location.search).has('dev');
if (showDev) devEl.style.display = 'block';

function frame(now) {
  requestAnimationFrame(frame);
  if (paused) return;
  const elapsed = now - last; last = now;
  acc += elapsed;
  const input = cmds();
  while (acc >= STEP) { update(STEP, input); acc -= STEP; }
  render(acc / STEP);
  if (showDev) {
    frames++; frameMs = elapsed;
    if (now - fpsAt >= 500) {
      fps = Math.round(frames * 1000 / (now - fpsAt));
      frames = 0; fpsAt = now;
    }
    devEl.textContent = fps + ' fps | ' + frameMs.toFixed(1) + ' ms';
  }
}

initState();
requestAnimationFrame(frame);
</script>
</body>
</html>
```

---

## 4. Wiring assets

Asset URLs come from `generate_image` results. Wire them in as jobs complete:

```js
loadImage('hero',   'https://cdn.xelta.ai/...');  // from generate_image result
loadImage('bg',     'https://cdn.xelta.ai/...');
loadImage('enemy',  'https://cdn.xelta.ai/...');
```

Draw them with:
```js
ctx.drawImage(IMG['hero'], x, y, w, h);
```

If an image isn't loaded yet, draw a colored placeholder rect so the game runs during dev.

---

## 5. Local test checklist (TEST gate — hard, no skipping)

Run `python -m http.server 8080` in the `test-games/<game-name>/` folder:

```
python -m http.server 8080
```

Open `http://localhost:8080` and verify:

- [ ] Game starts and first meaningful action is reachable within 5 seconds
- [ ] Core loop works: play → win/lose → restart
- [ ] All assets load — no broken image icons, no 404s in the browser console
- [ ] Keyboard controls work (`WASD`, arrows, `Space`)
- [ ] Touch controls work — test by shrinking the browser window to mobile size
- [ ] Game runs at 60 fps — check with `?dev=1` overlay
- [ ] No JS errors in the browser console (`F12 → Console`)
- [ ] Pause on tab-switch works (blur/focus)
- [ ] Game handles spam input without breaking
- [ ] `data-cfasync="false"` present on the `<script>` tag

Fix every failing item. Do not call `build_website` until all items pass.

**If the user pastes a screenshot of a visual bug, `Read` it before touching any code.** Fixing from
a text description alone has produced wrong fixes twice in this project's history — a screenshot
shows the exact problem (e.g. a zigzag edge means a `fillRect`-per-strip bug, not a curve-math bug);
guessing from wording alone risks fixing the wrong thing.

### Actually rendering the game, not just loading it

`node --check` and an HTTP 200 only prove the file parses and the server responds — they cannot catch a rendering bug (wrong perspective, broken shader, oversized/misplaced geometry). If Playwright is available, prefer driving a real headless Chromium over the page instead of asking the user to be the only visual QA:

```bash
npm init -y && npm install playwright && npx playwright install chromium
```

Launch with `args: ['--use-angle=swiftshader', '--no-sandbox']` — for WebGL games, `--use-gl=swiftshader` alone can leave the context lost (`gl.isContextLost() === true`) on some machines; `--use-angle=swiftshader` is the one that actually renders. Script a `goto` → click-to-start → move (`keyboard.down('KeyW')`) → `screenshot()`, check `console`/`pageerror` events, and **read the resulting PNG** — don't just check for the absence of console errors, look at whether the thing you changed actually looks right. This is not a replacement for the user's own playtest (feel, balance, and multi-minute bugs still need a human), but run it before telling the user a visual fix "should" work.

---

## 6. Deploy flow (DEPLOY phase — only after TEST passes)

Call `build_website` with:

```
prompt:   "Serve this browser game exactly as provided — do not modify the HTML, CSS, or JS:\n\n" + <full index.html content>
industry: "gaming"
pages:    "Game"
palette:  <palette string from STYLE FORMULA>
deploy:   true
```

The tool returns a Vercel URL. That URL is the deliverable — deliver it directly to the user.

**Never construct the URL by hand.** Use only what `build_website` returns.

To update an existing game after changes: re-run the full TEST checklist, then call `build_website` again. Check `get_website_history` to see previous deploys.

---

## 7. Post-deploy trailer (optional)

Call `create_reel` with a single descriptive sentence:

```
prompt: "Fast-paced browser game trailer — [game title]: [one sentence describing the gameplay and visual style]"
```

Deliver the reel URL alongside the game URL.

---

## 8. Common mistakes

| Mistake | Fix |
|---|---|
| `event.key === 'w'` for movement | Change to `event.code === 'KeyW'` |
| `Math.random()` in game logic | Replace with `rng()` (seeded) |
| External `<script src>` or CDN links | Inline all JS/CSS |
| Asset URLs hardcoded to `http://` | Must be `https://` |
| `/absolute/path` references | Change to `./relative` |
| Deploy before TEST passes | Run TEST, fix, then deploy |
| `document.getElementById` per frame | Cache DOM refs outside the loop |
| New objects allocated every frame | Pre-allocate, reuse, use pools |
| Skipping `generate_image` — drawing everything procedurally | assets.csv is meaningless without generated images wired in. Generate all assets, embed URLs. Procedural fallback only on auth error. |
| Omitting `model_id` in `generate_image` | Always pass `model_id: 'flux-dev-workflow'` — omitting returns a model list, not an image |
| Opponents initialized at negative positions | Start opponents at positive positions ahead of player (see §9) |
| `maxSpeed` too low relative to track length | Calibrate: `maxSpeed = trackLength / targetLapSeconds` (see §9) |
| Speed/visual effects drawn over sky | Clip effects to road zone — `ctx.rect(0, H*0.5, W, H*0.5); ctx.clip()` before drawing |
| No scoring system | Every game needs immediate + session + persistent reward (see game-design.md §5.6) |
| Mouse-look inverted or hard to fix | Use the confirmed sign convention in §10 — don't hand-derive rotation matrix signs from scratch |
| Jump never lands player on crates/platforms | Height-blind collision — use the `feetY` model in §10, not a pure vertical bob decoupled from XZ collision |
| `<script>` tag with no `data-cfasync="false"` | Cloudflare's Rocket Loader (enabled on the production hosting zone) rewrites script tags to defer execution, which can silently break input wiring while leaving render loops looking fine. Add `data-cfasync="false"` to the single inline `<script>` tag on every game, always — not just reactively after a report. |
| `sawtooth`/`square` oscillators for engine/motor audio | Use `triangle` — confirmed harsh/buzzy otherwise (see §9 audio) |
| Gear-based engine pitch (discrete jumps) | Drive pitch as a continuous function of speed only — gear resets cause audible ping-pong (see §9 audio) |

---

## 9. Racing game patterns

### Physics calibration — speed vs track length

`maxSpeed` and track length must be calibrated together. Rule:

```
maxSpeed = (lapSegments × segLen) / targetLapSeconds
```

Example: `lapSegments: 300`, `segLen: 200`, 60-second target lap → `maxSpeed = 300 × 200 / 60 = 1000`.

**Never set `lapSegments` above 400 without scaling `maxSpeed` accordingly.** A mismatch is the single most common reason a racing game feels impossibly slow.

Recommended starting values:
```js
const CFG = {
  segLen:       200,
  lapSegments:  300,       // short track feels fast — scale up later
  maxSpeed:     1000,      // units/sec — calibrated to 60-sec lap
  accel:        18,        // units per tick (added per fixed step)
  brake:        30,
  decel:        10,
  drawDist:     150,
  fogDist:      130,
  roadWidth:    2200,
  cameraHeight: 1000,
  cameraDepth:  0.84,
  playerZ:      600,
  centrifugal:  0.25,
  numOpponents: 4,
  totalLaps:    3,
  medalThresholds: { gold: 55, silver: 70, bronze: 90 }, // lap seconds
};
```

### Opponent placement — start AHEAD of player

Opponents must start at **positive positions**, staggered ahead of the player (who starts at 0):

```js
opponents: Array.from({ length: CFG.numOpponents }, (_, i) => ({
  position: (i + 1) * CFG.segLen * 12,   // ahead of player — never negative
  x:        (i % 2 === 0) ? -0.3 : 0.3,
  speed:    CFG.maxSpeed * (0.78 + rng() * 0.14),
  color:    OPP_COLORS[i % OPP_COLORS.length],
  lane:     (i % 2 === 0) ? -0.35 : 0.35,
}))
```

### Opponent rendering — use mod() for wrap-around

The naive `relPos = opp.position - player.position` breaks when either wraps past the end of the track. Use mod:

```js
const totalDist = N * CFG.segLen;
const relPos = mod(opp.position - S.position, totalDist);
if (relPos > CFG.drawDist * CFG.segLen) return; // behind or too far ahead
```

### Race position tracking

```js
function getRacePosition() {
  const totalDist = N * CFG.segLen;
  const playerProg = mod(S.position, totalDist);
  return 1 + S.opponents.filter(o =>
    mod(o.position, totalDist) > playerProg
  ).length;
}
```

Show position live in HUD as `P1 / P5` etc.

### Scoring — all three tiers (required)

```js
// Session reward — shown on finish screen
function getStarRating(bestLapSec) {
  if (bestLapSec <= CFG.medalThresholds.gold)   return 3;
  if (bestLapSec <= CFG.medalThresholds.silver)  return 2;
  if (bestLapSec <= CFG.medalThresholds.bronze)  return 1;
  return 0;
}

// Persistent reward — localStorage
function loadBest() {
  return parseFloat(localStorage.getItem('neon_bestLap') || 'Infinity');
}
function saveBest(t) {
  if (t < loadBest()) localStorage.setItem('neon_bestLap', t);
}
```

Finish screen must show: **final position**, **star rating**, **total time**, **best lap**, and personal best comparison ("New best!" or "Best: X").

### Visual effects — clip to road zone

Speed lines, exhaust, and motion blur must never render over the sky. Always clip:

```js
// Speed lines — road area only
ctx.save();
ctx.beginPath();
ctx.rect(0, H * 0.48, W, H * 0.52);
ctx.clip();
// ... draw speed lines ...
ctx.restore();
```

### Audio — layered engine synthesis

Two oscillators + noise give a much richer engine sound than one. **Use `triangle` for both
oscillators, never `sawtooth`/`square`** — confirmed in Neon Highway that sawtooth+square at low
Hz sounds harsh/buzzy (user-reported "very annoying"); triangle is dramatically smoother. **Drive
pitch as a continuous function of speed, not discrete gear steps** — a gear model causes audible
frequency ping-pong on every gear reset; the version below monotonically maps speed → pitch with
no jumps.

```js
function initAudio() {
  audioCtx = new AudioContext();

  // Layer 1: base engine rumble (triangle — NOT sawtooth, which sounds harsh at low Hz)
  engBase = audioCtx.createOscillator();
  engBase.type = 'triangle';
  engBaseGain = audioCtx.createGain();
  engBase.connect(engBaseGain);

  // Layer 2: harmonic overtone (triangle, ~1.52x freq — NOT exact 2x, which reads as harsh octave)
  engHarm = audioCtx.createOscillator();
  engHarm.type = 'triangle';
  engHarmGain = audioCtx.createGain();
  engHarm.connect(engHarmGain);

  // Layer 3: tire/wind noise (bandpass-filtered white noise)
  const buf = audioCtx.createBuffer(1, audioCtx.sampleRate * 2, audioCtx.sampleRate);
  buf.getChannelData(0).forEach((_, i, d) => d[i] = Math.random() * 2 - 1);
  noiseNode = audioCtx.createBufferSource();
  noiseNode.buffer = buf; noiseNode.loop = true;
  noiseFilter = audioCtx.createBiquadFilter();
  noiseFilter.type = 'bandpass'; noiseFilter.frequency.value = 1400;
  noiseGain = audioCtx.createGain(); noiseGain.gain.value = 0;
  noiseNode.connect(noiseFilter); noiseFilter.connect(noiseGain);

  [engBaseGain, engHarmGain, noiseGain].forEach(g => g.connect(audioCtx.destination));
  engBase.start(); engHarm.start(); noiseNode.start();
}

function updateAudio(speed, maxSpeed) {
  if (!audioCtx) return;
  const t = speed / maxSpeed;
  const SMOOTH = 0.15; // setTargetAtTime constant — below this, pitch/gain snap audibly

  // Monotonic function of speed only — no gear steps, so no ping-pong on gear reset
  const baseFreq = 65 + t * 90;
  engBase.frequency.setTargetAtTime(baseFreq,        audioCtx.currentTime, SMOOTH);
  engHarm.frequency.setTargetAtTime(baseFreq * 1.52, audioCtx.currentTime, SMOOTH); // not 2x — avoids octave harshness
  engBaseGain.gain.setTargetAtTime(0.038 + t * 0.050, audioCtx.currentTime, SMOOTH);
  engHarmGain.gain.setTargetAtTime(0.007 + t * 0.013, audioCtx.currentTime, SMOOTH);
  noiseGain.gain.setTargetAtTime(t > 0.55 ? (t - 0.55) * 0.15 : 0, audioCtx.currentTime, 0.08);
}
```

If the game wants a shift/beep cue for some other event (not gear-based pitch), keep it short and quiet:
```js
} else if (type === 'shift') {
  o.type = 'sine'; o.frequency.value = 260; // 260 Hz — anything higher reads as piercing
  g.gain.setValueAtTime(0.03, audioCtx.currentTime); // 0.03 max gain
  g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.08);
  o.start(); o.stop(audioCtx.currentTime + 0.08);
}
```

---

## 10. First-person / 3D shooter patterns

Applies to any raw-WebGL FPS/first-person game built per the top-level `CLAUDE.md` "3D racing / first-person / 3D anything → inline WebGL only" rule.

### Mouse-look sign convention — confirmed correct, do not re-derive from scratch

For a camera built as `camWorld = T(pos)·Ry(yaw)·Rx(pitch)`, with `forward = camWorld · (0,0,-1)`:

```js
// mouse (pointer lock)
pendingYaw   -= e.movementX * sens;
pendingPitch -= e.movementY * sens;

// touch drag-look
pendingYaw   -= dx * touchLookSens;
pendingPitch -= dy * touchLookSens;
```

Both axes are **minus**. This was gotten wrong twice in practice: pitch's sign was mis-derived by mentally reasoning about "matrix columns" instead of multiplying `R·v` row-by-row from the matrix's literal array layout, and yaw was flipped on a first guess when a user's bug report ("mouse works opposite") didn't say which axis was wrong. When a user reports inverted mouse, isolate the axis from their wording first — "left moves right" is yaw, "up/down backwards" is pitch — don't flip both.

### Jumping onto platforms — height-aware collision

A pure vertical bob offset (fully decoupled from XZ collision) makes it impossible to ever land on a crate, no matter how high the jump — horizontal collision rejects entry into the crate's footprint before gravity gets a chance to place the player on top of it.

```js
// state: absolute feet height, not a separate ground-flag + bob offset
state.feetY = 0;

function computeGroundHeight(x, z) {
  // tallest obstacle top whose footprint contains (x,z), else 0 (ground)
}

function resolvePlayerCollision(x, z) {
  // only push back for walls where wall.top > feetY + stepTolerance —
  // i.e. skip (walk over) any wall whose top is at/below current feet height
}
```

Each tick: apply gravity to `feetY`, then clamp to `computeGroundHeight(x, z)` at the new XZ position if falling through it (landing). Calibrate `jumpForce`/`gravity` so max jump height (`v²/2g`) clears short/cover-height obstacles but not tall/boundary walls — this naturally tiers geometry into "climbable" vs. "permanent cover" without extra flags. NPCs/enemies that don't jump should stay on simpler height-blind collision (push out of every footprint regardless of height) — don't apply this model to them.

### Viewmodel attachments (hands, scopes, etc.) — anchor to the visible depth band

Anything attached to a screen-locked viewmodel (built in the camera's own local space, drawn via a `camWorld · local` transform like the gun above) is subject to the exact same perspective/near-clip math as the world — including going off-screen. It is easy to add hand/arm geometry anchored to a part of the gun that is itself off-screen (e.g. the grip or stock, which sit low enough in view space to be cropped below the visible frame — the same reason a real FPS buttstock is never seen) and end up with geometry that exists in the mesh but never renders.
**Why:** Bug confirmed in Desert Strike — player hands were anchored to the pistol grip/stock position and were completely invisible, because that anchor's y-coordinate combined with the viewmodel's hip-offset put it well outside the vertical field of view at that depth.
**How to apply:**
- Anchor new viewmodel attachments to the same (y, z) depth band as a part of the gun you already know renders on screen (e.g. the main barrel/body box), not the grip/stock.
- Roughly: a point stays on screen when `|localY + viewmodelOffY| < depth / (projScale)`, where `depth = |localZ + viewmodelOffZ|` and `projScale = 1/tan(fovRad/2)`. Near-camera points (small depth) have a much narrower visible Y range — don't anchor attachments to the closest/forward-most part of the gun.
- Offset new geometry sideways (X) more than vertically (Y) — X has more headroom because the projection divides it by aspect ratio as well.
- Keep added viewmodel geometry small and close to the gun's own footprint (comparable half-extents to the gun body's own boxes). The fix above (correct depth band) makes attachments visible, but oversized boxes at close range still balloon into huge screen-filling slabs under perspective — near-camera geometry grows apparent size fast, so err on the side of small.

### Sky / large gradient surfaces — per-vertex color, not stacked bands

To fake a sky gradient without a texture, don't stack several flat-colored quads at increasing height — the seam between each flat band is clearly visible as a hard line. Instead build one quad per side spanning the full height and give its bottom-edge and top-edge vertices different colors; `vColor` is a varying in the fragment shader, so the GPU interpolates it smoothly across the quad for a seamless gradient.
**Why:** Bug confirmed in Desert Strike — a 5-band stacked-quad sky produced visible horizontal stripes across the whole sky.
**How to apply:** Add a quad helper that takes one color per vertex (`addQuadG(b, p0,p1,p2,p3, n, c0,c1,c2,c3)`) instead of one color for the whole quad, and use it for any large gradient surface (sky, distant terrain fade, water).

### Close-range hit detection — multi-sphere, not single-sphere

A single hit-sphere at an enemy's torso center requires an increasingly steep downward aim angle as the player closes distance, so close-range shots miss in practice even when visually on target. Test 3 stacked spheres per enemy (legs / torso / head), take the nearest hit along the ray — this also gives you head-part identification for free if the game wants headshots.

---

## 11. Additional confirmed patterns

### Third-person follow camera (raw WebGL) — `mat4LookAt`, not yaw/pitch Euler

For a camera that always looks at a computed world point (third-person follow — a ball, a car from
behind — not free-look), build the view matrix directly with a standard right-handed `lookAt`
instead of composing `T·Ry·Rx` rotations to match the same target:

```js
function mat4LookAt(eye, target, up) {
  const z = norm3([eye[0]-target[0], eye[1]-target[1], eye[2]-target[2]]); // camera +Z toward eye
  const x = norm3(cross3(up, z));
  const y = cross3(z, x);
  return new Float32Array([x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0,
    -dot3(x,eye), -dot3(y,eye), -dot3(z,eye), 1]); // column-major, matches mat4Translate layout
}
```

Confirmed working in Roll Rush (third-person ball-rolling runner) — simpler than re-deriving
yaw/pitch from a target vector every frame. Keep the yaw/pitch approach (§10) for first-person/
free-look cameras only; use `lookAt` whenever the target point is already known each frame.

**Sign trap for forward=+Z cameras:** if the player moves toward increasing world Z (forward = +Z,
typical for an endless runner), world **+X renders on screen-LEFT, not screen-right** — the reverse
of the OpenGL-default forward=-Z intuition. This was shipped backwards twice in this project (once
per game, because it was re-derived from scratch each time instead of checked against this note).
Before wiring left/right input for any forward=+Z camera, either re-derive
`cross(up, normalize(eye-target))` by hand or verify via a Playwright screenshot that pressing the
key moves the rendered object to the intended screen side — don't trust "it looks right in the
matrix code" alone.

### Low-poly UV-sphere builder (raw WebGL) — flat-shaded, matches this repo's box aesthetic

This repo's WebGL games are box-based (`addBox`) with flat per-quad shading. For a rolling ball,
orb, or any round object, build a UV-sphere as one `addQuad` per lat/long facet — flat single
normal per quad (average of the 4 corners' outward directions, normalized), not smooth per-vertex
normals. ~8-10 rings × 12-16 segments reads as an intentional low-poly faceted sphere consistent
with the rest of the scene, and reuses the existing mesh-builder primitives with no new shader.

### Drag-to-aim (slingshot) input — launcher must sit far enough from the screen edge

For any pull-back/slingshot aim input (Angry Birds-style: drag away from a fixed point, release to
fire), the launcher's distance from the nearest edge in the pull direction must be **≥ `maxDrag`**,
or steep/powerful shots become physically undraggable (the required drag point falls outside the
canvas). Confirmed in Vault Breaker: a launcher 80px from the left edge with `maxDrag: 200` made
several level targets mathematically unreachable at any drag point, especially on portrait phones
with little letterboxing margin.
- Position the launcher at `maxDrag + margin` from every edge the player might need to pull toward.
- Clamp the live aim point to logical playfield bounds in pointer handlers.
- Before finalizing level target positions, brute-force verify reachability in Node against the
  actual `CFG` constants (grid-search candidate drag points, simulate the real launch formula,
  check some point lands within the target radius) — don't hand-derive "optimal" trajectories by
  algebra alone.
- Call `canvas.setPointerCapture(e.pointerId)` on pointerdown — without it, a fast drag leaving the
  canvas bounds stops receiving `pointermove`/`pointerup`.

### Endless-runner procedural spawning — guarantee at least one clear lane

For any endless runner that procedurally spawns lane-based obstacles, cap how many lanes can be
filled at any single spawn slot to `numLanes - 1`, never all of them. If every lane can be blocked
simultaneously, a run can be unfairly unwinnable regardless of skill. Use a seeded Fisher-Yates
shuffle of lane indices sliced to the capped count — this makes every slot solvable by construction,
no reachability solver needed.

### Ballistic/physics floors — derive from flight time, not a fixed worst-case constant

For any projectile parameter with a "minimum safe value" that depends on flight time (loft, spin,
arc height), a fixed floor calibrated to the *worst case* (slowest/longest flight) over-constrains
every other case too. Confirmed in Golden Boot: a fixed `minLoftSpeed` safe for the slowest shot was
silently applied to fast/flat drags as well, forcing all of them into the same high arc — user
reported "drag downward does nothing," which was technically false but experientially true since
the floor swamped the real input signal. Derive the floor as a function of that specific shot's own
flight time (closed-form projectile equation solved for "just barely stays above ground"), and keep
a single constant only as a `Math.max(...)` fallback for edge cases.

### A real human playtest finds bugs batch simulation cannot

Automated Playwright driving (real code paths, N-shot randomized balance simulations) is necessary
but not sufficient. In Golden Boot it missed 4 real bugs a human playtest found immediately: a
physics bug that looked like a hardcoded score ceiling, the ballistic-floor bug above, a broken
decorative mesh, and a ball left spinning during the aiming phase (no one had written an assertion
for "the ball should not be spinning here" because no one thought to). Random/typical-input testing
under-samples the specific, deliberate things a human tries. Ship automated-test-passing work, but
always flag to the user that feel/balance/idle-animation-correctness still needs a real playthrough
before calling something final.
