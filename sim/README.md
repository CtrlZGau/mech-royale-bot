# sim/: local lockstep Mech Royale simulator

Plays our real bot (unchanged, as a subprocess speaking `mechfight/1`) against 15 in-process
house bots that act as the practice squad (shared vision, no friendly fire, hunt us). One
48-seed batch takes ~5-10 s on 20 cores. Standard library only.

## Commands

```sh
# 48 practice-like matches, parallel; prints score = mean(16 - place) and diagnostics
python3 sim/run.py --seeds 1-48 -- python3 -u bot/main.py

# paired A/B on identical seeds (spawns, loot, zone) with a bootstrap 95% CI of the difference
git archive v0-3.25 bot | tar -x -C /tmp/v0          # any old version, without touching the repo
python3 sim/run.py --seeds 1-48 --ab "python3 -u /tmp/v0/bot/main.py" --ab "python3 -u bot/main.py"

# one match, with replay (logs/sim/replay-<seed>.jsonl) + obs/action trace + schema validation
python3 -m sim.server --seed 3 --replay --trace --validate -- python3 -u bot/main.py
python3 scripts/view.py logs/sim/replay-3.jsonl         # official 3D viewer
python3 scripts/opponents.py logs/sim/replay-*.jsonl    # house-bot profile on sim output

# calibration table (every version vs its practice score), aim calibration, unit tests
python3 sim/calibrate.py --versions DIR                 # DIR/<v0|v1.1|v1.2|v1.3>/bot/...
python3 -m sim.calib_aim --diff expert
python3 -m sim.test_sim
```

Options: `--param k=v` (squad parameters, see `DEFAULTS` in `sim/house.py`), `--replay`,
`--trace` (writes `logs/sim/trace-<seed>.jsonl` in the `scripts/report.py` live-log format),
`--no-timeout` (debugging a bot in a debugger), `--budget-ms` (default 50, from the config),
`--json out.json` (per-match rows). Bot stderr goes to `logs/sim/stderr/`.
The run stops when our mech dies (place is fixed then); `python3 -m sim.server --full` keeps going.

`run.py` prints: score ± SE, places, kills/match, median survival, deaths < 60 s, zone deaths,
damage dealt/taken by weapon, laser share of damage taken, our / house accuracy, time stationary,
cubes, 2nd-weapon rate, unanswered observations (our bot over budget).

## Calibration (sim.md §5), 48 seeds, defaults

| version | practice | sim seeds 1-48 | sim seeds 49-96 |
|---|---|---|---|
| v0 (`v0-3.25`) | 3.25 | 3.06 ± 0.21 | 3.06 ± 0.22 |
| v1.2 (`ddd5e45`) | 2.98 | 2.83 ± 0.21 | 3.04 ± 0.23 |
| v1.1 (`6fd8444`) | 1.77 | 2.35 ± 0.19 | 2.44 ± 0.18 |
| v1.3 (`d57e515`) | ? | 3.08 ± 0.22 | 3.12 ± 0.21 |
| kit example bot | 1.15 | 0.58 ± 0.12 | 0.44 ± 0.10 |

Known misses: v1.1 (survive-first) is ~0.6 too high and the kit example bot ~0.6 too low.
Treat sim differences < ~0.4 (one SE of a paired 48-seed A/B is ~0.15-0.25) as noise; use
`--seeds 1-96` for decisions that matter.

## What is modelled

- **Map** (`geom.py`): bilinear terrain, 259 prisms (walls, roofs, bridges, holes), wall
  push-out with sliding, step-up 1.2 m, slope limit 50°, ceilings/bridges, DDA segment tests.
  Trees: trunks + ellipsoid crowns with transmittance `exp(-canopyDensity * len) < canopyBlockT`
  (path length ×1.5); validated against 37 live recordings + god-view replays: 90-93 % of real
  first sightings are visible in the model, ~1-2 % false sightings.
- **Engine** (`engine.py`): config from the recorded hello (`kit/check/stream.jsonl.gz`).
  Movement/jets/energy, aim slew, gun (spread, mag, reload), laser (charge, min_charge, hold_s
  auto-fire, half speed, cook-off), lightning (cone, LOS, chain, short), mortar (ballistic,
  splash, knockback, drain), rear ×1.5, cube scaling/size, armor, HP regen, zone (seeded phases),
  NPC drones, crates (¼ on roofs), floor loot + take/drop/swap rules, cubes + upgrade offers,
  supply drops, vision (near/wedge/LOS/memory 5 s), sounds with bearing noise, all obs events,
  `mechreplay/1` recording.
- **Server** (`server.py`): hello (absolute `kit/arena` map dir), ready, lockstep obs → action
  within the budget, tick echo, newest-obs trivially satisfied, coast then idle after `coast_s`,
  end; optional schema validation of every obs and action (`kit/check/check.py` Schema).
- **Squad** (`house.py`): difficulties 4 easy / 4 medium / 4 hard / 3 expert; weakest
  difficulty hunts first; hunters = `h0 + h_per_kill·kills + h_per_min·minutes` (all after zone
  phase 4 or ≤ 6 alive); hunters know where we are; roamers wander in the next circle, loot,
  break crates, fight drones, shoot us inside 25 m or when hit. Per bot: lead + AR(1) aim error
  (σ = diff·(2.5 m + 0.07·d)), strafe with flips, close when healthy / back off < 40 % HP,
  laser released at 0.8 s, dual fire, lightning < 42 m, mortar 35-110 m during reloads, jet over
  obstacles, combat hops, upgrades plating/armor first.

## Approximations / not modelled

Roof-edge and eave sliding rules (§7) are approximate; NPC drones walk on the ground; supply
drops land on open ground near the next circle; house bots ignore supply drops; mortar splash
LOS uses one ray. Spawns: open ground within 0.8 × start radius, ≥ 130 m apart (relaxed if
needed), facing the zone centre. Our slot is random per seed.
