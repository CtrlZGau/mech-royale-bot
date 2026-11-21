# Warden: the full strategy and state machine

Warden is our main policy. Code: `bot/warden.py` (class `WardenBot`), selected by `POLICY = "wd"` in
`bot/main.py`. It runs on our Python map layer (`bot/mb_world.py`: 2 m height/walkability grid, line of
sight, A* planner) and reuses a few base helpers from `bot/mb_bot.py` (path following, stuck recovery,
mortar-shell dodge, attacker memory).

This document describes every rule and every number, in the order the bot evaluates them each decision
(10 decisions per second, each answered in about 0.1–15 ms).

---

## 1. The big picture

Placement is the order mechs die in. In practice, 15 house bots act as one squad that hunts us, never
fights itself, and sends more hunters as we kill them. So practice points ≈ how many hunters we kill
before we die. Warden therefore:

1. **Opens carefully** (first 20 s): get a laser, avoid early fights, wait at a low-exposure spot.
2. **Hunts** (rest of the match): fights anything that threatens it or is weak, focuses wounded targets,
   finishes them up close, backs off only in short bursts when a fight turns, and keeps collecting cubes.
3. **Turns cautious for good** if the match is clearly not a pure squad hunt (someone else died, which
   happens in free-for-alls like the final): the Guard profile.

### Phases (profiles)

| Phase | When | Character |
|---|---|---|
| **Scout** | match time ≤ 20 s (and Guard not triggered) | Cautious. Laser-first loot from far away, fight only when engaged, peek from cover, wait at a quiet post. |
| **Hunt** | match time > 20 s (and Guard not triggered) | Aggressive squad hunter. |
| **Guard** | permanently, once a death we did not cause has been visible for 5 decisions in a row | Cautious default for free-for-all play. |

**Guard trigger:** each decision, `others_dead = start_alive − alive_now − our_kills`. If it is > 0 for
`OTHERS_DEAD_TICKS = 5` consecutive decisions, Guard is switched on for the rest of the match. In pure
squad practice this only happens if a house bot dies to the zone or a drone; in free-for-all rooms it
triggers within ~13–27 s (measured on 18 live recordings).


### Profile parameters

| Parameter | Scout | Hunt | Guard | Meaning |
|---|---|---|---|---|
| `fight_close` | 60 m | 60 m | 60 m | always fight a mech closer than this |
| `max_range` | 215 m | 240 m | 215 m | farthest mech we will target |
| `hp_weight` | 60/1400 per HP | 0.2 per HP | 60/1400 | target score: + weight × enemy HP |
| `wounded_bonus` | 0 | 100 | 0 | target score bonus for enemies ≤ 30% HP |
| `keep_bonus` | 0 | 120 | 0 | bonus for the target we shot in the last 2 s |
| `pull_hp` / `steady_hp` | 0.38 / 0.80 | (not used) | 0.38 / 0.80 | retreat below / recover above (cautious rule) |
| `pref_gun` / `pref_light` / `pref_laser` | 85 / 28 / 85 m | 85 / 28 / 140 m | 85 / 28 / 85 m | preferred fight range by loadout |
| `loot_r` | 150 m | 150 m | 90 m | radius for weapons, crates, drones |
| `laser_r` | 240 m | 240 m | 90 m | radius for laser pickups |
| `farm_hp` | 0.70 | 0.50 | 0.70 | minimum HP fraction to farm crates/drones |
| `crate_until` | always | always | 150 s | crates are farmed only before this match time |
| `cube_fight_r` | 0 (off) | 70 m | 0 (off) | grab cubes within this range even mid-fight |
| `finish_r` | 0 (off) | 14 m | 0 (off) | close to this range to finish a wounded target |
| `laser_ups` | no | yes | no | laser-centred upgrade order |
| `laser_first` | yes | yes | **no** | laser is the most wanted second weapon |
| `peek` | yes | no | yes | fight from cover edges, count hidden foes |

---

## 2. Per-decision pipeline

```
observation
  -> memory update (base + Warden)          section 3
  -> phase / Guard check                    section 1
  -> upgrade pick (if an offer is pending)  section 12
  -> threat picture + target choice         section 4
  -> back-off decision                      section 5
  -> zone timing                            section 6
  -> MODE (first matching rule wins)        section 7
  -> bullet / laser sidestep, stuck recovery section 9
  -> fire control                           section 10
  -> reload                                 section 10
  -> weapon pickup within reach             section 11
  -> action
```

---

## 3. Memory

From the base layer (`mb_bot.update_memory`):
- **Attackers:** for every `damaged` event from a mech: `attackers[id] = (time, bearing)`.
- **Last hit time:** any non-zone damage.
- **Threat bearing:** the last damage bearing, or a combat sound (gun, laser, laser_charge, lightning,
  mortar, step, boost, jet, reload) heard *near* or *mid* within 0.5 s; a sound only replaces an older
  bearing after 1.5 s.
- **Enemy sightings:** every mech we see, with its latest position, velocity, aim, HP, weapons and time.
- **Failed takes** (pickup ids, skipped for a few seconds) and a 1.6 s position history (stuck check).

Warden's own memory:
- **Item books** for crates, cubes and weapon pickups: id → position, time, kind. An item is forgotten
  when it should be visible (within 40 m, line of sight from our eye, unseen for > 0.35 s) but isn't.
- **Drones:** every NPC seen, kept 120 s.
- **Drone hits:** time and bearing of the last damage from a drone.
- **Our kills**, start alive count, and the "someone else died" streak (Guard trigger).

---

## 4. Threat picture and target choice

Only mechs seen within the last 2.5 s count. For each one seen *this* decision:
- `d` = horizontal distance, `shot` = clear line of fire from our muzzle (to its centre, or to 85% of its
  height if the centre is blocked), `aimed` = its torso aim is within 0.22 rad of us,
  `closing` = its approach speed toward us (m/s), `struck` = seconds since it last hit us.

### When a mech is "hot" (worth fighting)

**Hunt:** any of
- `d < 60 m`
- it hit us within the last 6 s
- endgame: next circle radius ≤ 70 m or ≤ 3 mechs alive
- `closing > 3 m/s` and `d < 180 m`
- its HP < 40% and `d < 160 m`
- `aimed` and `d < 190 m`

**Scout / Guard:** any of
- `d < 60 m`
- it hit us within the last 4 s
- `aimed`, its laser is charging, and `d < 120 m`
- its HP < 350 and `d < 120 m`
- ≤ 2 mechs alive

### Target score (lowest wins; only hot mechs with a clear shot inside `max_range`)

```
score = d + hp_weight × enemy_HP
        − wounded_bonus     if enemy HP ≤ 30% of its max      (Hunt: 100)
        − keep_bonus        if it was our target in the last 2 s (Hunt: 120)
        − 40                if it hit us in the last 4 s
        − 15                if it is closing faster than 3 m/s
```

The chosen target is remembered for the stickiness bonus.

**Hidden foe** (Scout/Guard only, because `peek` is on): the nearest hot mech *without* a clear shot
inside `max_range`. If there is no target, the hidden foe becomes the **foe** we fight around.

**Danger** = any visible mech is hot or hit us in the last 4 s. **Crowd** = mechs seen this decision that
are hot or have a clear shot within 150 m. **Finishing** = target within 120 m with HP < 200 or less than
half our HP.

---

## 5. When to back off

### Hunt: short bursts while losing
- `near` = mechs seen this decision within 160 m; `tf` = target HP fraction; `hpf` = our HP fraction.
- **Losing** if `(near ≥ 2 and hpf < 0.55)` or `(near ≥ 3 and hpf < 0.80)` or `(hpf < 0.35 and tf > hpf + 0.10)`.
- **Never losing** if the target is nearly dead: `tf < 0.25 and hpf > 0.20 and near ≤ 2`.
- Backing off lasts **1.5 s after the last "losing" decision**, then we fight again. No long heals.

### Scout / Guard: classic retreat
- **Swamped:** crowd ≥ 3, or crowd = 2 and `hpf < 0.65`, or **outgunned** (target carries a laser, we
  don't, it is > 55 m away, and `hpf < 0.80`).
- **Start backing off** when `hpf < 0.38` with danger in the last 2 s, or when swamped.
- **Stop** when not swamped and `hpf > 0.80`, or 7 s without danger and `hpf > 0.55`.

---

## 6. Zone timing

- **Safe point:** if we are inside the next circle shrunk by `margin = clamp(0.3 × next_radius, 3, 30) + 5`,
  stay where we are; otherwise the nearest point on that shrunk circle. If the next radius ≤ 30 m: the
  next centre.
- **Must leave by:** sample the shrinking circle at 25 points between shrink start and end (8 m pad); the
  first moment it no longer contains our position (∞ if we are already safely inside the next circle).
- **Travel need** = distance to the safe point × 1.45 / walk speed + 4 s.
- **Zone now** (urgent): we are outside the zone, or `now + need + 3 ≥ must_leave_by`.
- **Zone soon**: `now + need + 14 ≥ must_leave_by`.
- **Zone cost** of a spot (used to score positions): +150 + 5 per m if within 5 m of (or outside) the
  current circle; plus, as the shrink approaches, up to 25 + 0.8 per m for being near/outside the next one.

---

## 7. Mode selection (first matching rule wins)

| # | Mode | Condition | Movement |
|---|---|---|---|
| 1 | `shell` | a visible mortar shell will land within splash radius + 4.5 m of us within 2.5 s (simulated in 0.1 s steps) | move directly away (walkable direction), boost if energy > 15 |
| 2 | `zone` | **zone now** and the safe point is > 2 m away | path to the safe point; boost if outside or already late, energy > 5 |
| 3 | `cover` / `cornered` / `flee` | backing off, not finishing, not endgame | **cover search** (section 8): go to the nearest hidden spot (boost if energy > 10). If none: fight in place (`cornered`) if we have a target, else head for the safe point (`flee`; if a drone hit us in the last 3 s and we would stand still, strafe across its bearing, switching every 1.5 s) |
| 4 | `hide` | Scout/Guard, no target, hit within 2.5 s, not endgame, we know the hit's bearing | cover search against a "ghost" threat 80 m along the hit bearing plus visible mechs; boost if energy > 30 |
| 5 | `fight` / `fight-cube` | we have a foe | section 8 fighting; Hunt cube grab |
| 6 | `zone` | **zone soon** and the safe point is > 2 m away | path to the safe point |
| 7 | `loot-*` | a loot choice exists (section 11) | go to it; shoot drones/crates as described |
| 8 | `zone-walk` | Hunt (or next radius ≤ 30 m) and the safe point is > 3 m away | path to the safe point |
| 9 | `post` | Scout/Guard | walk to the low-exposure post; silently (≤ 4.6 m/s, below the footstep threshold) if no danger and not hit for 6 s |
| 10 | `idle` | nothing else | stand still (Hunt) |

---

## 8. Fighting, cover and positioning

### Line-of-sight wish
- **Armed** if the gun is loaded (reload ≤ 0.25 s left) and the foe is within gun range − 10 m, or the
  laser is nearly charged (≥ charge − 0.3 s) and in range, or lightning is in range + 6 m with energy for
  3 shots. Without peeking (Hunt), having a laser or lightning always counts as armed.
- `want_sight = +1` if armed, else `−1` (stay out of its sight while reloading/cooling down).
- Peeking only: if any visible mech is charging a laser aimed within 0.3 rad of us, `want_sight = −1.6`.

### Preferred range
Lightning (energy > 12): 28 m · laser: 85 m (Scout/Guard) or 140 m (Hunt) · gun only: 85 m.
Without lightning: at least 64 m against a lightning user, 80 m against a mortar user. Drones: 58 m.
**Finisher (Hunt):** if the foe is below 38% HP, we are above 42% HP and crowd ≤ 2, the preferred range
becomes 14 m for this decision.

### Fight step (where to move this decision)
Candidates: stay put, 16 directions at 6 m, and (peeking only) 16 more at 12 m. Each candidate must be in
bounds, walkable in a straight line and on a walkable cell. Score:

```
− 1.1 × |distance to foe − preferred range|
− 0.4 × step length
+ 45 × want_sight         if the candidate has line of fire to the foe's centre
+ 8                       (peeking, 6 m ring) if a spot 7 m further flips that line of fire (edge of cover)
− 30 per other enemy      within 170 m that can see the candidate's torso
− zone cost
+ 1.5 × (how much closer it brings us to a cube within 40 m)
+ 9 × (alignment with the current strafe side)   when want_sight > 0
staying put: − 8, and + 14 if we want to be out of sight and already are
```

**Strafe side:** flips with 60% probability every 0.3–1.0 s; if the strafe direction is blocked, flip
immediately and hold for 0.5 s. Boost toward a lightning user's range when it is > 50 m away and energy > 55.
**Hunt cube grab:** any remembered cube within 70 m (40 m when our HP ≤ 50%) overrides the fight move.

### Cover search (backing off and hiding)
- Threats: up to the 4 nearest visible mechs within 230 m (their eye height).
- If we are already hidden (no threat sees our top or our middle) and the zone cost here is 0: stay.
- Otherwise flood outward over walkable grid cells from our position (8-neighbour steps a mech can make,
  no corner cutting), up to 50 steps (~100 m), checking every other cell, at most 350 checks and ~8 ms.
- A cell qualifies if no threat can see a mech standing there at full or half height.
- Cell score = `2 × steps + zone cost − 0.25 × min(distance to nearest threat, 80)`; the search stops once
  steps can no longer beat the best found. The result is reused for 0.4 s.

### Scout/Guard post (waiting spot)
Re-picked every 12 s (or when it leaves 80% of the next circle): 12 random walkable ground cells inside
70% of the next radius, cost = `120 × exposure + 0.25 × distance + 40 × crowd + 10 × edge`, where exposure =
fraction of 24 sight lines (12 bearings × 60/130 m) that see the spot, crowd counts mechs seen in the last
20 s within 80 m and supply drops within 60 m, edge = distance from the next centre / next radius. A new
post replaces the old one only if it is ≥ 15 cheaper.

---

## 9. Dodges and stuck recovery

- **Bullet sidestep:** for each visible bullet that will pass within our radius + 1.1 m in the next 0.7 s,
  push perpendicular to its path, away from where it would pass.
- **Laser sidestep:** for each visible mech charging a laser aimed within 0.15 rad of us, push sideways
  (strength 1.5, current strafe side).
- The dodge is blended `move × 0.4 + dodge × 0.9`; boost if a laser is involved and energy > 25.
- **Stuck recovery (base layer):** moving but < 1.5 m travelled over 1.2 s (and not charging a laser) →
  0.8 s in a random direction within ±0.9 rad of the intended one with jump + boost, then replan.

---

## 10. Fire control

- **Muzzle:** our position + velocity × 0.075 s (latency) at muzzle height.
- **Target point:** mech centre (half its scaled height), or 85% of its height if the centre is blocked;
  drones at half height; crates at half height.
- **Gun lead:** 4 iterations of `t = latency + (distance − 3 m) / bullet speed`, target moved by its velocity
  × t (vertical motion only if it is airborne, at half weight).
- **Tolerances:** yaw `atan(0.75 × target radius / d)`, pitch `atan(0.4 × target height / d)`.
- **Slew-aware gate:** a weapon fires only if the remaining aim error *after* the torso turns for one more
  tick (turn rate × 0.05 s) is inside the tolerance.

| Weapon | Fires when |
|---|---|
| Gun | within 96% of its range, loaded, not reloading, gun-lead aim settles inside tolerance |
| Lightning | within range − 1.5 m, target inside cone/2 − 0.05 rad of the actual aim, enough energy |
| Laser | mechs only, within 97% of range. Keep charging until full − 0.05 s (switch to direct aim once > 45% charged); release when on target (yaw < 0.9 × tolerance, pitch inside tolerance). **Lethal early release:** if the current partial charge × 0.8 already kills the target (including cube damage bonus and +60% vs a mortar carrier), release immediately |

- **Drone defence:** if a drone hit us in the last 3 s and we have no mech target, shoot the visible drone
  (≤ 150 m, clear shot) closest to the hit bearing, even while backing off.
- **No crate/drone shooting** while backing off or below 50% HP (unless defending against a drone).
- **Hidden foe (peeking):** pre-charge the laser toward it (cooldown ready, in range, not beyond full +
  hold − 0.35 s).
- **No target:** never hold a laser charge past full + hold − 0.35 s. Look at the nearest hot/attacking
  mech; else the drone-hit bearing (2.5 s); else the last hit bearing (2.5 s); else sweep ±1.15 rad around
  the walking direction; else turn 0.35 rad per decision.
- **Reload:** when there is no target/drone/crate to shoot and the magazine is ≤ 60%, or 3 s without danger.

---

## 11. Loot

Every candidate must be within 260 m, inside the current circle by 6 m, reachable before the zone passes
it (`now + d × 1.4 / walk + 5 ≤ must leave by`), and not on a roof (≤ 3.2 m above ground).
**Utility = value / (distance + 18)**, × 1.35 for the item we are already chasing. Highest wins.

| Item | Condition | Value |
|---|---|---|
| Weapon pickup | wanted (below), within `loot_r` (lasers within `laser_r`) | 130 if a slot is empty, else 60; laser ≥ 150 when laser-first |
| Cube | within max(60 m, 0.7 × `loot_r`) | 75 (25 once at the cube stat cap) |
| Crate | HP > `farm_hp`, no danger, before `crate_until`, within `loot_r` | 45 |
| Drone | same, below the cube cap, seen in the last 12 s | 85 |
| Supply drop | Hunt only, no danger, quiet (section below) | 260 |

- **Chasing:** the same item for > 30 s → banned for 60 s.
- **Drone farming:** if the drone is visible, in clear sight and < 75 m → fight-step around it and shoot;
  else walk to it. **Crates:** if < 50 m with a clear shot → shoot it (back away if < 12 m); else walk to it.
- **Supply drops (Hunt):** only if in the zone, grounded, no visible projectiles, below the cube cap, not
  hit for 6 s, no mech seen within 180 m in the last 3 s, not held by another mech, and the trip fits:
  ETA (1.45 × distance / walk) ≤ 15 s, waiting for landing ≤ 6 s, ETA + wait + capture ≤ 20 s, and done
  ≥ 3 s before the zone passes the drop. Standing within 2.5 m of the drop centre: stop.

### Weapon pickups
- Wanted: a gun only if slot 0 is empty; never a kind we already hold; never a mortar.
- Laser-first (Scout, Hunt): laser 4 > lightning 3; Guard: lightning 3 > laser 2. A pickup is wanted only
  if it beats what is in slot 1.
- Within pickup radius − 0.3 m and 3.6 m vertically: take it; if both slots are full, drop slot 1 (never
  while slot 1's laser is charging). The gun is never dropped.

---

## 12. Upgrades

The first offered choice in this order wins.
- **Hunt (laser line):** plating, armor, laser_damage, laser_charge, nanites, gun_damage, gun_rate,
  lightning_efficiency, lightning_range, servos, gun_mag, gun_velocity, gyros, lightning_chain, reactor,
  capacitor, mortar_reload, mortar_splash, mortar_velocity, sensors, jets, laser_pierce.
- **Scout / Guard (gun line):** plating, armor, gun_damage, nanites, gun_rate, lightning_efficiency,
  lightning_range, laser_damage, servos, laser_charge, gun_mag, gun_velocity, gyros, lightning_chain,
  reactor, capacitor, mortar_reload, mortar_splash, mortar_velocity, sensors, jets, laser_pierce.

---

## 13. Navigation (base layer)

- 2 m grid of walking surfaces (terrain or roofs of solids starting < 9 m above the surface below),
  wall-adjacent cells cost more, gaps narrower than a mech are blocked.
- A* path planner advanced in ~15 ms slices; replan when the goal moves > 6 m, every 6 s, or when we drift
  > 40 m off the path; goals within 25 m with a clear straight walk skip the planner.
- Path following: steer to the farthest of the next 8 waypoints that is walkable in a straight line
  within 30 m. If no path yet: the nearest walkable direction to the goal.

