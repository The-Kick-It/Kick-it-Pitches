"""Small, dependency-free geometry helpers. Coordinates are (lat, lon) in degrees."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]
Segment = tuple[LatLon, LatLon]


def haversine_m(a: LatLon, b: LatLon) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def centroid(points: list[LatLon]) -> LatLon:
    """Vertex average, ignoring the closing vertex of a closed ring."""
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _to_local_m(points: list[LatLon]) -> list[tuple[float, float]]:
    """Equirectangular projection around the first point; accurate enough at pitch scale."""
    lat0 = math.radians(points[0][0])
    k = math.pi / 180 * EARTH_RADIUS_M
    return [((lon - points[0][1]) * k * math.cos(lat0), (lat - points[0][0]) * k) for lat, lon in points]


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def dimensions_m(points: list[LatLon]) -> tuple[float, float] | None:
    """(length, width) of the minimum-area bounding rectangle of a pitch outline."""
    hull = _convex_hull(_to_local_m(points))
    if len(hull) < 3:
        return None
    best: tuple[float, float, float] | None = None
    for i in range(len(hull)):
        (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % len(hull)]
        edge = math.hypot(x2 - x1, y2 - y1)
        if edge == 0:
            continue
        ux, uy = (x2 - x1) / edge, (y2 - y1) / edge
        us = [x * ux + y * uy for x, y in hull]
        vs = [-x * uy + y * ux for x, y in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0]:
            best = (w * h, w, h)
    if best is None:
        return None
    _, w, h = best
    return (round(max(w, h), 1), round(min(w, h), 1))


def football_format(length_m: float | None) -> str | None:
    """Rough match format from pitch length (FIFA / common Moroccan 'mini-foot' sizes)."""
    if length_m is None or length_m < 15:
        return None
    if length_m < 50:
        return "5v5"
    if length_m < 72:
        return "7v7"
    if length_m < 90:
        return "9v9"
    if length_m <= 130:
        return "11v11"
    return None  # implausible: probably a mis-drawn polygon


def point_in_segments(point: LatLon, segments: list[Segment]) -> bool:
    """Even-odd ray casting over an *unordered* set of boundary segments.

    Works for multipolygon relations without stitching their member ways into rings:
    every boundary edge (outer or inner) toggles parity exactly once per crossing.
    """
    y, x = point
    inside = False
    for (y1, x1), (y2, x2) in segments:
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def bbox_of(points: list[LatLon]) -> tuple[float, float, float, float]:
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats), min(lons), max(lats), max(lons))


def bbox_contains(bbox: tuple[float, float, float, float], p: LatLon) -> bool:
    s, w, n, e = bbox
    return s <= p[0] <= n and w <= p[1] <= e


def bbox_area(bbox: tuple[float, float, float, float]) -> float:
    s, w, n, e = bbox
    return (n - s) * (e - w)


def tile_bbox(bbox: tuple[float, float, float, float], rows: int, cols: int) -> list[tuple[float, float, float, float]]:
    s, w, n, e = bbox
    dlat, dlon = (n - s) / rows, (e - w) / cols
    return [
        (s + r * dlat, w + c * dlon, s + (r + 1) * dlat, w + (c + 1) * dlon)
        for r in range(rows)
        for c in range(cols)
    ]
