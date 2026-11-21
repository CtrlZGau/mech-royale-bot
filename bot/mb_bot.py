"""mechbot: survive, farm cubes, and win fights with hitscan weapons.

Layers each decision:
  memory   -> who attacked us, sounds, enemies
  economy  -> upgrade picks, weapon pickups
  strategy -> mode: fight / retreat / zone / loot / farm / wander
  tactics  -> movement (path following, strafing, dodging) + weapon fire control
"""
import math
import random
import time

import mechfight as mf

try:
    from mb_world import World, Planner
except Exception:  # never let a map problem kill the bot
    World = Planner = None

PI = math.pi
TAU = 2 * PI

WEAPON_SCORE = {"gun": 3.6, "lightning": 3.2, "laser": 3.1, "mortar": 1.0}
UPGRADE_SCORE = {
    "plating": 10, "armor": 9.5, "nanites": 7, "laser_damage": 7.5, "lightning_efficiency": 7,
    "gun_damage": 6.5, "laser_charge": 6.5, "gun_rate": 6, "lightning_range": 6, "lightning_chain": 4,
    "gyros": 5, "servos": 4.5, "gun_mag": 4.5, "gun_velocity": 4, "capacitor": 3.5, "reactor": 3.5,
    "sensors": 3, "jets": 2.5, "laser_pierce": 2, "mortar_reload": 2, "mortar_velocity": 1.5, "mortar_splash": 1.5,
}


def wrap(a):
    return (a + PI) % TAU - PI


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def unit(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-6 else (0.0, 0.0)


class Bot:
    name = "mechbot"

    def __init__(self, hello):
        c = hello["config"]
        self.cfg = c
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        self.planner = None
        if World is not None:
            try:
                t0 = time.time()
                m = hello["map"]
                self.world = World(m["dir"], c["mech"].get("step_up", 1.2), c["mech"].get("max_slope_deg", 50))
                self.planner = Planner(self.world)
                mf.log(f"map loaded in {time.time() - t0:.2f}s")
            except Exception as e:
                mf.log(f"map load failed: {e!r}")
        self.pickup_r = c["loot"]["pickup_radius"]
        self.cube_r = c["cubes"]["pickup_radius"]
        self.stat_cap = c["cubes"].get("stat_cap", 10)
        # memory
        self.last_hit_t = -99.0      # we took damage
        self.last_dealt_t = -99.0    # we dealt damage
        self.attackers = {}          # id -> (t, bearing)
        self.threat_bearing = None   # (t, yaw) last damage/sound direction
        self.enemy_seen = {}         # id -> last obs dict + 't'
        self.failed_take = {}        # pickup id -> t
        self.dead_crates = set()
        # movement state
        self.path_goal = None
        self.path = []
        self.path_t = -99.0
        self.hist = []               # (t, x, y)
        self.unstick_until = -1.0
        self.unstick_dir = (0.0, 0.0)
        self.strafe = 1
        self.strafe_until = 0.0
        self.wander = None
        self.wander_t = -99.0
        self.last_move = (0.0, 0.0)
        self.mode = "start"
        self.note = ""

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        self.t = t = obs["t"]
        me = obs["self"]
        self.me = me
        self.pos = me["pos"]
        self.obs = obs
        act = {}

        self.update_memory(obs)
        if obs.get("offer"):
            act["pick"] = self.choose_upgrade(obs["offer"])
        self.manage_weapons(obs, act)

        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        target = self.pick_target(enemies)

        hpf = me["hp"] / max(1.0, me["max_hp"])
        move = None
        boost = False
        jump = False
        aim_target = None  # (point, vel, radius, kind)

        zone_urgent, zone_goal = self.zone_status(obs)

        if target is not None:
            tp = target["pos"]
            d = dist2(tp, self.pos)
            enemy_hp = target.get("hp", 1400)
            n_close = sum(1 for e in enemies if dist2(e["pos"], self.pos) < 160)
            retreat = hpf < 0.3 and (enemy_hp > me["hp"] * 1.2 or n_close >= 2)
            retreat = retreat or (hpf < 0.5 and n_close >= 3)
            # losing the damage race: break off and heal while we still can
            retreat = retreat or (hpf < 0.4 and enemy_hp > me["hp"] * 1.6) or (hpf < 0.55 and n_close >= 2)
            aim_target = self.mech_aim_target(target)
            # Don't start fights we don't need (the winning house bots are the passive ones).
            # Fight back when attacked, when it's close, when it's nearly dead, or when it's lining up a laser on us.
            a = self.attackers.get(target["id"])
            attacked_by = a is not None and t - a[0] < 8
            engaged_any = any(t - a2[0] < 6 for a2 in self.attackers.values())
            weak = enemy_hp < 0.4 * target.get("max_hp", 1400) and d < 110 and hpf > 0.35
            threat = bool(target.get("charging")) and self.aimed_at_me(target, 0.15)
            # a mech pointing its gun at us (or walking at us) is coming for us: shoot first
            tv = target.get("vel", [0, 0, 0])
            closing = (tv[0] * (self.pos[0] - tp[0]) + tv[1] * (self.pos[1] - tp[1])) / max(d, 1.0) > 4.0
            hunting_us = d < 130 and (self.aimed_at_me(target, 0.12) or (closing and self.aimed_at_me(target, 0.5)))
            # a good opportunity: a lone mech in reliable gun range, and we're healthier
            opportunity = d < 100 and hpf > 0.8 and enemy_hp <= me["hp"] and n_close == 1
            fight = attacked_by or d < 70 or weak or threat or hunting_us or opportunity or (engaged_any and d < 120)
            self.cur_target = (target["id"], t)
            if retreat:
                self.mode = "retreat"
                move, boost = self.retreat_move(enemies, zone_goal)
            elif fight:
                self.mode = "fight"
                move, boost = self.fight_move(target, d, zone_goal)
            else:
                # ignore it: carry on with our plan (no farming noise), keep our front armor toward it
                self.mode = "avoid"
                aim_target = None
                self.face = target
                goal, kind, extra = self.choose_goal(obs, npcs, zone_urgent, zone_goal, hpf, quiet=True)
                move = self.navigate(goal) if goal else (0.0, 0.0)
                if kind == "drop_hold" and dist2(goal, self.pos) < extra * 0.5:
                    move = (0.0, 0.0)
            # cubes nearby are worth a detour unless the enemy is on top of us
            if self.mode != "retreat" and d > 40:
                cube = min(obs.get("cubes", []), key=lambda c: dist2(c["pos"], self.pos), default=None)
                if cube is not None and dist2(cube["pos"], self.pos) < 25 and abs(cube["pos"][2] - self.pos[2]) < 4:
                    move = self.blend(self.navigate((cube["pos"][0], cube["pos"][1])), move, 0.75)
                    self.note = "cube"
            if zone_urgent:
                zm = self.navigate(zone_goal)
                move = self.blend(zm, move, 0.65)
                boost = boost or me["energy"] > 30
        else:
            goal, kind, extra = self.choose_goal(obs, npcs, zone_urgent, zone_goal, hpf)
            self.mode = kind
            if kind in ("npc", "crate") and extra is not None:
                aim_target = extra
                tp = extra[0]
                d = dist2(tp, self.pos)
                stand = 45.0 if kind == "npc" else 25.0
                if d > stand or not self.has_los(tp):
                    move = self.navigate(goal)
                else:
                    move = self.strafe_move(tp, d, stand)
            elif kind == "drop_hold":
                move = (0.0, 0.0) if dist2(goal, self.pos) < extra * 0.5 else self.navigate(goal)
            else:
                move = self.navigate(goal) if goal else (0.0, 0.0)
            boost = zone_urgent or (not me["in_zone"]) or (kind in ("drop", "cube", "pickup") and me["energy"] > 60)

        # dodges override movement
        dodge = self.dodge(obs)
        if dodge is not None:
            move = dodge
            boost = me["energy"] > 15
        for e in enemies:
            if e.get("charging") and self.aimed_at_me(e, 0.12):
                jump = me["grounded"] and me["energy"] > 25
                boost = True
        # stuck handling
        move, sj, sb = self.unstick(move)
        jump = jump or sj
        boost = boost or sb

        # aim + fire
        fire = [False, False]
        if aim_target is not None:
            aim, fire = self.fire_control(aim_target, act)
        else:
            aim = self.scan_aim(move)
        self.reload_logic(act, aim_target is None)

        mx, my = move
        act.update(move=[round(mx, 3), round(my, 3)], aim=[round(aim[0], 4), round(aim[1], 4)],
                   fire=fire, boost=bool(boost and (me["energy"] > 8)), jump=bool(jump))
        self.last_move = move
        el = (time.perf_counter() - t0) * 1000
        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} c{me['cubes']:.0f} {self.note} {el:.0f}ms")
        return act

    # ------------------------------------------------------------------ memory
    def update_memory(self, obs):
        t = self.t
        for ev in obs.get("events", []):
            k = ev.get("type")
            if k == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_hit_t = t
                    if ev.get("bearing") is not None:
                        self.threat_bearing = (t, ev["bearing"])
                    if ev.get("by") is not None and ev["by"] < 100:
                        self.attackers[ev["by"]] = (t, ev.get("bearing"))
            elif k == "hit":
                self.last_dealt_t = t
            elif k == "take_failed":
                self.failed_take[ev["id"]] = t
            elif k == "took":
                self.path_goal = None
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload") \
                    and s["dist"] in ("near", "mid") and s["age"] < 0.5:
                if self.threat_bearing is None or t - self.threat_bearing[0] > 1.5:
                    self.threat_bearing = (t, s["bearing"])
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.enemy_seen[m["id"]] = dict(m, t=t)
        hist = self.hist
        hist.append((t, self.pos[0], self.pos[1]))
        while hist and t - hist[0][0] > 1.6:
            hist.pop(0)

    # ------------------------------------------------------------------ economy
    def choose_upgrade(self, offer):
        best, bi = -1e9, 0
        for i, ch in enumerate(offer["choices"]):
            s = UPGRADE_SCORE.get(ch["id"], 1.0)
            if s > best:
                best, bi = s, i
        return bi

    def held(self):
        return [w["kind"] if w else None for w in self.me["weapons"]]

    def weapon_value(self, kind):
        return WEAPON_SCORE.get(kind, 0.5)

    def wanted_pickup(self, p):
        """(score gain, drop slot or None) for taking pickup p, or None."""
        held = self.held()
        k = p["kind"]
        if k in held:
            return None
        v = self.weapon_value(k)
        if None in held:
            return (v, None)
        # both full: replace the worst, never a charging laser
        worst = min((0, 1), key=lambda i: self.weapon_value(held[i]))
        w = self.me["weapons"][worst]
        if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
            return None
        gain = v - self.weapon_value(held[worst])
        if gain > 0.15:
            return (gain, worst)
        return None

    def manage_weapons(self, obs, act):
        x, y, z = self.pos
        for p in obs.get("pickups", []):
            if self.t - self.failed_take.get(p["id"], -99) < 2.0:
                continue
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.pickup_r - 0.3 and abs(p["pos"][2] - z) < 3.8:
                w = self.wanted_pickup(p)
                if w:
                    act["take"] = p["id"]
                    if w[1] is not None:
                        act["drop"] = w[1]
                    return

    def reload_logic(self, act, idle):
        for w in self.me["weapons"]:
            if w and w["kind"] == "gun":
                if w.get("reload", 0) == 0 and idle and w.get("ammo", 99) < w["stats"].get("mag", 12) * 0.6 \
                        and self.t - self.last_dealt_t > 1.5:
                    act["reload"] = True

    # ------------------------------------------------------------------ strategy
    def zone_at(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * f]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def zone_status(self, obs):
        """(urgent, goal). Goal is a safe point inside the next circle, near us."""
        z = obs["zone"]
        t = self.t
        nc, nr = z["next_center"], z["next_radius"]
        x, y = self.pos[0], self.pos[1]
        dn = math.hypot(x - nc[0], y - nc[1])
        inner = max(0.0, nr - max(8.0, min(35.0, nr * 0.35)))
        if dn > inner:
            ux, uy = unit(x - nc[0], y - nc[1])
            goal = (nc[0] + ux * inner, nc[1] + uy * inner)
        else:
            goal = (x, y)
        if nr < 1:
            goal = (nc[0], nc[1])
        speed = self.me["stats"].get("walk_speed", 11.7)
        travel = max(0.0, dn - inner) / (speed * 0.7)
        # when does the boundary pass our position?
        t_exit = None
        tt = t
        end = max(z["shrink_end"], t)
        while tt <= end + 0.01:
            c, r = self.zone_at(z, tt)
            if math.hypot(x - c[0], y - c[1]) > r - 4:
                t_exit = tt
                break
            tt += 2.0
        urgent = (not self.me["in_zone"]) or (t_exit is not None and t_exit - t < travel + 12)
        self.zone_inner = (nc, inner)
        return urgent, goal

    def safe_spot(self, p, slack=0.0):
        nc, inner = self.zone_inner
        return math.hypot(p[0] - nc[0], p[1] - nc[1]) < inner + 10 + slack or self.zone_ok_now(p)

    def zone_ok_now(self, p):
        z = self.obs["zone"]
        c, r = self.zone_at(z, self.t + 20)
        return math.hypot(p[0] - c[0], p[1] - c[1]) < r - 15

    def choose_goal(self, obs, npcs, zone_urgent, zone_goal, hpf, quiet=False):
        """Returns (goal xy, mode, extra)."""
        x, y = self.pos[0], self.pos[1]
        self.note = ""
        if zone_urgent:
            return zone_goal, "zone", None
        healing = (hpf < 0.8 and self.t - self.last_hit_t < 25) or quiet
        cands = []
        # cubes: always worth a detour
        for c in obs.get("cubes", []):
            d = dist2(c["pos"], self.pos)
            if d < 90 and self.zone_ok_now(c["pos"]) and abs(c["pos"][2] - self.pos[2]) < 25:
                cands.append((d * 0.6, (c["pos"][0], c["pos"][1]), "cube", None))
        # weapon pickups we want
        for p in obs.get("pickups", []):
            if self.t - self.failed_take.get(p["id"], -99) < 4:
                continue
            w = self.wanted_pickup(p)
            d = dist2(p["pos"], self.pos)
            if w and d < 120 and self.zone_ok_now(p["pos"]) and abs(p["pos"][2] - self.pos[2]) < 25:
                cands.append((d * (0.5 if None in self.held() else 0.8), (p["pos"][0], p["pos"][1]), "pickup", None))
        # supply drops
        for dr in obs.get("drops", []):
            d = dist2(dr["pos"], self.pos)
            if dr.get("holder") not in (None, self.me["id"]) and dr.get("progress", 0) > 0:
                continue
            arrive = d / 15.0
            if d < 220 and arrive + 3 > dr["lands_in"] - 2:
                if d < dr["radius"] * 0.8 and dr["lands_in"] <= 0:
                    return (dr["pos"][0], dr["pos"][1]), "drop_hold", dr["radius"]
                cands.append((d * 0.45, (dr["pos"][0], dr["pos"][1]), "drop", None))
        # farm: crates and NPCs, only healthy
        if not healing and hpf > 0.6:
            for cr in obs.get("crates", []):
                d = dist2(cr["pos"], self.pos)
                if d < 110 and self.zone_ok_now(cr["pos"]):
                    tp = [cr["pos"][0], cr["pos"][1], cr["pos"][2] + 1.2]
                    cands.append((d * 0.9 + 10, (cr["pos"][0], cr["pos"][1]), "crate", (tp, [0, 0, 0], 1.4, "crate")))
        if not healing and hpf > 0.75 and self.t - self.last_hit_t > 3:
            for n in npcs:
                d = dist2(n["pos"], self.pos)
                if d < 130 and n.get("hp", 350) > 0:
                    tp = [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75]
                    cands.append((d + 25, (n["pos"][0], n["pos"][1]), "npc", (tp, n.get("vel", [0, 0, 0]), 1.8, "npc")))
        if cands:
            cands.sort(key=lambda c: c[0])
            _, g, k, extra = cands[0]
            if not healing or k in ("cube", "drop", "drop_hold"):
                return g, k, extra
        if healing:
            # wait (silently) in cover inside the safe circle and let regen work
            nc, inner = self.zone_inner
            if math.hypot(x - nc[0], y - nc[1]) < inner + 5:
                cover = self.find_cover(self.threat_points())
                if cover is not None and dist2(cover, self.pos) > 2.5:
                    return cover, "heal", None
                return (x, y), "heal", None
            return zone_goal, "zone", None
        # nothing to do: wander inside the safe circle to find loot
        nc, inner = self.zone_inner
        if math.hypot(x - nc[0], y - nc[1]) > inner + 5:
            return zone_goal, "zone", None
        if self.wander is None or self.t - self.wander_t > 25 or dist2(self.wander, self.pos) < 10 \
                or math.hypot(self.wander[0] - nc[0], self.wander[1] - nc[1]) > inner:
            for _ in range(20):
                a = self.rng.uniform(-PI, PI)
                r = inner * math.sqrt(self.rng.random()) * 0.85
                g = (nc[0] + r * math.cos(a), nc[1] + r * math.sin(a))
                if self.world is None or not self.world.prism[self.world.idx(*g)]:
                    break
            self.wander, self.wander_t = g, self.t
        return self.wander, "wander", None

    # ------------------------------------------------------------------ targeting
    def pick_target(self, enemies):
        best, bs = None, 1e18
        x, y, z = self.pos
        eye = [x, y, z + self.me["stats"].get("eye_height", 8)]
        for e in enemies:
            d = dist2(e["pos"], self.pos)
            if d > 260:
                continue
            s = d
            if self.world is not None:
                tp = [e["pos"][0], e["pos"][1], e["pos"][2] + 5]
                if not self.world.los(eye, tp):
                    s += 90
            a = self.attackers.get(e["id"])
            if a and self.t - a[0] < 4:
                s -= 150 if self.t - a[0] < 1.5 else 70
            s += e.get("hp", 1400) * 0.03
            ct = getattr(self, "cur_target", None)
            if ct and ct[0] == e["id"] and self.t - ct[1] < 3:
                s -= 45
            if s < bs:
                best, bs = e, s
        return best

    def mech_aim_target(self, e):
        size = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * e.get("cubes", 0)
        tp = [e["pos"][0], e["pos"][1], e["pos"][2] + 4.5 * size]
        return (tp, e.get("vel", [0, 0, 0]), 2.5 * size, "mech", e)

    def has_los(self, tp, from_muzzle=True):
        if self.world is None:
            return True
        x, y, z = self.pos
        h = self.me["stats"].get("muzzle_height" if from_muzzle else "eye_height", 7)
        return self.world.los([x, y, z + h], tp)

    def aimed_at_me(self, e, tol):
        yaw = math.atan2(self.pos[1] - e["pos"][1], self.pos[0] - e["pos"][0])
        return abs(wrap(e.get("aim", [9, 0])[0] - yaw)) < tol

    # ------------------------------------------------------------------ fire control
    def fire_control(self, tgt, act):
        tp, tv, trad, kind = tgt[0], tgt[1], tgt[2], tgt[3]
        me = self.me
        x, y, z = self.pos
        # the shot leaves during the next decision window: aim from where we'll be
        x += me["vel"][0] * 0.05
        y += me["vel"][1] * 0.05
        mh = me["stats"].get("muzzle_height", 7)
        mz = z + mh
        d = math.hypot(tp[0] - x, tp[1] - y)
        dz = tp[2] - mz
        los = self.has_los(tp)
        ay, ap = me["aim"]
        weapons = me["weapons"]
        slots = {w["kind"]: i for i, w in enumerate(weapons) if w}
        fire = [False, False]

        def direct(lead_t=0.0):
            px, py, pz = tp[0] + tv[0] * lead_t, tp[1] + tv[1] * lead_t, tp[2] + max(-3, min(3, tv[2] * lead_t))
            return math.atan2(py - y, px - x), math.atan2(pz - mz, math.hypot(px - x, py - y))

        def err(yaw, pitch):
            return abs(wrap(ay - yaw)), abs(ap - pitch)

        tol = math.atan2(trad * 0.75, max(d, 1.0))

        # gun lead solution
        gun_aim = None
        if "gun" in slots:
            gs = weapons[slots["gun"]]["stats"]
            sp = gs.get("speed", 252)
            lt = d / sp
            for _ in range(3):
                px, py, pz = tp[0] + tv[0] * lt, tp[1] + tv[1] * lt, tp[2] + max(-3, min(3, tv[2] * lt))
                lt = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - mz) ** 2) / sp
            gun_aim = direct(lt)

        # choose the primary weapon that drives the torso
        primary = None
        lw = weapons[slots["lightning"]] if "lightning" in slots else None
        laser = weapons[slots["laser"]] if "laser" in slots else None
        gun = weapons[slots["gun"]] if "gun" in slots else None
        mortar = weapons[slots["mortar"]] if "mortar" in slots else None
        l_energy = lw["stats"].get("energy", 3) if lw else 0
        if lw and d < lw["stats"].get("range", 45) * 0.92 and me["energy"] > l_energy + 4 and los:
            primary = "lightning"
        elif laser and (laser.get("charge", 0) > 0 or laser["cooldown"] < 0.5) and d < laser["stats"].get("range", 280) * 0.95 \
                and (los or laser.get("charge", 0) > 0):
            primary = "laser"
        elif gun and gun.get("reload", 0) == 0 and gun.get("ammo", 1) > 0 and d < gun["stats"].get("range", 230) and los:
            primary = "gun"
        elif mortar and d > 20:
            primary = "mortar"
        elif laser:
            primary = "laser"
        elif gun:
            primary = "gun"

        if primary == "gun" or (primary is None and gun_aim):
            aim = gun_aim or direct()
        elif primary == "mortar":
            aim = self.mortar_aim(mortar, tp, tv, los) or direct()
        elif primary == "laser":
            aim = direct(0.1)
        else:
            aim = direct()

        # lightning: auto-targets within its cone
        if lw and lw["cooldown"] <= 0.1 and los and me["energy"] > l_energy + 4:
            cone = math.radians(lw["stats"].get("cone_deg", 30)) * 0.45
            yaw_t = math.atan2(tp[1] - y, tp[0] - x)
            if d < lw["stats"].get("range", 45) * 0.95 and abs(wrap(ay - yaw_t)) < cone:
                fire[slots["lightning"]] = True
        # gun
        tspeed = math.hypot(tv[0], tv[1])
        gun_max = gun["stats"].get("range", 230) if gun else 0
        if kind == "mech" and tspeed > 4:
            gun_max = min(gun_max, 125.0)
        if gun and gun_aim and los and gun.get("ammo", 1) > 0 and gun.get("reload", 0) == 0 and d < gun_max:
            ey, ep = err(*gun_aim)
            if ey < math.atan2(trad * 0.65, max(d, 1.0)) + 0.004 and ep < math.atan2(3.0, max(d, 1.0)) + 0.004:
                fire[slots["gun"]] = True
        # laser: hold to charge, release when full and on target
        if laser:
            ls = laser["stats"]
            ch = laser.get("charge", 0)
            full = ls.get("charge", 0.75)
            la = direct(0.1)
            ey, ep = err(*la)
            on = ey < tol + 0.005 and ep < 0.05 and los
            si = slots["laser"]
            if ch > 0:
                if ch >= full * 0.97 and on:
                    fire[si] = False  # release -> beam
                elif ch > full + ls.get("hold_s", 2) - 0.3 and not on:
                    fire[si] = True   # it will auto-fire anyway
                else:
                    fire[si] = True
            elif laser["cooldown"] <= 0.05 and los and d < ls.get("range", 280) * 0.95 and ey < 0.6:
                fire[si] = True  # start charging
        # mortar
        if mortar and mortar["cooldown"] <= 0.05 and d > 18:
            ma = self.mortar_aim(mortar, tp, tv, los)
            if ma:
                ey, ep = err(*ma)
                if ey < 0.03 and ep < 0.03:
                    fire[slots["mortar"]] = True
        if fire[0] or fire[1]:
            self.note = primary or ""
        return aim, fire

    def mortar_aim(self, mortar, tp, tv, los):
        s = mortar["stats"]
        v = s.get("speed", 72)
        g = s.get("gravity", 22)
        x, y, z = self.pos
        oz = z + self.me["stats"].get("height", 9) + 0.5
        aim_z = tp[2] - 3.0
        lt = 0.0
        sol = None
        for _ in range(3):
            px, py = tp[0] + tv[0] * lt, tp[1] + tv[1] * lt
            d = math.hypot(px - x, py - y)
            dz = aim_z - oz
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1:
                return None
            r = math.sqrt(disc)
            th = math.atan2(v * v - r, g * d) if los else math.atan2(v * v + r, g * d)
            mp = self.me["stats"].get("max_pitch", 1.3)
            if th > mp:
                th = math.atan2(v * v - r, g * d)
            lt = d / max(1e-3, v * math.cos(th))
            sol = (math.atan2(py - y, px - x), th)
        return sol

    def scan_aim(self, move):
        t = self.t
        f = getattr(self, "face", None)
        self.face = None
        if f is not None:
            return (math.atan2(f["pos"][1] - self.pos[1], f["pos"][0] - self.pos[0]), 0.0)
        if self.threat_bearing and t - self.threat_bearing[0] < 3.0:
            return (self.threat_bearing[1], 0.0)
        # recently seen enemy that left memory: look where it was
        recent = [e for e in self.enemy_seen.values() if t - e["t"] < 8]
        if recent:
            e = max(recent, key=lambda e: e["t"])
            return (math.atan2(e["pos"][1] - self.pos[1], e["pos"][0] - self.pos[0]), 0.0)
        mx, my = move
        base = math.atan2(my, mx) if (mx or my) else self.me["aim"][0]
        if not (mx or my):
            return (wrap(self.me["aim"][0] + 0.9), 0.0)
        return (wrap(base + 1.1 * math.sin(t * 1.3)), 0.0)

    # ------------------------------------------------------------------ movement
    def navigate(self, goal):
        if goal is None:
            return (0.0, 0.0)
        x, y = self.pos[0], self.pos[1]
        gx, gy = goal
        d = math.hypot(gx - x, gy - y)
        if d < 1.5:
            return (0.0, 0.0)
        w = self.world
        if w is None:
            return self.probe_dir(unit(gx - x, gy - y))
        if d < 25 and w.walk_clear(x, y, gx, gy):
            return unit(gx - x, gy - y)
        pl = self.planner
        replan = (self.path_goal is None or math.hypot(self.path_goal[0] - gx, self.path_goal[1] - gy) > 6
                  or self.t - self.path_t > 6 or not self.path)
        if replan and not pl.active:
            pl.start(x, y, gx, gy)
            self.path_goal = (gx, gy)
        if pl.active:
            budget = max(0.002, 0.015 - (time.perf_counter() - self._tick_start()))
            if pl.step(budget):
                self.path = list(pl.path)
                self.path_t = self.t
        if not self.path:
            return self.probe_dir(unit(gx - x, gy - y))
        # drop passed waypoints
        path = self.path
        while len(path) > 1 and math.hypot(path[0][0] - x, path[0][1] - y) < 4.0:
            path.pop(0)
        # string-pull: furthest of the next few waypoints we can walk straight to
        tgt = path[0]
        for k in range(min(len(path) - 1, 8), 0, -1):
            p = path[k]
            if math.hypot(p[0] - x, p[1] - y) < 30 and w.walk_clear(x, y, p[0], p[1]):
                tgt = p
                break
        if math.hypot(tgt[0] - x, tgt[1] - y) > 40:
            self.path = []  # we drifted off; replan
            self.path_goal = None
        mf.draw.line([x, y, self.pos[2] + 1], [tgt[0], tgt[1], self.pos[2] + 1], "#40c0ff")
        return unit(tgt[0] - x, tgt[1] - y)

    def _tick_start(self):
        return getattr(self, "_t0", time.perf_counter())

    def probe_dir(self, want, length=7.0):
        """Return a walkable direction close to `want`."""
        w = self.world
        if w is None or (want == (0.0, 0.0)):
            return want
        x, y = self.pos[0], self.pos[1]
        base = math.atan2(want[1], want[0])
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.2, -2.2, PI):
            a = base + off
            dx, dy = math.cos(a), math.sin(a)
            if w.walk_clear(x, y, x + dx * length, y + dy * length) and self.zone_safe_step(x + dx * length, y + dy * length):
                return (dx, dy)
        return want

    def zone_safe_step(self, px, py):
        z = self.obs["zone"]
        c, r = self.zone_at(z, self.t + 3)
        if math.hypot(px - c[0], py - c[1]) < r - 3:
            return True
        # already outside: only accept steps that get closer
        return math.hypot(px - c[0], py - c[1]) < math.hypot(self.pos[0] - c[0], self.pos[1] - c[1])

    def preferred_range(self, target):
        held = self.held()
        ew = target.get("weapons", [])
        d = dist2(target["pos"], self.pos)
        if "lightning" in held and d < 60:
            r = 28.0
        elif "laser" in held:
            r = 95.0
        else:
            r = 70.0
        if "lightning" in ew and "lightning" not in held:
            r = max(r, 65.0)
        return r

    def fight_move(self, target, d, zone_goal):
        x, y = self.pos[0], self.pos[1]
        tp = target["pos"]
        want_r = self.preferred_range(target)
        los = self.has_los([tp[0], tp[1], tp[2] + 4.5])
        if not los and d > 30:
            # chase / reposition toward them along a path
            if self.me["hp"] > target.get("hp", 1400) * 0.8:
                return self.navigate((tp[0], tp[1])), False
            return self.strafe_move(tp, d, want_r), False
        boost = self.me["energy"] > 45 and (self.t - self.last_hit_t < 1.0)
        return self.strafe_move(tp, d, want_r), boost

    def strafe_move(self, tp, d, want_r):
        x, y = self.pos[0], self.pos[1]
        if self.t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = self.t + self.rng.uniform(0.7, 1.6)
        ux, uy = unit(tp[0] - x, tp[1] - y)
        radial = max(-1.0, min(1.0, (d - want_r) / 25.0))
        px, py = -uy * self.strafe, ux * self.strafe
        mx, my = ux * radial + px * 0.9, uy * radial + py * 0.9
        want = unit(mx, my)
        w = self.world
        if w is not None:
            L = 7.0
            if not (w.walk_clear(x, y, x + want[0] * L, y + want[1] * L) and self.zone_safe_step(x + want[0] * L, y + want[1] * L)):
                # try the other strafe side first
                alt = unit(ux * radial - px * 0.9, uy * radial - py * 0.9)
                if w.walk_clear(x, y, x + alt[0] * L, y + alt[1] * L) and self.zone_safe_step(x + alt[0] * L, y + alt[1] * L):
                    self.strafe = -self.strafe
                    self.strafe_until = self.t + self.rng.uniform(0.7, 1.4)
                    return alt
                return self.probe_dir(want)
        return want

    def threat_points(self, enemies=()):
        """Eye positions of mechs that may be hunting us (visible now or seen in the last 12 s)."""
        pts = []
        seen = set()
        for e in enemies:
            pts.append([e["pos"][0], e["pos"][1], e["pos"][2] + 8])
            seen.add(e["id"])
        recent = sorted((e for e in self.enemy_seen.values() if self.t - e["t"] < 12 and e["id"] not in seen),
                        key=lambda e: -e["t"])
        for e in recent[:3]:
            pts.append([e["pos"][0], e["pos"][1], e["pos"][2] + 8])
        if not pts and self.threat_bearing and self.t - self.threat_bearing[0] < 10:
            b = self.threat_bearing[1]
            pts.append([self.pos[0] + 90 * math.cos(b), self.pos[1] + 90 * math.sin(b), self.pos[2] + 8])
        return pts[:4]

    def find_cover(self, threats, max_r=30.0):
        """A nearby standing spot hidden from every threat, preferring spots hugging a wall. Cached ~2 s."""
        w = self.world
        if w is None or not threats:
            return None
        c = getattr(self, "_cover", None)
        if c and self.t - c[1] < 2.0 and dist2(c[0], self.pos) < max_r + 10:
            # still hidden?
            p = [c[0][0], c[0][1], w.height(*c[0]) + 6]
            if all(not w.los(tp, p) for tp in threats):
                return c[0]
        x, y = self.pos[0], self.pos[1]
        best, bs = None, -1e9
        for r in (7.0, 14.0, 22.0, max_r):
            for k in range(12):
                a = k * TAU / 12 + r * 0.1
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not w.in_bounds(px, py, 6):
                    continue
                ci = w.idx(px, py)
                if not w.cost[ci] or w.prism[ci] or abs(w.H[ci] - self.pos[2]) > 4:
                    continue
                if not self.zone_ok_now((px, py)):
                    continue
                p = [px, py, w.H[ci] + 6]
                hidden = sum(1 for tp in threats if not w.los(tp, p))
                if hidden < len(threats):
                    continue
                sc = -r - (0 if w.cost[ci] >= 2 else 6)  # prefer near walls
                # don't run toward the threats
                for tp in threats:
                    if math.hypot(tp[0] - px, tp[1] - py) < math.hypot(tp[0] - x, tp[1] - y) - 5:
                        sc -= 15
                if sc > bs:
                    best, bs = (px, py), sc
            if best is not None and r >= 14:
                break
        self._cover = (best, self.t) if best else None
        return best

    def retreat_move(self, enemies, zone_goal):
        cover = self.find_cover(self.threat_points(enemies))
        if cover is not None:
            self.note = "cover"
            mv = self.navigate(cover)
            if mv != (0.0, 0.0):
                return mv, self.me["energy"] > 20
        return self.retreat_dir(enemies, zone_goal)

    def retreat_dir(self, enemies, zone_goal):
        x, y = self.pos[0], self.pos[1]
        ax = ay = 0.0
        for e in enemies:
            d = max(5.0, dist2(e["pos"], self.pos))
            ux, uy = unit(x - e["pos"][0], y - e["pos"][1])
            ax += ux / d
            ay += uy / d
        away = unit(ax, ay)
        # bias toward safe zone interior
        nc, inner = self.zone_inner
        zx, zy = unit(nc[0] - x, nc[1] - y)
        dz = math.hypot(nc[0] - x, nc[1] - y)
        zb = 0.8 if dz > inner * 0.8 else 0.25
        want = unit(away[0] + zx * zb, away[1] + zy * zb)
        # prefer a direction that breaks line of sight
        best, bs = None, -1e9
        w = self.world
        if w is not None:
            base = math.atan2(want[1], want[0])
            for off in (0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
                a = base + off
                dx, dy = math.cos(a), math.sin(a)
                L = 12.0
                if not w.walk_clear(x, y, x + dx * L, y + dy * L):
                    continue
                if not self.zone_safe_step(x + dx * L, y + dy * L):
                    continue
                s = math.cos(off) * 2
                p = [x + dx * L, y + dy * L, self.pos[2] + 7]
                hidden = sum(1 for e in enemies if not w.los([e["pos"][0], e["pos"][1], e["pos"][2] + 8], p))
                s += hidden * 1.5
                if s > bs:
                    best, bs = (dx, dy), s
        return (best or self.probe_dir(want)), self.me["energy"] > 20

    def dodge(self, obs):
        """Move away from mortar shells about to land near us."""
        x, y, z = self.pos
        sr = self.cfg["weapons"]["mortar"].get("splash_radius", 7) + 4.5
        g = self.cfg["weapons"]["mortar"].get("gravity", 22)
        for p in obs.get("projectiles", []):
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            for k in range(1, 50):
                tt = k * 0.1
                qz = pz + vz * tt - 0.5 * g * tt * tt
                qx, qy = px + vx * tt, py + vy * tt
                ground = self.world.height(qx, qy) if self.world else z
                if qz <= ground:
                    if math.hypot(qx - x, qy - y) < sr and tt < 2.5:
                        return self.probe_dir(unit(x - qx, y - qy))
                    break
        return None

    def unstick(self, move):
        t = self.t
        if t < self.unstick_until:
            return self.unstick_dir, True, True
        if (move[0] or move[1]) and len(self.hist) > 8 and t - self.hist[0][0] > 1.2:
            moved = math.hypot(self.pos[0] - self.hist[0][1], self.pos[1] - self.hist[0][2])
            charging = any(w and w.get("charge", 0) > 0 for w in self.me["weapons"])
            if moved < 1.5 and not charging:
                self.unstick_until = t + 0.8
                a = math.atan2(move[1], move[0]) + self.rng.uniform(-0.9, 0.9)
                self.unstick_dir = (math.cos(a), math.sin(a))
                self.hist.clear()
                self.path = []
                self.path_goal = None
                return self.unstick_dir, True, True
        return move, False, False

    def blend(self, a, b, wa):
        return unit(a[0] * wa + b[0] * (1 - wa), a[1] * wa + b[1] * (1 - wa))


class TimedBot(Bot):
    def act(self, obs):
        self._t0 = time.perf_counter()
        return super().act(obs)


if __name__ == "__main__":
    mf.run(TimedBot)
