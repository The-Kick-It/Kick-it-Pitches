"""Export the database to CSV (spreadsheets) and GeoJSON (maps / app seeding)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

VENUES_SQL = """
SELECT v.*,
       COUNT(p.id)                              AS pitch_count,
       GROUP_CONCAT(DISTINCT p.format)          AS formats,
       GROUP_CONCAT(DISTINCT p.surface)         AS surfaces,
       MAX(p.lit)                               AS any_lit
FROM venues v
JOIN venue_sports s ON s.venue_id = v.id AND s.sport = :sport
LEFT JOIN pitches p ON p.venue_id = v.id AND p.sport = :sport
WHERE (:city IS NULL OR v.city = :city)
GROUP BY v.id
ORDER BY v.city, v.name IS NULL, v.name
"""

PITCHES_SQL = """
SELECT p.*, v.city, v.name AS venue_name
FROM pitches p JOIN venues v ON v.id = p.venue_id
WHERE p.sport = :sport AND (:city IS NULL OR v.city = :city)
ORDER BY v.city, p.venue_id, p.id
"""


def _rows(conn: sqlite3.Connection, sql: str, sport: str, city: str | None) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, {"sport": sport, "city": city})]


def _write_csv(path: Path, rows: list[dict[str, Any]], drop: set[str]) -> None:
    if not rows:
        return
    fields = [k for k in rows[0] if k not in drop]
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows Arabic correctly
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export(conn: sqlite3.Connection, sport: str, out_dir: Path, city: str | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{sport}-{city}" if city else sport
    venues = _rows(conn, VENUES_SQL, sport, city)
    pitches = _rows(conn, PITCHES_SQL, sport, city)

    paths = {
        "venues_csv": out_dir / f"{stem}-venues.csv",
        "pitches_csv": out_dir / f"{stem}-pitches.csv",
        "geojson": out_dir / f"{stem}.geojson",
    }
    _write_csv(paths["venues_csv"], venues, drop={"photo_refs"})
    _write_csv(paths["pitches_csv"], pitches, drop={"geometry"})

    pitches_by_venue: dict[str, list[dict[str, Any]]] = {}
    for p in pitches:
        pitches_by_venue.setdefault(p["venue_id"], []).append(
            {k: v for k, v in p.items() if k not in ("geometry", "venue_id", "city", "venue_name")}
        )
    features = []
    for v in venues:
        props = {k: val for k, val in v.items() if k not in ("lat", "lon")}
        props["sources"] = json.loads(props["sources"])
        props["photo_refs"] = json.loads(props["photo_refs"] or "[]")
        props["pitches"] = pitches_by_venue.get(v["id"], [])
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [v["lon"], v["lat"]]}, "properties": props})
    paths["geojson"].write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return paths


def stats(conn: sqlite3.Connection, sport: str) -> list[dict[str, Any]]:
    sql = """
    SELECT v.city,
           COUNT(DISTINCT v.id)                                         AS venues,
           COUNT(DISTINCT CASE WHEN v.name IS NOT NULL THEN v.id END)   AS named,
           COUNT(DISTINCT CASE WHEN v.phone IS NOT NULL THEN v.id END)  AS with_phone,
           COUNT(DISTINCT p.id)                                         AS pitches,
           COUNT(DISTINCT CASE WHEN p.format IS NOT NULL THEN p.id END) AS with_format,
           COUNT(DISTINCT CASE WHEN p.surface IS NOT NULL THEN p.id END) AS with_surface
    FROM venues v
    JOIN venue_sports s ON s.venue_id = v.id AND s.sport = :sport
    LEFT JOIN pitches p ON p.venue_id = v.id AND p.sport = :sport
    GROUP BY v.city ORDER BY venues DESC
    """
    return [dict(r) for r in conn.execute(sql, {"sport": sport})]
