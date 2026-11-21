# sim.md: build a local Mech Royale simulator (brief for a coding agent)

You are building a **local, lockstep match simulator** for the `mechfight/1` bot game so this
team can evaluate bot changes in minutes instead of waiting on rate-limited practice submissions
(4/hour, ~15 min each). Read this whole file, then `AGENTS.md` (repo root, local only: project rules
and shared memory; add yourself to §4 "Active work" before starting and log findings in §5–§7),
then `kit/protocol.md` (the complete rules). Do **not** modify `bot/` or `kit/`.

Deadline context: the competition cutoff is **2026-11-22 16:00 UTC**. A useful, calibrated sim in
~5–6 hours is worth far more than a perfect one later. Build in the order of §6 and keep it runnable
at every step.

## 1. Why it is needed (what the sim must reproduce)

Scored **practice** = 48 matches of our bot vs **15 house bots acting as one squad**: they share
vision, never fight each other, and hunt us weakest-first ("one early, more as you thin them out,
all of them late"). House bots mostly die only to us (or the zone), so **practice points ≈ house bots
dead before we die**. Score = mean(16 − place).

Known practice results (the calibration targets, §5):

| version | git ref | how to run | practice score |
|---|---|---|---|
| v0 | tag `v0-3.25` (`bot/policy.py` at that tag, class `BasePolicy`) | `git worktree add /tmp/v0 v0-3.25` then `python3 -u /tmp/v0/bot/main.py` | **3.25** |
| v1.1 | `6fd8444` | worktree, `bot/main.py` | **1.77** |
| v1.2 | `ddd5e45` | worktree, `bot/main.py` | **2.98** |
| v1.3 | `d57e515` | worktree, `bot/main.py` | pending (ask in AGENTS.md §9) |

Leaderboard: #1 ≈ 5.96, top-32 cutoff ≈ 5.2.

Live free-for-all results (play server) do **not** predict practice: v1 scored 11.0 live but 1.77 in
practice. Live rooms are also real-time over the internet (our actions arrive late); scored matches
are lockstep. The sim must be **lockstep**.

## 2. Interface: run our real bot unchanged

- The sim is the server. It launches each external bot as a subprocess (`cmd` list), speaks the exact
  protocol from `kit/protocol.md` §2/§6 over stdin/stdout: `hello` → `ready` → per decision `obs` →
  action → … → `end`. Validate against `kit/mechfight.schema.json` (reuse the validator class in
  `kit/check/check.py`).
- **Lockstep**: send obs, wait for the answer up to `tick_budget_ms` (50 ms; allow a `--no-timeout`
  debug flag), apply, simulate `decide_every` = 2 ticks at 20 Hz. No answer → coast rules (§2 of
  protocol: keep move/boost/aim, drop one-shots; idle after `coast_s`).
- `hello.config`: copy the full config from the recorded hello in `kit/check/stream.jsonl.gz`
  (first line). That is the real rule set (weapons, mech, vision, sound, zone phases, loot, npc,
  drops, upgrades, debug caps). `hello.map.dir` = absolute path of `kit/arena`.
- Our bot is one mech (slot random per match from the match seed). The other 15 are **in-process
  Python house bots** (§4), no subprocess, so a match runs fast.
- Seeds: `--seeds 1..48` fixed, so A/B comparisons of two bots use identical spawns/loot/zone.

## 3. World and rules to implement (priority order)

Units m/s/rad; x east, y north, z up; yaw from +x CCW (protocol §5).

**Map** (`kit/arena/v2/world.json`, `terrain.i16`; protocol §11): terrain bilinear heights; 259
prisms (`building`, `stadium`, `bridge`: polygon `ring`, `base`, `top`); trees (vision only:
canopy soft, trunks block sight; never block shots). `bot/world.py` already has a fast prism
index, `blocked(a, b)` segment test, `solid_at`, `ground()`; copy or import it (don't edit it).

**Movement** (config `mech`): accelerate toward commanded horizontal velocity (`ground_accel` 58.5 /
`air_accel` 12), `walk_speed` 11.7, `boost_speed` 19.5 (drains `boost_drain`/s), gravity 22, jump
`jump_speed` 11 if grounded, jets in air (`jet_accel` 34, `max_rise_speed` 10, `jet_drain`), energy
100 with regen 18/s after 0.6 s. Mech = cylinder r 2.5, h 9. Collisions: slide along prism walls
(project out of polygons, keep the tangential part); `step_up` 1.2 m; slopes > 50° block; stand on
prism tops (roofs) when feet are above `top`. Approximate eaves/roof-edge rules; don't sink time.
Keep inside `bounds` with `match.arena_margin`. Push overlapping mechs apart.

**Aim**: torso slews toward commanded aim at `turn_rate` 4 rad/s yaw and `pitch_rate` 3, pitch
clamped to `[min_pitch, max_pitch]`. Shots leave along the **actual** torso aim from muzzle
(`pos.z + muzzle_height`), origin 3 m out (less if a wall is closer).

**Weapons** (config `weapons`):
- gun: projectile 252 m/s straight, range 230, 45 dmg, cooldown 0.24 s, mag 12, reload 2.4 s,
  Gaussian spread `spread_deg` 0.6; projectile radius `projectile.gun_radius` 0.3; hits mech
  cylinders, NPCs, crates, prisms, terrain. Sub-step projectiles per tick (252 m/s = 12.6 m/tick).
- laser: hold to charge (up to 0.75 s), release fires hitscan along aim (range 280); damage
  `min_frac`→`damage` 315 linear in charge; auto-fire after `hold_s` at full; cooldown 1.65; half move
  speed while charging; `charging: true` visible to others; cook-off +60% vs mortar holders.
- lightning: instant, nearest target within 45 m and ±15° (cone 30°) of aim yaw with LOS, 60 dmg,
  chains 2 more within 14 m at ×0.7, costs 3 energy, cooldown 0.25; short: cancels a laser charge
  and blocks charging for 2 s.
- mortar: may be omitted in v1 of the sim (house bots rarely use it: ~2% of damage). If time allows:
  72 m/s ballistic, g 22, 200 direct + 160 splash (7 m, 30% edge), knockback, energy drain.
- Damage modifiers: rear hits (outside the 180° front arc of the target's aim yaw) ×1.5; cubes
  ×(1 + 0.04·min(cubes, 10)); armor upgrades reduce weapon damage.

**Health**: 1400 HP, regen 45/s after 5 s without dealing or taking damage (not outside the zone).

**Zone**: config `zone` phases (start r 560 @ (41.3, −72.8); hold/shrink schedule; dps per phase);
next circle placed randomly inside the current one (seeded). `obs.zone` fields exactly as protocol.

**Vision** (config `vision`): see a mech if the eye (`pos.z+8`) has LOS (prisms block; trees: trunks
block, crowns soft) to its centre or top, and either within `near_range` 45 m any direction, or
within `range` 200 m inside the 120° yaw wedge of the torso aim. Keep tracking seen mechs/NPCs for
`memory_s` 5 s. Projectiles/crates/cubes/pickups only while visible.

**Sound** (config `sound`): emit gun/laser/laser_charge/lightning/jet/boost/step/reload/death/crate
sounds with carry ranges; heard ones go to `obs.sounds` with bearing noise σ 7° and near/mid/far.

**Events** in `obs.events`: `damaged` (amount, weapon, bearing, by, rear), `hit`, `kill`, `pickup`,
`took`, `dropped`, `take_failed`, `upgrade`, `effect`.

**Second tier** (after calibration passes, or if a calibration miss points at them): NPC drones (12,
350 HP, leash 30 m, chase 60 m, 25 dmg bullets 90 m/s, 2 cubes each), crates (36, 300 HP, 1 cube,
50% weapon), floor loot (40 pickups; `take`/`drop` rules), cubes (+4% HP/dmg, size +5%, upgrade
offers at thresholds; apply `config.upgrades` effects), supply drops. Until then: every mech spawns
with gun in slot 0; house bots get a random second weapon (laser 50%, lightning 30%, mortar or
none 20%); our bot gets whatever floor loot exists near spawn (implement pickups early if cheap).

## 4. House-bot squad (the part that decides calibration)

Behaviour measured from 37 real replays of the play-server house bots (`logs/opponents-b1.md`,
`logs/winners.md`, `scripts/opponents.py`). Implement one parameterised bot class; difficulty
(easy/medium/hard/expert: practice has 4/4/4/3) scales aim noise, reaction time and aggression.

**Squad logic (practice):**
- Shared vision: every house bot knows what any house bot currently sees (and the 5 s memory).
- They never target or damage each other (friendly fire off for them).
- Hunt schedule: at start **1 hunter** chases us; when a hunter dies, add more ("more as you thin
  them out"); in the final phase (e.g. alive ≤ 6, or zone phase ≥ 4) **all** hunt. Hunters pick
  "weakest first": since we are the only target, this is about how many come. Make the schedule a
  parameter (e.g. hunters = 1 + 2·kills_by_us, all after phase 4) and tune it in calibration.
- Non-hunters: wander inside the next zone circle, loot, break crates, fight drones; they still
  shoot us if we come into their (shared) view within ~100 m.

**Per-bot combat behaviour (measured):**
- Always moving (95–98% of the time), ~12 m/s, boosting 22–30% of the time.
- Engage range: gun fights at median 55–72 m; close slowly when healthy (−1.3 to −4 m/s radial),
  back off when HP < 40% (+0.6 to +1.1 m/s).
- Gun hit rate vs mechs (in-process, no lag): expert/red ≈ 45%, astra ≈ 20% (astra fires 3× more).
  By range (red-best): 0–25 m 81%, 25–50 70%, 50–75 50%, 75–100 43%, 100–125 32%, 125–150 21%.
  Implement as aim error (lead with Gaussian noise) rather than dice rolls, tuned to these rates.
- Laser: release at **exactly 0.8 s** of charge every time; laser shots hit 17–29%.
- Lightning used at 32–38 m. Mortar at 43–87 m, rarely.
- Strafe perpendicular while firing; dual fire (gun + second weapon on the same decision).
- Faces the nearest mech 44–61% of the time (they scan/turn), so they don't see everything.
- Upgrades: plating and armor first.
- Zone: stay well inside (median 150–255 m from the edge in live rooms).

## 5. Calibration (definition of done)

Run each bot version in §1 on the same 48 seeds. The sim is **trusted** when:
1. Ranking matches practice: v0 > v1.2 > v1.1 (and v1.3 vs v0 once its practice score arrives).
2. Absolute scores within ~±0.8 of practice (v0 3.25, v1.1 1.77, v1.2 2.98).
3. Mechanism sanity: the kit example bot (`kit/python/example_bot.py`, practice baseline **1.15**)
   scores ~1.15 ± 0.6.
Tune only §4 squad parameters (hunter schedule, aim noise, aggression) to hit these, and record
every calibration run (params → scores) in AGENTS.md §7.

## 6. Build order

1. `sim/engine.py`: world state, movement with prism collision, aim slewing, zone, HP/regen. Unit
   test: a mech walking into a building slides; can't walk up a wall; regen timing.
2. `sim/server.py`: protocol to an external bot subprocess (lockstep, budget, coast/idle, newest-obs
   is trivially satisfied), obs builder (vision with LOS + memory, events, zone, self.weapons stats).
   Test: `python3 -u kit/python/example_bot.py` plays a full match without protocol errors.
3. Gun + laser + lightning + rear hits + cube scaling. Test: two scripted mechs duel; damage numbers
   match config.
4. House bots + squad (§4). Test: they converge on our mech; they don't shoot each other.
5. `sim/run.py`: `python3 sim/run.py --seeds 1-48 --jobs 20 -- <bot cmd...>` runs matches in
   parallel (multiprocessing, one bot subprocess per match) and prints mean(16 − place), places,
   median survival, our kills, damage dealt/taken by weapon, laser share of damage taken, time
   stationary, deaths < 60 s. `--ab <cmdA> --ab <cmdB>` runs both on the same seeds and reports the
   paired difference with a bootstrap 95% CI.
6. Replay output: write `logs/sim/replay-<seed>.jsonl` in the official `mechreplay/1` format
   (header + frames + events; see any `logs/replay-*.jsonl` for the exact schema) so
   `python3 scripts/view.py logs/sim/replay-<seed>.jsonl` shows it in the official 3D viewer, and
   so `scripts/report.py` / `scripts/opponents.py` work on sim output.
7. Calibration (§5), then second-tier features if a miss points at them.

Performance target: one 480 s match (9600 ticks, 16 mechs) in ≤ 60 s of CPU, so 48 seeds ≈ 3 min
on 20 cores. Precompute map grids at start; avoid per-tick O(n²·prisms) LOS (cache vision every
decision, i.e. every 2 ticks, and use the prism grid index).

## 7. Deliverables and etiquette

- Code in `sim/` (+ `sim/README.md` with commands). Standard library only (numpy optional if
  installed; check `python3 -c "import numpy"`). Commit in small steps with clear messages, push to
  `main` (never force-push; `git pull --rebase` first). Don't touch `bot/`, `kit/`, `AGENTS.md`
  rules sections; append findings to AGENTS.md §5–§7 and your status to §4.
- Never commit `AGENTS.md`, `CLAUDE.md`, `logs/`.
- When calibrated, post the table "version → sim score vs practice score" to AGENTS.md §5.
