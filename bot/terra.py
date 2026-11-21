"""Terra: an exact world model for movement and shooting decisions.

Layers (1 m fine raster, from the terrain grid and the prism footprints):
  floor      terrain height
  crest      top of the solid stack standing on the floor (solids starting <= 1.5 m above the stack join it)
  tread      walking height (crest, or a low bridge deck)
  hindered   stack rises > 1.2 m above the floor, or a low overhead solid
  sheets     solids floating above the stack (overhangs, high bridges)
Route grid (2 m cells, positioned at minX + 2 i): a cell is sealed when it is a roof, or a mech of radius 2.6
cannot stand there without a wall inside it; margin = metres to the nearest sealed cell (<= 20);
patches = 4-connected walkable areas (hub = the largest).
Line of fire: segment against prism faces/caps (via 16 m bins) and terrain (ray march with an 8 m block-max skip).
Route search: A* over the route grid with a wall-margin cost, advanced in time slices.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import struct
import sys
import time
import zlib
from array import array

BIN = 16.0          # spatial bins for prism lookups
STEP_UP = 1.2
REACH_R = 2.6       # radius used for standing tests on the route grid
PITCH = 2.0         # route grid spacing (m)
HOPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
CACHE_VERSION = 3


def inside_poly(lines, x, y):
    """Even-odd test against a flat list of segments [x0, y0, x1, y1, ...]."""
    odd = False
    for k in range(0, len(lines), 4):
        ax, ay, bx, by = lines[k], lines[k + 1], lines[k + 2], lines[k + 3]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            odd = not odd
    return odd


class Block:
    __slots__ = ("kind", "base", "top", "lines", "x0", "y0", "x1", "y1")

    def __init__(self, kind, base, top, lines):
        self.kind, self.base, self.top, self.lines = kind, base, top, lines
        xs, ys = lines[0::2], lines[1::2]
        self.x0, self.x1, self.y0, self.y1 = min(xs), max(xs), min(ys), max(ys)


class Terra:
    def __init__(self, map_dir: str, cache_path: str | None = None):
        t0 = time.perf_counter()
        wpath = os.path.join(map_dir, "v2", "world.json")
        raw = open(wpath, "rb").read()
        self.digest = hashlib.sha1(raw).hexdigest()
        wj = json.loads(raw)
        tr = wj["terrain"]
        self.ox, self.oy = float(tr["minX"]), float(tr["minY"])
        self.fw, self.fh = int(tr["nx"]), int(tr["ny"])
        b = wj["bounds"]
        self.ex0, self.ex1, self.ey0, self.ey1 = float(b["minX"]), float(b["maxX"]), float(b["minY"]), float(b["maxY"])
        hgt = array("h")
        with open(os.path.join(map_dir, "v2", tr.get("file", "terrain.i16")), "rb") as f:
            hgt.frombytes(f.read())
        if sys.byteorder != "little":
            hgt.byteswap()
        n = self.fw * self.fh
        self.floor = array("f", (v * 0.01 for v in hgt[:n]))
        self.blocks = []
        for p in wj.get("prisms", []):
            lines = []
            for ring in [p["ring"]] + list(p.get("holes") or []):
                m = len(ring) // 2
                for a in range(m):
                    c = (a + 1) % m
                    lines += [ring[2 * a], ring[2 * a + 1], ring[2 * c], ring[2 * c + 1]]
            if lines:
                self.blocks.append(Block(p["kind"], float(p["base"]), float(p["top"]), lines))
        self._bins()
        self._land_caps()
        self._layers(wj.get("prisms", []))
        loaded = False
        if cache_path and os.path.exists(cache_path):
            loaded = self._load_grid(cache_path)
        if not loaded:
            self._lay_grid()
        self.hub_link = None
        self.hub_toward(0)   # build the hub link table now (startup budget), not mid-match
        self.build_s = time.perf_counter() - t0
        self.grid_from_cache = loaded
        self._route_job = None

    # ------------------------------------------------------------------ bins
    def _bins(self):
        self.bx0, self.by0 = self.ex0 - BIN, self.ey0 - BIN
        self.bnx = int(math.ceil((self.ex1 - self.ex0) / BIN)) + 3
        self.bny = int(math.ceil((self.ey1 - self.ey0) / BIN)) + 3
        self.bin_blocks = [[] for _ in range(self.bnx * self.bny)]
        for i, bk in enumerate(self.blocks):
            for c in self.each_bin(bk.x0, bk.y0, bk.x1, bk.y1):
                self.bin_blocks[c].append(i)

    def bin_of(self, x, y):
        i = int(math.floor((x - self.bx0) / BIN))
        j = int(math.floor((y - self.by0) / BIN))
        if i < 0 or j < 0 or i >= self.bnx or j >= self.bny:
            return -1
        return j * self.bnx + i

    def each_bin(self, x0, y0, x1, y1):
        i0 = max(0, int(math.floor((x0 - self.bx0) / BIN)))
        i1 = min(self.bnx - 1, int(math.floor((x1 - self.bx0) / BIN)))
        j0 = max(0, int(math.floor((y0 - self.by0) / BIN)))
        j1 = min(self.bny - 1, int(math.floor((y1 - self.by0) / BIN)))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                yield j * self.bnx + i

    def trace_bins(self, x0, y0, x1, y1):
        """Bins crossed by the segment, in order (2D DDA)."""
        dx, dy = x1 - x0, y1 - y0
        ci = int(math.floor((x0 - self.bx0) / BIN))
        cj = int(math.floor((y0 - self.by0) / BIN))
        ei = int(math.floor((x1 - self.bx0) / BIN))
        ej = int(math.floor((y1 - self.by0) / BIN))
        si = 1 if dx > 0 else -1
        sj = 1 if dy > 0 else -1
        inf = float("inf")
        tdx = tdy = tmx = tmy = inf
        if abs(dx) > 1e-9:
            tdx = BIN / abs(dx)
            tmx = ((ci + 1) * BIN + self.bx0 - x0) / abs(dx) if dx > 0 else (x0 - (ci * BIN + self.bx0)) / abs(dx)
        if abs(dy) > 1e-9:
            tdy = BIN / abs(dy)
            tmy = ((cj + 1) * BIN + self.by0 - y0) / abs(dy) if dy > 0 else (y0 - (cj * BIN + self.by0)) / abs(dy)
        for _ in range(4096):
            if 0 <= ci < self.bnx and 0 <= cj < self.bny:
                yield cj * self.bnx + ci
            if ci == ei and cj == ej:
                return
            if tmx < tmy:
                t = tmx
                tmx += tdx
                ci += si
            else:
                t = tmy
                tmy += tdy
                cj += sj
            if t > 1:
                return

    # ------------------------------------------------------------------ terrain
    def _land_caps(self):
        fw, fh = self.fw, self.fh
        self.cnx, self.cny = (fw + 7) // 8, (fh + 7) // 8
        bm = [-1e30] * (self.cnx * self.cny)
        fl = self.floor
        for j in range(fh):
            row = j * fw
            cb = (j // 8) * self.cnx
            for i in range(fw):
                v = fl[row + i]
                k = cb + i // 8
                if v > bm[k]:
                    bm[k] = v
        cap = [0.0] * len(bm)
        for j in range(self.cny):
            for i in range(self.cnx):
                v = bm[j * self.cnx + i]
                if i + 1 < self.cnx:
                    v = max(v, bm[j * self.cnx + i + 1])
                if j + 1 < self.cny:
                    v = max(v, bm[(j + 1) * self.cnx + i])
                if i + 1 < self.cnx and j + 1 < self.cny:
                    v = max(v, bm[(j + 1) * self.cnx + i + 1])
                cap[j * self.cnx + i] = v
        self.land_cap = cap

    def floor_z(self, x, y):
        fx = min(max(x - self.ox, 0.0), self.fw - 1 - 1e-6)
        fy = min(max(y - self.oy, 0.0), self.fh - 1 - 1e-6)
        i, j = int(fx), int(fy)
        tx, ty = fx - i, fy - j
        fw, fl = self.fw, self.floor
        a, b = fl[j * fw + i], fl[j * fw + i + 1]
        c, d = fl[(j + 1) * fw + i], fl[(j + 1) * fw + i + 1]
        return (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty

    def floor_slope(self, x, y):
        return ((self.floor_z(x + 1, y) - self.floor_z(x - 1, y)) / 2,
                (self.floor_z(x, y + 1) - self.floor_z(x, y - 1)) / 2)

    # ------------------------------------------------------------------ fine layers
    def _layers(self, prisms):
        fw, fh, ox, oy = self.fw, self.fh, self.ox, self.oy
        spans = {}
        for p in prisms:
            rings = [p["ring"]] + list(p.get("holes") or [])
            ys = p["ring"][1::2]
            j0 = int(math.ceil(min(ys) - oy))
            j1 = int(math.floor(max(ys) - oy))
            piece = (float(p["base"]), float(p["top"]), p["kind"] == "bridge")
            for j in range(max(j0, 0), min(j1, fh - 1) + 1):
                y = oy + j + 1e-4
                xs = []
                for r in rings:
                    m = len(r) // 2
                    for a in range(m):
                        c = (a + 1) % m
                        ax_, ay_, bx_, by_ = r[2 * a], r[2 * a + 1], r[2 * c], r[2 * c + 1]
                        if (ay_ <= y) != (by_ <= y):
                            xs.append(ax_ + (y - ay_) / (by_ - ay_) * (bx_ - ax_))
                xs.sort()
                for k in range(0, len(xs) - 1, 2):
                    i0 = int(math.ceil(xs[k] - ox))
                    i1 = int(math.floor(xs[k + 1] - ox))
                    for i in range(max(i0, 0), min(i1, fw - 1) + 1):
                        spans.setdefault(j * fw + i, []).append(piece)
        n = fw * fh
        self.crest = array("f", self.floor)
        self.tread = array("f", self.floor)
        self.hindered = bytearray(n)
        self.overhang = bytearray(n)
        self.sheets = {}
        for idx, pieces in spans.items():
            pieces.sort(key=lambda q: q[0])
            g = self.floor[idx]
            top = g
            deck = -1e9
            low = False
            for base, ptop, bridge in pieces:
                if ptop <= top:
                    continue
                if base <= top + 1.5:
                    top = ptop
                    continue
                self.sheets.setdefault(idx, []).append((base, ptop))
                if base - top < 10.5:
                    if bridge and ptop - g < 6.5:
                        deck = ptop
                    else:
                        low = True
            self.crest[idx] = top
            self.tread[idx] = max(top, deck) if deck > top else top
            self.overhang[idx] = 1 if low else 0
            self.hindered[idx] = 1 if (top - g > 1.2 or low) else 0

    def fine_idx(self, x, y):
        i = min(max(int(round(x - self.ox)), 0), self.fw - 1)
        j = min(max(int(round(y - self.oy)), 0), self.fh - 1)
        return j * self.fw + i

    def inside(self, x, y):
        return self.ex0 < x < self.ex1 and self.ey0 < y < self.ey1

    def pile(self, x, y, deck):
        """(height, block index) of the solid stack under (x, y); deck = also step onto low bridge decks."""
        g = self.floor_z(x, y)
        z, which = g, -1
        c = self.bin_of(x, y)
        if c < 0:
            return z, which
        ids = self.bin_blocks[c]
        for _ in range(8):
            moved = False
            for bi in ids:
                bk = self.blocks[bi]
                if bk.top <= z or x < bk.x0 or x > bk.x1 or y < bk.y0 or y > bk.y1:
                    continue
                if not (bk.base <= z + 1.5 or (deck and bk.kind == "bridge" and bk.top - g < 6.5)):
                    continue
                if inside_poly(bk.lines, x, y):
                    z, which = bk.top, bi
                    moved = True
            if not moved:
                break
        return z, which

    def tread_z(self, x, y):
        return self.pile(x, y, True)[0]

    def crest_z(self, x, y):
        return self.pile(x, y, False)[0]

    def is_rock(self, x, y, z):
        if z < self.floor_z(x, y):
            return True
        c = self.bin_of(x, y)
        if c < 0:
            return False
        for bi in self.bin_blocks[c]:
            bk = self.blocks[bi]
            if bk.base < z < bk.top and bk.x0 <= x <= bk.x1 and bk.y0 <= y <= bk.y1 and inside_poly(bk.lines, x, y):
                return True
        return False

    def rock_cached(self, x, y, z):
        idx = self.fine_idx(x, y)
        if z < self.crest[idx] - 0.05:
            return True
        for base, top in self.sheets.get(idx, ()):
            if base < z < top:
                return True
        return False

    def wall_near(self, x, y, z, r, h):
        lo, hi = z + STEP_UP, z + h
        r2 = r * r
        for c in self.each_bin(x - r, y - r, x + r, y + r):
            for bi in self.bin_blocks[c]:
                bk = self.blocks[bi]
                if bk.top <= lo or bk.base >= hi or x + r < bk.x0 or x - r > bk.x1 or y + r < bk.y0 or y - r > bk.y1:
                    continue
                e = bk.lines
                for k in range(0, len(e), 4):
                    x0, y0 = e[k], e[k + 1]
                    ux, uy = e[k + 2] - x0, e[k + 3] - y0
                    l2 = ux * ux + uy * uy
                    tt = 0.0
                    if l2 > 1e-9:
                        tt = max(0.0, min(1.0, ((x - x0) * ux + (y - y0) * uy) / l2))
                    mx, my = x0 + ux * tt - x, y0 + uy * tt - y
                    if mx * mx + my * my < r2:
                        return True
                if inside_poly(e, x, y):
                    return True
        return False

    def fits(self, x, y, z, r):
        if x < self.ex0 + r or x > self.ex1 - r or y < self.ey0 + r or y > self.ey1 - r:
            return False
        return not self.wall_near(x, y, z, r, 9)

    # ------------------------------------------------------------------ route grid
    def _lay_grid(self):
        self.cols, self.rows = (self.fw + 1) // 2, (self.fh + 1) // 2
        n = self.cols * self.rows
        sealed = bytearray(n)
        stand_z = array("f", bytes(4 * n))
        for c in range(n):
            x, y = self.cell_xy(c)
            z, which = self.pile(x, y, True)
            stand_z[c] = z
            roof = which >= 0 and self.blocks[which].kind != "bridge" and z - self.floor_z(x, y) > 1.5
            sealed[c] = 1 if (roof or not self.fits(x, y, z, REACH_R)) else 0
        self.sealed, self.stand_z = sealed, stand_z
        self._derive_grid()

    def _derive_grid(self):
        cols, rows = self.cols, self.rows
        n = cols * rows
        sealed = self.sealed
        margin = array("f", [1e9] * n)
        q = []
        for c in range(n):
            if sealed[c]:
                margin[c] = 0.0
                q.append(c)
        k = 0
        while k < len(q):
            c = q[k]
            k += 1
            d = margin[c] + PITCH
            if d > 20:
                continue
            ci, cj = c % cols, c // cols
            for di, dj in HOPS[:4]:
                i, j = ci + di, cj + dj
                if 0 <= i < cols and 0 <= j < rows:
                    nb = j * cols + i
                    if margin[nb] > d:
                        margin[nb] = d
                        q.append(nb)
        for c in range(n):
            if margin[c] > 20:
                margin[c] = 20.0
        self.margin = margin
        patch = array("i", [0] * n)
        label, best_label, best_size = 0, 0, 0
        for c0 in range(n):
            if sealed[c0] or patch[c0]:
                continue
            label += 1
            patch[c0] = label
            q = [c0]
            k = 0
            while k < len(q):
                c = q[k]
                k += 1
                ci, cj = c % cols, c // cols
                for di, dj in HOPS[:4]:
                    i, j = ci + di, cj + dj
                    if 0 <= i < cols and 0 <= j < rows:
                        nb = j * cols + i
                        if not patch[nb] and self.can_hop(c, nb, False) and self.can_hop(nb, c, False):
                            patch[nb] = label
                            q.append(nb)
            if len(q) > best_size:
                best_size, best_label = len(q), label
        self.patch, self.hub = patch, best_label

    def save_grid(self, path):
        blob = zlib.compress(bytes(self.sealed) + self.stand_z.tobytes(), 9)
        head = json.dumps({"v": CACHE_VERSION, "digest": self.digest, "cols": self.cols, "rows": self.rows}).encode()
        with open(path, "wb") as f:
            f.write(struct.pack("<I", len(head)) + head + blob)

    def _load_grid(self, path):
        try:
            data = open(path, "rb").read()
            hl = struct.unpack("<I", data[:4])[0]
            head = json.loads(data[4:4 + hl])
            if head.get("v") != CACHE_VERSION or head.get("digest") != self.digest:
                return False
            self.cols, self.rows = head["cols"], head["rows"]
            n = self.cols * self.rows
            body = zlib.decompress(data[4 + hl:])
            self.sealed = bytearray(body[:n])
            self.stand_z = array("f")
            self.stand_z.frombytes(body[n:n + 4 * n])
            self._derive_grid()
            return True
        except Exception:
            return False

    def cell_at(self, x, y):
        ci = min(max(int(round((x - self.ox) / PITCH)), 0), self.cols - 1)
        cj = min(max(int(round((y - self.oy) / PITCH)), 0), self.rows - 1)
        return cj * self.cols + ci

    def cell_xy(self, c):
        return self.ox + (c % self.cols) * PITCH, self.oy + (c // self.cols) * PITCH

    def open_at(self, x, y):
        return not self.sealed[self.cell_at(x, y)]

    def open_near(self, c):
        if not self.sealed[c]:
            return c
        cols, rows = self.cols, self.rows
        ci, cj = c % cols, c // cols
        best, best_d = -1, 1 << 30
        for r in range(1, 21):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i, j = ci + di, cj + dj
                    if 0 <= i < cols and 0 <= j < rows and not self.sealed[j * cols + i]:
                        d = di * di + dj * dj
                        if d < best_d:
                            best, best_d = j * cols + i, d
            if best >= 0:
                return best
        return -1

    def can_hop(self, a, b, diag):
        if self.sealed[b]:
            return False
        return self.stand_z[b] - self.stand_z[a] <= (3.1 if diag else 2.2)

    def stroll_ok(self, x0, y0, x1, y1):
        dx, dy = x1 - x0, y1 - y0
        n = int(math.hypot(dx, dy) / 1.0) + 1
        prev = self.cell_at(x0, y0)
        for k in range(1, n + 1):
            t = k / n
            c = self.cell_at(x0 + dx * t, y0 + dy * t)
            if c == prev:
                continue
            if self.sealed[c] or self.stand_z[c] - self.stand_z[prev] > 3.1:
                return False
            prev = c
        return True

    def on_hub(self, x, y):
        c = self.cell_at(x, y)
        return not self.sealed[c] and self.patch[c] == self.hub

    def stray(self, x, y):
        c = self.open_near(self.cell_at(x, y))
        return c < 0 or self.patch[c] != self.hub

    def hub_toward(self, c):
        if self.hub_link is None:
            cols, rows = self.cols, self.rows
            n = cols * rows
            link = array("i", [-1] * n)
            depth = bytearray(n)
            q = []
            for i in range(n):
                if not self.sealed[i] and self.patch[i] == self.hub:
                    link[i] = i
                    q.append(i)
            k = 0
            while k < len(q):
                c0 = q[k]
                k += 1
                if depth[c0] >= 20:
                    continue
                ci, cj = c0 % cols, c0 // cols
                for di, dj in HOPS:
                    i, j = ci + di, cj + dj
                    if 0 <= i < cols and 0 <= j < rows:
                        nb = j * cols + i
                        if link[nb] < 0:
                            link[nb] = link[c0]
                            depth[nb] = depth[c0] + 1
                            q.append(nb)
            self.hub_link = link
        return self.hub_link[c]

    def breakout(self, x, y, budget_s=0.012):
        """(takeoff xy, landing xy) to get from an isolated pocket back to the hub, or None.
        Stops early after budget_s (keeps the best plan found so far)."""
        t_end = time.perf_counter() + budget_s
        s = self.open_near(self.cell_at(x, y))
        if s < 0:
            return None
        cols, rows = self.cols, self.rows
        steps = {s: 0}
        q = [s]
        best, best_s = -1, float("inf")
        k = 0
        while k < len(q) and k < 60000:
            if (k & 255) == 0 and k and time.perf_counter() > t_end:
                break
            c = q[k]
            k += 1
            ci, cj = c % cols, c // cols
            m = self.hub_toward(c)
            if m >= 0:
                cx, cy = self.cell_xy(c)
                mx, my = self.cell_xy(m)
                sc = steps[c] * PITCH + 4 * math.hypot(mx - cx, my - cy)
                if sc < best_s:
                    best, best_s = c, sc
            for hi, (di, dj) in enumerate(HOPS):
                i, j = ci + di, cj + dj
                if not (0 <= i < cols and 0 <= j < rows):
                    continue
                nb = j * cols + i
                if nb in steps or not self.can_hop(c, nb, hi >= 4):
                    continue
                if hi >= 4 and (self.sealed[cj * cols + i] or self.sealed[j * cols + ci]):
                    continue
                steps[nb] = steps[c] + 1
                q.append(nb)
        if best < 0:
            return None
        lx, ly = self.cell_xy(best)
        mx, my = self.cell_xy(self.hub_toward(best))
        d = math.hypot(mx - lx, my - ly)
        if d > 0.1:
            ex, ey = mx + (mx - lx) / d * 5, my + (my - ly) / d * 5
            if self.inside(ex, ey) and self.on_hub(ex, ey):
                mx, my = ex, ey
        return (lx, ly), (mx, my)

    def airiness(self, x, y):
        z = self.tread_z(x, y) + 7
        total = 0.0
        for k in range(24):
            a = k * math.pi / 12
            ca, sa = math.cos(a), math.sin(a)
            d = 2.5
            while d < 200:
                px, py = x + ca * d, y + sa * d
                if not self.inside(px, py) or self.rock_cached(px, py, z):
                    break
                d += 2.5
            total += d
        return total / (24 * 200)

    # ------------------------------------------------------------------ routing (A*, time sliced)
    def route_begin(self, sx, sy, gx, gy, max_expand=22000):
        s = self.open_near(self.cell_at(sx, sy))
        g = self.open_near(self.cell_at(gx, gy))
        if s < 0 or g < 0:
            self._route_job = {"done": True, "path": None}
            return
        if s == g:
            self._route_job = {"done": True, "path": [s]}
            return
        cols = self.cols
        gi, gj = g % cols, g // cols

        def h(c):
            dx, dy = abs(c % cols - gi), abs(c // cols - gj)
            mn, mx = min(dx, dy), max(dx, dy)
            return (mx + 0.4142 * mn) * PITCH * 1.15

        self._route_job = {"done": False, "s": s, "g": g, "h": h, "cost": {s: 0.0}, "came": {s: -1}, "shut": set(),
                           "heap": [(h(s), s)], "best": s, "best_h": h(s), "n": 0, "max": max_expand, "path": None}

    def route_step(self, budget_s):
        """Advance the current search; returns True when finished (path in route_result())."""
        job = self._route_job
        if job is None or job["done"]:
            return True
        cols, rows = self.cols, self.rows
        sealed, margin = self.sealed, self.margin
        heap, cost, came, shut = job["heap"], job["cost"], job["came"], job["shut"]
        h, g = job["h"], job["g"]
        t_end = time.perf_counter() + budget_s
        found = False
        while heap and job["n"] < job["max"]:
            if (job["n"] & 63) == 0 and time.perf_counter() > t_end:
                return False
            _, c = heapq.heappop(heap)
            if c in shut:
                continue
            shut.add(c)
            job["n"] += 1
            if c == g:
                found = True
                job["best"] = c
                break
            hc = h(c)
            if hc < job["best_h"]:
                job["best"], job["best_h"] = c, hc
            ci, cj = c % cols, c // cols
            for hi, (di, dj) in enumerate(HOPS):
                i, j = ci + di, cj + dj
                if not (0 <= i < cols and 0 <= j < rows):
                    continue
                nb = j * cols + i
                diag = hi >= 4
                if nb in shut or not self.can_hop(c, nb, diag):
                    continue
                step = PITCH
                if diag:
                    if sealed[cj * cols + i] or sealed[j * cols + ci]:
                        continue
                    step *= 1.4142
                cl = margin[nb]
                if cl < 4:
                    step *= 1 + (4 - cl) * 0.15
                ng = cost[c] + step
                if nb not in cost or ng < cost[nb]:
                    cost[nb] = ng
                    came[nb] = c
                    heapq.heappush(heap, (ng + h(nb), nb))
        path = []
        c = job["best"]
        while c >= 0:
            path.append(c)
            if c == job["s"]:
                break
            c = came.get(c, -1)
        path.reverse()
        job["path"], job["done"], job["found"] = path, True, found
        return True

    def route_result(self):
        job = self._route_job
        return job["path"] if job and job["done"] else None

    def route_busy(self):
        return self._route_job is not None and not self._route_job["done"]

    # ------------------------------------------------------------------ line of fire
    def hit_block(self, bi, x, y, z, dx, dy, dz, tmax):
        bk = self.blocks[bi]
        t0, t1 = 0.0, tmax
        for o, d, lo, hi in ((x, dx, bk.x0, bk.x1), (y, dy, bk.y0, bk.y1), (z, dz, bk.base, bk.top)):
            if abs(d) < 1e-9:
                if not (lo <= o <= hi):
                    return False
                continue
            a, b = (lo - o) / d, (hi - o) / d
            if a > b:
                a, b = b, a
            t0, t1 = max(t0, a), min(t1, b)
            if t0 > t1:
                return False
        e = bk.lines
        if bk.base <= z <= bk.top and inside_poly(e, x, y):
            return True
        best = tmax
        for k in range(0, len(e), 4):
            ex, ey = e[k + 2] - e[k], e[k + 3] - e[k + 1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-9:
                continue
            px, py = e[k] - x, e[k + 1] - y
            t = (px * ey - py * ex) / den
            s = (px * dy - py * dx) / den
            if t < 0 or t >= best or s < 0 or s > 1:
                continue
            hz = z + dz * t
            if hz < bk.base or hz > bk.top:
                continue
            return True
        if abs(dz) > 1e-9:
            for cz, nz in ((bk.top, 1), (bk.base, -1)):
                t = (cz - z) / dz
                if t < 0 or t >= best or (nz > 0 and dz > 0) or (nz < 0 and dz < 0):
                    continue
                if inside_poly(e, x + dx * t, y + dy * t):
                    return True
        return False

    def hit_land(self, x, y, z, x1, y1, z1, tmax):
        dx, dy, dz = x1 - x, y1 - y, z1 - z
        hl = math.hypot(dx, dy)
        if z - self.floor_z(x, y) < 0:
            return True
        step = 0.5 / hl if hl > 1e-9 else 1.0
        t = 0.0
        it = 0
        cnx, cny, caps = self.cnx, self.cny, self.land_cap
        while t < tmax:
            it += 1
            if it > 100000:
                return False
            px, py = x + dx * t, y + dy * t
            bi = int(math.floor((px - self.ox) / 8.0))
            bj = int(math.floor((py - self.oy) / 8.0))
            inb = 0 <= bi < cnx and 0 <= bj < cny
            tb = tmax
            if hl > 1e-9:
                ex, ey = self.ox + bi * 8.0, self.oy + bj * 8.0
                nx = ny = float("inf")
                if dx > 1e-9:
                    nx = (ex + 8.0 - px) / dx
                elif dx < -1e-9:
                    nx = (ex - px) / dx
                if dy > 1e-9:
                    ny = (ey + 8.0 - py) / dy
                elif dy < -1e-9:
                    ny = (ey - py) / dy
                tb = min(t + max(0.0, min(nx, ny)) + 1e-6, tmax)
            if inb and min(z + dz * t, z + dz * tb) > caps[bj * cnx + bi]:
                t = tb
                continue
            s = min(t + step, tmax)
            if z + dz * s - self.floor_z(x + dx * s, y + dy * s) < 0:
                return True
            if s >= tmax:
                break
            t = s
        return False

    def fire_lane(self, a, b):
        """True when the straight segment a->b meets no prism and no terrain (trees never stop shots)."""
        x, y, z = a
        dx, dy, dz = b[0] - x, b[1] - y, b[2] - z
        tried = set()
        for c in self.trace_bins(x, y, b[0], b[1]):
            for bi in self.bin_blocks[c]:
                if bi in tried:
                    continue
                tried.add(bi)
                if self.hit_block(bi, x, y, z, dx, dy, dz, 1.0):
                    return False
        return not self.hit_land(x, y, z, b[0], b[1], b[2], 1.0)


def build_cache(map_dir, out_path):
    t = Terra(map_dir, cache_path=None)
    t.save_grid(out_path)
    return t


if __name__ == "__main__":
    md = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kit", "arena")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "terra_grid.bin")
    tt = build_cache(md, out)
    print(f"grid built in {tt.build_s:.1f}s -> {out} ({os.path.getsize(out)} bytes)")
