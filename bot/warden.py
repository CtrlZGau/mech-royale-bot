"""Warden: a squad-hunter policy (two phases: Scout, then Hunt) on top of the mechbot map/planner layer.

Phases
  Scout (first SCOUT_S seconds): careful. Seek a laser (wide radius), fight only what is close, hurting us,
           lining up a laser on us, or nearly dead; otherwise wait at a low-exposure post inside the zone.
  Hunt  (afterwards, for the rest of the match): fight anything that threatens or is weak, keep hard
           focus on wounded targets, back off only in short bursts while a fight is being lost, close in
           to finish a wounded target, grab cubes even mid-fight, laser-centred loadout and upgrades.

Each decision: memory -> upgrade -> threat picture & target -> zone timing -> mode -> movement
(dodges, unstick) -> fire control -> reload -> weapon pickup.
Uses mb_bot.TimedBot only as the world/planner/cover/mortar-dodge layer (bot/mb_bot.py is unchanged).
"""
import math
import time

import mechfight as mf
from mb_bot import TimedBot, dist2, unit, wrap

PI = math.pi
TAU = 2 * PI
LATENCY = 0.075  # seconds between our decision and the shot leaving

SCOUT_S = 20.0

# ---- tuning by phase
SCOUT = dict(fight_close=60.0, max_range=215.0, hp_weight=60.0 / 1400, wounded_bonus=0.0, keep_bonus=0.0,
             pull_hp=0.38, steady_hp=0.8, pref_gun=85.0, pref_light=28.0, pref_laser=85.0,
             loot_r=150.0, laser_r=240.0, farm_hp=0.7, cube_fight_r=0.0, finish_r=0.0, laser_ups=False)
HUNT = dict(fight_close=60.0, max_range=240.0, hp_weight=0.2, wounded_bonus=100.0, keep_bonus=120.0,
            pull_hp=0.38, steady_hp=0.8, pref_gun=85.0, pref_light=28.0, pref_laser=140.0,
            loot_r=150.0, laser_r=240.0, farm_hp=0.5, cube_fight_r=70.0, finish_r=14.0, laser_ups=True)

UPG_GUNLINE = ["plating", "armor", "gun_damage", "nanites", "gun_rate", "lightning_efficiency", "lightning_range",
               "laser_damage", "servos", "laser_charge", "gun_mag", "gun_velocity", "gyros", "lightning_chain",
               "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors", "jets",
               "laser_pierce"]
UPG_LASERLINE = ["plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
                 "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros",
                 "lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
                 "sensors", "jets", "laser_pierce"]
SLOT_VALUE = {"laser": 4, "lightning": 3, "mortar": 0}  # mortar never wanted


class WardenBot(TimedBot):
    name = "warden"

    def __init__(self, hello):
        super().__init__(hello)
        self.name = "warden"
        self.k = dict(SCOUT)
        self.phase = "scout"
        self.items = {"crate": {}, "cube": {}, "pickup": {}}  # id -> {"pos", "t"}
        self.drones = {}                                     # id -> obs dict + "t"
        self.keep_id, self.keep_t = None, -99.0
        self.lost_at = -99.0
        self.backing = False
        self.danger_t = -99.0
        self.drone_hit_t, self.drone_bearing = -99.0, 0.0
        self.chase = None        # (kind, id) current loot target
        self.chase_t = -99.0
        self.banned = {}         # loot id -> until
        self.side = 1.0
        self.side_until = 0.0
        self.post = None
        self.post_t = -99.0
        self.alive0 = None
        self.my_kills = 0
        mf.log("policy warden")

    # ================================================================== helpers
    def scale(self, cubes):
        return 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * (cubes or 0)

    def body_center(self, m):
        if m.get("kind") == "npc":
            return [m["pos"][0], m["pos"][1], m["pos"][2] + self.cfg["npc"].get("height", 3.5) * 0.5]
        return [m["pos"][0], m["pos"][1], m["pos"][2] + self.cfg["mech"]["height"] * self.scale(m.get("cubes", 0)) * 0.5]

    def eye_of(self, m):
        return [m["pos"][0], m["pos"][1], m["pos"][2] + self.cfg["mech"]["eye_height"] * self.scale(m.get("cubes", 0))]

    def muzzle(self):
        me = self.me
        return [self.pos[0] + me["vel"][0] * LATENCY, self.pos[1] + me["vel"][1] * LATENCY,
                self.pos[2] + me["stats"].get("muzzle_height", 7)]

    def clear_shot(self, m):
        if self.world is None:
            return True
        mz = self.muzzle()
        if self.world.los(mz, self.body_center(m)):
            return True
        if m.get("kind") == "mech":
            top = [m["pos"][0], m["pos"][1], m["pos"][2] + self.cfg["mech"]["height"] * self.scale(m.get("cubes", 0)) * 0.85]
            return self.world.los(mz, top)
        return False

    def gear(self, kind):
        for i, w in enumerate(self.me["weapons"]):
            if w and w["kind"] == kind:
                return i, w
        return -1, None

    def aiming_at_me(self, m, tol):
        b = math.atan2(self.pos[1] - m["pos"][1], self.pos[0] - m["pos"][0])
        return abs(wrap(m.get("aim", [9, 0])[0] - b)) < tol

    def approach_speed(self, m):
        dx, dy = m["pos"][0] - self.pos[0], m["pos"][1] - self.pos[1]
        d = math.hypot(dx, dy)
        if d < 1:
            return 0.0
        rv = (m["vel"][0] - self.me["vel"][0]) * dx + (m["vel"][1] - self.me["vel"][1]) * dy
        return -rv / d

    def struck_ago(self, mid):
        a = self.attackers.get(mid)
        return self.t - a[0] if a else 1e9

    # ================================================================== memory
    def remember(self, obs):
        t = self.t
        eye = [self.pos[0], self.pos[1], self.pos[2] + self.me["stats"].get("eye_height", 8)]
        for key, lst in (("crate", obs.get("crates", [])), ("cube", obs.get("cubes", [])), ("pickup", obs.get("pickups", []))):
            book = self.items[key]
            for it in lst:
                book[it["id"]] = {"pos": it["pos"], "t": t, "kind": it.get("kind"), "hp": it.get("hp")}
            for iid in list(book):
                e = book[iid]
                if e["t"] == t:
                    continue
                if dist2(e["pos"], self.pos) < 40 and t - e["t"] > 0.35 and self.world is not None and \
                        self.world.los(eye, [e["pos"][0], e["pos"][1], e["pos"][2] + 1.5]):
                    del book[iid]  # should be visible but isn't: gone
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                self.drones[m["id"]] = dict(m, t=t)
        for iid in [i for i, d in self.drones.items() if t - d["t"] > 120]:
            del self.drones[iid]
        for ev in obs.get("events", []):
            if ev["type"] == "damaged" and ev.get("weapon") == "npc" and ev.get("bearing") is not None:
                self.drone_hit_t, self.drone_bearing = t, ev["bearing"]
            elif ev["type"] == "kill":
                self.my_kills += 1
        if self.alive0 is None:
            self.alive0 = obs["alive"]
        want = "hunt" if t > SCOUT_S else "scout"
        if want != self.phase:
            self.phase = want
            self.k = dict(HUNT if want == "hunt" else SCOUT)

    # ================================================================== zone timing
    def zone_hold(self, z, t):
        if t <= z["shrink_start"] or z["shrink_end"] <= z["shrink_start"]:
            return z["center"], z["radius"]
        f = max(0.0, min(1.0, (t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"])))
        return ([z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
                 z["center"][1] + (z["next_center"][1] - z["center"][1]) * f],
                z["radius"] + (z["next_radius"] - z["radius"]) * f)

    def must_leave_by(self, z, x, y, pad):
        nc, nr = z["next_center"], z["next_radius"]
        if math.hypot(x - nc[0], y - nc[1]) <= nr - pad:
            return 1e9
        for k in range(25):
            tt = z["shrink_start"] + (z["shrink_end"] - z["shrink_start"]) * k / 24
            c, r = self.zone_hold(z, tt)
            if math.hypot(x - c[0], y - c[1]) > r - pad:
                return tt
        return z["shrink_end"]

    def safe_point(self, z, x, y):
        nc, nr = z["next_center"], z["next_radius"]
        if nr <= 30:
            return nc[0], nc[1]
        margin = max(3.0, min(30.0, 0.3 * nr))
        dx, dy = x - nc[0], y - nc[1]
        d = math.hypot(dx, dy)
        r = max(0.0, nr - margin - 5)
        if d < 1 or d <= r:
            return x, y
        return nc[0] + dx / d * r, nc[1] + dy / d * r

    def zone_cost(self, z, x, y):
        c, r = self.zone_hold(z, self.t) if self.t > z["shrink_start"] else (z["center"], z["radius"])
        cost = 0.0
        out = math.hypot(x - c[0], y - c[1]) - r
        if out > -5:
            cost += 150 + 5 * (out + 5)
        outn = math.hypot(x - z["next_center"][0], y - z["next_center"][1]) - z["next_radius"]
        if outn > -5:
            soon = max(0.1, min(1.0, 1 - (z["shrink_start"] - self.t) / 25))
            cost += soon * (25 + 0.8 * (outn + 5))
        return cost

    # ================================================================== threat picture
    def appraise(self, obs, hpf, endgame):
        """-> (target, visible threats list, danger, crowd)"""
        t = self.t
        K = self.k
        vis = []
        best, best_s = None, 1e18
        for e in self.enemy_seen.values():
            if t - e["t"] > 2.5:
                continue
            vis.append(e)
            if e["t"] != t:
                e["_shot"] = False
                e["_hot"] = False
                continue
            d = dist2(e["pos"], self.pos)
            e["_d"] = d
            e["_shot"] = self.clear_shot(e)
            hp = e.get("hp", 1400)
            mhp = max(e.get("max_hp", 1400), 1)
            aimed = self.aiming_at_me(e, 0.22)
            closing = self.approach_speed(e)
            struck = self.struck_ago(e["id"])
            if self.phase == "hunt":
                hot = (d < K["fight_close"] or struck < 6 or endgame or (closing > 3 and d < 180)
                       or (hp < 0.4 * mhp and d < 160) or (aimed and d < 190))
            else:
                hot = (d < K["fight_close"] or struck < 4 or (aimed and e.get("charging") and d < 120)
                       or (hp < 350 and d < 120) or obs["alive"] <= 2)
            e["_hot"] = hot
            if not hot or not e["_shot"] or d > K["max_range"]:
                continue
            s = d + K["hp_weight"] * hp
            if K["wounded_bonus"] and hp <= 0.3 * mhp:
                s -= K["wounded_bonus"]
            if K["keep_bonus"] and e["id"] == self.keep_id and t - self.keep_t < 2:
                s -= K["keep_bonus"]
            if struck < 4:
                s -= 40
            if closing > 3:
                s -= 15
            if s < best_s:
                best, best_s = e, s
        if best is not None:
            self.keep_id, self.keep_t = best["id"], t
        danger = any(e.get("_hot") or self.struck_ago(e["id"]) < 4 for e in vis)
        crowd = sum(1 for e in vis if e["t"] == t and (e.get("_hot") or (e.get("_shot") and e.get("_d", 999) < 150)))
        if danger or t - self.last_hit_t < 3:
            self.danger_t = t
        return best, vis, danger, crowd

    # ================================================================== movement pieces
    def go(self, gx, gy):
        return self.navigate((gx, gy))

    def keep_range(self, m):
        _, light = self.gear("lightning")
        _, laser = self.gear("laser")
        K = self.k
        if m.get("kind") == "npc":
            return 58.0
        r = K["pref_gun"]
        if light is not None and self.me["energy"] > 12:
            r = K["pref_light"]
        elif laser is not None:
            r = K["pref_laser"]
        if light is None:
            if "lightning" in (m.get("weapons") or []):
                r = max(r, 64.0)
            if "mortar" in (m.get("weapons") or []):
                r = max(r, 80.0)
        return r

    def brawl_step(self, obs, foe, vis, want_sight, range_override=None):
        """Pick the best nearby spot to fight from: hold range, keep (or break) line of fire, strafe,
        stay out of other enemies' sight, stay in the zone, drift toward cubes."""
        w = self.world
        x, y = self.pos[0], self.pos[1]
        rp = range_override if range_override else self.keep_range(foe)
        bx, by = foe["pos"][0] - x, foe["pos"][1] - y
        bd = math.hypot(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if self.t >= self.side_until:
            if self.rng.random() < 0.6:
                self.side = -self.side
            self.side_until = self.t + 0.3 + self.rng.random() * 0.7
        sx, sy = -uy * self.side, ux * self.side
        if w is None:
            return unit(sx + ux * (bd - rp) / 25.0, sy + uy * (bd - rp) / 25.0)
        center = self.body_center(foe)
        mh = self.me["stats"].get("muzzle_height", 7)
        cube = None
        for c in self.items["cube"].values():
            dc = dist2(c["pos"], self.pos)
            if dc < 40 and abs(c["pos"][2] - self.pos[2]) < 4 and (cube is None or dc < dist2(cube["pos"], self.pos)):
                cube = c
        best, mv = -1e18, (0.0, 0.0)
        side_blocked = False
        for k in range(-1, 16):
            if k < 0:
                cx, cy, dx, dy, r = x, y, 0.0, 0.0, 0.0
            else:
                a = k * PI / 8
                dx, dy, r = math.cos(a), math.sin(a), 6.0
                cx, cy = x + dx * r, y + dy * r
                if not w.in_bounds(cx, cy, 4) or not w.walk_clear(x, y, cx, cy) or not w.cost[w.idx(cx, cy)]:
                    if dx * sx + dy * sy > 0.9:
                        side_blocked = True
                    continue
            cz = w.height(cx, cy)
            dd = math.hypot(foe["pos"][0] - cx, foe["pos"][1] - cy)
            sc = -1.1 * abs(dd - rp) - r * 0.4
            sight = w.los([cx, cy, cz + mh], center)
            if sight:
                sc += 45 * want_sight
            torso = [cx, cy, cz + self.me["stats"].get("height", 9) * 0.6]
            for e in vis:
                if e is foe or dist2(e["pos"], (cx, cy)) > 170:
                    continue
                if w.los(self.eye_of(e), torso):
                    sc -= 30
            sc -= self.zone_cost(obs["zone"], cx, cy)
            if cube is not None:
                sc += 1.5 * (dist2(cube["pos"], self.pos) - math.hypot(cube["pos"][0] - cx, cube["pos"][1] - cy))
            if want_sight > 0:
                sc += 9 * (dx * sx + dy * sy)
            if k < 0:
                sc -= 8
                if want_sight < 0 and not sight:
                    sc += 14
            if sc > best:
                best, mv = sc, (dx, dy)
        if side_blocked:
            self.side = -self.side
            self.side_until = self.t + 0.5
        return mv

    def take_cover(self, vis):
        pts = [self.eye_of(e) for e in sorted(vis, key=lambda e: dist2(e["pos"], self.pos))[:4]
               if dist2(e["pos"], self.pos) < 230]
        if not pts:
            return None
        spot = self.find_cover(pts, max_r=30.0)
        if spot is None:
            return None
        if dist2(spot, self.pos) < 1.5:
            return (0.0, 0.0)
        return self.go(*spot)

    def sidestep(self, obs):
        """Lateral dodge from incoming bullets and from lasers charging at us. -> (dx, dy, laser?)"""
        me = self.me
        cx, cy, cz = self.pos[0], self.pos[1], self.pos[2] + me["stats"].get("height", 9) * 0.56
        reach = me["stats"].get("radius", 2.5) + 1.1
        dx = dy = 0.0
        las = False
        for p in obs.get("projectiles", []):
            if p["kind"] == "mortar":
                continue
            vx, vy, vz = p["vel"]
            v2 = vx * vx + vy * vy + vz * vz
            if v2 < 1:
                continue
            tc = ((cx - p["pos"][0]) * vx + (cy - p["pos"][1]) * vy + (cz - p["pos"][2]) * vz) / v2
            if tc <= 0 or tc > 0.7:
                continue
            ox, oy = p["pos"][0] + vx * tc - cx, p["pos"][1] + vy * tc - cy
            if math.hypot(ox, oy) < reach:
                vh = math.hypot(vx, vy)
                if vh < 1:
                    continue
                px, py = -vy / vh, vx / vh
                if px * ox + py * oy > 0:
                    px, py = -px, -py
                dx += px
                dy += py
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            to_me = math.atan2(cy - m["pos"][1], cx - m["pos"][0])
            if abs(wrap(m["aim"][0] - to_me)) > 0.15:
                continue
            dx += math.cos(to_me + PI / 2 * self.side) * 1.5
            dy += math.sin(to_me + PI / 2 * self.side) * 1.5
            las = True
        L = math.hypot(dx, dy)
        return (dx / L, dy / L, las) if L > 0.01 else (0.0, 0.0, False)

    def low_exposure(self, x, y):
        w = self.world
        h = w.height(x, y)
        p = [x, y, h + 6.0]
        seen = n = 0
        for k in range(12):
            a = k * TAU / 12
            for r in (60.0, 130.0):
                qx, qy = x + r * math.cos(a), y + r * math.sin(a)
                if not w.in_bounds(qx, qy, 2):
                    continue
                n += 1
                if w.los([qx, qy, w.height(qx, qy) + 8.0], p):
                    seen += 1
        return seen / n if n else 1.0

    def scout_post(self, obs):
        """A quiet, low-exposure waiting spot inside the next circle (refreshed every 12 s)."""
        z = obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        w = self.world
        stale = self.post is None or dist2(self.post, nc) > nr * 0.8
        if w is not None and (stale or self.t - self.post_t > 12):
            self.post_t = self.t

            def cost(px, py):
                crowd = sum(max(0.0, 80 - dist2(e["pos"], (px, py))) / 80 for e in self.enemy_seen.values()
                            if self.t - e["t"] < 20)
                crowd += sum(max(0.0, 60 - dist2(d["pos"], (px, py))) / 60 for d in obs.get("drops", []))
                edge = math.hypot(px - nc[0], py - nc[1]) / max(30.0, nr)
                return 120 * self.low_exposure(px, py) + 0.25 * dist2((px, py), self.pos) + 40 * crowd + 10 * edge

            best, bc = None, 1e18
            for _ in range(12):
                a = self.rng.uniform(-PI, PI)
                r = math.sqrt(self.rng.random()) * nr * 0.7
                px, py = nc[0] + r * math.cos(a), nc[1] + r * math.sin(a)
                if not w.in_bounds(px, py, 6) or not w.cost[w.idx(px, py)] or w.prism[w.idx(px, py)]:
                    continue
                c = cost(px, py)
                if c < bc:
                    best, bc = (px, py), c
            if best is not None and (stale or self.post is None or bc < cost(*self.post) - 15):
                self.post = best
        return self.post

    # ================================================================== loot
    def wants_gear(self, kind):
        if kind == "gun":
            return self.me["weapons"][0] is None
        if self.gear(kind)[0] >= 0 or SLOT_VALUE.get(kind, 0) <= 0:
            return False
        cur = self.me["weapons"][1]
        return SLOT_VALUE.get(kind, 0) > (SLOT_VALUE.get(cur["kind"], 0) if cur else 0)

    def drop_ok(self, obs, d):
        me = self.me
        if self.phase != "hunt" or self.backing or not me["in_zone"] or not me["grounded"] or obs.get("projectiles"):
            return False
        if me["cubes"] >= self.cfg["cubes"].get("stat_cap", 10) or self.t - self.last_hit_t < 6:
            return False
        if any(self.t - e["t"] <= 3 and dist2(e["pos"], self.pos) <= 180 for e in self.enemy_seen.values()):
            return False
        if d.get("holder") is not None and d["holder"] != me["id"]:
            return False
        eta = 1.45 * dist2(d["pos"], self.pos) / max(me["stats"].get("walk_speed", 11.7), 1)
        wait = max(0.0, d.get("lands_in", 0) - eta)
        total = eta + wait + d.get("capture_s", 4)
        if eta > 15 or wait > 6 or total > 20:
            return False
        return self.t + total + 3 <= self.must_leave_by(obs["zone"], d["pos"][0], d["pos"][1], 8)

    def pick_loot(self, obs, hpf, danger):
        t = self.t
        K = self.k
        z = obs["zone"]
        c, r = self.zone_hold(z, t) if t > z["shrink_start"] else (z["center"], z["radius"])
        walk = max(self.me["stats"].get("walk_speed", 11.7), 1)
        best, best_u = None, 0.0

        def offer(kind, iid, pos, value):
            nonlocal best, best_u
            if self.banned.get(iid, -1) > t:
                return
            d = dist2(pos, self.pos)
            if d > 260 or math.hypot(pos[0] - c[0], pos[1] - c[1]) > r - 6:
                return
            if t + d * 1.4 / walk + 5 > self.must_leave_by(z, pos[0], pos[1], 6):
                return
            if self.world is not None and pos[2] - self.world.height(pos[0], pos[1]) > 3.2:
                return
            u = value / (d + 18)
            if self.chase == (kind, iid):
                u *= 1.35
            if u > best_u:
                best, best_u = (kind, iid, pos), u

        for iid, p in self.items["pickup"].items():
            if self.t - self.failed_take.get(iid, -99) < 4 or not self.wants_gear(p.get("kind")):
                continue
            rad = K["laser_r"] if p.get("kind") == "laser" else K["loot_r"]
            if dist2(p["pos"], self.pos) < rad:
                v = 130.0 if self.me["weapons"][1] is None else 60.0
                if p.get("kind") == "laser":
                    v = max(v, 150.0)
                offer("pickup", iid, p["pos"], v)
        capped = self.me["cubes"] >= self.cfg["cubes"].get("stat_cap", 10)
        cube_v = 25.0 if capped else 75.0
        for iid, cb in self.items["cube"].items():
            if dist2(cb["pos"], self.pos) < max(60.0, K["loot_r"] * 0.7):
                offer("cube", iid, cb["pos"], cube_v)
        if hpf > K["farm_hp"] and not danger:
            for iid, cr in self.items["crate"].items():
                if dist2(cr["pos"], self.pos) < K["loot_r"]:
                    offer("crate", iid, cr["pos"], cube_v * 0.6)
            if not capped:
                for iid, dr in self.drones.items():
                    if t - dr["t"] < 12 and dist2(dr["pos"], self.pos) < K["loot_r"]:
                        offer("drone", iid, dr["pos"], 85.0)
        for d in obs.get("drops", []):
            if not danger and dist2(d["pos"], self.pos) / walk < d.get("lands_in", 0) + 25 and self.drop_ok(obs, d):
                offer("drop", d["id"], d["pos"], 260.0)
        if best is None:
            self.chase = None
            return None
        key = (best[0], best[1])
        if key != self.chase:
            self.chase, self.chase_t = key, t
        elif t - self.chase_t > 30:
            self.banned[best[1]] = t + 60
            self.chase = None
            return None
        return best

    # ================================================================== main
    def act(self, obs):
        self._t0 = time.perf_counter()
        self.t = t = obs["t"]
        me = obs["self"]
        self.me, self.pos, self.obs = me, me["pos"], obs
        act = {}
        self.update_memory(obs)   # base layer: enemies, attackers, threat bearing, position history
        self.remember(obs)
        self.zone_status(obs)     # base layer: sets zone_inner used by its cover finder
        K = self.k
        hpf = me["hp"] / max(1.0, me["max_hp"])
        z = obs["zone"]
        endgame = z["next_radius"] <= 70 or obs["alive"] <= 3

        if obs.get("offer"):
            order = UPG_LASERLINE if K["laser_ups"] else UPG_GUNLINE
            ids = [ch["id"] for ch in obs["offer"]["choices"]]
            act["pick"] = min(range(len(ids)), key=lambda i: order.index(ids[i]) if ids[i] in order else 99)

        _, gun = self.gear("gun")
        _, light = self.gear("lightning")
        _, laser = self.gear("laser")

        target, vis, danger, crowd = self.appraise(obs, hpf, endgame)
        finishing = target is not None and target["_d"] < 120 and \
            (target.get("hp", 1400) < 200 or target.get("hp", 1400) < me["hp"] * 0.5)

        # ---- when to back off
        if self.phase == "hunt":
            near = sum(1 for e in vis if e["t"] == t and e.get("_d", 999) < 160)
            losing = (near >= 2 and hpf < 0.55) or (near >= 3 and hpf < 0.8)
            if target is not None:
                tf = target.get("hp", 1400) / max(target.get("max_hp", 1400), 1)
                if hpf < 0.35 and tf > hpf + 0.1:
                    losing = True
                if tf < 0.25 and hpf > 0.2 and near <= 2:
                    losing = False
            if losing:
                self.lost_at = t
            self.backing = self.lost_at > 0 and t - self.lost_at < 1.5
        else:
            outgunned = target is not None and "laser" in (target.get("weapons") or []) and laser is None \
                and target["_d"] > 55 and hpf < 0.8
            swamped = crowd >= 3 or (crowd == 2 and hpf < 0.65) or outgunned
            if not self.backing and ((hpf < K["pull_hp"] and t - self.danger_t < 2) or swamped):
                self.backing = True
            if self.backing and not swamped and (hpf > K["steady_hp"] or (t - self.danger_t > 7 and hpf > 0.55)):
                self.backing = False

        # ---- zone timing
        gx, gy = self.safe_point(z, self.pos[0], self.pos[1])
        zd = math.hypot(gx - self.pos[0], gy - self.pos[1])
        need = zd * 1.45 / max(me["stats"].get("walk_speed", 11.7), 1) + 4
        leave_by = self.must_leave_by(z, self.pos[0], self.pos[1], 8)
        zone_now = (not me["in_zone"]) or t + need + 3 >= leave_by
        zone_soon = t + need + 14 >= leave_by

        move = (0.0, 0.0)
        boost = False
        mode = "idle"
        shoot_drone = None
        shoot_crate = None
        shell = self.dodge(obs)
        if shell is not None:
            move, boost, mode = shell, me["energy"] > 15, "shell"
        elif zone_now and zd > 2:
            move, mode = self.go(gx, gy), "zone"
            boost = ((not me["in_zone"]) or t + need >= leave_by) and me["energy"] > 5
        elif self.backing and not finishing and not endgame:
            mv = self.take_cover(vis)
            if mv is not None:
                move, mode = mv, "cover"
                boost = me["energy"] > 10 and (mv[0] or mv[1])
            elif target is not None:
                move, mode = self.brawl_step(obs, target, vis, 1.0), "cornered"
            else:
                move, mode = self.go(gx, gy), "flee"
                if t - self.drone_hit_t < 3 and move == (0.0, 0.0):
                    s = 1.0 if (t % 3) < 1.5 else -1.0
                    move = (-math.sin(self.drone_bearing) * s, math.cos(self.drone_bearing) * s)
        elif self.phase == "scout" and target is None and t - self.last_hit_t < 2.5 and not endgame \
                and self.threat_bearing is not None:
            ghost = {"pos": [self.pos[0] + math.cos(self.threat_bearing[1]) * 80,
                             self.pos[1] + math.sin(self.threat_bearing[1]) * 80, self.pos[2]], "cubes": 0}
            mv = self.take_cover([ghost] + vis)
            if mv is not None:
                move, mode = mv, "hide"
                boost = me["energy"] > 30 and (mv[0] or mv[1])
        if mode == "idle" and target is not None:
            hot_laser = laser is not None and laser.get("charge", 0) >= laser["stats"].get("charge", 0.75) - 0.3
            want_sight = 1.0
            if K["finish_r"] and target.get("hp", 1400) < 0.38 * target.get("max_hp", 1400) and hpf > 0.42 and crowd <= 2:
                move = self.brawl_step(obs, target, vis, want_sight, range_override=K["finish_r"])
            else:
                move = self.brawl_step(obs, target, vis, want_sight)
            if light is not None and target["_d"] > 50 and me["energy"] > 55:
                boost = True
            mode = "fight"
            if K["cube_fight_r"]:
                rr = K["cube_fight_r"] * (0.57 if hpf <= 0.5 else 1.0)
                cb = None
                for c in self.items["cube"].values():
                    dc = dist2(c["pos"], self.pos)
                    if dc < rr:
                        rr, cb = dc, c
                if cb is not None:
                    move, mode = self.go(cb["pos"][0], cb["pos"][1]), "fight-cube"
            _ = hot_laser
        elif mode == "idle" and zone_soon and zd > 2:
            move, mode = self.go(gx, gy), "zone"
        elif mode == "idle":
            pick = self.pick_loot(obs, hpf, danger)
            if pick is not None:
                kind, iid, pos = pick
                mode = "loot-" + kind
                d = dist2(pos, self.pos)
                if kind == "drone":
                    dr = self.drones.get(iid)
                    if dr is not None and t - dr["t"] < 1 and d < 75 and self.clear_shot(dr):
                        shoot_drone = dr
                        move = self.brawl_step(obs, dr, vis, 1.0)
                    else:
                        move = self.go(pos[0], pos[1])
                elif kind == "crate":
                    cp = [pos[0], pos[1], pos[2] + self.cfg["crates"].get("height", 2.4) * 0.5]
                    if d < 50 and (self.world is None or self.world.los(self.muzzle(), cp)):
                        shoot_crate = cp
                        if d < 12:
                            move = unit(self.pos[0] - pos[0], self.pos[1] - pos[1])
                    else:
                        move = self.go(pos[0], pos[1])
                else:
                    move = self.go(pos[0], pos[1])
                    if kind == "drop" and d < 2.5:
                        move = (0.0, 0.0)
            elif zd > 3 and (self.phase == "hunt" or z["next_radius"] <= 30):
                move, mode = self.go(gx, gy), "zone-walk"
            elif self.phase == "scout":
                post = self.scout_post(obs)
                if post is not None and dist2(post, self.pos) > 3:
                    move, mode = self.go(*post), "post"
                    if not danger and t - self.last_hit_t > 6:
                        q = min(1.0, 4.6 / max(me["stats"].get("walk_speed", 11.7), 1))  # silent walk
                        move = (move[0] * q, move[1] * q)

        # ---- dodges and stuck recovery
        dx, dy, las = self.sidestep(obs)
        if dx or dy:
            mx, my = move[0] * 0.4 + dx * 0.9, move[1] * 0.4 + dy * 0.9
            L = math.hypot(mx, my)
            move = (mx / L, my / L) if L > 1 else (mx, my)
            if las and me["energy"] > 25:
                boost = True
        move, sj, sb = self.unstick(move)

        # ---- fire control
        aim, fire = self.fire(obs, target, shoot_drone, shoot_crate, vis, move, hpf)

        # ---- reload
        if gun is not None and gun.get("reload", 0) <= 0 and gun.get("ammo", 99) < gun["stats"].get("mag", 12):
            if target is None and shoot_drone is None and shoot_crate is None and \
                    (gun.get("ammo", 99) <= gun["stats"].get("mag", 12) * 0.6 or t - self.danger_t > 3):
                act["reload"] = True

        # ---- weapon pickup in reach
        for p in obs.get("pickups", []):
            if dist2(p["pos"], self.pos) > self.cfg["loot"]["pickup_radius"] - 0.3 or abs(p["pos"][2] - self.pos[2]) > 3.6:
                continue
            if not self.wants_gear(p["kind"]):
                continue
            w1 = me["weapons"][1]
            if w1 is not None and me["weapons"][0] is not None:
                if w1["kind"] == "laser" and w1.get("charge", 0) > 0:
                    break
                act["drop"] = 1
            act["take"] = p["id"]
            break

        act.update(move=[round(move[0], 3), round(move[1], 3)], aim=[round(aim[0], 4), round(aim[1], 4)],
                   fire=fire, boost=bool((boost or sb) and (move[0] or move[1]) and me["energy"] > 8),
                   jump=bool(sj))
        self.last_move = move
        self.mode = mode
        mf.draw.label(f"{self.phase}:{mode} hp{me['hp']:.0f} c{me['cubes']}")
        return act

    # ================================================================== fire control
    def lead_aim(self, mz, tp, tv, speed, ground_target):
        tt_extra = 0.0
        px = py = pz = 0.0
        for _ in range(4):
            tt = LATENCY + tt_extra
            px, py, pz = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt, tp[2]
            if not ground_target:
                pz += tv[2] * tt * 0.5
            d = math.sqrt((px - mz[0]) ** 2 + (py - mz[1]) ** 2 + (pz - mz[2]) ** 2)
            tt_extra = max(0.0, d - 3) / max(speed, 1)
        return math.atan2(py - mz[1], px - mz[0]), math.atan2(pz - mz[2], math.hypot(px - mz[0], py - mz[1]))

    def fire(self, obs, target, drone, crate, vis, move, hpf):
        me = self.me
        st = me["stats"]
        t = self.t
        fire = [False, False]
        mz = self.muzzle()
        _, gun = self.gear("gun")
        defend = False
        if target is None and t - self.drone_hit_t < 3:
            best, off_best = None, 0.7
            for dr in self.drones.values():
                if t - dr["t"] > 0.5 or dist2(dr["pos"], self.pos) > 150 or not self.clear_shot(dr):
                    continue
                off = abs(wrap(math.atan2(dr["pos"][1] - self.pos[1], dr["pos"][0] - self.pos[0]) - self.drone_bearing))
                if off < off_best:
                    best, off_best = dr, off
            if best is not None:
                drone, crate, defend = best, None, True
        tp = tv = None
        radius = height = 0.0
        is_mech = False
        if target is not None:
            sc = self.scale(target.get("cubes", 0))
            radius, height = self.cfg["mech"]["radius"] * sc, self.cfg["mech"]["height"] * sc
            tp = [target["pos"][0], target["pos"][1], target["pos"][2] + height * 0.5]
            tv = target["vel"]
            if self.world is not None and not self.world.los(mz, tp):
                tp[2] = target["pos"][2] + height * 0.85
            is_mech = True
        elif drone is not None:
            radius, height = self.cfg["npc"].get("radius", 1.8), self.cfg["npc"].get("height", 3.5)
            tp = [drone["pos"][0], drone["pos"][1], drone["pos"][2] + height * 0.5]
            tv = drone.get("vel", [0, 0, 0])
        elif crate is not None:
            radius, height = self.cfg["crates"].get("radius", 1.4), self.cfg["crates"].get("height", 2.4)
            tp, tv = crate, [0, 0, 0]
        if tp is not None and not is_mech and (self.backing or hpf < 0.5) and not defend:
            tp = None
        if tp is None:
            # keep a charged laser from wasting itself; watch the most relevant direction
            for i, w in enumerate(me["weapons"]):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = w["charge"] < w["stats"].get("charge", 0.75) + w["stats"].get("hold_s", 2) - 0.35
            watch = None
            for e in vis:
                if e.get("_hot") or self.struck_ago(e["id"]) < 6:
                    if watch is None or dist2(e["pos"], self.pos) < dist2(watch["pos"], self.pos):
                        watch = e
            if watch is not None:
                yaw = math.atan2(watch["pos"][1] - self.pos[1], watch["pos"][0] - self.pos[0])
            elif t - self.drone_hit_t < 2.5:
                yaw = self.drone_bearing
            elif t - self.last_hit_t < 2.5 and self.threat_bearing is not None:
                yaw = self.threat_bearing[1]
            elif move[0] or move[1]:
                yaw = math.atan2(move[1], move[0]) + 1.15 * math.sin(t * 1.3)
            else:
                yaw = me["aim"][0] + 0.35
            return [wrap(yaw), 0.0], fire

        dist = math.hypot(tp[0] - self.pos[0], tp[1] - self.pos[1])
        on_ground = abs(tv[2]) < 0.5
        gy = gp = 0.0
        gun_ok = False
        if gun is not None:
            gy, gp = self.lead_aim(mz, tp, tv, gun["stats"].get("speed", 252), on_ground)
            gun_ok = dist <= gun["stats"].get("range", 230) * 0.96
        hp_pt = [tp[0] + tv[0] * (LATENCY + 0.05), tp[1] + tv[1] * (LATENCY + 0.05), tp[2]]
        hy = math.atan2(hp_pt[1] - mz[1], hp_pt[0] - mz[0])
        hpitch = math.atan2(hp_pt[2] - mz[2], math.hypot(hp_pt[0] - mz[0], hp_pt[1] - mz[1]))
        aim = [gy, gp] if gun is not None else [hy, hpitch]
        tol_y = math.atan2(radius * 0.75, max(dist, 1.0))
        tol_p = math.atan2(height * 0.4, max(dist, 1.0))
        ay, ap = me["aim"]

        def settles(wy, wp):
            ey = max(0.0, abs(wrap(wy - ay)) - st.get("turn_rate", 4) * 0.05)
            ep = max(0.0, abs(wp - ap) - st.get("pitch_rate", 3) * 0.05)
            return ey < tol_y and ep < tol_p

        for i, w in enumerate(me["weapons"][:2]):
            if not w:
                continue
            ws = w["stats"]
            if w["kind"] == "gun":
                if gun_ok and w.get("ammo", 0) > 0 and w.get("reload", 0) <= 0 and settles(gy, gp):
                    fire[i] = True
            elif w["kind"] == "lightning":
                cone = math.radians(ws.get("cone_deg", 30))
                if dist < ws.get("range", 45) - 1.5 and abs(wrap(hy - ay)) < cone * 0.5 - 0.05 \
                        and me["energy"] >= ws.get("energy", 3):
                    fire[i] = True
            elif w["kind"] == "laser":
                if not is_mech or dist > ws.get("range", 280) * 0.97:
                    continue
                full = ws.get("charge", 0.75)
                ch = w.get("charge", 0)
                # release early if a partial charge already kills
                if ch >= ws.get("min_charge", 0.15) and ch < full - 0.051:
                    dmg = ws.get("damage", 315) * (ws.get("min_frac", 0.35) + (1 - ws.get("min_frac", 0.35)) * ch / full)
                    dmg *= 1 + self.cfg["cubes"].get("damage_per_cube", 0.04) * min(me["cubes"], self.cfg["cubes"].get("stat_cap", 10))
                    if "mortar" in (target.get("weapons") or []):
                        dmg *= 1 + ws.get("cookoff", 0.6)
                    if dmg * 0.8 >= target.get("hp", 1400) and abs(wrap(hy - ay)) < tol_y * 0.9 and abs(hpitch - ap) < tol_p:
                        aim = [hy, hpitch]
                        fire[i] = False
                        continue
                if ch < full - 0.051:
                    fire[i] = True
                    if ch > full * 0.45:
                        aim = [hy, hpitch]
                else:
                    aim = [hy, hpitch]
                    fire[i] = not (abs(wrap(hy - ay)) < tol_y * 0.9 and abs(hpitch - ap) < tol_p)
        aim[1] = max(st.get("min_pitch", -0.7), min(st.get("max_pitch", 1.3), aim[1]))
        return aim, fire
