"""Static map: terrain heights (and raw prisms for later use). Loaded once at hello.

protocol.md §11: v2/terrain.i16 is little-endian int16 centimeters, nx*ny, row-major from
(minX, minY), step 1 m. Index j*nx + i is ground height at x = minX + i*step, y = minY + j*step.
"""
from __future__ import annotations

import array
import json
import os
import sys
from typing import Any, Dict, List


class World:
    def __init__(self, map_dir: str) -> None:
        self.ok = False
        self.prisms: List[Dict[str, Any]] = []
        self.bounds: Dict[str, Any] = {}
        try:
            with open(os.path.join(map_dir, "v2", "world.json")) as f:
                w = json.load(f)
            t = w["terrain"]
            self.nx, self.ny = int(t["nx"]), int(t["ny"])
            self.min_x, self.min_y, self.step = float(t["minX"]), float(t["minY"]), float(t["step"])
            self.prisms = w.get("prisms", [])
            self.bounds = w.get("bounds", {})
            h = array.array("h")
            with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
                h.frombytes(f.read())
            if sys.byteorder != "little":
                h.byteswap()
            self.h = h
            self.ok = len(h) == self.nx * self.ny
        except Exception as e:  # never die on map problems: play without the map
            print(f"world: map load failed: {e!r}", file=sys.stderr, flush=True)

        self._index_prisms()

    # ------------------------------------------------------------------ prisms: LOS and cover
    CELL = 16.0

    def _index_prisms(self) -> None:
        """Bounding boxes + a uniform grid index so segment tests only touch nearby prisms."""
        self.boxes: List[tuple] = []
        self.grid: Dict[tuple, List[int]] = {}
        for p in self.prisms:
            r = p.get("ring") or []
            if len(r) < 6:
                continue
            pts = list(zip(r[0::2], r[1::2]))
            xs, ys = r[0::2], r[1::2]
            k = len(self.boxes)
            self.boxes.append((min(xs), min(ys), max(xs), max(ys), float(p["base"]), float(p["top"]), pts))
            c = self.CELL
            for cx in range(int(min(xs) // c), int(max(xs) // c) + 1):
                for cy in range(int(min(ys) // c), int(max(ys) // c) + 1):
                    self.grid.setdefault((cx, cy), []).append(k)

    def _near(self, x0: float, y0: float, x1: float, y1: float):
        c = self.CELL
        out = set()
        for cx in range(int(min(x0, x1) // c), int(max(x0, x1) // c) + 1):
            for cy in range(int(min(y0, y1) // c), int(max(y0, y1) // c) + 1):
                out.update(self.grid.get((cx, cy), ()))
        return out

    @staticmethod
    def _in_poly(x: float, y: float, pts) -> bool:
        inside = False
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
                inside = not inside
            j = i
        return inside

    def blocked(self, a, b) -> bool:
        """True if the 3D segment a->b passes through a building/stadium/bridge prism.
        (Trees and holes ignored: trees never stop shots, and only trunks block sight.)"""
        ax, ay, az = a
        bx, by, bz = b
        dx, dy, dz = bx - ax, by - ay, bz - az
        for k in self._near(ax, ay, bx, by):
            x0, y0, x1, y1, base, top, pts = self.boxes[k]
            if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
                continue
            if max(az, bz) < base or min(az, bz) > top:
                continue
            n = len(pts)
            for i in range(n):
                px, py = pts[i]
                qx, qy = pts[(i + 1) % n]
                ex, ey = qx - px, qy - py
                den = dx * ey - dy * ex
                if abs(den) < 1e-12:
                    continue
                s = ((px - ax) * ey - (py - ay) * ex) / den  # along a->b
                u = ((px - ax) * dy - (py - ay) * dx) / den  # along edge
                if 0 <= s <= 1 and 0 <= u <= 1:
                    z = az + s * dz
                    if base <= z <= top:
                        return True
        return False

    def solid_at(self, x: float, y: float, z: float) -> bool:
        """Is (x, y, z) inside a prism?"""
        for k in self._near(x, y, x, y):
            x0, y0, x1, y1, base, top, pts = self.boxes[k]
            if x0 <= x <= x1 and y0 <= y <= y1 and base <= z <= top and self._in_poly(x, y, pts):
                return True
        return False

    def find_cover(self, me, threat_eye, max_r: float, zone=None, step_rings=(6.0, 10.0, 16.0, 24.0, 34.0)):
        """Nearest reachable ground point within max_r that a viewer at threat_eye cannot see
        (both our centre and head hidden). Returns (x, y, z) or None. zone = (cx, cy, r) keeps it inside."""
        import math
        best, best_d = None, 1e9
        mx, my, mz = me
        for r in step_rings:
            if r > max_r:
                break
            for k in range(16):
                ang = k * math.pi / 8
                x, y = mx + r * math.cos(ang), my + r * math.sin(ang)
                if zone is not None and math.hypot(x - zone[0], y - zone[1]) > zone[2] - 12:
                    continue
                g = self.ground(x, y)
                if abs(g - mz) > 3.0 or self.solid_at(x, y, g + 1.0):
                    continue
                if self.blocked((mx, my, mz + 2.0), (x, y, g + 2.0)):
                    continue  # can't walk straight there
                if not self.blocked(threat_eye, (x, y, g + 4.5)) or not self.blocked(threat_eye, (x, y, g + 8.5)):
                    continue
                if r < best_d:
                    best, best_d = (x, y, g), r
            if best is not None:
                return best
        return best

    def ground(self, x: float, y: float) -> float:
        """Bilinear terrain height (m). 0 if the map is unavailable."""
        if not self.ok:
            return 0.0
        fx = (x - self.min_x) / self.step
        fy = (y - self.min_y) / self.step
        i = min(max(int(fx), 0), self.nx - 2)
        j = min(max(int(fy), 0), self.ny - 2)
        tx = min(max(fx - i, 0.0), 1.0)
        ty = min(max(fy - j, 0.0), 1.0)
        n, h = self.nx, self.h
        a = h[j * n + i] * (1 - tx) + h[j * n + i + 1] * tx
        b = h[(j + 1) * n + i] * (1 - tx) + h[(j + 1) * n + i + 1] * tx
        return (a * (1 - ty) + b * ty) / 100.0
