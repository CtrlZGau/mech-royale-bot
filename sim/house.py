"""In-process house bots acting as one squad (sim.md §4).

Squad: shared vision of the external mech(s), no friendly fire (engine), a hunter schedule
(`hunters = h0 + h_per_kill * kills_by_ext`, everyone once zone phase >= all_phase or alive <=
all_alive), the rest roam inside the next circle, farm crates/drones/loot and shoot the
target if the squad sees it within `roam_engage` m.

Every number that matters for calibration is in DEFAULTS (override with --param k=v).
"""
from __future__ import annotations

import math

from .engine import Mech, wrap

DEFAULTS = {
    # squad
    "h0": 1,                 # hunters at start
    "h_per_kill": 1,         # extra hunters per house bot we kill
    "h_per_min": 1.0,        # extra hunters per match minute
    "all_phase": 4,          # zone phase index (0-based) from which everybody hunts
    "all_alive": 6,          # everyone hunts once alive mechs <= this
    "hunt_order": 1,         # 1: weakest difficulty hunts first; 0: nearest bots hunt first
    "omniscient": 1,         # hunters always know where the target is (else: last squad sighting)
    "search_s": 1e9,         # (omniscient=0) hunters search the last sighting this long, then roam
    "roam_frac": 1.2,        # roamers wander within roam_frac * next radius of the next centre
    "roam_engage": 25.0,    # non-hunters shoot the target if the squad sees it within this
    "hunt_engage": 200.0,    # hunters start shooting inside this range
    # combat
    "want_range": 60.0,      # preferred gun range
    "aim_a": 2.5,            # lateral aim error sigma (m) = diff_mult * (aim_a + aim_b * dist)
    "aim_b": 0.07,
    "aim_rho": 0.85,         # AR(1) correlation of the aim error per decision
    "laser_release": 0.8,    # all house bots release the laser at 0.8 s
    "fire_gate": 0.05,       # fire only when actual aim is within this (rad) of commanded aim + target width
    "retreat_frac": 0.4,     # back off below this HP fraction
    "close_frac": 0.7,       # close in above this HP fraction
    "boost_p": 0.27,
    "hop_p": 0.04,           # per-decision chance to hop in a fight (airborne 17-45 % in live replays)
    "start_weapon": 0,       # spawn with a random second weapon (else: loot it)
    "farm": 1,               # roamers break crates / shoot drones / grab cubes and loot
    "farm_aim": 0.25,        # aim-error multiplier vs crates/drones (slow, predictable targets)
    "react": 1.0,            # multiplier on per-difficulty reaction time
    # per-difficulty overrides (else DIFF below): aim_easy / aim_medium / aim_hard / aim_expert,
    # firep_easy ... (chance to pull the gun trigger on a decision when aligned)
    "aim_easy": 2.5, "aim_medium": 1.7, "aim_hard": 1.1, "aim_expert": 0.9,
    # strafe_<diff>: perpendicular move intensity in fights (1 = full-speed strafe)
    "strafe_easy": 0.3, "strafe_medium": 0.6, "strafe_hard": 1.0, "strafe_expert": 1.0,
}

DIFF = {
    #           aim   react  fire_p  aggression
    "easy":   (1.7, 0.60, 0.65, 0.8),
    "medium": (1.35, 0.40, 0.8, 0.9),
    "hard":   (1.1, 0.25, 0.9, 1.0),
    "expert": (0.9, 0.15, 1.0, 1.1),
}
DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2, "expert": 3}
PRACTICE_MIX = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3

UPGRADE_PREF = ["plating", "armor", "nanites", "gun_rate", "gun_damage", "laser_damage", "laser_charge",
                "servos", "gun_mag", "lightning_efficiency", "capacitor", "reactor", "gyros", "jets", "sensors"]


def _lead(sx, sy, sz, tx, ty, tz, vx, vy, vz, speed):
    t = 0.0
    for _ in range(3):
        px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t
        d = math.sqrt((px - sx) ** 2 + (py - sy) ** 2 + (pz - sz) ** 2)
        t = d / speed
    return tx + vx * t, ty + vy * t, tz + vz * t, d


class HouseBot:
    def __init__(self, squad, mech, diff):
        self.sq = squad
        self.m = mech
        self.diff = diff
        self.aim_mult, self.react_s, self.fire_p, self.aggr = DIFF[diff]
        self.aim_mult = squad.p.get("aim_" + diff, self.aim_mult)
        self.fire_p = squad.p.get("firep_" + diff, self.fire_p)
        self.react_s *= squad.p["react"]
        r = squad.M.crng
        self.rng = r
        self.ex = self.ey = 0.0
        self.strafe = 1 if r.random() < 0.5 else -1
        self.strafe_until = 0.0
        self.goal = None
        self.goal_until = 0.0
        self.stuck_t = 0.0
        self.jet_until = 0.0
        self.detour = 0.0
        self.detour_until = 0.0
        self.boost_until = 0.0
        self.seen_since = None   # when this bot started knowing the target
        self.laser_hold = False
        self.farm_target = None
        self.farm_until = 0.0
        self.hunter = False
        self.last_pos = (mech.x, mech.y)
        self.hit_by = {}

    # engine callbacks
    def on_damaged(self, src, amount, weapon):
        if isinstance(src, Mech):
            self.hit_by[src.id] = self.sq.M.t
        elif src is not None and getattr(src, "kind", None) == "npc":
            # a drone shooting us: shoot back (house bots almost never die to drones in live replays)
            self.farm_target = src
            self.farm_until = self.sq.M.t + 10
            self.npc_hit_t = self.sq.M.t

    def on_death(self):
        self.sq.on_house_death(self)

    # ------------------------------------------------------------ helpers
    def _aim_noise(self, dist, mult=1.0):
        p = self.sq.p
        sig = mult * self.aim_mult * (p["aim_a"] + p["aim_b"] * dist)
        rho = p["aim_rho"]
        k = math.sqrt(max(0.0, 1 - rho * rho))
        self.ex = rho * self.ex + k * self.rng.gauss(0, sig)
        self.ey = rho * self.ey + k * self.rng.gauss(0, sig * 0.5)
        return self.ex, self.ey

    def _aim_at(self, tx, ty, tz, vx, vy, vz, speed, noisy=True, mult=1.0):
        m = self.m
        sx, sy, sz = m.x, m.y, m.z + m.stats["muzzle_height"]
        px, py, pz, d = _lead(sx, sy, sz, tx, ty, tz, vx, vy, vz, speed)
        yaw = math.atan2(py - sy, px - sx)
        hd = math.hypot(px - sx, py - sy)
        pitch = math.atan2(pz - sz, hd)
        if noisy:
            ex, ey = self._aim_noise(d, mult)
            yaw += ex / max(hd, 1.0)
            pitch += ey / max(hd, 1.0)
        return yaw, pitch, d

    def _move_toward(self, gx, gy):
        m = self.m
        dx, dy = gx - m.x, gy - m.y
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return 0.0, 0.0
        return dx / L, dy / L

    def _blocked_ahead(self, ux, uy, dist, dz=1.5):
        m = self.m
        g = self.sq.M.map
        r = m.stats["radius"]
        z = m.z + dz
        x0, y0 = m.x + ux * r * 0.5, m.y + uy * r * 0.5
        if g.seg_prism(x0, y0, z, m.x + ux * (dist + r), m.y + uy * (dist + r), z, any_hit=True) is not None:
            return True
        # steep terrain (cliffs, stadium banks)
        g1 = g.ground(m.x + ux * (r + 1.5), m.y + uy * (r + 1.5))
        return g1 > m.z + m.stats["step_up"] + 0.3 and g1 > g.floor_at(m.x, m.y, m.z, 0.5)[0] + 1.0

    def _nav(self, mv, act):
        """Probe ahead; jump + jet over obstacles (house bots are airborne 17-45 % of the time),
        detour sideways if out of energy or still making no progress."""
        m = self.m
        t = self.sq.M.t
        want = math.hypot(*mv)
        if want < 0.3:
            return mv
        ux, uy = mv[0] / want, mv[1] / want
        if t < self.detour_until:
            c, s = math.cos(self.detour), math.sin(self.detour)
            ux, uy = ux * c - uy * s, ux * s + uy * c
        spd = math.hypot(m.vx, m.vy)
        along = m.vx * ux + m.vy * uy
        if along < 2.0:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.1)
        if m.grounded:
            blocked = (self.sq.M.tick // 2 + m.id) % 2 == 0 and self._blocked_ahead(ux, uy, 5.0)
            if blocked or self.stuck_t > 0.6:
                if m.energy > 30:
                    act["jump"] = True
                    self.jet_until = t + 6.0
                elif t >= self.detour_until:
                    self.detour = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.9, 1.6)
                    self.detour_until = t + self.rng.uniform(1.0, 2.5)
                self.stuck_t = 0.0
        else:
            if t < self.jet_until and m.energy > 2:
                # keep jetting while the way ahead is blocked at foot level (or we're still rising slowly)
                if self._blocked_ahead(ux, uy, 4.0, 0.3) or m.vz < 1.0 and self.stuck_t > 0.3:
                    act["boost"] = True
                else:
                    self.jet_until = t + 0.3
            if self.stuck_t > 2.5 and t >= self.detour_until:
                self.detour = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.9, 1.6)
                self.detour_until = t + self.rng.uniform(1.0, 2.5)
                self.stuck_t = 0.0
        return (ux * want, uy * want)

    def _zone_goal(self):
        M = self.sq.M
        c, r, c1, r1, ss, se, dps, ph = M.zone_state()
        m = self.m
        d_cur = math.hypot(m.x - c[0], m.y - c[1])
        d_next = math.hypot(m.x - c1[0], m.y - c1[1])
        t = M.t
        # outside the current circle, or the shrink is coming and we're outside the next one
        if d_cur > r - 15 or (t > ss - 25 and d_next > r1 * 0.85):
            return c1
        return None

    def _roam_goal(self):
        M = self.sq.M
        t = M.t
        c, r, c1, r1, ss, se, dps, ph = M.zone_state()
        p = self.sq.p
        # roam_frac: wander radius as a fraction of the next circle (>1 = spread into the current one)
        rr = min(r1 * p["roam_frac"], r * 0.8) if t < ss - 25 else r1 * 0.6
        if self.goal is None or t > self.goal_until or math.hypot(self.goal[0] - self.m.x, self.goal[1] - self.m.y) < 8 \
                or math.hypot(self.goal[0] - c1[0], self.goal[1] - c1[1]) > max(rr, r1):
            for _ in range(20):
                d = self.rng.random() ** 0.5 * rr
                a = self.rng.uniform(0, 2 * math.pi)
                x, y = c1[0] + d * math.cos(a), c1[1] + d * math.sin(a)
                b = M.map.bounds
                if b["minX"] + 20 < x < b["maxX"] - 20 and b["minY"] + 20 < y < b["maxY"] - 20:
                    break
            self.goal = (x, y)
            self.goal_until = t + self.rng.uniform(12, 30)
        return self.goal

    def _loot_weapon(self, act, radius):
        M = self.sq.M
        m = self.m
        best = None
        for it in M.items_near(m.x, m.y, radius):
            if it["kind"] in ("cube", "gun") or m.has(it["kind"]) or abs(it["z"] - m.z) > 4:
                continue
            d = math.hypot(it["x"] - m.x, it["y"] - m.y)
            if best is None or d < best[0]:
                best = (d, it)
        if best is None:
            return None
        d, it = best
        if d < M.cfg["loot"]["pickup_radius"] - 0.5:
            act["take"] = it["id"]
        return self._move_toward(it["x"], it["y"])

    def _farm(self, act):
        """Roamers: grab nearby weapons / cubes, break crates, shoot drones. Returns move or None."""
        M = self.sq.M
        m = self.m
        p = self.sq.p
        t = M.t
        # weapon loot when slot 1 empty
        if m.weapons[1] is None:
            mv = self._loot_weapon(act, 60.0)
            if mv is not None:
                return mv
        # cubes
        best = None
        for it in M.items_near(m.x, m.y, 40):
            if it["kind"] != "cube" or abs(it["z"] - m.z) > 4:
                continue
            d = math.hypot(it["x"] - m.x, it["y"] - m.y)
            if best is None or d < best[0]:
                best = (d, it)
        if best:
            return self._move_toward(best[1]["x"], best[1]["y"])
        # crates / drones to shoot
        ft = self.farm_target
        if ft is not None:
            alive = (ft["id"] in M.crates) if isinstance(ft, dict) else ft.alive
            if not alive or t > self.farm_until:
                self.farm_target = ft = None
        if ft is None and int(t * 10) % 10 == m.id % 10:
            best = None
            for c in M.crates.values():
                d = math.hypot(c["x"] - m.x, c["y"] - m.y)
                if d < 70 and abs(c["z"] - m.z) < 12 and (best is None or d < best[0]):
                    best = (d, c)
            for n in M.npcs:
                if n.alive:
                    d = math.hypot(n.x - m.x, n.y - m.y)
                    if d < 60 and (best is None or d < best[0]):
                        best = (d, n)
            if best:
                self.farm_target = best[1]
                self.farm_until = t + 15
                ft = best[1]
        if ft is not None:
            if isinstance(ft, dict):
                tx, ty, tz, vx, vy = ft["x"], ft["y"], ft["z"] + 1.2, 0.0, 0.0
            else:
                tx, ty, tz, vx, vy = ft.x, ft.y, ft.z + 1.7, ft.vx, ft.vy
            gun = m.wstats["gun"]
            yaw, pitch, d = self._aim_at(tx, ty, tz, vx, vy, 0.0, gun["speed"], mult=p["farm_aim"])
            act["aim"] = [yaw, pitch]
            if abs(wrap(yaw - m.yaw)) < 0.08 and d < 120:
                fire = [False, False]
                for slot, w in enumerate(m.weapons):
                    if w is not None and w.kind == "gun":
                        fire[slot] = True
                act["fire"] = fire
            if d > 30:
                return self._move_toward(tx, ty)
            return (-math.sin(yaw) * self.strafe, math.cos(yaw) * self.strafe)
        return None

    # ------------------------------------------------------------ decision
    def decide(self, know, target, own_los):
        M = self.sq.M
        m = self.m
        p = self.sq.p
        t = M.t
        act = {"fire": [False, False]}
        if m.offer is not None:
            ch = m.offer["choices"]
            best = min(range(len(ch)), key=lambda i: UPGRADE_PREF.index(ch[i]) if ch[i] in UPGRADE_PREF else 99)
            act["pick"] = best
        mv = None
        zg = self._zone_goal()
        engage = False
        dist = None
        if target is not None:
            dist = math.hypot(target.x - m.x, target.y - m.y)
            rng = p["hunt_engage"] if self.hunter else p["roam_engage"]
            recently_hit = t - self.hit_by.get(target.id, -1e9) < 4.0
            if know and (dist <= rng or recently_hit):
                engage = True
        if engage:
            if self.seen_since is None:
                self.seen_since = t
            mv = self._combat(target, dist, own_los, act, zg)
        else:
            self.seen_since = None
            self._release_laser(act)
            if zg is not None:
                mv = self._move_toward(*zg)
            elif self.farm_target is not None and not isinstance(self.farm_target, dict) and \
                    t - getattr(self, "npc_hit_t", -1e9) < 4 and self.farm_target.alive:
                mv = self._farm(act)
            elif self.hunter and target is not None:
                if p["omniscient"] or know:
                    gx, gy = target.x, target.y
                elif self.sq.last_pos and M.t - self.sq.last_seen_t < p["search_s"]:
                    gx, gy = self.sq.last_pos
                else:
                    gx, gy = self._roam_goal()  # gave up searching
                mv = self._loot_weapon(act, 40.0) if m.weapons[1] is None else None
                if mv is None:
                    mv = self._move_toward(gx, gy)
                    act["aim"] = [math.atan2(gy - m.y, gx - m.x), 0.0]
            else:
                if p["farm"]:
                    mv = self._farm(act)
                if mv is None:
                    g = self._roam_goal()
                    mv = self._move_toward(*g)
            if "aim" not in act:
                # look where we walk, with a slow scan
                yaw = math.atan2(mv[1], mv[0]) if mv and (mv[0] or mv[1]) else m.yaw
                yaw += 0.6 * math.sin(t * 0.7 + m.id)
                act["aim"] = [yaw, 0.0]
            if t > self.boost_until and self.rng.random() < 0.05:
                self.boost_until = t + self.rng.uniform(0.5, 2.0) if self.rng.random() < p["boost_p"] * 2 else t
        mv = self._nav(mv or (0.0, 0.0), act)
        act["move"] = [mv[0], mv[1]]
        if m.grounded and t < self.boost_until and m.energy > 30:
            act["boost"] = True
        return act

    def _release_laser(self, act):
        m = self.m
        for slot, w in enumerate(m.weapons):
            if w is not None and w.kind == "laser" and w.charging:
                act["fire"][slot] = False

    def _combat(self, tg, dist, own_los, act, zg):
        M = self.sq.M
        m = self.m
        p = self.sq.p
        t = M.t
        hpf = m.hp / max(1.0, m.max_hp(M.cfg))
        gun = m.wstats["gun"]
        tz = tg.z + tg.stats["height"] * 0.5
        tvz = tg.vz if not tg.grounded else 0.0
        yaw, pitch, d3 = self._aim_at(tg.x, tg.y, tz, tg.vx, tg.vy, tvz, gun["speed"])
        act["aim"] = [yaw, pitch]
        ready = self.seen_since is not None and t - self.seen_since >= self.react_s
        width = math.atan2(tg.stats["radius"], max(dist, 1.0))
        aligned = abs(wrap(yaw - m.yaw)) < p["fire_gate"] + width and abs(pitch - m.pitch) < p["fire_gate"] + 0.05
        fire = act["fire"]
        if ready and own_los and aligned:
            for slot, w in enumerate(m.weapons):
                if w is None:
                    continue
                k = w.kind
                if k == "gun":
                    if dist < gun["range"] * 0.95 and self.rng.random() < self.fire_p:
                        fire[slot] = True
                elif k == "lightning":
                    if dist < m.wstats["lightning"]["range"] - 3:
                        fire[slot] = True
                elif k == "mortar":
                    if 35 < dist < 110 and (m.weapons[0] is None or m.weapons[0].reload > 0):
                        ws = m.wstats["mortar"]
                        v, g = ws["speed"], ws["gravity"]
                        h = tg.z - (m.z + m.stats["height"] + 0.5)
                        disc = v ** 4 - g * (g * dist * dist + 2 * h * v * v)
                        if disc > 0:
                            act["aim"] = [yaw, math.atan((v * v - math.sqrt(disc)) / (g * dist))]
                            fire[slot] = abs(act["aim"][1] - m.pitch) < 0.05
        # laser: start charging when ready, release at exactly laser_release seconds
        for slot, w in enumerate(m.weapons):
            if w is not None and w.kind == "laser":
                if w.charging:
                    # the release applies on the next tick: let go one decision early so it fires at laser_release
                    fire[slot] = w.charge < p["laser_release"] - 0.1 - 1e-6
                elif ready and own_los and dist < 220 and w.cooldown <= 0:
                    fire[slot] = True
        # movement: strafe + radial
        if t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = t + self.rng.uniform(1.0, 3.0)
            if self.rng.random() < p["boost_p"]:
                self.boost_until = t + self.rng.uniform(0.6, 1.6)
        dx, dy = tg.x - m.x, tg.y - m.y
        L = math.hypot(dx, dy) or 1.0
        ux, uy = dx / L, dy / L
        px, py = -uy * self.strafe, ux * self.strafe
        want = p["want_range"] / self.aggr
        if hpf < p["retreat_frac"]:
            radial = -0.45
        elif not own_los:
            radial = 0.9      # get line of sight
        elif dist > want * 1.25:
            radial = 0.8 if self.hunter else 0.4
        elif dist < want * 0.6:
            radial = -0.4
        elif hpf > p["close_frac"]:
            radial = 0.25
        else:
            radial = 0.0
        sf = p.get("strafe_" + self.diff, 1.0)
        mx_, my_ = sf * px * (1 - abs(radial)) ** 0.5 + ux * radial, sf * py * (1 - abs(radial)) ** 0.5 + uy * radial
        if m.grounded and m.energy > 40 and self.rng.random() < p["hop_p"]:
            act["jump"] = True
            if self.rng.random() < 0.5:
                self.jet_until = t + self.rng.uniform(0.3, 1.2)
        if not m.grounded and t < self.jet_until and m.energy > 10:
            act["boost"] = True
        if zg is not None:
            gx, gy = self._move_toward(*zg)
            mx_, my_ = (mx_ + gx * 1.5), (my_ + gy * 1.5)
        L = math.hypot(mx_, my_)
        if L < 1e-6:
            return (0.0, 0.0)
        return (mx_ / L, my_ / L) if L > 1 else (mx_, my_)


class Squad:
    def __init__(self, M, params, diffs):
        self.M = M
        self.p = dict(DEFAULTS)
        self.p.update(params or {})
        self.bots = []
        self.last_seen_t = -1e9
        self.last_pos = None
        self.ext_kills = 0
        for m, d in zip([mm for mm in M.mechs if mm.team == "house"], diffs):
            hb = HouseBot(self, m, d)
            m.house = hb
            m.diff = d
            m.name = d
            self.bots.append(hb)
        if self.p["start_weapon"]:
            r = M.rng
            for hb in self.bots:
                x = r.random()
                kind = "laser" if x < 0.5 else ("lightning" if x < 0.8 else ("mortar" if x < 0.9 else None))
                if kind:
                    from .engine import Weapon
                    hb.m.weapons[1] = Weapon(kind, hb.m.wstats[kind])

    def on_house_death(self, hb):
        if hb.m.killed_by is not None and self.M.mechs[hb.m.killed_by].team == "ext":
            self.ext_kills += 1

    def _n_hunters(self, alive_house):
        M = self.M
        p = self.p
        ph = M.zone_state()[7]
        if ph >= p["all_phase"] or M.n_alive() <= p["all_alive"]:
            return alive_house
        n = p["h0"] + p["h_per_kill"] * self.ext_kills + p["h_per_min"] * (M.t / 60.0)
        return max(0, min(alive_house, int(n)))

    def decide(self):
        M = self.M
        exts = [m for m in M.mechs if m.alive and m.team == "ext"]
        target = min(exts, key=lambda m: m.hp) if exts else None
        bots = [b for b in self.bots if b.m.alive]
        acts = {}
        los = {}
        know = False
        if target is not None:
            tx, ty = target.x, target.y
            for b in bots:
                m = b.m
                d2 = (m.x - tx) ** 2 + (m.y - ty) ** 2
                if d2 > 235 * 235:
                    los[m.id] = False
                    continue
                s = M.sees_mech(m, target)
                if s:
                    know = True
                    los[m.id] = True
                else:
                    # own line of fire (no vision cone needed to keep shooting what the squad sees)
                    los[m.id] = not M.map.sight_blocked(m.x, m.y, m.z + m.stats["muzzle_height"], tx, ty,
                                                         target.z + target.stats["height"] * 0.5, trees=False) and d2 < 230 * 230
            if know:
                self.last_seen_t = M.t
                self.last_pos = (tx, ty)
            know = M.t - self.last_seen_t <= M.cfg["vision"]["memory_s"]
            # hunters: nearest bots to the target, sticky
            nh = self._n_hunters(len(bots))
            cur = [b for b in bots if b.hunter]
            if len(cur) > nh:
                cur.sort(key=lambda b: (DIFF_RANK[b.diff], (b.m.x - tx) ** 2 + (b.m.y - ty) ** 2) if self.p["hunt_order"]
                         else (b.m.x - tx) ** 2 + (b.m.y - ty) ** 2)
                for b in cur[nh:]:
                    b.hunter = False
            elif len(cur) < nh:
                if p_order := self.p["hunt_order"]:
                    # 1 = weakest difficulty joins the hunt first (easy, medium, hard, expert), nearest within a tier
                    key = lambda b: (DIFF_RANK[b.diff], (b.m.x - tx) ** 2 + (b.m.y - ty) ** 2)
                else:
                    key = lambda b: (b.m.x - tx) ** 2 + (b.m.y - ty) ** 2
                rest = sorted([b for b in bots if not b.hunter], key=key)
                for b in rest[:nh - len(cur)]:
                    b.hunter = True
        for b in bots:
            acts[b.m.id] = b.decide(know, target, los.get(b.m.id, False))
        return acts


def setup(M, params=None):
    diffs = list(PRACTICE_MIX)
    M.rng.shuffle(diffs)
    n_house = sum(1 for m in M.mechs if m.team == "house")
    diffs = (diffs * 2)[:n_house]
    M.squad = Squad(M, params, diffs)
    M.npc_lead = float((params or {}).get("npc_lead", 1.0))
    return M.squad
