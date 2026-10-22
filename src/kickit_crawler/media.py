"""Venue photos from freely licensed sources, downloaded with their author and licence.

Photos come from the OSM tags of each venue and its pitches:
  - `wikimedia_commons=File:…` or `Category:…`
  - `image=<url>` (Commons URLs are resolved; other sites are kept as links only,
    because their licence is unknown)
  - `wikidata=Q…`, whose "image" property (P18) points to a Commons file
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx
from PIL import Image

from .db import now_iso
from .http import request_json

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
DOWNLOAD_WIDTH = 1024
CATEGORY_MAX_FILES = 4
MAX_PHOTOS_PER_VENUE = 4
# Wikidata / Commons-category photos describe the whole institution; for a school that means
# classrooms and graduation days, so they are only trusted for venues that *are* sports places.
INDIRECT_OK_TYPES = {"stadium", "sports_centre"}
NOT_A_PHOTO = re.compile(r"\b(logo|map|plan|carte|blason|emblem|flag|drapeau|diagram|seating)\b", re.I)
COMMONS_FILE_URL = re.compile(r"(?:commons\.wikimedia\.org/wiki/|upload\.wikimedia\.org/.+/)(File:)?([^/?#]+\.(?:jpe?g|png|webp|tiff?))", re.I)


@dataclass
class Photo:
    venue_id: str
    source: str  # wikimedia_commons | osm_image
    ref: str  # Commons file title, or the URL for other images
    page_url: str
    image_url: str | None = None
    license: str | None = None
    license_url: str | None = None
    author: str | None = None


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    return re.sub(r"\s+", " ", text) or None


def _commons_title(value: str) -> str:
    value = value.strip()
    return value if value.startswith(("File:", "Category:")) else f"File:{value}"


def references_from_tags(tags: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(Commons titles, Wikidata ids, other image URLs) referenced by one OSM element."""
    titles, qids, urls = [], [], []
    for value in filter(None, tags.get("wikimedia_commons", "").split(";")):
        titles.append(_commons_title(value))
    for url in filter(None, tags.get("image", "").split(";")):
        m = COMMONS_FILE_URL.search(url)
        if m:
            titles.append("File:" + unquote(m.group(2)).replace("_", " "))
        elif url.startswith("http"):
            urls.append(url.strip())
    if re.fullmatch(r"Q\d+", tags.get("wikidata", "")):
        qids.append(tags["wikidata"])
    return titles, qids, urls


def _wikidata_images(client: httpx.Client, qids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in range(0, len(qids), 50):
        data = request_json(client, "GET", WIKIDATA_API, params={
            "action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "claims", "format": "json",
        })
        for qid, entity in data.get("entities", {}).items():
            claims = entity.get("claims", {}).get("P18", [])
            out[qid] = [f"File:{c['mainsnak']['datavalue']['value']}" for c in claims if "datavalue" in c["mainsnak"]]
    return out


def _category_files(client: httpx.Client, category: str) -> list[str]:
    data = request_json(client, "GET", COMMONS_API, params={
        "action": "query", "list": "categorymembers", "cmtitle": category, "cmtype": "file",
        "cmlimit": CATEGORY_MAX_FILES, "format": "json",
    })
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def _commons_info(client: httpx.Client, titles: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(titles), 50):
        data = request_json(client, "GET", COMMONS_API, params={
            "action": "query", "titles": "|".join(titles[i:i + 50]), "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime", "iiurlwidth": DOWNLOAD_WIDTH, "format": "json",
        })
        query = data.get("query", {})
        # The API normalises titles (underscores, capitalisation); map results back to what we asked for.
        renamed = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            if "imageinfo" in page:
                out[renamed.get(page["title"], page["title"])] = page["imageinfo"][0]
    return out


def find_photos(conn: sqlite3.Connection, client: httpx.Client) -> list[Photo]:
    refs: dict[str, tuple[list[str], list[str], list[str]]] = {}
    rows = conn.execute(
        """SELECT sr.venue_id, sr.payload, v.venue_type FROM source_records sr
           JOIN venues v ON v.id = sr.venue_id WHERE sr.source = 'osm'"""
    )
    for row in rows:
        titles, qids, urls = references_from_tags(json.loads(row["payload"]))
        if row["venue_type"] not in INDIRECT_OK_TYPES:
            titles = [t for t in titles if t.startswith("File:")]
            qids = []
        if titles or qids or urls:
            acc = refs.setdefault(row["venue_id"], ([], [], []))
            for target, values in zip(acc, (titles, qids, urls)):
                target.extend(v for v in values if v not in target)

    all_qids = sorted({q for _, qids, _ in refs.values() for q in qids})
    by_qid = _wikidata_images(client, all_qids) if all_qids else {}

    venue_titles: dict[str, list[str]] = {}
    for venue_id, (titles, qids, _) in refs.items():
        expanded: list[str] = []
        for t in titles + [f for q in qids for f in by_qid.get(q, [])]:
            for f in _category_files(client, t) if t.startswith("Category:") else [t]:
                if f not in expanded and not NOT_A_PHOTO.search(f.replace("_", " ")):
                    expanded.append(f)
        venue_titles[venue_id] = expanded[:MAX_PHOTOS_PER_VENUE]

    info = _commons_info(client, sorted({t for ts in venue_titles.values() for t in ts}))
    photos: list[Photo] = []
    for venue_id, titles in venue_titles.items():
        for t in titles:
            ii = info.get(t)
            if not ii or not ii.get("mime", "").startswith("image/"):
                continue
            meta = ii.get("extmetadata", {})
            photos.append(Photo(
                venue_id=venue_id,
                source="wikimedia_commons",
                ref=t,
                page_url=ii["descriptionurl"],
                image_url=ii.get("thumburl") or ii["url"],
                license=_strip_html(meta.get("LicenseShortName", {}).get("value")),
                license_url=meta.get("LicenseUrl", {}).get("value"),
                author=_strip_html(meta.get("Artist", {}).get("value")),
            ))
    for venue_id, (_, _, urls) in refs.items():
        photos.extend(Photo(venue_id=venue_id, source="osm_image", ref=u, page_url=u) for u in urls)
    return photos


def download(client: httpx.Client, photo: Photo, media_dir: Path) -> Path | None:
    """Saves a JPEG at most DOWNLOAD_WIDTH wide. Only Commons files are downloaded (known licence)."""
    if photo.source != "wikimedia_commons" or not photo.image_url:
        return None
    name = hashlib.sha1(f"{photo.venue_id}|{photo.ref}".encode()).hexdigest()[:16] + ".jpg"
    path = media_dir / "photos" / name
    if path.exists():
        return path
    resp = client.get(photo.image_url)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content))
    img.thumbnail((DOWNLOAD_WIDTH, DOWNLOAD_WIDTH * 2))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=84, optimize=True)
    return path


def collect_photos(conn: sqlite3.Connection, client: httpx.Client, media_dir: Path) -> int:
    photos = find_photos(conn, client)
    ts = now_iso()
    saved = 0
    with conn:
        conn.execute("DELETE FROM media WHERE kind = 'photo'")
        for p in photos:
            try:
                path = download(client, p, media_dir)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("Could not download %s: %s", p.image_url, exc)
                path = None
            size = Image.open(path).size if path else (None, None)
            conn.execute(
                """INSERT OR REPLACE INTO media (venue_id, kind, source, ref, page_url, image_url, local_path,
                       width, height, license, license_url, author, fetched_at)
                   VALUES (?, 'photo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.venue_id, p.source, p.ref, p.page_url, p.image_url, str(path) if path else None,
                 *size, p.license, p.license_url, p.author, ts),
            )
            saved += 1
    log.info("[media] %d photos for %d venues", saved, len({p.venue_id for p in photos}))
    return saved
