"""Small math helpers. Pure functions, no state."""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

TAU = 2 * math.pi


def wrap(a: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    a = (a + math.pi) % TAU - math.pi
    return math.pi if a == -math.pi else a


def angdiff(a: float, b: float) -> float:
    """Signed smallest difference a - b."""
    return wrap(a - b)


def dist2d(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3d(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def yaw_to(src: Sequence[float], dst: Sequence[float]) -> float:
    return math.atan2(dst[1] - src[1], dst[0] - src[0])


def unit(x: float, y: float) -> Tuple[float, float]:
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-9 else (0.0, 0.0)


def lead_time(rel: Sequence[float], vel: Sequence[float], speed: float) -> Optional[float]:
    """Time t > 0 at which a projectile of `speed` fired from the origin meets a target at
    `rel` moving with `vel` (3D, straight line). None if it can never catch it."""
    a = vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2 - speed * speed
    b = 2 * (rel[0] * vel[0] + rel[1] * vel[1] + rel[2] * vel[2])
    c = rel[0] ** 2 + rel[1] ** 2 + rel[2] ** 2
    if abs(a) < 1e-9:
        return -c / b if b < 0 else None
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    r = math.sqrt(disc)
    ts = [t for t in ((-b - r) / (2 * a), (-b + r) / (2 * a)) if t > 0]
    return min(ts) if ts else None


def ballistic_pitch(d: float, h: float, v: float, g: float, high: bool = False) -> Optional[float]:
    """Launch pitch to hit a point `d` m away horizontally and `h` m up with speed v, gravity g."""
    if d < 1e-6:
        return None
    v2 = v * v
    disc = v2 * v2 - g * (g * d * d + 2 * h * v2)
    if disc < 0:
        return None
    r = math.sqrt(disc)
    return math.atan((v2 + r) / (g * d) if high else (v2 - r) / (g * d))


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
