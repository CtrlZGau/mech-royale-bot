"""Aim calibration: one house bot (difficulty D) vs a scripted strafing dummy at fixed ranges.

    python3 -m sim.calib_aim [--diff expert] [--param aim_a=1.0 ...]

Prints gun hit rate per range bucket next to the measured house-bot rates (red-best, live
replays, sim.md §4). The dummy moves like a house bot in a fight: ~12 m/s strafe with direction
flips every 1-3 s, boosts ~25 % of the time, hops now and then, keeps the range.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim import house  # noqa: E402
from sim.engine import Match  # noqa: E402
from sim.server import load_config, load_map  # noqa: E402

TARGET = {12: 0.81, 37: 0.70, 62: 0.50, 87: 0.43, 112: 0.32, 137: 0.21}


def find_open(gmap, rng, need=170):
    best = None
    for _ in range(4000):
        x = rng.uniform(-450, 480)
        y = rng.uniform(-280, 330)
        if not gmap.open_ground(x, y):
            continue
        clear = 0
        for k in range(16):
            a = k * math.pi / 8
            ok = True
            for r in range(10, need, 10):
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if gmap.prisms_at(px, py):
                    ok = False
                    break
            clear += ok
        if best is None or clear > best[0]:
            best = (clear, x, y)
            if clear >= 10:
                break
    return best[1], best[2]


def run(D, diff, params, seconds=60, seed=1):
    cfg = load_config()
    gmap = load_map()
    M = Match(cfg, gmap, seed, ext_ids=[0], house_factory=lambda MM: house.setup(MM, dict(params, h0=1, start_weapon=0)))
    shooter = M.mechs[1]
    shooter.house.aim_mult, shooter.house.react_s, shooter.house.fire_p, shooter.house.aggr = house.DIFF[diff]
    for m in M.mechs[2:]:
        m.alive = False
        m.place = 16
    rng = random.Random(seed)
    cx, cy = find_open(gmap, rng)
    dummy = M.mechs[0]
    dummy.x, dummy.y, dummy.z = cx, cy, gmap.ground(cx, cy)
    a = rng.uniform(0, 2 * math.pi)
    shooter.x, shooter.y = cx + D * math.cos(a), cy + D * math.sin(a)
    shooter.z = gmap.ground(shooter.x, shooter.y)
    M.squad.p["want_range"] = D
    M.squad.p["hunt_engage"] = 400
    shots = hits = 0
    orig_fire = M._fire_gun
    stats = {"shots": 0, "hits": 0}

    def fire(m, w, ws):
        if m is shooter:
            stats["shots"] += 1
        return orig_fire(m, w, ws)
    M._fire_gun = fire
    orig_dmg = M.damage

    def dmg(tgt, amount, src, weapon, *a, **k):
        if src is shooter and tgt is dummy and weapon == "gun":
            stats["hits"] += 1
        r = orig_dmg(tgt, amount, src, weapon, *a, **k)
        dummy.hp = dummy.max_hp(cfg)  # immortal
        dummy.alive = True
        dummy._dying = False
        shooter.hp = shooter.max_hp(cfg)
        return r
    M.damage = dmg
    strafe = 1
    flip = 0.0
    boost_until = 0.0
    while M.t < seconds and not M.over:
        t = M.t
        if t > flip:
            strafe = -strafe if rng.random() < 0.7 else strafe
            flip = t + rng.uniform(1.0, 3.0)
            if rng.random() < 0.27:
                boost_until = t + rng.uniform(0.6, 1.6)
        dx, dy = shooter.x - dummy.x, shooter.y - dummy.y
        L = math.hypot(dx, dy) or 1
        ux, uy = dx / L, dy / L
        radial = max(-0.8, min(0.8, (L - D) / 15))
        mv = (-uy * strafe * (1 - abs(radial)) ** .5 + ux * radial, ux * strafe * (1 - abs(radial)) ** .5 + uy * radial)
        act = {"move": list(mv), "boost": t < boost_until, "jump": rng.random() < 0.02, "aim": [math.atan2(dy, dx), 0]}
        acts = {0: act}
        acts.update(M.squad.decide())
        M.decide(acts)
        if math.hypot(dummy.x - cx, dummy.y - cy) > 120:  # keep the fight in the open area
            dummy.x, dummy.y = cx, cy
            dummy.z = gmap.ground(cx, cy)
    return stats["shots"], stats["hits"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", default="expert")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--param", action="append", default=[])
    a = ap.parse_args()
    params = {}
    for kv in a.param:
        k, v = kv.split("=", 1)
        params[k] = float(v)
    tot_s = tot_h = 0
    for D, want in TARGET.items():
        s = h = 0
        for seed in (1, 2, 3):
            ss, hh = run(D, a.diff, params, a.seconds / 3, seed)
            s += ss
            h += hh
        tot_s += s
        tot_h += h
        print(f"range {D:4d} m: shots {s:4d} hit {h / max(s, 1):5.2f}   (measured red-best {want:.2f})")
    print(f"overall {tot_h / max(tot_s, 1):.2f}")


if __name__ == "__main__":
    main()
