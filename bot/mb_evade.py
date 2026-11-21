"""Our stealth on top of mechbot 4.10 (mb_bot.py @120f1ff, unchanged): the V2 "evade" strategy.

Differences from mechbot's act():
  - Fight ONLY when engaged: a mech damaged us recently, a laser is charging at us, a mech is
    within CLOSE_M, or we were hit recently and it is within ENGAGED_NEAR_M. No "opportunity",
    "weak target", "hunting us" or "within 70 m" fights.
  - When not fighting and the zone is not urgent: go to the point inside the safe circle that
    maximises the distance to every known mech (seen in the last 15 s, extrapolated) and every
    recently heard enemy sound, preferring points hidden from the nearest threat and paths that
    do not pass close to a threat. Re-planned every 0.5 s; the goal only switches when clearly better.
  - No crate or drone farming (noise, and it is starting fights). Cubes / weapon pickups / supply
    drops only when no threat is within SAFE_LOOT_M.
Kept from mechbot: retreat-to-cover and healing, zone handling, path planner, dodges, stuck
recovery, fire control, weapon and upgrade choices.
"""
import math
import time

import mechfight as mf
from mb_bot import TimedBot, dist2, unit, wrap

PI = math.pi
SOUND_RANGE = {"near": 40.0, "mid": 110.0, "far": 220.0}
ENEMY_SOUNDS = ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "reload", "mortar")
MEMORY_S = 15.0
ATTACKED_S = 6.0       # a mech that hit us within this is engaging us
CLOSE_M = 35.0         # a mech this close is engaged regardless
ENGAGED_NEAR_M = 70.0  # after anyone hit us recently, fight mechs this close
SAFE_LOOT_M = 90.0
REPLAN_S = 0.5
SWITCH_GAIN = 15.0     # new goal must score this much better to replace the current one


class EvadeBot(TimedBot):
    name = "ctrlz-mb-evade"

    def __init__(self, hello):
        super().__init__(hello)
        self.sound_marks = []  # (t, x, y, weight)
        self.evade_goal_pt = None
        self.evade_score = -1e18
        self.evade_t = -99.0
        mf.log("policy ctrlz-mb-evade (mechbot 4.10 + V2 evade)")

    # ------------------------------------------------------------------ memory
    def update_memory(self, obs):
        super().update_memory(obs)
        t = self.t
        x, y = self.pos[0], self.pos[1]
        for s in obs.get("sounds", []):
            if s["kind"] in ENEMY_SOUNDS:
                r = SOUND_RANGE.get(s["dist"], 150.0)
                w = 0.6 if s["dist"] == "far" else 0.8
                self.sound_marks.append((t - s.get("age", 0), x + r * math.cos(s["bearing"]),
                                         y + r * math.sin(s["bearing"]), w))
        self.sound_marks = [m for m in self.sound_marks if t - m[0] < 4.0][-40:]

    def threats(self):
        """[((x, y), weight)] from remembered mechs (extrapolated <= 2 s) and recent sounds."""
        out = []
        t = self.t
        for e in self.enemy_seen.values():
            age = t - e["t"]
            if age > MEMORY_S:
                continue
            v = e.get("vel", [0, 0, 0])
            dt = min(age, 2.0)
            out.append(((e["pos"][0] + v[0] * dt, e["pos"][1] + v[1] * dt), 1.0 if age < 1 else 0.85))
        for ts, sx, sy, w in self.sound_marks:
            out.append(((sx, sy), w))
        return out

    def nearest_threat_d(self):
        th = self.threats()
        return min((math.hypot(q[0] - self.pos[0], q[1] - self.pos[1]) for q, _ in th), default=1e9)

    # ------------------------------------------------------------------ evade goal
    def evade_goal(self):
        """Point inside the safe circle maximising distance to all threats (cached, hysteresis)."""
        t = self.t
        if self.evade_goal_pt is not None and t - self.evade_t < REPLAN_S \
                and dist2(self.evade_goal_pt, self.pos) > 4:
            return self.evade_goal_pt
        nc, inner = self.zone_inner
        r = max(inner, 3.0)
        x, y = self.pos[0], self.pos[1]
        th = self.threats()
        w = self.world
        cands = []
        for rad in (20.0, 45.0, 80.0):
            for k in range(16):
                a = k * PI / 8
                cands.append((x + rad * math.cos(a), y + rad * math.sin(a)))
        for _ in range(16):
            rr = r * math.sqrt(self.rng.random())
            a = self.rng.uniform(-PI, PI)
            cands.append((nc[0] + rr * math.cos(a), nc[1] + rr * math.sin(a)))
        if self.evade_goal_pt is not None:
            cands.append(self.evade_goal_pt)
        inside_now = math.hypot(x - nc[0], y - nc[1]) <= r

        def base_score(p):
            dc = math.hypot(p[0] - nc[0], p[1] - nc[1])
            if dc > r and (inside_now or dc >= math.hypot(x - nc[0], y - nc[1])):
                return None
            if w is not None:
                if not w.in_bounds(p[0], p[1], 6):
                    return None
                ci = w.idx(p[0], p[1])
                if not w.cost[ci] or w.prism[ci]:
                    return None  # not a ground standing cell
            travel = math.hypot(p[0] - x, p[1] - y)
            if th:
                s = min(min(math.hypot(p[0] - q[0], p[1] - q[1]) / wt for q, wt in th), 350.0)
                for q, wt in th:
                    ca = _closest_approach((x, y), p, q)
                    if ca < 60:
                        s -= (60 - ca) * wt
            else:
                s = 200.0 - 100.0 * abs(dc / max(r, 1) - 0.55)
            s -= 0.12 * travel
            if dc > r:
                s -= 2.0 * (dc - r)
            return s

        scored = [(s, p) for p in cands for s in [base_score(p)] if s is not None]
        if not scored:
            self.evade_goal_pt, self.evade_t = (nc[0], nc[1]), t
            return self.evade_goal_pt
        scored.sort(reverse=True)
        nearest_q = min(th, key=lambda qw: math.hypot(qw[0][0] - x, qw[0][1] - y))[0] if th else None
        best, best_s = scored[0][1], -1e18
        for s, p in scored[:8]:
            if w is not None and nearest_q is not None:
                eye = [nearest_q[0], nearest_q[1], w.height(*nearest_q) + 8.0]
                if not w.los(eye, [p[0], p[1], w.height(p[0], p[1]) + 6.0]):
                    s += 35
            if s > best_s:
                best, best_s = p, s
        # Hysteresis: keep the current goal unless the new one is clearly better.
        if self.evade_goal_pt is not None and dist2(self.evade_goal_pt, self.pos) > 4:
            cur = base_score(self.evade_goal_pt)
            if cur is not None and best_s < cur + SWITCH_GAIN:
                best = self.evade_goal_pt
        self.evade_goal_pt, self.evade_t = best, t
        return best

    def quiet_goal(self, obs, npcs, zone_urgent, zone_goal, hpf):
        """mechbot's goal chooser minus farming; loot only when safe; else evade."""
        if zone_urgent:
            return zone_goal, "zone", None
        goal, kind, extra = self.choose_goal(obs, npcs, zone_urgent, zone_goal, hpf)
        if kind in ("zone", "drop_hold"):
            return goal, kind, extra
        if kind in ("cube", "pickup", "drop") and self.nearest_threat_d() > SAFE_LOOT_M:
            return goal, kind, extra
        if kind == "heal" and self.t - self.last_hit_t < 6:
            return goal, kind, extra  # mechbot's cover-heal right after a fight
        return self.evade_goal(), "evade", None

    # ------------------------------------------------------------------ main (mechbot act, modified)
    def act(self, obs):
        self._t0 = time.perf_counter()
        t0 = self._t0
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
        aim_target = None

        zone_urgent, zone_goal = self.zone_status(obs)

        if target is not None:
            tp = target["pos"]
            d = dist2(tp, self.pos)
            enemy_hp = target.get("hp", 1400)
            n_close = sum(1 for e in enemies if dist2(e["pos"], self.pos) < 160)
            retreat = hpf < 0.3 and (enemy_hp > me["hp"] * 1.2 or n_close >= 2)
            retreat = retreat or (hpf < 0.5 and n_close >= 3)
            retreat = retreat or (hpf < 0.4 and enemy_hp > me["hp"] * 1.6) or (hpf < 0.55 and n_close >= 2)
            aim_target = self.mech_aim_target(target)
            a = self.attackers.get(target["id"])
            attacked_by = a is not None and t - a[0] < ATTACKED_S
            engaged_any = any(t - a2[0] < ATTACKED_S for a2 in self.attackers.values())
            threat = bool(target.get("charging")) and self.aimed_at_me(target, 0.15)
            # Ours: fight only when someone is engaging us.
            fight = attacked_by or threat or d < CLOSE_M or (engaged_any and d < ENGAGED_NEAR_M)
            self.cur_target = (target["id"], t)
            if retreat:
                self.mode = "retreat"
                move, boost = self.retreat_move(enemies, zone_goal)
            elif fight:
                self.mode = "fight"
                move, boost = self.fight_move(target, d, zone_goal)
            else:
                self.mode = "evade"
                aim_target = None
                self.face = target  # keep front armour (and vision) on it
                goal, kind, extra = self.quiet_goal(obs, npcs, zone_urgent, zone_goal, hpf)
                move = self.navigate(goal) if goal else (0.0, 0.0)
                boost = me["energy"] > 40 and d < 120
                if kind == "drop_hold" and dist2(goal, self.pos) < extra * 0.5:
                    move = (0.0, 0.0)
            if zone_urgent:
                zm = self.navigate(zone_goal)
                move = self.blend(zm, move, 0.65)
                boost = boost or me["energy"] > 30
        else:
            goal, kind, extra = self.quiet_goal(obs, npcs, zone_urgent, zone_goal, hpf)
            self.mode = kind
            if kind == "drop_hold":
                move = (0.0, 0.0) if dist2(goal, self.pos) < extra * 0.5 else self.navigate(goal)
            else:
                move = self.navigate(goal) if goal else (0.0, 0.0)
            boost = zone_urgent or (not me["in_zone"]) or (kind in ("drop", "cube", "pickup") and me["energy"] > 60)

        dodge = self.dodge(obs)
        if dodge is not None:
            move = dodge
            boost = me["energy"] > 15
        for e in enemies:
            if e.get("charging") and self.aimed_at_me(e, 0.12):
                jump = me["grounded"] and me["energy"] > 25
                boost = True
        move, sj, sb = self.unstick(move)
        jump = jump or sj
        boost = boost or sb

        fire = [False, False]
        if aim_target is not None:
            aim, fire = self.fire_control(aim_target, act)
        else:
            aim = self.scan_aim(move)
            # never leave a laser stuck mid-charge
            for i, w in enumerate(me["weapons"]):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = w["charge"] < w["stats"].get("charge", 0.75) + 1.0
        self.reload_logic(act, aim_target is None)

        mx, my = move
        act.update(move=[round(mx, 3), round(my, 3)], aim=[round(aim[0], 4), round(aim[1], 4)],
                   fire=fire, boost=bool(boost and (me["energy"] > 8)), jump=bool(jump))
        self.last_move = move
        if self.evade_goal_pt is not None and self.mode == "evade":
            mf.draw.point([self.evade_goal_pt[0], self.evade_goal_pt[1], self.pos[2] + 1], "#40ff80", 6)
        el = (time.perf_counter() - t0) * 1000
        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} c{me['cubes']:.0f} near{self.nearest_threat_d():.0f} {el:.0f}ms")
        return act



def _closest_approach(a, b, q):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L = dx * dx + dy * dy
    s = 0.0 if L < 1e-9 else max(0.0, min(1.0, ((q[0] - ax) * dx + (q[1] - ay) * dy) / L))
    return math.hypot(ax + s * dx - q[0], ay + s * dy - q[1])
