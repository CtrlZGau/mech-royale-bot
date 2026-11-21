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
