"""Pure geodesic helpers. No I/O.

Coordinates are ``(lat, lon)`` tuples in decimal degrees; distances are meters.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Sequence

LatLon = tuple[float, float]
Path = list[LatLon]

EARTH_R = 6_371_008.8  # mean Earth radius, meters


class GeoError(ValueError):
    """Raised for paths/coordinates the engine refuses to handle."""


def valid_latlon(lat: float, lon: float) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
    )


def haversine(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: LatLon, b: LatLon) -> float:
    """Initial great-circle bearing from a to b, degrees in [0, 360)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _wrap_lon(lon: float) -> float:
    return (lon + 540.0) % 360.0 - 180.0


def destination(p: LatLon, bearing_deg: float, dist_m: float) -> LatLon:
    """Point reached by travelling ``dist_m`` from ``p`` on an initial bearing."""
    lat1, lon1 = math.radians(p[0]), math.radians(p[1])
    brg = math.radians(bearing_deg)
    d = dist_m / EARTH_R
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brg))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2)
    )
    return (math.degrees(lat2), _wrap_lon(math.degrees(lon2)))


def interpolate(a: LatLon, b: LatLon, t: float) -> LatLon:
    """Linear interpolation in lat/lon. Fine for short (<1 km) segments."""
    if t <= 0:
        return (a[0], a[1])
    if t >= 1:
        return (b[0], b[1])
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def cumulative_distances(path: Sequence[LatLon]) -> list[float]:
    """Prefix sums of segment lengths; ``cum[0] == 0`` and ``cum[-1]`` is the total length."""
    if not path:
        return []
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + haversine(path[i - 1], path[i]))
    return cum


def point_at_distance(path: Sequence[LatLon], cum: Sequence[float], d: float) -> tuple[LatLon, int]:
    """Position ``d`` meters along ``path`` and the index of the segment it lies on."""
    if not path:
        raise GeoError("empty path")
    if len(path) == 1:
        return (path[0][0], path[0][1]), 0
    if d <= 0:
        return (path[0][0], path[0][1]), 0
    if d >= cum[-1]:
        return (path[-1][0], path[-1][1]), len(path) - 2
    i = bisect_right(cum, d) - 1
    i = max(0, min(i, len(path) - 2))
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg <= 0 else (d - cum[i]) / seg
    return interpolate(path[i], path[i + 1], t), i


def dedupe(path: Sequence[LatLon], min_gap_m: float = 0.01) -> Path:
    """Drop consecutive points closer than ``min_gap_m``."""
    out: Path = []
    for p in path:
        p = (float(p[0]), float(p[1]))
        if not out or haversine(out[-1], p) >= min_gap_m:
            out.append(p)
    return out


def crosses_antimeridian(path: Sequence[LatLon]) -> bool:
    return any(abs(path[i][1] - path[i - 1][1]) > 180.0 for i in range(1, len(path)))


def densify(path: Sequence[LatLon], max_seg_m: float = 50.0) -> Path:
    """Split long segments along the great circle so no segment exceeds ``max_seg_m``."""
    if max_seg_m <= 0:
        raise GeoError("max_seg_m must be positive")
    if len(path) < 2:
        return [tuple(p) for p in path]  # type: ignore[misc]
    out: Path = [(path[0][0], path[0][1])]
    for a, b in zip(path, path[1:]):
        d = haversine(a, b)
        n = int(math.ceil(d / max_seg_m)) if d > max_seg_m else 1
        if n > 1:
            brg = bearing(a, b)
            for k in range(1, n):
                out.append(destination(a, brg, d * k / n))
        out.append((b[0], b[1]))
    return out


def _to_xy(origin: LatLon, p: LatLon) -> tuple[float, float]:
    """Local equirectangular projection to meters (x east, y north)."""
    x = math.radians(p[1] - origin[1]) * EARTH_R * math.cos(math.radians(origin[0]))
    y = math.radians(p[0] - origin[0]) * EARTH_R
    return x, y


def _perp_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(path: Sequence[LatLon], tolerance_m: float = 3.0) -> Path:
    """Douglas–Peucker simplification (iterative, so huge routes don't hit recursion limits)."""
    n = len(path)
    if n <= 2:
        return [(p[0], p[1]) for p in path]
    origin = path[0]
    xy = [_to_xy(origin, p) for p in path]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        best, idx = -1.0, -1
        for i in range(s + 1, e):
            d = _perp_dist(xy[i], xy[s], xy[e])
            if d > best:
                best, idx = d, i
        if idx != -1 and best > tolerance_m:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [(path[i][0], path[i][1]) for i in range(n) if keep[i]]


def offset_meters(p: LatLon, dn: float, de: float) -> LatLon:
    """Move ``p`` by ``dn`` meters north and ``de`` meters east."""
    lat = p[0] + math.degrees(dn / EARTH_R)
    coslat = max(1e-9, math.cos(math.radians(p[0])))
    lon = p[1] + math.degrees(de / (EARTH_R * coslat))
    return (max(-90.0, min(90.0, lat)), _wrap_lon(lon))


def path_length(path: Sequence[LatLon]) -> float:
    c = cumulative_distances(path)
    return c[-1] if c else 0.0
