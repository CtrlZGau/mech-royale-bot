"""Unit tests for the simulator.  python3 -m sim.test_sim"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim import house  # noqa: E402
from sim.engine import Match, Weapon  # noqa: E402
from sim.server import load_config, load_map  # noqa: E402

CFG = load_config()
MAP = load_map()


def bare_match(seed=1):
    """Match with no house AI: mechs 0 and 1 alive, the rest removed."""
    M = Match(CFG, MAP, seed, ext_ids=[0, 1])
    for m in M.mechs[2:]:
        m.alive = False
        m.place = 16
    for n in M.npcs:
        n.alive = False
    M.crates.clear()
    return M


def big_building():
    best = None
    for p in MAP.prisms:
        if p.kind == "building" and p.top - MAP.ground(*p.ring[0]) > 12 and p.base < MAP.ground(*p.ring[0]) + 0.5:
            w = min(p.x1 - p.x0, p.y1 - p.y0)
            if best is None or w > best[0]:
                best = (w, p)
    return best[1]


def place(m, x, y):
    m.x, m.y = x, y
    m.z = MAP.floor_at(x, y, MAP.ground(x, y) + 0.5, 1.2)[0]
    m.vx = m.vy = m.vz = 0.0
    m.grounded = True


def run(M, acts, seconds):
    for _ in range(int(seconds * 10)):
        M.decide(acts)


def wall_spot():
    """A point on open ground with a tall building wall 4-10 m to the east."""
    for p in MAP.prisms:
        g = MAP.ground(*p.ring[0])
        if p.kind != "building" or p.top - g < 12 or p.base > g + 0.5:
            continue
        for k in range(1, 20):
            y = p.y0 + (p.y1 - p.y0) * k / 20
            x = p.x0 - 9
            gz = MAP.ground(x, y)
            if not MAP.open_ground(x, y, 2.6):
                continue
            s = MAP.seg_prism(x, y, gz + 2, x + 15, y, gz + 2, any_hit=True)
            if s is not None and s > 0.3:
                return p, x, y
    raise AssertionError("no wall spot found")


def test_wall_slide():
    p, x, y = wall_spot()
    M = bare_match()
    m = M.mechs[0]
    place(m, x, y)
    run(M, {0: {"move": [0.8, 0.6]}}, 3.0)
    assert not p.inside2d(m.x, m.y), "walked into a building"
    moved = math.hypot(m.x - x, m.y - y)
    assert moved > 12, f"stuck on the wall instead of sliding (moved {moved:.1f} m)"
    assert m.z < p.top - 1, "climbed the wall"


def test_cant_walk_up_wall():
    p, x, y = wall_spot()
    M = bare_match()
    m = M.mechs[0]
    place(m, x, y)
    run(M, {0: {"move": [1, 0]}}, 3.0)
    assert not p.inside2d(m.x, m.y), "walked into a building"
    assert m.x - x < 10, "went through the wall"
    assert m.z < MAP.ground(m.x, m.y) + 1.3, "walked up a wall"


def test_jet_climb():
    M = bare_match()
    m = M.mechs[0]
    z0 = m.z
    run(M, {0: {"jump": True, "boost": True}}, 0.1)
    run(M, {0: {"boost": True}}, 3.0)
    assert m.z > z0 + 15, f"jets too weak: rose {m.z - z0:.1f} m in 3 s"
    assert m.energy < 60


def test_regen_timing():
    M = bare_match()
    a, b = M.mechs[0], M.mechs[1]
    M.damage(a, 400, b, "gun", b.x, b.y)
    hp0 = a.hp
    run(M, {}, 4.5)
    assert abs(a.hp - hp0) < 1e-6, "regen started before regen_delay"
    run(M, {}, 1.5)
    gained = a.hp - hp0
    assert 10 < gained < 45 * 1.2, f"regen after delay wrong: {gained:.1f}"


def test_damage_numbers():
    M = bare_match()
    a, b = M.mechs[0], M.mechs[1]
    place(a, 0, 0)
    place(b, 30, 0)
    b.yaw = math.pi  # b faces a
    hp = b.hp
    M.damage(b, CFG["weapons"]["gun"]["damage"], a, "gun", a.x, a.y)
    assert abs(hp - b.hp - 45) < 1e-6, "front gun hit != 45"
    b.yaw = 0.0  # back to a
    hp = b.hp
    M.damage(b, 45, a, "gun", a.x, a.y)
    assert abs(hp - b.hp - 67.5) < 1e-6, "rear hit != x1.5"
    b.yaw = math.pi
    a.cubes = 5
    hp = b.hp
    M.damage(b, 45, a, "gun", a.x, a.y)
    assert abs(hp - b.hp - 45 * 1.2) < 1e-6, "cube scaling wrong"


def test_duel_weapons():
    """Two scripted mechs on open ground: gun bullets, full laser, lightning chain numbers."""
    from sim.calib_aim import find_open
    import random
    cx, cy = find_open(MAP, random.Random(3))
    M = bare_match()
    a, b = M.mechs[0], M.mechs[1]
    place(a, cx, cy)
    place(b, cx + 30, cy)
    a.yaw = a.cmd_yaw = 0.0
    b.yaw = b.cmd_yaw = math.pi
    a.weapons[1] = Weapon("laser", a.wstats["laser"])
    dz = (b.z + 4.5) - (a.z + 7)
    aim = [0.0, math.atan2(dz, 30)]
    hp = b.hp
    # gun: one shot
    M.decide({0: {"aim": aim, "fire": [True, False]}})
    run(M, {0: {"aim": aim}}, 0.5)
    assert abs((hp - b.hp) - 45) < 1e-6, f"gun hit {hp - b.hp}"
    # laser: hold 0.8 s then release -> full 315
    hp = b.hp
    for _ in range(8):
        M.decide({0: {"aim": aim, "fire": [False, True]}})
    M.decide({0: {"aim": aim, "fire": [False, False]}})
    assert abs((hp - b.hp) - 315) < 1e-6, f"laser hit {hp - b.hp}"
    # lightning: 60 to b
    a.weapons[1] = Weapon("lightning", a.wstats["lightning"])
    hp = b.hp
    M.decide({0: {"aim": aim, "fire": [False, True]}})
    assert abs((hp - b.hp) - 60) < 1e-6, f"lightning hit {hp - b.hp}"


def test_squad_no_friendly_fire_and_converge():
    from sim.calib_aim import find_open
    import random
    M = Match(CFG, MAP, 4, ext_ids=[0], house_factory=lambda MM: house.setup(MM, {"h0": 15}))
    tgt = M.mechs[0]
    cx, cy = M.zone_state()[0]
    d0 = sorted(math.hypot(m.x - tgt.x, m.y - tgt.y) for m in M.mechs[1:])
    for _ in range(300):
        acts = {0: {}}
        acts.update(M.squad.decide())
        M.decide(acts)
        tgt.hp = tgt.max_hp(CFG)  # immortal target
    house_taken = sum(sum(v for k, v in m.dmg_taken.items() if k != "npc" and k != "zone") for m in M.mechs[1:])
    house_dealt_to_house = sum(1 for m in M.mechs[1:] if m.killed_by is not None and m.killed_by != 0)
    assert house_taken == 0, f"house bots took weapon damage {house_taken} (friendly fire)"
    assert house_dealt_to_house == 0
    d1 = sorted(math.hypot(m.x - tgt.x, m.y - tgt.y) for m in M.mechs[1:] if m.alive)
    assert st_median(d1) < st_median(d0) * 0.6, f"squad did not converge: {st_median(d0):.0f} -> {st_median(d1):.0f}"
    assert sum(tgt.dmg_taken.values()) > 1000, "squad never shot the target"


def st_median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"FAIL {t.__name__}: {e}")
    print("PASS" if not bad else f"{bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
