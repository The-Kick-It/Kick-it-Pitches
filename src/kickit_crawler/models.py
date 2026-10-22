"""Normalised records produced by every source and stored in the database."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Pitch:
    """One playing field. A venue usually has one or more."""

    id: str  # e.g. "osm:way/123456"
    sport: str
    lat: float
    lon: float
    name: str | None = None
    surface: str | None = None  # artificial_turf | grass | sand | dirt | hard | ...
    lit: bool | None = None
    indoor: bool | None = None
    length_m: float | None = None
    width_m: float | None = None
    format: str | None = None  # 5v5 | 7v7 | 9v9 | 11v11, inferred from size
    access: str | None = None
    fee: str | None = None
    source_url: str | None = None
    geometry: dict[str, Any] | None = None  # GeoJSON
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class Venue:
    """A place players go to: a sports centre, stadium, school ground, or standalone pitch(es)."""

    id: str  # stable id derived from the primary source record
    city: str
    lat: float
    lon: float
    sports: set[str] = field(default_factory=set)
    name: str | None = None
    name_ar: str | None = None
    venue_type: str | None = None  # sports_centre | stadium | school | university | park | pitch
    address: str | None = None
    district: str | None = None
    neighbourhood: str | None = None
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    opening_hours: str | None = None
    access: str | None = None
    fee: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    google_maps_url: str | None = None
    business_status: str | None = None
    photo_refs: list[str] = field(default_factory=list)
    pitches: list[Pitch] = field(default_factory=list)
    # (source, source_id, raw payload) for every record merged into this venue.
    source_records: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({s for s, _, _ in self.source_records})
