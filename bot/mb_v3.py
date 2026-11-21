"""v3: experiments on top of mechbot 4.10 (mb_bot.py @120f1ff, unchanged). One flag per idea.

Flags (env MB_FLAGS="anchor,aggro3" or the DEFAULT_FLAGS set below for the submitted image):
  anchor  : with nothing to do (mechbot "wander"), and from zone phase 2 instead of far farming,
            hold a low-exposure, wall-hugging standing spot inside the coming circle, away from
            known mechs. Re-picked when the circle changes or the spot gets compromised.
  aggro3  : from the third circle on (current radius <= 240 m), fight any visible mech within
            130 m while HP > 60% (mechbot otherwise only fights when engaged / close / opportunity).
  dodge2  : laser dodge: when a mech's laser is charging at us, sidestep perpendicular to its aim
            and reverse at 0.5 s (house bots release at 0.8 s); mechbot alone only jumps + boosts.
  nofarm  : never shoot crates (noise, regen reset); drones still allowed.
  gunloose: gun fire gate 0.9 x target radius (mechbot 0.65) and 150 m max vs moving targets (125).
  laserpref: laser drives the torso whenever its cooldown < 1.2 s (mechbot 0.5 s).
  farmplus: farm crates within 170 m above 50% HP and drones within 190 m above 60% (mechbot 110/60%, 130/75%).
  cubeplus: detour for cubes within 150 m (mechbot 90).
  healmore: heal in cover up to 92% HP (mechbot 80%).
  aggro   : any phase: fight a visible mech within 100 m while HP > 75% and no other mech within 160 m.
"""
import math
import os

import mechfight as mf
from mb_bot import TimedBot, dist2, unit, wrap

PI = math.pi
TAU = 2 * PI
DEFAULT_FLAGS = set()
_env = os.environ.get("MB_FLAGS")
FLAGS = set(x.strip() for x in _env.split(",") if x.strip()) if _env is not None else set(DEFAULT_FLAGS)


class V3Bot(TimedBot):
    def __init__(self, hello):
        super().__init__(hello)
        self.flags = set(FLAGS)
        self.name = ("mbv3-" + "+".join(sorted(self.flags)))[:32] if self.flags else "mbv3"
        self._anchor = None
        self._charge_t = {}
        self._dodge_side = {}
        self._fake_attackers = []
        self.gun_gate = 0.9 if "gunloose" in self.flags else 0.65
        self.gun_move_max = 150.0 if "gunloose" in self.flags else 125.0
        self.laser_pref = "laserpref" in self.flags
        # goal-chooser thresholds (mechbot defaults)
        self.heal_hp, self.cube_r = 0.8, 90.0
        self.crate_hp, self.crate_r, self.npc_hp, self.npc_r = 0.6, 110.0, 0.75, 130.0
        if "farmplus" in self.flags:
            self.crate_hp, self.crate_r, self.npc_hp, self.npc_r = 0.5, 170.0, 0.6, 190.0
        if "cubeplus" in self.flags:
            self.cube_r = 150.0
        if "healmore" in self.flags:
            self.heal_hp = 0.92
        mf.log(f"policy {self.name}")

    # ------------------------------------------------------------------ helpers
    def zone_radius(self):
        return self.obs["zone"]["radius"]

    def exposure(self, x, y):
        """Fraction of 24 sight lines (12 bearings x 50/110 m, eye height) that see a mech at (x, y)."""
        w = self.world
        h = w.height(x, y)
        p = [x, y, h + 6.0]
        vis = tot = 0
        for k in range(12):
            a = k * TAU / 12
            ca, sa = math.cos(a), math.sin(a)
            for r in (50.0, 110.0):
                qx, qy = x + r * ca, y + r * sa
                if not w.in_bounds(qx, qy, 2):
                    continue
                tot += 1
                if w.los([qx, qy, w.height(qx, qy) + 8.0], p):
                    vis += 1
        return vis / tot if tot else 1.0

    # ------------------------------------------------------------------ anchor
    def anchor_goal(self):
        w = self.world
        if w is None:
            return None
        nc, inner = self.zone_inner
        key = (round(nc[0]), round(nc[1]), round(inner))
        a = self._anchor
        compromised = a is not None and a["best"] is not None and self.t - self.last_hit_t < 2 \
            and dist2(a["best"], self.pos) < 25
        if a is None or a["key"] != key or compromised:
            a = self._anchor = {"key": key, "best": None, "bs": 1e18, "n": 0}
        if a["n"] < 90:
            r_lim = max(inner * 0.75, 5.0)
            recent = [e for e in self.enemy_seen.values() if self.t - e["t"] < 20]
            for _ in range(3):
                a["n"] += 1
                if self.rng.random() < 0.5:
                    ang = self.rng.uniform(-PI, PI)
                    rr = min(r_lim, 120.0) * math.sqrt(self.rng.random())
                    px, py = self.pos[0] + rr * math.cos(ang), self.pos[1] + rr * math.sin(ang)
                else:
                    ang = self.rng.uniform(-PI, PI)
                    rr = r_lim * math.sqrt(self.rng.random())
                    px, py = nc[0] + rr * math.cos(ang), nc[1] + rr * math.sin(ang)
                if math.hypot(px - nc[0], py - nc[1]) > r_lim or not w.in_bounds(px, py, 8):
                    continue
                ci = w.idx(px, py)
                if not w.cost[ci] or w.prism[ci]:
                    continue
                sc = self.exposure(px, py) * 100
                sc += 0 if w.cost[ci] >= 2 else 10  # prefer hugging a wall
                sc += 0.06 * math.hypot(px - self.pos[0], py - self.pos[1])
                for e in recent:
                    de = math.hypot(e["pos"][0] - px, e["pos"][1] - py)
                    if de < 150:
                        sc += (150 - de) / 150 * 40
                if sc < a["bs"]:
                    a["bs"], a["best"] = sc, (px, py)
        if a["best"] is not None:
            self.note = f"anc{a['bs']:.0f}"
        return a["best"]

    def choose_goal(self, obs, npcs, zone_urgent, zone_goal, hpf, quiet=False):
        goal, kind, extra = self._mb_choose_goal(obs, npcs, zone_urgent, zone_goal, hpf, quiet)
        if "nofarm" in self.flags and kind == "crate":
            kind = "wander"
        if "anchor" in self.flags and not zone_urgent:
            late = self.zone_radius() <= 380.5
            if kind == "wander" or (late and kind in ("crate", "npc") and goal is not None
                                    and dist2(goal, self.pos) > 40):
                g = self.anchor_goal()
                if g is not None:
                    return g, "anchor", None
        return goal, kind, extra

    def _mb_choose_goal(self, obs, npcs, zone_urgent, zone_goal, hpf, quiet=False):
        """Returns (goal xy, mode, extra)."""
        x, y = self.pos[0], self.pos[1]
        self.note = ""
        if zone_urgent:
            return zone_goal, "zone", None
        healing = (hpf < self.heal_hp and self.t - self.last_hit_t < 25) or quiet
        cands = []
        # cubes: always worth a detour
        for c in obs.get("cubes", []):
            d = dist2(c["pos"], self.pos)
            if d < self.cube_r and self.zone_ok_now(c["pos"]) and abs(c["pos"][2] - self.pos[2]) < 25:
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
        if not healing and hpf > self.crate_hp:
            for cr in obs.get("crates", []):
                d = dist2(cr["pos"], self.pos)
                if d < self.crate_r and self.zone_ok_now(cr["pos"]):
                    tp = [cr["pos"][0], cr["pos"][1], cr["pos"][2] + 1.2]
                    cands.append((d * 0.9 + 10, (cr["pos"][0], cr["pos"][1]), "crate", (tp, [0, 0, 0], 1.4, "crate")))
        if not healing and hpf > self.npc_hp and self.t - self.last_hit_t > 3:
            for n in npcs:
                d = dist2(n["pos"], self.pos)
                if d < self.npc_r and n.get("hp", 350) > 0:
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


    # ------------------------------------------------------------------ dodge2
    def dodge(self, obs):
        d = super().dodge(obs)
        if d is not None or "dodge2" not in self.flags:
            return d
        t = self.t
        best = None
        for e in obs["mechs"]:
            if e["kind"] != "mech":
                continue
            if e.get("charging"):
                self._charge_t.setdefault(e["id"], t)
                if self.aimed_at_me(e, 0.12):
                    if best is None or self._charge_t[e["id"]] < self._charge_t[best["id"]]:
                        best = e
            else:
                self._charge_t.pop(e["id"], None)
                self._dodge_side.pop(e["id"], None)
        if best is None:
            return None
        el = t - self._charge_t[best["id"]]
        ux, uy = unit(self.pos[0] - best["pos"][0], self.pos[1] - best["pos"][1])
        side = self._dodge_side.setdefault(best["id"], self.rng.choice((-1, 1)))
        if el >= 0.5:
            side = -side
        self.note = "ldodge"
        return self.probe_dir((-uy * side, ux * side), 6.0)

    # ------------------------------------------------------------------ fire control (mechbot's, parameterised)
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
        elif laser and (laser.get("charge", 0) > 0 or laser["cooldown"] < (1.2 if self.laser_pref else 0.5)) \
                and d < laser["stats"].get("range", 280) * 0.95 and (los or laser.get("charge", 0) > 0):
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
            gun_max = min(gun_max, self.gun_move_max)
        if gun and gun_aim and los and gun.get("ammo", 1) > 0 and gun.get("reload", 0) == 0 and d < gun_max:
            ey, ep = err(*gun_aim)
            if ey < math.atan2(trad * self.gun_gate, max(d, 1.0)) + 0.004 and ep < math.atan2(3.0, max(d, 1.0)) + 0.004:
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


    # ------------------------------------------------------------------ aggro3
    def act(self, obs):
        fake = None
        me = obs["self"]
        late = "aggro3" in self.flags and obs["zone"]["radius"] <= 240.5 and me["hp"] > 0.6 * me["max_hp"]
        early = "aggro" in self.flags and me["hp"] > 0.75 * me["max_hp"]
        if late or early:
            rng_m = 130 if late else 100
            close = [m for m in obs["mechs"] if m["kind"] == "mech"
                     and math.hypot(m["pos"][0] - me["pos"][0], m["pos"][1] - me["pos"][1]) < 160]
            if late or len(close) <= 1:
                vis = [m for m in close
                       if math.hypot(m["pos"][0] - me["pos"][0], m["pos"][1] - me["pos"][1]) < rng_m]
                if vis:
                    tgt = min(vis, key=lambda m: math.hypot(m["pos"][0] - me["pos"][0], m["pos"][1] - me["pos"][1]))
                    if tgt["id"] not in self.attackers:
                        # mark it as engaging us so mechbot's own logic fights it; removed after this tick
                        fake = tgt["id"]
                        self.attackers[fake] = (obs["t"] - 7.9, None)
        try:
            return super().act(obs)
        finally:
            if fake is not None and self.attackers.get(fake, (None,))[1] is None \
                    and abs(self.attackers[fake][0] - (obs["t"] - 7.9)) < 1e-6:
                del self.attackers[fake]
