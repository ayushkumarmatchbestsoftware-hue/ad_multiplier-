# Game Design Framework

> **Read order:** This is the FIRST reference of any game run. Read it whole,
> top to bottom, before all other references and before any code. The closing
> reference is `build-game.md` — read it last, after this file and after all
> asset references, but before writing any game code.

---

## 0. Execution rules

1. Planning lives in reasoning. The only written planning artifact is `design/assets.csv`.
2. Each phase closes with its gate — an unmet item blocks the move.
3. The game tolerates hostile input: spam, conflicting keys held simultaneously, losing focus mid-action, rapid restarts.
4. Numbers before code. Any budget, limit, or threshold is fixed before building the thing it judges.
5. No game code until PLAN closes (profile thought through, `assets.csv` written, STYLE FORMULA set).

---

## 1. Game profile

Think through one point on every axis — the profile shapes every decision below:

| Axis | Spectrum |
|---|---|
| **Time** | real-time ↔ turn-based ↔ pause-at-will ↔ no time |
| **Space** | continuous 2D ↔ grid ↔ abstract ↔ absent |
| **Agency** | one character ↔ squad ↔ disembodied hand (cursor/card player) |
| **Conflict** | vs system ↔ vs players ↔ vs self ↔ none |
| **Content** | authored ↔ procedural ↔ emergent |
| **Outcome** | win/lose ↔ endless ↔ score-chase ↔ player-set goals |
| **Players** | solo ↔ local co-op ↔ local versus |
| **Session** | under 2 min ↔ 5–15 min ↔ open-ended |
| **Engagement** | execution ↔ calculation ↔ discovery ↔ expression ↔ accumulation |

**Delivery context (fixed at planning, never retrofitted):**
- Default target: desktop + mobile (touch + keyboard)
- All controls must work on both — no hover-only interactions
- Keyboard bindings use `event.code` (physical keys), never `event.key`
- Performance budget targets the weakest platform (mobile)

---

## 2. Laws

- **L1 — Experience first.** Design the experience, not the artifact. Every decision resolves against the experience formula (§3.1).
- **L2 — Meaningful interaction.** Every player action must produce a visible effect now and an echo later. A slot with no effect = a dead mechanic.
- **L3 — Mastery.** One new pattern at a time; the next pattern comes after the previous one's exam. Design the sequence of mastery, not the volume of content.
- **L4 — Undecided outcome.** Keep 2–3 uncertainty sources active (execution, randomness, hidden info). When one runs dry, another must already be active.

---

## 3. Concept

- **3.1 Experience formula** — one sentence: "The player feels ___ because the game constantly ___." No genre labels. Every later decision points back to this.
- **3.2 Four pillars** — mechanics, story (even abstract games have a tension arc), aesthetics, technology. Each must reinforce the other three.
- **3.3 Formal elements** — players, goals, actions, rules, resources, conflict, boundaries, outcome.
- **3.4 Interest curve** — hook immediately, alternate peaks and breathers, peak near the end.

---

## 4. System

- **4.1 Verbs.** Few strong verbs over many weak ones. A strong verb means several object types respond differently to it.
- **4.2 Loops.** Sign every feedback loop. Positive loops snowball (cap them). Negative loops dampen skill (guard against them). A comeback must be possible; good play must still win.
- **4.3 Information.** Decide what the player can see. Hidden information that affects the outcome needs a discoverable trail — hidden-with-no-trail reads as unfair.

---

## 5. Walkthrough — seven subsystems

Answer each or justify its absence:

1. **Representation** — camera, layout, text order. Never hides information needed for the current decision.
2. **Input** — cheapest gestures on most frequent actions. Conflicting simultaneous inputs resolve predictably.
3. **Agency metrics** — jump length, move speed, options on screen. Frozen before asset production; changing them later breaks everything built on them.
4. **Resistance × verb matrix** — every source of resistance is answered by some verb. Unanswerable resistance = frustration.
5. **Peaks** — each period ends in a combined exam of what it taught.
6. **Rewards** — feed the declared engagement source. The strongest reward is a new verb or mechanic. Every game needs a reason to replay — implement **all three tiers**:
   - **Immediate** — visible feedback within 1 frame (score pop, flash, sound cue). Dead silence = dead mechanic.
   - **Session** — end-of-run summary: final position or score, star rating (1–3 stars), time. Stars are based on *how well*, not just *that* the player finished.
   - **Persistent** — personal best stored in `localStorage` and shown on the start screen. Players must be able to see what they're beating.
   - Star/medal thresholds live in `CFG` (e.g. `medalThresholds: { gold: 60, silver: 75, bronze: 90 }`). Never hardcode them inline.
   - Never reward only completion — reward the quality of play.
7. **Game entry** — short counted path from launch to first meaningful action. Controls learnable on demand at any moment.

---

## 6. Code rules

Game code starts only after PLAN closes, from the `build-game.md` skeleton — never from scratch.

- **Architecture:** decouple what will change; don't abstract what won't.
- **Performance law:** 60 fps target on mobile. Set the frame budget before code, not after. Same-type entities batch into one draw call. Zero allocations inside the frame loop.
- **Dev overlay:** FPS + frame time toggled by `?dev=1` query flag. Ships disabled. Numbers come from it — never guess.
- **Fixed-timestep loop:** game logic never depends on frame rate.
- **Input as command objects:** input is converted to commands before entering the game loop. Supports keyboard, touch, and gamepad simultaneously.

---

## 7. Balance

- Options live on one cost curve — above it is dominant, below is useless.
- Perceived difficulty = gap between player-power curve and challenge curve.
- All balance numbers live in a config object at the top of the JS — tuned one change at a time.
- Randomness: tune expected value AND variance separately. Streaks read as cheating — soften them.

---

## 8. Player's head

- Critical signals use multiple channels (color + shape + sound) — nothing relies on color alone.
- Nothing instructional under load.
- Current goal visible at any return to the game.
- Error cost = repeat the interesting part, not the boring part.

---

## 9. Limits

Perceptual quality — animation feel, juiciness, audio polish — needs human eyes and hands the pipeline cannot fully replace. Say so at delivery rather than overclaiming.

Close gate before moving to `build-game.md`: profile set, laws applied, experience formula written, `assets.csv` complete, STYLE FORMULA approved.
