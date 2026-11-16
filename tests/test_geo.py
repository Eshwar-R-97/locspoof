import math

import pytest

from locpicker import geo

CHICAGO = (41.8781, -87.6298)
MILWAUKEE = (43.0389, -87.9065)


def test_haversine_chicago_milwaukee():
    d = geo.haversine(CHICAGO, MILWAUKEE)
    assert d == pytest.approx(130_500, rel=0.01)


def test_haversine_known_reference():
    # One degree of latitude along a meridian ≈ 111.195 km on the mean sphere
    assert geo.haversine((0, 0), (1, 0)) == pytest.approx(111_195, rel=0.005)
    assert geo.haversine(CHICAGO, CHICAGO) == 0


def test_bearing_cardinals():
    assert geo.bearing((0, 0), (1, 0)) == pytest.approx(0, abs=1e-6)
    assert geo.bearing((0, 0), (0, 1)) == pytest.approx(90, abs=1e-6)
    assert geo.bearing((0, 0), (-1, 0)) == pytest.approx(180, abs=1e-6)
    assert geo.bearing((0, 0), (0, -1)) == pytest.approx(270, abs=1e-6)


@pytest.mark.parametrize("brg", [0, 37, 90, 181, 300])
@pytest.mark.parametrize("dist", [3, 250, 12_000])
def test_destination_roundtrip(brg, dist):
    q = geo.destination(CHICAGO, brg, dist)
    assert geo.haversine(CHICAGO, q) == pytest.approx(dist, rel=1e-6, abs=1e-6)


def test_interpolate_endpoints():
    assert geo.interpolate(CHICAGO, MILWAUKEE, 0) == CHICAGO
    assert geo.interpolate(CHICAGO, MILWAUKEE, 1) == MILWAUKEE
    mid = geo.interpolate((0, 0), (2, 4), 0.5)
    assert mid == (1, 2)


def test_point_at_distance_start_mid_end():
    path = [(0, 0), (0, 0.01), (0.01, 0.01)]
    cum = geo.cumulative_distances(path)
    assert cum[0] == 0
    total = cum[-1]
    p0, i0 = geo.point_at_distance(path, cum, 0)
    assert p0 == path[0] and i0 == 0
    pe, ie = geo.point_at_distance(path, cum, total)
    assert pe == path[-1] and ie == 1
    pm, im = geo.point_at_distance(path, cum, cum[1])
    assert geo.haversine(pm, path[1]) < 0.01
    half, _ = geo.point_at_distance(path, cum, cum[1] / 2)
    assert geo.haversine(half, (0, 0.005)) < 0.5
    # past the end clamps
    assert geo.point_at_distance(path, cum, total * 2)[0] == path[-1]


def test_zero_length_and_single_point():
    assert geo.cumulative_distances([]) == []
    assert geo.cumulative_distances([CHICAGO]) == [0.0]
    p, i = geo.point_at_distance([CHICAGO], [0.0], 5)
    assert p == CHICAGO and i == 0
    with pytest.raises(geo.GeoError):
        geo.point_at_distance([], [], 0)


def test_duplicate_points_dont_divide_by_zero():
    path = [CHICAGO, CHICAGO, MILWAUKEE]
    cum = geo.cumulative_distances(path)
    p, _ = geo.point_at_distance(path, cum, 0)
    assert p == CHICAGO
    assert len(geo.dedupe(path)) == 2


def test_very_short_trip():
    b = geo.destination(CHICAGO, 45, 3)
    path = [CHICAGO, b]
    cum = geo.cumulative_distances(path)
    assert cum[-1] == pytest.approx(3, rel=1e-6)
    p, _ = geo.point_at_distance(path, cum, 1.5)
    assert geo.haversine(p, CHICAGO) == pytest.approx(1.5, rel=0.01)


def test_antimeridian_detection():
    assert geo.crosses_antimeridian([(0, 179.9), (0, -179.9)])
    assert not geo.crosses_antimeridian([CHICAGO, MILWAUKEE])


def test_densify_limits_segment_length():
    path = geo.densify([CHICAGO, MILWAUKEE], max_seg_m=1000)
    cum = geo.cumulative_distances(path)
    segs = [b - a for a, b in zip(cum, cum[1:])]
    assert max(segs) <= 1000.01
    assert path[0] == CHICAGO and path[-1] == MILWAUKEE
    assert cum[-1] == pytest.approx(geo.haversine(CHICAGO, MILWAUKEE), rel=1e-6)


def test_simplify_removes_collinear():
    straight = [geo.destination(CHICAGO, 90, d) for d in range(0, 1001, 10)]
    simp = geo.simplify(straight, 1)
    assert len(simp) == 2
    zig = [CHICAGO, geo.offset_meters(CHICAGO, 50, 50), geo.offset_meters(CHICAGO, 0, 100)]
    assert len(geo.simplify(zig, 3)) == 3


def test_offset_meters():
    q = geo.offset_meters(CHICAGO, 30, 40)
    assert geo.haversine(CHICAGO, q) == pytest.approx(50, rel=0.001)
    assert q[0] > CHICAGO[0] and q[1] > CHICAGO[1]


def test_valid_latlon():
    assert geo.valid_latlon(0, 0)
    assert not geo.valid_latlon(91, 0)
    assert not geo.valid_latlon(0, 181)
    assert not geo.valid_latlon(math.nan, 0)
