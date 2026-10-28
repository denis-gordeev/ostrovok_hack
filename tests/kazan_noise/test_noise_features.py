from datetime import date
from math import log10

import pytest
from noise_helpers import osm_node, osm_relation, osm_way, raw_response
from shapely.geometry import LineString

from kazan_noise.features import (build_features, building_height,
                                  parse_height, parse_maxspeed, rail_source,
                                  road_source, runway_sources)
from kazan_noise.weights import OFF

LINE = LineString([(0, 0), (100, 0)])
TODAY = date(2026, 10, 27)


def box(lon, lat, d=0.001):
    return [(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d), (lon, lat)]


CITY = box(49.0, 55.7, 0.2)


def scene_raw(extra_areas=(), extra_lines=(), extra_buildings=()):
    broken = osm_relation(50, [("outer", box(49.1, 55.76)[:-2])], {"building": "yes"})
    return {
        "boundary": raw_response([osm_relation(1, [("outer", CITY)], {"boundary": "administrative"})]),
        "buildings": raw_response(
            [
                osm_way(10, box(49.10, 55.75), {"building": "yes", "height": "12"}),
                broken,
                osm_way(11, [(49.11, 55.75), (49.12, 55.75)], {"barrier": "wall"}),
                *extra_buildings,
            ]
        ),
        "lines": raw_response(
            [
                osm_way(20, [(49.1, 55.8), (49.11, 55.8)], {"highway": "trunk"}),
                osm_way(21, [(49.1, 55.81), (49.11, 55.81)], {"railway": "tram"}),
                *extra_lines,
            ]
        ),
        "areas": raw_response(
            [
                osm_way(30, box(49.05, 55.72), {"leisure": "park"}),
                osm_node(31, 55.78, 49.12, {"amenity": "bar", "name": "Бар"}),
                osm_way(32, box(49.13, 55.72), {"landuse": "construction"}, "2022-01-01T00:00:00Z"),
                osm_way(33, box(49.14, 55.72), {"landuse": "construction"}, "2026-05-01T00:00:00Z"),
                *extra_areas,
            ]
        ),
    }


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"height": "12"}, 12.0),
        ({"height": "12 m"}, 12.0),
        ({"height": "12,5"}, 12.5),
        ({"building:levels": "5"}, 16.0),
        ({"height": "abc", "building:levels": "2"}, 7.0),
        ({}, None),
    ],
)
def test_parse_height(tags, expected):
    assert parse_height(tags) == expected


@pytest.mark.parametrize(
    "tags, area, expected",
    [
        ({"building": "garage"}, 20, 3.0),
        ({"building": "apartments"}, 900, 27.0),
        ({"building": "yes"}, 50, 3.0),
        ({"building": "yes"}, 200, 7.0),
        ({"building": "yes"}, 1000, 15.0),
        ({"building": "yes"}, 5000, 21.0),
        ({"building": "roof"}, 500, None),
        ({"building": "construction"}, 500, 3.0),
        ({"building": "yes", "building:levels": "9"}, 50, 28.0),
        ({"building": "garage", "height": "5"}, 20, 5.0),
    ],
)
def test_building_height(tags, area, expected):
    assert building_height(tags, area) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("60", 60.0),
        ("RU:urban", 60.0),
        ("RU:rural", 90.0),
        ("RU:motorway", 110.0),
        ("40 mph", pytest.approx(64.36)),
        ("walk", None),
        (None, None),
    ],
)
def test_parse_maxspeed(value, expected):
    assert parse_maxspeed(value) == expected


def test_trunk_weight_uses_lanes_speed_and_heavy_traffic():
    s = road_source({"highway": "trunk", "lanes": "6", "maxspeed": "80", "name": "проспект Победы"}, LINE, "way/1")
    assert s.w_day == pytest.approx(10 * log10(3000 * 6 / 4) + 20 * log10(80) + 14 + 3)
    assert s.w_night == pytest.approx(10 * log10(600 * 6 / 4) + 20 * log10(80) + 14 + 3)
    assert (s.kind, s.subtype, s.name, s.radius, s.height) == ("road", "trunk", "проспект Победы", 1000, 0.5)


def test_link_has_reduced_flow_and_bad_lanes_fall_back():
    s = road_source({"highway": "primary_link", "lanes": "x"}, LINE, "way/1")
    assert s.w_day == pytest.approx(10 * log10(1500 * 0.3) + 20 * log10(60) + 14)
    assert s.subtype == "primary_link"


@pytest.mark.parametrize("lanes", ["nan", "inf", "0", "-2"])
def test_non_finite_or_non_positive_lanes_fall_back_to_default(lanes):
    s = road_source({"highway": "primary", "lanes": lanes}, LINE, "way/1")
    assert s.w_day == pytest.approx(10 * log10(1500) + 20 * log10(60) + 14)
    assert s.w_night == pytest.approx(10 * log10(250) + 20 * log10(60) + 14)


@pytest.mark.parametrize("extra", [{"tunnel": "yes"}, {"layer": "-1"}, {"covered": "yes"}])
def test_underground_roads_are_not_sources(extra):
    assert road_source({"highway": "primary", **extra}, LINE, "way/1") is None


def test_tunnel_no_is_surface_and_bad_layer_is_zero():
    assert road_source({"highway": "primary", "tunnel": "no", "layer": "x"}, LINE, "way/1") is not None


def test_bridge_raises_source_height():
    assert road_source({"highway": "primary", "bridge": "yes"}, LINE, "way/1").height == 6.5


def test_rail_siding_is_10_db_quieter():
    main, siding = (rail_source({"railway": "rail", **t}, LINE, "way/1") for t in ({}, {"service": "siding"}))
    assert main.w_day - siding.w_day == pytest.approx(10.0)


def test_rail_kinds_names_and_underground():
    tram = rail_source({"railway": "tram", "ref": "5"}, LINE, "way/1")
    assert (tram.kind, tram.name) == ("tram", "5")
    assert tram.w_day == pytest.approx(10 * log10(10) + 20 * log10(40) + 26)
    assert rail_source({"railway": "subway", "tunnel": "yes"}, LINE, "way/1") is None
    assert rail_source({"railway": "subway"}, LINE, "way/1").kind == "tram"


def test_zero_flow_switches_mode_off():
    s = runway_sources(LineString([(0, 0), (2500, 0)]), {"icao": "UWKG"}, "way/9")[0]
    assert s.w_night == OFF and s.w_day == pytest.approx(10 * log10(0.3) + 86)


def test_runway_gives_runway_and_two_glide_corridors():
    sources = runway_sources(LineString([(0, 0), (2500, 0)]), {"icao": "UWKD"}, "way/9")
    assert [s.subtype for s in sources] == ["runway", "corridor", "corridor"]
    assert [round(s.geom.length) for s in sources[1:]] == [8000, 8000]
    assert sources[0].w_day == pytest.approx(10 * log10(6) + 86) and sources[0].radius == 3000


def test_build_features_drops_stale_construction_and_counts_layers():
    raw = scene_raw()
    features, report = build_features(raw, today=TODAY)
    assert [s.kind for s in features.sources].count("construction") == 1
    assert report.skipped["construction:stale"] == 1
    assert report.skipped["geometry"] == 1
    assert features.obstacles.is_building.tolist() == [True, False]
    assert features.osm_timestamp == "2026-10-26T18:41:36Z"
    assert features.boundary.contains(features.obstacles.geoms[0])
    assert features.obstacles.heights.tolist() == [12.0, 2.5]
    assert report.counts["buildings"] == 1 and report.counts["barriers"] == 1 and report.counts["green"] == 1
    assert features.green.factors.tolist() == [0.5]
    assert report.counts["source:road"] == 1 and report.counts["source:tram"] == 1
    assert report.counts["source:bar"] == 1 and report.counts["source:construction"] == 1


def test_unknown_highway_is_counted():
    raw = scene_raw(extra_lines=[osm_way(22, [(49.1, 55.82), (49.11, 55.82)], {"highway": "track"})])
    _, report = build_features(raw, today=TODAY)
    assert report.skipped["highway:other"] == 1


def test_bar_way_becomes_centroid_and_worship_is_day_only():
    raw = scene_raw(
        extra_areas=[
            osm_way(40, box(49.15, 55.78), {"amenity": "pub"}),
            osm_way(41, box(49.16, 55.78), {"amenity": "place_of_worship"}),
            osm_way(42, box(49.16, 55.78)[:-1], {"amenity": "bar"}),
        ]
    )
    features, report = build_features(raw, today=TODAY)
    pub = next(s for s in features.sources if s.osm_id == "way/40")
    assert pub.geom.geom_type == "Point" and pub.kind == "bar" and pub.subtype == "pub"
    church = next(s for s in features.sources if s.osm_id == "way/41")
    assert (church.kind, church.w_night, church.height, church.radius) == ("worship", OFF, 20.0, 300)
    assert report.skipped["geometry"] == 2


def test_building_construction_is_obstacle_and_area_source():
    site = osm_way(34, box(49.14, 55.74), {"building": "construction"}, "2026-06-01T00:00:00Z")
    raw = scene_raw(
        extra_buildings=[osm_way(12, box(49.13, 55.75), {"building": "construction"})],
        extra_areas=[site],
    )
    features, _ = build_features(raw, today=TODAY)
    assert features.obstacles.heights.tolist() == [12.0, 3.0, 2.5]
    assert [s.osm_id for s in features.sources if s.kind == "construction"] == ["way/33", "way/34"]


def test_construction_without_timestamp_stays_and_industrial_has_flat_night_drop():
    raw = scene_raw(
        extra_areas=[
            osm_way(35, box(49.17, 55.72), {"landuse": "construction"}),
            osm_way(36, box(49.18, 55.72), {"landuse": "industrial"}),
        ]
    )
    features, _ = build_features(raw, today=TODAY)
    assert "way/35" in [s.osm_id for s in features.sources]
    ind = next(s for s in features.sources if s.kind == "industrial")
    assert (ind.w_day, ind.w_night, ind.height, ind.radius) == (60, 55, 5.0, 1000)


def test_runway_is_matched_to_aerodrome_by_icao():
    aero = osm_way(60, box(49.2, 55.6, 0.05), {"aeroway": "aerodrome", "icao": "UWKG"})
    runway = osm_way(61, [(49.21, 55.62), (49.23, 55.62)], {"aeroway": "runway"})
    features, _ = build_features(scene_raw(extra_areas=[aero], extra_lines=[runway]), today=TODAY)
    air = [s for s in features.sources if s.kind == "aircraft"]
    assert [s.subtype for s in air] == ["runway", "corridor", "corridor"]
    assert air[0].w_day == pytest.approx(10 * log10(0.3) + 86)
