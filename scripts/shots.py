"""Shot analysis from traces (logs/sim/trace-*.jsonl or logs/live-*.jsonl): per weapon, hit rate and damage
per shot by range and target speed, plus damage per second of trigger time.

    python3 scripts/shots.py logs/sim/trace-*.jsonl

A shot = gun ammo decrement / laser or lightning cooldown jump. Its target = the visible mech closest to the
torso aim ray. A shot "hits" if a matching `hit` event (same weapon, target mech) arrives within the
projectile flight time + 0.25 s (gun) or the same/next decision (laser, lightning).
"""
import collections
import glob
import json
import math
import sys


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def load(path):
    obs = []
    for l in open(path):
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if d.get("dir") == "in" and d["msg"].get("type") == "obs":
            obs.append(d["msg"])
    return obs


def band(d):
    for lim in (30, 60, 90, 120, 160):
        if d < lim:
            return f"<{lim}"
    return ">=160"


def main():
    paths = sys.argv[1:] or sorted(glob.glob("logs/sim/trace-*.jsonl"))
    stats = collections.defaultdict(lambda: [0, 0, 0.0])  # key -> shots, hits, dmg
    for p in paths:
        obs = load(p)
        for k in range(1, len(obs)):
            o, prev = obs[k], obs[k - 1]
            me, pm = o["self"], prev["self"]
            pw = {w["kind"]: w for w in pm["weapons"] if w}
            for w in me["weapons"]:
                if not w or w["kind"] not in pw:
                    continue
                q = pw[w["kind"]]
                kind = w["kind"]
                n = 0
                if kind == "gun" and "ammo" in w and "ammo" in q and w["ammo"] < q["ammo"]:
                    n = q["ammo"] - w["ammo"]
                elif kind in ("laser", "lightning") and w["cooldown"] > q["cooldown"] + 0.05:
                    n = 1
                if not n:
                    continue
                # target: visible mech nearest the aim ray (use previous obs = what we aimed at)
                yaw = pm["aim"][0]
                best, bd = None, 1e9
                for m in prev["mechs"]:
                    if m["kind"] != "mech":
                        continue
                    dx, dy = m["pos"][0] - pm["pos"][0], m["pos"][1] - pm["pos"][1]
                    d = math.hypot(dx, dy)
                    off = abs(wrap(math.atan2(dy, dx) - yaw)) * d
                    if off < 12 and off < bd:
                        best, bd = (m, d), off
                if best is None:
                    continue
                m, d = best
                spd = math.hypot(m["vel"][0], m["vel"][1])
                sp = "still" if spd < 2 else ("slow" if spd < 8 else "fast")
                horizon = (d / 252.0 + 0.25) if kind == "gun" else 0.25
                hits, dmg = 0, 0.0
                for o2 in obs[k:k + 1 + int(horizon / 0.1) + 1]:
                    for e in o2["events"]:
                        if e["type"] == "hit" and e["weapon"] == kind and e["target"] == m["id"]:
                            hits += 1
                            dmg += e["amount"]
                hits = min(hits, n)
                for key in ((kind, band(d), sp), (kind, band(d), "all"), (kind, "all", sp), (kind, "all", "all")):
                    s = stats[key]
                    s[0] += n
                    s[1] += hits
                    s[2] += dmg
    print(f"# Shot analysis over {len(paths)} traces\n")
    for kind in ("gun", "laser", "lightning"):
        rows = [(k, v) for k, v in stats.items() if k[0] == kind]
        if not rows:
            continue
        print(f"## {kind}")
        print("| range | target | shots | hit % | dmg/shot |")
        print("|---|---|---|---|---|")
        order = ["<30", "<60", "<90", "<120", "<160", ">=160", "all"]
        for b in order:
            for sp in ("still", "slow", "fast", "all"):
                v = stats.get((kind, b, sp))
                if v and v[0] >= 5:
                    print(f"| {b} | {sp} | {v[0]} | {100 * v[1] / v[0]:.0f}% | {v[2] / v[0]:.0f} |")
        print()


if __name__ == "__main__":
    main()
