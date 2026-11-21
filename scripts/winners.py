"""What do winners do differently? Per-mech features from every replay, correlated with placement.

    python3 scripts/winners.py               # all logs/replay-*.jsonl

For every mech in every match: early-game (first 45 s) behaviour, loadout timing, positioning,
fight initiation, cubes, then Spearman correlation of each feature with place (negative = more
of it -> better place) and means for top-3 vs early deaths (place >= 12).
Also: in pairwise duels, does the mech that lands the first hit win?
"""
import collections
import glob
import json
import math
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
from world import World  # noqa: E402

W = World(os.path.join(ROOT, "kit", "arena"))
EARLY = 45 * 20  # ticks


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def analyse(path, rows, duels):
    L = [json.loads(l) for l in open(path)]
    h = next((d for d in L if d.get("type") == "header"), None)
    res = next((d for d in L if d.get("type") == "result"), None)
    frames = [d for d in L if "m" in d]
    if not h or not res or len(frames) < 100:
        return
    kind = {i: ("US" if b == "human" else b.replace("house:", "")) for i, b in enumerate(h["bots"])}
    rmap = {r["id"]: r for r in res["mechs"]}
    f = collections.defaultdict(lambda: collections.defaultdict(float))
    prev = {}
    first_hit_pair = {}  # (a,b) unordered -> first src
    for fr in frames:
        t = fr["t"]
        M = {m[0]: m for m in fr["m"]}
        alive = {i: m for i, m in M.items() if m[6] > 0}
        z = fr["z"]
        for i, m in alive.items():
            F = f[i]
            if i in prev:
                d = math.hypot(m[1] - prev[i][1], m[2] - prev[i][2])
                F["dist_travelled"] += d
                if t <= EARLY:
                    F["early_dist"] += d
            near = min((math.hypot(m[1] - o[1], m[2] - o[2]) for j, o in alive.items() if j != i), default=999)
            F["n"] += 1
            F["nearest_sum"] += min(near, 300)
            if near < 100:
                F["t_enemy_within_100"] += 1
            if t <= EARLY:
                F["early_n"] += 1
                F["early_nearest_sum"] += min(near, 300)
            F["zone_frac_sum"] += math.hypot(m[1] - z[0], m[2] - z[1]) / max(z[2], 1)
            if m[3] - W.ground(m[1], m[2]) > 4:
                F["roof"] += 1
            if not (m[8] & 1):
                F["air"] += 1
            if m[8] & 2:
                F["boost"] += 1
            if m[12] >= 0 and "t_2nd_weapon" not in F:
                F["t_2nd_weapon"] = t / 20
            if m[12] == 2 or m[11] == 2:
                F["laser_frames"] += 1
            if t == EARLY or (t > EARLY and "cubes_45s" not in F):
                F["cubes_45s"] = m[9]
                F["hp_45s"] = m[6] / max(m[10], 1)
        for e in fr.get("ev", []):
            if e["e"] == "hit":
                s, d = e.get("src"), e.get("dst")
                if s is None or s not in kind:
                    continue
                if d < 100:
                    f[s]["dmg_to_mechs"] += e["amt"]
                    if t <= EARLY:
                        f[s]["early_dmg_dealt"] += e["amt"]
                        f[d]["early_dmg_taken"] += e["amt"]
                    key = (min(s, d), max(s, d))
                    first_hit_pair.setdefault(key, (s, t))
                elif d < 1000:
                    f[s]["drone_hits"] += 1
                else:
                    f[s]["crate_hits"] += 1
            elif e["e"] == "fire" and e.get("id") in kind and t <= EARLY:
                f[e["id"]]["early_shots"] += 1
        prev = alive
    # duels: did the first hitter of a pair kill the other (or outlive it)?
    for (a, b), (s, t0) in first_hit_pair.items():
        other = b if s == a else a
        rs, ro = rmap.get(s), rmap.get(other)
        if not rs or not ro:
            continue
        if ro.get("killed_by") == s:
            duels["first_hitter_killed_other"] += 1
        elif rs.get("killed_by") == other:
            duels["first_hitter_killed_by_other"] += 1
        else:
            duels["neither"] += 1
    for i, F in f.items():
        r = rmap.get(i)
        if not r or not F["n"]:
            continue
        row = {
            "type": kind.get(i), "place": r["place"],
            "early_dist_m": F["early_dist"], "early_nearest_m": F["early_nearest_sum"] / max(F["early_n"], 1),
            "mean_nearest_m": F["nearest_sum"] / F["n"], "frac_enemy_within_100": F["t_enemy_within_100"] / F["n"],
            "zone_dist_frac": F["zone_frac_sum"] / F["n"], "roof_frac": F["roof"] / F["n"], "air_frac": F["air"] / F["n"],
            "boost_frac": F["boost"] / F["n"], "t_2nd_weapon": F.get("t_2nd_weapon", 999),
            "laser_frac": F["laser_frames"] / F["n"], "cubes_45s": F.get("cubes_45s", 0), "hp_45s": F.get("hp_45s", 0),
            "early_dmg_dealt": F["early_dmg_dealt"], "early_dmg_taken": F["early_dmg_taken"],
            "early_shots": F["early_shots"], "drone_hits": F["drone_hits"], "crate_hits": F["crate_hits"],
            "cubes_max": r.get("cubes_max", 0), "kills": r.get("kills", 0),
            "dealt_per_taken": r.get("damage_dealt", 0) / max(r.get("damage_taken", 1), 1),
            "upgrades": len(r.get("upgrades", [])),
        }
        rows.append(row)


def main():
    rows, duels = [], collections.Counter()
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "logs", "replay-*.jsonl")))
    for p in paths:
        analyse(p, rows, duels)
    print(f"# What winners do differently: {len(rows)} mech-matches from {len(paths)} replays\n")
    feats = [k for k in rows[0] if k not in ("type", "place")]
    places = [r["place"] for r in rows]
    top = [r for r in rows if r["place"] <= 3]
    early = [r for r in rows if r["place"] >= 12]
    print("Spearman ρ with place (negative = more of it, better place). Top-3 vs place ≥ 12 means.\n")
    print("| feature | ρ | top-3 mean | early-death mean |")
    print("|---|---|---|---|")
    for k in sorted(feats, key=lambda k: spearman([r[k] for r in rows], places)):
        rho = spearman([r[k] for r in rows], places)
        print(f"| {k} | {rho:+.2f} | {st.mean(r[k] for r in top):.2f} | {st.mean(r[k] for r in early):.2f} |")
    tot = sum(duels.values())
    print(f"\nPairwise duels (first mech to land a hit on the other), n={tot}: "
          f"first hitter killed the other {100 * duels['first_hitter_killed_other'] / tot:.0f}%, "
          f"was killed by it {100 * duels['first_hitter_killed_by_other'] / tot:.0f}%, neither {100 * duels['neither'] / tot:.0f}%")
    print("\nTop-3 finishes by type:", dict(collections.Counter(r["type"] for r in top).most_common()))
    print("Early deaths (place ≥ 12) by type:", dict(collections.Counter(r["type"] for r in early).most_common()))


if __name__ == "__main__":
    main()
