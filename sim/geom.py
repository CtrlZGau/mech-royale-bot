"""Static map for the simulator: terrain, prisms (walls/roofs/bridges), trees.

Everything is precomputed once per process and shared by all matches (read-only).
Units m; x east, y north, z up (protocol §5, §11).

Main queries
- ground(x, y)                      bilinear terrain height
- floor_at(x, y, z, step)           highest walkable surface under (x, y) at most `step` above z
- ceiling_at(x, y, z)               lowest prism underside above z (None if open sky)
- push_out(x, y, z, r, h, step)     move a cylinder out of walls (returns new x, y, normals)
- seg_hit(a, b)                     first fraction s in [0, 1] where segment a->b hits a prism or
                                    terrain (None if clear). Used for shots and LOS.
- sight_blocked(a, b)               seg_hit + tree trunks + dense canopy (vision only)
"""
from __future__ import annotations

import array
import json
import math
import os
import sys

CELL = 8.0
INV_CELL = 1.0 / CELL
TCELL = 8.0  # tree grid


def _area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a * 0.5


def _in_loop(x, y, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class Prism:
    __slots__ = ("i", "kind", "base", "top", "ring", "holes", "x0", "y0", "x1", "y1", "area")

    def __init__(self, i, p):
        self.i = i
        self.kind = p.get("kind", "building")
        self.base = float(p["base"])
        self.top = float(p["top"])
        r = p["ring"]
        self.ring = list(zip(r[0::2], r[1::2]))
        self.holes = [list(zip(h[0::2], h[1::2])) for h in (p.get("holes") or []) if len(h) >= 6]
        xs, ys = r[0::2], r[1::2]
        self.x0, self.y0, self.x1, self.y1 = min(xs), min(ys), max(xs), max(ys)
        self.area = abs(_area(self.ring))

    def inside2d(self, x, y):
        if x < self.x0 or x > self.x1 or y < self.y0 or y > self.y1:
            return False
        if not _in_loop(x, y, self.ring):
            return False
        for h in self.holes:
            if _in_loop(x, y, h):
                return False
        return True


class Map:
    def __init__(self, map_dir):
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        self.bounds = w["bounds"]
        self.rules = w.get("rules", {})
        t = w["terrain"]
        self.nx, self.ny = int(t["nx"]), int(t["ny"])
        self.tminx, self.tminy, self.tstep = float(t["minX"]), float(t["minY"]), float(t["step"])
        h = array.array("h")
        with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
            h.frombytes(f.read())
        if sys.byteorder != "little":
            h.byteswap()
        self.h = [v * 0.01 for v in h]  # list of floats: fastest indexing in CPython
        self.prisms = [Prism(i, p) for i, p in enumerate(w["prisms"]) if len(p.get("ring") or []) >= 6]
        self._index_prisms()
        self._index_trees(w.get("trees") or {})
        self.canopy_density = float(self.rules.get("canopyDensity", 0.35))
        self.canopy_block_t = float(self.rules.get("canopyBlockT", 0.25))
        # canopy: transmittance exp(-density * path_len) below canopyBlockT blocks sight
        self.crown_shape = "ell"
        self.trunks = True
        self.set_canopy(1.5)  # ellipsoid crowns, 1.5x: best match to live obs (90-93% of sightings, ~1-2% false)
        self._roofs = [p for p in self.prisms if p.kind == "building" and p.area > 150 and p.top - self.ground(*p.ring[0]) < 55]

    def set_canopy(self, scale):
        """canopy: transmittance exp(-density * path_len) below canopyBlockT blocks sight;
        `scale` multiplies the path length needed (calibrated against live obs, sim/README.md)."""
        self.canopy_scale = scale
        self.canopy_max_len = scale * -math.log(max(self.canopy_block_t, 1e-6)) / max(self.canopy_density, 1e-6)

    # ------------------------------------------------------------------ indexing
    def _index_prisms(self):
        self.cell_edges = {}   # (cx, cy) -> list of (ax, ay, bx, by, nx, ny, base, top, prism_i)
        self.cell_prisms = {}  # (cx, cy) -> list of Prism
        for p in self.prisms:
            loops = [(p.ring, False)] + [(h, True) for h in p.holes]
            for pts, is_hole in loops:
                a = _area(pts)
                # outward (away from solid) normal: ring CCW -> (dy, -dx); holes reversed
                sgn = 1.0 if (a > 0) != is_hole else -1.0
                n = len(pts)
                for k in range(n):
                    ax, ay = pts[k]
                    bx, by = pts[(k + 1) % n]
                    dx, dy = bx - ax, by - ay
                    L = math.hypot(dx, dy)
                    if L < 1e-6:
                        continue
                    e = (ax, ay, bx, by, sgn * dy / L, -sgn * dx / L, p.base, p.top, p.i)
                    for cx in range(int(math.floor(min(ax, bx) * INV_CELL)), int(math.floor(max(ax, bx) * INV_CELL)) + 1):
                        for cy in range(int(math.floor(min(ay, by) * INV_CELL)), int(math.floor(max(ay, by) * INV_CELL)) + 1):
                            self.cell_edges.setdefault((cx, cy), []).append(e)
            for cx in range(int(math.floor(p.x0 * INV_CELL)), int(math.floor(p.x1 * INV_CELL)) + 1):
                for cy in range(int(math.floor(p.y0 * INV_CELL)), int(math.floor(p.y1 * INV_CELL)) + 1):
                    self.cell_prisms.setdefault((cx, cy), []).append(p)
        self.by_index = {p.i: p for p in self.prisms}

    def _index_trees(self, t):
        self.trees = []
        self.tree_cells = {}
        F = t.get("fields") or []
        d = t.get("data") or []
        n = len(F)
        if not n:
            return
        col = {k: i for i, k in enumerate(F)}
        for i in range(len(d) // n):
            r = d[i * n:(i + 1) * n]
            x, y, z = r[col["x"]], r[col["y"]], r[col["z"]]
            tr = (x, y, z, z + r[col["height"]], r[col["crownR"]], z + r[col["crownBase"]], r[col["trunkR"]],
                  r[col.get("crownX", 0)], r[col.get("crownY", 1)])
            k = len(self.trees)
            self.trees.append(tr)
            R = max(tr[4], tr[6])
            for cx in range(int(math.floor((min(x, tr[7]) - R) / TCELL)), int(math.floor((max(x, tr[7]) + R) / TCELL)) + 1):
                for cy in range(int(math.floor((min(y, tr[8]) - R) / TCELL)), int(math.floor((max(y, tr[8]) + R) / TCELL)) + 1):
                    self.tree_cells.setdefault((cx, cy), []).append(k)

    # ------------------------------------------------------------------ terrain
    def ground(self, x, y):
        fx = (x - self.tminx)
        fy = (y - self.tminy)
        i = int(fx)
        j = int(fy)
        nx = self.nx
        if i < 0:
            i = 0
        elif i > nx - 2:
            i = nx - 2
        if j < 0:
            j = 0
        elif j > self.ny - 2:
            j = self.ny - 2
        tx = fx - i
        ty = fy - j
        if tx < 0.0:
            tx = 0.0
        elif tx > 1.0:
            tx = 1.0
        if ty < 0.0:
            ty = 0.0
        elif ty > 1.0:
            ty = 1.0
        h = self.h
        k = j * nx + i
        a = h[k] + (h[k + 1] - h[k]) * tx
        b = h[k + nx] + (h[k + nx + 1] - h[k + nx]) * tx
        return a + (b - a) * ty

    # ------------------------------------------------------------------ surfaces
    def prisms_at(self, x, y):
        return self.cell_prisms.get((int(math.floor(x * INV_CELL)), int(math.floor(y * INV_CELL))), ())

    def floor_at(self, x, y, z, step=1.2):
        """Highest surface under (x, y) whose height is <= z + step. Returns (height, on_prism)."""
        g = self.ground(x, y)
        best = g
        on = False
        lim = z + step
        for p in self.prisms_at(x, y):
            t = p.top
            if t > best and t <= lim and p.inside2d(x, y):
                best = t
                on = True
        return best, on

    def ceiling_at(self, x, y, z):
        """Lowest prism base strictly above z at (x, y), or None."""
        best = None
        for p in self.prisms_at(x, y):
            b = p.base
            if b > z and (best is None or b < best) and p.inside2d(x, y):
                best = b
        return best

    def solid_at(self, x, y, z):
        for p in self.prisms_at(x, y):
            if p.base <= z <= p.top and p.inside2d(x, y):
                return True
        return z < self.ground(x, y) - 0.05

    def open_ground(self, x, y, r=3.0, h=9.0):
        """Ground point with no prism within r (any height between ground and ground + h)."""
        g = self.ground(x, y)
        for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            for p in self.prisms_at(x + dx, y + dy):
                if p.base < g + h and p.top > g - 0.5 and p.inside2d(x + dx, y + dy):
                    return False
        _, _, push = self.push_out(x, y, g, r, h, 1.2)
        return not push

    def random_roof_point(self, rng, tries=40):
        for _ in range(tries):
            p = rng.choice(self._roofs)
            for _ in range(10):
                x = rng.uniform(p.x0, p.x1)
                y = rng.uniform(p.y0, p.y1)
                if p.inside2d(x, y):
                    # point must be the top surface (nothing above it)
                    f, on = self.floor_at(x, y, p.top + 0.01, 0.05)
                    if on and abs(f - p.top) < 1e-6 and self.ceiling_at(x, y, p.top + 0.1) is None:
                        return x, y, p.top
        return None

    # ------------------------------------------------------------------ walls
    def push_out(self, x, y, z, r, h, step):
        """Push a cylinder (feet z, radius r, height h) out of every wall it overlaps.
        A prism is a wall for this mech if base < z + h and top > z + step.
        Returns (x, y, list of unit push normals)."""
        lo = z + step
        hi = z + h
        normals = []
        for _ in range(4):
            best_pen = 0.0
            best = None
            seen = None
            for cx in range(int(math.floor((x - r) * INV_CELL)), int(math.floor((x + r) * INV_CELL)) + 1):
                for cy in range(int(math.floor((y - r) * INV_CELL)), int(math.floor((y + r) * INV_CELL)) + 1):
                    es = self.cell_edges.get((cx, cy))
                    if not es:
                        continue
                    for e in es:
                        if e[6] >= hi or e[7] <= lo:
                            continue
                        ax, ay, bx, by, nx, ny = e[0], e[1], e[2], e[3], e[4], e[5]
                        # quick reject on edge bbox
                        if (ax < bx and (x + r < ax or x - r > bx)) or (ax >= bx and (x + r < bx or x - r > ax)):
                            continue
                        if (ay < by and (y + r < ay or y - r > by)) or (ay >= by and (y + r < by or y - r > ay)):
                            continue
                        dx, dy = bx - ax, by - ay
                        L2 = dx * dx + dy * dy
                        u = ((x - ax) * dx + (y - ay) * dy) / L2
                        if u < 0.0:
                            u = 0.0
                        elif u > 1.0:
                            u = 1.0
                        qx, qy = ax + u * dx, ay + u * dy
                        ox, oy = x - qx, y - qy
                        d = math.sqrt(ox * ox + oy * oy)
                        side = (x - ax) * nx + (y - ay) * ny
                        if side >= 0.0:
                            if d >= r:
                                continue
                            pen = r - d
                            if d > 1e-6:
                                px, py = ox / d, oy / d
                            else:
                                px, py = nx, ny
                        else:
                            # centre is behind this edge: only meaningful if close (inside the solid)
                            if d > r:
                                continue
                            pen = r + d
                            px, py = nx, ny
                        if pen > best_pen:
                            best_pen = pen
                            best = (px, py)
            if best is None:
                break
            x += best[0] * (best_pen + 1e-4)
            y += best[1] * (best_pen + 1e-4)
            normals.append(best)
        return x, y, normals

    # ------------------------------------------------------------------ segment tests
    def _cells(self, ax, ay, bx, by):
        """Grid cells (CELL) visited by the 2D segment, in order (Amanatides-Woo DDA)."""
        cx = int(math.floor(ax * INV_CELL))
        cy = int(math.floor(ay * INV_CELL))
        ex = int(math.floor(bx * INV_CELL))
        ey = int(math.floor(by * INV_CELL))
        out = [((cx, cy), 0.0)]
        if cx == ex and cy == ey:
            return out
        dx, dy = bx - ax, by - ay
        sx = 1 if dx > 0 else -1
        sy = 1 if dy > 0 else -1
        if dx != 0:
            tdx = abs(CELL / dx)
            nxb = (cx + (1 if dx > 0 else 0)) * CELL
            tmx = (nxb - ax) / dx
        else:
            tdx = tmx = 1e30
        if dy != 0:
            tdy = abs(CELL / dy)
            nyb = (cy + (1 if dy > 0 else 0)) * CELL
            tmy = (nyb - ay) / dy
        else:
            tdy = tmy = 1e30
        n = abs(ex - cx) + abs(ey - cy)
        for _ in range(n):
            if tmx < tmy:
                te = tmx
                cx += sx
                tmx += tdx
            else:
                te = tmy
                cy += sy
                tmy += tdy
            out.append(((cx, cy), te))
        return out

    def seg_prism(self, ax, ay, az, bx, by, bz, ignore_start_solid=False, any_hit=False):
        """First fraction s where the segment crosses a prism wall (within base..top), or enters
        through a roof (endpoint inside). None if clear."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        best = None
        cells = self._cells(ax, ay, bx, by)
        zmin = az if az < bz else bz
        zmax = az if az > bz else bz
        ce = self.cell_edges
        for c, te in cells:
            if best is not None and te > best:
                break  # every later cell starts after the hit we have
            es = ce.get(c)
            if not es:
                continue
            for e in es:
                if e[6] > zmax or e[7] < zmin:
                    continue
                ex0, ey0 = e[0], e[1]
                fx, fy = e[2] - ex0, e[3] - ey0
                den = dx * fy - dy * fx
                if den == 0.0:
                    continue
                s = ((ex0 - ax) * fy - (ey0 - ay) * fx) / den
                if s < 0.0 or s > 1.0 or (best is not None and s >= best):
                    continue
                u = ((ex0 - ax) * dy - (ey0 - ay) * dx) / den
                if u < 0.0 or u > 1.0:
                    continue
                z = az + s * dz
                if e[6] <= z <= e[7]:
                    best = s
                    if any_hit:
                        return s
        # roof/floor entry: end point inside a solid
        if best is None:
            for p in self.prisms_at(bx, by):
                if p.base <= bz <= p.top and p.inside2d(bx, by):
                    if ignore_start_solid or not (p.base <= az <= p.top and p.inside2d(ax, ay)):
                        # crossing a horizontal face: find it
                        if az > p.top and dz < 0:
                            s = (p.top - az) / dz
                        elif az < p.base and dz > 0:
                            s = (p.base - az) / dz
                        else:
                            s = 1.0
                        best = max(0.0, min(1.0, s))
                        break
        return best

    def seg_terrain(self, ax, ay, az, bx, by, bz, step=3.0):
        dx, dy, dz = bx - ax, by - ay, bz - az
        L = math.sqrt(dx * dx + dy * dy)
        n = int(L / step) + 1
        g = self.ground
        prev_s = 0.0
        for k in range(1, n + 1):
            s = k / n
            z = az + dz * s
            if z < g(ax + dx * s, ay + dy * s) - 0.05:
                # refine by bisection
                lo, hi = prev_s, s
                for _ in range(5):
                    m = (lo + hi) * 0.5
                    if az + dz * m < g(ax + dx * m, ay + dy * m) - 0.05:
                        hi = m
                    else:
                        lo = m
                return hi
            prev_s = s
        return None

    def seg_hit(self, ax, ay, az, bx, by, bz):
        s1 = self.seg_prism(ax, ay, az, bx, by, bz)
        if s1 is not None:
            # terrain only matters before s1
            s2 = self.seg_terrain(ax, ay, az, ax + (bx - ax) * s1, ay + (by - ay) * s1, az + (bz - az) * s1)
            return s1 if s2 is None else s2 * s1
        return self.seg_terrain(ax, ay, az, bx, by, bz)

    def sight_blocked(self, ax, ay, az, bx, by, bz, trees=True):
        if self.seg_prism(ax, ay, az, bx, by, bz, any_hit=True) is not None:
            return True
        if self.seg_terrain(ax, ay, az, bx, by, bz, 4.0) is not None:
            return True
        if not trees or not self.trees:
            return False
        return self._trees_block(ax, ay, az, bx, by, bz)

    def _trees_block(self, ax, ay, az, bx, by, bz):
        dx, dy, dz = bx - ax, by - ay, bz - az
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            return False
        L = math.sqrt(L2)
        L3 = math.sqrt(L2 + dz * dz)
        seen = set()
        crown = 0.0
        x0, x1 = (ax, bx) if ax < bx else (bx, ax)
        y0, y1 = (ay, by) if ay < by else (by, ay)
        tc = self.tree_cells
        trees = self.trees
        # walk the tree grid along the segment (sample cells every half cell)
        n = int(L / (TCELL * 0.5)) + 1
        for k in range(n + 1):
            s = k / n
            key = (int(math.floor((ax + dx * s) / TCELL)), int(math.floor((ay + dy * s) / TCELL)))
            ids = tc.get(key)
            if not ids:
                continue
            for i in ids:
                if i in seen:
                    continue
                seen.add(i)
                tx, ty, tz, ttop, cr, cbase, trr, ccx, ccy = trees[i]
                # trunk: vertical cylinder from tz to cbase (the crown hides the rest)
                u = ((tx - ax) * dx + (ty - ay) * dy) / L2
                if self.trunks and 0.0 < u < 1.0:
                    qx, qy = ax + dx * u - tx, ay + dy * u - ty
                    if qx * qx + qy * qy < trr * trr:
                        z = az + dz * u
                        if tz <= z <= cbase:
                            return True
                if self.crown_shape == "ell":
                    # crown: ellipsoid, horizontal radius cr, vertical semi-axis (ttop - cbase) / 2
                    hz = (ttop - cbase) * 0.5
                    if hz <= 0.05:
                        continue
                    k = cr / hz
                    zc = (ttop + cbase) * 0.5
                    fx, fy, fz = ax - ccx, ay - ccy, (az - zc) * k
                    gz = dz * k
                    A = L2 + gz * gz
                    B = 2 * (fx * dx + fy * dy + fz * gz)
                    C = fx * fx + fy * fy + fz * fz - cr * cr
                    disc = B * B - 4 * A * C
                    if disc <= 0:
                        continue
                    sq = math.sqrt(disc)
                    s0 = max(0.0, (-B - sq) / (2 * A))
                    s1 = min(1.0, (-B + sq) / (2 * A))
                else:
                    # crown: vertical cylinder radius cr between cbase and ttop
                    u = ((ccx - ax) * dx + (ccy - ay) * dy) / L2
                    qx, qy = ax + dx * u - ccx, ay + dy * u - ccy
                    d2 = qx * qx + qy * qy
                    if d2 >= cr * cr:
                        continue
                    half = math.sqrt(cr * cr - d2) / L
                    s0, s1 = max(0.0, u - half), min(1.0, u + half)
                    if s1 <= s0:
                        continue
                    if dz != 0.0:
                        za, zb = (cbase - az) / dz, (ttop - az) / dz
                        if za > zb:
                            za, zb = zb, za
                        s0, s1 = max(s0, za), min(s1, zb)
                    elif not (cbase <= az <= ttop):
                        continue
                if s1 > s0:
                    crown += (s1 - s0) * L3
                    if crown > self.canopy_max_len:
                        return True
        return False
