"""Merge venues coming from different sources that describe the same physical place."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import geo
from .models import Venue
from .sources.google_places import normalize

# Same place if names are similar within this distance...
NAMED_MATCH_RADIUS_M = 150.0
NAME_SIMILARITY = 0.6
# ...or, if the OSM venue has no name, if it is this close.
UNNAMED_MATCH_RADIUS_M = 60.0

GENERIC_WORDS = re.compile(
    r"\b(terrain|terrains|stade|complexe|sportif|sportive|sport|sports|club|foot|football|"
    r"de|du|des|la|le|les|l|d|city|soccer|five|arena|espace|centre|center|mini)\b"
)


def _core_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", " ", GENERIC_WORDS.sub(" ", re.sub(r"[^\w\s]", " ", normalize(name)))).strip()


def name_similarity(a: str | None, b: str | None) -> float:
    ca, cb = _core_name(a), _core_name(b)
    if not ca or not cb:
        return 0.0
    if ca in cb or cb in ca:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


def _absorb(base: Venue, other: Venue) -> None:
    """Fill gaps in `base` with `other`'s data. OSM geometry/pitches win; Google contact data wins."""
    base.sports |= other.sports
    for attr in ("name", "name_ar", "address", "district", "neighbourhood", "email"):
        if getattr(base, attr) is None:
            setattr(base, attr, getattr(other, attr))
    # Google is more current for commercial info.
    for attr in ("phone", "website", "opening_hours", "rating", "rating_count", "google_maps_url", "business_status"):
        if getattr(other, attr) is not None:
            setattr(base, attr, getattr(other, attr))
    base.photo_refs = base.photo_refs or other.photo_refs
    base.pitches.extend(other.pitches)
    base.source_records.extend(other.source_records)


def merge_into(base_venues: list[Venue], extra: list[Venue]) -> list[Venue]:
    """Match each `extra` venue to the best base venue nearby; unmatched ones are appended."""
    result = list(base_venues)
    claimed: set[str] = set()
    for venue in extra:
        best: tuple[float, Venue] | None = None
        for base in base_venues:
            if base.id in claimed:
                continue
            d = geo.haversine_m((venue.lat, venue.lon), (base.lat, base.lon))
            if d > NAMED_MATCH_RADIUS_M:
                continue
            sim = name_similarity(venue.name, base.name)
            ok = sim >= NAME_SIMILARITY or (not _core_name(base.name) and d <= UNNAMED_MATCH_RADIUS_M)
            score = sim - d / 1000
            if ok and (best is None or score > best[0]):
                best = (score, base)
        if best:
            _absorb(best[1], venue)
            claimed.add(best[1].id)
        else:
            result.append(venue)
    return result


def merge_venue_lists(first: list[Venue], *others: list[Venue]) -> list[Venue]:
    merged = first
    for venues in others:
        merged = merge_into(merged, venues)
    return merged
