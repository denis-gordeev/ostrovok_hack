import numpy as np
import pytest
from noise_helpers import (
    KAZAN_LAT,
    KAZAN_LON,
    KAZAN_X,
    KAZAN_Y,
    osm_node,
    osm_relation,
    osm_way,
)

from kazan_noise.osm import element_geometry, project, project_point, unproject_xy

SQUARE = [(49.10, 55.79), (49.11, 55.79), (49.11, 55.80), (49.10, 55.80), (49.10, 55.79)]


def test_closed_way_with_area_becomes_polygon():
    assert element_geometry(osm_way(1, SQUARE), area=True).geom_type == "Polygon"


def test_open_way_for_area_is_skipped():
    assert element_geometry(osm_way(1, SQUARE[:-1]), area=True) is None


def test_way_with_null_coords_or_single_point_is_skipped():
    el = osm_way(1, SQUARE[:1])
    el["geometry"].append(None)
    assert element_geometry(el, area=False) is None


def test_way_without_area_is_linestring():
    assert element_geometry(osm_way(1, SQUARE[:3]), area=False).geom_type == "LineString"


def test_node_is_point():
    geom = element_geometry(osm_node(1, 55.79, 49.10), area=False)
    assert geom.geom_type == "Point" and (geom.x, geom.y) == (49.10, 55.79)


def test_bowtie_polygon_is_made_valid():
    bow = [(0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0), (0.0, 0.0)]
    geom = element_geometry(osm_way(1, bow), area=True)
    assert geom.is_valid and geom.geom_type in ("Polygon", "MultiPolygon") and geom.area > 0


def test_multipolygon_with_inner_ring_has_hole():
    inner = [(49.103, 55.793), (49.106, 55.793), (49.106, 55.796), (49.103, 55.793)]
    geom = element_geometry(osm_relation(1, [("outer", SQUARE), ("inner", inner)], {"type": "multipolygon"}), area=True)
    assert geom.geom_type == "Polygon" and len(geom.interiors) == 1


def test_multipolygon_outer_split_into_two_ways():
    rel = osm_relation(1, [("outer", SQUARE[:3]), ("outer", SQUARE[2:])], {"type": "multipolygon"})
    assert element_geometry(rel, area=True).area == pytest.approx(0.0001)


def test_broken_multipolygon_returns_none():
    rel = osm_relation(1, [("outer", SQUARE[:3])], {"type": "multipolygon"})
    assert element_geometry(rel, area=True) is None


def test_projection_round_trip():
    x, y = project_point(KAZAN_LAT, KAZAN_LON)
    assert (x, y) == (pytest.approx(KAZAN_X, abs=0.1), pytest.approx(KAZAN_Y, abs=0.1))
    lat, lon = unproject_xy(np.array([x]), np.array([y]))
    assert lat[0] == pytest.approx(KAZAN_LAT, abs=1e-7) and lon[0] == pytest.approx(KAZAN_LON, abs=1e-7)


def test_project_geometries_to_metres():
    geom = element_geometry(osm_way(1, SQUARE), area=True)
    (proj,) = project([geom])
    # 0.01° долготы на 55.8° ≈ 628 м, 0.01° широты ≈ 1112 м.
    assert proj.area == pytest.approx(628 * 1112, rel=0.01)
