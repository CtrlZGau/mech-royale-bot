"""Batch runner: many seeds in parallel, summary metrics, paired A/B.

    python3 sim/run.py --seeds 1-48 --jobs 20 -- python3 -u bot/main.py
    python3 sim/run.py --seeds 1-48 --ab "python3 -u /tmp/a/bot/main.py" --ab "python3 -u bot/main.py"
    python3 sim/run.py --seeds 1-48 --param h0=1 --param aim_a=1.5 -- python3 -u bot/main.py
    python3 sim/run.py --seeds 1-8 --replay -- python3 -u bot/main.py     # logs/sim/replay-<seed>.jsonl

Score = mean(16 - place) of our mech, exactly like the practice board.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import shlex
import statistics as st
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim import server  # noqa: E402


def parse_seeds(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def _job(args):
    seed, cmd, params, opts = args
    try:
        r = server.run_match(seed, [cmd], params, record=opts.get("replay", False),
                             replay_dir=os.path.join(ROOT, "logs", "sim"), no_timeout=opts.get("no_timeout", False),
                             budget_ms=opts.get("budget_ms"), stderr_dir=opts.get("stderr_dir"),
                             trace_dir=os.path.join(ROOT, "logs", "sim") if opts.get("trace") else None)
    except Exception as e:  # keep the batch going
        import traceback
        return {"seed": seed, "error": repr(e), "tb": traceback.format_exc()}
    s = r["slots"][0]
    me = r["result"]["mechs"][s]
    house = [m for m in r["result"]["mechs"] if m["id"] != s]
    return {"seed": seed, "place": me["place"], "kills": me["kills"], "t_end": r["t_end"], "death_t": me["death_t"],
            "dealt": me["damage_by_weapon"], "taken": me["taken_by_weapon"], "stationary_s": me["stationary_s"],
            "alive_s": me["alive_s"], "shots": me["shots"], "hits": me["hits"], "killed_with": me["killed_with"],
            "loadout": me["loadout"], "cubes": me["cubes_max"], "bot": r["bots"][str(s)] if str(s) in r["bots"] else r["bots"].get(s),
            "wall_s": r["wall_s"], "problems": r["problems"],
            "house_hits": {k: sum(m["hits"].get(k, 0) for m in house) for k in ("gun", "laser", "lightning", "mortar")},
            "house_shots": {k: sum(m["shots"].get(k, 0) for m in house) for k in ("gun", "laser", "lightning", "mortar")}}


def run_batch(seeds, cmd, params, jobs, opts):
    tasks = [(sd, cmd, params, opts) for sd in seeds]
    if jobs <= 1:
        return [_job(t) for t in tasks]
    with mp.get_context("fork").Pool(jobs) as pool:
        return list(pool.imap(_job, tasks))


def summarize(rows, label=""):
    ok = [r for r in rows if "error" not in r]
    errs = [r for r in rows if "error" in r]
    pts = [16 - r["place"] for r in ok]
    lines = []
    if not ok:
        return "no results: " + "; ".join(r["error"] for r in errs[:3])
    surv = [r["death_t"] if r["death_t"] is not None else r["t_end"] for r in ok]

    def agg(key):
        tot = {}
        for r in ok:
            for k, v in r[key].items():
                tot[k] = tot.get(k, 0) + v
        return {k: round(v / len(ok)) for k, v in sorted(tot.items(), key=lambda kv: -kv[1])}
    taken = agg("taken")
    tt = sum(taken.values()) or 1
    shots = {}
    hits = {}
    for r in ok:
        for k, v in r["shots"].items():
            shots[k] = shots.get(k, 0) + v
        for k, v in r["hits"].items():
            hits[k] = hits.get(k, 0) + v
    acc = {k: round(hits.get(k, 0) / v, 2) for k, v in shots.items() if v}
    hs = {k: sum(r["house_shots"][k] for r in ok) for k in ("gun", "laser", "lightning", "mortar")}
    hh = {k: sum(r["house_hits"][k] for r in ok) for k in ("gun", "laser", "lightning", "mortar")}
    hacc = {k: round(hh[k] / hs[k], 2) for k in hs if hs[k]}
    late = sum((r["bot"] or {}).get("late", 0) for r in ok)
    ans = sum((r["bot"] or {}).get("answered", 0) for r in ok)
    se = st.pstdev(pts) / max(1, len(pts)) ** 0.5
    lines.append(f"{label}score {st.mean(pts):.2f} ± {se:.2f} (SE)  n={len(ok)}" + (f"  ERRORS {len(errs)}" if errs else ""))
    lines.append(f"  places {sorted(r['place'] for r in ok)}")
    lines.append(f"  kills/match {st.mean(r['kills'] for r in ok):.2f}   median survival {st.median(surv):.0f} s   deaths<60s {sum(1 for s in surv if s < 60)}/{len(ok)}"
                 f"   zone deaths {sum(1 for r in ok if r['killed_with'] == 'zone')}")
    lines.append(f"  dealt/match {agg('dealt')}   taken/match {taken}   laser share of taken {taken.get('laser', 0) / tt:.0%}")
    lines.append(f"  our accuracy {acc}   house accuracy (all targets) {hacc}")
    lines.append(f"  stationary {sum(r['stationary_s'] for r in ok) / max(1e-9, sum(r['alive_s'] for r in ok)):.0%}   cubes {st.mean(r['cubes'] for r in ok):.1f}"
                 f"   2nd weapon {sum(1 for r in ok if len(r['loadout']) > 1)}/{len(ok)}   unanswered obs {late}/{late + ans}")
    probs = [p for r in ok for p in r["problems"]]
    if probs:
        lines.append(f"  protocol problems: {probs[:5]}")
    for r in errs[:3]:
        lines.append(f"  seed {r['seed']} error: {r['tb'][-600:]}")
    return "\n".join(lines)


def bootstrap_diff(a, b, n=5000, seed=1):
    rng = random.Random(seed)
    d = [x - y for x, y in zip(a, b)]
    k = len(d)
    means = sorted(sum(d[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return st.mean(d), means[int(0.025 * n)], means[int(0.975 * n)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", default="1-48")
    ap.add_argument("--jobs", type=int, default=min(20, os.cpu_count() or 4))
    ap.add_argument("--param", action="append", default=[], help="house/squad parameter k=v (see sim/house.py DEFAULTS)")
    ap.add_argument("--ab", action="append", default=[], help="bot command (quoted); give twice for a paired A/B")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--no-timeout", action="store_true")
    ap.add_argument("--budget-ms", type=float, default=None)
    ap.add_argument("--json", default=None, help="write per-match rows to this file")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    seeds = parse_seeds(a.seeds)
    params = {}
    for kv in a.param:
        k, v = kv.split("=", 1)
        params[k] = float(v)
    opts = {"replay": a.replay, "trace": a.trace, "no_timeout": a.no_timeout, "budget_ms": a.budget_ms,
            "stderr_dir": os.path.join(ROOT, "logs", "sim", "stderr")}
    os.makedirs(opts["stderr_dir"], exist_ok=True)
    cmds = [shlex.split(c) for c in a.ab]
    if a.cmd:
        cmds.append(a.cmd[1:] if a.cmd[0] == "--" else a.cmd)
    if not cmds:
        ap.error("give a bot command after -- or with --ab")
    results = []
    for i, cmd in enumerate(cmds):
        t0 = time.time()
        rows = run_batch(seeds, cmd, params, a.jobs, opts)
        results.append(rows)
        label = f"[{chr(65 + i)}] " if len(cmds) > 1 else ""
        print(f"{label}{' '.join(cmd)}   params {params or 'default'}   ({time.time() - t0:.0f} s)")
        print(summarize(rows, label))
        if not a.quiet:
            print("  per seed: " + " ".join(f"{r['seed']}:{16 - r['place']}" for r in rows if "error" not in r))
        sys.stdout.flush()
    if len(results) == 2:
        A = {r["seed"]: 16 - r["place"] for r in results[0] if "error" not in r}
        B = {r["seed"]: 16 - r["place"] for r in results[1] if "error" not in r}
        common = sorted(set(A) & set(B))
        m, lo, hi = bootstrap_diff([A[s] for s in common], [B[s] for s in common])
        print(f"A - B = {m:+.2f}  (95% CI {lo:+.2f} .. {hi:+.2f}, paired over {len(common)} seeds)")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"params": params, "cmds": cmds, "rows": results}, f)


if __name__ == "__main__":
    main()
