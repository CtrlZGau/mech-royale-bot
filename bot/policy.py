"""Policy v1 ("batch A"): survive first.

Changes vs v0 (see AGENTS.md, logs/analysis-b1.md):
  Fix 1  disengage on damage RATE, not an HP threshold; strict engage rules; sticky heal mode.
  Fix 2  laser dodge: track when each enemy starts charging (house bots release at ~0.8 s),
         step into cover within 8 m, else boost sideways and reverse at ~0.5 s; counter-shoot them.
  Fix 3  never stand still while anyone may see us; idle drift is slow (<5 m/s, silent).
  Laser  our own laser releases reliably (no endless holding), and doesn't charge while dodging.
  Zone   urgency is sticky for 3 s (no fight/zone flapping).

Priorities each decision:
  zone emergency > laser dodge > retreat/heal > engage > loot > drift.
"""
from __future__ import annotations

import math
import random
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf
from util import angdiff, ballistic_pitch, clamp, dist2d, lead_time, unit, wrap
from world import World

WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}

UPGRADE_PREF = [
    "armor", "plating", "nanites", "gun_rate", "laser_damage", "laser_charge", "gun_damage",
    "servos", "lightning_efficiency", "gun_mag", "capacitor", "reactor", "jets", "gyros",
    "sensors", "gun_velocity", "lightning_range", "lightning_chain", "mortar_reload",
    "mortar_splash", "mortar_velocity", "laser_pierce",
]

# --- tunables (batch A) ---
RETREAT_DMG_1_5S = 200.0      # HP lost in the last 1.5 s that forces a retreat
RETREAT_HP_FRAC = 0.55
ENGAGE_HP_FRAC = 0.70
HEAL_EXIT_HP_FRAC = 0.90
MEMORY_S = 5.0                # "known" enemy window
LASER_REVERSE_S = 0.5         # reverse the dodge direction this long after charge start
LASER_JUMP_S = None           # e.g. 0.7 to add a jump; off until tested
SILENT_FRAC = 0.40            # move length for silent idle drift (0.4 * 11.7 = 4.7 m/s < 5)


class BasePolicy:
    name = "ctrlz-v1-survive"

    def __init__(self, hello: mf.Hello) -> None:
        self.cfg = hello["config"]
        self.me_id = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.world = World(hello["map"]["dir"])
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.stuck_ticks = 0
        self.unstick_until = 0.0
        self.unstick_dir = (0.0, 0.0)
        self.threat_bearing: Optional[float] = None
        self.threat_until = 0.0
        self.hp_hist: deque = deque()
        self.seen: Dict[int, Tuple[float, Dict[str, Any]]] = {}
        self.charge_start: Dict[int, float] = {}
        self.dodge_side: Dict[int, int] = {}
        self.heal = False
        self.last_damaged_t = -1e9
        self.last_enemy_t = -1e9
        self.zone_urgent_until = 0.0
        self.cover: Optional[Tuple[float, float, float]] = None
        self.cover_t = -1e9
        self.cover_key = None
        self.wander: Optional[Tuple[float, float]] = None
        self.last_target_pos: Optional[List[float]] = None
        self.block_charge = False
        mf.log(f"policy {self.name} slot={self.me_id} map_ok={self.world.ok} prisms={len(self.world.boxes)}")

    # ================================================================== main
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        t = obs["t"]
        pos = me["pos"]
        act: Dict[str, Any] = {}
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        self.update_memory(obs, t)
        known = self.known_enemies(t)
        dmg_rate = self.damage_last(1.5, me["hp"])
        hp_frac = me["hp"] / me["max_hp"]

        zone_vec, zone_urgent, late_shrink = self.zone_pressure(obs)
        laser_threat = self.laser_threat(obs, t)

        # ---- fix 1: retreat / heal / engage decisions
        threat = self.main_threat(obs, known)
        cover_near = None
        if laser_threat is not None:
            cover_near = self.cover_from(pos, laser_threat, 8.0, obs, t)
        retreat = (dmg_rate > RETREAT_DMG_1_5S or hp_frac < RETREAT_HP_FRAC or len(known) >= 2
                   or (laser_threat is not None and cover_near is None))
        if retreat:
            self.heal = True
        if self.heal and hp_frac >= HEAL_EXIT_HP_FRAC and not known and t - self.last_damaged_t > 5:
            self.heal = False

        target = self.choose_target(obs, t)
        engage = (not self.heal and hp_frac > ENGAGE_HP_FRAC and len(known) == 1 and not late_shrink
                  and target is not None and target["kind"] == "mech"
                  and target.get("hp", 0) <= me["hp"] * 1.15)
        if laser_threat is not None and laser_threat.get("_visible"):
            target = laser_threat  # counter-shoot the charger (half speed while charging)
        if target is not None and target["kind"] == "npc" and (self.heal or known):
            target = None

        # ---- movement
        move: Tuple[float, float] = (0.0, 0.0)
        boost = False
        jump = False
        label = "drift"
        allow_still = False
        self.block_charge = False
        if zone_urgent and (not me["in_zone"] or late_shrink):
            move, label, boost = zone_vec, "zone!", True
        elif laser_threat is not None:
            move, jump = self.dodge_move(obs, laser_threat, cover_near, t)
            boost, label, self.block_charge = me["energy"] > 10, f"dodge {laser_threat['id']}", True
        elif self.heal:
            move, allow_still, label = self.retreat_move(obs, threat, zone_vec, t)
            boost = threat is not None and me["energy"] > 40 and math.hypot(*move) > 0.5
        elif target is not None and target["kind"] == "mech" and engage:
            move, label = self.combat_move(obs, target, zone_vec, t), f"fight {target['id']}"
            boost = me["energy"] > 0.5 * me["max_energy"]
        elif target is not None and target["kind"] == "mech":
            # Seen but not worth engaging: keep shooting, back off toward cover.
            move, allow_still, label = self.retreat_move(obs, target, zone_vec, t)
            label = f"kite {target['id']}"
            boost = me["energy"] > 50
        elif zone_urgent:
            move, label, boost = zone_vec, "zone", me["energy"] > 40
        else:
            loot = self.loot_goal(obs)
            if loot is not None:
                move, label = unit(loot[0] - pos[0], loot[1] - pos[1]), "loot"
            elif target is not None:  # npc, healthy, nobody around
                move, label = self.combat_move(obs, target, zone_vec, t), f"npc {target['id']}"
            else:
                move, label = self.drift_move(obs, zone_vec, known, t)

        # ---- fix 3: never stand still while we may be seen
        exposed = bool(known) or t - self.last_damaged_t < 5
        if exposed and not allow_still and math.hypot(*move) < 0.5:
            move = self.strafe_vec(obs, threat, t)

        take = self.try_take(obs)
        if take is not None:
            act["take"] = take

        if not allow_still:
            move = self.unstick(obs, move, t)
        aim, fire = self.aim_and_fire(obs, target, move, t)
        if self.heal and target is None:
            fire = [False, False]  # don't reset regen on nothing
        act["aim"] = aim
        act["fire"] = fire
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost) if me["grounded"] else (me["energy"] > 20 and t < self.unstick_until)
        if jump and me["grounded"]:
            act["jump"] = True
        if t < self.unstick_until and me["grounded"] and self.stuck_jump:
            act["jump"] = True
            self.stuck_jump = False

        gun = me["weapons"][0]
        if gun and gun["kind"] == "gun" and target is None and gun.get("reload", 0) == 0 \
                and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.5:
            act["reload"] = True

        mf.draw.label(f"{label} hp{me['hp']:.0f} e{me['energy']:.0f}{' H' if self.heal else ''}")
        return act

    stuck_jump = False

    # ================================================================== memory
    def update_memory(self, obs, t):
        me = obs["self"]
        self.hp_hist.append((t, me["hp"]))
        while self.hp_hist and t - self.hp_hist[0][0] > 6:
            self.hp_hist.popleft()
        for m in obs["mechs"]:
            if m["kind"] != "mech" or m["id"] == me["id"]:
                continue
            self.seen[m["id"]] = (t, m)
            self.last_enemy_t = t
            if m.get("charging"):
                self.charge_start.setdefault(m["id"], t)
            else:
                self.charge_start.pop(m["id"], None)
                self.dodge_side.pop(m["id"], None)
        for i in [i for i, (ts, _) in self.seen.items() if t - ts > MEMORY_S]:
            del self.seen[i]
            self.charge_start.pop(i, None)
        for ev in obs.get("events", []):
            if ev["type"] == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_damaged_t = t
                if ev.get("bearing") is not None:
                    self.threat_bearing = ev["bearing"]
                    self.threat_until = t + 2.5
            elif ev["type"] in ("kill", "took", "upgrade"):
                mf.log(f"t={t:.1f} event {ev}")

    def known_enemies(self, t):
        return [m for (ts, m) in self.seen.values() if t - ts <= MEMORY_S]

    def damage_last(self, window, hp_now):
        t_now = self.hp_hist[-1][0] if self.hp_hist else 0
        peak = hp_now
        for ts, hp in self.hp_hist:
            if t_now - ts <= window:
                peak = max(peak, hp)
        return peak - hp_now

    def main_threat(self, obs, known):
        pos = obs["self"]["pos"]
        vis = [m for m in obs["mechs"] if m["kind"] == "mech"]
        pool = vis or known
        return min(pool, key=lambda m: dist2d(m["pos"], pos)) if pool else None

    # ================================================================== upgrades
    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for want in UPGRADE_PREF:
            if want in ids:
                return ids.index(want)
        return 0

    # ================================================================== targeting
    def choose_target(self, obs, t):
        me = obs["self"]
        best, best_score = None, 1e18
        for m in obs["mechs"]:
            if m["kind"] != "mech" or m["id"] == me["id"]:
                continue
            d = dist2d(m["pos"], me["pos"])
            if d > 230:
                continue
            hp_frac = m.get("hp", 1) / max(m.get("max_hp", 1), 1)
            score = d + 120 * hp_frac
            if score < best_score:
                best, best_score = m, score
        # Fix 6 (cheap part): drones only at full HP with no enemy seen for 10 s.
        if best is None and me["hp"] >= 0.95 * me["max_hp"] and t - self.last_enemy_t > 10:
            for m in obs["mechs"]:
                if m["kind"] == "npc":
                    d = dist2d(m["pos"], me["pos"])
                    if d < 70 and d < best_score:
                        best, best_score = m, d
        return best

    # ================================================================== fix 2: lasers
    def laser_threat(self, obs, t):
        """The enemy whose charging laser is aimed at us (earliest charge first), or None."""
        me = obs["self"]
        pos = me["pos"]
        best = None
        vis_ids = {m["id"] for m in obs["mechs"]}
        for i, t0 in self.charge_start.items():
            if i not in self.seen:
                continue
            m = self.seen[i][1]
            if t - t0 > 1.6:
                continue
            d = max(dist2d(m["pos"], pos), 1.0)
            bearing = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            if abs(angdiff(m["aim"][0], bearing)) < math.atan2(4.0, d) + 0.04 and d < 300:
                if best is None or t0 < self.charge_start[best["id"]]:
                    best = dict(m)
                    best["_visible"] = i in vis_ids
                    best["_elapsed"] = t - t0
        return best

    def dodge_move(self, obs, enemy, cover, t):
        me = obs["self"]
        pos = me["pos"]
        if cover is not None:
            return unit(cover[0] - pos[0], cover[1] - pos[1]), False
        to_me = unit(pos[0] - enemy["pos"][0], pos[1] - enemy["pos"][1])
        i = enemy["id"]
        if i not in self.dodge_side:
            # Pick the side with room to move (no wall within 8 m), else random.
            sides = []
            for s in (1, -1):
                px, py = -to_me[1] * s, to_me[0] * s
                free = not self.world.blocked((pos[0], pos[1], pos[2] + 2), (pos[0] + px * 8, pos[1] + py * 8, pos[2] + 2))
                sides.append((free, self.rng.random(), s))
            self.dodge_side[i] = max(sides)[2]
        s = self.dodge_side[i]
        if enemy["_elapsed"] >= LASER_REVERSE_S:
            s = -s
        jump = LASER_JUMP_S is not None and abs(enemy["_elapsed"] - LASER_JUMP_S) < 0.06
        return (-to_me[1] * s, to_me[0] * s), jump

    # ================================================================== cover
    def cover_from(self, pos, threat, max_r, obs, t):
        key = (threat["id"], round(pos[0] / 3), round(pos[1] / 3), max_r)
        if key == self.cover_key and t - self.cover_t < 0.5:
            return self.cover
        tp = threat["pos"]
        eye = (tp[0], tp[1], tp[2] + self.cfg["mech"].get("eye_height", 8))
        z = obs["zone"]
        self.cover = self.world.find_cover(pos, eye, max_r, zone=(z["center"][0], z["center"][1], z["radius"]))
        self.cover_key, self.cover_t = key, t
        return self.cover

    def retreat_move(self, obs, threat, zone_vec, t):
        """Back off toward a point that breaks line of sight. Returns (move, allow_still, label)."""
        me = obs["self"]
        pos = me["pos"]
        if threat is None:
            # Nobody known: heal in place if quiet, but keep inside the zone.
            if math.hypot(*zone_vec) > 0:
                return zone_vec, False, "heal-move"
            return (0.0, 0.0), True, "heal"
        cover = self.cover_from(pos, threat, 34.0, obs, t)
        visible_now = any(m["id"] == threat["id"] for m in obs["mechs"])
        if cover is not None:
            d = dist2d(cover, pos)
            if d < 2.0:
                return (0.0, 0.0), True, "cover"
            return unit(cover[0] - pos[0], cover[1] - pos[1]), False, "to-cover"
        away = unit(pos[0] - threat["pos"][0], pos[1] - threat["pos"][1])
        if t > self.strafe_until:
            self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
            self.strafe_until = t + self.rng.uniform(0.6, 1.4)
        perp = (-away[1] * self.strafe_dir, away[0] * self.strafe_dir)
        mx = away[0] + 0.6 * perp[0] + 0.8 * zone_vec[0]
        my = away[1] + 0.6 * perp[1] + 0.8 * zone_vec[1]
        return unit(mx, my), False, "back-off" if visible_now else "back-off*"

    # ================================================================== zone
    def zone_pressure(self, obs):
        """(direction toward safety, urgent?, in the last 20 s of a shrink?)"""
        me, z = obs["self"], obs["zone"]
        pos = me["pos"]
        t = obs["t"]
        shrinking = z["shrink_end"] > z["shrink_start"] and t >= z["shrink_start"]
        f = clamp((t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]), 0, 1) if shrinking else 0.0
        cx = z["center"][0] + (z["next_center"][0] - z["center"][0]) * f
        cy = z["center"][1] + (z["next_center"][1] - z["center"][1]) * f
        r_now = z["radius"] + (z["next_radius"] - z["radius"]) * f
        nx, ny, nr = z["next_center"][0], z["next_center"][1], z["next_radius"]
        to_next = unit(nx - pos[0], ny - pos[1])
        d_next = dist2d(pos, (nx, ny))
        d_now = dist2d(pos, (cx, cy))
        time_left = max(z["shrink_end"] - t, 0.0)
        speed = me["stats"].get("walk_speed", 11.7)
        need = max(d_next - nr * 0.8, 0.0)
        urgent = (not me["in_zone"]) or d_now > r_now - 15 or (need > 0 and need > speed * time_left * 0.6)
        if urgent:
            self.zone_urgent_until = t + 3.0
        urgent = urgent or t < self.zone_urgent_until
        late = shrinking and time_left < 20 and need > 0
        return (to_next if need > 0 or urgent else (0.0, 0.0)), urgent, late

    # ================================================================== movement
    def strafe_vec(self, obs, threat, t):
        pos = obs["self"]["pos"]
        if t > self.strafe_until:
            self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
            self.strafe_until = t + self.rng.uniform(0.8, 1.8)
        if threat is not None:
            to_t = unit(threat["pos"][0] - pos[0], threat["pos"][1] - pos[1])
        else:
            yaw = obs["self"]["aim"][0]
            to_t = (math.cos(yaw), math.sin(yaw))
        return (-to_t[1] * self.strafe_dir, to_t[0] * self.strafe_dir)

    def combat_move(self, obs, target, zone_vec, t):
        me = obs["self"]
        pos, tp = me["pos"], target["pos"]
        d = dist2d(pos, tp)
        to_t = unit(tp[0] - pos[0], tp[1] - pos[1])
        if t > self.strafe_until:
            self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.7 else self.strafe_dir
            self.strafe_until = t + self.rng.uniform(0.8, 2.0)
        perp = (-to_t[1] * self.strafe_dir, to_t[0] * self.strafe_dir)
        kinds = {w["kind"] for w in me["weapons"] if w}
        lo, hi = (20, 38) if "lightning" in kinds else (55, 110)
        if target["kind"] == "npc":
            lo, hi = 25, 50
        radial = 0.0
        if d > hi:
            radial = 0.5  # slow approach (red-bot style)
        elif d < lo:
            radial = -0.8
        mx = perp[0] + to_t[0] * radial + zone_vec[0] * 0.7
        my = perp[1] + to_t[1] * radial + zone_vec[1] * 0.7
        return unit(mx, my)

    def drift_move(self, obs, zone_vec, known, t):
        """Always moving; silent (< step_speed) when nobody is around."""
        me = obs["self"]
        pos = me["pos"]
        z = obs["zone"]
        if math.hypot(*zone_vec) > 0:
            return zone_vec, "drift-zone"
        if self.wander is None or dist2d(self.wander, pos) < 6 or \
                dist2d(self.wander, z["next_center"]) > z["next_radius"] * 0.6:
            r = z["next_radius"] * 0.5 * math.sqrt(self.rng.random())
            a = self.rng.uniform(-math.pi, math.pi)
            self.wander = (z["next_center"][0] + r * math.cos(a), z["next_center"][1] + r * math.sin(a))
        v = unit(self.wander[0] - pos[0], self.wander[1] - pos[1])
        k = 1.0 if known else SILENT_FRAC
        return (v[0] * k, v[1] * k), "drift"

    def loot_goal(self, obs):
        me = obs["self"]
        pos = me["pos"]
        best, bd = None, 1e9
        if me["weapons"][1] is None:
            held = {w["kind"] for w in me["weapons"] if w}
            for p in obs["pickups"]:
                if p["kind"] in held or WEAPON_PREF.get(p["kind"], 0) == 0:
                    continue
                d = dist2d(p["pos"], pos) - 15 * WEAPON_PREF[p["kind"]]
                if dist2d(p["pos"], pos) < 90 and abs(p["pos"][2] - pos[2]) < 6 and d < bd:
                    best, bd = p["pos"], d
        for c in obs["cubes"]:
            d = dist2d(c["pos"], pos)
            if d < 50 and abs(c["pos"][2] - pos[2]) < 6 and d < bd:
                best, bd = c["pos"], d
        return best

    def try_take(self, obs):
        me = obs["self"]
        if me["weapons"][1] is not None:
            return None
        held = {w["kind"] for w in me["weapons"] if w}
        for p in obs["pickups"]:
            if p["kind"] in held or WEAPON_PREF.get(p["kind"], 0) == 0:
                continue
            if dist2d(p["pos"], me["pos"]) < self.reach - 0.3 and abs(p["pos"][2] - me["pos"][2]) < 4:
                return p["id"]
        return None

    def unstick(self, obs, move, t):
        me = obs["self"]
        if t < self.unstick_until:
            return self.unstick_dir
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if math.hypot(*move) > 0.5 and speed < 1.5:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        if self.stuck_ticks >= 6:
            self.stuck_ticks = 0
            s = self.rng.choice((-1, 1))
            self.unstick_dir = unit(-move[1] * s + move[0] * 0.3, move[0] * s + move[1] * 0.3)
            self.unstick_until = t + 1.0
            self.stuck_jump = me["energy"] > 30
            return self.unstick_dir
        return move

    # ================================================================== aiming
    def aim_and_fire(self, obs, target, move, t):
        me = obs["self"]
        pos = me["pos"]
        muzzle_z = pos[2] + me["stats"].get("muzzle_height", 7)
        weapons = me["weapons"]
        fire = [False, False]
        cur_yaw, cur_pitch = me["aim"]
        lo_p, hi_p = self.cfg["mech"]["min_pitch"], self.cfg["mech"]["max_pitch"]

        if target is None:
            # A laser mid-charge: keep holding toward the last target, release late (never stuck).
            aim_at = None
            for i, w in enumerate(weapons):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    full = w["stats"]["charge"]
                    fire[i] = w["charge"] < full + 1.0
            if self.last_target_pos is not None and any(fire):
                lp = self.last_target_pos
                aim_at = [math.atan2(lp[1] - pos[1], lp[0] - pos[0]), 0.0]
            if aim_at:
                return aim_at, fire
            if self.threat_bearing is not None and t < self.threat_until:
                return [self.threat_bearing, 0.0], fire
            known = self.known_enemies(t)
            if known:
                k = min(known, key=lambda m: dist2d(m["pos"], pos))
                return [math.atan2(k["pos"][1] - pos[1], k["pos"][0] - pos[0]), 0.0], fire
            if math.hypot(*move) > 0.1:
                base = math.atan2(move[1], move[0])
                return [wrap(base + 0.6 * math.sin(t * 1.3)), 0.0], fire
            return [wrap(cur_yaw + 0.35), 0.0], fire

        tp = target["pos"]
        self.last_target_pos = tp
        h_mult = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * target.get("cubes", 0) \
            if target["kind"] == "mech" else 1.0
        tz = tp[2] + (4.5 if target["kind"] == "mech" else 1.75) * h_mult
        rel = [tp[0] - pos[0], tp[1] - pos[1], tz - muzzle_z]
        d = math.hypot(rel[0], rel[1])
        radius = (2.5 * h_mult) if target["kind"] == "mech" else 1.8

        gun = next((w for w in weapons if w and w["kind"] == "gun"), None)
        spd = gun["stats"]["speed"] if gun else 252.0
        lt = lead_time(rel, target["vel"], spd) or (d / spd)
        aim_pt = [rel[0] + target["vel"][0] * lt, rel[1] + target["vel"][1] * lt, rel[2] + target["vel"][2] * lt]
        yaw = math.atan2(aim_pt[1], aim_pt[0])
        pitch = math.atan2(aim_pt[2], math.hypot(aim_pt[0], aim_pt[1]))
        yaw_err = abs(angdiff(cur_yaw, yaw))
        tol = math.atan2(radius * 0.9, max(d, 1.0)) + 0.01
        aim_ok = yaw_err < tol and abs(cur_pitch - pitch) < tol + 0.03

        mortar_aim = None
        for i, w in enumerate(weapons):
            if not w:
                continue
            k, st = w["kind"], w["stats"]
            if k == "gun":
                fire[i] = aim_ok and d < st["range"] * 0.95 and w.get("reload", 0) == 0 and w.get("ammo", 1) > 0
            elif k == "laser":
                charge = w.get("charge", 0)
                full = st["charge"]
                if charge > 0:
                    # Release: full and on target (loose tolerance), or overdue regardless.
                    release = (charge >= full * 0.97 and yaw_err < 2.0 * tol) or charge >= full + 0.6
                    fire[i] = not release
                elif d < st["range"] * 0.95 and w["cooldown"] <= 0.05 and not self.block_charge and yaw_err < 0.5:
                    fire[i] = True
            elif k == "lightning":
                cone = math.radians(st["cone_deg"]) * 0.45
                fire[i] = d < st["range"] * 0.92 and yaw_err < cone and me["energy"] > st.get("energy", 3) + 5
            elif k == "mortar":
                if 30 < d < 220 and w["cooldown"] <= 0:
                    tt = d / (st["speed"] * 0.8)
                    lx, ly = rel[0] + target["vel"][0] * tt, rel[1] + target["vel"][1] * tt
                    p = ballistic_pitch(math.hypot(lx, ly), tp[2] - (pos[2] + me["stats"].get("height", 9) + 0.5),
                                        st["speed"], st["gravity"])
                    if p is not None:
                        mortar_aim = [math.atan2(ly, lx), p]
                        fire[i] = abs(angdiff(cur_yaw, mortar_aim[0])) < 0.04 and abs(cur_pitch - p) < 0.04

        if mortar_aim is not None and gun is not None and gun.get("reload", 0) > 0:
            yaw, pitch = mortar_aim
        mf.draw.line([pos[0], pos[1], muzzle_z], [tp[0], tp[1], tz], "#ff5040")
        return [yaw, clamp(pitch, lo_p, hi_p)], fire
