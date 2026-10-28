import math
from pathlib import Path

import pytest

from kazan_noise.config import NoiseConfig
from kazan_noise.queries import (
    areas_query,
    area_id,
    bbox_with_buffer,
    boundary_query,
    buildings_query,
    lines_query,
)


def test_area_id_for_kazan():
    assert area_id(367666) == 3600367666


def test_boundary_query_requests_relation_geometry():
    q = boundary_query(367666)
    assert q.startswith("[out:json][timeout:300];")
    assert "rel(367666);" in q and q.rstrip().endswith("out geom;")


def test_buildings_query_uses_kazan_area_and_relations():
    q = buildings_query(367666)
    assert q.startswith("[out:json][timeout:300];")
    assert "area(id:3600367666)" in q and 'wr["building"]' in q
    assert 'way["barrier"~"^(wall|noise_barrier|city_wall)$"]' in q and q.rstrip().endswith("out geom;")
    assert "out tags geom" not in q
    assert 'way["historic"="citywalls"]' in q


def test_lines_query_is_limited_by_bbox():
    q = lines_query((55.5, 48.7, 56.0, 49.5))
    assert q.startswith("[out:json][timeout:300];")
    assert "(55.5,48.7,56.0,49.5)" in q
    assert 'way["railway"~"^(rail|tram|subway|light_rail)$"]' in q and 'way["aeroway"="runway"]' in q
    assert 'way["highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]' in q
    assert 'way["highway"~"^(residential|unclassified|living_street|service)$"]' in q
    assert q.rstrip().endswith("out tags geom;")


def test_areas_query_requests_meta_and_construction_buildings():
    q = areas_query((55.5, 48.7, 56.0, 49.5))
    assert q.startswith("[out:json][timeout:300];")
    assert 'wr["building"="construction"]' in q and q.rstrip().endswith("out meta geom;")
    assert 'nwr["landuse"~"^(construction|industrial)$"]' in q
    assert 'nwr["amenity"~"^(bar|pub|nightclub|place_of_worship)$"]' in q
    assert 'wr["aeroway"="aerodrome"]' in q and 'wr["landuse"="forest"]' in q
    assert 'wr["natural"="wood"]' in q and 'wr["leisure"="park"]' in q


def test_bbox_buffer_is_3_km():
    bounds = {"minlat": 55.6031357, "minlon": 48.8205846, "maxlat": 55.9382194, "maxlon": 49.3812469}
    s, w, n, e = bbox_with_buffer(bounds, 3000)
    assert s == pytest.approx(55.6031357 - 3000 / 111320, abs=1e-6)
    assert w == pytest.approx(48.8205846 - 3000 / (111320 * math.cos(math.radians(55.7706776))), abs=1e-6)
    assert n == pytest.approx(55.9382194 + 3000 / 111320, abs=1e-6)
    assert e == pytest.approx(49.3812469 + 3000 / (111320 * math.cos(math.radians(55.7706776))), abs=1e-6)


def test_config_defaults():
    cfg = NoiseConfig(_env_file=None)
    assert cfg.boundary_relation == 367666 and cfg.overpass_urls[1].startswith("https://maps.mail.ru")
    assert cfg.features_path == Path("data/kazan_noise/features.npz")
    assert cfg.raw_dir == Path("data/kazan_noise/raw")
    assert cfg.grid_path == Path("data/kazan_noise/grid.npz")
    assert cfg.meta_path == Path("data/kazan_noise/meta.json")
