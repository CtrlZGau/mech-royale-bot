"""v3: experiments on top of mechbot (mb_bot.py @120f1ff, unchanged). One flag per idea.

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
  guntight: gun gate 0.5 x radius, 110 m vs moving targets.
  retreatearly / retreatlate: shift mechbot's retreat HP thresholds up / down.
  oppwide : opportunity fights out to 130 m, HP > 70%, vs targets up to 1.2x our HP.
  close50 / close90: always-fight distance 50 / 90 m (mechbot 70).
  MB_P env: "name=value,..." overrides any threshold attribute (tuning).
  focus   : finish kills: target score weights enemy HP 0.08/HP (mechbot 0.03), stickiness 6 s / -90 (3 s / -45).
  npcsafe : farm drones only above 92% HP and within 90 m (mechbot 75%, 130 m); drones killed mechbot in 12/96 sim matches.
  dualaim : while the laser charges (< 85%), aim with the gun's lead so the gun keeps firing; laser aim for release.
  laserclose: laser does not take over the torso inside 35 m (62% laser hit rate there in the sim).
  laserlong: laser drives the torso beyond 110 m even on cooldown (gun hit rate 75% at 120-160 m, 15% beyond).
  loot2   : weapon pickups from 200 m (mechbot 120) at weight 0.3 x distance while a slot is empty (0.5).
  cubemax : cubes first whenever not fighting: visible cubes from 250 m at weight 0.25 x d (mechbot 90 m,
            0.6), crates from 170 m above 50% HP at 0.6 x d (110 m, 60%, 0.9), drones from 170 m above 65% HP
            (130 m, 75%), mid-fight cube detour within 40 m (25 m).
  aimfix  : gun gate 0.5 x radius beyond 100 m (where hit rate drops to 82%); own-position prediction includes
            acceleration toward the commanded move (direction flips: 90% vs 93% hit rate).
  aggro   : any phase: fight a visible mech within 100 m while HP > 75% and no other mech within 160 m.
"""
import math
import os
import time

import mechfight as mf
from mb_bot import TimedBot, dist2, unit, wrap

PI = math.pi
TAU = 2 * PI
DEFAULT_FLAGS = {"cubemax"}
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
        # state machine thresholds (mechbot defaults)
        self.r_low, self.r_mid3, self.r_race, self.r_two = 0.3, 0.5, 0.4, 0.55
        self.opp_d, self.opp_hp, self.opp_ratio, self.close_d = 100.0, 0.8, 1.0, 70.0
        self.gun_gate = 0.5 if "guntight" in self.flags else self.gun_gate
        self.gun_move_max = 110.0 if "guntight" in self.flags else self.gun_move_max
        if "retreatearly" in self.flags:
            self.r_low, self.r_race, self.r_two = 0.4, 0.5, 0.65
        if "retreatlate" in self.flags:
            self.r_low, self.r_race, self.r_two = 0.22, 0.3, 0.45
        if "oppwide" in self.flags:
            self.opp_d, self.opp_hp, self.opp_ratio = 130.0, 0.7, 1.2
        if "close50" in self.flags:
            self.close_d = 50.0
        if "close90" in self.flags:
            self.close_d = 90.0
        self.hp_w, self.stick_s, self.stick_b = 0.03, 3.0, 45.0
        self.laser_min_d = 35.0 if "laserclose" in self.flags else 0.0
        self.pick_r, self.pick_w = 120.0, 0.5
        self.cube_w, self.crate_w, self.npc_w, self.cube_fight_r = 0.6, 0.9, 1.0, 25.0
        if "cubemax" in self.flags:
            # cubes first whenever we are not fighting (user strategy)
            self.cube_r, self.cube_w = 250.0, 0.25
            self.crate_r, self.crate_hp, self.crate_w = 170.0, 0.5, 0.6
            self.npc_r, self.npc_hp, self.npc_w = 170.0, 0.65, 0.8
            self.cube_fight_r = 40.0
        if "loot2" in self.flags:
            self.pick_r, self.pick_w = 200.0, 0.3
        if "focus" in self.flags:
            self.hp_w, self.stick_s, self.stick_b = 0.08, 6.0, 90.0
        if "npcsafe" in self.flags:
            self.npc_hp, self.npc_r = 0.92, 90.0
        # env overrides for tuning: MB_P="r_two=0.6,opp_d=120"
        for kv in (os.environ.get("MB_P") or "").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if hasattr(self, k.strip()):
                    setattr(self, k.strip(), float(v))
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
                cands.append((d * self.cube_w, (c["pos"][0], c["pos"][1]), "cube", None))
        # weapon pickups we want
        for p in obs.get("pickups", []):
            if self.t - self.failed_take.get(p["id"], -99) < 4:
                continue
            w = self.wanted_pickup(p)
            d = dist2(p["pos"], self.pos)
            if w and d < self.pick_r and self.zone_ok_now(p["pos"]) and abs(p["pos"][2] - self.pos[2]) < 25:
                cands.append((d * (self.pick_w if None in self.held() else 0.8), (p["pos"][0], p["pos"][1]), "pickup", None))
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
                    cands.append((d * self.crate_w + 10, (cr["pos"][0], cr["pos"][1]), "crate", (tp, [0, 0, 0], 1.4, "crate")))
        if not healing and hpf > self.npc_hp and self.t - self.last_hit_t > 3:
            for n in npcs:
                d = dist2(n["pos"], self.pos)
                if d < self.npc_r and n.get("hp", 350) > 0:
                    tp = [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75]
                    cands.append((d * self.npc_w + 25, (n["pos"][0], n["pos"][1]), "npc", (tp, n.get("vel", [0, 0, 0]), 1.8, "npc")))
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
            s += e.get("hp", 1400) * self.hp_w
            ct = getattr(self, "cur_target", None)
            if ct and ct[0] == e["id"] and self.t - ct[1] < self.stick_s:
                s -= self.stick_b
            if s < bs:
                best, bs = e, s
        return best


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

    def _mb_act(self, obs):
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
            retreat = hpf < self.r_low and (enemy_hp > me["hp"] * 1.2 or n_close >= 2)
            retreat = retreat or (hpf < self.r_mid3 and n_close >= 3)
            # losing the damage race: break off and heal while we still can
            retreat = retreat or (hpf < self.r_race and enemy_hp > me["hp"] * 1.6) or (hpf < self.r_two and n_close >= 2)
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
            opportunity = d < self.opp_d and hpf > self.opp_hp and enemy_hp <= me["hp"] * self.opp_ratio and n_close == 1
            fight = attacked_by or d < self.close_d or weak or threat or hunting_us or opportunity or (engaged_any and d < 120)
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
                if cube is not None and dist2(cube["pos"], self.pos) < self.cube_fight_r and abs(cube["pos"][2] - self.pos[2]) < 4:
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

    # ------------------------------------------------------------------ fire control (mechbot's, parameterised)
    def fire_control(self, tgt, act):
        tp, tv, trad, kind = tgt[0], tgt[1], tgt[2], tgt[3]
        me = self.me
        x, y, z = self.pos
        # the shot leaves during the next decision window: aim from where we'll be
        x += me["vel"][0] * 0.05
        y += me["vel"][1] * 0.05
        if "aimfix" in self.flags:
            # plus the velocity change toward our last commanded move (ground accel), mid-strafe-flip
            acc = me["stats"].get("ground_accel", 58.5) if me.get("grounded") else me["stats"].get("air_accel", 12)
            lm = self.last_move or (0.0, 0.0)
            sp = me["stats"].get("walk_speed", 11.7)
            dvx, dvy = lm[0] * sp - me["vel"][0], lm[1] * sp - me["vel"][1]
            dv = math.hypot(dvx, dvy)
            if dv > 1e-6:
                k = min(1.0, acc * 0.05 / dv)  # how much of the change happens in 0.05 s
                x += 0.5 * dvx * k * 0.05
                y += 0.5 * dvy * k * 0.05
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
        elif laser and (laser.get("charge", 0) > 0 or laser["cooldown"] < (1.2 if self.laser_pref else 0.5)
                        or ("laserlong" in self.flags and d > 110)) \
                and d < laser["stats"].get("range", 280) * 0.95 and (los or laser.get("charge", 0) > 0) \
                and not (d < self.laser_min_d and laser.get("charge", 0) == 0):
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
            if "dualaim" in self.flags and gun_aim is not None and laser is not None \
                    and laser.get("charge", 0) < laser["stats"].get("charge", 0.75) * 0.85 \
                    and gun is not None and gun.get("reload", 0) == 0 and gun.get("ammo", 1) > 0:
                aim = gun_aim  # keep the gun firing while the laser charges; switch to laser aim at the end
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
            gate = self.gun_gate if not ("aimfix" in self.flags and d > 100) else min(self.gun_gate, 0.5)
            if ey < math.atan2(trad * gate, max(d, 1.0)) + 0.004 and ep < math.atan2(3.0, max(d, 1.0)) + 0.004:
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
            elif laser["cooldown"] <= 0.05 and los and d < ls.get("range", 280) * 0.95 and ey < 0.6 \
                    and d >= self.laser_min_d:
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
            self._t0 = time.perf_counter()
            return self._mb_act(obs)
        finally:
            if fake is not None and self.attackers.get(fake, (None,))[1] is None \
                    and abs(self.attackers[fake][0] - (obs["t"] - 7.9)) < 1e-6:
                del self.attackers[fake]
