"""Command line: `kickit crawl`, `kickit export`, `kickit stats`, `kickit cities`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import db, export, mapview, media, pipeline
from .config import CITIES, SPORTS
from .http import make_client
from .sources import google_places, osm

log = logging.getLogger("kickit")

DEFAULT_DB = Path("data/kickit.db")
DEFAULT_EXPORT_DIR = Path("data/exports")
DEFAULT_CACHE_DIR = Path("data/cache")
DEFAULT_MEDIA_DIR = Path("data/media")


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _cities(selected: list[str] | None) -> list[str]:
    if not selected or selected == ["all"]:
        return list(CITIES)
    unknown = [c for c in selected if c not in CITIES]
    if unknown:
        sys.exit(f"Unknown city: {', '.join(unknown)}. Run `kickit cities` to list them.")
    return selected


def cmd_crawl(args: argparse.Namespace) -> None:
    sport = SPORTS[args.sport]
    key = google_places.api_key()
    if args.google == "yes" and not key:
        sys.exit("--google yes needs GOOGLE_PLACES_API_KEY (see .env.example).")
    use_google = args.google == "yes" or (args.google == "auto" and key is not None)
    if not use_google:
        log.info("Google Places not queried this run: OpenStreetMap + previously stored Google data only.")

    conn = db.connect(args.db)
    failed: list[str] = []
    with make_client() as client:
        overpass = osm.OverpassClient(client, cache_dir=None if args.no_cache else DEFAULT_CACHE_DIR)
        google = (client, key, args.google_grid) if use_google else None
        for slug in _cities(args.city):
            city = CITIES[slug]
            try:
                venues, sources = pipeline.crawl_city(conn, city, sport, overpass=overpass, google=google)
            except RuntimeError as exc:
                log.error("%s: crawl failed, keeping previous data: %s", city.name, exc)
                failed.append(slug)
                continue
            db.save_venues(conn, venues, sport.key, city.slug, sources)
            log.info("%s: saved %d venues / %d pitches", city.name, len(venues), sum(len(v.pitches) for v in venues))

    _print_stats(conn, sport.key)
    paths = export.export(conn, sport.key, args.export_dir)
    print("\nExports:\n" + "\n".join(f"  {p}" for p in paths.values()))
    if failed:
        sys.exit(f"Failed cities (re-run with --city {' '.join(failed)})")


def _print_stats(conn, sport: str) -> None:
    rows = export.stats(conn, sport)
    if not rows:
        print(f"No {sport} venues in the database yet.")
        return
    cols = list(rows[0])
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("\n" + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print("  ".join(str(sum(r[c] for r in rows) if c != "city" else "TOTAL").ljust(w) for c, w in zip(cols, widths)))


def cmd_export(args: argparse.Namespace) -> None:
    conn = db.connect(args.db)
    for city in args.city or [None]:
        for p in export.export(conn, args.sport, args.export_dir, city).values():
            print(p)


def cmd_stats(args: argparse.Namespace) -> None:
    _print_stats(db.connect(args.db), args.sport)


def cmd_map(args: argparse.Namespace) -> None:
    conn = db.connect(args.db)
    with make_client() as client:
        overpass = osm.OverpassClient(client, cache_dir=DEFAULT_CACHE_DIR, cache_ttl_s=30 * 24 * 3600)
        print(mapview.write_map(conn, overpass, args.out))


def cmd_media(args: argparse.Namespace) -> None:
    conn = db.connect(args.db)
    with make_client(timeout=60) as client:
        media.collect_photos(conn, client, args.media_dir)
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n, COUNT(local_path) AS downloaded, COUNT(DISTINCT venue_id) AS venues FROM media GROUP BY source"
    ).fetchall()
    for r in rows:
        print(f"{r['source']:18} {r['n']:4} photos  {r['downloaded']:4} downloaded  {r['venues']:4} venues")


def cmd_cities(_: argparse.Namespace) -> None:
    for c in CITIES.values():
        print(f"{c.slug:12} {c.name:12} osm relation {c.osm_relation_id}")


def main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="kickit", description="Crawl sports pitches in Morocco into a database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Fetch pitches from the sources and save them")
    crawl.add_argument("--sport", choices=sorted(SPORTS), default="football")
    crawl.add_argument("--city", nargs="+", help="City slugs (default: all 10)")
    crawl.add_argument(
        "--google",
        choices=["auto", "yes", "no"],
        default="auto",
        help="Query Google Places (auto: only if GOOGLE_PLACES_API_KEY is set). OSM is always crawled.",
    )
    crawl.add_argument("--google-grid", type=int, default=3, help="Split each city into N x N search tiles")
    crawl.add_argument("--no-cache", action="store_true", help="Ignore the 24h Overpass response cache")
    crawl.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    crawl.set_defaults(func=cmd_crawl)

    exp = sub.add_parser("export", help="Write CSV + GeoJSON from the database")
    exp.add_argument("--sport", choices=sorted(SPORTS), default="football")
    exp.add_argument("--city", nargs="+", help="Also export one file set per city")
    exp.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    exp.set_defaults(func=cmd_export)

    st = sub.add_parser("stats", help="Coverage summary per city")
    st.add_argument("--sport", choices=sorted(SPORTS), default="football")
    st.set_defaults(func=cmd_stats)

    md = sub.add_parser("media", help="Download freely licensed venue photos (Wikimedia Commons / Wikidata)")
    md.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR)
    md.set_defaults(func=cmd_media)

    mp = sub.add_parser("map", help="Write a self-contained HTML map of every venue and sport")
    mp.add_argument("--out", type=Path, default=DEFAULT_EXPORT_DIR / "kickit-map.html")
    mp.set_defaults(func=cmd_map)

    sub.add_parser("cities", help="List supported cities").set_defaults(func=cmd_cities)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
