"""Google Places API (New) source — optional, needs GOOGLE_PLACES_API_KEY.

OSM is good at *where* pitches are and what they look like, but in Morocco it rarely has
names, phone numbers or opening hours. Google Places fills that gap for commercial venues
(the 5-a-side complexes players actually book).

Text Search returns at most 60 results per query, so each city's bounding box is split into
tiles and every query is run per tile. Requesting contact/rating fields bills at the
"Text Search Enterprise" SKU — check the free monthly quota before large runs.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any

import httpx

from .. import geo
from ..config import City, SportSpec
from ..http import request_json
from ..models import Venue

log = logging.getLogger(__name__)

SOURCE = "google"
ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
MAX_PAGES = 3  # API hard limit: 3 pages x 20 results
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.photos.name",
        "nextPageToken",
    ]
)
# Places whose type is one of these are kept even if the name has no sport keyword.
SPORT_TYPES = {"sports_complex", "athletic_field", "stadium", "sports_club", "sports_activity_location", "arena"}
# Results with these primary types are almost always noise (shops selling football kits, cafés showing matches...).
EXCLUDED_PRIMARY_TYPES = {
    "store", "sporting_goods_store", "clothing_store", "shoe_store", "cafe", "restaurant",
    "coffee_shop", "bar", "hotel", "lodging", "gym", "fitness_center",
}


def api_key() -> str | None:
    return os.environ.get("GOOGLE_PLACES_API_KEY") or None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def is_relevant(place: dict[str, Any], sport: SportSpec) -> bool:
    if place.get("businessStatus") == "CLOSED_PERMANENTLY":
        return False
    if place.get("primaryType") in EXCLUDED_PRIMARY_TYPES:
        return False
    name = normalize(place.get("displayName", {}).get("text", ""))
    if any(k in name for k in sport.name_keywords):
        return True
    return bool(SPORT_TYPES & set(place.get("types", [])))


def _search(client: httpx.Client, key: str, query: str, tile: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    s, w, n, e = tile
    body: dict[str, Any] = {
        "textQuery": query,
        "languageCode": "fr",
        "regionCode": "MA",
        "pageSize": 20,
        "locationRestriction": {"rectangle": {"low": {"latitude": s, "longitude": w}, "high": {"latitude": n, "longitude": e}}},
    }
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK}
    places: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        data = request_json(client, "POST", ENDPOINT, json=body, headers=headers)
        places.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
    return places


def place_to_venue(place: dict[str, Any], city: City, sport: SportSpec) -> Venue:
    loc = place["location"]
    name = place.get("displayName", {}).get("text")
    hours = place.get("regularOpeningHours", {}).get("weekdayDescriptions")
    venue = Venue(
        id=f"{SOURCE}:{place['id']}",
        city=city.slug,
        lat=round(loc["latitude"], 7),
        lon=round(loc["longitude"], 7),
        sports={sport.key},
        name=name,
        name_ar=name if name and re.search(r"[؀-ۿ]", name) else None,
        venue_type="sports_centre",
        address=place.get("formattedAddress"),
        phone=place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber"),
        website=place.get("websiteUri"),
        opening_hours="; ".join(hours) if hours else None,
        rating=place.get("rating"),
        rating_count=place.get("userRatingCount"),
        google_maps_url=place.get("googleMapsUri"),
        business_status=place.get("businessStatus"),
        photo_refs=[p["name"] for p in place.get("photos", [])[:5]],
    )
    venue.source_records.append((SOURCE, place["id"], place))
    return venue


def fetch_city(client: httpx.Client, key: str, city: City, sport: SportSpec, grid: int = 3) -> list[Venue]:
    seen: dict[str, dict[str, Any]] = {}
    tiles = geo.tile_bbox(city.bbox, grid, grid)
    for query in sport.google_queries:
        for tile in tiles:
            for place in _search(client, key, query, tile):
                seen.setdefault(place["id"], place)
    kept = [p for p in seen.values() if "location" in p and is_relevant(p, sport)]
    log.info("[google] %s: %d unique places, %d relevant to %s", city.name, len(seen), len(kept), sport.key)
    return [place_to_venue(p, city, sport) for p in kept]
