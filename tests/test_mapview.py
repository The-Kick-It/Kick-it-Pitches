import json

from kickit_crawler import db, mapview
from kickit_crawler.config import CITIES, SPORTS
from kickit_crawler.sources import osm

from test_pipeline import CONTAINERS, PITCHES, PLACES


def test_simplify_drops_collinear_points_and_keeps_corners():
    line = [(0.0, i * 1e-5) for i in range(50)] + [(0.001, 49e-5)]
    out = mapview._simplify(line, 5)
    assert out == [line[0], line[49], line[50]]


def test_write_map_embeds_venues(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    venues = osm.build_venues(CITIES["casablanca"], SPORTS["football"], PITCHES, CONTAINERS, PLACES)
    db.save_venues(conn, venues, "football", "casablanca", ["osm"])
    monkeypatch.setattr(mapview, "CITIES", {"casablanca": CITIES["casablanca"]})
    monkeypatch.setattr(mapview, "build_basemap", lambda _o, c: {"slug": c.slug, "name": c.name, "bbox": list(c.bbox),
                        "boundary": [], "districts": [], "roads": [], "coast": [], "labels": []})

    html = mapview.write_map(conn, None, tmp_path / "map.html").read_text()

    assert mapview.DATA_PLACEHOLDER not in html
    start = html.index("const DATA = ") + len("const DATA = ")
    data = json.JSONDecoder().raw_decode(html[start:])[0]
    centre = next(v for v in data["venues"] if v["id"] == "osm:way/101")
    assert centre["sports"] == ["football"]
    assert len(centre["pitches"]) == 2
    assert len(centre["pitches"][0]["outline"]) == 10  # 5 closed-ring points, flattened
