"""Тесты распространения звука: дискретизация, расходимость, экраны, зелень."""

import math
from math import log10

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, box

from kazan_noise.features import Source
from kazan_noise.propagation import (SubSources, discretize, divergence_db,
                                     energy_sum, foliage_db, path_difference,
                                     shield_db)


def test_divergence():
    assert divergence_db(np.array([10.0, 20.0, 1.0])) == pytest.approx([31.0, 37.0206, 24.9794], abs=1e-3)


def test_energy_sum():
    assert energy_sum(np.array([60.0, 60.0])) == pytest.approx(63.0103, abs=1e-3)
    assert energy_sum(np.array([])) == -math.inf


@pytest.mark.parametrize(
    "profile, delta, vertices, shield",
    [
        ([(50, 10)], 1.1920, 1, 15.80),
        ([(50, 1)], 0.0, 0, 0.0),
        ([(50, 50)], 38.2379, 1, 20.0),
        ([(45, 20), (55, 20)], 6.7419, 2, 23.04),
        ([(45, 60), (55, 60)], None, 2, 25.0),
    ],
)
def test_path_difference_and_shield(profile, delta, vertices, shield):
    d, n = path_difference(0.5, 4.0, 100.0, profile)
    assert n == vertices and (delta is None or d == pytest.approx(delta, abs=1e-3))
    assert shield_db(d, n) == pytest.approx(shield, abs=0.01)


@pytest.mark.parametrize("path, expected", [(5.0, 0.0), (100.0, 5.0), (400.0, 10.0)])
def test_foliage(path, expected):
    assert foliage_db(path) == pytest.approx(expected)


def road(w=0.0, radius=1e9):
    return Source("road", "way/1", None, "primary", LineString([(-4000, 0), (4000, 0)]), 0.0, w, w, radius)


def level(sub, rx, ry):
    d = np.hypot(sub.x - rx, sub.y - ry)
    return energy_sum(sub.w_day - divergence_db(d))


def test_line_source_loses_3_db_per_distance_doubling_and_ignores_step():
    at = lambda ry, steps: level(discretize([road()], [0], 0, ry, steps=steps), 0, ry)
    assert at(20, ((math.inf, 5),)) - at(40, ((math.inf, 5),)) == pytest.approx(3.024, abs=0.05)
    assert at(20, ((math.inf, 5),)) == pytest.approx(at(20, ((math.inf, 10),)), abs=0.2)


def test_default_steps_coarsen_with_distance():
    sub = discretize([road()], [0], 0, 20)
    xs = np.sort(sub.x)
    assert np.diff(xs[np.abs(xs) < 150]).max() == pytest.approx(10) and np.diff(xs[xs > 600]).min() == pytest.approx(50)


def test_radius_drops_far_subsources():
    assert np.abs(discretize([road(radius=300)], [0], 0, 0).x).max() <= 300


def test_area_source_grid_and_cap():
    small = Source("construction", "way/2", None, "construction", box(0, 0, 100, 100), 3.0, 65, 45, 1000)
    big = Source("construction", "way/3", None, "construction", box(0, 0, 1000, 1000), 3.0, 65, 45, 1e9)
    s = discretize([small], [0], 0, 0)
    assert len(s.x) == 25 and s.w_day == pytest.approx(65 + 10 * log10(400))
    assert energy_sum(discretize([big], [0], 0, 0).w_day) == pytest.approx(115.0, abs=0.01)


def test_corridor_rises_on_glide_slope():
    c = Source("aircraft", "way/9", None, "corridor", LineString([(0, 0), (8000, 0)]), 0.0, 90, 85, 1e9)
    sub = discretize([c], [0], 0, 0)
    assert sub.z[np.argmin(np.abs(sub.x - 1000))] == pytest.approx(1000 * math.tan(math.radians(3)), abs=3)


def test_point_and_empty():
    p = Source("bar", "node/1", None, "bar", Point(10, 20), 1.5, 75, 85, 150)
    s = discretize([p], [0], 0, 0)
    assert (s.x, s.y, s.z, s.w_day, s.w_night, s.source_idx) == ([10], [20], [1.5], [75], [85], [0])
    assert len(discretize([p], [0], 1000, 0).x) == 0
    assert len(SubSources.empty().x) == 0


def test_energy_sum_all_minus_inf():
    assert energy_sum(np.array([-math.inf, -math.inf])) == -math.inf


def test_small_area_energy_is_exact():
    tiny = Source("construction", "way/4", None, "construction", box(0, 0, 15, 15), 3.0, 65, 45, 1000)
    assert energy_sum(discretize([tiny], [0], 0, 0).w_day) == pytest.approx(65 + 10 * log10(225), abs=0.01)


def test_split_ways_match_single_way():
    single = road()
    ways = [
        Source(
            "road",
            f"way/{i}",
            None,
            "primary",
            LineString([(-4000 + 30 * i, 0), (-4000 + 30 * (i + 1), 0)]),
            0.0,
            0.0,
            0.0,
            1e9,
        )
        for i in range(266)
    ]
    for ry in (20, 600):
        one = level(discretize([single], [0], 0, ry), 0, ry)
        parts = discretize(ways, range(len(ways)), 0, ry)
        assert level(parts, 0, ry) == pytest.approx(one, abs=0.2)


@pytest.mark.parametrize("w_day, w_night", [(65, 45), (45, 65)])
def test_area_cap_shifts_both_modes_by_the_same_amount(w_day, w_night):
    big = Source("construction", "way/3", None, "construction", box(0, 0, 1000, 1000), 3.0, w_day, w_night, 1e9)
    s = discretize([big], [0], 0, 0)
    assert max(energy_sum(s.w_day), energy_sum(s.w_night)) == pytest.approx(115.0, abs=0.01)
    assert s.w_night - s.w_day == pytest.approx(np.full(len(s.x), w_night - w_day), abs=1e-9)


def test_cache_shared_by_receivers_gives_the_same_subsources():
    sources = [
        road(radius=1000),
        Source("road", "way/5", None, "primary", LineString([(0, 0), (0, 3)]), 0.0, 0.0, 0.0, 1000),
        Source("industrial", "way/6", None, "industrial", box(100, 100, 400, 300), 5.0, 60, 55, 1000),
        Source("bar", "node/7", None, "bar", Point(10, 20), 1.5, 75, 85, 150),
        Source("aircraft", "way/8", None, "corridor", LineString([(0, 0), (8000, 0)]), 0.0, 90, 85, 3000),
    ]
    cache = {}
    for rx, ry in [(0, 20), (5, 25), (0, 600)]:
        plain = discretize(sources, range(len(sources)), rx, ry)
        cached = discretize(sources, range(len(sources)), rx, ry, cache=cache)
        for a, b in zip(vars(plain).values(), vars(cached).values(), strict=True):
            assert a.dtype == b.dtype and np.array_equal(a, b)
    assert sorted(cache) == list(range(len(sources)))


def test_area_source_geometry_is_not_prepared_in_place():
    # Подготовленную геометрию нельзя делить между потоками приёмников (NoiseModel.at_many).
    area = Source("construction", "way/2", None, "construction", box(0, 0, 100, 100), 3.0, 65, 45, 1000)
    discretize([area], [0], 0, 0)
    assert not shapely.is_prepared(area.geom)
