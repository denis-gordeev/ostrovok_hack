from datetime import UTC, datetime

import pytest
from kudago_helpers import ENDLESS, STARTLESS, WINDOW_END, WINDOW_START, api_date, api_event, msk

from kudago_events.parse import Session, parse_event


def parse(raw: dict):
    return parse_event(raw, window_start=WINDOW_START, window_end=WINDOW_END)


def test_parses_event_fields_and_place():
    row = parse(api_event(7))
    assert row.event_id == 7
    assert row.title == "Концерт группы «Пикник»"
    assert row.site_url == "https://kzn.kudago.com/event/event-7/"
    assert (row.categories, row.tags) == (["concert"], ["рок"])
    assert (row.age_restriction, row.price, row.is_free, row.favorites_count) == (
        "16+",
        "от 2500 до 9000 рублей",
        False,
        42,
    )
    assert (row.description, row.body_text) == ("Легендарная рок-группа.", "Полное описание концерта.")
    assert (row.place_title, row.place_address) == ("Татнефть Арена", "ул. Чистопольская, 42")
    assert (row.place_lat, row.place_lon) == (55.8205, 49.1268)
    assert row.place_url == "https://kzn.kudago.com/place/tatneft-arena/"
    assert row.raw["id"] == 7


def test_session_times_are_utc_moments():
    [session] = parse(api_event()).sessions
    # 19:00 и 21:00 по Москве — 16:00 и 18:00 UTC.
    assert session == Session(datetime(2026, 11, 20, 16, tzinfo=UTC), datetime(2026, 11, 20, 18, tzinfo=UTC))


def test_sentinel_dates_become_open_ends():
    raw = api_event(dates=[api_date(STARTLESS, ENDLESS)])
    assert parse(raw).sessions == [Session(None, None)]


def test_startless_and_endless_flags_open_the_ends():
    raw = api_event(dates=[api_date(msk(2026, 6, 3), msk(2026, 12, 31), is_startless=True, is_endless=True)])
    assert parse(raw).sessions == [Session(None, None)]


def test_end_before_start_is_clamped_to_start():
    raw = api_event(dates=[api_date(msk(2026, 11, 20, 19), msk(2026, 11, 20, 18))])
    [session] = parse(raw).sessions
    assert session.end_at == session.start_at == datetime(2026, 11, 20, 16, tzinfo=UTC)


def test_keeps_only_sessions_touching_the_window():
    raw = api_event(
        dates=[
            api_date(msk(2026, 6, 1, 19), msk(2026, 6, 1, 21)),  # прошло
            api_date(msk(2026, 6, 3), msk(2026, 12, 31)),  # идёт с июня — пересекает окно
            api_date(msk(2026, 11, 2, 12), msk(2026, 11, 2, 12)),  # точка внутри окна
            api_date(msk(2026, 12, 28), msk(2026, 12, 28, 2)),  # начинается ровно в конце окна
            api_date(msk(2027, 1, 15, 19), msk(2027, 1, 15, 21)),  # после окна
        ]
    )
    starts = [s.start_at for s in parse(raw).sessions]
    assert starts == [datetime(2026, 6, 2, 21, tzinfo=UTC), datetime(2026, 11, 2, 9, tzinfo=UTC)]


def test_event_without_sessions_in_window_is_skipped():
    assert parse(api_event(dates=[api_date(msk(2026, 6, 1, 19), msk(2026, 6, 1, 21))])) is None
    assert parse(api_event(dates=[])) is None


def test_event_without_site_url_is_skipped():
    """Лицензия KudaGo требует ссылку на событие: без неё показывать нечего."""
    assert parse(api_event(site_url="")) is None
    assert parse(api_event(site_url=None)) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(18, "18+"), (0, "0+"), ("12+", "12+"), ("6", "6+"), (" 16+ ", "16+"), ("", None), (None, None)],
)
def test_age_restriction_is_normalized(value, expected):
    assert parse(api_event(age_restriction=value)).age_restriction == expected


def test_event_without_place_has_empty_place_fields():
    row = parse(api_event(place=None))
    assert (row.place_title, row.place_address, row.place_lat, row.place_lon, row.place_url) == (None,) * 5


def test_unexpanded_place_has_empty_place_fields():
    row = parse(api_event(place={"id": 745}))
    assert (row.place_title, row.place_lat) == (None, None)


def test_blank_texts_become_none_and_counts_default_to_zero():
    row = parse(api_event(description="  ", body_text="", favorites_count=None, tags=None, categories=None))
    assert (row.description, row.body_text, row.favorites_count, row.tags, row.categories) == (None, None, 0, [], [])
