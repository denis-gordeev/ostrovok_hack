import json
import shutil
from dataclasses import replace

import numpy as np
import pytest
from noise_helpers import (
    FAR_FROM_ROAD,
    KAZAN_LAT,
    KAZAN_LON,
    NEAR_ROAD,
    ON_BUILDING,
    kazan_snapshot,
    scene_latlon,
    scene_service,
)
from shapely.geometry import LineString, Point, box

from kazan_noise.config import DAY, NIGHT
from kazan_noise.features import Source
from kazan_noise.grid import load_grid, save_grid
from kazan_noise.service import NoiseService, OutsideKazanError, _share
from kazan_noise.snapshot import SnapshotMissingError


def test_point_near_road_is_louder_and_explained(service):
    near, far = service.noise_at(*NEAR_ROAD), service.noise_at(*FAR_FROM_ROAD)
    assert near.day.score > far.day.score and near.main_sources[0].kind == "road"
    assert 0 < near.main_sources[0].share_day <= 1
    assert json.loads(near.model_dump_json())["day"]["label"]
    assert near.quiet_side is None and not near.inside_building


def test_outside_kazan(service):
    with pytest.raises(OutsideKazanError, match="вне городского округа"):
        service.noise_at(55.0, 49.0)


def test_swapped_lat_lon_hint(service):
    with pytest.raises(OutsideKazanError, match="перепутаны lat и lon"):
        service.noise_at(KAZAN_LON, KAZAN_LAT)


def test_noise_many_matches_noise_at_and_marks_points_outside(service):
    swapped = (KAZAN_LON, KAZAN_LAT)
    near, outside, on_building, swapped_error = service.noise_many([NEAR_ROAD, (55.0, 49.0), ON_BUILDING, swapped])
    assert near == service.noise_at(*NEAR_ROAD) and on_building == service.noise_at(*ON_BUILDING)
    assert isinstance(outside, OutsideKazanError) and "вне городского округа" in str(outside)
    assert isinstance(swapped_error, OutsideKazanError) and "перепутаны lat и lon" in str(swapped_error)
    assert service.noise_many([]) == []


def test_point_on_building_has_quiet_side(service):
    r = service.noise_at(*ON_BUILDING)
    assert r.inside_building and r.quiet_side is not None and r.quiet_side.day.score <= r.day.score


def test_main_sources_limited(service):
    r = service.noise_at(*NEAR_ROAD)
    assert len(r.main_sources) <= 3
    assert all(max(s.share_day, s.share_night) >= 0.01 for s in r.main_sources)


def test_missing_grid_is_reported(tmp_path):
    kazan_snapshot(tmp_path)
    with pytest.raises(SnapshotMissingError, match="kazan-noise grid"):
        NoiseService.load(tmp_path)


def _copy_grid(service_dir, tmp_path, **changes) -> None:
    """Снимок из service_dir и его сетка с подменёнными полями — в tmp_path."""
    shutil.copy(service_dir / "features.npz", tmp_path / "features.npz")
    grid = replace(load_grid(service_dir / "grid.npz"), **changes)
    save_grid(grid, tmp_path / "grid.npz")


def test_grid_without_built_up_cells_asks_to_rebuild(service_dir, service_grid, tmp_path):
    _copy_grid(service_dir, tmp_path, built_up=np.zeros_like(service_grid.built_up))
    with pytest.raises(SnapshotMissingError, match="запустите kazan-noise grid"):
        NoiseService.load(tmp_path)


def test_grid_of_another_snapshot_asks_to_rebuild(service_dir, tmp_path):
    _copy_grid(service_dir, tmp_path, osm_timestamp="2020-01-01T00:00:00Z")
    with pytest.raises(SnapshotMissingError, match="по другому снимку: запустите kazan-noise grid"):
        NoiseService.load(tmp_path)


def street(i: int, name: str | None, line: list[tuple[float, float]]) -> Source:
    return Source("road", f"way/{i}", name, "primary", LineString(line), 0.5, 80.0, 72.0, 1000)


ROAD = street(1, "ул. Тестовая", [(-2000, 0), (2000, 0)])
# Ночной клуб во дворе здания у дороги: ночью громче фасад во двор.
CLUB = Source("bar", "node/2", "Клуб", "nightclub", Point(0, 70), 1.5, 85.0, 95.0, 150)
BLOCK = (box(-50, 20, 50, 120).difference(box(-10, 60, 10, 80)), 20.0)


def test_night_score_and_quiet_side_come_from_facades_of_their_mode():
    service = scene_service(sources=[ROAD, CLUB], buildings=[BLOCK])
    r = service.noise_at(*scene_latlon(0, 40))
    receivers = [service.model.receiver(x, y) for x, y in service.model.facade_points(0).tolist()]
    loud_day = max(receivers, key=lambda q: q.day_db)
    loud_night = max(receivers, key=lambda q: q.night_db)
    # Ночью громче двор, а не фасад на дорогу.
    assert loud_night.night_db > loud_day.night_db + 5
    assert r.model_level_day == round(loud_day.day_db, 1) and r.model_level_night == round(loud_night.night_db, 1)
    assert r.night.score == service.scale.score(loud_night.night_db, NIGHT)
    assert r.quiet_side.day.score == service.scale.score(min(q.day_db for q in receivers), DAY)
    assert r.quiet_side.night.score == service.scale.score(min(q.night_db for q in receivers), NIGHT)
    assert r.quiet_side.day.score <= r.day.score and r.quiet_side.night.score <= r.night.score
    # Доли — у шумного фасада своего режима: днём дорога, ночью клуб, без экрана со стороны двора.
    road, club = sorted(r.main_sources, key=lambda s: s.kind != "road")
    assert road.share_day > 0.9 and club.share_night > 0.9 and not club.shielded and not road.shielded


def test_share_is_energy_fraction():
    assert _share(70.0, 73.0) == pytest.approx(0.50, abs=0.01)
    assert _share(60.0, 60.0) == pytest.approx(1.0)


def test_ways_of_one_named_street_merge_into_one_entry():
    at = scene_latlon(0, 30)
    whole = scene_service(sources=[ROAD]).noise_at(*at)
    halves = [street(1, "ул. Тестовая", [(-2000, 0), (0, 0)]), street(2, "ул. Тестовая", [(0, 0), (2000, 0)])]
    (merged,) = scene_service(sources=halves).noise_at(*at).main_sources
    assert merged.name == "ул. Тестовая" and merged.share_day > 0.9
    assert merged.share_day == pytest.approx(whole.main_sources[0].share_day, abs=0.01)
    unnamed = [street(1, None, [(-2000, 0), (0, 0)]), street(2, None, [(0, 0), (2000, 0)])]
    shares = [s.share_day for s in scene_service(sources=unnamed).noise_at(*at).main_sources]
    assert shares == pytest.approx([0.5, 0.5], abs=0.05)


def test_bar_with_largest_night_share_beats_louder_streets():
    streets = [
        street(1, "ул. А", [(-2000, -20), (2000, -20)]),
        street(2, "ул. Б", [(-2000, 20), (2000, 20)]),
        street(3, "ул. В", [(20, -2000), (20, 2000)]),
    ]
    bar = Source("bar", "node/4", "Бар", "bar", Point(0, 5), 1.5, 75.0, 85.0, 150)
    r = scene_service(sources=[*streets, bar]).noise_at(*scene_latlon(0, 0))
    assert len(r.main_sources) == 3 and r.main_sources[0].name == "Бар"
    assert r.main_sources[0].share_night > 0.5 > r.main_sources[1].share_day


def test_source_behind_building_is_flagged_shielded():
    behind = street(1, "ул. За домом", [(-2000, 0), (2000, 0)])
    open_ = street(2, "ул. Открытая", [(-2000, 150), (2000, 150)])
    r = scene_service(sources=[behind, open_], buildings=[(box(-1000, 20, 1000, 40), 15.0)]).noise_at(
        *scene_latlon(0, 60)
    )
    flags = {s.name: s.shielded for s in r.main_sources}
    assert flags == {"ул. За домом": True, "ул. Открытая": False}
