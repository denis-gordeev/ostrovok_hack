from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.reviews import parse_api_reviews, parse_page_reviews, parse_review_date

AUTHOR_FIELDS = {"author_name", "author_residency", "authorName", "authorResidency"}


def test_parse_review_date_short_offset():
    assert parse_review_date("2021-05-12 06:42:23.080211+00") == datetime(2021, 5, 12, 6, 42, 23, 80211, tzinfo=timezone.utc)


def test_parse_api_reviews():
    rows, skipped = parse_api_reviews(load_fixture("reviews_api.json"), hotel_id="don_kikhot_hotel_")
    assert skipped == 1  # без id
    first, second, third = rows
    assert (first.review_id, first.rating, first.traveller_type, first.likes) == (2164782, Decimal("10"), "family", 2)
    assert (first.stay_check_in, first.stay_check_out, first.adults) == (date(2026, 6, 7), date(2026, 6, 8), 2)
    assert first.room_name == "Двухместный номер на мансарде"
    assert first.hotel_reply is None and first.minus is None
    assert second.hotel_reply == "Спасибо за отзыв!"
    assert (third.stay_check_in, third.stay_check_out) == (date(2023, 5, 27), date(2023, 5, 29))
    for row in rows:
        assert not AUTHOR_FIELDS & set(asdict(row)) and "Автор" not in repr(row) and "xx-xx" not in repr(row)


def test_parse_page_reviews():
    page = load_fixture("hotel_page.json")
    rows, skipped = parse_page_reviews(page["reviews"], hotel_id="don_kikhot_hotel_")
    assert skipped == 0
    assert [(r.review_id, r.traveller_type, r.trip_type, r.hotel_reply) for r in rows] == [
        (1952999, "couple", "leisure", None), (1315975, "family", "leisure", "Спасибо!")]
    assert rows[1].images == ["https://cdn.ostrovok.ru/t/{size}/rv1.JPEG"]
    assert all("Автор" not in repr(r) for r in rows)


def test_malformed_items_are_skipped():
    body = {"reviews": [{"id": 1, "review_date": "2021-01-01 00:00:00+00", "reply": "oops"},
                        {"id": 2, "review_date": "2021-01-01 00:00:00+00", "order_context": []}]}
    rows, skipped = parse_api_reviews(body, hotel_id="h")
    assert rows == [] and skipped == 2


def test_unparsable_stay_date_degrades_to_none():
    body = {"reviews": [{"id": 1, "review_date": "2021-01-01 00:00:00+00",
                         "order_context": {"check_in": "not-a-date", "check_out": "2021-01-03"}}]}
    rows, skipped = parse_api_reviews(body, hotel_id="h")
    assert skipped == 0 and len(rows) == 1
    assert (rows[0].stay_check_in, rows[0].stay_check_out) == (None, date(2021, 1, 3))


def test_page_reply_accepts_strings_and_dicts():
    """На живых страницах ответ отеля — список строк; старую форму со словарём тоже понимаем."""
    base = {"id": 1, "reviewDate": "2025-08-01 09:00:00+00", "orderContext": {}}
    rows, skipped = parse_page_reviews(
        {"list": [{**base, "reply": ["Спасибо!"]}, {**base, "id": 2, "reply": [{"text": "Ждём снова"}]}]},
        hotel_id="x",
    )
    assert skipped == 0
    assert [r.hotel_reply for r in rows] == ["Спасибо!", "Ждём снова"]
