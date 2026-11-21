"""Aggregate failure report over a batch of live matches.

    python3 scripts/report.py b1            # uses logs/runs/b1-*.txt to find the recordings
    python3 scripts/report.py b1 > logs/report-b1.md

Per match: place, survival, damage, accuracy, weapons, cubes, stuck time, zone damage, killer and
a death classification. Then aggregates and the most common failure patterns.
"""
import collections
import glob
import json
import math
import os
import re
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_match(rec_path, replay_path):
    ins, outs = [], {}
    for line in open(rec_path):
        try:
            d = json.loads(line)
        except ValueError:
            continue  # truncated line
        m = d["msg"]
        if d["dir"] == "in":
            ins.append(m)
        elif "tick" in m:
            outs[m["tick"]] = m
    names, kinds = {}, {}
    if replay_path and os.path.exists(replay_path):
        for line in open(replay_path):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "header":
                for mm in d.get("mechs", []):
                    names[mm["id"]] = mm.get("name")
                for i, b in enumerate(d.get("bots", [])):
                    kinds[i] = b.replace("house:", "")
                break
    return ins, outs, names, kinds


def analyse(ins, outs, names, kinds):
    obs = [m for m in ins if m.get("type") == "obs"]
    end = next((m for m in ins if m.get("type") == "end"), None)
    res = next((m for m in ins if m.get("type") == "results"), None)
    if not obs or not end:
        return None
    me_id = obs[0]["self"]["id"]
    last = obs[-1]
    r = {"place": end["place"], "kills": end["kills"], "t_end": last["t"], "alive_at_end": last["alive"],
         "won": end["place"] == 1}
    taken = collections.Counter()
    dealt = collections.Counter()
    by = collections.Counter()
    rear = 0.0
    hits = collections.Counter()
    shots = collections.Counter()
    fails = collections.Counter()
    got_weapon = {}
    prev = None
    stuck = 0
    still = 0
    fight_ticks = 0
    multi_ticks = 0
    zone_out = 0
    max_seen = 0
    cubes = 0
    labels = collections.Counter()
    for o in obs:
        s = o["self"]
        a = outs.get(o["tick"], {})
        lbl = ((a.get("debug") or {}).get("label") or "?").split(" ")[0]
        labels[lbl] += 1
        enemies = [m for m in o["mechs"] if m["kind"] == "mech"]
        max_seen = max(max_seen, len(enemies))
        if lbl == "fight":
            fight_ticks += 1
            if len(enemies) >= 2:
                multi_ticks += 1
        if not s["in_zone"]:
            zone_out += 1
        cubes = max(cubes, s["cubes"])
        if math.hypot(s["vel"][0], s["vel"][1]) < 1.0:
            still += 1
        mv = a.get("move", [0, 0])
        if math.hypot(*mv) > 0.5 and math.hypot(s["vel"][0], s["vel"][1]) < 1.5:
            stuck += 1
        for w in s["weapons"]:
            if w and w["kind"] not in got_weapon:
                got_weapon[w["kind"]] = o["t"]
        if prev:
            pw = {w["kind"]: w for w in prev["self"]["weapons"] if w}
            for w in s["weapons"]:
                if not w or w["kind"] not in pw:
                    continue
                p = pw[w["kind"]]
                if w["kind"] == "gun" and "ammo" in w and "ammo" in p and w["ammo"] < p["ammo"]:
                    shots["gun"] += p["ammo"] - w["ammo"]
                elif w["kind"] != "gun" and w["cooldown"] > p["cooldown"] + 0.05:
                    shots[w["kind"]] += 1
        for e in o["events"]:
            if e["type"] == "damaged":
                taken[e["weapon"]] += e["amount"]
                by[e.get("by")] += e["amount"]
                if e.get("rear"):
                    rear += e["amount"]
            elif e["type"] == "hit":
                dealt[e["weapon"]] += e["amount"]
                hits[e["weapon"]] += 1
            elif e["type"] == "take_failed":
                fails[e["reason"]] += 1
        prev = o

    # Death context: last 10 s.
    t0 = last["t"] - 10
    window = [o for o in obs if o["t"] >= t0]
    w_taken, w_by = collections.Counter(), collections.Counter()
    for o in window:
        for e in o["events"]:
            if e["type"] == "damaged":
                w_taken[e["weapon"]] += e["amount"]
                w_by[e.get("by")] += e["amount"]
    hp10 = window[0]["self"]["hp"]
    seen10 = max(len([m for m in o["mechs"] if m["kind"] == "mech"]) for o in window)
    attackers = [k for k in w_by if k is not None]
    killer = max(attackers, key=lambda k: w_by[k]) if attackers else None
    if r["won"]:
        cause = "won"
    elif last["self"]["hp"] > 0 and end["place"] > 1 and not w_taken:
        cause = "survived to time / unknown"
    elif w_taken.get("zone", 0) > 0.4 * sum(w_taken.values()):
        cause = "zone"
    elif len(attackers) >= 2 or seen10 >= 3:
        cause = "outnumbered (2+ attackers)"
    else:
        cause = "lost 1v1"
    tot_taken = sum(taken.values())
    r.update({
        "dealt": sum(dealt.values()), "dealt_w": dict(dealt), "taken": tot_taken, "taken_w": dict(taken),
        "rear_pct": 100 * rear / tot_taken if tot_taken else 0, "zone_dmg": taken.get("zone", 0),
        "zone_out_s": zone_out / 10, "still_pct": 100 * still / len(obs), "stuck_s": stuck / 10, "fight_s": fight_ticks / 10,
        "multi_fight_s": multi_ticks / 10, "max_seen": max_seen, "cubes": cubes,
        "weapons": got_weapon, "take_fail": dict(fails), "shots": dict(shots), "hits": dict(hits),
        "cause": cause, "killer": killer,
        "killer_name": (kinds.get(killer) or names.get(killer)) if killer is not None and not r["won"] else None,
        "attackers10": len(attackers), "hp10": hp10, "seen10": seen10, "labels": dict(labels),
        "killer_weapons": None,
    })
    if res:
        for x in res.get("mechs", []):
            names.setdefault(x["id"], x.get("name"))
        if killer is not None and not r["won"]:
            r["killer_name"] = kinds.get(killer) or names.get(killer) or f"id{killer}"
        rows = sorted(res.get("mechs", []), key=lambda x: x["place"])
        r["winner"] = (rows[0]["name"], kinds.get(rows[0]["id"]), rows[0]["kills"], rows[0]["cubes"], rows[0]["damage"]) if rows else None
        r["field_dmg_median"] = st.median([x["damage"] for x in rows]) if rows else 0
        r["field_cubes_top3"] = [x["cubes"] for x in rows[:3]]
    # last-moments trace for the report
    r["trace"] = []
    for o in [o for o in obs if o["t"] > last["t"] - 8][::10]:
        a = outs.get(o["tick"], {})
        en = [(m["id"], round(math.dist(m["pos"][:2], o["self"]["pos"][:2]))) for m in o["mechs"] if m["kind"] == "mech"]
        r["trace"].append(f"t={o['t']:.0f} hp={o['self']['hp']:.0f} [{(a.get('debug') or {}).get('label', '')}] "
                          f"in_zone={o['self']['in_zone']} enemies(id,m)={en}")
    return r


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "b1"
    runs = sorted(glob.glob(os.path.join(ROOT, "logs", "runs", f"{tag}-*.txt")), key=lambda p: int(re.findall(r"-(\d+)\.txt", p)[0]))
    matches = []
    for p in runs:
        txt = open(p).read()
        rec = re.search(r"recording to (\S+)", txt)
        rep = re.search(r"recording replay to (\S+)", txt)
        if not rec:
            continue
        ins, outs, names, kinds = load_match(rec.group(1), rep.group(1) if rep else None)
        a = analyse(ins, outs, names, kinds)
        if a:
            a["file"] = os.path.relpath(rec.group(1), ROOT)
            a["replay"] = os.path.relpath(rep.group(1), ROOT) if rep else None
            a["run"] = os.path.basename(p)
            matches.append(a)
    if not matches:
        print("no completed matches")
        return
    n = len(matches)
    pts = [16 - m["place"] for m in matches]
    P = print
    P(f"# Live batch report `{tag}`: {n} matches, bot `BasePolicy v0`\n")
    P("Opponents: play-server fill bots (free-for-all, compressed timeline), not the practice squad.\n")
    P("## Summary\n")
    P(f"- **Mean points (16 − place): {st.mean(pts):.2f}**  (median place {st.median([m['place'] for m in matches])}, "
      f"wins {sum(m['won'] for m in matches)}, top-3 {sum(m['place'] <= 3 for m in matches)}, "
      f"bottom-half {sum(m['place'] > 8 for m in matches)})")
    P(f"- Survival time: median {st.median([m['t_end'] for m in matches]):.0f} s")
    P(f"- Damage dealt/taken per match: {st.mean([m['dealt'] for m in matches]):.0f} / {st.mean([m['taken'] for m in matches]):.0f}")
    P(f"- Kills per match: {st.mean([m['kills'] for m in matches]):.2f}; cubes per match: {st.mean([m['cubes'] for m in matches]):.2f}")
    tk = collections.Counter()
    for m in matches:
        tk.update(m["taken_w"])
    P(f"- **Mechanism metrics:** deaths before 60 s: {sum((not m['won']) and m['t_end'] < 60 for m in matches)}/{n}; "
      f"time stationary: {st.mean([m['still_pct'] for m in matches]):.0f}%; "
      f"laser share of damage taken: {100 * tk.get('laser', 0) / max(sum(tk.values()), 1):.0f}%; "
      f"stuck {st.mean([m['stuck_s'] for m in matches]):.1f} s/match; zone deaths {sum(m['cause'] == 'zone' for m in matches)}")
    causes = collections.Counter(m["cause"] for m in matches)
    P(f"- **Outcome / cause of death:** " + ", ".join(f"{k} ×{v}" for k, v in causes.most_common()))
    P("")
    P("## Per match\n")
    P("| # | place | pts | died/end t | alive then | dealt | taken | zone dmg | stuck s | fight s (2+ seen) | 2nd weapon @t | cubes | cause | killer |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, m in enumerate(matches, 1):
        w2 = ", ".join(f"{k}@{v:.0f}" for k, v in m["weapons"].items() if k != "gun") or "none"
        P(f"| {i} | {m['place']} | {16 - m['place']} | {m['t_end']:.0f} | {m['alive_at_end']} | {m['dealt']:.0f} | "
          f"{m['taken']:.0f} | {m['zone_dmg']:.0f} | {m['stuck_s']:.1f} | {m['fight_s']:.0f} ({m['multi_fight_s']:.0f}) | "
          f"{w2} | {m['cubes']} | {m['cause']} | {m['killer_name'] or '-'} |")
    P("")
    P("## Combat\n")
    shots, hits, dealt = collections.Counter(), collections.Counter(), collections.Counter()
    taken = collections.Counter()
    for m in matches:
        shots.update(m["shots"]); hits.update(m["hits"]); dealt.update(m["dealt_w"]); taken.update(m["taken_w"])
    for w in sorted(set(shots) | set(hits)):
        acc = f"{100 * hits[w] / shots[w]:.0f}%" if shots[w] else "n/a"
        P(f"- `{w}`: shots {shots[w]}, hits {hits[w]} (hit rate {acc}; hits include drones/crates), damage {dealt[w]:.0f}")
    P(f"- Damage taken by weapon (all matches): " + ", ".join(f"{k} {v:.0f}" for k, v in taken.most_common()))
    P(f"- Rear-hit share of damage taken: {st.mean([m['rear_pct'] for m in matches]):.0f}%")
    P(f"- Time labelled 'fight': {sum(m['fight_s'] for m in matches):.0f} s, of which with 2+ enemies visible: "
      f"{sum(m['multi_fight_s'] for m in matches):.0f} s")
    P("")
    P("## Movement, loot, zone\n")
    P(f"- Stuck (commanded move, speed < 1.5 m/s): {sum(m['stuck_s'] for m in matches):.0f} s total, "
      f"{st.mean([m['stuck_s'] for m in matches]):.1f} s per match")
    got2 = [m for m in matches if any(k != 'gun' for k in m['weapons'])]
    P(f"- Got a 2nd weapon in {len(got2)}/{n} matches; kinds: "
      + str(collections.Counter(k for m in matches for k in m['weapons'] if k != 'gun')))
    tf = collections.Counter()
    for m in matches:
        tf.update(m["take_fail"])
    P(f"- take_failed reasons: {dict(tf)}")
    P(f"- Zone damage total {sum(m['zone_dmg'] for m in matches):.0f}; time outside zone {sum(m['zone_out_s'] for m in matches):.0f} s")
    P(f"- Cubes collected: {[m['cubes'] for m in matches]} vs top-3 finishers' cubes: {[m.get('field_cubes_top3') for m in matches]}")
    lab = collections.Counter()
    for m in matches:
        lab.update(m["labels"])
    tot = sum(lab.values())
    P(f"- Decision mix: " + ", ".join(f"{k} {100 * v / tot:.0f}%" for k, v in lab.most_common()))
    P("")
    P("## Last 8 s of every loss\n")
    for i, m in enumerate(matches, 1):
        if m["won"]:
            continue
        P(f"**Match {i}**: place {m['place']}, cause *{m['cause']}*, killer {m['killer_name']} "
          f"(attackers in last 10 s: {m['attackers10']}, max enemies seen: {m['seen10']}, HP 10 s before: {m['hp10']:.0f}). "
          f"Replay: `{m['replay']}`")
        P("```")
        for line in m["trace"]:
            P(line)
        P("```")


if __name__ == "__main__":
    main()
