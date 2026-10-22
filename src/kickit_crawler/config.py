"""Static configuration: the cities we crawl and how each sport is recognised in each source."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    slug: str
    name: str
    # OSM relation of the city's administrative boundary (the "Pachalik").
    osm_relation_id: int
    # (south, west, north, east) — used to tile Google Places searches.
    bbox: tuple[float, float, float, float]

    @property
    def overpass_area_id(self) -> int:
        return 3_600_000_000 + self.osm_relation_id


# The 10 best-known Moroccan cities. Relation ids were resolved with Nominatim.
CITIES: dict[str, City] = {
    c.slug: c
    for c in [
        City("casablanca", "Casablanca", 4072985, (33.4933, -7.7512, 33.6409, -7.4574)),
        City("rabat", "Rabat", 2799215, (33.8964, -6.9306, 34.0378, -6.7615)),
        City("marrakech", "Marrakech", 2799538, (31.5442, -8.0893, 31.7104, -7.8847)),
        City("fes", "Fès", 2799557, (33.9764, -5.0782, 34.0755, -4.9377)),
        City("tangier", "Tanger", 2758781, (35.6996, -5.9484, 35.8260, -5.7135)),
        City("agadir", "Agadir", 2529624, (30.3507, -9.6766, 30.4934, -9.4841)),
        City("meknes", "Meknès", 2561177, (33.8439, -5.6033, 33.9238, -5.4676)),
        City("oujda", "Oujda", 2527998, (34.6415, -1.9661, 34.7442, -1.8517)),
        City("kenitra", "Kénitra", 2804287, (34.2022, -6.6612, 34.3109, -6.5027)),
        City("tetouan", "Tétouan", 2464649, (35.5411, -5.4303, 35.6107, -5.2611)),
    ]
}


@dataclass(frozen=True)
class SportSpec:
    key: str
    # Values of the OSM `sport=*` tag that mean this sport.
    osm_sports: tuple[str, ...]
    # Free-text queries sent to Google Places (French / Arabic / English mix, as used in Morocco).
    google_queries: tuple[str, ...]
    # A Google result is kept only if its name matches one of these (lowercase, accent-stripped).
    name_keywords: tuple[str, ...]

    @property
    def osm_sport_regex(self) -> str:
        alts = "|".join(self.osm_sports)
        return f"(^|;)({alts})(;|$)"


SPORTS: dict[str, SportSpec] = {
    s.key: s
    for s in [
        SportSpec(
            key="football",
            # `football` is technically ambiguous in OSM, but in Morocco it is always soccer.
            osm_sports=("soccer", "football", "futsal"),
            google_queries=(
                "terrain de football",
                "terrain de foot",
                "foot à 5",
                "city foot",
                "complexe sportif football",
                "ملعب كرة القدم",
            ),
            name_keywords=(
                "foot", "soccer", "futsal", "terrain", "stade", "stadium", "five",
                "city", "complexe", "sport", "arena", "ملعب", "كرة",
            ),
        ),
        SportSpec(
            key="padel",
            osm_sports=("padel",),
            google_queries=("padel", "club de padel", "بادل"),
            name_keywords=("padel", "paddle", "بادل"),
        ),
        SportSpec(
            key="tennis",
            osm_sports=("tennis",),
            google_queries=("court de tennis", "club de tennis", "ملعب التنس"),
            name_keywords=("tennis", "تنس"),
        ),
        SportSpec(
            key="basketball",
            osm_sports=("basketball",),
            google_queries=("terrain de basket", "basketball court", "ملعب كرة السلة"),
            name_keywords=("basket", "سلة", "sport", "salle", "complexe"),
        ),
    ]
}
