"""Profile the house bots' behaviour from god-view replays (logs/replay-*.jsonl).

    python3 scripts/opponents.py                 # every replay in logs/
    python3 scripts/opponents.py logs/replay-A.jsonl logs/replay-B.jsonl

Groups mechs by bot type (header `bots`, e.g. house:expert; we are "human") and reports, per type:
results (place, kills, death time, killer weapon), loadout and upgrades, movement (speed, boosting,
rooftops, distance to zone edge, distance to nearest mech), combat (hit ranges per weapon, who they
hit: mechs / drones / crates, whether they focus low-HP or already-damaged targets, third-partying),
retreat behaviour when low HP, aim discipline (facing the nearest enemy), laser charge habits.
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

WORLD = World(os.path.join(ROOT, "kit", "arena"))
WK = ["gun", "mortar", "laser", "lightning"]


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def fmt(xs, unit=""):
    if not xs:
        return "-"
    return f"med {st.median(xs):.0f}{unit} (p25 {q(xs, .25):.0f}, p75 {q(xs, .75):.0f}, n={len(xs)})"


class Agg:
    def __init__(self):
        self.d = collections.defaultdict(list)
        self.c = collections.defaultdict(collections.Counter)

    def add(self, k, v):
        self.d[k].append(v)

    def cnt(self, k, v, n=1):
        self.c[k][v] += n


def analyse(path, aggs):
    L = [json.loads(l) for l in open(path)]
    h = next((d for d in L if d.get("type") == "header"), None)
    res = next((d for d in L if d.get("type") == "result"), None)
    frames = [d for d in L if "m" in d]
    if not h or not res or not frames:
        return False
    kind = {i: b.replace("house:", "") for i, b in enumerate(h["bots"])}
    names = {m["id"]: m["name"] for m in h["mechs"]}
    kind = {i: ("US (" + names.get(i, "?") + ")" if k == "human" else k) for i, k in kind.items()}

    for r in res["mechs"]:
        a = aggs[kind[r["id"]]]
        a.add("place", r["place"])
        a.add("points", 16 - r["place"])
        a.add("kills", r["kills"])
        a.add("dealt", r.get("damage_dealt", 0))
        a.add("taken", r.get("damage_taken", 0))
        a.add("cubes", r.get("cubes_max", 0))
        a.add("drops", r.get("drops_taken", 0))
        a.add("weapons_taken", r.get("weapons_taken", 0))
        if r.get("first_weapon_t") is not None:
            a.add("first_weapon_t", r["first_weapon_t"])
        if r.get("death_t") is not None:
            a.add("death_t", r["death_t"])
            a.cnt("killed_with", r.get("killed_with"))
            kb = r.get("killed_by")
            a.cnt("killed_by_type", kind.get(kb, "zone/none") if kb is not None else "zone/none")
        a.cnt("loadout", "+".join(sorted(r.get("loadout") or [])))
        for u in r.get("upgrades", []):
            a.cnt("upgrades", u)
        for w, v in (r.get("damage_by_weapon") or {}).items():
            a.cnt("dmg_by_weapon", w, v)

    # Per-frame state.
    pos_hist = collections.defaultdict(list)  # id -> [(t, x, y, z, yaw, hp, maxhp, flags)]
    zone_at = {}
    for f in frames:
        zone_at[f["t"]] = f["z"]
        for m in f["m"]:
            pos_hist[m[0]].append((f["t"], m[1], m[2], m[3], m[4], m[6], m[10], m[8], m[11], m[12]))

    by_t = collections.defaultdict(dict)
    for i, hist in pos_hist.items():
        for row in hist:
            by_t[row[0]][i] = row

    ticks = sorted(by_t)
    for i, hist in pos_hist.items():
        a = aggs[kind[i]]
        prev = None
        for row in hist:
            t, x, y, z, yaw, hp, mhp, flags, s0, s1 = row
            if hp <= 0:
                break
            if prev and t - prev[0] > 0:
                dt = (t - prev[0]) / 20
                sp = math.hypot(x - prev[1], y - prev[2]) / dt
                a.add("speed", sp)
                a.cnt("moving", sp > 1.0)
            a.cnt("boosting", bool(flags & 2))
            a.cnt("grounded", bool(flags & 1))
            hag = z - WORLD.ground(x, y)
            a.cnt("rooftop(>4m above terrain)", hag > 4)
            zc = zone_at.get(t)
            if zc:
                a.add("zone_margin_m", zc[2] - math.hypot(x - zc[0], y - zc[1]))
            others = [(j, r) for j, r in by_t[t].items() if j != i and r[5] > 0]
            if others:
                j, r = min(others, key=lambda jr: math.hypot(jr[1][1] - x, jr[1][2] - y))
                dn = math.hypot(r[1] - x, r[2] - y)
                if t % 20 == 0:
                    a.add("nearest_mech_m", dn)
                if dn < 200:
                    err = abs((math.atan2(r[2] - y, r[1] - x) - yaw + math.pi) % (2 * math.pi) - math.pi)
                    a.cnt("faces_nearest(<200m, ±20°)", err < math.radians(20))
                # Retreat: radial speed relative to nearest enemy, by HP band.
                if prev and dn < 150 and t - prev[0] > 0:
                    pd = math.hypot(r[1] - prev[1], r[2] - prev[2])
                    radial = (dn - pd) / ((t - prev[0]) / 20)
                    band = "hp<40%" if hp < 0.4 * mhp else "hp>70%" if hp > 0.7 * mhp else None
                    if band:
                        a.add(f"radial_speed_vs_nearest {band} (+ = away)", radial)
            prev = row

    # Events.
    recent_hits = collections.defaultdict(list)  # dst -> [(t, src)]
    charge_start = {}
    for f in frames:
        t = f["t"]
        for e in f.get("ev", []):
            k = e["e"]
            if k == "hit":
                src, dst = e.get("src"), e.get("dst")
                if src is None or src not in kind:
                    continue
                a = aggs[kind[src]]
                tgt = "mech" if dst < 100 else "drone" if dst < 1000 else "crate"
                a.cnt("hits_on", tgt)
                if tgt == "mech":
                    a.cnt("hits_on_type", kind.get(dst))
                s, d = by_t[t].get(src), by_t[t].get(dst)
                if s and d:
                    a.add(f"hit_range_{e['w']}", math.hypot(s[1] - d[1], s[2] - d[2]))
                    if tgt == "mech":
                        a.add("target_hp_frac_when_hit", d[5] / max(d[6], 1))
                        others = [x for x in recent_hits[dst] if t - x[0] < 60 and x[1] != src]
                        a.cnt("third_party(target hit by another <3s)", bool(others))
                recent_hits[dst].append((t, src))
            elif k == "fire" and e.get("id") in kind:
                aggs[kind[e["id"]]].cnt("fires", e["w"])
            elif k == "charge" and e.get("id") in kind:
                charge_start[e["id"]] = t
            elif k == "laser" and e.get("id") in kind and e["id"] in charge_start:
                aggs[kind[e["id"]]].add("laser_charge_s", (t - charge_start.pop(e["id"])) / 20)
                aggs[kind[e["id"]]].cnt("laser_hit", bool(e.get("hits")))
            elif k == "weapon" and e.get("by") in kind and e.get("op") == "take":
                aggs[kind[e["by"]]].cnt("took", e.get("kind"))
                aggs[kind[e["by"]]].add("take_t", t / 20)
            elif k == "crate" and e.get("by") in kind:
                aggs[kind[e["by"]]].cnt("crates_broken", "n")
            elif k == "upgrade" and e.get("id") in kind:
                aggs[kind[e["id"]]].add("upgrade_t", t / 20)
    return True


def main():
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "logs", "replay-*.jsonl")))
    aggs = collections.defaultdict(Agg)
    used = [p for p in paths if analyse(p, aggs)]
    P = print
    P(f"# House-bot behaviour profile: {len(used)} replays\n")
    order = sorted(aggs, key=lambda k: -st.mean(aggs[k].d["points"]))
    P("## Results by bot type\n")
    P("| type | n | mean pts | mean place | kills | dealt | taken | cubes | death t (med) | weapons taken |")
    P("|---|---|---|---|---|---|---|---|---|---|")
    for k in order:
        d = aggs[k].d
        P(f"| {k} | {len(d['place'])} | {st.mean(d['points']):.1f} | {st.mean(d['place']):.1f} | {st.mean(d['kills']):.2f} | "
          f"{st.mean(d['dealt']):.0f} | {st.mean(d['taken']):.0f} | {st.mean(d['cubes']):.1f} | "
          f"{st.median(d['death_t']) if d['death_t'] else float('nan'):.0f} | {st.mean(d['weapons_taken']):.2f} |")
    P("")
    for k in order:
        a = aggs[k]
        d, c = a.d, a.c

        def pct(name, key=True):
            tot = sum(c[name].values())
            return f"{100 * c[name][key] / tot:.0f}%" if tot else "-"

        P(f"## {k}\n")
        P(f"- **Loadouts at end:** {dict(c['loadout'].most_common())}")
        P(f"- **Weapons taken:** {dict(c['took'])}; take time {fmt(d['take_t'], 's')}")
        P(f"- **Upgrades:** {dict(c['upgrades'].most_common(8))}")
        P(f"- **Damage by weapon:** " + ", ".join(f"{w} {v:.0f}" for w, v in c['dmg_by_weapon'].most_common()))
        P(f"- **Movement:** speed {fmt(d['speed'], ' m/s')}; moving {pct('moving')}; boosting {pct('boosting')}; "
          f"airborne {100 - float(pct('grounded').rstrip('%') or 0):.0f}%; rooftop {pct('rooftop(>4m above terrain)')}")
        P(f"- **Zone margin** (m inside the edge, − = outside): {fmt(d['zone_margin_m'], ' m')}")
        P(f"- **Distance to nearest mech:** {fmt(d['nearest_mech_m'], ' m')}")
        P(f"- **Faces nearest mech** (<200 m, ±20°): {pct('faces_nearest(<200m, ±20°)')}")
        for band in ("hp>70%", "hp<40%"):
            key = f"radial_speed_vs_nearest {band} (+ = away)"
            if d[key]:
                P(f"- **Radial speed vs nearest mech, {band}:** mean {st.mean(d[key]):+.1f} m/s (n={len(d[key])})")
        P(f"- **Hits on:** {dict(c['hits_on'])}; on mech types {dict(c['hits_on_type'].most_common())}")
        for w in WK:
            if d[f"hit_range_{w}"]:
                P(f"- **Hit range {w}:** {fmt(d[f'hit_range_{w}'], ' m')}")
        if d["target_hp_frac_when_hit"]:
            P(f"- **Target HP fraction when hit:** mean {st.mean(d['target_hp_frac_when_hit']):.2f}; "
              f"third-party hits {pct('third_party(target hit by another <3s)')}")
        P(f"- **Fires:** {dict(c['fires'])}; laser charge-to-fire {fmt([x * 1000 for x in d['laser_charge_s']], ' ms')}, laser shots that hit {pct('laser_hit')}")
        P(f"- **Crates broken:** {c['crates_broken']['n']}; upgrades at {fmt(d['upgrade_t'], 's')}")
        P(f"- **Died to:** weapon {dict(c['killed_with'])}; killer type {dict(c['killed_by_type'].most_common())}")
        P("")


if __name__ == "__main__":
    main()
