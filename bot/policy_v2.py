"""Policy v2 "evade": stay inside the zone, keep maximum distance from every other mech, and only
fight whoever is fighting us.

Each decision:
  1. Threat picture: visible mechs, mechs seen in the last 15 s (extrapolated a little), and enemy
     sounds placed at their estimated range (near 40 m / mid 110 m / far 220 m).
  2. Safe area: the zone circle we must be inside soon (next circle when its shrink is close or
     running, else the current one), shrunk by a margin.
  3. Goal: among ~60 candidate points inside the safe area, the one that maximises the distance to
     the nearest threat, preferring points hidden from threats by buildings, reachable in a straight
     line, and whose path does not pass close to a threat. Re-planned every 0.5 s.
  4. Fight only when engaged: a mech damaged us in the last 4 s, a visible laser is charging at us,
     a mech is within 30 m, or a drone is shooting us. Then shoot it (v0 aim/fire) while still
     moving away. Otherwise never fire (shots are heard up to 320 m).
  5. Laser dodge, unstick, zone emergency, upgrades and weapon pickups (only when safe) from v0.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf
from nav import Nav
from policy_v0 import V0Policy
from util import angdiff, clamp, dist2d, unit, wrap

SOUND_RANGE = {"near": 40.0, "mid": 110.0, "far": 220.0}
ENEMY_SOUNDS = ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "reload", "mortar")
MEMORY_S = 15.0          # how long we keep avoiding a mech we saw
ENGAGED_S = 4.0          # a mech that hit us is "engaging" for this long
CLOSE_FORCE_M = 30.0     # a mech this close is engaged regardless
REPLAN_S = 0.5


class V2Policy(V0Policy):
    def __init__(self, hello: mf.Hello) -> None:
        super().__init__(hello)
        self.name = "ctrlz-v2-evade"
        self.mem: Dict[int, Tuple[float, List[float], List[float]]] = {}  # id -> (t, pos, vel)
        self.sounds: List[Tuple[float, float, float, float]] = []  # (t, x, y, weight)
        self.attackers: Dict[int, float] = {}
        self.npc_hit_t = -1e9
        self.goal: Optional[Tuple[float, float]] = None
        self.goal_t = -1e9
        self.nav = Nav(self.world)
        self.path: List[Tuple[float, float]] = []
        self.path_goal: Optional[Tuple[float, float]] = None
        self.path_t = -1e9
        mf.log(f"policy {self.name} slot={self.me_id} nav_ok={self.nav.ok} "
                f"build={getattr(self.nav, 'build_s', 0):.2f}s")

    # ================================================================== main
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        t = obs["t"]
        pos = me["pos"]
        act: Dict[str, Any] = {}
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])
        self.read_events(obs, t)
        self.update_threats(obs, t)

        threats = self.threat_points(t)
        safe = self.safe_circle(obs)
        zone_vec, zone_urgent = self.zone_pressure(obs)
        target = self.engaged_target(obs, t)

        # ---- goal: maximise distance from every threat inside the safe circle
        if self.goal is None or t - self.goal_t >= REPLAN_S or dist2d(self.goal, pos) < 4:
            self.goal = self.plan_goal(pos, threats, safe, obs)
            self.goal_t = t
        label = "evade"
        dest = self.goal
        if zone_urgent and not me["in_zone"]:
            z = obs["zone"]
            nc, nr = z["next_center"], z["next_radius"]
            d = dist2d(pos, nc)
            k = max(d - nr * 0.6, 0.0) / max(d, 1e-6)
            dest, label = (pos[0] + (nc[0] - pos[0]) * k, pos[1] + (nc[1] - pos[1]) * k), "zone!"
        move = self.steer(pos, dest, t)

        # ---- laser dodge (v0 feature code), forced on
        self.f["dodge"] = True
        move, dodging = self.feature_moves(obs, move, zone_urgent, t)
        if dodging:
            label = "dodge"
        if not dodging and self.stuck_ticks >= 4:
            self.path_t = -1e9  # replan next tick
        move = self.unstick(obs, move, t)

        # ---- weapons pickups only when nobody is near
        nearest = min((math.hypot(p[0] - pos[0], p[1] - pos[1]) for p, _w in threats), default=1e9)
        if nearest > 90:
            take = self.try_take(obs)
            if take is not None:
                act["take"] = take

        # ---- aim and fire
        if target is not None:
            aim, fire = self.aim_and_fire(obs, target, move, t)
            label = f"engaged {target['id']}"
        else:
            fire = [False, False]
            # Keep the laser from being stuck mid-charge.
            for i, w in enumerate(me["weapons"]):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = w["charge"] < w["stats"]["charge"] + 1.0
            aim = self.watch_aim(obs, threats, move, t)

        act["aim"] = aim
        act["fire"] = fire
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        boost = (zone_urgent and not me["in_zone"]) or (dodging and me["energy"] > 10) \
            or (nearest < 120 and me["energy"] > 40 and math.hypot(*move) > 0.5)
        act["boost"] = bool(boost) if me["grounded"] else (me["energy"] > 20 and t < self.unstick_until)
        gun = me["weapons"][0]
        if gun and gun["kind"] == "gun" and target is None and gun.get("reload", 0) == 0 \
                and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.5:
            act["reload"] = True

        mf.draw.point([self.goal[0], self.goal[1], pos[2] + 1], "#40ff80", 6)
        mf.draw.label(f"{label} near{nearest:.0f} hp{me['hp']:.0f}")
        return act

    # ================================================================== threat picture
    def update_threats(self, obs, t):
        me = obs["self"]
        pos = me["pos"]
        for m in obs["mechs"]:
            if m["kind"] == "mech" and m["id"] != me["id"]:
                self.mem[m["id"]] = (t, list(m["pos"]), list(m["vel"]))
        for i in [i for i, (ts, _, _) in self.mem.items() if t - ts > MEMORY_S]:
            del self.mem[i]
        for ev in obs.get("events", []):
            if ev["type"] == "damaged":
                if ev.get("by") is not None and ev.get("weapon") not in ("zone", "npc"):
                    self.attackers[ev["by"]] = t
                if ev.get("weapon") == "npc":
                    self.npc_hit_t = t
        for snd in obs.get("sounds", []):
            if snd["kind"] in ENEMY_SOUNDS:
                r = SOUND_RANGE.get(snd["dist"], 150.0)
                b = snd["bearing"]
                w = 0.6 if snd["dist"] == "far" else 0.8
                self.sounds.append((t - snd.get("age", 0), pos[0] + r * math.cos(b), pos[1] + r * math.sin(b), w))
        self.sounds = [s for s in self.sounds if t - s[0] < 4.0][-40:]

    def threat_points(self, t):
        """[(xy, weight)]: remembered mechs (extrapolated up to 2 s) and recent sounds."""
        out = []
        for ts, p, v in self.mem.values():
            dt = min(t - ts, 2.0)
            out.append(((p[0] + v[0] * dt, p[1] + v[1] * dt), 1.0 if t - ts < 1 else 0.85))
        for ts, x, y, w in self.sounds:
            out.append(((x, y), w))
        return out

    def engaged_target(self, obs, t):
        me = obs["self"]
        pos = me["pos"]
        vis = [m for m in obs["mechs"] if m["kind"] == "mech"]
        cands = []
        for m in vis:
            d = dist2d(m["pos"], pos)
            hit_us = t - self.attackers.get(m["id"], -1e9) < ENGAGED_S
            charging_at_us = False
            if m.get("charging"):
                bearing = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
                charging_at_us = abs(angdiff(m["aim"][0], bearing)) < math.atan2(4.0, max(d, 1)) + 0.04
            if hit_us or charging_at_us or d < CLOSE_FORCE_M:
                cands.append((0 if hit_us else 1, d, m))
        if cands:
            return min(cands, key=lambda c: (c[0], c[1]))[2]
        if t - self.npc_hit_t < 3.0:
            npcs = [m for m in obs["mechs"] if m["kind"] == "npc" and dist2d(m["pos"], pos) < 100]
            if npcs:
                return min(npcs, key=lambda m: dist2d(m["pos"], pos))
        return None

    # ================================================================== zone / goal
    def safe_circle(self, obs):
        """(cx, cy, r) we should stay inside, with margin."""
        z = obs["zone"]
        t = obs["t"]
        soon = t >= z["shrink_start"] - 25
        if soon:
            cx, cy, r = z["next_center"][0], z["next_center"][1], z["next_radius"]
        else:
            cx, cy, r = z["center"][0], z["center"][1], z["radius"]
        margin = min(25.0, 0.2 * r)
        return cx, cy, max(r - margin, 3.0)

    def plan_goal(self, pos, threats, safe, obs):
        cx, cy, r = safe
        rng = self.rng
        cands = []
        for rad in (20.0, 45.0, 80.0):
            for k in range(16):
                a = k * math.pi / 8
                cands.append((pos[0] + rad * math.cos(a), pos[1] + rad * math.sin(a)))
        for _ in range(16):
            rr = r * math.sqrt(rng.random())
            a = rng.uniform(-math.pi, math.pi)
            cands.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        if self.goal is not None:
            cands.append(self.goal)
        inside_now = math.hypot(pos[0] - cx, pos[1] - cy) <= r

        scored = []
        for p in cands:
            dc = math.hypot(p[0] - cx, p[1] - cy)
            if dc > r:
                if inside_now:
                    continue
                # Outside the safe area: only points that get us closer to it count.
                if dc >= math.hypot(pos[0] - cx, pos[1] - cy):
                    continue
            travel = math.hypot(p[0] - pos[0], p[1] - pos[1])
            if threats:
                dmin = min(math.hypot(p[0] - q[0], p[1] - q[1]) / w for q, w in threats)
                s = min(dmin, 350.0)
                # Paths that pass close to a threat are bad.
                for q, w in threats:
                    ca = _closest_approach(pos, p, q)
                    if ca < 60:
                        s -= (60 - ca) * w
            else:
                s = 200.0 - 100.0 * abs(dc / max(r, 1) - 0.55)  # no info: mid-ring of the safe area
            s -= 0.12 * travel
            if dc > r:
                s -= 2.0 * (dc - r)
            scored.append((s, p))
        if not scored:
            return (cx, cy)
        scored.sort(reverse=True)
        # Expensive checks only on the best few: straight walkable path, hidden from the nearest threat.
        best, best_s = scored[0][1], -1e18
        nearest_q = min(threats, key=lambda qw: math.hypot(qw[0][0] - pos[0], qw[0][1] - pos[1]))[0] if threats else None
        for s, p in scored[:8]:
            g = self.world.ground(p[0], p[1])
            if not self.nav.free(p[0], p[1]):
                s -= 200
            elif not self.nav.line_free(pos, p):
                s -= 15  # reachable by path, just not straight
            if nearest_q is not None:
                qz = self.world.ground(nearest_q[0], nearest_q[1]) + 8.0
                if self.world.blocked((nearest_q[0], nearest_q[1], qz), (p[0], p[1], g + 4.5)):
                    s += 35
            if s > best_s:
                best, best_s = p, s
        return best

    # ================================================================== path following
    def steer(self, pos, dest, t):
        if dest is None or dist2d(dest, pos) < 2.0:
            return (0.0, 0.0)
        if (t - self.path_t > 0.6 and (self.path_goal is None or dist2d(self.path_goal, dest) > 8
                                        or t - self.path_t > 1.5 or not self.path)):
            self.path = self.nav.path(pos, dest)
            self.path_goal, self.path_t = dest, t
        while self.path and dist2d(self.path[0], pos) < 3.0:
            self.path.pop(0)
        if not self.path:
            return unit(dest[0] - pos[0], dest[1] - pos[1])
        wp = self.path[0]
        for cand in self.path[1:4]:
            if self.nav.line_free(pos, cand):
                wp = cand
        mf.draw.line([pos[0], pos[1], pos[2] + 1], [wp[0], wp[1], pos[2] + 1], "#40ff80")
        return unit(wp[0] - pos[0], wp[1] - pos[1])

    # ================================================================== aim when not fighting
    def watch_aim(self, obs, threats, move, t):
        """Keep the nearest threat in view (vision follows the torso); else look where we go."""
        pos = obs["self"]["pos"]
        if self.threat_bearing is not None and t < self.threat_until:
            return [self.threat_bearing, 0.0]
        if threats:
            q = min(threats, key=lambda qw: math.hypot(qw[0][0] - pos[0], qw[0][1] - pos[1]))[0]
            if math.hypot(q[0] - pos[0], q[1] - pos[1]) < 220:
                return [math.atan2(q[1] - pos[1], q[0] - pos[0]), 0.0]
        if math.hypot(*move) > 0.1:
            base = math.atan2(move[1], move[0])
            return [wrap(base + 0.9 * math.sin(t * 1.1)), 0.0]
        return [wrap(obs["self"]["aim"][0] + 0.35), 0.0]


def _closest_approach(a, b, q):
    """Distance from point q to segment a-b (2D)."""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    s = clamp(((q[0] - ax) * dx + (q[1] - ay) * dy) / L, 0.0, 1.0) if L > 1e-9 else 0.0
    return math.hypot(ax + s * dx - q[0], ay + s * dy - q[1])
