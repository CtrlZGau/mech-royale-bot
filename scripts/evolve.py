"""Evolutionary search over Warden's state machine in the local sim.

    python3 scripts/evolve.py --gens 6 --pop 14 --seeds 1-48 --jobs 10
    python3 scripts/evolve.py --report            # gene-effect report from logs/evolve/history.jsonl

Fitness = practice-mode sim score (mean 16 - place) on the fixed training seeds (same seeds for every
candidate -> paired comparison). Elites carry over; children = crossover of two elites + mutation.
Every evaluation is appended to logs/evolve/history.jsonl. Validate winners on fresh seeds afterwards
(sim/run.py --ab ...), never trust the training score alone.
"""
import argparse
import json
import os
import random
import statistics as st
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "logs", "evolve", "history.jsonl")
SIM_P = ["--param", "omniscient=0", "--param", "search_s=15", "--param", "h_per_min=0.5"]

# gene: (default, kind, choices-or-range)   kind: "c" continuous [lo, hi], "d" discrete list
GENES = {
    # global rules
    "g.scout_s": (20.0, "d", [0.0, 10.0, 20.0, 35.0, 60.0]),
    "g.guard": (0, "d", [0, 1]),
    "g.back_s": (1.5, "c", [0.5, 4.0]),
    "g.lose_two_hp": (0.55, "c", [0.35, 0.75]),
    "g.lose_three_hp": (0.8, "c", [0.6, 0.95]),
    "g.lose_low_hp": (0.35, "c", [0.2, 0.5]),
    "g.closing_v": (3.0, "c", [1.0, 6.0]),
    "g.closing_r": (180.0, "c", [100.0, 240.0]),
    "g.weak_frac": (0.4, "c", [0.2, 0.6]),
    "g.weak_r": (160.0, "c", [90.0, 220.0]),
    "g.aimed_r": (190.0, "c", [100.0, 240.0]),
    "g.struck_s": (6.0, "c", [2.0, 10.0]),
    "g.lethal": (1, "d", [0, 1]),
    "g.sidestep": (1, "d", [0, 1]),
    "g.drops": (1, "d", [0, 1]),
    "g.drone_farm": (1, "d", [0, 1]),
    "g.crate_farm": (1, "d", [0, 1]),
    "g.cube_v": (75.0, "c", [30.0, 200.0]),
    # Hunt profile
    "hunt.fight_close": (60.0, "c", [35.0, 100.0]),
    "hunt.max_range": (240.0, "c", [170.0, 270.0]),
    "hunt.hp_weight": (0.2, "c", [0.03, 0.45]),
    "hunt.wounded_bonus": (100.0, "c", [0.0, 220.0]),
    "hunt.keep_bonus": (120.0, "c", [0.0, 220.0]),
    "hunt.pref_gun": (85.0, "c", [55.0, 120.0]),
    "hunt.pref_laser": (140.0, "c", [80.0, 180.0]),
    "hunt.pref_light": (28.0, "c", [18.0, 40.0]),
    "hunt.loot_r": (150.0, "c", [80.0, 240.0]),
    "hunt.farm_hp": (0.5, "c", [0.3, 0.85]),
    "hunt.cube_fight_r": (70.0, "c", [0.0, 110.0]),
    "hunt.finish_r": (14.0, "d", [0.0, 8.0, 14.0, 22.0, 32.0]),
    "hunt.peek": (0, "d", [0, 1]),
    # Scout profile
    "scout.fight_close": (60.0, "c", [35.0, 100.0]),
    "scout.loot_r": (150.0, "c", [80.0, 240.0]),
    "scout.laser_r": (240.0, "c", [120.0, 260.0]),
    "scout.peek": (1, "d", [0, 1]),
    "scout.pref_laser": (85.0, "c", [70.0, 160.0]),
}


def default_genome():
    return {k: v[0] for k, v in GENES.items()}


def mutate(g, rng, n=None, scale=0.35):
    g = dict(g)
    keys = list(GENES)
    for k in rng.sample(keys, n or rng.randint(2, 5)):
        dflt, kind, dom = GENES[k]
        if kind == "d":
            g[k] = rng.choice(dom)
        else:
            lo, hi = dom
            g[k] = round(min(hi, max(lo, g[k] + rng.gauss(0, scale * (hi - lo)))), 3)
    return g


def crossover(a, b, rng):
    return {k: (a[k] if rng.random() < 0.5 else b[k]) for k in GENES}


def to_env(g):
    return ",".join(f"{k}={g[k]}" for k in GENES if g[k] != GENES[k][0])


def evaluate(g, seeds, jobs):
    wdp = to_env(g)
    os.makedirs(os.path.join(ROOT, "logs", "evolve"), exist_ok=True)
    out = os.path.join(ROOT, "logs", "evolve", f"run-{int(time.time() * 1000)}.json")
    cmd = [sys.executable, os.path.join(ROOT, "sim", "run.py"), "--seeds", seeds, "--jobs", str(jobs), *SIM_P,
           "--json", out, "--quiet", "--", "env", "MECH_POLICY=wd", f"WD_P={wdp}", "python3", "-u",
           os.path.join(ROOT, "bot", "main.py")]
    subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    rows = json.load(open(out))["rows"][0]
    os.remove(out)
    pts = [16 - r["place"] for r in rows]
    kills = [r["kills"] for r in rows]
    return st.mean(pts), st.stdev(pts) / len(pts) ** 0.5, st.mean(kills), len(rows)


def log(rec):
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    with open(HIST, "a") as f:
        f.write(json.dumps(rec) + "\n")


def run(a):
    rng = random.Random(a.rng)
    pop = [default_genome()]
    while len(pop) < a.pop:
        pop.append(mutate(default_genome(), rng, n=rng.randint(2, 6)))
    scored = []
    for gen in range(a.gens):
        t0 = time.time()
        for g in pop:
            s, se, k, n = evaluate(g, a.seeds, a.jobs)
            rec = {"gen": gen, "score": round(s, 3), "se": round(se, 3), "kills": round(k, 2), "n": n,
                   "wd_p": to_env(g), "genome": g}
            log(rec)
            scored.append((s, g, rec))
            print(f"gen {gen} score {s:.2f} ±{se:.2f} kills {k:.2f}  {to_env(g)[:150] or '(baseline)'}", flush=True)
        scored.sort(key=lambda x: -x[0])
        # dedupe by genome
        seen, uniq = set(), []
        for s, g, r in scored:
            key = json.dumps(g, sort_keys=True)
            if key not in seen:
                seen.add(key)
                uniq.append((s, g, r))
        scored = uniq[: a.elite]
        print(f"== gen {gen} done in {time.time() - t0:.0f}s; elites: " +
              " | ".join(f"{s:.2f}" for s, _, _ in scored), flush=True)
        if gen == a.gens - 1:
            break
        elites = [g for _, g, _ in scored]
        pop = []
        while len(pop) < a.pop - 0:
            if rng.random() < 0.6 and len(elites) > 1:
                c = crossover(*rng.sample(elites, 2), rng)
                c = mutate(c, rng, n=rng.randint(1, 3))
            else:
                c = mutate(rng.choice(elites), rng)
            pop.append(c)
    print("\nTOP:")
    for s, g, r in scored:
        print(f"{s:.2f}  WD_P={to_env(g)}")


def report():
    recs = [json.loads(l) for l in open(HIST)]
    base = [r["score"] for r in recs if not r["wd_p"]]
    b = st.mean(base) if base else None
    print(f"# Evolution report: {len(recs)} evaluations; baseline (defaults) {b if b is None else round(b, 2)}\n")
    print("Best 10:")
    for r in sorted(recs, key=lambda r: -r["score"])[:10]:
        print(f"  {r['score']:.2f} (kills {r['kills']})  {r['wd_p'] or '(baseline)'}")
    print("\nGene effects (mean score when the gene differs from default vs when it is default):")
    rows = []
    for k, (dflt, kind, dom) in GENES.items():
        on = [r["score"] for r in recs if r["genome"].get(k) != dflt]
        off = [r["score"] for r in recs if r["genome"].get(k) == dflt]
        if len(on) >= 3 and off:
            hi = [r["score"] for r in recs if r["genome"].get(k) != dflt and kind == "c" and r["genome"][k] > dflt]
            lo = [r["score"] for r in recs if r["genome"].get(k) != dflt and kind == "c" and r["genome"][k] < dflt]
            rows.append((st.mean(on) - st.mean(off), k, len(on), st.mean(hi) if hi else None, st.mean(lo) if lo else None))
    for d, k, n, hi, lo in sorted(rows, key=lambda x: -x[0]):
        extra = ""
        if hi is not None or lo is not None:
            extra = f"   higher: {hi:.2f}" if hi is not None else ""
            extra += f"   lower: {lo:.2f}" if lo is not None else ""
        print(f"  {d:+.2f}  {k} (changed in {n}){extra}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gens", type=int, default=6)
    ap.add_argument("--pop", type=int, default=14)
    ap.add_argument("--elite", type=int, default=4)
    ap.add_argument("--seeds", default="1-48")
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--rng", type=int, default=7)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    report() if a.report else run(a)
