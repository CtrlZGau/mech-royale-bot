"""Deep free-for-all analysis of recorded broadcast matches (logs/watch/*-seed*.jsonl, mechreplay/1 with events).
    python3 scripts/watch_deep.py            showdown matches only
    python3 scripts/watch_deep.py --all      every recorded match (lobbies too)
Sections: per-team results, weapons/accuracy, engagements (who shoots first, who wins, at what range), movement in
fights (strafing, closing, boosting), economy (cubes, crates, drones, upgrades), and top-4 finishers vs the rest.
"""
import collections
import glob
import json
import math
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGHT_GAP = 4.0     # s without a hit between the two mechs ends an engagement
EVERY = "--all" in sys.argv


def load(path):
    out = {"chan": {}, "head": None, "frames": [], "result": None}
    for line in open(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "channel":
            out["chan"] = d
        elif t == "header":
            out["head"] = d
        elif t == "result":
            out["result"] = d
        elif "m" in d and "t" in d:
            out["frames"].append(d)
    return out


def files():
    best = {}
    for p in glob.glob(os.path.join(ROOT, "logs", "watch", "*-seed*.jsonl")):
        seed = p.rsplit("seed", 1)[-1]
        if seed not in best or os.path.getsize(p) > os.path.getsize(best[seed]):
            best[seed] = p
    return sorted(best.values())


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else float("nan")


def analyse(m):
    head, frames, res = m["head"], m["frames"], m["result"]
    hz = head["tick_hz"]
    names = {x["id"]: x["name"] for x in head["mechs"]}
    rows = {}           # tick -> {id: row}
    for f in frames:
        rows[f["t"]] = {r[0]: r for r in f["m"]}
    ticks = sorted(rows)
    per = {i: collections.defaultdict(float) for i in names}
    per_list = {i: collections.defaultdict(list) for i in names}
    hits = []           # (tick, src, dst, amt, weapon)
    for f in frames:
        for e in f.get("ev", []):
            k = e["e"]
            if k == "fire" and e.get("id") in per:
                per[e["id"]]["fire_" + e.get("w", "?")] += 1
            elif k == "hit" and e.get("src") in per:
                dst = e.get("dst")
                tgt = "mech" if dst in names else ("drone" if dst is not None and 100 <= dst < 1000 else "crate")
                per[e["src"]][f"dmg_{tgt}"] += e.get("amt", 0)
                per[e["src"]][f"hit_{e.get('w', '?')}_{tgt}"] += 1
                if tgt == "mech":
                    hits.append((e["t"], e["src"], dst, e.get("amt", 0), e.get("w", "?")))
            elif k == "laser" and e.get("id") in per:
                per[e["id"]]["laser_shots"] += 1
                per[e["id"]]["laser_hit"] += 1 if any(h in names for h in e.get("hits", [])) else 0
            elif k == "zap" and e.get("id") in per:
                per[e["id"]]["zaps"] += 1
            elif k == "pickup" and e.get("by") in per:
                per[e["by"]]["cubes_picked"] += 1
            elif k == "crate" and e.get("by") in per:
                per[e["by"]]["crates"] += 1
            elif k == "death" and e.get("by") in per and e.get("id", 0) >= 100:
                per[e["by"]]["drones"] += 1
            elif k == "upgrade" and e.get("id") in per:
                per_list[e["id"]]["ups"].append(e.get("up"))
            elif k == "weapon" and e.get("by") in per and e.get("op") == "take":
                per_list[e["by"]]["took"].append((round(e["t"] / hz, 1), e.get("kind")))
    # engagements: consecutive hits between an unordered pair, split by FIGHT_GAP
    pairs = collections.defaultdict(list)
    for h in hits:
        pairs[tuple(sorted((h[1], h[2])))].append(h)
    fights = []
    for (a, b), hs in pairs.items():
        hs.sort()
        cur = [hs[0]]
        for h in hs[1:]:
            if (h[0] - cur[-1][0]) / hz > FIGHT_GAP:
                fights.append(((a, b), cur))
                cur = []
            cur.append(h)
        fights.append(((a, b), cur))
    death = {x["id"]: x.get("death_t") for x in res["mechs"]} if res else {}
    killer = {x["id"]: x.get("killed_by") for x in res["mechs"]} if res else {}
    fight_rows = []
    for (a, b), hs in fights:
        t0, t1 = hs[0][0] / hz, hs[-1][0] / hz
        first = hs[0][1]
        dmg = collections.Counter()
        for h in hs:
            dmg[h[1]] += h[3]
        # outcome: a kill inside the fight (or within 3 s after) decides it, else the damage balance
        winner = None
        for x, y in ((a, b), (b, a)):
            if death.get(y) is not None and t0 - 0.5 <= death[y] <= t1 + 3 and killer.get(y) == x:
                winner = x
        decided = winner is not None
        if winner is None and dmg[a] != dmg[b]:
            winner = a if dmg[a] > dmg[b] else b
        # range at the first hit
        r0 = rows.get(hs[0][0]) or rows.get(hs[0][0] - 1) or rows.get(hs[0][0] + 1) or {}
        rng = math.hypot(r0[a][1] - r0[b][1], r0[a][2] - r0[b][2]) if a in r0 and b in r0 else None
        fight_rows.append({"pair": (a, b), "t0": t0, "dur": t1 - t0, "first": first, "winner": winner, "kill": decided,
                           "dmg": dict(dmg), "range": rng})
    # movement while in a fight: any hit dealt or taken by the mech within the last 2 s, opponent = last exchange partner
    last_partner = {}
    hit_ticks = collections.defaultdict(list)
    for h in hits:
        hit_ticks[h[1]].append((h[0], h[2]))
        hit_ticks[h[2]].append((h[0], h[1]))
    for i in names:
        hit_ticks[i].sort()
    prev = None
    for tk in ticks:
        cur = rows[tk]
        if prev is not None:
            dt = (tk - prev[0]) / hz
            for i, r in cur.items():
                p = prev[1].get(i)
                if not p or dt <= 0:
                    continue
                vx, vy = (r[1] - p[1]) / dt, (r[2] - p[2]) / dt
                sp = math.hypot(vx, vy)
                s = per[i]
                hl = [x for x in hit_ticks[i] if tk - 2 * hz <= x[0] <= tk]
                if hl:
                    opp = hl[-1][1]
                    o = cur.get(opp)
                    s["fight_frames"] += 1
                    s["fight_speed"] += sp
                    s["fight_air"] += 0 if (r[8] & 1) else 1
                    s["fight_boost"] += 1 if (r[8] & 2) else 0
                    s["fight_still"] += 1 if sp < 1.5 else 0
                    if o:
                        dx, dy = o[1] - r[1], o[2] - r[2]
                        d = math.hypot(dx, dy) or 1
                        radial = (vx * dx + vy * dy) / d
                        lateral = abs(-vx * dy / d + vy * dx / d)
                        s["fight_lateral"] += lateral
                        s["fight_radial"] += radial
                        s["fight_dist"] += d
                else:
                    s["calm_frames"] += 1
                    s["calm_speed"] += sp
                    s["calm_still"] += 1 if sp < 1.5 else 0
        prev = (tk, cur)
    return names, per, per_list, fight_rows, res


def main():
    paths = files()
    team = collections.defaultdict(lambda: collections.defaultdict(list))
    allfights = []
    mech_rows = []    # every mech-match, for the top-4 vs rest contrast
    used = 0
    for p in paths:
        m = load(p)
        if not m["head"] or not m["result"]:
            continue
        if not EVERY and "Showdown" not in (m["chan"].get("title") or ""):
            continue
        used += 1
        names, per, per_list, fights, res = analyse(m)
        allfights += [(names, f) for f in fights]
        for x in res["mechs"]:
            i, s, L = x["id"], per[x["id"]], per_list[x["id"]]
            ff = max(1, s["fight_frames"])
            cf = max(1, s["calm_frames"])
            row = {
                "name": x["name"], "place": x["place"], "pts": 16 - x["place"], "kills": x["kills"],
                "dealt": x["damage_dealt"], "taken": x["damage_taken"], "cubes": x["cubes_max"],
                "alive": x["death_t"] if x["death_t"] is not None else res["ticks"] / m["head"]["tick_hz"],
                "first_weapon": x.get("first_weapon_t"), "loadout": "/".join(x.get("loadout") or []),
                "laser_dmg": (x.get("damage_by_weapon") or {}).get("laser", 0),
                "gun_dmg": (x.get("damage_by_weapon") or {}).get("gun", 0),
                "light_dmg": (x.get("damage_by_weapon") or {}).get("lightning", 0),
                "gun_acc": s["hit_gun_mech"] / max(1, s["fire_gun"]) if s["fire_gun"] else None,
                "gun_acc_all": (s["hit_gun_mech"] + s["hit_gun_drone"] + s["hit_gun_crate"]) / max(1, s["fire_gun"]) if s["fire_gun"] else None,
                "laser_acc": s["laser_hit"] / s["laser_shots"] if s["laser_shots"] else None,
                "dmg_drone": s["dmg_drone"], "dmg_crate": s["dmg_crate"], "crates": s["crates"], "drones": s["drones"],
                "cubes_picked": s["cubes_picked"], "ups": L["ups"], "killed_with": x.get("killed_with"),
                "fight_share": s["fight_frames"] / max(1, s["fight_frames"] + s["calm_frames"]),
                "fight_speed": s["fight_speed"] / ff, "fight_lateral": s["fight_lateral"] / ff,
                "fight_radial": s["fight_radial"] / ff, "fight_still": s["fight_still"] / ff,
                "fight_boost": s["fight_boost"] / ff, "fight_air": s["fight_air"] / ff, "fight_dist": s["fight_dist"] / ff,
                "calm_speed": s["calm_speed"] / cf, "calm_still": s["calm_still"] / cf,
            }
            mech_rows.append(row)
            for k, v in row.items():
                team[x["name"]][k].append(v)
    print(f"# {used} {'recorded' if EVERY else 'showdown'} matches with results\n")
    if not used:
        return
    print("## Results per team (sorted by mean points)")
    print("team | n | pts | wins | kills | dealt | taken | alive s | cubes | 2nd weapon s | laser dmg share | died to")
    for name, d in sorted(team.items(), key=lambda kv: -mean(kv[1]["pts"])):
        tot = sum(d["dealt"]) or 1
        print(f"{name:18s} | {len(d['pts'])} | {mean(d['pts']):4.1f} | {sum(p == 1 for p in d['place'])} | {mean(d['kills']):.1f} | "
              f"{mean(d['dealt']):5.0f} | {mean(d['taken']):5.0f} | {mean(d['alive']):4.0f} | {mean(d['cubes']):4.1f} | "
              f"{mean(d['first_weapon']):5.1f} | {sum(d['laser_dmg']) / tot:4.0%} | {collections.Counter(k for k in d['killed_with'] if k).most_common(2)}")
    print("\n## Weapons and accuracy per team")
    print("team | loadouts | gun acc vs mechs | laser hit rate | damage gun / laser / lightning | dmg to drones / crates")
    for name, d in sorted(team.items(), key=lambda kv: -mean(kv[1]["pts"])):
        print(f"{name:18s} | {dict(collections.Counter(d['loadout']))} | {mean(d['gun_acc']):4.0%} | {mean(d['laser_acc']):4.0%} | "
              f"{mean(d['gun_dmg']):5.0f} / {mean(d['laser_dmg']):5.0f} / {mean(d['light_dmg']):4.0f} | {mean(d['dmg_drone']):4.0f} / {mean(d['dmg_crate']):4.0f}")
    print("\n## Movement: in fights vs calm (speed m/s; lateral = sideways strafe speed, radial >0 = closing in)")
    print("team | in fight % | fight speed | lateral | radial | still | boost | airborne | fight dist m | calm speed | calm still")
    for name, d in sorted(team.items(), key=lambda kv: -mean(kv[1]["pts"])):
        print(f"{name:18s} | {mean(d['fight_share']):4.0%} | {mean(d['fight_speed']):4.1f} | {mean(d['fight_lateral']):4.1f} | "
              f"{mean(d['fight_radial']):+5.1f} | {mean(d['fight_still']):4.0%} | {mean(d['fight_boost']):4.0%} | {mean(d['fight_air']):4.0%} | "
              f"{mean(d['fight_dist']):4.0f} | {mean(d['calm_speed']):4.1f} | {mean(d['calm_still']):4.0%}")
    print("\n## Economy and upgrades")
    print("team | cubes max | cubes picked | crates broken | drones killed | first upgrades (most common opening)")
    for name, d in sorted(team.items(), key=lambda kv: -mean(kv[1]["pts"])):
        firsts = collections.Counter(tuple(u[:3]) for u in d["ups"] if u)
        print(f"{name:18s} | {mean(d['cubes']):4.1f} | {mean(d['cubes_picked']):4.1f} | {mean(d['crates']):4.1f} | {mean(d['drones']):4.1f} | "
              f"{firsts.most_common(1)[0][0] if firsts else '-'}")
    # engagements
    print(f"\n## Engagements ({len(allfights)} mech-vs-mech exchanges; a kill within the fight decides it, else damage)")
    first_win = sum(1 for n, f in allfights if f["winner"] is not None and f["winner"] == f["first"])
    decided = sum(1 for n, f in allfights if f["winner"] is not None)
    print(f"first to hit wins {first_win}/{decided} ({first_win / max(1, decided):.0%}); fights ending in a kill: "
          f"{sum(f['kill'] for n, f in allfights)}")
    byrng = collections.defaultdict(lambda: [0, 0])
    for n, f in allfights:
        if f["range"] is None or f["winner"] is None:
            continue
        b = int(min(f["range"], 299) // 50) * 50
        byrng[b][0] += 1
        byrng[b][1] += f["winner"] == f["first"]
    print("opening range: " + ", ".join(f"{b}-{b + 50} m: {v[0]} fights, first hitter wins {v[1] / v[0]:.0%}" for b, v in sorted(byrng.items())))
    tw = collections.defaultdict(lambda: [0, 0, 0, 0])   # fights, won, opened, kills
    for n, f in allfights:
        for i in f["pair"]:
            nm = n[i]
            tw[nm][0] += 1
            tw[nm][1] += f["winner"] == i
            tw[nm][2] += f["first"] == i
            tw[nm][3] += f["kill"] and f["winner"] == i
    print("team | fights | won | opened (hit first) | kills from fights")
    for nm, v in sorted(tw.items(), key=lambda kv: -kv[1][1] / max(1, kv[1][0])):
        print(f"{nm:18s} | {v[0]:3d} | {v[1] / max(1, v[0]):4.0%} | {v[2] / max(1, v[0]):4.0%} | {v[3]}")
    # contrast
    top = [r for r in mech_rows if r["place"] <= 4]
    rest = [r for r in mech_rows if r["place"] > 4]
    print(f"\n## Top-4 finishers ({len(top)}) vs the rest ({len(rest)})")
    for k, lab in (("kills", "kills"), ("dealt", "damage dealt"), ("taken", "damage taken"), ("cubes", "cubes"),
                   ("first_weapon", "second weapon at s"), ("gun_acc", "gun accuracy vs mechs"), ("laser_acc", "laser hit rate"),
                   ("fight_share", "time in fights"), ("fight_lateral", "strafe speed in fights"), ("fight_radial", "closing speed in fights"),
                   ("fight_still", "still in fights"), ("fight_dist", "fight distance"), ("calm_speed", "speed when calm"),
                   ("calm_still", "still when calm"), ("crates", "crates"), ("drones", "drones"), ("dmg_drone", "damage to drones")):
        a, b = mean(r[k] for r in top), mean(r[k] for r in rest)
        pct = k in ("gun_acc", "laser_acc", "fight_share", "fight_still", "calm_still")
        fmt = (lambda v: f"{v:5.0%}") if pct else (lambda v: f"{v:7.1f}")
        print(f"  {lab:26s} top {fmt(a)}   rest {fmt(b)}")
    lo = collections.Counter(r["loadout"] for r in top)
    lr = collections.Counter(r["loadout"] for r in rest)
    print(f"  loadout                    top {dict(lo)}   rest {dict(lr)}")


if __name__ == "__main__":
    main()
