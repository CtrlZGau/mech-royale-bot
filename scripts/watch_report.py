"""Per-team free-for-all report from recorded broadcast matches (logs/watch/*-seed*.jsonl, mechreplay/1).
    python3 scripts/watch_report.py [files...]      (default: every recorded match)
Placement = order of disappearance (last mech standing = 1st); points = 16 - place, as in the final.
"""
import collections
import glob
import json
import math
import os
import statistics as st
import sys

WEAP = None


def load(path):
    head, frames, chan = None, [], {}
    for line in open(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "channel":
            chan = d
        elif d.get("type") == "header":
            head = d
        elif "m" in d and "t" in d:
            frames.append(d)
    return chan, head, frames


def match_stats(head, frames):
    names = {m["id"]: m["name"] for m in head["mechs"]}
    kinds = head.get("weapon_kinds") or ["gun", "mortar", "laser", "lightning"]
    last_seen, first_t = {}, frames[0]["t"]
    per = {i: collections.defaultdict(float) for i in names}
    prev = {}
    for f in frames:
        rows = {r[0]: r for r in f["m"]}
        alive = list(rows.values())
        for i, r in rows.items():
            last_seen[i] = f["t"]
            s = per[i]
            s["frames"] += 1
            if i in prev:
                p = prev[i]
                step = math.hypot(r[1] - p[1], r[2] - p[2])
                s["moved"] += step
                if step < 0.15:
                    s["still"] += 1
            if not (r[8] & 1):
                s["air"] += 1
            if r[8] & 4:
                s["charging"] += 1
            others = [math.hypot(o[1] - r[1], o[2] - r[2]) for o in alive if o[0] != i]
            if others:
                s["nearest_sum"] += min(others)
                s["within100"] += sum(1 for d in others if d < 100) > 0
            z = f.get("z")
            if z and math.hypot(r[1] - z[0], r[2] - z[1]) > z[2]:
                s["outzone"] += 1
            s["cubes_max"] = max(s["cubes_max"], r[9])
            s["maxhp"] = max(s["maxhp"], r[10])
            w = tuple(sorted(kinds[k] for k in (r[11], r[12]) if 0 <= k < len(kinds)))
            if len(w) > 1 and not s.get("second_t"):
                s["second_t"] = (f["t"] - first_t) / head["tick_hz"]
            s["loadout"] = w
            if i in prev and r[6] < prev[i][6]:
                s["hp_lost"] += prev[i][6] - r[6]
        prev = rows
    end = frames[-1]["t"]
    order = sorted(names, key=lambda i: (last_seen.get(i, -1), i))  # earliest gone first
    out = {}
    for rank, i in enumerate(reversed(order), 1):
        s = per[i]
        fr = max(1, s["frames"])
        out[names[i]] = {
            "place": rank, "pts": 16 - rank, "alive_s": (last_seen.get(i, first_t) - first_t) / head["tick_hz"],
            "moved_per_min": s["moved"] / max(1, fr * head["frame_every"] / head["tick_hz"] / 60),
            "still": s["still"] / fr, "air": s["air"] / fr, "charging": s["charging"] / fr,
            "nearest": s["nearest_sum"] / fr, "near100": s["within100"] / fr, "outzone": s["outzone"] / fr,
            "cubes": s["cubes_max"], "maxhp": s["maxhp"], "second_s": s.get("second_t"), "loadout": s["loadout"],
            "still_alive_at_end": last_seen.get(i) == end,
        }
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    every = "--all" in sys.argv  # default: showdown matches only
    files = args or sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "watch", "*-seed*.jsonl")))
    best = {}  # one file per match seed: the most complete recording (a reconnect starts a second file)
    for p in files:
        seed = p.rsplit("seed", 1)[-1]
        if seed not in best or os.path.getsize(p) > os.path.getsize(best[seed]):
            best[seed] = p
    files = sorted(best.values())
    teams = collections.defaultdict(list)
    done = 0
    for p in files:
        chan, head, frames = load(p)
        if not head or len(frames) < 50:
            continue
        if not every and "Showdown" not in (chan.get("title") or ""):
            continue
        ms = match_stats(head, frames)
        finished = sum(1 for v in ms.values() if v["still_alive_at_end"]) <= 1
        done += finished
        print(f"{os.path.basename(p)}: {chan.get('title','')} match {chan.get('match')}/{chan.get('of')}  "
              f"{'finished' if finished else 'IN PROGRESS'}  t_end {frames[-1]['t']/head['tick_hz']:.0f}s")
        for name, v in sorted(ms.items(), key=lambda kv: kv[1]["place"]):
            print(f"  {v['place']:2d}. {name:18s} alive {v['alive_s']:5.0f}s  cubes {v['cubes']:2.0f}  maxhp {v['maxhp']:5.0f}  "
                  f"2nd weapon {('%4.0fs' % v['second_s']) if v['second_s'] else '  - '} {'/'.join(v['loadout']):15s} "
                  f"move {v['moved_per_min']:4.0f} m/min still {v['still']:4.0%} air {v['air']:4.0%} nearest {v['nearest']:4.0f} m "
                  f"enemy<100m {v['near100']:4.0%} laser-charging {v['charging']:4.0%}")
            if finished:
                teams[name].append(v)
    if teams:
        print(f"\n== totals over {done} finished matches")
        for name, vs in sorted(teams.items(), key=lambda kv: -sum(v["pts"] for v in kv[1])):
            print(f"  {name:18s} pts {sum(v['pts'] for v in vs):4d} (mean {st.mean(v['pts'] for v in vs):4.1f})  wins "
                  f"{sum(v['place'] == 1 for v in vs)}  mean alive {st.mean(v['alive_s'] for v in vs):4.0f}s  cubes "
                  f"{st.mean(v['cubes'] for v in vs):4.1f}  still {st.mean(v['still'] for v in vs):4.0%}  nearest "
                  f"{st.mean(v['nearest'] for v in vs):4.0f} m")


if __name__ == "__main__":
    main()
