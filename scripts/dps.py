"""Combat efficiency from traces: damage we deal to mechs per second while a mech is in sight.

    python3 scripts/dps.py DIR_A [DIR_B ...]       # each DIR holds trace-<seed>.jsonl files

Per trace: "engaged time" = decisions with a visible enemy mech within 150 m. Reports per DIR:
dealt to mechs per engaged second (DPS), taken from mechs per engaged second, exchange ratio,
kills, per-weapon shots / hits / damage. With two DIRs on the same seeds, prints the paired
difference of DPS with a bootstrap 95% CI.
"""
import glob
import json
import math
import os
import random
import statistics as st
import sys


def per_trace(path):
    eng = 0.0
    dealt = taken = 0.0
    kills = 0
    wd = {}
    prev = None
    for l in open(path):
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if d.get("dir") != "in" or d["msg"].get("type") != "obs":
            continue
        o = d["msg"]
        me = o["self"]
        vis = [m for m in o["mechs"] if m["kind"] == "mech"
               and math.hypot(m["pos"][0] - me["pos"][0], m["pos"][1] - me["pos"][1]) < 150]
        if vis:
            eng += 0.1
        for e in o["events"]:
            if e["type"] == "hit" and e["target"] < 100:
                dealt += e["amount"]
                wd[e["weapon"]] = wd.get(e["weapon"], 0) + e["amount"]
            elif e["type"] == "damaged" and e.get("by") is not None and e["by"] < 100:
                taken += e["amount"]
            elif e["type"] == "kill":
                kills += 1
        prev = o
    return {"eng": eng, "dealt": dealt, "taken": taken, "kills": kills, "wd": wd}


def load_dir(dr):
    out = {}
    for p in glob.glob(os.path.join(dr, "trace-*.jsonl")):
        seed = int(os.path.basename(p)[6:-6])
        out[seed] = per_trace(p)
    return out


def main():
    dirs = sys.argv[1:]
    data = [load_dir(d) for d in dirs]
    print("| variant | matches | engaged s/match | DPS dealt (mechs) | DPS taken | exchange | kills/match | dealt by weapon/match |")
    print("|---|---|---|---|---|---|---|---|")
    for d, rows in zip(dirs, data):
        eng = sum(r["eng"] for r in rows.values()) or 1
        dealt = sum(r["dealt"] for r in rows.values())
        taken = sum(r["taken"] for r in rows.values())
        n = len(rows) or 1
        wd = {}
        for r in rows.values():
            for k, v in r["wd"].items():
                wd[k] = wd.get(k, 0) + v
        print(f"| {os.path.basename(d.rstrip('/'))} | {len(rows)} | {eng / n:.0f} | {dealt / eng:.0f} | {taken / eng:.0f} | "
              f"{dealt / max(taken, 1):.2f} | {sum(r['kills'] for r in rows.values()) / n:.2f} | "
              f"{ {k: round(v / n) for k, v in sorted(wd.items(), key=lambda kv: -kv[1])} } |")
    if len(data) >= 2:
        base = data[0]
        for d, rows in zip(dirs[1:], data[1:]):
            seeds = sorted(set(base) & set(rows))
            if not seeds:
                continue

            def dps(sel, rr):
                e = sum(rr[s]["eng"] for s in sel) or 1
                return sum(rr[s]["dealt"] for s in sel) / e

            diff = dps(seeds, rows) - dps(seeds, base)
            rng = random.Random(1)
            bs = sorted(dps(sm, rows) - dps(sm, base) for sm in
                        ([rng.choice(seeds) for _ in seeds] for _ in range(1000)))
            print(f"\n{os.path.basename(d.rstrip('/'))} - {os.path.basename(dirs[0].rstrip('/'))}: DPS {diff:+.1f} "
                  f"(95% CI {bs[25]:+.1f} .. {bs[975]:+.1f}, {len(seeds)} paired seeds)")


if __name__ == "__main__":
    main()
