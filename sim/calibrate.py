"""Calibration: run every known bot version on the same seeds for one squad-parameter set and
print sim score vs practice score (sim.md §5).

    python3 sim/calibrate.py --versions DIR [--seeds 1-48] [--param k=v ...]

DIR holds one folder per version with its bot/ tree (export with
`git archive <ref> bot | tar -x -C DIR/<name>`): v0 (tag v0), v1.1 (6fd8444), v1.2 (ddd5e45),
v1.3 (d57e515). The kit example bot is always included.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim.run import parse_seeds, run_batch  # noqa: E402

def _load_targets():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "practice_scores.json")
    try:
        d = json.load(open(p))
    except (OSError, ValueError):
        d = {}
    return {k: d.get(k) for k in ("v0", "v1.1", "v1.2", "v1.3", "example")}


PRACTICE = _load_targets()  # practice results are kept locally (logs/practice_scores.json, untracked)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", required=True)
    ap.add_argument("--seeds", default="1-48")
    ap.add_argument("--jobs", type=int, default=20)
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--only", default=None, help="comma list of versions")
    a = ap.parse_args()
    params = {}
    for kv in a.param:
        k, v = kv.split("=", 1)
        params[k] = float(v)
    seeds = parse_seeds(a.seeds)
    names = [n for n in PRACTICE if n == "example" or os.path.isdir(os.path.join(a.versions, n))]
    if a.only:
        names = [n for n in names if n in a.only.split(",")]
    t0 = time.time()
    row = {}
    for n in names:
        cmd = ["python3", "-u", os.path.join(ROOT, "kit", "python", "example_bot.py")] if n == "example" else \
            ["python3", "-u", os.path.join(a.versions, n, "bot", "main.py")]
        rows = [r for r in run_batch(seeds, cmd, params, a.jobs, {"stderr_dir": None}) if "error" not in r]
        pts = [16 - r["place"] for r in rows]
        row[n] = (st.mean(pts), st.pstdev(pts) / max(1, len(pts)) ** 0.5, st.mean(r["kills"] for r in rows),
                  st.median([r["death_t"] or r["t_end"] for r in rows]))
    err = [abs(row[n][0] - PRACTICE[n]) for n in row if PRACTICE.get(n) is not None]
    print(f"params {params or 'default'}  seeds {a.seeds}  ({time.time() - t0:.0f} s)")
    print("  " + "  ".join(f"{n} {row[n][0]:.2f}±{row[n][1]:.2f} (k{row[n][2]:.1f} s{row[n][3]:.0f}"
                           + (f" vs {PRACTICE[n]})" if PRACTICE.get(n) is not None else ")") for n in row))
    print(f"  mean |sim - practice| {st.mean(err):.2f}   max {max(err):.2f}")


if __name__ == "__main__":
    main()
