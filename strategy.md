# Warden: the full strategy and state machine (Go bot, `gobot/`)

This document describes exactly what the image runs: the Go bot in `gobot/` (built by the top-level
`Dockerfile`, entry `/bot/warden`). Every number below is the default in `gobot/params.go` or a constant in
`gobot/warden.go`, `gobot/land.go` or `gobot/route.go`. Any profile or global number can be overridden for
experiments with the `WD_P` environment variable, e.g. `WD_P="g.back_s=1.2,hunt.finish_r=14"` (`g.` = global,
`scout.` / `hunt.` / `guard.` = a phase profile). The older Python bots live in `bot/` and are not used by the image.

| File | Contents |
| --- | --- |
| `gobot/proto.go` | mechfight/1 message types, the stdin/stdout loop, error handling |
| `gobot/params.go` | phase profiles, global switches, upgrade orders, weapon-slot values, `WD_P` parsing |
| `gobot/warden.go` | memory, threat picture, back-off rules, zone timing, mode selection, movement, fire control, loot |
| `gobot/land.go` | world model: terrain, building prisms, height layers, solids, line of fire |
| `gobot/route.go` | 2 m route grid, wall margin, walkable patches, A* routing, pocket breakout, openness |
| `gobot/omap.go` | insertion-ordered map (stable iteration, so ties always break the same way) |
| `gobot/probe.go` | developer tool only (`warden probe <map dir>`): answers world-model queries on stdin |

---

## 1. The big picture

Practice matches put us against 15 house bots that act as one squad, share vision, never fight each other and
hunt us. Our place is the number of mechs still alive when we die, so points come from killing hunters before
they kill us. The bot plays in three phases:

1. **Scout (first 20 s):** careful. Fights only what is close, hurting us, lining up a laser on us or nearly
   dead. Otherwise it waits quietly at a sheltered post inside the next safe circle and peeks from cover edges.
2. **Hunt (after 20 s, for the rest of the match):** aggressive. Fights anything that threatens us or is weak,
   focuses hard on wounded targets, backs off only in short bursts while losing, closes in to finish, grabs
   cubes mid-fight, seeks lasers from far away, picks laser-first upgrades.
3. **Guard (permanent, once triggered):** if a death we did not cause is seen for 5 decisions in a row, the
   match is not a pure squad hunt (mechs are killing each other). The bot switches for good to the cautious
   Scout-style profile, with crate and drone farming limited to the first 150 s.

---

## 2. Phase profiles

Phase each decision: `guard` once Guard has triggered, else `hunt` if `t > scout_s` (20 s), else `scout`. When
the phase changes, its profile is copied in fresh.

| Key | Scout | Hunt | Guard | Meaning |
| --- | --- | --- | --- | --- |
| `fight_close` | 60 | 60 | 60 | a mech closer than this (m) is always fought |
| `max_range` | 215 | 240 | 215 | farthest target considered (m) |
| `hp_weight` | 60/1400 | 0.1 | 60/1400 | target score += weight × target HP |
| `wounded_bonus` | 0 | 200 | 0 | target score −= this if target HP ≤ 30 % |
| `keep_bonus` | 0 | 200 | 0 | target score −= this for the target kept within the last 2 s |
| `pull_hp` | 0.38 | 0.38 | 0.38 | back off below this HP fraction (with danger in the last 2 s) |
| `steady_hp` | 0.8 | 0.8 | 0.8 | stop backing off above this HP fraction |
| `pref_gun` | 85 | 85 | 85 | preferred fight range with the gun only (m) |
| `pref_light` | 28 | 28 | 28 | preferred range with lightning (energy > 12) |
| `pref_laser` | 85 | 120 | 85 | preferred range with a laser |
| `loot_r` | 90 | 150 | 90 | loot radius for pickups, crates, drones (cubes: max(60, 0.7 × `loot_r`)) |
| `laser_r` | 90 | 240 | 90 | a laser pickup is sought from this far when larger than `loot_r` |
| `farm_hp` | 0.7 | 0.5 | 0.7 | farm crates/drones only above this HP fraction |
| `cube_fight_r` | 0 | 70 | 0 | grab cubes within this radius in the middle of a fight |
| `finish_r` | 0 | 20 | 0 | close to this range to finish a wounded target |
| `laser_ups` | no | yes | no | laser-first upgrade order |
| `peek` | yes | no | yes | edge-of-cover peeking; also fight a hidden engaged mech |
| `lethal` | no | yes | no | release a part-charged laser early if it kills |
| `crate_until` | none | none | 150 s | no crate/drone farming after this match time |
| `laser_first` | yes | yes | yes | a laser outranks lightning for the second slot |
| `speed_ups` | no | no | no | speed-first upgrade order (experiment switch) |

Global switches (`g.*`):

| Key | Default | Meaning |
| --- | --- | --- |
| `scout_s` | 20 | Scout length (s) |
| `guard` | 1 | Guard trigger enabled |
| `others_dead_ticks` | 5 | consecutive decisions with an unexplained death that trigger Guard |
| `back_s` | 1.5 | Hunt back-off burst length (s) after the last "losing" decision |
| `lose_two_hp` | 0.55 | losing if ≥ 2 mechs within 160 m and our HP fraction below this |
| `lose_three_hp` | 0.8 | losing if ≥ 3 mechs within 160 m and our HP fraction below this |
| `lose_low_hp` | 0.35 | losing if our HP fraction is below this and the target's is higher by > 0.1 |
| `closing_v`, `closing_r` | 3, 180 | Hunt: a mech approaching faster than 3 m/s within 180 m is hot |
| `weak_frac`, `weak_r` | 0.4, 160 | Hunt: a mech below 40 % HP within 160 m is hot |
| `aimed_r` | 190 | Hunt: a mech aiming at us within 190 m is hot |
| `struck_s` | 6 | Hunt: a mech that hit us within 6 s is hot |
| `lethal` | 1 | master switch for the early lethal laser release |
| `sidestep` | 1 | bullet and laser sidestep dodges |
| `drops` | 0 | supply drops ignored |
| `drone_farm`, `crate_farm` | 1, 1 | farm drones and crates for cubes |
| `cube_v` | 75 | loot value of a cube (25 once at the cube stat cap) |
| `esc_guard` | 0 | if 1: a pocket escape whose walk to the takeoff makes no progress for 2 s is given up for 5 s (waiting for energy and flying are not counted) |
| `lethal_k` | 0.8 | early laser release when this share of the partial-charge damage kills |

---

## 3. Per-decision pipeline

For every observation answered (only the newest one when several are queued):

1. **Memory** (section 4): mechs, drones, items, events, Guard trigger, phase, held zone circle.
2. **Upgrade pick** if an offer is open (section 12).
3. **Threat picture**: target, visible list, danger, crowd, hidden foe (section 5).
4. **Back-off state** (section 6).
5. **Zone timing** (section 7).
6. **Mode selection** (section 8; the first matching rule wins).
7. **Sidestep dodge** blend, then **stuck detection** (section 10).
8. **Fire control** and aim (section 11), then the laser pre-charge against a hidden foe.
9. **Reload** decision (section 11).
10. **Weapon pickup** in reach (section 13).
11. The action is sent with move and aim at **full precision**. Rounding them changes the simulated motion
    slightly, and over a match that grows into different outcomes. A debug label `phase:mode hpX cY tID`
    is attached (team-only, no effect on play).

---

## 4. Memory

- **Mechs**: every mech in view is stored with the time seen (insertion-ordered). A mech not seen for 20 s is
  forgotten, together with whether it hit us and its approach speed. A mech we kill is forgotten at once.
- **Who hit us**: a `damaged` event naming a shooter records the time, but only for a mech we already know.
  For a drone we already know, it records a drone hit.
- **Being hurt** (time and bearing): only `damaged` events that name a shooter count. The bearing is kept from
  the last such event (0 before the first one).
- **Drone hits**: `damaged` events from weapon `npc` with a bearing (time and bearing).
- **Drones**: every drone in view is stored with the line of fire and distance measured at that moment (from
  the plain muzzle: our position + muzzle height). Forgotten after 120 s unseen.
- **Items** (crates, cubes, weapon pickups): stored by id with position and time. An item not in view is
  deleted if it is within 40 m, was last seen more than 0.35 s ago, and our eye (position + eye height) has a
  clear line to 1.5 m above it: it should be visible, so it is gone.
- **Guard trigger**: (mechs alive at the first observation − mechs alive now − our kills) > 0 for 5 decisions in
  a row → Guard for the rest of the match.
- **Held zone circle**: when a new `shrink_start` appears, or while `t ≤ shrink_start`, the observed circle is
  stored as the start circle of the current shrink (the observed circle itself moves while it shrinks).

---

## 5. Threat picture and target choice

The visible list holds every remembered mech seen within the last 2.5 s. A mech not seen this decision keeps
its last position and distance but is neither hot nor shootable. For each mech seen now:

- `d` = horizontal distance.
- **Shootable** = a clear line from the plain muzzle to its body centre (height × size scale × 0.5), or else to
  85 % of its height.
- **Aimed** = its aim yaw is within 0.22 rad of the direction to us.
- **Approach speed** = −(relative velocity · direction to it) / d, measured only when d > 1 m (closer than that,
  the last measured value stands).
- **Struck** = seconds since it hit us.

### When a mech is "hot" (worth fighting)

- **Hunt**: d < 60, or struck < 6 s, or endgame (next radius ≤ 70 m or ≤ 3 mechs alive), or approaching faster
  than 3 m/s within 180 m, or HP < 40 % within 160 m, or aimed at us within 190 m.
- **Scout / Guard**: d < 60, or struck < 4 s, or aimed at us while charging a laser within 120 m, or HP < 350
  within 120 m, or ≤ 2 mechs alive.

A hot mech that is **not** shootable and is within `max_range` is a candidate **hidden foe** (the nearest wins).

### Target score (lowest wins; hot, shootable mechs within `max_range` only)

`score = d + hp_weight × HP − wounded_bonus (if HP ≤ 30 %) − keep_bonus (same target within 2 s) − 40 (hit us
within 4 s) − 15 (approaching faster than 3 m/s)`. The winner is kept as the current target for the next 2 s.

- **Foe** = the target; with no target and `peek` on (Scout/Guard), the hidden foe.
- **Danger** = any visible mech is hot or hit us within 4 s. **Crowd** = mechs seen now that are hot, or
  shootable within 150 m. Danger, or being hurt within 3 s, refreshes the danger time.
- **Finishing** = a target within 120 m with HP < 200 or HP below half of ours.

---

## 6. When to back off

Two rules run every decision, in this order:

1. **Crowd / low-HP rule** (all phases). *Swamped* = crowd ≥ 3, or crowd = 2 with our HP < 65 %, or *outgunned*
   (the target carries a laser, we do not, it is more than 55 m away and our HP < 80 %). Start backing off if
   HP < `pull_hp` with danger in the last 2 s, or if swamped; starting resets the cover search. Stop when not
   swamped and HP > `steady_hp`, or when there has been no danger for 7 s and HP > 55 %.
2. **Losing rule** (Hunt only; overrides rule 1). `near` = mechs seen now within 160 m. Losing if near ≥ 2 and
   HP < 55 %, or near ≥ 3 and HP < 80 %, or HP < 35 % while the target's HP fraction is higher by more than 0.1.
   Not losing if the target is below 25 % HP, we are above 20 % and near ≤ 2. Backing off lasts 1.5 s after the
   last losing decision.

---

## 7. Zone timing

- **Circle at time t**: moves linearly from the held start circle to the next circle across the shrink.
- **Circle now**: the observed circle if the shrink has not started or the observed circle already differs from
  the held one; otherwise the circle at t = now.
- **Safe point**: the nearest point inside (next radius − margin − 5), margin = clamp(0.3 × next radius, 3, 30);
  the next centre itself once the next radius ≤ 30; our own position if we are already inside.
- **Must leave by** (pad 8 m): the first of 25 evenly spaced times across the shrink at which the moving circle,
  shrunk by the pad, no longer covers our spot; never, if we are inside the next circle by the pad already.
- `need` = distance to the safe point × 1.45 / walk speed + 4 s.
- **Zone now** = outside the zone, or now + need + 3 ≥ must-leave-by. **Zone soon** = now + need + 14 ≥
  must-leave-by.
- **Zone cost of a spot** (used by fight steps, cover and loot): if outside the current circle or within 5 m
  of its edge, 150 + 5 × (overshoot + 5); if outside the next circle or within 5 m of its edge, add soon × (25 +
  0.8 × (overshoot + 5)), with soon = clamp(1 − (shrink start − now) / 25, 0.1, 1).

---

## 8. Mode selection (first matching rule wins)

| # | Mode | Condition | Movement / boost |
| --- | --- | --- | --- |
| 1 | `escape` | stranded in a walkable pocket cut off from the main area (section 9) | walk to the takeoff, wait for energy, jump and jet to the landing |
| 2 | `unstuck` | inside the 0.5 s unstuck window | the random unstuck direction |
| 3 | `shell` | a mortar shell will land within splash radius + our radius + 2 m of us in < 2.5 s | the first walkable of 5 directions away from the impact (0, ±0.7, ±1.4 rad, 6 m); boost if energy > 15 |
| 4 | `zone` | zone now, and the safe point is more than 2 m away | travel to the safe point; boost if outside or out of time, with energy > 5 |
| 5 | `cover` / `cornered` / `flee` | backing off, not finishing, not endgame | cover search; if none, fight step against the target wanting sight (`cornered`); if no target either, travel to the safe point, sidestepping across the bearing of a drone that hit us in the last 3 s (side flips every 1.5 s) when standing still |
| 6 | `hide` | Scout/Guard, no target, hurt within 2.5 s, not endgame | cover search against a ghost 80 m along the hurt bearing plus the visible mechs (falls through if no cover is found); boost if energy > 30 |
| 7 | `fight` / `fight-cube` | a foe exists | fight step (section 9); details below |
| 8 | `zone` | zone soon, and the safe point is more than 2 m away | travel to the safe point |
| 9 | `loot-*` | a loot pick exists (section 13) | drone: fight step if it was seen < 1 s ago, shootable and within 75 m (as measured when seen), else travel to it; crate: shoot from here if within 50 m with a clear plain-muzzle line (step straight back if within 12 m), else travel to it; other items: travel (stand on a supply drop within 2.5 m) |
| 10 | `zone-walk` | Hunt (or next radius ≤ 30), safe point more than 3 m away | travel to the safe point |
| 11 | `post` | Scout/Guard, post more than 3 m away | travel to the post; silent walk (speed × 4.6 / walk speed) with no danger and no hurt for 6 s |
| 12 | `idle` | nothing else | stand |

**Fight details (rule 7):**

- **Armed** = a gun with ammo, reload ≤ 0.25 s and the foe within gun range − 10; or a nearly charged laser
  (charge ≥ full − 0.3 s, foe within laser range − 5); or lightning with the foe within its range + 6 and energy
  for 3 shots. In Hunt (peek off), holding lightning or a laser always counts as armed.
- **Sight wish** = +1 if armed, else −1 (stay out of its sight while reloading). With `peek` on and no hot laser:
  −1.6 if any mech seen now is charging a laser aimed within 0.3 rad of us.
- **Finisher**: with `finish_r` set, foe HP < 38 %, our HP > 42 % and crowd ≤ 2, the fight step uses `finish_r`
  in place of every preferred range.
- **Boost** if holding lightning, the foe is more than 50 m away, energy > 55 and sight is wanted.
- **Mid-fight cubes** (`cube_fight_r`, × 0.57 at ≤ 50 % HP): travel to the nearest remembered cube within it.

---

## 9. Movement pieces

### Preferred range

Drones: 58 m. Mechs: lightning with energy > 12 → `pref_light`; else a laser → `pref_laser`; else `pref_gun`.
Without lightning: at least 64 m against a lightning carrier and at least 80 m against a mortar carrier.

### Fight step (where to move this decision)

Candidates: stay, plus 16 directions at 6 m (and, with `peek` on, a second ring at 12 m). A ring spot is skipped
if the straight walk there is not walkable or its route cell is sealed; a skipped inner-ring spot within 0.9 of
the strafe direction flips the strafe side for 0.5 s. Score (highest wins):

- −1.1 × |distance to the foe − preferred range| − 0.4 × step length;
- +45 × sight wish if the spot (at muzzle height) has a line to the foe's body centre;
- `peek`, inner ring, line state equals the wish: +8 if one of 8 open spots 7 m away flips the line (cover edge);
- −30 for every other visible mech within 170 m whose eye sees the spot at 60 % of body height;
- − zone cost; +1.5 × (distance gained toward a remembered cube within 40 m and 4 m in height);
- wall margin below 4 m: −2.5 × (4 − margin); sight wanted: +9 × (alignment with the strafe direction);
- staying: −8, and +14 if sight is not wanted and the spot is hidden.

The strafe side flips with probability 0.6 every 0.3–1.0 s (seeded random generator).

### Cover search

Threats = visible mechs within 230 m (the nearest 4). A spot is hidden if no threat's eye sees it at full or
half body height. The result is reused for 0.4 s. If our own spot is hidden and has zero zone cost, stay.
Otherwise search the route grid breadth-first (8-neighbour steps, step-up limits, no corner cutting) up to 50
steps and 350 evaluated cells (every other cell, checkerboard): score = 2 × steps + zone cost − 0.25 × min(
distance to the nearest threat, 80); stop once 2 × steps exceeds the best score + 30. Travel to the best cell
(stand within 1.5 m of it).

### Post (Scout/Guard waiting spot)

Re-chosen when there is none, when it lies outside 80 % of the next radius, or every 12 s: 40 random points
inside 70 % of the next radius that are on the main walkable area. Cost = 120 × openness + 0.25 × distance from
us + 40 × crowd + 10 × (distance from the next centre / max(30, next radius)); crowd = sum over mechs seen in the
last 20 s of max(0, 80 − d) / 80, plus supply drops max(0, 60 − d) / 60. Openness = mean free sight distance over
24 rays at 7 m above the walking height, out to 200 m, as a fraction of 200 m. The new spot replaces the old one if
the old one is out of the circle or costs more than 15 extra.

### Travel

- Within 0.8 m: stand. On a raised obstacle (more than 3 m above the ground on a hindered cell): head straight.
- Closer than 45 m with a walkable straight line: head straight for the goal.
- Otherwise follow an A* route, re-planned when there is none, the goal moved more than 5 m, or it is older than
  2.5 s. Track the nearest of the next 10 route cells; if more than 12 m off the route, drop it and head
  straight; else head for the farthest of the next 14 route cells that is straight-walkable (straight to the goal
  if that is the last cell and the goal is within 4 m of it), else the next cell.

### Pocket escape

When grounded, not standing on a raised obstacle, and the nearest open cell is not on the main walkable area:
plan (at most every 6 s) the cheapest takeoff cell reachable from here (breadth-first, up to 60,000 cells, score =
2 × steps + 4 × distance to the nearest main-area cell within 20 steps of it) and a landing point at that
main-area cell, moved 5 m further if that point is also on the main area. Walk to the takeoff (2.5 m); wait for
energy ≥ min(70, 70 % of max); jump and fly toward the landing, boosting while lower than the highest solid top
on the next 30 m + 2.5 m.

---

## 10. Dodges and stuck recovery

- **Sidestep** (not while escaping or unstuck): every non-mortar projectile that will pass within our radius +
  1.1 m of our torso (56 % of height) in the next 0.7 s adds a perpendicular push away from its line; every mech
  charging a laser aimed within 0.15 rad of us adds a 1.5× push across the beam on the strafe side and allows a
  boost (energy > 25). Blend: move × 0.4 + dodge × 0.9, normalised if longer than 1.
- **Stuck**: commanding movement while grounded with horizontal speed below 1.2 m/s for more than 0.7 s → a
  random direction for 0.5 s with a jump, and the route is dropped.

---

## 11. Fire control

The muzzle is predicted 75 ms ahead (position + velocity × 0.075, at muzzle height).

- **Drone defence**: with no target and a drone hit within 3 s, pick a drone seen within 0.5 s that was
  shootable and within 150 m when seen, nearest in angle to the hit bearing within 0.7 rad (a drone that hit us
  within 3 s counts as angle 0). No reload while defending.
- **Aim point**: target mech → body centre (85 % of its height if the centre is blocked from the predicted
  muzzle); drone → half its height; crate → half its height. Crates and drones are not shot while backing off or
  below 50 % HP unless defending.
- **No target**: aim at the nearest hot (or recently hitting) visible mech, by distance when last seen; else
  the drone-hit bearing (2.5 s); else the hurt bearing (2.5 s); else the movement direction with a ±1.15 ×
  sin(1.3 t) sweep; else turn by 0.35 rad. Pitch level.
- **Gun**: lead in 4 iterations (flight time = (distance − 3) / bullet speed + 75 ms; vertical lead only for
  airborne targets, at half weight). Fire when within 96 % of range, with ammo, not reloading, and the aim error
  after one turn step (turn rate × 0.05, pitch rate × 0.05) is within 75 % of the target radius in yaw and 40 %
  of its height in pitch.
- **Lightning**: fire when within range − 1.5, the target is inside the cone half-angle − 0.05, and energy suffices.
- **Laser** (mechs only, within 97 % of range): charge until full − 0.051 s, aiming at the predicted body point
  once past 45 % charge. When full, release once on target (yaw within 90 % of tolerance, pitch within
  tolerance); otherwise keep holding.
- **Lethal release** (Hunt): with charge ≥ minimum charge, release early if `lethal_k` (80 %) of the partial damage (× cube
  damage bonus, × cookoff bonus against a mortar carrier) kills the target and we are on target.
- **Mortar** (only if one is ever held; mortar pickups are never wanted): lob at the target's feet (low arc, 3
  iterations, path checked against solids) when ready and 16–200 m away; it steers the aim only if the gun cannot fire.
- **Laser pre-charge**: with no target but a foe (the hidden one) within laser range, keep a ready laser charging
  (up to full + hold time − 0.35 s) so it fires the moment the foe appears.
- Pitch is clamped to the mech's pitch limits.
- **Reload**: when shooting at nothing (no target, drone or crate, not defending), not already reloading, ammo
  below the magazine, and ammo ≤ 60 % of the magazine or no danger for 3 s.

---

## 12. Upgrades

The offered choice that comes earliest in the active order wins:

- **Laser line** (Hunt): plating, armor, laser_damage, laser_charge, nanites, gun_damage, gun_rate,
  lightning_efficiency, lightning_range, servos, gun_mag, gun_velocity, gyros, lightning_chain, reactor,
  capacitor, mortar_reload, mortar_splash, mortar_velocity, sensors, jets, laser_pierce.
- **Gun line** (Scout/Guard): plating, armor, gun_damage, nanites, gun_rate, lightning_efficiency,
  lightning_range, laser_damage, servos, laser_charge, gun_mag, gun_velocity, gyros, lightning_chain, reactor,
  capacitor, mortar_reload, mortar_splash, mortar_velocity, sensors, jets, laser_pierce.
- **Speed line** (switch only): plating, armor, servos, jets, reactor, laser_damage, laser_charge, nanites,
  gun_damage, gun_rate, lightning_efficiency, lightning_range, gun_mag, gun_velocity, gyros, lightning_chain,
  capacitor, mortar_reload, mortar_splash, mortar_velocity, sensors, laser_pierce.

---

## 13. Loot

Utility = value / (distance + 18), × 1.35 for the item already being chased; the highest wins. An item is
rejected if it is banned, more than 260 m away, outside the current circle − 6 m, unreachable before the zone
deadline at its spot (now + distance × 1.4 / walk speed + 5 s, pad 6 m), more than 3.2 m above the ground, or
not on the main walkable area.

| Item | Radius | Value |
| --- | --- | --- |
| Wanted weapon pickup | `loot_r`, or `laser_r` for a laser when larger | 130 if the second slot is empty, else 60; a sought laser at least 150 |
| Cube | max(60, 0.7 × `loot_r`) | 75 (25 at the stat cap) |
| Crate (HP above `farm_hp`, no danger, before `crate_until`) | `loot_r` | 0.6 × cube value |
| Drone seen within 12 s (same conditions, below the cube cap) | `loot_r` | 85 |
| Supply drop | switched off | — |

Chasing the same item id for more than 30 s bans it for 60 s.

### Weapon pickups

Slot 1 is the gun (taken only if empty). The second slot takes a weapon worth more than the current one:
laser 4, lightning 3, mortar 0 (never). A wanted pickup within pickup radius − 0.3 m and 3.6 m in height is taken
(dropping the second slot if both are full), unless the held laser is charging.

---

## 14. World model

- **Terrain**: 1 m height grid (int16 centimetres, single precision), bilinear ground height.
- **Buildings**: prisms (polygon rings with holes, base and top heights) indexed in 16 m bins.
- **Height layers** (1 m raster, single precision): the solid stack on the ground (a solid starting ≤ 1.5 m
  above the stack joins it), the walking height (the stack, or a low bridge deck less than 6.5 m above the
  ground), hindered cells (stack more than 1.2 m above the ground, or a low overhead solid), and floating slabs.
- **Line of fire**: the segment against every prism in the bins it crosses (bounding-box test, start-inside test,
  wall and cap intersections) and against the terrain (0.5 m march with 8 m block-maximum skipping). Trees never
  stop shots.
- **Route grid**: 2 m cells at the terrain origin + 2i (nearest cell rounds halves away from zero). A cell is
  sealed if it is a roof (a non-bridge solid more than 1.5 m above the ground) or a mech of radius 2.6 m cannot
  stand there without a wall (from step height up to 9 m) inside it. Wall margin = 4-neighbour distance to the
  nearest sealed cell, capped at 20 m. Patches = 4-connected cells that can step both ways (rise ≤ 2.2 m
  straight, ≤ 3.1 m diagonal); the largest patch is the main area.
- **A\***: 8 neighbours, no corner cutting, step cost 2 m (× 1.4142 diagonal) × (1 + 0.15 × (4 − margin)) when the
  margin is below 4 m, octile heuristic × 1.15, up to 22,000 expansions, single-precision costs, open list ordered
  by cost only. When the goal is unreachable, the route ends at the closest reached cell.
- **Walkable straight line**: 1 m samples; fails on a sealed cell or a rise of more than 3.1 m between cells.

---

## 15. Protocol and robustness

- Answers `hello` with `ready` (name `warden`) after building the world model (about 0.1 s). If the map cannot
  be read, it plays on a flat arena; an empty map directory means `/arena`.
- Random generator seeded with the hello seed (0 → slot + 1).
- Only the newest queued observation is answered. An unreadable observation is answered by standing still. A
  failure inside a decision answers by standing still with the last aim. Non-finite move or aim values are
  replaced (move → 0, aim left out).
- Missing numeric fields count as zero; missing mech stats fall back to the config's mech numbers.
- A typical decision takes well under 2 ms.
