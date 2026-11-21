"""Base policy v0: survive in the zone, loot a second weapon, fight what we see.

Priorities each decision (highest first):
  1. Zone: never stay outside; head for the next circle early.
  2. Combat: target the best visible mech (low HP, close), lead shots, strafe, keep a range band.
  3. Loot: fill the empty weapon slot, grab cubes nearby.
  4. Idle: drift toward the next zone centre, scan with the torso.
Every branch reads numbers from hello.config / self.stats, never hard-coded balance.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional

import mechfight as mf
from util import angdiff, ballistic_pitch, clamp, dist2d, lead_time, unit, wrap, yaw_to
from world import World

# Weapon desirability for filling slot 1 (gun is always slot 0 at spawn).
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}

# Upgrade preference: survival first, then damage of what we hold.
UPGRADE_PREF = [
    "armor", "plating", "nanites", "gun_rate", "laser_damage", "laser_charge", "gun_damage",
    "servos", "lightning_efficiency", "gun_mag", "capacitor", "reactor", "jets", "gyros",
    "sensors", "gun_velocity", "lightning_range", "lightning_chain", "mortar_reload",
    "mortar_splash", "mortar_velocity", "laser_pierce",
]


class BasePolicy:
    name = "ctrlz-base-v0"

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
        self.last_move = (0.0, 0.0)
        mf.log(f"policy {self.name} slot={self.me_id} map_ok={self.world.ok}")

    # ------------------------------------------------------------------ main
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        t = obs["t"]
        act: Dict[str, Any] = {}

        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        self.read_events(obs, t)

        target = self.choose_target(obs)
        zone_vec, zone_urgent = self.zone_pressure(obs)

        move = (0.0, 0.0)
        label = "idle"
        if zone_urgent:
            move, label = zone_vec, "zone!"
        elif target is not None:
            move, label = self.combat_move(obs, target, zone_vec, t), f"fight {target['id']}"
        else:
            loot = self.loot_goal(obs)
            if loot is not None:
                move = unit(loot[0] - me["pos"][0], loot[1] - me["pos"][1])
                label = "loot"
            else:
                move, label = zone_vec, "drift"

        take = self.try_take(obs)
        if take is not None:
            act["take"] = take

        move = self.unstick(obs, move, t)
        self.last_move = move

        aim, fire = self.aim_and_fire(obs, target, move, t)
        act["aim"] = aim
        act["fire"] = fire
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = self.want_boost(obs, zone_urgent, target)

        gun = me["weapons"][0]
        if gun and gun["kind"] == "gun" and target is None and gun.get("reload", 0) == 0 \
                and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.5:
            act["reload"] = True

        mf.draw.label(f"{label} hp{me['hp']:.0f} e{me['energy']:.0f}")
        return act

    # ------------------------------------------------------------------ events
    def read_events(self, obs: mf.Observation, t: float) -> None:
        for ev in obs.get("events", []):
            if ev["type"] == "damaged" and ev.get("bearing") is not None:
                self.threat_bearing = ev["bearing"]
                self.threat_until = t + 2.5
            elif ev["type"] in ("kill", "took", "take_failed", "upgrade"):
                mf.log(f"t={t:.1f} event {ev}")

    # ------------------------------------------------------------------ upgrades
    def pick_upgrade(self, offer: Dict[str, Any]) -> int:
        ids = [c["id"] for c in offer["choices"]]
        for want in UPGRADE_PREF:
            if want in ids:
                return ids.index(want)
        return 0

    # ------------------------------------------------------------------ targeting
    def choose_target(self, obs: mf.Observation) -> Optional[Dict[str, Any]]:
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
        if best is None and me["hp"] > 0.6 * me["max_hp"]:
            # No mech in sight: farm a nearby NPC drone for cubes while healthy.
            for m in obs["mechs"]:
                if m["kind"] == "npc":
                    d = dist2d(m["pos"], me["pos"])
                    if d < 70 and d < best_score:
                        best, best_score = m, d
        return best

    # ------------------------------------------------------------------ zone
    def zone_pressure(self, obs: mf.Observation):
        """(direction toward safety, urgent?)"""
        me, z = obs["self"], obs["zone"]
        pos = me["pos"]
        t = obs["t"]
        # Where the edge is right now (interpolated if shrinking) and where it is going.
        if t >= z["shrink_start"] and z["shrink_end"] > z["shrink_start"]:
            f = clamp((t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]), 0, 1)
        else:
            f = 0.0
        cx = z["center"][0] + (z["next_center"][0] - z["center"][0]) * f
        cy = z["center"][1] + (z["next_center"][1] - z["center"][1]) * f
        r_now = z["radius"] + (z["next_radius"] - z["radius"]) * f
        nx, ny, nr = z["next_center"][0], z["next_center"][1], z["next_radius"]
        to_next = unit(nx - pos[0], ny - pos[1])
        d_next = dist2d(pos, (nx, ny))
        d_now = dist2d(pos, (cx, cy))
        time_left = max(z["shrink_end"] - t, 0.0)
        speed = me["stats"].get("walk_speed", 11.7)
        need = max(d_next - nr * 0.8, 0.0)  # metres to get comfortably inside the next circle
        urgent = (not me["in_zone"]) or d_now > r_now - 15 or (need > 0 and need > speed * time_left * 0.6)
        return to_next if need > 0 or urgent else (0.0, 0.0), urgent

    # ------------------------------------------------------------------ movement
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
        lo, hi = (20, 38) if "lightning" in kinds else (60, 140)
        if target["kind"] == "npc":
            lo, hi = 25, 50
        radial = 0.0
        hp_frac = me["hp"] / me["max_hp"]
        if hp_frac < 0.3:
            radial = -1.0  # back off when hurt
        elif d > hi:
            radial = 0.8
        elif d < lo:
            radial = -0.8
        mx = perp[0] + to_t[0] * radial + zone_vec[0] * 0.7
        my = perp[1] + to_t[1] * radial + zone_vec[1] * 0.7
        return unit(mx, my)

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
        """If we push a direction but don't move, jump/jet and sidestep for a moment."""
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
            return self.unstick_dir
        return move

    def want_boost(self, obs, zone_urgent, target):
        me = obs["self"]
        e, emax = me["energy"], me["max_energy"]
        if zone_urgent and not me["in_zone"]:
            return True
        if not me["grounded"]:
            return e > 20 and obs["t"] < self.unstick_until  # jet only while unsticking
        if target is not None and target["kind"] == "mech":
            return e > 0.5 * emax
        return zone_urgent and e > 0.3 * emax

    # ------------------------------------------------------------------ aiming
    def aim_and_fire(self, obs, target, move, t):
        me = obs["self"]
        pos = me["pos"]
        muzzle_z = pos[2] + me["stats"].get("muzzle_height", 7)
        weapons = me["weapons"]
        fire = [False, False]
        cur_yaw, cur_pitch = me["aim"]

        if target is None:
            # Release a charging laser rather than hold it uselessly.
            for i, w in enumerate(weapons):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = False
            if self.threat_bearing is not None and t < self.threat_until:
                return [self.threat_bearing, 0.0], fire
            if math.hypot(*move) > 0.1:
                base = math.atan2(move[1], move[0])
                return [wrap(base + 0.6 * math.sin(t * 1.3)), 0.0], fire
            return [wrap(cur_yaw + 0.35), 0.0], fire

        tp = target["pos"]
        h_mult = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * target.get("cubes", 0) \
            if target["kind"] == "mech" else 1.0
        tz = tp[2] + (4.5 if target["kind"] == "mech" else 1.75) * h_mult
        rel = [tp[0] - pos[0], tp[1] - pos[1], tz - muzzle_z]
        d = math.hypot(rel[0], rel[1])
        radius = 2.5 if target["kind"] == "mech" else 1.8

        # Primary aim solution: gun lead (also fine for hitscan at these speeds).
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
                if d < st["range"] * 0.95 and w["cooldown"] <= 0.05:
                    # Hold to charge; release (False) once full and on target.
                    full = charge >= st["charge"] * 0.97
                    fire[i] = not (full and aim_ok)
                    if charge >= st["charge"] + st.get("hold_s", 2) * 0.8:
                        fire[i] = False
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

        # If the mortar is the only thing ready and the gun is reloading, point the tube.
        if mortar_aim is not None and gun is not None and gun.get("reload", 0) > 0:
            yaw, pitch = mortar_aim
        mf.draw.line([pos[0], pos[1], muzzle_z], [tp[0], tp[1], tz], "#ff5040")
        lo_p, hi_p = self.cfg["mech"]["min_pitch"], self.cfg["mech"]["max_pitch"]
        return [yaw, clamp(pitch, lo_p, hi_p)], fire
