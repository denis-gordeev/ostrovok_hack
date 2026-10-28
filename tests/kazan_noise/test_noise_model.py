"""Тесты модели в точке: фон, экраны, листва, вклады, фасады здания."""

import numpy as np
import pytest
import shapely
from noise_helpers import local_scene
from shapely.geometry import LineString, Point, box

from kazan_noise.features import Source
from kazan_noise.model import NoiseModel
from kazan_noise.propagation import air_db, divergence_db, energy_sum
from kazan_noise.weights import MAX_FACADE_POINTS, OFF

PRIMARY = Source("road", "way/1", "ул. Тестовая", "primary", LineString([(-2000, 0), (2000, 0)]), 0.5, 80.0, 72.0, 1000)
TERTIARY = Source("road", "way/2", None, "tertiary", LineString([(-2000, 400), (2000, 400)]), 0.5, 70.0, 60.0, 1000)


def test_no_sources_gives_background():
    r = NoiseModel(local_scene()).receiver(0, 0)
    assert (r.day_db, r.night_db, r.contributions) == (35.0, 35.0, [])


def test_building_shields_road():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 60)
    shielded = NoiseModel(local_scene(sources=[PRIMARY], buildings=[(box(-300, 20, 300, 40), 15.0)])).receiver(0, 60)
    assert shielded.day_db <= open_.day_db - 6


def test_forest_strip_takes_10_db():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 220)
    forest = NoiseModel(local_scene(sources=[PRIMARY], green=[(box(-5000, 10, 5000, 210), 1.0)])).receiver(0, 220)
    # Сравниваем вклад дороги: в итоге есть фон 35 дБ, он маскирует часть разницы (≈8,5 дБ вместо 10).
    assert open_.contributions[0].day_db - forest.contributions[0].day_db == pytest.approx(10.0, abs=0.1)
    assert forest.day_db < open_.day_db - 8


def test_contributions_grouped_sorted_and_sum_to_total():
    r = NoiseModel(local_scene(sources=[PRIMARY, TERTIARY])).receiver(0, 50)
    assert [c.source_idx for c in r.contributions] == [0, 1]
    assert energy_sum(np.array([c.day_db for c in r.contributions] + [35.0])) == pytest.approx(r.day_db, abs=0.01)


def test_point_inside_building_gives_loud_and_quiet_facades():
    m = NoiseModel(local_scene(sources=[PRIMARY], buildings=[(box(-20, 20, 20, 60), 20.0)]))
    p = m.at(0, 40)
    assert p.inside_building and p.loud_day.y < 25 and p.quiet_day.y > 55
    assert p.loud_day.day_db > p.quiet_day.day_db + 10
    # Одна дорога: ночью шумный и тихий фасады те же, что днём.
    assert (p.loud_night, p.quiet_night) == (p.loud_day, p.quiet_day)


# Дорога с юга, ночной клуб во дворе: днём громче фасад на дорогу, ночью — фасад во двор.
CLUB = Source("bar", "node/2", "Клуб", "nightclub", Point(0, 70), 1.5, 85.0, 95.0, 150)
YARD = box(-10, 60, 10, 80)
BLOCK = (box(-50, 20, 50, 120).difference(YARD), 20.0)


def test_loud_and_quiet_facades_are_chosen_per_mode():
    m = NoiseModel(local_scene(sources=[PRIMARY, CLUB], buildings=[BLOCK]))
    p = m.at(0, 40)
    assert p.loud_day.y < 20 and YARD.contains(Point(p.loud_night.x, p.loud_night.y))
    receivers = [m.receiver(x, y) for x, y in m.facade_points(0).tolist()]
    assert p.loud_day.day_db == max(r.day_db for r in receivers)
    assert p.loud_night.night_db == max(r.night_db for r in receivers)
    assert p.quiet_day.day_db == min(r.day_db for r in receivers)
    assert p.quiet_night.night_db == min(r.night_db for r in receivers)



def test_at_many_matches_at_point_by_point_with_any_number_of_threads():
    scene = local_scene(sources=[PRIMARY, CLUB], buildings=[BLOCK])
    points = [(0, 40), (0, 150), (200, 10), (0, 40)]
    serial = NoiseModel(scene, threads=1)
    expected = [serial.at(x, y) for x, y in points]
    assert serial.at_many(points) == expected
    assert NoiseModel(scene, threads=4).at_many(points) == expected
    assert NoiseModel(scene, threads=4).at_many([]) == []

def test_facade_points_are_capped_and_outside():
    m = NoiseModel(local_scene(buildings=[(box(0, 0, 400, 400), 20.0)]))
    pts = m.facade_points(0)
    assert len(pts) == MAX_FACADE_POINTS == 8
    assert shapely.distance(box(0, 0, 400, 400), shapely.points(pts)).min() > 1.9


def test_nudge_outside_moves_point_2_m_off_facade():
    m = NoiseModel(local_scene(buildings=[(box(0, 0, 40, 40), 10.0)]))
    x, y = m.nudge_outside(5, 20)
    assert box(0, 0, 40, 40).distance(Point(x, y)) == pytest.approx(2.0, abs=0.05)


def test_wall_shields_road():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 30)
    barrier = (LineString([(-500, 10), (500, 10)]), 4.0)
    wall = NoiseModel(local_scene(sources=[PRIMARY], walls=[barrier])).receiver(0, 30)
    assert wall.day_db <= open_.day_db - 5


def test_source_overhead_uses_3d_distance_without_shielding():
    bells = Source("worship", "node/3", None, "place_of_worship", Point(0, 0), 20.0, 80.0, OFF, 300)
    r = NoiseModel(local_scene(sources=[bells]), receiver_height=4.0).receiver(0, 0)
    direct = 80.0 - divergence_db(np.array([16.0]))[0] - air_db(np.array([16.0]))[0]
    assert r.day_db == pytest.approx(energy_sum(np.array([direct, 35.0])), abs=1e-6)
    (c,) = r.contributions
    assert (c.source_idx, c.min_distance_m, c.mean_shield_db) == (0, 0.0, 0.0)
    assert c.day_db == pytest.approx(direct, abs=1e-6)


def test_building_at_ignores_walls_and_open_point_has_no_quiet_side():
    m = NoiseModel(
        local_scene(
            buildings=[(box(0, 0, 40, 40), 10.0), (box(30, 30, 60, 60), 10.0)],
            walls=[(LineString([(-50, 5), (-10, 5)]), 2.5)],
        )
    )
    assert (m.building_at(35, 35), m.building_at(50, 50), m.building_at(-20, 5), m.building_at(100, 100)) == (
        0,
        1,
        None,
        None,
    )
    assert m.nudge_outside(100, 100) == (100, 100)
    p = m.at(100, 100)
    assert not p.inside_building and p.quiet_day is None and p.quiet_night is None
    assert p.loud_day is p.loud_night and (p.loud_day.x, p.loud_day.y) == (100, 100)


def test_courtyard_gets_a_facade_point():
    block = box(0, 0, 400, 400).difference(box(185, 185, 215, 215))
    m = NoiseModel(local_scene(buildings=[(block, 20.0)]))
    pts = m.facade_points(0)
    in_yard = shapely.contains_xy(box(185, 185, 215, 215), pts[:, 0], pts[:, 1])
    assert len(pts) <= MAX_FACADE_POINTS and in_yard.sum() >= 1


def test_source_is_not_shielded_by_its_own_building():
    bar = Source("bar", "node/5", None, "bar", Point(0, 0), 1.5, 75.0, 85.0, 150)
    own = (box(-10, -10, 10, 10), 15.0)
    other = (box(-20, 60, 20, 80), 15.0)
    free = NoiseModel(local_scene(sources=[bar])).receiver(0, 50)
    inside = NoiseModel(local_scene(sources=[bar], buildings=[own])).receiver(0, 50)
    assert inside.night_db == pytest.approx(free.night_db, abs=0.1)
    behind_open = NoiseModel(local_scene(sources=[bar], buildings=[own])).receiver(0, 100)
    behind = NoiseModel(local_scene(sources=[bar], buildings=[own, other])).receiver(0, 100)
    assert behind.contributions[0].night_db <= behind_open.contributions[0].night_db - 10
    assert behind.contributions[0].mean_shield_db > 5


ROW = [(box(0, 0, 40, 40), 20.0), (box(40, 0, 80, 40), 20.0)]


def test_facade_points_of_adjacent_buildings_stay_outside_the_neighbour():
    m = NoiseModel(local_scene(buildings=ROW))
    for idx, other in ((0, 1), (1, 0)):
        pts = m.facade_points(idx)
        geom = ROW[other][0]
        assert len(pts) > 0 and not shapely.contains_xy(geom, pts[:, 0], pts[:, 1]).any()


def test_nudge_outside_leaves_the_whole_touching_block():
    m = NoiseModel(local_scene(buildings=ROW))
    x, y = m.nudge_outside(38, 20)
    assert m.building_at(x, y) is None
    assert min(geom.distance(Point(x, y)) for geom, _ in ROW) == pytest.approx(2.0, abs=0.05)


def test_obstacles_below_line_of_sight_do_not_shield():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 60)
    low_wall = (LineString([(-30, 50), (30, 50)]), 1.0)
    r = NoiseModel(local_scene(sources=[PRIMARY], walls=[low_wall])).receiver(0, 60)
    assert r.day_db == pytest.approx(open_.day_db, abs=1e-9)


def test_receiver_on_a_wall_line_is_finite_and_unshielded():
    wall = (LineString([(-500, 10), (500, 10)]), 4.0)
    r = NoiseModel(local_scene(sources=[PRIMARY], walls=[wall])).receiver(0, 10)
    assert np.isfinite([r.day_db, r.night_db]).all() and r.contributions[0].mean_shield_db == 0.0


def test_high_source_over_low_wall_is_unshielded():
    bells = Source("worship", "node/3", None, "place_of_worship", Point(-50, 20), 20.0, 80.0, OFF, 300)
    wall = (LineString([(-500, 10), (500, 10)]), 4.0)
    r = NoiseModel(local_scene(sources=[bells], walls=[wall])).receiver(0, 0)
    assert np.isfinite(r.day_db) and r.contributions[0].mean_shield_db == 0.0


def test_foliage_ignores_short_paths_and_applies_park_factor():
    bar = Source("bar", "node/5", None, "bar", Point(0, 0), 1.5, 75.0, 85.0, 150)
    open_ = NoiseModel(local_scene(sources=[bar])).receiver(0, 50).contributions[0].day_db
    short = NoiseModel(local_scene(sources=[bar], green=[(box(-100, 10, 100, 18), 1.0)])).receiver(0, 50)
    park = NoiseModel(local_scene(sources=[bar], green=[(box(-100, 5, 100, 45), 0.5)])).receiver(0, 50)
    assert short.contributions[0].day_db == pytest.approx(open_, abs=1e-9)
    assert open_ - park.contributions[0].day_db == pytest.approx(0.05 * 40 * 0.5, abs=1e-9)


def test_nudge_outside_avoids_a_building_across_a_small_gap():
    m = NoiseModel(local_scene(buildings=[(box(0, 0, 40, 40), 20.0), (box(41, 0, 81, 40), 20.0)]))
    x, y = m.nudge_outside(39, 20)
    assert m.building_at(x, y) is None
    assert box(0, 0, 40, 40).distance(Point(x, y)) == pytest.approx(2.0, abs=0.05)


def test_pruning_quiet_far_subsources_keeps_levels():
    # Громкая дорога за домом, колокольня, громкая только днём, и много слабых дальних улиц.
    bells = Source("worship", "node/3", None, "place_of_worship", Point(100, 150), 20.0, 95.0, OFF, 300)
    weak = [
        Source("road", f"way/{10 + i}", None, "service", LineString([(-900, 300 + 25 * i), (900, 300 + 25 * i)]),
               0.5, 40.0, 30.0, 1000)
        for i in range(25)
    ]
    scene = local_scene(sources=[PRIMARY, bells, *weak], buildings=[(box(-1000, 20, 1000, 40), 15.0)])
    full = NoiseModel(scene, prune=False).receiver(0, 60)
    pruned = NoiseModel(scene).receiver(0, 60)
    assert pruned.day_db == pytest.approx(full.day_db, abs=0.2)
    assert pruned.night_db == pytest.approx(full.night_db, abs=0.2)
    # Колокольня ночью выключена, но днём не отброшена; слабые дальние улицы отброшены целиком.
    assert 1 in [c.source_idx for c in pruned.contributions]
    assert len(pruned.contributions) < len(full.contributions)
