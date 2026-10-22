"""Build a self-contained HTML map of the venues in the database.

Artifact pages and offline files can't load map tiles, so the page draws its own light basemap
(city boundary, arrondissements, main roads, coastline, neighbourhood names) from OSM data
fetched here and embedded alongside the venues.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

from PIL import Image

from . import geo
from .config import CITIES, City
from .sources import osm

log = logging.getLogger(__name__)

ROAD_CLASSES = {"motorway": 1, "trunk": 1, "primary": 2, "secondary": 3}
SIMPLIFY_M = 8.0
DATA_PLACEHOLDER = "/*__KICKIT_DATA__*/null"
EMBED_PHOTO_WIDTH = 640


def _simplify(points: list[geo.LatLon], tolerance_m: float = SIMPLIFY_M) -> list[geo.LatLon]:
    """Douglas-Peucker on a local metric projection."""
    if len(points) < 3:
        return points
    lat0 = math.radians(points[0][0])
    k = math.pi / 180 * geo.EARTH_RADIUS_M
    xy = [(lon * k * math.cos(lat0), lat * k) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-9
        best, best_d = -1, tolerance_m
        for m in range(i + 1, j):
            d = abs(dy * (xy[m][0] - x1) - dx * (xy[m][1] - y1)) / norm
            if d > best_d:
                best, best_d = m, d
        if best >= 0:
            keep[best] = True
            stack += [(i, best), (best, j)]
    return [p for p, kept in zip(points, keep) if kept]


def _flat(points: list[geo.LatLon], digits: int = 5) -> list[float]:
    return [round(c, digits) for p in points for c in p]


def basemap_query(city: City) -> str:
    s, w, n, e = city.bbox
    pad = 0.05
    bbox = f"{s - pad},{w - pad},{n + pad},{e + pad}"
    roads = "|".join(ROAD_CLASSES)
    return (
        f"[out:json][timeout:240];\n"
        f"(\n  relation({city.osm_relation_id});\n"
        f'  way["highway"~"^({roads})$"]({bbox});\n'
        f'  way["natural"="coastline"]({bbox});\n);\nout geom;'
    )


def build_basemap(overpass: osm.OverpassClient, city: City) -> dict[str, Any]:
    elements = overpass.run(basemap_query(city))
    places = overpass.run(osm.places_query(city))
    boundary: list[list[float]] = []
    roads: list[list[Any]] = []
    coast: list[list[float]] = []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "relation":
            boundary = [
                _flat(_simplify([(g["lat"], g["lon"]) for g in m["geometry"]], 20))
                for m in el.get("members", [])
                if m.get("type") == "way" and m.get("role") == "outer" and m.get("geometry")
            ]
        elif tags.get("natural") == "coastline":
            coast.append(_flat(_simplify([(g["lat"], g["lon"]) for g in el["geometry"]], 15)))
        elif "highway" in tags:
            roads.append([ROAD_CLASSES[tags["highway"]], _flat(_simplify([(g["lat"], g["lon"]) for g in el["geometry"]]))])

    districts, labels = [], []
    for el in places:
        tags = el.get("tags", {})
        name = tags.get("name:fr") or tags.get("name")
        if not name:
            continue
        if el["type"] == "node":
            labels.append([name, round(el["lat"], 5), round(el["lon"], 5), tags.get("place")])
        elif tags.get("admin_level") == "10":
            districts.append(
                [_flat(_simplify([(g["lat"], g["lon"]) for g in m["geometry"]], 15))
                 for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry")]
            )
    s, w, n, e = city.bbox
    return {
        "slug": city.slug, "name": city.name, "bbox": [s, w, n, e],
        "boundary": boundary, "districts": districts, "roads": roads, "coast": coast, "labels": labels,
    }


def _data_uri(path: str) -> str | None:
    """Small JPEG for embedding: the page can't load images from other sites."""
    try:
        img = Image.open(path)
    except OSError:
        return None
    img.thumbnail((EMBED_PHOTO_WIDTH, EMBED_PHOTO_WIDTH))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def photos_payload(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for m in conn.execute("SELECT * FROM media WHERE kind = 'photo' ORDER BY venue_id, local_path IS NULL, ref"):
        src = _data_uri(m["local_path"]) if m["local_path"] else None
        out.setdefault(m["venue_id"], []).append({
            "src": src, "page": m["page_url"], "author": m["author"], "license": m["license"],
            "license_url": m["license_url"], "source": m["source"],
        })
    return out


def venues_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    sports: dict[str, list[str]] = {}
    for r in conn.execute("SELECT venue_id, sport FROM venue_sports ORDER BY sport"):
        sports.setdefault(r["venue_id"], []).append(r["sport"])
    pitches: dict[str, list[dict[str, Any]]] = {}
    for p in conn.execute("SELECT * FROM pitches ORDER BY id"):
        outline = None
        if p["geometry"]:
            g = json.loads(p["geometry"])
            ring = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0] if g["type"] == "MultiPolygon" else None
            if ring:
                outline = _flat([(lat, lon) for lon, lat in ring], 6)
        pitches.setdefault(p["venue_id"], []).append({
            "id": p["id"], "sport": p["sport"], "name": p["name"], "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
            "surface": p["surface"], "lit": p["lit"], "indoor": p["indoor"], "length": p["length_m"], "width": p["width_m"],
            "format": p["format"], "access": p["access"], "url": p["source_url"], "outline": outline,
        })
    photos = photos_payload(conn)
    out = []
    for v in conn.execute("SELECT * FROM venues ORDER BY city, id"):
        out.append({
            "id": v["id"], "city": v["city"], "name": v["name"], "name_ar": v["name_ar"], "type": v["venue_type"],
            "lat": round(v["lat"], 6), "lon": round(v["lon"], 6), "district": v["district"],
            "neighbourhood": v["neighbourhood"], "address": v["address"], "phone": v["phone"], "website": v["website"],
            "hours": v["opening_hours"], "access": v["access"], "fee": v["fee"], "rating": v["rating"],
            "rating_count": v["rating_count"], "gmaps": v["google_maps_url"], "sources": json.loads(v["sources"]),
            "sports": sports.get(v["id"], []), "pitches": pitches.get(v["id"], []), "updated": v["last_seen_at"],
            "photos": photos.get(v["id"], []),
        })
    return out


def write_map(conn: sqlite3.Connection, overpass: osm.OverpassClient, out: Path) -> Path:
    cities = []
    for city in CITIES.values():
        log.info("[map] basemap for %s", city.name)
        cities.append(build_basemap(overpass, city))
    data = {"cities": cities, "venues": venues_payload(conn)}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = resources.files("kickit_crawler").joinpath("web/map.html").read_text(encoding="utf-8")
    if DATA_PLACEHOLDER not in template:
        raise RuntimeError("map template is missing the data placeholder")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template.replace(DATA_PLACEHOLDER, payload), encoding="utf-8")
    return out
