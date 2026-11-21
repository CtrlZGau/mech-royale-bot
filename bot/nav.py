"""Grid navigation: walkability grid from prisms + terrain slope, capped A*, path smoothing.

Built once at hello (~0.3 s). Cells are CELL m. A cell is blocked if a prism occupies the space a
mech needs there (prism top above ground + step_up and base below ground + mech height), or the
terrain is too steep. No dilation (validated: 0.5% of real mech positions fall in blocked cells).
"""
from __future__ import annotations

import heapq
import math
import time
from typing import List, Optional, Tuple

CELL = 4.0
MECH_H = 9.5
STEP_UP = 1.2
MAX_SLOPE_DEG = 50.0


class Nav:
    def __init__(self, world, cell: float = CELL) -> None:
        t0 = time.perf_counter()
        self.ok = False
        self.cell = cell
        b = world.bounds or {}
        if not world.ok or not b:
            return
        self.min_x, self.min_y = float(b["minX"]), float(b["minY"])
        self.nx = int((float(b["maxX"]) - self.min_x) / cell) + 1
        self.ny = int((float(b["maxY"]) - self.min_y) / cell) + 1
        nx, ny = self.nx, self.ny
        g = [0.0] * (nx * ny)
        for j in range(ny):
            yc = self.min_y + (j + 0.5) * cell
            for i in range(nx):
                g[j * nx + i] = world.ground(self.min_x + (i + 0.5) * cell, yc)
        self.gz = g
        blk = bytearray(nx * ny)
        # Prisms: scanline fill with even-odd rule over ring + holes.
        for p in world.prisms:
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            base, top = float(p["base"]), float(p["top"])
            loops = [ring] + [h for h in (p.get("holes") or []) if len(h) >= 6]
            edges = []
            ys = ring[1::2]
            for lp in loops:
                pts = list(zip(lp[0::2], lp[1::2]))
                for k in range(len(pts)):
                    edges.append((pts[k], pts[(k + 1) % len(pts)]))
            j0 = max(0, int((min(ys) - self.min_y) / cell))
            j1 = min(ny - 1, int((max(ys) - self.min_y) / cell) + 1)
            for j in range(j0, j1 + 1):
                yc = self.min_y + (j + 0.5) * cell
                xs = []
                for (ax, ay), (bx, by) in edges:
                    if (ay <= yc < by) or (by <= yc < ay):
                        xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
                xs.sort()
                for k in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil((xs[k] - self.min_x) / cell - 0.5)))
                    i1 = min(nx - 1, int(math.floor((xs[k + 1] - self.min_x) / cell - 0.5)))
                    for i in range(i0, i1 + 1):
                        gz = g[j * nx + i]
                        if top <= gz + STEP_UP or base >= gz + MECH_H:
                            continue  # low deck / step, or overhead bridge
                        blk[j * nx + i] = 1
        # Steep terrain between neighbouring cells.
        lim = cell * math.tan(math.radians(MAX_SLOPE_DEG))
        for j in range(ny):
            for i in range(nx):
                k = j * nx + i
                if i + 1 < nx and abs(g[k + 1] - g[k]) > lim:
                    blk[k] = blk[k + 1] = 1
                if j + 1 < ny and abs(g[k + nx] - g[k]) > lim:
                    blk[k] = blk[k + nx] = 1
        # No dilation: a full-cell dilation blocked 13% of positions real mechs walk on (walls slide
        # anyway). Block the map border.
        d = bytearray(blk)
        for i in range(nx):
            d[i] = d[(ny - 1) * nx + i] = 1
        for j in range(ny):
            d[j * nx] = d[j * nx + nx - 1] = 1
        self.blk = d
        self.ok = True
        self.build_s = time.perf_counter() - t0

    # ------------------------------------------------------------------ helpers
    def idx(self, x: float, y: float) -> Tuple[int, int]:
        return (min(max(int((x - self.min_x) / self.cell), 0), self.nx - 1),
                min(max(int((y - self.min_y) / self.cell), 0), self.ny - 1))

    def center(self, i: int, j: int) -> Tuple[float, float]:
        return (self.min_x + (i + 0.5) * self.cell, self.min_y + (j + 0.5) * self.cell)

    def free(self, x: float, y: float) -> bool:
        if not self.ok:
            return True
        i, j = self.idx(x, y)
        return not self.blk[j * self.nx + i]

    def nearest_free(self, i: int, j: int, r: int = 8) -> Tuple[int, int]:
        if not self.blk[j * self.nx + i]:
            return i, j
        for rr in range(1, r + 1):
            for dj in range(-rr, rr + 1):
                for di in (-rr, rr) if abs(dj) != rr else range(-rr, rr + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < self.nx and 0 <= jj < self.ny and not self.blk[jj * self.nx + ii]:
                        return ii, jj
        return i, j

    def line_free(self, a, b) -> bool:
        """Straight walk a->b stays on free cells (sampled every half cell)."""
        if not self.ok:
            return True
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(d / (self.cell * 0.5)))
        for k in range(1, n + 1):
            s = k / n
            if not self.free(a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s):
                return False
        return True

    # ------------------------------------------------------------------ A*
    def path(self, a, b, max_expand: int = 2000) -> List[Tuple[float, float]]:
        """World-space waypoints from a to b (or toward the closest reachable cell). [] if no map."""
        if not self.ok:
            return [(b[0], b[1])]
        nx, blk = self.nx, self.blk
        si, sj = self.nearest_free(*self.idx(a[0], a[1]))
        gi, gj = self.nearest_free(*self.idx(b[0], b[1]))
        start, goal = sj * nx + si, gj * nx + gi
        if start == goal:
            return [(b[0], b[1])]

        def h(k):
            i, j = k % nx, k // nx
            dx, dy = abs(i - gi), abs(j - gj)
            return (dx + dy) + (1.4142 - 2) * min(dx, dy)

        openh = [(h(start), 0.0, start)]
        gs = {start: 0.0}
        came = {}
        best, best_h = start, h(start)
        n = 0
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
        while openh and n < max_expand:
            f, gk, k = heapq.heappop(openh)
            if gk > gs.get(k, 1e18):
                continue
            n += 1
            if k == goal:
                best = k
                break
            hk = f - gk
            if hk < best_h:
                best, best_h = k, hk
            i, j = k % nx, k // nx
            for di, dj, c in nbrs:
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= self.ny:
                    continue
                kk = jj * nx + ii
                if blk[kk]:
                    continue
                if di and dj and (blk[j * nx + ii] or blk[jj * nx + i]):
                    continue  # no corner cutting
                ng = gk + c
                if ng < gs.get(kk, 1e18):
                    gs[kk] = ng
                    came[kk] = k
                    heapq.heappush(openh, (ng + h(kk), ng, kk))
        cells = [best]
        while cells[-1] in came:
            cells.append(came[cells[-1]])
        cells.reverse()
        pts = [self.center(k % nx, k // nx) for k in cells[1:]]
        if best == goal:
            pts.append((b[0], b[1]))
        return self.smooth((a[0], a[1]), pts)

    def smooth(self, a, pts):
        """String-pulling: keep only waypoints needed to stay on free cells."""
        if not pts:
            return pts
        out = []
        cur = a
        k = 0
        while k < len(pts):
            far = k
            for m in range(min(len(pts) - 1, k + 30), k, -1):
                if self.line_free(cur, pts[m]):
                    far = m
                    break
            out.append(pts[far])
            cur = pts[far]
            k = far + 1
        return out
