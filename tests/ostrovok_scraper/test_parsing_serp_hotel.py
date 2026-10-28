from datetime import time
from decimal import Decimal

import pytest

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.hotel_page import extract_next_data, parse_city_region, parse_hotel, parse_rooms
from ostrovok_scraper.parsing.serp import MapHotel, parse_map_hotels, parse_session_region


def test_parse_map_hotels_skips_broken_items():
    hotels, skipped = parse_map_hotels(load_fixture("serp_map.json"))
    assert skipped == 1
    assert hotels[0] == MapHotel("don_kikhot_hotel_", 6479652, "russia/kazan", "Отель Дон Кихот", 3, Decimal("5609"))
    assert [h.stars for h in hotels] == [3, 0]


def test_parse_map_hotels_handles_missing_list():
    assert parse_map_hotels({"search_finished": True, "map_hotels": None}) == ([], 0)


def test_parse_session_region():
    region = parse_session_region(load_fixture("serp_map.json")["session_region"])
    assert (region.region_id, region.name, region.name_en, region.type, region.parent_id) == (
        6288539, "Республика Татарстан", "Republic of Tatarstan", "Province (State)", None)


def test_extract_next_data_keeps_only_needed_parts():
    html = '<html><script id="__NEXT_DATA__" type="application/json">' \
           '{"props": {"pageProps": {"hotel": {"id": "x"}, "hotelRegionData": {}, "reviews": {"total": 0}, "uid": "u"}}}' \
           '</script></html>'
    assert extract_next_data(html) == {"hotel": {"id": "x"}, "hotelRegionData": {}, "reviews": {"total": 0}}
    with pytest.raises(ValueError, match="__NEXT_DATA__ not found"):
        extract_next_data("<html></html>")


def test_parse_hotel():
    row = parse_hotel(load_fixture("hotel_page.json"), stars=3)
    assert (row.hotel_id, row.master_id, row.region_id, row.kind, row.stars) == ("don_kikhot_hotel_", 6479652, 1993, "hotel", 3)
    assert (row.check_in_from, row.check_out_until) == (time(14, 0), time(12, 0))
    assert (row.rating, row.rating_count, row.reviews_total) == (Decimal("8.9"), 336, 2)
    assert row.flags == ["has_internet", "has_parking", "has_pets"]
    assert row.metro == [{"name": "Кремлёвская", "distance_m": 877}]
    assert len(row.images) == 5
    assert row.amenities == load_fixture("hotel_page.json")["hotel"]["amenityGroups"]
    assert "checkInCheckOut" not in row.policies and row.policies["extraBed"][0]["price"] == 1600
    assert "visa" not in row.policies and "noShow" not in row.policies
    assert "hotel@example.com" not in repr(row)
    assert (row.free_child_age, row.free_child_age_source) == (3, "default")


def test_parse_rooms_and_city_region():
    rooms = parse_rooms(load_fixture("hotel_page.json"))
    assert [(r.rg_hash, r.name, r.bedding_type, r.size_m2) for r in rooms] == [
        ("3202302", "Двухместный номер Первой категории", "двуспальная кровать", Decimal("25")),
        ("320240200004", "Двухместный номер Первой категории Мансарда", "2 отдельные кровати", Decimal("25")),
    ]
    city = parse_city_region(load_fixture("hotel_page.json"), parent_id=6288539)
    assert (city.region_id, city.name, city.name_en, city.parent_id, city.catalog_slug) == (
        1993, "Казань", "Kazan", 6288539, "russia/kazan")


def test_parse_hotel_degrades_small_defects():
    page = load_fixture("hotel_page.json")
    hotel = page["hotel"]
    hotel["location"]["nearestSubways"] = [{"name": "Без расстояния"}, {"name": "Кремлёвская", "distance": 877}]
    hotel["policies"]["checkInCheckOut"]["value"]["checkInTimeStart"] = {"hours": 24, "minutes": 0}
    hotel["images"] = [{"nope": 1}] + hotel["images"]
    row = parse_hotel(page, stars=3)
    assert row.metro == [{"name": "Кремлёвская", "distance_m": 877}]
    assert row.check_in_from is None and row.check_out_until == time(12, 0)
    assert len(row.images) == 5 and all(isinstance(i, str) for i in row.images)


def test_parse_hotel_null_check_in_check_out():
    page = load_fixture("hotel_page.json")
    page["hotel"]["policies"]["checkInCheckOut"] = None
    row = parse_hotel(page, stars=3)
    assert row.check_in_from is None and row.check_out_until is None


@pytest.mark.parametrize("name", [None, ""])
def test_parse_hotel_rejects_empty_name(name):
    page = load_fixture("hotel_page.json")
    page["hotel"]["name"] = name
    with pytest.raises(ValueError):
        parse_hotel(page, stars=3)
