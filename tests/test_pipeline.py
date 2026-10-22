"""OSM venue building, cross-source merging and database upserts, on hand-made Overpass data."""

import json

from kickit_crawler import db, merge, pipeline
from kickit_crawler.config import CITIES, SPORTS
from kickit_crawler.models import Venue
from kickit_crawler.sources import google_places, osm

CASA = CITIES["casablanca"]
FOOTBALL = SPORTS["football"]
M = 1 / 111_195  # ~1 metre in degrees of latitude (longitude scale is close enough for these tests)


def square(lat, lon, size_m):
    pts = [(lat, lon), (lat, lon + size_m * M), (lat + size_m * M, lon + size_m * M), (lat + size_m * M, lon), (lat, lon)]
    return [{"lat": a, "lon": b} for a, b in pts]


def way(id_, geometry, **tags):
    return {"type": "way", "id": id_, "geometry": geometry, "tags": tags}


def pitch(id_, lat, lon, size_m=30, **tags):
    return way(id_, square(lat, lon, size_m), leisure="pitch", sport="soccer", **tags)


LAT, LON = 33.57, -7.59
CONTAINERS = [
    way(100, square(LAT, LON, 1000), leisure="park", name="Big Park"),
    way(101, square(LAT, LON, 200), leisure="sports_centre", name="City Foot 5", phone="+212 600000000"),
    {"type": "node", "id": 102, "lat": LAT + 0.05, "lon": LON, "tags": {"leisure": "sports_centre", "sport": "soccer", "name": "Mapped As Point"}},
]
PITCHES = [
    pitch(1, LAT + 10 * M, LON + 10 * M, surface="artificial_turf"),
    pitch(2, LAT + 60 * M, LON + 10 * M),
    pitch(3, LAT + 500 * M, LON + 500 * M),  # in the park only
    pitch(4, LAT + 0.02, LON),  # orphan, clustered with 5
    pitch(5, LAT + 0.02 + 40 * M, LON),
    pitch(6, LAT + 0.03, LON, size_m=100, lit="yes"),  # lone orphan
]
PLACES = [
    {"type": "node", "id": 200, "lat": LAT, "lon": LON, "tags": {"place": "neighbourhood", "name": "Maârif"}},
    {
        "type": "relation", "id": 300, "tags": {"boundary": "administrative", "admin_level": "10", "name:fr": "Anfa"},
        "members": [{"type": "way", "role": "outer", "geometry": square(LAT - 0.001, LON - 0.001, 3000)}],
    },
]


def build():
    return {v.id: v for v in osm.build_venues(CASA, FOOTBALL, PITCHES, CONTAINERS, PLACES)}


def test_pitches_go_to_the_smallest_enclosing_container():
    venues = build()
    centre = venues["osm:way/101"]
    assert centre.name == "City Foot 5"
    assert centre.phone == "+212 600000000"
    assert centre.venue_type == "sports_centre"
    assert sorted(p.id for p in centre.pitches) == ["osm:way/1", "osm:way/2"]
    assert [p.id for p in venues["osm:way/100"].pitches] == ["osm:way/3"]


def test_orphans_are_clustered_by_distance():
    venues = build()
    assert sorted(p.id for p in venues["osm:way/4"].pitches) == ["osm:way/4", "osm:way/5"]
    lone = venues["osm:way/6"]
    assert lone.venue_type == "pitch"
    assert lone.pitches[0].lit is True
    assert lone.pitches[0].format == "11v11"


def test_sport_tagged_point_venue_is_kept_without_pitches():
    venue = build()["osm:node/102"]
    assert venue.name == "Mapped As Point"
    assert venue.pitches == []


def test_pitch_attributes_and_location_labels():
    centre = build()["osm:way/101"]
    p1 = next(p for p in centre.pitches if p.id == "osm:way/1")
    assert p1.surface == "artificial_turf"
    assert p1.format == "5v5"
    assert centre.district == "Anfa"
    assert centre.neighbourhood == "Maârif"


def google_place(pid, name, lat, lon, **extra):
    return {"id": pid, "displayName": {"text": name}, "location": {"latitude": lat, "longitude": lon}, "types": [], **extra}


def test_google_relevance_filter():
    assert google_places.is_relevant(google_place("a", "Terrain Al Amal", 0, 0), FOOTBALL)
    assert google_places.is_relevant(google_place("b", "Chez Ali", 0, 0, types=["sports_complex"]), FOOTBALL)
    assert not google_places.is_relevant(google_place("c", "Decathlon", 0, 0, primaryType="sporting_goods_store"), FOOTBALL)
    assert not google_places.is_relevant(google_place("d", "Café du Stade", 0, 0, primaryType="cafe"), FOOTBALL)


def test_google_venue_merges_into_matching_osm_venue():
    osm_venues = list(build().values())
    g_same = google_places.place_to_venue(
        google_place("G1", "City Foot 5 Casablanca", LAT + 50 * M, LON + 50 * M, rating=4.3, userRatingCount=120,
                     nationalPhoneNumber="0522 00 00 00"),
        CASA, FOOTBALL,
    )
    g_unnamed_match = google_places.place_to_venue(google_place("G2", "Ali Five", LAT + 0.03, LON + 20 * M), CASA, FOOTBALL)
    g_new = google_places.place_to_venue(google_place("G3", "Urban Soccer", LAT + 0.1, LON), CASA, FOOTBALL)

    merged = {v.id: v for v in merge.merge_venue_lists(osm_venues, [g_same, g_unnamed_match, g_new])}

    centre = merged["osm:way/101"]
    assert centre.rating == 4.3
    assert centre.phone == "0522 00 00 00"  # Google contact data wins
    assert centre.sources == ["google", "osm"]
    assert len(centre.pitches) == 2
    assert merged["osm:way/6"].name == "Ali Five"  # unnamed OSM pitch takes the Google name
    assert "google:G3" in merged
    assert len(merged) == len(osm_venues) + 1


def test_save_is_idempotent_and_prunes_vanished_venues(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    venues = list(build().values())
    db.save_venues(conn, venues, "football", "casablanca", ["osm"])
    db.save_venues(conn, venues, "football", "casablanca", ["osm"])
    assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == len(venues)
    assert conn.execute("SELECT COUNT(*) FROM pitches").fetchone()[0] == len(PITCHES)

    # Next crawl: the lone pitch is gone from OSM.
    remaining = [v for v in build().values() if v.id != "osm:way/6"]
    db.save_venues(conn, remaining, "football", "casablanca", ["osm"])
    assert conn.execute("SELECT COUNT(*) FROM venues WHERE id = 'osm:way/6'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pitches").fetchone()[0] == len(PITCHES) - 1


def test_crawl_without_google_reuses_stored_google_places(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    g = google_places.place_to_venue(
        google_place("G1", "City Foot 5", LAT + 50 * M, LON + 50 * M, nationalPhoneNumber="0522"), CASA, FOOTBALL
    )
    db.save_venues(conn, merge.merge_venue_lists(list(build().values()), [g]), "football", "casablanca", ["osm", "google"])

    monkeypatch.setattr(osm, "fetch_city", lambda *_: list(build().values()))
    venues, sources = pipeline.crawl_city(conn, CASA, FOOTBALL, overpass=None, google=None)
    db.save_venues(conn, venues, "football", "casablanca", sources)

    row = conn.execute("SELECT phone, sources FROM venues WHERE id = 'osm:way/101'").fetchone()
    assert row["phone"] == "0522"
    assert json.loads(row["sources"]) == ["google", "osm"]
    assert sources == ["osm"]


def test_multi_sport_pitch_is_kept_once_per_sport(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    shared = [way(7, square(LAT + 0.04, LON, 30), leisure="pitch", sport="soccer;basketball")]
    for key in ("football", "basketball"):
        venues = osm.build_venues(CASA, SPORTS[key], shared, [], [])
        db.save_venues(conn, venues, key, "casablanca", ["osm"])
    rows = conn.execute("SELECT sport FROM pitches WHERE id = 'osm:way/7' ORDER BY sport").fetchall()
    assert [r["sport"] for r in rows] == ["basketball", "football"]
