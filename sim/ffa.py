"""Free-for-all of up to 16 external policies in the local sim (no house squad: everyone vs everyone).

    python3 -m sim.ffa --seeds 1-24 --jobs 8 --roster sim/roster.json [--replay]

Roster JSON: [{"name": "mb", "cmd": ["python3", "-u", "bot/main.py"], "env": {"MECH_POLICY": "mb"}}, ...]
(1-16 entries; if fewer than 16, the rest of the seats are filled with the house squad).
Each policy's slot is shuffled per seed. Reports per policy: mean points (16 - place), wins, top-3,
kills, survival, damage dealt/taken, gun accuracy, stationary share, cause-of-death mix.
This is a FREE-FOR-ALL like the final, not practice (practice = 1 bot vs the squad: sim/run.py).
"""
from __future__ import annotations

import argparse
import collections
import json
import multiprocessing as mp
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim.server import run_match  # noqa: E402


def parse_seeds(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _wrap(entry):
    env = " ".join(f"{k}={v}" for k, v in (entry.get("env") or {}).items())
    return (["env"] + env.split() + entry["cmd"]) if env else entry["cmd"]


def one(args):
    seed, roster, replay = args
    import random
    order = list(range(len(roster)))
    random.Random(seed * 7919 + 1).shuffle(order)  # who gets which external slot
    cmds = [_wrap(roster[i]) for i in order]
    r = run_match(seed, cmds, record=replay, replay_dir=os.path.join(ROOT, "logs", "sim", "ffa") if replay else None,
                  full=True, cwd=ROOT, stderr_dir=os.path.join(ROOT, "logs", "sim", "ffa-stderr"))
    mechs = r["result"]["mechs"]
    rows = []
    for k, slot in enumerate(r["slots"]):
        m = mechs[slot]
        killer = m.get("killed_by")
        kname = None
        if killer is not None:
            if killer in r["slots"]:
                kname = roster[order[r["slots"].index(killer)]]["name"]
            else:
                kname = "house/npc"
        rows.append({"seed": seed, "policy": roster[order[k]]["name"], "place": m["place"], "kills": m["kills"],
                     "alive_s": m.get("alive_s"), "dealt": m["damage_dealt"], "taken": m["damage_taken"],
                     "taken_w": m.get("taken_by_weapon", {}), "shots": m.get("shots", {}), "hits": m.get("hits", {}),
                     "stationary_s": m.get("stationary_s"), "killed_with": m.get("killed_with"), "killer": kname,
                     "cubes": m.get("cubes_max", 0), "late": r["bots"][slot]["late"], "max_ms": r["bots"][slot]["max_ms"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1-16")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--roster", required=True)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    roster = json.load(open(a.roster))
    os.makedirs(os.path.join(ROOT, "logs", "sim", "ffa-stderr"), exist_ok=True)
    seeds = parse_seeds(a.seeds)
    with mp.Pool(a.jobs) as pool:
        rows = [r for rs in pool.imap_unordered(one, [(s, roster, a.replay) for s in seeds]) for r in rs]
    if a.json:
        json.dump(rows, open(a.json, "w"))
    by = collections.defaultdict(list)
    for r in rows:
        by[r["policy"]].append(r)
    print(f"# FFA: {len(roster)} policies x {len(seeds)} seeds (everyone vs everyone, run to the end)\n")
    print("| policy | pts | wins | top3 | kills | alive s | dealt | taken | gun acc | still % | laser % taken | died to | late obs |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, rs in sorted(by.items(), key=lambda kv: -st.mean(16 - r["place"] for r in kv[1])):
        pts = [16 - r["place"] for r in rs]
        se = st.stdev(pts) / len(pts) ** 0.5 if len(pts) > 1 else 0
        shots = sum(r["shots"].get("gun", 0) for r in rs)
        hits = sum(r["hits"].get("gun", 0) for r in rs)
        taken = sum(r["taken"] for r in rs) or 1
        laser = sum(r["taken_w"].get("laser", 0) for r in rs)
        still = sum(r["stationary_s"] or 0 for r in rs) / max(sum(r["alive_s"] or 0 for r in rs), 1)
        died = collections.Counter(r["killed_with"] for r in rs if r["killed_with"])
        print(f"| {name} | {st.mean(pts):.2f} ± {se:.2f} | {sum(r['place'] == 1 for r in rs)} | {sum(r['place'] <= 3 for r in rs)} | "
              f"{st.mean(r['kills'] for r in rs):.2f} | {st.median(r['alive_s'] or 0 for r in rs):.0f} | "
              f"{st.mean(r['dealt'] for r in rs):.0f} | {st.mean(r['taken'] for r in rs):.0f} | "
              f"{100 * hits / shots if shots else 0:.0f}% | {100 * still:.0f}% | {100 * laser / taken:.0f}% | "
              f"{dict(died.most_common(3))} | {sum(r['late'] for r in rs)} |")


if __name__ == "__main__":
    main()
