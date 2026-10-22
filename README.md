# Kick-it pitches crawler

Builds the venue/pitch database behind the Kick-it matchmaking app (football, padel, tennis,
basketball). Football in Morocco's 10 main cities is supported first.

## Sources

| Source | What it gives | Cost | Notes |
| --- | --- | --- | --- |
| **OpenStreetMap** (Overpass API) — always on | Exact location, outline, size, surface, lighting, grouping into venues, district and neighbourhood | Free | Licensed under ODbL: credit "© OpenStreetMap contributors" in the app. Most pitches have no name or phone. |
| **Google Places API (New)** — optional | Name, address, phone, website, opening hours, rating, photos | Paid per request after the free quota | Set `GOOGLE_PLACES_API_KEY` in `.env`. Uses the official API; scraping the Google Maps pages would break Google's terms. |

Google results are merged into the matching OSM venue when they are close together and have
similar names. Otherwise they are added as new venues. Later runs without Google reuse the
stored Google data, so you only pay for Google calls when you want fresh data.

## Usage

```bash
uv sync
uv run kickit crawl                          # football, all 10 cities
uv run kickit crawl --city casablanca rabat  # a subset
uv run kickit crawl --sport padel            # other sports: padel, tennis, basketball
uv run kickit crawl --google yes             # require Google Places (needs the key)
uv run kickit stats                          # how complete the data is, per city
uv run kickit export --city marrakech        # also write one set of files per city
uv run kickit map                            # interactive map of every sport -> data/exports/kickit-map.html
uv run pytest
```

Overpass responses are cached in `data/cache/` for 24 hours (`--no-cache` skips the cache). A
city that fails keeps its previous data, and the command prints which cities to re-run.

## Output

- `data/kickit.db`: SQLite database
- `data/exports/<sport>-venues.csv`: one row per venue, with its number of pitches, formats and surfaces
- `data/exports/<sport>-pitches.csv`: one row per pitch
- `data/exports/<sport>.geojson`: venues with their pitches nested, ready for a map or to seed the app
- `data/exports/kickit-map.html`: a single-file interactive map with filters for sport, format,
  surface, venue type and floodlights. It draws its own basemap (city boundary, main roads,
  coastline, neighbourhood names) instead of loading map tiles. It only goes online to load
  Leaflet and its fonts.

### Schema

- **venues**: `id`, `city`, `name`, `name_ar`, `venue_type` (sports_centre / stadium / school /
  university / park / pitch), `lat`, `lon`, `address`, `district`, `neighbourhood`, `phone`,
  `website`, `email`, `opening_hours`, `access`, `fee`, `rating`, `rating_count`,
  `google_maps_url`, `business_status`, `photo_refs`, `sources`, `first_seen_at`, `last_seen_at`
- **venue_sports**: (`venue_id`, `sport`). A venue can host several sports.
- **pitches**: `id`, `venue_id`, `sport`, `name`, `lat`, `lon`, `surface`, `lit`, `indoor`,
  `length_m`, `width_m`, `format` (5v5 / 7v7 / 9v9 / 11v11, worked out from the pitch length),
  `access`, `fee`, `source_url`, `geometry` (GeoJSON outline)
- **source_records**: the raw payload of each source record, so data can be re-processed without crawling again
- **crawl_runs**: one row for each city crawled

IDs stay the same across runs (`osm:way/123`, `google:ChIJ…`). Re-crawling updates rows in
place and removes venues that no longer exist in the sources.

## How OSM pitches become venues

1. Fetch every `leisure=pitch` tagged with the sport inside the city's official boundary.
2. Assign each pitch to the smallest sports centre, stadium, school or park around it.
3. Group pitches that are within 60 m of each other and outside any of those into one venue.
4. Keep venues tagged with the sport even when no pitch is drawn inside them.
5. Label each venue with its arrondissement (OSM boundary) and the nearest neighbourhood.

## Adding a city or sport

- City: add an entry to `CITIES` in `src/kickit_crawler/config.py`. Find the OSM relation id with
  `https://nominatim.openstreetmap.org/search?q=<city>,Morocco&format=json`.
- Sport: add a `SportSpec` (OSM `sport=*` values, Google search phrases, name keywords).
