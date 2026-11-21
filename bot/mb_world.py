"""Static world model built from /arena/v2: a 2 m height grid, line of sight, and a budgeted A* planner.

Hwalk[c] is the surface a mech stands on in cell c: the terrain, or the roof of a solid that starts
less than a mech height above it (so roofs, low bridges, stadium stands). LOS uses the same grid.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys
import time
from array import array
from typing import List, Optional, Tuple

CELL = 2.0
MECH_H = 9.0
SQ2 = math.sqrt(2.0)
NB = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
      (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)]
# bit masks of opposite neighbor pairs in NB: E/W, N/S, NE/SW, SE/NW
OPPOSITE = (0b11, 0b1100, (1 << 4) | (1 << 7), (1 << 5) | (1 << 6))


class World:
    def __init__(self, map_dir: str, step_up: float = 1.2, max_slope_deg: float = 50.0):
        w = json.load(open(os.path.join(map_dir, "v2", "world.json")))
        tr = w["terrain"]
        tnx, tny = int(tr["nx"]), int(tr["ny"])
        tminx, tminy, tstep = float(tr["minX"]), float(tr["minY"]), float(tr["step"])
        scale = float(tr.get("scale", 0.01))
        raw = array("h")
        with open(os.path.join(map_dir, "v2", tr.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if raw.itemsize == 2 and sys.byteorder == "big":
            raw.byteswap()
        b = w["bounds"]
        self.minx, self.miny = float(b["minX"]), float(b["minY"])
        self.maxx, self.maxy = float(b["maxX"]), float(b["maxY"])
        self.nx = int(math.ceil((self.maxx - self.minx) / CELL))
        self.ny = int(math.ceil((self.maxy - self.miny) / CELL))
        nx, ny = self.nx, self.ny
        n = nx * ny

        # ground height at cell centers (nearest terrain vertex)
        G = array("f", bytes(4 * n))
        for j in range(ny):
            yc = self.miny + (j + 0.5) * CELL
            tj = min(tny - 1, max(0, int(round((yc - tminy) / tstep))))
            row = tj * tnx
            base = j * nx
            for i in range(nx):
                xc = self.minx + (i + 0.5) * CELL
                ti = min(tnx - 1, max(0, int(round((xc - tminx) / tstep))))
                G[base + i] = raw[row + ti] * scale
        self.G = G
        H = array("f", G)
        prism = bytearray(n)

        # rasterize prisms, lowest base first, so stacked blocks build up the roof
        prisms = sorted(w.get("prisms", []), key=lambda p: p["base"])
        for p in prisms:
            pb, pt = float(p["base"]), float(p["top"])
            edges = []
            for ring in [p["ring"]] + list(p.get("holes") or []):
                m = len(ring) // 2
                for k in range(m):
                    x1, y1 = ring[2 * k], ring[2 * k + 1]
                    x2, y2 = ring[(2 * ((k + 1) % m))], ring[(2 * ((k + 1) % m)) + 1]
                    if y1 != y2:
                        edges.append((x1, y1, x2, y2))
            if not edges:
                continue
            ys = [e[1] for e in edges] + [e[3] for e in edges]
            j0 = max(0, int((min(ys) - self.miny) / CELL) - 1)
            j1 = min(ny - 1, int((max(ys) - self.miny) / CELL) + 1)
            for j in range(j0, j1 + 1):
                yc = self.miny + (j + 0.5) * CELL
                xs = []
                for x1, y1, x2, y2 in edges:
                    if (y1 <= yc < y2) or (y2 <= yc < y1):
                        xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
                if len(xs) < 2:
                    continue
                xs.sort()
                for k in range(0, len(xs) - 1, 2):
                    ia = max(0, int(math.ceil((xs[k] - self.minx) / CELL - 0.5)))
                    ib = min(nx - 1, int(math.floor((xs[k + 1] - self.minx) / CELL - 0.5)))
                    base = j * nx
                    for i in range(ia, ib + 1):
                        c = base + i
                        if pb < H[c] + MECH_H - 0.5 and pt > H[c]:
                            H[c] = pt
                            prism[c] = 1
        self.H = H
        self.prism = prism

        # per-cell traversal cost multiplier (0 = blocked). Walls = neighbor much higher.
        climb_terrain = CELL * math.tan(math.radians(max_slope_deg - 3))
        self.climb_prism = step_up + 0.3
        self.climb_terrain = climb_terrain
        cost = bytearray(n)
        for j in range(ny):
            for i in range(nx):
                c = j * nx + i
                h = H[c]
                walls = 0
                for k, (di, dj, _) in enumerate(NB):
                    ii, jj = i + di, j + dj
                    if ii < 0 or jj < 0 or ii >= nx or jj >= ny or H[jj * nx + ii] > h + 2.0:
                        walls |= 1 << k
                if not walls:
                    cost[c] = 1
                elif any((walls & m) == m for m in OPPOSITE):
                    cost[c] = 0  # walls on opposite sides: a gap narrower than a mech
                else:
                    cost[c] = 4
        # second ring: mild penalty so paths keep some distance from walls
        cost2 = bytearray(cost)
        for j in range(1, ny - 1):
            for i in range(1, nx - 1):
                c = j * nx + i
                if cost[c] == 1:
                    for di, dj, _ in NB:
                        if cost[(j + dj) * nx + i + di] in (0, 4):
                            cost2[c] = 2
                            break
        self.cost = cost2

    # ---- grid helpers ----
    def cell(self, x: float, y: float) -> Tuple[int, int]:
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        return min(self.nx - 1, max(0, i)), min(self.ny - 1, max(0, j))

    def idx(self, x: float, y: float) -> int:
        i, j = self.cell(x, y)
        return j * self.nx + i

    def center(self, c: int) -> Tuple[float, float]:
        j, i = divmod(c, self.nx)
        return self.minx + (i + 0.5) * CELL, self.miny + (j + 0.5) * CELL

    def height(self, x: float, y: float) -> float:
        return self.H[self.idx(x, y)]

    def ground(self, x: float, y: float) -> float:
        return self.G[self.idx(x, y)]

    def in_bounds(self, x: float, y: float, margin: float = 3.0) -> bool:
        return self.minx + margin < x < self.maxx - margin and self.miny + margin < y < self.maxy - margin

    def can_step(self, a: int, b: int) -> bool:
        if not self.cost[b]:
            return False
        dh = self.H[b] - self.H[a]
        if dh <= 0.0:
            return True
        lim = self.climb_prism if (self.prism[a] or self.prism[b]) else self.climb_terrain
        return dh <= lim

    def los(self, p: List[float], q: List[float], step: float = 1.5) -> bool:
        """True if the straight segment p->q (3D) clears the height grid."""
        dx, dy, dz = q[0] - p[0], q[1] - p[1], q[2] - p[2]
        d = math.hypot(dx, dy)
        k = int(d / step)
        if k < 2:
            return True
        H = self.H
        nx = self.nx
        minx, miny = self.minx, self.miny
        inv = 1.0 / k
        for s in range(1, k):
            t = s * inv
            x = p[0] + dx * t
            y = p[1] + dy * t
            i = int((x - minx) / CELL)
            j = int((y - miny) / CELL)
            if i < 0 or j < 0 or i >= nx or j >= self.ny:
                return False
            if H[j * nx + i] > p[2] + dz * t + 0.3:
                return False
        return True

    def walk_clear(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """Straight walk p->q stays on steppable, unblocked cells."""
        d = math.hypot(x1 - x0, y1 - y0)
        k = max(1, int(d / (CELL * 0.7)))
        prev = self.idx(x0, y0)
        for s in range(1, k + 1):
            t = s / k
            c = self.idx(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if c != prev:
                if not self.can_step(prev, c) or self.cost[c] >= 4:
                    return False
                prev = c
        return True

    def nearest_open(self, x: float, y: float, rmax: int = 12) -> int:
        i0, j0 = self.cell(x, y)
        c0 = j0 * self.nx + i0
        if self.cost[c0] in (1, 2) and not self.prism[c0]:
            return c0
        for r in range(1, rmax + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny:
                        c = j * self.nx + i
                        if self.cost[c] in (1, 2) and not self.prism[c]:
                            dd = di * di + dj * dj
                            if best is None or dd < best[0]:
                                best = (dd, c)
            if best:
                return best[1]
        return c0


class Planner:
    """A* that can be advanced a few ms per tick. Produces a list of world (x, y) waypoints."""

    def __init__(self, world: World):
        self.w = world
        self.reset()

    def reset(self):
        self.active = False
        self.goal = None
        self.path: List[Tuple[float, float]] = []
        self.done_goal = None

    def start(self, sx: float, sy: float, gx: float, gy: float):
        w = self.w
        self.s = w.idx(sx, sy)
        self.g = w.nearest_open(gx, gy)
        self.goal = (gx, gy)
        self.gi, self.gj = self.g % w.nx, self.g // w.nx
        self.open = [(0.0, self.s)]
        self.gc = {self.s: 0.0}
        self.par = {self.s: -1}
        self.closed = set()
        self.best = (self._h(self.s), self.s)
        self.active = True
        self.expanded = 0

    def _h(self, c: int) -> float:
        i, j = c % self.w.nx, c // self.w.nx
        dx, dy = abs(i - self.gi), abs(j - self.gj)
        return (max(dx, dy) + (SQ2 - 1) * min(dx, dy)) * 1.25

    def step(self, budget_s: float, max_expand: int = 40000) -> bool:
        """Advance; returns True when a path is ready (in self.path)."""
        if not self.active:
            return False
        w = self.w
        nx, ny = w.nx, w.ny
        cost, H, prism = w.cost, w.H, w.prism
        cp, ct = w.climb_prism, w.climb_terrain
        openl, gc, par, closed = self.open, self.gc, self.par, self.closed
        g = self.g
        t_end = time.perf_counter() + budget_s
        n = 0
        while openl:
            n += 1
            if (n & 63) == 0 and time.perf_counter() > t_end:
                return False
            _, c = heapq.heappop(openl)
            if c in closed:
                continue
            if c == g:
                self._finish(c)
                return True
            closed.add(c)
            self.expanded += 1
            if self.expanded > max_expand:
                break
            gcur = gc[c]
            hcur = H[c]
            i, j = c % nx, c // nx
            pa = prism[c]
            for di, dj, dc in NB:
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= ny:
                    continue
                b = jj * nx + ii
                cb = cost[b]
                if not cb or b in closed:
                    continue
                dh = H[b] - hcur
                if dh > 0.0 and dh > (cp if (pa or prism[b]) else ct):
                    continue
                if di and dj:  # no corner cutting
                    if not cost[j * nx + ii] or not cost[jj * nx + i]:
                        continue
                ng = gcur + dc * cb
                if ng < gc.get(b, 1e18):
                    gc[b] = ng
                    par[b] = c
                    hb = self._h(b)
                    heapq.heappush(openl, (ng + hb, b))
                    if hb < self.best[0]:
                        self.best = (hb, b)
        # no full path: go to the closest reached node
        self._finish(self.best[1])
        return True

    def _finish(self, c: int):
        cells = []
        while c != -1:
            cells.append(c)
            c = self.par[c]
        cells.reverse()
        self.path = [self.w.center(c) for c in cells[::2]] + ([self.w.center(cells[-1])] if len(cells) % 2 == 0 else [])
        if self.path and self.goal and self.w.walk_clear(self.path[-1][0], self.path[-1][1], *self.goal):
            self.path.append(self.goal)
        self.active = False
        self.done_goal = self.goal
        self.open = []
        self.gc = {}
        self.par = {}
        self.closed = set()
