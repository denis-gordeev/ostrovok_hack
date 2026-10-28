from datetime import date, datetime, timezone
from decimal import Decimal

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.rates import free_child_age, parse_rates


def rates_by_hash():
    rows, skipped = parse_rates(
        load_fixture("rates_2.json"), hotel_id="don_kikhot_hotel_", night=date(2026, 11, 17), requested_guests=2
    )
    return {r.source_rate_hash: r for r in rows}, skipped


def test_parse_rates_dedupes_and_skips_broken():
    rows, skipped = rates_by_hash()
    assert set(rows) == {"sr-a", "sr-c", "sr-d"}  # sr-b дороже sr-a с тем же ключом
    assert skipped == 2  # sr-e без payment_types, sr-f без rooms


def test_parse_rates_refundable_rate():
    r = rates_by_hash()[0]["sr-a"]
    assert (r.rg_hash, r.meal, r.has_breakfast, r.payment_type, r.price) == (
        "3202302",
        "nomeal",
        False,
        "now",
        Decimal("5609.00"),
    )
    assert (r.night_type, r.requested_guests, r.occupancy, r.allotment) == ("weekday", 2, 2, 12)
    assert r.free_cancellation_before == datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)
    assert r.free_cancellation_hours == Decimal("34.0")  # 00:00 МСК 17.11 = 16.11 21:00 UTC
    assert r.penalty_after_ratio == Decimal("1.0000")
    assert (r.no_show_penalty, r.no_show_nights, r.discount_pct) == (Decimal("5609.00"), 1, Decimal("2"))


def test_parse_rates_non_refundable_rate():
    r = rates_by_hash()[0]["sr-c"]
    assert (r.meal, r.has_breakfast, r.payment_type) == ("breakfast", True, "hotel")
    assert r.free_cancellation_before is None and r.free_cancellation_hours is None
    assert r.penalty_after_ratio == Decimal("1.0000")


def test_parse_rates_null_rates():
    assert parse_rates({"rates": None}, hotel_id="x", night=date(2026, 11, 20), requested_guests=8) == ([], 0)


def test_free_child_age():
    assert free_child_age({"rates": [{"occupancy": 2}, {"occupancy": 3}]}) == 5
    assert free_child_age({"rates": [{"occupancy": 3}]}) == 3
    assert free_child_age({"rates": None}) == 3


def _rate(**changes):
    rate = {
        "payment_options": {"payment_types": [{"type": "now", "amount": "100.00"}]},
        "rooms": [{"rg_hash": "1"}],
    }
    return rate | changes


def test_parse_rates_skips_non_dict_meal_data_and_nan_price():
    bad_meals = _rate(meal_data={"meals": ["x"]})
    nan = _rate(payment_options={"payment_types": [{"type": "now", "amount": "NaN"}]})
    rows, skipped = parse_rates(
        {"rates": [bad_meals, nan, _rate()]}, hotel_id="x", night=date(2026, 11, 17), requested_guests=2
    )
    assert len(rows) == 1 and skipped == 2


def test_parse_rates_skips_missing_payment_type():
    rate = _rate(payment_options={"payment_types": [{"type": None, "amount": "100.00"}]})
    assert parse_rates({"rates": [rate]}, hotel_id="x", night=date(2026, 11, 17), requested_guests=2) == ([], 1)
