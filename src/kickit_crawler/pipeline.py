"""One crawl step: fetch a city from each source, merge, and return venues ready to save."""

from __future__ import annotations

import json
import logging
import sqlite3

import httpx

from . import merge
from .config import City, SportSpec
from .models import Venue
from .sources import google_places, osm

log = logging.getLogger(__name__)


def stored_google_venues(conn: sqlite3.Connection, city: City, sport: SportSpec) -> list[Venue]:
    """Google places saved by an earlier crawl, so a run that skips Google (no key, or to save
    API cost) still merges them instead of overwriting their phone numbers, ratings, etc."""
    rows = conn.execute(
        """SELECT sr.payload FROM source_records sr
           JOIN venues v ON v.id = sr.venue_id
           JOIN venue_sports s ON s.venue_id = v.id AND s.sport = ?
           WHERE sr.source = ? AND v.city = ?""",
        (sport.key, google_places.SOURCE, city.slug),
    )
    return [google_places.place_to_venue(json.loads(r["payload"]), city, sport) for r in rows]


def crawl_city(
    conn: sqlite3.Connection,
    city: City,
    sport: SportSpec,
    *,
    overpass: osm.OverpassClient,
    google: tuple[httpx.Client, str, int] | None,
) -> tuple[list[Venue], list[str]]:
    """Returns the merged venues and the sources queried this run.

    OSM is always the base layer. Pass `google=(client, api_key, grid)` to query Google Places;
    with None, Google places stored by earlier crawls are merged in instead.
    """
    lists = [osm.fetch_city(overpass, city, sport)]
    sources = [osm.SOURCE]
    if google:
        client, key, grid = google
        lists.append(google_places.fetch_city(client, key, city, sport, grid=grid))
        sources.append(google_places.SOURCE)
    else:
        previous = stored_google_venues(conn, city, sport)
        if previous:
            log.info("[google] %s: reusing %d places from the previous crawl", city.name, len(previous))
            lists.append(previous)
    return merge.merge_venue_lists(*lists), sources
