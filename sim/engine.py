"""Mech Royale lockstep world simulation (mechfight/1 rules, kit/protocol.md §5-§10).

One Match = one game. The server (sim/server.py) feeds it one action per mech per decision and
calls `decide()` which simulates `decide_every` ticks. Observations for external bots come from
`obs_for(mech)`. House bots (sim/house.py) read the Match directly.

Approximations (documented in sim/README.md): eaves/roof-edge sliding, terrain slope test,
crown-as-cylinder canopy, NPC drones walk on the ground, supply drops land on open ground.
"""
from __future__ import annotations

import copy
import math
import random

TAU = 2 * math.pi
WK = ["gun", "mortar", "laser", "lightning"]


def wrap(a):
    a = (a + math.pi) % TAU - math.pi
    return a


def seg_cyl(ax, ay, az, dx, dy, dz, cx, cy, r, z0, z1):
    """First s in [0,1] where segment a + s*d enters the vertical cylinder; None if never.
    If a is already inside, returns 0."""
    fx, fy = ax - cx, ay - cy
    A = dx * dx + dy * dy
    C = fx * fx + fy * fy - r * r
    if A < 1e-12:
        if C > 0:
            return None
        s0, s1 = 0.0, 1.0
    else:
        B = 2 * (fx * dx + fy * dy)
        disc = B * B - 4 * A * C
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        s0 = (-B - sq) / (2 * A)
        s1 = (-B + sq) / (2 * A)
        if s1 < 0 or s0 > 1:
            return None
        if s0 < 0:
            s0 = 0.0
        if s1 > 1:
            s1 = 1.0
    # vertical slab
    if abs(dz) < 1e-12:
        if az < z0 or az > z1:
            return None
        return s0
    t0 = (z0 - az) / dz
    t1 = (z1 - az) / dz
    if t0 > t1:
        t0, t1 = t1, t0
    lo = max(s0, t0)
    hi = min(s1, t1)
    if lo > hi:
        return None
    return lo


class Weapon:
    __slots__ = ("kind", "cooldown", "ammo", "reload", "charge", "charging", "held_full", "release_pending")

    def __init__(self, kind, stats):
        self.kind = kind
        self.cooldown = 0.0
        self.ammo = int(stats.get("mag", 0)) if kind == "gun" else 0
        self.reload = 0.0
        self.charge = 0.0
        self.charging = False
        self.held_full = 0.0
        self.release_pending = False


class Mech:
    def __init__(self, mid, cfg):
        self.id = mid
        self.kind = "mech"
        self.name = f"m{mid}"
        self.house = None        # HouseBot or None
        self.team = "house"      # "ext" for external bots
        self.diff = None
        self.base_mech = copy.deepcopy(cfg["mech"])
        self.base_weapons = copy.deepcopy(cfg["weapons"])
        self.upgrades = []
        self.cubes = 0
        self.recompute()
        self.hp = self.stats["hp"]
        self.energy = self.stats["energy"]
        self.x = self.y = self.z = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.yaw = self.pitch = 0.0
        self.cmd_yaw = self.cmd_pitch = 0.0
        self.grounded = True
        self.alive = True
        self.place = None
        self.death_t = None
        self.weapons = [Weapon("gun", self.wstats["gun"]), None]
        self.last_spend = -1e9
        self.last_combat = -1e9
        self.zone_out_since = None
        self.shorted_until = -1e9
        self.events = []
        self.sounds = []
        self.offers = []       # queued upgrade offers (dicts)
        self.offer = None
        self.offer_seq = 0
        self.step_acc = 0.0
        self.boosting = False
        self.jetting = False
        self.act = {}
        self.kills = 0
        self.killed_by = None
        self.killed_with = None
        self.dmg_dealt = {}
        self.dmg_taken = {}
        self.hits_made = {}
        self.shots = {}
        self.rear_taken = 0
        self.hits_taken = {}
        self.weapons_taken = 0
        self.first_weapon_t = None
        self.cubes_max = 0
        self.drops_taken = 0
        self.stationary_t = 0.0
        self.alive_t = 0.0
        self.seen = {}         # id -> last seen time (for obs memory)
        self.vis_cache = {}
        self.in_zone = True
        self._dying = False
        self.spawn = (0.0, 0.0, 0.0)

    # -------------------------------------------------------------- derived stats
    def recompute(self):
        m = copy.deepcopy(self.base_mech)
        w = copy.deepcopy(self.base_weapons)
        ups = getattr(self, "_updefs", {})
        for uid in self.upgrades:
            u = ups.get(uid)
            if not u:
                continue
            for eff in u.get("effects", []):
                path = eff["path"].split(".")
                tgt = m if path[0] == "mech" else w[path[1]]
                key = path[-1]
                if "mul" in eff:
                    tgt[key] = tgt[key] * eff["mul"]
                if "add" in eff:
                    tgt[key] = tgt[key] + eff["add"]
        sc = 1 + 0.05 * self.cubes
        m["radius"] = self.base_mech["radius"] * sc
        m["height"] = self.base_mech["height"] * sc
        m["eye_height"] = self.base_mech["eye_height"] * sc
        m["muzzle_height"] = self.base_mech["muzzle_height"] * sc
        self.stats = m
        self.wstats = w

    def max_hp(self, cfg):
        return self.stats["hp"] * (1 + cfg["cubes"]["hp_per_cube"] * min(self.cubes, cfg["cubes"]["stat_cap"]))

    def dmg_mult(self, cfg):
        return 1 + cfg["cubes"]["damage_per_cube"] * min(self.cubes, cfg["cubes"]["stat_cap"])

    def has(self, kind):
        return any(w is not None and w.kind == kind for w in self.weapons)

    def weapon_kinds(self):
        return [w.kind for w in self.weapons if w is not None]

    def charging(self):
        return any(w is not None and w.kind == "laser" and w.charging for w in self.weapons)


class Npc:
    __slots__ = ("id", "kind", "hx", "hy", "x", "y", "z", "vx", "vy", "yaw", "hp", "max_hp", "target",
                 "cooldown", "gx", "gy", "regoal", "alive", "returning")

    def __init__(self, nid, x, y, z, hp):
        self.id = nid
        self.kind = "npc"
        self.hx, self.hy = x, y
        self.x, self.y, self.z = x, y, z
        self.vx = self.vy = 0.0
        self.yaw = 0.0
        self.hp = self.max_hp = hp
        self.target = None
        self.cooldown = 0.0
        self.gx, self.gy = x, y
        self.regoal = 0.0
        self.alive = True
        self.returning = False


class Proj:
    __slots__ = ("id", "kind", "owner", "team", "x", "y", "z", "vx", "vy", "vz", "ttl", "dmg", "rad", "grav", "w")

    def __init__(self, pid, kind, owner, team, x, y, z, vx, vy, vz, ttl, dmg, rad, grav, w):
        self.id, self.kind, self.owner, self.team = pid, kind, owner, team
        self.x, self.y, self.z, self.vx, self.vy, self.vz = x, y, z, vx, vy, vz
        self.ttl, self.dmg, self.rad, self.grav, self.w = ttl, dmg, rad, grav, w


class Match:
    def __init__(self, cfg, gmap, seed, ext_ids=(), house_factory=None, record=False, params=None):
        self.cfg = cfg
        self.map = gmap
        self.seed = seed
        self.params = params or {}
        self.rng = random.Random(seed * 1000003 + 17)          # world setup (identical across A/B)
        self.crng = random.Random(seed * 7919 + 3)            # combat noise
        self.hz = cfg["match"]["tick_hz"]
        self.dt = 1.0 / self.hz
        self.decide_every = cfg["match"]["decide_every"]
        self.tick = 0
        self.t = 0.0
        self.next_id = {"proj": 1, "crate": 1000, "cube": 5000, "pickup": 8000, "drop": 20000, "npc": 100}
        self.updefs = {u["id"]: u for u in cfg.get("upgrades", [])}
        n = cfg["match"]["mechs"]
        self.mechs = []
        for i in range(n):
            m = Mech.__new__(Mech)
            m._updefs = self.updefs
            Mech.__init__(m, i, cfg)
            self.mechs.append(m)
        for i in ext_ids:
            self.mechs[i].team = "ext"
        self.npcs = []
        self.crates = {}
        self.items = {}         # id -> dict(kind, x, y, z, [ammo])  cubes + weapon pickups
        self.item_cells = {}
        self.projs = []
        self.beams = []         # (tick, by, kind, path)
        self.drops = []
        self.sound_log = []
        self.record = record
        self.frames = []
        self.rev = []           # replay events since last frame
        self.over = False
        self.result_places = {}
        self._setup_zone()
        self._spawn_mechs()
        self._spawn_world()
        if house_factory:
            house_factory(self)

    # ================================================================ setup
    def _setup_zone(self):
        z = self.cfg["zone"]
        cx, cy = z["start_center"]
        r = z["start_radius"]
        t = 0.0
        self.zone_phases = []   # list of (hold_start, shrink_start, shrink_end, c0, r0, c1, r1, dps)
        for ph in z["phases"]:
            r1 = ph["radius"]
            d = self.rng.random() ** 0.5 * max(0.0, r - r1)
            a = self.rng.uniform(0, TAU)
            c1 = (cx + d * math.cos(a), cy + d * math.sin(a))
            ss = t + ph["hold_s"]
            se = ss + ph["shrink_s"]
            self.zone_phases.append((t, ss, se, (cx, cy), r, c1, r1, ph["dps"]))
            cx, cy = c1
            r = r1
            t = se
        self.zone_end_t = t

    def zone_state(self, t=None):
        """(center, radius, next_center, next_radius, shrink_start, shrink_end, dps, phase_idx)."""
        t = self.t if t is None else t
        for i, (h0, ss, se, c0, r0, c1, r1, dps) in enumerate(self.zone_phases):
            if t < se:
                if t <= ss:
                    c, r = c0, r0
                else:
                    f = (t - ss) / (se - ss)
                    c = (c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f)
                    r = r0 + (r1 - r0) * f
                return c, r, c1, r1, ss, se, dps, i
        h0, ss, se, c0, r0, c1, r1, dps = self.zone_phases[-1]
        return c1, r1, c1, r1, ss, se, dps, len(self.zone_phases) - 1

    def _random_open(self, cx, cy, rad, margin=None, tries=400):
        b = self.map.bounds
        mg = self.cfg["match"]["arena_margin"] if margin is None else margin
        for _ in range(tries):
            d = self.rng.random() ** 0.5 * rad
            a = self.rng.uniform(0, TAU)
            x, y = cx + d * math.cos(a), cy + d * math.sin(a)
            if not (b["minX"] + mg < x < b["maxX"] - mg and b["minY"] + mg < y < b["maxY"] - mg):
                continue
            if self.map.open_ground(x, y):
                return x, y, self.map.ground(x, y)
        return None

    def _spawn_mechs(self):
        mc = self.cfg["match"]
        z = self.cfg["zone"]
        cx, cy = z["start_center"]
        rad = z["start_radius"] * mc["spawn_zone_fraction"]
        sep = mc["spawn_min_separation"]
        pts = []
        while len(pts) < len(self.mechs):
            cur_sep = sep
            for attempt in range(3000):
                p = self._random_open(cx, cy, rad)
                if p and all(math.hypot(p[0] - q[0], p[1] - q[1]) >= cur_sep for q in pts):
                    pts.append(p)
                    break
                if attempt % 500 == 499:
                    cur_sep *= 0.85
        for m, p in zip(self.mechs, pts):
            m.x, m.y, m.z = p
            m.yaw = m.cmd_yaw = math.atan2(cy - m.y, cx - m.x)
            m.spawn = p

    def _spawn_world(self):
        cfg = self.cfg
        zc = cfg["zone"]["start_center"]
        zr = cfg["zone"]["start_radius"]
        # NPC drones
        for _ in range(cfg["npc"]["count"]):
            p = self._random_open(zc[0], zc[1], zr)
            if p:
                nid = self._nid("npc")
                self.npcs.append(Npc(nid, p[0], p[1], p[2], cfg["npc"]["hp"]))
        # crates
        cc = cfg["crates"]
        for k in range(cc["count"]):
            p = None
            if self.rng.random() < cc["roof_fraction"]:
                p = self.map.random_roof_point(self.rng)
            if p is None:
                p = self._random_open(zc[0], zc[1], zr)
            if p:
                cid = self._nid("crate")
                self.crates[cid] = {"id": cid, "x": p[0], "y": p[1], "z": p[2], "hp": cc["hp"]}
        # floor loot
        lc = cfg["loot"]
        kinds = [k for k, w in lc["weights"].items() if w > 0]
        weights = [lc["weights"][k] for k in kinds]
        for k in range(lc["floor_count"]):
            p = None
            if self.rng.random() < lc["floor_roof_fraction"]:
                p = self.map.random_roof_point(self.rng)
            if p is None:
                p = self._random_open(zc[0], zc[1], zr)
            if p:
                kind = self.rng.choices(kinds, weights)[0]
                self._add_item(kind, p[0], p[1], p[2], log=False)
        # supply drops: placed in the circle the zone is closing to at call time
        dc = cfg["drops"]
        for tc in dc["times"]:
            st = self.zone_state(tc)
            c1, r1 = st[2], st[3]
            p = self._random_open(c1[0], c1[1], max(5.0, r1 * dc["spread"]), margin=10)
            if p is None:
                p = (c1[0], c1[1], self.map.ground(*c1))
            did = self._nid("drop")
            self.drops.append({"id": did, "x": p[0], "y": p[1], "z": p[2], "call_t": tc, "land_t": tc + dc["fall_s"],
                               "holder": None, "progress": 0.0, "taken": False, "called": False, "landed": False})

    def _nid(self, k):
        v = self.next_id[k]
        self.next_id[k] = v + 1
        return v

    # ================================================================ items
    def _add_item(self, kind, x, y, z, ammo=None, log=True, src=None):
        k = "cube" if kind == "cube" else "pickup"
        iid = self._nid(k)
        it = {"id": iid, "kind": kind, "x": x, "y": y, "z": z}
        if ammo is not None:
            it["ammo"] = ammo
        self.items[iid] = it
        key = (int(x // 8), int(y // 8))
        self.item_cells.setdefault(key, set()).add(iid)
        if log and self.record:
            if kind == "cube":
                self.rev.append({"t": self.tick, "e": "cube", "id": iid, "at": [round(x, 2), round(y, 2), round(z, 2)]})
            else:
                self.rev.append({"t": self.tick, "e": "weapon", "op": "spawn", "id": iid, "kind": kind,
                                 "at": [round(x, 2), round(y, 2), round(z, 2)], "src": src})
        return iid

    def _del_item(self, iid):
        it = self.items.pop(iid, None)
        if it:
            key = (int(it["x"] // 8), int(it["y"] // 8))
            s = self.item_cells.get(key)
            if s:
                s.discard(iid)
        return it

    def items_near(self, x, y, r):
        out = []
        for cx in range(int((x - r) // 8), int((x + r) // 8) + 1):
            for cy in range(int((y - r) // 8), int((y + r) // 8) + 1):
                s = self.item_cells.get((cx, cy))
                if s:
                    for iid in s:
                        it = self.items[iid]
                        if (it["x"] - x) ** 2 + (it["y"] - y) ** 2 <= r * r:
                            out.append(it)
        return out

    # ================================================================ events / sounds
    def ev(self, m, e):
        if m.team == "ext":
            m.events.append(e)

    def sound(self, kind, x, y, z, src):
        self.sound_log.append((self.t, kind, x, y, z, src))

    def alive_mechs(self):
        return [m for m in self.mechs if m.alive]

    def n_alive(self):
        return sum(1 for m in self.mechs if m.alive)

    # ================================================================ damage
    def damage(self, tgt, amount, src, weapon, ox=None, oy=None, effect=None, raw=False):
        """Apply weapon damage from `src` (Mech/Npc/None) to `tgt` (Mech/Npc/crate dict).
        ox, oy: where the hit came from (for rear armor). Returns damage dealt."""
        cfg = self.cfg
        if isinstance(tgt, dict):  # crate
            if tgt["hp"] <= 0:
                return 0
            if isinstance(src, Mech) and not raw:
                amount *= src.dmg_mult(cfg)
            tgt["hp"] -= amount
            if isinstance(src, Mech):
                self.ev(src, {"type": "hit", "target": tgt["id"], "amount": round(amount, 1), "weapon": weapon})
                if self.record:
                    self.rev.append({"t": self.tick, "e": "hit", "src": src.id, "dst": tgt["id"], "amt": round(amount, 1), "w": weapon})
            if tgt["hp"] <= 0:
                self._crate_destroyed(tgt, src)
            return amount
        if not tgt.alive:
            return 0
        if isinstance(src, Mech) and isinstance(tgt, Mech) and src.team == "house" and tgt.team == "house" and src is not tgt:
            return 0  # squad: no friendly fire
        rear = False
        if not raw:
            if isinstance(src, Mech):
                amount *= src.dmg_mult(cfg)
            if isinstance(tgt, Mech):
                if ox is not None:
                    a = math.atan2(oy - tgt.y, ox - tgt.x)
                    if abs(wrap(a - tgt.yaw)) > math.radians(tgt.stats["front_arc_deg"]) / 2:
                        rear = True
                        amount *= tgt.stats["rear_mult"]
                if weapon == "laser" and tgt.has("mortar"):
                    amount *= 1 + cfg["weapons"]["laser"]["cookoff"]
                    effect = "cookoff"
                    if self.record:
                        self.rev.append({"t": self.tick, "e": "cookoff", "id": tgt.id, "by": src.id if src else None,
                                         "at": [round(tgt.x, 2), round(tgt.y, 2), round(tgt.z + 4.5, 2)]})
                    self.ev(tgt, {"type": "effect", "effect": "cookoff", "by": src.id if src else None})
                if weapon != "zone":
                    amount *= max(0.0, 1 - tgt.stats.get("armor", 0))
        if amount <= 0:
            return 0
        tgt.hp -= amount
        t = self.t
        if isinstance(tgt, Mech):
            tgt.last_combat = t
            tgt.dmg_taken[weapon] = tgt.dmg_taken.get(weapon, 0) + amount
            if rear:
                tgt.rear_taken += 1
            tgt.hits_taken[weapon] = tgt.hits_taken.get(weapon, 0) + 1
            if tgt.team == "ext":
                if weapon == "zone":
                    e = {"type": "damaged", "amount": round(amount, 1), "weapon": "zone", "bearing": None, "by": None}
                else:
                    b = math.atan2(oy - tgt.y, ox - tgt.x) if ox is not None else None
                    e = {"type": "damaged", "amount": round(amount, 1), "weapon": weapon,
                         "bearing": round(b, 3) if b is not None else None, "by": src.id if src is not None else None}
                    if rear:
                        e["rear"] = True
                tgt.events.append(e)
            if tgt.house is not None and src is not None and src is not tgt:
                tgt.house.on_damaged(src, amount, weapon)
        else:  # npc
            if src is not None and isinstance(src, Mech):
                tgt.target = src.id
                tgt.returning = False
        if isinstance(src, Mech) and src is not tgt:
            src.last_combat = t
            src.dmg_dealt[weapon] = src.dmg_dealt.get(weapon, 0) + amount
            if isinstance(tgt, Mech):
                src.hits_made[weapon] = src.hits_made.get(weapon, 0) + 1
            e = {"type": "hit", "target": tgt.id, "amount": round(amount, 1), "weapon": weapon}
            if effect:
                e["effect"] = effect
            if rear:
                e["rear"] = True
            self.ev(src, e)
        if self.record and weapon != "zone":
            r = {"t": self.tick, "e": "hit", "src": src.id if src is not None else None, "dst": tgt.id, "amt": round(amount, 1), "w": weapon}
            if effect:
                r["fx"] = effect
            if rear:
                r["rear"] = 1
            self.rev.append(r)
        if tgt.hp <= 0:
            tgt.hp = 0
            if isinstance(tgt, Mech):
                tgt.killed_by = src.id if isinstance(src, Mech) and src is not tgt else None
                tgt.killed_with = weapon
                tgt.alive = False   # place assigned at end of tick
                tgt._dying = True
            else:
                self._npc_died(tgt, src)
        return amount

    def _crate_destroyed(self, c, src):
        self.crates.pop(c["id"], None)
        self.sound("crate", c["x"], c["y"], c["z"], None)
        if self.record:
            self.rev.append({"t": self.tick, "e": "crate", "id": c["id"], "by": src.id if src is not None else None})
        for _ in range(self.cfg["crates"]["cubes"]):
            self._add_item("cube", c["x"], c["y"], c["z"])
        if self.crng.random() < self.cfg["loot"]["crate_weapon_chance"]:
            lc = self.cfg["loot"]["weights"]
            kinds = [k for k, w in lc.items() if w > 0]
            kind = self.crng.choices(kinds, [lc[k] for k in kinds])[0]
            self._add_item(kind, c["x"] + 1.0, c["y"], c["z"], src=c["id"])

    def _npc_died(self, n, src):
        n.alive = False
        if self.record:
            self.rev.append({"t": self.tick, "e": "death", "id": n.id, "by": src.id if src is not None else None})
        for k in range(self.cfg["npc"]["cubes"]):
            a = self.crng.uniform(0, TAU)
            self._add_item("cube", n.x + 1.2 * math.cos(a), n.y + 1.2 * math.sin(a), n.z)

    def _mech_died(self, m, place):
        m.place = place
        m.death_t = self.t
        m.vx = m.vy = m.vz = 0
        self.sound("death", m.x, m.y, m.z, m.id)
        killer = self.mechs[m.killed_by] if m.killed_by is not None else None
        if killer is not None:
            killer.kills += 1
            self.ev(killer, {"type": "kill", "target": m.id})
        if self.record:
            self.rev.append({"t": self.tick, "e": "death", "id": m.id, "by": m.killed_by, "place": place})
        for slot, w in enumerate(m.weapons):
            if w is not None and w.kind != "gun":
                a = self.crng.uniform(0, TAU)
                iid = self._add_item(w.kind, m.x + 2 * math.cos(a), m.y + 2 * math.sin(a), m.z, log=False)
                if self.record:
                    self.rev.append({"t": self.tick, "e": "weapon", "op": "drop", "id": iid, "kind": w.kind, "by": m.id,
                                     "slot": slot, "at": [round(m.x, 2), round(m.y, 2), round(m.z, 2)]})
        for k in range(m.cubes + self.cfg["cubes"]["death_drop_extra"]):
            a = self.crng.uniform(0, TAU)
            d = self.crng.uniform(0.5, 3)
            self._add_item("cube", m.x + d * math.cos(a), m.y + d * math.sin(a), m.z)
        if m.house is not None:
            m.house.on_death()

    # ================================================================ cubes / upgrades
    def give_cube(self, m, n=1):
        cfg = self.cfg
        old_max = m.max_hp(cfg)
        thr = cfg["cubes"]["upgrade_thresholds"]
        for _ in range(n):
            m.cubes += 1
            if m.cubes in thr:
                self._make_offer(m)
        m.cubes_max = max(m.cubes_max, m.cubes)
        m.recompute()
        new_max = m.max_hp(cfg)
        if new_max > old_max:
            m.hp += new_max - old_max

    def _make_offer(self, m):
        held = set(m.weapon_kinds())
        cands = []
        for uid, u in self.updefs.items():
            if m.upgrades.count(uid) >= u.get("max", 1):
                continue
            req = u.get("requires")
            if req and req not in held:
                continue
            cands.append(uid)
        if not cands:
            return
        self.crng.shuffle(cands)
        ch = cands[:3]
        m.offer_seq += 1
        m.offers.append({"id": m.offer_seq, "choices": ch})

    def _apply_upgrade(self, m, uid):
        old_max = m.max_hp(self.cfg)
        old_e = m.stats["energy"]
        m.upgrades.append(uid)
        m.recompute()
        new_max = m.max_hp(self.cfg)
        if new_max > old_max:
            m.hp += new_max - old_max
        if m.stats["energy"] > old_e:
            m.energy += m.stats["energy"] - old_e
        self.ev(m, {"type": "upgrade", "id": uid})
        if self.record:
            self.rev.append({"t": self.tick, "e": "upgrade", "id": m.id, "up": uid})

    def _offers_tick(self, m, pick):
        if m.offer is None and m.offers:
            o = m.offers.pop(0)
            o["expires"] = self.t + self.cfg["bot"]["offer_timeout_s"]
            m.offer = o
        if m.offer is None:
            return
        if pick is not None and isinstance(pick, int) and 0 <= pick < len(m.offer["choices"]):
            self._apply_upgrade(m, m.offer["choices"][pick])
            m.offer = None
        elif self.t >= m.offer["expires"]:
            self._apply_upgrade(m, m.offer["choices"][0])
            m.offer = None

    # ================================================================ main loop
    def decide(self, actions):
        """actions: id -> action dict (already coast-resolved by the caller). Simulates decide_every ticks."""
        for m in self.mechs:
            if m.alive:
                m.act = actions.get(m.id) or {}
                # one-shots that apply once at the decision
                a = m.act
                self._offers_tick(m, a.get("pick"))
                if a.get("take") is not None or a.get("drop") is not None:
                    self._take_drop(m, a.get("take"), a.get("drop"))
                if a.get("reload"):
                    w = m.weapons[0] if m.weapons[0] and m.weapons[0].kind == "gun" else (
                        m.weapons[1] if m.weapons[1] and m.weapons[1].kind == "gun" else None)
                    if w and w.reload <= 0 and w.ammo < m.wstats["gun"]["mag"]:
                        w.reload = m.wstats["gun"]["reload"]
                        self.sound("reload", m.x, m.y, m.z, m.id)
        for k in range(self.decide_every):
            self._tick(first=(k == 0))
            if self.over:
                break

    def _take_drop(self, m, take, drop):
        ws = m.wstats
        if take is not None:
            it = self.items.get(take)
            if it is None or it["kind"] == "cube":
                self.ev(m, {"type": "take_failed", "id": take, "reason": "gone"})
                return
            if math.hypot(it["x"] - m.x, it["y"] - m.y) > self.cfg["loot"]["pickup_radius"] or abs(it["z"] - m.z) > 4:
                self.ev(m, {"type": "take_failed", "id": take, "reason": "range"})
                return
            if m.has(it["kind"]):
                self.ev(m, {"type": "take_failed", "id": take, "reason": "have"})
                return
            slot = None
            if drop in (0, 1):
                slot = drop
            elif m.weapons[0] is None:
                slot = 0
            elif m.weapons[1] is None:
                slot = 1
            if slot is None:
                self.ev(m, {"type": "take_failed", "id": take, "reason": "full"})
                return
            old = m.weapons[slot]
            if old is not None and old.kind == "laser" and old.charging:
                self.ev(m, {"type": "take_failed", "id": take, "reason": "busy"})
                return
            self._del_item(take)
            if old is not None:
                did = self._add_item(old.kind, m.x, m.y, m.z, ammo=old.ammo if old.kind == "gun" else None, log=False)
                self.ev(m, {"type": "dropped", "id": did, "kind": old.kind, "slot": slot})
                if self.record:
                    self.rev.append({"t": self.tick, "e": "weapon", "op": "drop", "id": did, "kind": old.kind, "by": m.id,
                                     "slot": slot, "at": [round(m.x, 2), round(m.y, 2), round(m.z, 2)]})
            w = Weapon(it["kind"], ws[it["kind"]])
            if it["kind"] == "gun" and "ammo" in it:
                w.ammo = it["ammo"]
            w.cooldown = max(self.cfg["loot"]["equip_s"], ws[it["kind"]].get("cooldown", 0))
            m.weapons[slot] = w
            m.weapons_taken += 1
            if m.first_weapon_t is None and it["kind"] != "gun":
                m.first_weapon_t = self.t
            self.ev(m, {"type": "took", "id": take, "kind": it["kind"], "slot": slot})
            if self.record:
                self.rev.append({"t": self.tick, "e": "weapon", "op": "take", "id": take, "kind": it["kind"], "by": m.id, "slot": slot})
        elif drop in (0, 1):
            old = m.weapons[drop]
            if old is None or (old.kind == "laser" and old.charging):
                return
            m.weapons[drop] = None
            did = self._add_item(old.kind, m.x, m.y, m.z, ammo=old.ammo if old.kind == "gun" else None, log=False)
            self.ev(m, {"type": "dropped", "id": did, "kind": old.kind, "slot": drop})
            if self.record:
                self.rev.append({"t": self.tick, "e": "weapon", "op": "drop", "id": did, "kind": old.kind, "by": m.id,
                                 "slot": drop, "at": [round(m.x, 2), round(m.y, 2), round(m.z, 2)]})

    def _tick(self, first):
        dt = self.dt
        for m in self.mechs:
            if m.alive:
                self._mech_controls(m, first)
        for m in self.mechs:
            if m.alive:
                self._mech_move(m)
        self._push_apart()
        for n in self.npcs:
            if n.alive:
                self._npc_tick(n)
        self._projectiles()
        self._pickups()
        self._drops_tick()
        self._zone_and_regen()
        # deaths
        dying = [m for m in self.mechs if getattr(m, "_dying", False)]
        if dying:
            alive_before = self.n_alive() + len(dying)
            place = alive_before - len(dying) + 1
            for m in dying:
                m._dying = False
                self._mech_died(m, place)
        self.tick += 1
        self.t = self.tick * dt
        if self.record and self.tick % self.cfg["replay"]["frame_every"] == 0:
            self._frame()
        # cull old sounds / beams
        if self.tick % 20 == 0:
            self.sound_log = [s for s in self.sound_log if self.t - s[0] < 1.0]
            self.beams = [b for b in self.beams if self.tick - b[0] < 20]
        alive = self.n_alive()
        if alive <= 1 or self.t >= self.cfg["match"]["duration_s"] - 1e-9:
            self._finish()

    def _finish(self):
        if self.over:
            return
        self.over = True
        surv = sorted([m for m in self.mechs if m.alive], key=lambda m: -m.hp)
        for i, m in enumerate(surv):
            m.place = i + 1
        if self.record:
            self.rev.append({"t": self.tick, "e": "end", "tick": self.tick, "places": [m.place for m in self.mechs]})

    # ---------------------------------------------------------------- controls (aim, weapons)
    def _mech_controls(self, m, first):
        a = m.act
        s = m.stats
        dt = self.dt
        aim = a.get("aim")
        if aim is not None:
            m.cmd_yaw = float(aim[0])
            m.cmd_pitch = float(aim[1])
        cp = min(max(m.cmd_pitch, s["min_pitch"]), s["max_pitch"])
        d = wrap(m.cmd_yaw - m.yaw)
        mx = s["turn_rate"] * dt
        m.yaw = wrap(m.yaw + (d if abs(d) <= mx else math.copysign(mx, d)))
        dp = cp - m.pitch
        mp = s["pitch_rate"] * dt
        m.pitch += dp if abs(dp) <= mp else math.copysign(mp, dp)
        fire = a.get("fire") or (False, False)
        for slot in (0, 1):
            w = m.weapons[slot]
            if w is None:
                continue
            trig = bool(fire[slot]) if slot < len(fire) else False
            self._weapon_tick(m, w, trig)

    def _muzzle(self, m, pitch=None):
        s = m.stats
        yaw = m.yaw
        p = m.pitch if pitch is None else pitch
        cp = math.cos(p)
        dx, dy, dz = cp * math.cos(yaw), cp * math.sin(yaw), math.sin(p)
        mx, my, mz = m.x, m.y, m.z + s["muzzle_height"]
        off = s["radius"] + 0.5
        ox, oy, oz = mx + dx * off, my + dy * off, mz + dz * off
        hit = self.map.seg_hit(mx, my, mz, ox, oy, oz)
        blocked = False
        if hit is not None:
            ox, oy, oz = mx + dx * off * hit, my + dy * off * hit, mz + dz * off * hit
            blocked = True
        return (ox, oy, oz), (dx, dy, dz), blocked

    def _weapon_tick(self, m, w, trig):
        dt = self.dt
        ws = m.wstats[w.kind]
        if w.cooldown > 0:
            w.cooldown -= dt
        k = w.kind
        if k == "gun":
            if w.reload > 0:
                w.reload -= dt
                if w.reload <= 0:
                    w.reload = 0.0
                    w.ammo = int(ws["mag"])
            if trig and w.reload <= 0 and w.ammo > 0 and w.cooldown <= 1e-9:
                self._fire_gun(m, w, ws)
                w.cooldown += ws["cooldown"]
                w.ammo -= 1
                if w.ammo <= 0:
                    w.reload = ws["reload"]
                    self.sound("reload", m.x, m.y, m.z, m.id)
            elif w.cooldown < 0:
                w.cooldown = 0.0
        elif k == "lightning":
            if trig and w.cooldown <= 1e-9 and m.energy >= ws["energy"]:
                self._fire_lightning(m, w, ws)
                w.cooldown += ws["cooldown"]
                m.energy -= ws["energy"]
                m.last_spend = self.t
            elif w.cooldown < 0:
                w.cooldown = 0.0
        elif k == "mortar":
            if trig and w.cooldown <= 1e-9:
                self._fire_mortar(m, w, ws)
                w.cooldown = ws["cooldown"]
            elif w.cooldown < 0:
                w.cooldown = 0.0
        elif k == "laser":
            if w.cooldown < 0:
                w.cooldown = 0.0
            if w.charging:
                w.charge += dt
                full = ws["charge"]
                if w.charge >= full:
                    w.held_full += dt
                if not trig:
                    w.release_pending = True
                if w.release_pending and w.charge >= ws["min_charge"] - 1e-9:
                    self._fire_laser(m, w, ws)
                elif w.held_full >= ws["hold_s"]:
                    self._fire_laser(m, w, ws)
            elif trig and w.cooldown <= 1e-9 and self.t >= m.shorted_until:
                w.charging = True
                w.charge = 0.0
                w.held_full = 0.0
                w.release_pending = False
                self.sound("laser_charge", m.x, m.y, m.z, m.id)
                if self.record:
                    self.rev.append({"t": self.tick, "e": "charge", "id": m.id, "yaw": round(m.yaw, 3), "pitch": round(m.pitch, 3),
                                     "dur": ws["charge"] + ws["hold_s"], "full": ws["charge"], "range": ws["range"]})

    def _fire_gun(self, m, w, ws):
        sp = math.radians(ws["spread_deg"])
        yaw = m.yaw + self.crng.gauss(0, sp)
        pitch = m.pitch + self.crng.gauss(0, sp)
        cp = math.cos(pitch)
        d = (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))
        s = m.stats
        mx, my, mz = m.x, m.y, m.z + s["muzzle_height"]
        off = s["radius"] + 0.5
        o = (mx + d[0] * off, my + d[1] * off, mz + d[2] * off)
        m.shots["gun"] = m.shots.get("gun", 0) + 1
        self.sound("gun", m.x, m.y, m.z, m.id)
        if self.record:
            self.rev.append({"t": self.tick, "e": "fire", "id": m.id, "w": "gun"})
        hit = self.map.seg_hit(mx, my, mz, o[0], o[1], o[2])
        if hit is not None:
            return  # barrel in a wall
        spd = ws["speed"]
        pid = self._nid("proj")
        self.projs.append(Proj(pid, "gun", m, m.team, o[0], o[1], o[2], d[0] * spd, d[1] * spd, d[2] * spd,
                               ws["range"] / spd, ws["damage"], self.cfg["projectile"]["gun_radius"], 0.0, "gun"))

    def _fire_mortar(self, m, w, ws):
        o, d, blocked = self._muzzle(m)
        s = m.stats
        # mortar tube is at height + 0.5
        o = (o[0], o[1], o[2] + (s["height"] + 0.5 - s["muzzle_height"]))
        m.shots["mortar"] = m.shots.get("mortar", 0) + 1
        self.sound("mortar", m.x, m.y, m.z, m.id)
        if self.record:
            self.rev.append({"t": self.tick, "e": "fire", "id": m.id, "w": "mortar"})
        if blocked:
            self._explode(o[0], o[1], o[2], m, ws, None)
            return
        spd = ws["speed"]
        pid = self._nid("proj")
        self.projs.append(Proj(pid, "mortar", m, m.team, o[0], o[1], o[2], d[0] * spd, d[1] * spd, d[2] * spd,
                               ws["ttl"], ws["damage"], 0.3, ws["gravity"], "mortar"))

    def _targets_for(self, m):
        """Damageable things for shooter m: list of (obj, cx, cy, r, z0, z1)."""
        out = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if m is not None and m.team == "house" and o.team == "house":
                continue
            st = o.stats
            out.append((o, o.x, o.y, st["radius"], o.z, o.z + st["height"]))
        nc = self.cfg["npc"]
        for n in self.npcs:
            if n.alive:
                out.append((n, n.x, n.y, nc["radius"], n.z, n.z + nc["height"]))
        cc = self.cfg["crates"]
        for c in self.crates.values():
            out.append((c, c["x"], c["y"], cc["radius"], c["z"], c["z"] + cc["height"]))
        return out

    def _fire_laser(self, m, w, ws):
        frac = min(1.0, w.charge / ws["charge"]) if ws["charge"] > 0 else 1.0
        dmg = ws["damage"] * (ws["min_frac"] + (1 - ws["min_frac"]) * frac)
        w.charging = False
        w.charge = 0.0
        w.held_full = 0.0
        w.release_pending = False
        w.cooldown = ws["cooldown"]
        o, d, blocked = self._muzzle(m)
        m.shots["laser"] = m.shots.get("laser", 0) + 1
        self.sound("laser", m.x, m.y, m.z, m.id)
        rng = ws["range"]
        ex, ey, ez = o[0] + d[0] * rng, o[1] + d[1] * rng, o[2] + d[2] * rng
        wall = 0.0 if blocked else self.map.seg_hit(o[0], o[1], o[2], ex, ey, ez)
        smax = 1.0 if wall is None else wall
        hits = []
        if smax > 0:
            dx, dy, dz = ex - o[0], ey - o[1], ez - o[2]
            for (obj, cx, cy, r, z0, z1) in self._targets_for(m):
                # quick reject: distance from the line in 2D
                s = seg_cyl(o[0], o[1], o[2], dx, dy, dz, cx, cy, r, z0, z1)
                if s is not None and s <= smax:
                    hits.append((s, obj))
            hits.sort(key=lambda h: h[0])
        n_hit = 1 + int(ws.get("pierce", 0))
        hit_objs = hits[:n_hit]
        end_s = hit_objs[-1][0] if hit_objs and len(hit_objs) >= n_hit else smax
        for s, obj in hit_objs:
            self.damage(obj, dmg, m, "laser", o[0], o[1])
        to = (o[0] + (ex - o[0]) * end_s, o[1] + (ey - o[1]) * end_s, o[2] + (ez - o[2]) * end_s)
        self.beams.append((self.tick, m.id, "laser", [list(o), list(to)]))
        if self.record:
            self.rev.append({"t": self.tick, "e": "laser", "id": m.id, "from": [round(v, 2) for v in o],
                             "to": [round(v, 2) for v in to], "hits": [h[1]["id"] if isinstance(h[1], dict) else h[1].id for h in hit_objs]})

    def _fire_lightning(self, m, w, ws):
        s = m.stats
        mx, my, mz = m.x, m.y, m.z + s["muzzle_height"]
        m.shots["lightning"] = m.shots.get("lightning", 0) + 1
        self.sound("lightning", m.x, m.y, m.z, m.id)
        rng = ws["range"]
        half = math.radians(ws["cone_deg"]) / 2
        best = None
        cands = self._targets_for(m)
        for (obj, cx, cy, r, z0, z1) in cands:
            dx, dy = cx - mx, cy - my
            dist = math.hypot(dx, dy)
            zc = (z0 + z1) * 0.5
            d3 = math.sqrt(dist * dist + (zc - mz) ** 2)
            if d3 > rng + r:
                continue
            if abs(wrap(math.atan2(dy, dx) - m.yaw)) > half + (math.atan2(r, dist) if dist > 0 else 0):
                continue
            if best is not None and d3 >= best[0]:
                continue
            if self.map.seg_hit(mx, my, mz, cx, cy, zc) is not None:
                continue
            best = (d3, obj, cx, cy, zc)
        path = [[round(mx, 2), round(my, 2), round(mz, 2)]]
        hits = []
        if best is None:
            # arc along the aim to the first wall or range
            o, d, blocked = self._muzzle(m)
            ex, ey, ez = mx + d[0] * rng, my + d[1] * rng, mz + d[2] * rng
            hw = self.map.seg_hit(mx, my, mz, ex, ey, ez)
            f = 1.0 if hw is None else hw
            path.append([round(mx + (ex - mx) * f, 2), round(my + (ey - my) * f, 2), round(mz + (ez - mz) * f, 2)])
        else:
            dmg = ws["damage"]
            cur = best
            done = set()
            jumps = int(ws["chain"])
            for j in range(jumps + 1):
                d3, obj, cx, cy, zc = cur
                oid = obj["id"] if isinstance(obj, dict) else obj.id
                done.add(oid)
                hits.append(oid)
                path.append([round(cx, 2), round(cy, 2), round(zc, 2)])
                src_xy = (path[-2][0], path[-2][1])
                effect = None
                if isinstance(obj, Mech) and obj.has("laser"):
                    effect = "short"
                    for ow in obj.weapons:
                        if ow is not None and ow.kind == "laser" and ow.charging:
                            ow.charging = False
                            ow.charge = 0.0
                    obj.shorted_until = self.t + ws["short_s"]
                    self.ev(obj, {"type": "effect", "effect": "short", "by": m.id})
                    if self.record:
                        self.rev.append({"t": self.tick, "e": "short", "id": obj.id, "by": m.id, "cancelled": True, "dur": ws["short_s"]})
                self.damage(obj, dmg, m, "lightning", src_xy[0], src_xy[1], effect=effect)
                if j == jumps:
                    break
                nxt = None
                for (o2, x2, y2, r2, a2, b2) in cands:
                    oid2 = o2["id"] if isinstance(o2, dict) else o2.id
                    if oid2 in done:
                        continue
                    if isinstance(o2, Mech) and not o2.alive:
                        continue
                    if isinstance(o2, Npc) and not o2.alive:
                        continue
                    z2 = (a2 + b2) * 0.5
                    dd = math.sqrt((x2 - cx) ** 2 + (y2 - cy) ** 2 + (z2 - zc) ** 2)
                    if dd <= ws["chain_range"] and (nxt is None or dd < nxt[0]):
                        if self.map.seg_hit(cx, cy, zc, x2, y2, z2) is None:
                            nxt = (dd, o2, x2, y2, z2)
                if nxt is None:
                    break
                cur = nxt
                dmg *= ws["chain_falloff"]
        self.beams.append((self.tick, m.id, "lightning", path))
        if self.record:
            self.rev.append({"t": self.tick, "e": "zap", "id": m.id, "path": path, "hits": hits})

    def _explode(self, x, y, z, owner, ws, direct):
        """Mortar blast at (x, y, z). direct = object hit directly (gets ws.damage)."""
        if self.record:
            self.rev.append({"t": self.tick, "e": "boom", "p": 0, "at": [round(x, 2), round(y, 2), round(z, 2)], "r": ws["splash_radius"]})
        self.sound("explosion", x, y, z, owner.id if owner else None)
        if direct is not None:
            self.damage(direct, ws["damage"], owner, "mortar", x, y)
        R = ws["splash_radius"]
        cands = self._targets_for(None)
        for (obj, cx, cy, r, z0, z1) in cands:
            if obj is direct:
                continue
            zc = min(max(z, z0), z1)
            dx, dy = cx - x, cy - y
            dh = max(0.0, math.hypot(dx, dy) - r)
            dist = math.sqrt(dh * dh + (zc - z) ** 2)
            if dist > R:
                continue
            if self.map.seg_hit(x, y, z + 0.3, cx, cy, (z0 + z1) / 2) is not None:
                continue
            f = 1 - (1 - ws["splash_edge"]) * (dist / R)
            friendly = isinstance(obj, Mech) and owner is not None and obj.team == "house" and owner.team == "house" and obj is not owner
            if friendly:
                continue
            amt = ws["splash"] * f
            if obj is owner:
                amt *= owner.stats["self_damage_mult"]
            self.damage(obj, amt, owner, "mortar", x, y, effect="concuss" if isinstance(obj, Mech) else None)
            if isinstance(obj, Mech) and obj.alive:
                d = math.hypot(dx, dy) or 1.0
                obj.vx += dx / d * ws["knock"] * f
                obj.vy += dy / d * ws["knock"] * f
                obj.vz = max(obj.vz, 0) + ws["knock_up"] * f
                obj.grounded = False
                obj.energy = max(0.0, obj.energy - ws["drain"] * f)
                self.ev(obj, {"type": "effect", "effect": "concuss", "by": owner.id if owner else None})
                if self.record:
                    self.rev.append({"t": self.tick, "e": "concuss", "id": obj.id, "by": owner.id if owner else None,
                                     "v": [round(dx / d * ws["knock"] * f, 2), round(dy / d * ws["knock"] * f, 2), round(ws["knock_up"] * f, 2)],
                                     "drain": round(ws["drain"] * f, 2)})

    # ---------------------------------------------------------------- movement
    def _mech_move(self, m):
        a = m.act
        s = m.stats
        dt = self.dt
        mv = a.get("move") or (0.0, 0.0)
        mx, my = float(mv[0]), float(mv[1])
        L = math.hypot(mx, my)
        if L > 1:
            mx, my = mx / L, my / L
            L = 1.0
        boost = bool(a.get("boost"))
        moving = L > 0.05
        spend = False
        m.boosting = False
        m.jetting = False
        if m.grounded:
            spd = s["walk_speed"]
            if boost and moving and m.energy > 0:
                spd = s["boost_speed"]
                m.energy = max(0.0, m.energy - s["boost_drain"] * dt)
                spend = True
                m.boosting = True
            if m.charging():
                spd *= s["charge_move_mult"]
            tvx, tvy = mx * spd, my * spd
            acc = s["ground_accel"]
        else:
            spd = s["walk_speed"] * (s["charge_move_mult"] if m.charging() else 1.0)
            tvx, tvy = mx * spd, my * spd
            acc = s["air_accel"]
        dvx, dvy = tvx - m.vx, tvy - m.vy
        dl = math.hypot(dvx, dvy)
        mxa = acc * dt
        if dl > mxa:
            dvx, dvy = dvx / dl * mxa, dvy / dl * mxa
        m.vx += dvx
        m.vy += dvy
        # vertical
        if m.grounded and a.get("jump") and self._first_tick_of_decision():
            m.vz = s["jump_speed"]
            m.grounded = False
        if not m.grounded:
            if boost and m.energy > 0:
                m.vz = min(m.vz + s["jet_accel"] * dt, max(m.vz, s["max_rise_speed"]))
                m.energy = max(0.0, m.energy - s["jet_drain"] * dt)
                spend = True
                m.jetting = True
            m.vz -= s["gravity"] * dt
        if spend:
            m.last_spend = self.t
        elif self.t - m.last_spend >= s["energy_regen_delay"]:
            m.energy = min(s["energy"], m.energy + s["energy_regen"] * dt)
        # integrate horizontally with collision
        r = s["radius"]
        h = s["height"]
        step = s["step_up"]
        ox, oy, oz = m.x, m.y, m.z
        nx_, ny_ = m.x + m.vx * dt, m.y + m.vy * dt
        nz = m.z + m.vz * dt
        # ceiling (bridges / eaves): stop a rising head
        if m.vz > 0:
            c = self.map.ceiling_at(nx_, ny_, oz + h - 0.5)
            if c is not None and nz + h > c:
                nz = max(oz, c - h)
                m.vz = 0.0
        gmap = self.map
        nx_, ny_, normals = gmap.push_out(nx_, ny_, max(nz, oz) if m.vz <= 0 else nz, r, h, step)
        for (px, py) in normals:
            vn = m.vx * px + m.vy * py
            if vn < 0:
                m.vx -= vn * px
                m.vy -= vn * py
        # slope limit on terrain
        if m.grounded:
            g0 = gmap.ground(ox, oy)
            g1 = gmap.ground(nx_, ny_)
            hd = math.hypot(nx_ - ox, ny_ - oy)
            if hd > 1e-6 and g1 - g0 > hd * self._tan_slope(s) and g1 > oz + 0.05:
                f, on = gmap.floor_at(nx_, ny_, oz, step)
                if not on:
                    nx_, ny_ = ox, oy
                    m.vx = m.vy = 0.0
        # bounds
        b = gmap.bounds
        nx_ = min(max(nx_, b["minX"] + r), b["maxX"] - r)
        ny_ = min(max(ny_, b["minY"] + r), b["maxY"] - r)
        # floor
        f, on = gmap.floor_at(nx_, ny_, max(nz, oz) if m.grounded else max(nz, oz), step if m.grounded else 0.3)
        if nz <= f:
            nz = f
            if m.vz < 0:
                m.vz = 0.0
            m.grounded = True
        elif m.grounded and m.vz <= 0 and nz - f < 0.7:
            nz = f  # follow slopes / small steps down
            m.vz = 0.0
        else:
            m.grounded = False
        m.x, m.y, m.z = nx_, ny_, nz
        hs = math.hypot(m.vx, m.vy)
        if hs < 0.5:
            m.stationary_t += dt
        m.alive_t += dt
        # step / boost / jet sounds
        if m.grounded and hs > self.cfg["sound"]["step_speed"]:
            m.step_acc += dt
            if m.step_acc >= self.cfg["sound"]["step_every"]:
                m.step_acc = 0.0
                self.sound("boost" if m.boosting else "step", m.x, m.y, m.z, m.id)
        if m.jetting and self.tick % 6 == 0:
            self.sound("jet", m.x, m.y, m.z, m.id)

    def _tan_slope(self, s):
        v = getattr(self, "_ts", None)
        if v is None:
            v = self._ts = math.tan(math.radians(s["max_slope_deg"]))
        return v

    def _first_tick_of_decision(self):
        return self.tick % self.decide_every == 0

    def _push_apart(self):
        ms = [m for m in self.mechs if m.alive]
        n = len(ms)
        for i in range(n):
            a = ms[i]
            for j in range(i + 1, n):
                b = ms[j]
                dx = b.x - a.x
                if dx > 6 or dx < -6:
                    continue
                dy = b.y - a.y
                if dy > 6 or dy < -6:
                    continue
                rr = a.stats["radius"] + b.stats["radius"]
                d2 = dx * dx + dy * dy
                if d2 >= rr * rr or abs(a.z - b.z) > max(a.stats["height"], b.stats["height"]):
                    continue
                d = math.sqrt(d2) or 1e-3
                pen = (rr - d) / 2
                ux, uy = (dx / d, dy / d) if d2 > 1e-9 else (1.0, 0.0)
                a.x -= ux * pen
                a.y -= uy * pen
                b.x += ux * pen
                b.y += uy * pen

    # ---------------------------------------------------------------- NPC drones
    def _npc_tick(self, n):
        nc = self.cfg["npc"]
        dt = self.dt
        n.cooldown -= dt
        tgt = self.mechs[n.target] if n.target is not None else None
        home_d = math.hypot(n.x - n.hx, n.y - n.hy)
        if tgt is not None:
            if not tgt.alive or math.hypot(tgt.x - n.x, tgt.y - n.y) > nc["deaggro_range"] or home_d > nc["chase_leash"]:
                n.target = None
                tgt = None
                n.returning = True
        if tgt is None and not n.returning and home_d <= nc["leash"] and self.tick % 10 == n.id % 10:
            # aggro on mechs within aggro_range with LOS
            best = None
            for m in self.mechs:
                if not m.alive:
                    continue
                d = math.hypot(m.x - n.x, m.y - n.y)
                if d < nc["aggro_range"] and (best is None or d < best[0]):
                    if not self.map.sight_blocked(n.x, n.y, n.z + nc["height"] * 0.7, m.x, m.y, m.z + 4.5, trees=False):
                        best = (d, m)
            if best:
                n.target = best[1].id
                tgt = best[1]
        if tgt is not None:
            dx, dy = tgt.x - n.x, tgt.y - n.y
            d = math.hypot(dx, dy) or 1.0
            n.yaw = math.atan2(dy, dx)
            if d > 25:
                gx, gy = dx / d, dy / d
            else:
                gx, gy = -dy / d * 0.5, dx / d * 0.5
            if n.cooldown <= 0 and d < nc["deaggro_range"]:
                ox, oy, oz = n.x, n.y, n.z + nc["height"] * 0.7
                tz = tgt.z + tgt.stats["height"] * 0.5
                if not self.map.sight_blocked(ox, oy, oz, tgt.x, tgt.y, tz, trees=False):
                    sp = nc["projectile_speed"]
                    # lead
                    tt = d / sp * getattr(self, "npc_lead", 1.0)  # calibration: 1 = perfect lead, 0 = none
                    px, py, pz = tgt.x + tgt.vx * tt, tgt.y + tgt.vy * tt, tz
                    yaw = math.atan2(py - oy, px - ox) + self.crng.gauss(0, math.radians(nc["spread_deg"]))
                    pitch = math.atan2(pz - oz, math.hypot(px - ox, py - oy)) + self.crng.gauss(0, math.radians(nc["spread_deg"]))
                    cp = math.cos(pitch)
                    pid = self._nid("proj")
                    off = nc["radius"] + 0.5
                    vx, vy, vz = cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)
                    self.projs.append(Proj(pid, "npc", n, "npc", ox + vx * off, oy + vy * off, oz + vz * off,
                                           vx * sp, vy * sp, vz * sp, 120.0 / sp, nc["damage"], self.cfg["projectile"]["npc_radius"], 0.0, "npc"))
                    self.sound("npc_gun", n.x, n.y, n.z, n.id)
                    n.cooldown = nc["cooldown"]
        else:
            if n.returning:
                gx, gy = n.hx - n.x, n.hy - n.y
                if math.hypot(gx, gy) < 3:
                    n.returning = False
            else:
                if self.t >= n.regoal or math.hypot(n.gx - n.x, n.gy - n.y) < 2:
                    a = self.crng.uniform(0, TAU)
                    d = self.crng.uniform(0, nc["leash"])
                    n.gx, n.gy = n.hx + d * math.cos(a), n.hy + d * math.sin(a)
                    n.regoal = self.t + self.crng.uniform(4, 10)
                gx, gy = n.gx - n.x, n.gy - n.y
            L = math.hypot(gx, gy) or 1.0
            gx, gy = gx / L, gy / L
            n.yaw = math.atan2(gy, gx)
        n.vx, n.vy = gx * nc["speed"], gy * nc["speed"]
        nx_, ny_ = n.x + n.vx * dt, n.y + n.vy * dt
        nx_, ny_, _ = self.map.push_out(nx_, ny_, n.z, nc["radius"], nc["height"], 1.2)
        f, on = self.map.floor_at(nx_, ny_, n.z, 1.2)
        n.x, n.y, n.z = nx_, ny_, f

    # ---------------------------------------------------------------- projectiles
    def _projectiles(self):
        if not self.projs:
            return
        dt = self.dt
        keep = []
        gmap = self.map
        mechs = [m for m in self.mechs if m.alive]
        npcs = [n for n in self.npcs if n.alive]
        nc = self.cfg["npc"]
        cc = self.cfg["crates"]
        crates = list(self.crates.values())
        for p in self.projs:
            ax, ay, az = p.x, p.y, p.z
            if p.grav:
                p.vz -= p.grav * dt
            bx, by, bz = ax + p.vx * dt, ay + p.vy * dt, az + p.vz * dt
            dx, dy, dz = bx - ax, by - ay, bz - az
            best_s = gmap.seg_hit(ax, ay, az, bx, by, bz)
            best_o = None
            x0, x1 = (ax, bx) if ax < bx else (bx, ax)
            y0, y1 = (ay, by) if ay < by else (by, ay)
            pr = p.rad
            own = p.owner
            for m in mechs:
                if m is own:
                    continue
                if p.team == "house" and m.team == "house":
                    continue
                r = m.stats["radius"] + pr
                if m.x + r < x0 or m.x - r > x1 or m.y + r < y0 or m.y - r > y1:
                    continue
                s = seg_cyl(ax, ay, az, dx, dy, dz, m.x, m.y, r, m.z - pr, m.z + m.stats["height"] + pr)
                if s is not None and (best_s is None or s < best_s):
                    best_s, best_o = s, m
            if p.team != "npc":
                r = nc["radius"] + pr
                for n in npcs:
                    if n.x + r < x0 or n.x - r > x1 or n.y + r < y0 or n.y - r > y1:
                        continue
                    s = seg_cyl(ax, ay, az, dx, dy, dz, n.x, n.y, r, n.z - pr, n.z + nc["height"] + pr)
                    if s is not None and (best_s is None or s < best_s):
                        best_s, best_o = s, n
                r = cc["radius"] + pr
                for c in crates:
                    if c["x"] + r < x0 or c["x"] - r > x1 or c["y"] + r < y0 or c["y"] - r > y1:
                        continue
                    s = seg_cyl(ax, ay, az, dx, dy, dz, c["x"], c["y"], r, c["z"] - pr, c["z"] + cc["height"] + pr)
                    if s is not None and (best_s is None or s < best_s):
                        best_s, best_o = s, c
            if best_s is not None:
                hx, hy, hz = ax + dx * best_s, ay + dy * best_s, az + dz * best_s
                if p.kind == "mortar":
                    self._explode(hx, hy, hz, own, own.wstats["mortar"] if isinstance(own, Mech) else self.cfg["weapons"]["mortar"], best_o)
                elif best_o is not None:
                    src = own if (not isinstance(own, Mech) or own.alive or True) else None
                    dmg = p.dmg
                    if isinstance(own, Mech):
                        dmg = own.wstats["gun"]["damage"] if p.kind == "gun" else dmg
                    self.damage(best_o, dmg, src if p.kind != "npc" else own, p.w, ax - dx * 10, ay - dy * 10)
                if self.record:
                    hid = None if best_o is None else (best_o["id"] if isinstance(best_o, dict) else best_o.id)
                    self.rev.append({"t": self.tick, "e": "impact", "p": p.id, "at": [round(hx, 2), round(hy, 2), round(hz, 2)], "hit": hid})
                continue
            p.x, p.y, p.z = bx, by, bz
            p.ttl -= dt
            if p.ttl <= 0:
                if p.kind == "mortar":
                    self._explode(bx, by, bz, own, own.wstats["mortar"], None)
                continue
            keep.append(p)
        self.projs = keep

    # ---------------------------------------------------------------- pickups, drops, zone
    def _pickups(self):
        if self.tick % 2:
            return
        cr = self.cfg["cubes"]["pickup_radius"]
        claims = {}
        for m in self.mechs:
            if not m.alive:
                continue
            for it in self.items_near(m.x, m.y, cr):
                if it["kind"] != "cube" or abs(it["z"] - m.z) > 4:
                    continue
                d = math.hypot(it["x"] - m.x, it["y"] - m.y)
                c = claims.get(it["id"])
                if c is None or d < c[0] or (d == c[0] and m.id < c[1].id):
                    claims[it["id"]] = (d, m)
        for iid, (d, m) in claims.items():
            self._del_item(iid)
            self.give_cube(m)
            self.ev(m, {"type": "pickup", "cubes": m.cubes})
            if self.record:
                self.rev.append({"t": self.tick, "e": "pickup", "id": iid, "by": m.id})

    def _drops_tick(self):
        dc = self.cfg["drops"]
        t = self.t
        for d in self.drops:
            if d["taken"]:
                continue
            if not d["called"] and t >= d["call_t"]:
                d["called"] = True
                self.sound("drop", d["x"], d["y"], d["z"], None)
                if self.record:
                    self.rev.append({"t": self.tick, "e": "drop", "op": "call", "id": d["id"], "at": [round(d["x"], 2), round(d["y"], 2), round(d["z"], 2)],
                                     "lands": int(round(d["land_t"] * self.hz)), "r": dc["radius"]})
            if not d["called"]:
                continue
            if not d["landed"] and t >= d["land_t"]:
                d["landed"] = True
                self.sound("drop", d["x"], d["y"], d["z"], None)
                if self.record:
                    self.rev.append({"t": self.tick, "e": "drop", "op": "land", "id": d["id"]})
            if not d["landed"]:
                continue
            inside = [m for m in self.mechs if m.alive and math.hypot(m.x - d["x"], m.y - d["y"]) <= dc["radius"] and abs(m.z - d["z"]) < 6]
            if len(inside) == 1:
                m = inside[0]
                if d["holder"] != m.id:
                    d["holder"] = m.id
                    d["progress"] = 0.0
                d["progress"] += self.dt
                if d["progress"] >= dc["capture_s"]:
                    d["taken"] = True
                    self.give_cube(m, dc["cubes"])
                    if dc.get("repair"):
                        m.hp = m.max_hp(self.cfg)
                        m.energy = m.stats["energy"]
                    m.drops_taken += 1
                    self.ev(m, {"type": "drop", "id": d["id"], "cubes": m.cubes})
                    if self.record:
                        self.rev.append({"t": self.tick, "e": "drop", "op": "take", "id": d["id"], "by": m.id})
            elif len(inside) == 0:
                d["holder"] = None
                d["progress"] = 0.0

    def _zone_and_regen(self):
        c, r, c1, r1, ss, se, dps, ph = self.zone_state()
        dt = self.dt
        cfg = self.cfg
        for m in self.mechs:
            if not m.alive:
                continue
            inz = math.hypot(m.x - c[0], m.y - c[1]) <= r
            m.in_zone = inz
            if not inz:
                m.zone_acc = getattr(m, "zone_acc", 0.0) + dps * dt
                m.hp -= dps * dt
                m.last_combat = self.t
                if m.team == "ext":
                    m.events.append({"type": "damaged", "amount": round(dps * dt, 2), "weapon": "zone", "bearing": None, "by": None})
                m.dmg_taken["zone"] = m.dmg_taken.get("zone", 0) + dps * dt
                if m.hp <= 0:
                    m.hp = 0
                    m.alive = False
                    m._dying = True
                    m.killed_by = None
                    m.killed_with = "zone"
                continue
            mh = m.max_hp(cfg)
            if m.hp < mh and self.t - m.last_combat >= m.stats["regen_delay"]:
                m.hp = min(mh, m.hp + m.stats["regen_per_s"] * dt)

    # ================================================================ vision
    def can_see(self, v, x, y, zc, ztop, tree_ok=True):
        """Mech v sees point (x, y) with centre height zc / top ztop?"""
        s = v.stats
        dx, dy = x - v.x, y - v.y
        d2 = dx * dx + dy * dy
        near = self.cfg["vision"]["near_range"] + s.get("near_range_bonus", 0)
        rng = self.cfg["vision"]["range"] + s.get("vision_bonus", 0)
        if d2 > rng * rng:
            return False
        trees = False
        if d2 > near * near:
            if abs(wrap(math.atan2(dy, dx) - v.yaw)) > math.radians(self.cfg["vision"]["fov_deg"]) / 2:
                return False
            trees = self.cfg["vision"].get("canopy_blocks", True) and tree_ok
        ex, ey, ez = v.x, v.y, v.z + s["eye_height"]
        g = self.map
        if not g.sight_blocked(ex, ey, ez, x, y, zc, trees):
            return True
        return not g.sight_blocked(ex, ey, ez, x, y, ztop, trees)

    def sees_mech(self, v, o):
        st = o.stats
        return self.can_see(v, o.x, o.y, o.z + st["height"] * 0.5, o.z + st["height"])

    # ================================================================ observation
    def obs_for(self, m):
        cfg = self.cfg
        t = self.t
        mem = cfg["vision"]["memory_s"]
        s = m.stats
        # mechs + npcs
        seen_list = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees_mech(m, o):
                m.seen[o.id] = t
            if t - m.seen.get(o.id, -1e9) <= mem + 1e-9:
                e = {"id": o.id, "kind": "mech", "pos": [round(o.x, 2), round(o.y, 2), round(o.z, 2)],
                     "vel": [round(o.vx, 2), round(o.vy, 2), round(o.vz, 2)], "aim": [round(o.yaw, 3), round(o.pitch, 3)],
                     "cubes": o.cubes, "weapons": o.weapon_kinds(), "charging": o.charging()}
                if cfg["vision"].get("show_enemy_hp", True):
                    e["hp"] = round(o.hp, 1)
                    e["max_hp"] = round(o.max_hp(cfg), 1)
                seen_list.append(e)
        nc = cfg["npc"]
        for n in self.npcs:
            if not n.alive:
                continue
            if (n.x - m.x) ** 2 + (n.y - m.y) ** 2 < 250 * 250 and self.can_see(m, n.x, n.y, n.z + nc["height"] * 0.5, n.z + nc["height"]):
                m.seen[n.id] = t
            if t - m.seen.get(n.id, -1e9) <= mem + 1e-9:
                seen_list.append({"id": n.id, "kind": "npc", "pos": [round(n.x, 2), round(n.y, 2), round(n.z, 2)],
                                  "vel": [round(n.vx, 2), round(n.vy, 2), 0], "aim": [round(n.yaw, 3), 0], "hp": round(n.hp, 1), "max_hp": n.max_hp})
        # static-ish things: cached visibility, refreshed every 0.5 s
        vc = m.vis_cache
        refresh = (self.tick // self.decide_every) % 5 == 0

        def vis(key, x, y, z0, z1):
            if refresh or key not in vc:
                vc[key] = self.can_see(m, x, y, z0, z1)
            return vc[key]
        rng = cfg["vision"]["range"] + s.get("vision_bonus", 0)
        r2 = rng * rng
        crates = []
        ch = cfg["crates"]["height"]
        for c in self.crates.values():
            if (c["x"] - m.x) ** 2 + (c["y"] - m.y) ** 2 > r2:
                continue
            if vis(c["id"], c["x"], c["y"], c["z"] + ch * 0.5, c["z"] + ch):
                crates.append({"id": c["id"], "pos": [round(c["x"], 2), round(c["y"], 2), round(c["z"], 2)], "hp": round(c["hp"], 1)})
        cubes, pickups = [], []
        for it in self.items.values():
            if (it["x"] - m.x) ** 2 + (it["y"] - m.y) ** 2 > r2:
                continue
            if vis(it["id"], it["x"], it["y"], it["z"] + 0.5, it["z"] + 1.0):
                if it["kind"] == "cube":
                    cubes.append({"id": it["id"], "pos": [round(it["x"], 2), round(it["y"], 2), round(it["z"], 2)]})
                else:
                    pickups.append({"id": it["id"], "kind": it["kind"], "pos": [round(it["x"], 2), round(it["y"], 2), round(it["z"], 2)]})
        projs = []
        for p in self.projs:
            if p.owner is m:
                continue
            if (p.x - m.x) ** 2 + (p.y - m.y) ** 2 > r2:
                continue
            if self.can_see(m, p.x, p.y, p.z, p.z, tree_ok=False):
                projs.append({"id": p.id, "kind": p.kind, "pos": [round(p.x, 2), round(p.y, 2), round(p.z, 2)],
                              "vel": [round(p.vx, 2), round(p.vy, 2), round(p.vz, 2)]})
        # beams since last obs that we could see (shooter visible or end near us)
        beams = []
        last = getattr(m, "_last_obs_tick", self.tick - self.decide_every)
        for (bt, by, kind, path) in self.beams:
            if bt < last or by == m.id:
                continue
            ok = False
            for pt in (path[0], path[-1]):
                if self.can_see(m, pt[0], pt[1], pt[2], pt[2], tree_ok=False):
                    ok = True
                    break
            if ok:
                beams.append({"by": by, "kind": kind, "path": [[round(v, 2) for v in q] for q in path], "age": round((self.tick - bt) * self.dt, 2)})
        # sounds since last obs
        sounds = []
        sc = cfg["sound"]
        sig = math.radians(sc["bearing_noise_deg"]) * s.get("hearing", 1)
        last_t = last * self.dt
        for (st_, kind, x, y, z, src) in self.sound_log:
            if st_ < last_t - 1e-9 or src == m.id:
                continue
            d = math.hypot(x - m.x, y - m.y)
            if d > sc["range"].get(kind, 0):
                continue
            b = math.atan2(y - m.y, x - m.x) + self.crng.gauss(0, sig)
            sounds.append({"kind": kind, "bearing": round(wrap(b), 3), "dist": "near" if d < sc["near"] else ("mid" if d < sc["mid"] else "far"),
                           "age": round(max(0.0, t - st_), 2)})
        m._last_obs_tick = self.tick
        # self
        ws = []
        for w in m.weapons:
            if w is None:
                ws.append(None)
                continue
            e = {"kind": w.kind, "cooldown": round(max(0.0, w.cooldown), 3), "stats": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.wstats[w.kind].items()}}
            if w.kind == "gun":
                e["ammo"] = w.ammo
                e["reload"] = round(max(0.0, w.reload), 3)
            if w.kind == "laser":
                e["charge"] = round(w.charge, 3) if w.charging else 0
            ws.append(e)
        zc, zr, nc_, nr, ss, se, dps, ph = self.zone_state()
        inz = math.hypot(m.x - zc[0], m.y - zc[1]) <= zr
        drops = []
        for d in self.drops:
            if d["called"] and not d["taken"]:
                drops.append({"id": d["id"], "pos": [round(d["x"], 2), round(d["y"], 2), round(d["z"], 2)], "radius": cfg["drops"]["radius"],
                              "lands_in": round(max(0.0, d["land_t"] - t), 2), "capture_s": cfg["drops"]["capture_s"],
                              "progress": round(d["progress"], 2), "holder": d["holder"]})
        offer = None
        if m.offer is not None:
            offer = {"id": m.offer["id"], "expires": round(m.offer["expires"], 2),
                     "choices": [{"id": u, "name": self.updefs[u].get("name", u), "desc": self.updefs[u].get("desc", "")} for u in m.offer["choices"]]}
        events = m.events
        m.events = []
        obs = {
            "type": "obs", "tick": self.tick, "t": round(t, 2), "alive": self.n_alive(),
            "self": {"id": m.id, "pos": [round(m.x, 2), round(m.y, 2), round(m.z, 2)], "vel": [round(m.vx, 2), round(m.vy, 2), round(m.vz, 2)],
                     "aim": [round(m.yaw, 3), round(m.pitch, 3)], "hp": round(m.hp, 1), "max_hp": round(m.max_hp(cfg), 1),
                     "energy": round(m.energy, 1), "max_energy": round(s["energy"], 1), "grounded": m.grounded, "cubes": m.cubes,
                     "upgrades": list(m.upgrades), "weapons": ws, "in_zone": inz,
                     "stats": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}},
            "mechs": seen_list, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": pickups,
            "sounds": sounds, "beams": beams, "events": events,
            "zone": {"center": [round(zc[0], 2), round(zc[1], 2)], "radius": round(zr, 2), "next_center": [round(nc_[0], 2), round(nc_[1], 2)],
                     "next_radius": nr, "shrink_start": ss, "shrink_end": se, "dps": dps},
            "offer": offer, "drops": drops,
        }
        return obs

    # ================================================================ replay
    def _frame(self):
        wk = {k: i for i, k in enumerate(WK)}
        ms = []
        for m in self.mechs:
            if not m.alive and m.death_t is not None and self.t - m.death_t > 0.2:
                continue
            fl = (1 if m.grounded else 0) | (2 if (m.boosting or m.jetting) else 0) | (4 if m.charging() else 0)
            ms.append([m.id, round(m.x, 2), round(m.y, 2), round(m.z, 2), round(m.yaw, 3), round(m.pitch, 3), round(max(0, m.hp)),
                       round(m.energy), fl, m.cubes, round(m.max_hp(self.cfg)),
                       wk[m.weapons[0].kind] if m.weapons[0] else -1, wk[m.weapons[1].kind] if m.weapons[1] else -1])
        ns = [[n.id, round(n.x, 2), round(n.y, 2), round(n.z, 2), round(n.yaw, 2), round(n.hp)] for n in self.npcs if n.alive]
        ps = [[p.id, 0 if p.kind == "gun" else (1 if p.kind == "mortar" else 2), round(p.x, 2), round(p.y, 2), round(p.z, 2)] for p in self.projs]
        zc, zr = self.zone_state()[:2]
        ds = []
        for d in self.drops:
            if d["called"] and not d["taken"]:
                stt = 0 if not d["landed"] else (2 if d["holder"] is not None else 1)
                ds.append([d["id"], stt, d["holder"] if d["holder"] is not None else -1, round(d["progress"] / self.cfg["drops"]["capture_s"], 2)])
        f = {"t": self.tick, "m": ms, "n": ns, "p": ps, "z": [round(zc[0], 2), round(zc[1], 2), round(zr, 2)]}
        if ds:
            f["d"] = ds
        if self.rev:
            f["ev"] = self.rev
            self.rev = []
        self.frames.append(f)

    def replay_header(self, bots, names):
        cfg = self.cfg
        zone = [{"center": list(cfg["zone"]["start_center"]), "radius": cfg["zone"]["start_radius"], "shrink_start": 0, "shrink_end": 0}]
        for (h0, ss, se, c0, r0, c1, r1, dps) in self.zone_phases:
            zone.append({"center": [round(c1[0], 2), round(c1[1], 2)], "radius": r1, "shrink_start": ss, "shrink_end": se})
        return {
            "type": "header", "format": "mechreplay/1", "tick_hz": self.hz, "frame_every": cfg["replay"]["frame_every"], "seed": self.seed,
            "frame": "x east, y north, z up, meters (viewer/public/arena/v2/world.json)",
            "fields": {"m": ["id", "x", "y", "z", "aim_yaw", "aim_pitch", "hp", "energy", "flags", "cubes", "max_hp", "slot0", "slot1"],
                       "slot": "index into weapon_kinds, -1 = empty", "n": ["id", "x", "y", "z", "yaw", "hp"],
                       "p": ["id", "kind(0 gun,1 mortar,2 npc)", "x", "y", "z"], "z": ["cx", "cy", "r"],
                       "d": ["id", "state(0 inbound,1 landed,2 capturing,3 contested)", "holder", "progress 0..1"],
                       "flags": {"1": "grounded", "2": "boosting", "4": "laser charging"}},
            "protocol": "mechfight/1", "bots": bots,
            "mechs": [{"id": m.id, "name": names[m.id], "weapons": ["gun"], "slots": ["gun", None], "spawn": [round(v, 2) for v in m.spawn]} for m in self.mechs],
            "npcs": [{"id": n.id, "spawn": [round(n.hx, 2), round(n.hy, 2), round(n.z, 2)]} for n in self.npcs],
            "crates": [{"id": c["id"], "pos": [round(c["x"], 2), round(c["y"], 2), round(c["z"], 2)]} for c in self._crates0],
            "weapon_kinds": WK,
            "pickups": [{"id": it["id"], "kind": it["kind"], "pos": [round(it["x"], 2), round(it["y"], 2), round(it["z"], 2)]} for it in self._items0],
            "drops": [{"id": d["id"], "pos": [round(d["x"], 2), round(d["y"], 2), round(d["z"], 2)], "radius": cfg["drops"]["radius"],
                       "call_t": d["call_t"], "land_t": d["land_t"]} for d in self.drops],
            "zone": zone,
        }

    def snapshot_initial(self):
        self._crates0 = [dict(c) for c in self.crates.values()]
        self._items0 = [dict(it) for it in self.items.values()]

    def result(self):
        out = []
        for m in self.mechs:
            out.append({"id": m.id, "name": m.name, "place": m.place, "kills": m.kills,
                        "damage_dealt": round(sum(m.dmg_dealt.values())), "damage_taken": round(sum(m.dmg_taken.values())),
                        "damage_by_weapon": {k: round(v) for k, v in m.dmg_dealt.items()},
                        "taken_by_weapon": {k: round(v) for k, v in m.dmg_taken.items()},
                        "cubes_max": m.cubes_max, "upgrades": list(m.upgrades), "loadout": m.weapon_kinds(),
                        "first_weapon_t": m.first_weapon_t, "weapons_taken": m.weapons_taken,
                        "death_t": round(m.death_t, 2) if m.death_t is not None else None,
                        "killed_by": m.killed_by, "killed_with": m.killed_with, "effects": {"rear": m.rear_taken},
                        "drops_taken": m.drops_taken, "shots": dict(m.shots), "hits": dict(m.hits_made), "hits_taken": dict(m.hits_taken),
                        "stationary_s": round(m.stationary_t, 1), "alive_s": round(m.alive_t, 1)})
        return {"type": "result", "ticks": self.tick, "mechs": out}
