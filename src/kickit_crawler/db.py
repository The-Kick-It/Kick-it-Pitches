"""SQLite storage. The schema is plain SQL so it ports to Postgres/PostGIS for the app backend."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Venue

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id               TEXT PRIMARY KEY,
    city             TEXT NOT NULL,
    name             TEXT,
    name_ar          TEXT,
    venue_type       TEXT,
    lat              REAL NOT NULL,
    lon              REAL NOT NULL,
    address          TEXT,
    district         TEXT,
    neighbourhood    TEXT,
    phone            TEXT,
    website          TEXT,
    email            TEXT,
    opening_hours    TEXT,
    access           TEXT,
    fee              TEXT,
    rating           REAL,
    rating_count     INTEGER,
    google_maps_url  TEXT,
    business_status  TEXT,
    photo_refs       TEXT,           -- JSON array of Google photo resource names
    sources          TEXT NOT NULL,  -- JSON array, e.g. ["google","osm"]
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS venues_city ON venues(city);

CREATE TABLE IF NOT EXISTS venue_sports (
    venue_id  TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    sport     TEXT NOT NULL,
    PRIMARY KEY (venue_id, sport)
);
CREATE INDEX IF NOT EXISTS venue_sports_sport ON venue_sports(sport);

-- One OSM pitch can be tagged for several sports (e.g. sport=soccer;basketball): one row per sport.
CREATE TABLE IF NOT EXISTS pitches (
    id            TEXT NOT NULL,
    venue_id      TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    sport         TEXT NOT NULL,
    name          TEXT,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    surface       TEXT,
    lit           INTEGER,
    indoor        INTEGER,
    length_m      REAL,
    width_m       REAL,
    format        TEXT,
    access        TEXT,
    fee           TEXT,
    source_url    TEXT,
    geometry      TEXT,  -- GeoJSON
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (id, sport)
);
CREATE INDEX IF NOT EXISTS pitches_venue ON pitches(venue_id);

CREATE TABLE IF NOT EXISTS source_records (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    venue_id    TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,  -- raw JSON from the source, kept for re-processing
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

-- Photos (and later satellite views) of a venue. Downloaded files keep their author and licence
-- so the app can show the attribution the licence requires.
CREATE TABLE IF NOT EXISTS media (
    venue_id     TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,  -- photo
    source       TEXT NOT NULL,  -- wikimedia_commons | osm_image
    ref          TEXT NOT NULL,  -- Commons file title, or the image URL
    page_url     TEXT,
    image_url    TEXT,
    local_path   TEXT,           -- NULL when not downloaded (unknown licence)
    width        INTEGER,
    height       INTEGER,
    license      TEXT,
    license_url  TEXT,
    author       TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (venue_id, source, ref)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sport        TEXT NOT NULL,
    city         TEXT NOT NULL,
    sources      TEXT NOT NULL,
    venues       INTEGER NOT NULL,
    pitches      INTEGER NOT NULL,
    finished_at  TEXT NOT NULL
);
"""

VENUE_COLUMNS = [
    "id", "city", "name", "name_ar", "venue_type", "lat", "lon", "address", "district", "neighbourhood",
    "phone", "website", "email", "opening_hours", "access", "fee", "rating", "rating_count",
    "google_maps_url", "business_status",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _bool(v: bool | None) -> int | None:
    return None if v is None else int(v)


def save_venues(conn: sqlite3.Connection, venues: list[Venue], sport: str, city: str, sources: list[str]) -> None:
    ts = now_iso()
    with conn:
        for v in venues:
            values = [getattr(v, c) for c in VENUE_COLUMNS]
            placeholders = ", ".join("?" for _ in VENUE_COLUMNS)
            # COALESCE keeps values from a previous crawl (e.g. Google phone numbers) when this run lacks them.
            updates = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in VENUE_COLUMNS[1:])
            prev = conn.execute("SELECT sources, photo_refs FROM venues WHERE id = ?", (v.id,)).fetchone()
            all_sources = sorted(set(v.sources) | set(json.loads(prev["sources"]) if prev else []))
            photo_refs = v.photo_refs or (json.loads(prev["photo_refs"]) if prev else [])
            conn.execute(
                f"""INSERT INTO venues ({", ".join(VENUE_COLUMNS)}, photo_refs, sources, first_seen_at, last_seen_at)
                    VALUES ({placeholders}, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET {updates},
                        photo_refs = excluded.photo_refs, sources = excluded.sources,
                        last_seen_at = excluded.last_seen_at""",
                [*values, json.dumps(photo_refs), json.dumps(all_sources), ts, ts],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO venue_sports (venue_id, sport) VALUES (?, ?)", [(v.id, s) for s in sorted(v.sports)]
            )
            for p in v.pitches:
                conn.execute(
                    """INSERT OR REPLACE INTO pitches (id, venue_id, sport, name, lat, lon, surface, lit, indoor,
                           length_m, width_m, format, access, fee, source_url, geometry, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.id, v.id, p.sport, p.name, p.lat, p.lon, p.surface, _bool(p.lit), _bool(p.indoor),
                        p.length_m, p.width_m, p.format, p.access, p.fee, p.source_url,
                        json.dumps(p.geometry) if p.geometry else None, ts,
                    ),
                )
            conn.executemany(
                """INSERT OR REPLACE INTO source_records (source, source_id, venue_id, payload, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [(s, sid, v.id, json.dumps(payload, ensure_ascii=False), ts) for s, sid, payload in v.source_records],
            )
        _prune(conn, {v.id for v in venues}, sport, city, ts)
        conn.execute(
            "INSERT INTO crawl_runs (sport, city, sources, venues, pitches, finished_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sport, city, json.dumps(sources), len(venues), sum(len(v.pitches) for v in venues), ts),
        )


def _prune(conn: sqlite3.Connection, seen: set[str], sport: str, city: str, ts: str) -> None:
    """Drop pitches that disappeared, detach `sport` from venues of `city` this crawl no longer
    found, and delete venues left with no sport.

    Safe because every crawl covers all sources: OSM is always fetched and Google is either
    fetched or re-merged from the stored records (see pipeline.crawl_city).
    """
    conn.executemany(
        "DELETE FROM pitches WHERE venue_id = ? AND sport = ? AND last_seen_at < ?",
        [(vid, sport, ts) for vid in seen],
    )
    rows = conn.execute(
        """SELECT v.id FROM venues v JOIN venue_sports s ON s.venue_id = v.id
           WHERE v.city = ? AND s.sport = ?""",
        (city, sport),
    ).fetchall()
    for vid in (r["id"] for r in rows if r["id"] not in seen):
        conn.execute("DELETE FROM venue_sports WHERE venue_id = ? AND sport = ?", (vid, sport))
        conn.execute("DELETE FROM pitches WHERE venue_id = ? AND sport = ?", (vid, sport))
    conn.execute("DELETE FROM venues WHERE id NOT IN (SELECT venue_id FROM venue_sports)")
