"""OpenStreetMap source via the Overpass API (free, ODbL-licensed).

Per city we fetch three things:
  1. pitches tagged with the sport (`leisure=pitch` + `sport=*`),
  2. "containers" that group pitches into a venue (sports centres, stadiums, schools, parks),
  3. districts and neighbourhoods, to label each venue's location.
Pitches are then assigned to the smallest container that encloses them; pitches outside any
container are clustered by proximity into standalone venues.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import geo
from ..config import City, SportSpec
from ..http import request_json
from ..models import Pitch, Venue

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
SOURCE = "osm"
ORPHAN_CLUSTER_RADIUS_M = 60.0
NEIGHBOURHOOD_MAX_DISTANCE_M = 1500.0

SURFACES = {
    "artificial_turf": "artificial_turf",
    "artificial_grass": "artificial_turf",
    "synthetic": "artificial_turf",
    "grass": "grass",
    "sand": "sand",
    "dirt": "dirt",
    "earth": "dirt",
    "ground": "dirt",
    "compacted": "dirt",
    "fine_gravel": "dirt",
    "gravel": "dirt",
    "clay": "clay",
    "asphalt": "hard",
    "concrete": "hard",
    "paved": "hard",
    "tartan": "hard",
    "acrylic": "hard",
    "rubber": "hard",
}

# Tag -> venue_type for elements that can group pitches into a venue.
CONTAINER_KINDS = [
    ("leisure", "sports_centre", "sports_centre"),
    ("leisure", "sports_hall", "sports_centre"),
    ("leisure", "stadium", "stadium"),
    ("amenity", "school", "school"),
    ("amenity", "college", "school"),
    ("amenity", "university", "university"),
    ("leisure", "recreation_ground", "park"),
    ("landuse", "recreation_ground", "park"),
    ("leisure", "park", "park"),
]

ARABIC_RE = re.compile(r"[؀-ۿ]")


class OverpassClient:
    def __init__(self, client: httpx.Client, cache_dir: Path | None = None, cache_ttl_s: float = 24 * 3600):
        override = os.environ.get("OVERPASS_URL")
        self.endpoints = (override,) if override else DEFAULT_ENDPOINTS
        self.client = client
        self.cache_dir = cache_dir
        self.cache_ttl_s = cache_ttl_s

    def run(self, query: str) -> list[dict[str, Any]]:
        cache_file = None
        if self.cache_dir:
            key = hashlib.sha256(query.encode()).hexdigest()[:24]
            cache_file = self.cache_dir / f"overpass-{key}.json"
            if cache_file.exists() and time.time() - cache_file.stat().st_mtime < self.cache_ttl_s:
                return json.loads(cache_file.read_text())

        last_error: Exception | None = None
        for endpoint in self.endpoints:
            try:
                data = request_json(self.client, "POST", endpoint, data={"data": query}, attempts=3)
                break
            except RuntimeError as exc:
                log.warning("Overpass endpoint %s failed: %s", endpoint, exc)
                last_error = exc
        else:
            raise RuntimeError("All Overpass endpoints failed") from last_error

        if remark := data.get("remark"):
            # Overpass reports query timeouts / memory errors here with a partial result.
            raise RuntimeError(f"Overpass returned an incomplete result: {remark}")
        elements = data["elements"]
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(elements))
        return elements


def _area_header(city: City) -> str:
    return f"[out:json][timeout:240];\narea(id:{city.overpass_area_id})->.city;\n"


def pitches_query(city: City, sport: SportSpec) -> str:
    return _area_header(city) + f'nwr["leisure"="pitch"]["sport"~"{sport.osm_sport_regex}"](area.city);\nout geom;'


def containers_query(city: City) -> str:
    parts = "\n".join(f'  nwr["{k}"="{v}"](area.city);' for k, v, _ in CONTAINER_KINDS)
    return _area_header(city) + f"(\n{parts}\n);\nout geom;"


def places_query(city: City) -> str:
    return _area_header(city) + (
        '(\n  relation["boundary"="administrative"]["admin_level"~"^(9|10)$"](area.city);\n'
        '  node["place"~"^(suburb|quarter|neighbourhood)$"](area.city);\n);\nout geom;'
    )


def fetch_city(overpass: OverpassClient, city: City, sport: SportSpec) -> list[Venue]:
    log.info("[osm] %s: fetching %s pitches", city.name, sport.key)
    pitches = overpass.run(pitches_query(city, sport))
    log.info("[osm] %s: %d pitches, fetching venues and districts", city.name, len(pitches))
    containers = overpass.run(containers_query(city))
    places = overpass.run(places_query(city))
    return build_venues(city, sport, pitches, containers, places)


# --------------------------------------------------------------------------- element helpers


def element_key(el: dict[str, Any]) -> str:
    return f"{el['type']}/{el['id']}"


def _rings(el: dict[str, Any]) -> list[list[geo.LatLon]]:
    """Point lists of each way making up the element (outer + inner members for relations)."""
    if el["type"] == "node":
        return [[(el["lat"], el["lon"])]]
    if el["type"] == "way":
        return [[(g["lat"], g["lon"]) for g in el.get("geometry", []) if g]]
    return [
        [(g["lat"], g["lon"]) for g in m["geometry"] if g]
        for m in el.get("members", [])
        if m.get("type") == "way" and m.get("geometry")
    ]


def _outer_points(el: dict[str, Any]) -> list[geo.LatLon]:
    if el["type"] == "relation":
        outer = [
            (g["lat"], g["lon"])
            for m in el.get("members", [])
            if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")
            for g in m["geometry"]
            if g
        ]
        if outer:
            return outer
    return [p for ring in _rings(el) for p in ring]


def _segments(el: dict[str, Any]) -> list[geo.Segment]:
    return [(ring[i], ring[i + 1]) for ring in _rings(el) for i in range(len(ring) - 1)]


def _geojson(el: dict[str, Any]) -> dict[str, Any] | None:
    if el["type"] == "node":
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    rings = [[[lon, lat] for lat, lon in r] for r in _rings(el) if len(r) >= 4 and r[0] == r[-1]]
    if not rings:
        return None
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": rings}
    return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}


def _names(tags: dict[str, str]) -> tuple[str | None, str | None]:
    """(display name, Arabic name). Prefers French/Latin names for display."""
    name = tags.get("name")
    name_ar = tags.get("name:ar") or (name if name and ARABIC_RE.search(name) else None)
    display = tags.get("name:fr") or tags.get("name:en") or name
    return display, name_ar


def _yes_no(value: str | None) -> bool | None:
    if value is None:
        return None
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def _first(tags: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        if tags.get(k):
            return tags[k]
    return None


def _address(tags: dict[str, str]) -> str | None:
    street = tags.get("addr:street")
    if not street:
        return tags.get("addr:full")
    parts = [f"{tags['addr:housenumber']} {street}" if tags.get("addr:housenumber") else street]
    if tags.get("addr:postcode"):
        parts.append(tags["addr:postcode"])
    return ", ".join(parts)


def _matches_sport(tags: dict[str, str], sport: SportSpec) -> bool:
    return bool(re.search(sport.osm_sport_regex, tags.get("sport", "")))


# --------------------------------------------------------------------------- building venues


def make_pitch(el: dict[str, Any], sport: SportSpec) -> Pitch:
    tags = el.get("tags", {})
    points = _outer_points(el)
    lat, lon = geo.centroid(points)
    dims = geo.dimensions_m(points) if el["type"] != "node" else None
    length, width = dims if dims else (None, None)
    name, _ = _names(tags)
    indoor = _yes_no(tags.get("indoor"))
    if indoor is None:
        indoor = _yes_no(tags.get("covered"))
    return Pitch(
        id=f"{SOURCE}:{element_key(el)}",
        sport=sport.key,
        lat=round(lat, 7),
        lon=round(lon, 7),
        name=name,
        surface=SURFACES.get(tags.get("surface", ""), tags.get("surface")),
        lit=_yes_no(tags.get("lit")),
        indoor=indoor,
        length_m=length,
        width_m=width,
        format=geo.football_format(length) if sport.key == "football" else None,
        access=tags.get("access"),
        fee=tags.get("fee"),
        source_url=f"https://www.openstreetmap.org/{element_key(el)}",
        geometry=_geojson(el),
        tags=tags,
    )


@dataclass
class _Container:
    el: dict[str, Any]
    kind: str
    bbox: tuple[float, float, float, float]
    segments: list[geo.Segment]

    def contains(self, p: geo.LatLon) -> bool:
        return geo.bbox_contains(self.bbox, p) and geo.point_in_segments(p, self.segments)


def _container_kind(tags: dict[str, str]) -> str | None:
    for key, value, kind in CONTAINER_KINDS:
        if tags.get(key) == value:
            return kind
    return None


def _venue_from_tags(vid: str, city: City, lat: float, lon: float, tags: dict[str, str], kind: str) -> Venue:
    name, name_ar = _names(tags)
    return Venue(
        id=vid,
        city=city.slug,
        lat=round(lat, 7),
        lon=round(lon, 7),
        name=name,
        name_ar=name_ar,
        venue_type=kind,
        address=_address(tags),
        phone=_first(tags, "phone", "contact:phone", "contact:mobile"),
        website=_first(tags, "website", "contact:website", "contact:facebook", "contact:instagram"),
        email=_first(tags, "email", "contact:email"),
        opening_hours=tags.get("opening_hours"),
        access=tags.get("access"),
        fee=tags.get("fee"),
    )


class _Locator:
    """Labels a point with its district (admin boundary) and nearest neighbourhood."""

    def __init__(self, places: list[dict[str, Any]]):
        self.districts: list[tuple[int, str, _Container]] = []
        self.neighbourhoods: list[tuple[geo.LatLon, str]] = []
        for el in places:
            tags = el.get("tags", {})
            label = tags.get("name:fr") or tags.get("name")
            if not label:
                continue
            if el["type"] == "node":
                self.neighbourhoods.append(((el["lat"], el["lon"]), label))
            else:
                pts = _outer_points(el)
                if pts:
                    level = int(tags.get("admin_level", "0"))
                    self.districts.append((level, label, _Container(el, "district", geo.bbox_of(pts), _segments(el))))
        # Most specific (highest admin_level) first.
        self.districts.sort(key=lambda d: -d[0])

    def district(self, p: geo.LatLon) -> str | None:
        return next((label for _, label, c in self.districts if c.contains(p)), None)

    def neighbourhood(self, p: geo.LatLon) -> str | None:
        best = min(self.neighbourhoods, key=lambda n: geo.haversine_m(p, n[0]), default=None)
        if best and geo.haversine_m(p, best[0]) <= NEIGHBOURHOOD_MAX_DISTANCE_M:
            return best[1]
        return None


def build_venues(
    city: City,
    sport: SportSpec,
    pitch_elements: list[dict[str, Any]],
    container_elements: list[dict[str, Any]],
    place_elements: list[dict[str, Any]],
) -> list[Venue]:
    containers: list[_Container] = []
    standalone: list[dict[str, Any]] = []  # sport-tagged centres mapped as a single point
    for el in container_elements:
        tags = el.get("tags", {})
        kind = _container_kind(tags)
        if not kind:
            continue
        if el["type"] == "node":
            if _matches_sport(tags, sport):
                standalone.append(el)
            continue
        pts = _outer_points(el)
        if len(pts) >= 3:
            containers.append(_Container(el, kind, geo.bbox_of(pts), _segments(el)))
    # Smallest first, so a pitch goes to the sports centre rather than the park around it.
    containers.sort(key=lambda c: geo.bbox_area(c.bbox))

    by_container: dict[str, tuple[_Container, list[Pitch]]] = {}
    orphans: list[tuple[Pitch, dict[str, Any]]] = []
    for el in pitch_elements:
        if not el.get("tags"):
            continue
        pitch = make_pitch(el, sport)
        p = (pitch.lat, pitch.lon)
        container = next((c for c in containers if c.contains(p)), None)
        if container:
            by_container.setdefault(element_key(container.el), (container, []))[1].append(pitch)
        else:
            orphans.append((pitch, el))

    venues: list[Venue] = []
    for key, (container, pitches) in by_container.items():
        lat, lon = geo.centroid(_outer_points(container.el))
        tags = container.el.get("tags", {})
        venue = _venue_from_tags(f"{SOURCE}:{key}", city, lat, lon, tags, container.kind)
        venue.pitches = pitches
        venue.source_records.append((SOURCE, key, container.el.get("tags", {})))
        venues.append(venue)

    # Sport-tagged containers with no mapped pitch inside are still venues (e.g. a 5-a-side complex).
    for container in containers:
        key = element_key(container.el)
        if key not in by_container and _matches_sport(container.el.get("tags", {}), sport):
            standalone.append(container.el)
    for el in standalone:
        lat, lon = geo.centroid(_outer_points(el))
        tags = el.get("tags", {})
        venue = _venue_from_tags(f"{SOURCE}:{element_key(el)}", city, lat, lon, tags, _container_kind(tags) or "sports_centre")
        venue.source_records.append((SOURCE, element_key(el), tags))
        venues.append(venue)

    venues.extend(_cluster_orphans(city, orphans))

    locator = _Locator(place_elements)
    for v in venues:
        v.sports.add(sport.key)
        for pitch in v.pitches:
            v.source_records.append((SOURCE, pitch.id.removeprefix(f"{SOURCE}:"), pitch.tags))
        v.district = locator.district((v.lat, v.lon))
        v.neighbourhood = locator.neighbourhood((v.lat, v.lon))
    return venues


def _cluster_orphans(city: City, orphans: list[tuple[Pitch, dict[str, Any]]]) -> list[Venue]:
    """Union-find: pitches within ORPHAN_CLUSTER_RADIUS_M of each other form one venue."""
    parent = list(range(len(orphans)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(orphans)):
        for j in range(i + 1, len(orphans)):
            a, b = orphans[i][0], orphans[j][0]
            if geo.haversine_m((a.lat, a.lon), (b.lat, b.lon)) <= ORPHAN_CLUSTER_RADIUS_M:
                parent[find(i)] = find(j)

    groups: dict[int, list[Pitch]] = {}
    for i, (pitch, _) in enumerate(orphans):
        groups.setdefault(find(i), []).append(pitch)

    venues = []
    for pitches in groups.values():
        pitches.sort(key=lambda p: p.id)
        lat, lon = geo.centroid([(p.lat, p.lon) for p in pitches])
        # Venue-level info lives on the pitches here, so take the first non-empty value.
        merged: dict[str, str] = {}
        for p in pitches:
            for k, val in p.tags.items():
                merged.setdefault(k, val)
        venue = _venue_from_tags(pitches[0].id, city, lat, lon, merged, "pitch")
        venue.pitches = pitches
        venues.append(venue)
    return venues
