from datetime import date

import pytest

from ostrovok_scraper import domain


@pytest.mark.parametrize(
    ("night", "expected"),
    [(date(2026, 11, 17), "weekday"), (date(2026, 11, 19), "weekday"), (date(2026, 11, 20), "weekend"),
     (date(2026, 11, 21), "weekend"), (date(2026, 11, 22), "weekday")],
)
def test_night_type_friday_and_saturday_are_weekend(night, expected):
    assert domain.night_type(night) == expected


@pytest.mark.parametrize(
    ("guests", "paxes"),
    [(1, [{"adults": 1}]), (6, [{"adults": 6}]),
     (7, [{"adults": 6, "child_ages": [16]}]), (8, [{"adults": 6, "child_ages": [16, 16]}])],
)
def test_paxes_for_guests(guests, paxes):
    assert domain.paxes_for_guests(guests) == paxes


@pytest.mark.parametrize("guests", [0, 9])
def test_paxes_for_guests_rejects_out_of_range(guests):
    with pytest.raises(ValueError):
        domain.paxes_for_guests(guests)


def test_window_nights_inclusive_and_limited():
    assert domain.window_nights(date(2026, 11, 28), date(2026, 11, 29)) == [date(2026, 11, 28), date(2026, 11, 29)]
    assert domain.window_nights(date(2026, 10, 27), date(2026, 11, 29), limit=2) == [date(2026, 10, 27), date(2026, 10, 28)]
    assert domain.window_nights(date(2026, 12, 1), date(2026, 11, 29)) == []


def test_pick_reference_night_prefers_ref_then_nearest_same_type():
    ref = date(2026, 11, 17)  # вторник
    assert domain.pick_reference_night({ref, date(2026, 11, 18)}, ref) == ref
    # 16.11 (пн) и 18.11 (ср) одинаково близко — берём более раннюю; 20.11 (пт) другого типа
    assert domain.pick_reference_night({date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 20)}, ref) == date(2026, 11, 16)
    assert domain.pick_reference_night({date(2026, 11, 20), date(2026, 11, 21)}, ref) is None
    assert domain.pick_reference_night(set(), ref) is None


def test_keys_roundtrip():
    key = domain.rates_key("don_kikhot_hotel_", date(2026, 11, 17), 4)
    assert key == "hotel=don_kikhot_hotel_;night=2026-11-17;guests=4"
    assert domain.parse_key(key) == {"hotel": "don_kikhot_hotel_", "night": "2026-11-17", "guests": "4"}
    assert domain.serp_key(date(2026, 11, 17), 2) == "night=2026-11-17;guests=2"
    assert domain.hotel_key("x") == "hotel=x"
    assert domain.child_age_key("x", date(2026, 11, 17)) == "hotel=x;night=2026-11-17"
    assert domain.reviews_key("x", 2) == "hotel=x;page=2"
