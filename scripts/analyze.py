"""Failure report for a recorded live match (logs/live-*.jsonl from scripts/live.py).

    python3 scripts/analyze.py              # newest recording
    python3 scripts/analyze.py logs/live-20261121-142241.jsonl

Prints: result, HP timeline, damage taken by weapon / attacker / rear, damage dealt, the last
seconds before death, zone exposure, time stuck, and take failures.
"""
import collections
import glob
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path):
    ins, outs = [], {}
    for line in open(path):
        d = json.loads(line)
        m = d["msg"]
        if d["dir"] == "in":
            ins.append(m)
        elif "tick" in m:
            outs[m["tick"]] = m
    return ins, outs


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(ROOT, "logs", "live-*.jsonl")))[-1]
    ins, outs = load(path)
    obs = [m for m in ins if m.get("type") == "obs"]
    end = next((m for m in ins if m.get("type") == "end"), None)
    res = next((m for m in ins if m.get("type") == "results"), None)
    print(f"file: {path}\nobs: {len(obs)}  answered: {sum(o['tick'] in outs for o in obs)}")
    if not obs:
        return
    last = obs[-1]
    print(f"RESULT: place={end and end.get('place')} kills={end and end.get('kills')} "
          f"died at t={last['t']:.1f}s alive_then={last['alive']}  score={16 - end['place'] if end else '?'}")

    print("\nHP timeline (every 10 s):")
    nxt = 0
    for o in obs:
        if o["t"] >= nxt:
            s = o["self"]
            enemies = [m for m in o["mechs"] if m["kind"] == "mech"]
            ws = [w["kind"] for w in s["weapons"] if w]
            print(f"  t={o['t']:5.1f} hp={s['hp']:6.0f}/{s['max_hp']:.0f} e={s['energy']:3.0f} cubes={s['cubes']} "
                  f"in_zone={s['in_zone']} seen_mechs={len(enemies)} weapons={ws} alive={o['alive']}")
            nxt += 10

    taken_w, taken_by, rear, dealt_w = collections.Counter(), collections.Counter(), [0, 0], collections.Counter()
    zone_s, kills, hits_on = 0.0, [], collections.Counter()
    fails = collections.Counter()
    for o in obs:
        if not o["self"]["in_zone"]:
            zone_s += 0.1
        for e in o["events"]:
            if e["type"] == "damaged":
                taken_w[e["weapon"]] += e["amount"]
                taken_by[e.get("by")] += e["amount"]
                rear[bool(e.get("rear"))] += e["amount"]
            elif e["type"] == "hit":
                dealt_w[e["weapon"]] += e["amount"]
                hits_on["mech" if e["target"] < 100 else "npc" if e["target"] < 1000 else "crate"] += e["amount"]
            elif e["type"] == "kill":
                kills.append((o["t"], e["target"]))
            elif e["type"] == "take_failed":
                fails[e["reason"]] += 1
    tot = sum(taken_w.values()) or 1
    print(f"\nDamage taken: {sum(taken_w.values()):.0f}  by weapon {dict(taken_w)}")
    print(f"  by attacker: {dict(taken_by.most_common(6))}")
    print(f"  rear hits: {rear[1]:.0f} ({100 * rear[1] / tot:.0f}% of damage)   outside zone: {zone_s:.1f}s")
    print(f"Damage dealt: {sum(dealt_w.values()):.0f}  by weapon {dict(dealt_w)}  on {dict(hits_on)}  kills {kills}")
    print(f"take failures: {dict(fails)}")

    # Stuck: commanded move but low speed.
    stuck = 0
    for o in obs:
        a = outs.get(o["tick"], {})
        mv = a.get("move", [0, 0])
        if math.hypot(*mv) > 0.5 and math.hypot(o["self"]["vel"][0], o["self"]["vel"][1]) < 1.5:
            stuck += 1
    print(f"commanded-move-but-not-moving: {stuck * 0.1:.1f}s")

    print("\nLast 6 s before the end:")
    for o in [o for o in obs if o["t"] > last["t"] - 6][::5]:
        s = o["self"]
        a = outs.get(o["tick"], {})
        lbl = (a.get("debug") or {}).get("label", "")
        en = [(m["id"], round(math.dist(m["pos"][:2], s["pos"][:2])), m.get("weapons")) for m in o["mechs"] if m["kind"] == "mech"]
        dmg = [(e["weapon"], round(e["amount"]), e.get("by"), "R" if e.get("rear") else "") for e in o["events"] if e["type"] == "damaged"]
        print(f"  t={o['t']:5.1f} hp={s['hp']:5.0f} [{lbl}] enemies={en} dmg={dmg}")
    if res:
        print("\nresults:", json.dumps(res)[:800])


if __name__ == "__main__":
    main()
