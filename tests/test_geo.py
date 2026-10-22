from kickit_crawler import geo

# A ~40m x 20m rectangle in Casablanca (1e-5 deg lat ≈ 1.11 m).
LAT0, LON0 = 33.57, -7.59
DLAT = 20 / 111_195
DLON = 40 / (111_195 * 0.8334)  # cos(33.57°)
RECT = [(LAT0, LON0), (LAT0, LON0 + DLON), (LAT0 + DLAT, LON0 + DLON), (LAT0 + DLAT, LON0), (LAT0, LON0)]


def test_haversine_one_degree_latitude():
    assert abs(geo.haversine_m((0, 0), (1, 0)) - 111_195) < 10


def test_dimensions_of_rectangle():
    length, width = geo.dimensions_m(RECT)
    assert abs(length - 40) < 0.5
    assert abs(width - 20) < 0.5


def test_dimensions_of_rotated_rectangle_are_unchanged():
    # Rotate the rectangle 30° around its first corner in local metres; min-area box must still be 40 x 20.
    import math

    k = 111_195
    cos_lat = math.cos(math.radians(LAT0))
    a = math.radians(30)
    pts = []
    for x, y in [(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]:
        rx, ry = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        pts.append((LAT0 + ry / k, LON0 + rx / (k * cos_lat)))
    length, width = geo.dimensions_m(pts)
    assert abs(length - 40) < 0.5
    assert abs(width - 20) < 0.5


def test_football_format_thresholds():
    assert geo.football_format(None) is None
    assert geo.football_format(40) == "5v5"
    assert geo.football_format(60) == "7v7"
    assert geo.football_format(80) == "9v9"
    assert geo.football_format(105) == "11v11"
    assert geo.football_format(170) is None


def test_point_in_unordered_segments_with_hole():
    outer = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
    hole = [(4, 4), (4, 6), (6, 6), (6, 4), (4, 4)]
    segs = [(r[i], r[i + 1]) for r in (outer, hole) for i in range(len(r) - 1)]
    segs.reverse()  # order must not matter
    assert geo.point_in_segments((2, 2), segs)
    assert not geo.point_in_segments((5, 5), segs)
    assert not geo.point_in_segments((12, 5), segs)


def test_tile_bbox_covers_original():
    tiles = geo.tile_bbox((0, 0, 3, 6), 3, 2)
    assert len(tiles) == 6
    assert tiles[0] == (0, 0, 1, 3)
    assert tiles[-1] == (2, 3, 3, 6)
