import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from hotel_agent.apis.kudago.client import EventsClient
from hotel_agent.apis.kudago.models import EventsError
from kudago_events.parse import EventRow, Session
from kudago_events.store import apply_migrations, connect, finish_load, replace_events, start_load

pytestmark = pytest.mark.db

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 28)
CHECKIN, CHECKOUT = date(2026, 11, 19), date(2026, 11, 22)


def at(month: int, day: int, hour: int = 19) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=MSK)


def row(event_id: int, *sessions: Session, **fields) -> EventRow:
    base = {
        "event_id": event_id,
        "title": f"Событие {event_id}",
        "slug": None,
        "site_url": f"https://kzn.kudago.com/event/e{event_id}/",
        "description": None,
        "body_text": None,
        "categories": ["concert"],
        "tags": [],
        "age_restriction": None,
        "price": None,
        "is_free": None,
        "favorites_count": 0,
        "place_title": None,
        "place_address": None,
        "place_lat": None,
        "place_lon": None,
        "place_url": None,
        "raw": {"id": event_id},
    }
    return EventRow(**{**base, **fields}, sessions=list(sessions))


CONCERT = row(
    1,
    Session(at(11, 20), at(11, 20, 21)),
    Session(at(12, 5), at(12, 5, 21)),
    title="Концерт «Пикник»",
    favorites_count=10,
    categories=["concert"],
    age_restriction="16+",
    price="от 2500 рублей",
    is_free=False,
    description="Коротко.",
    body_text="Полное описание концерта.",
    place_title="Татнефть Арена",
    place_address="ул. Чистопольская, 42",
    place_lat=55.82,
    place_lon=49.13,
)
EXHIBITION = row(
    2,
    Session(at(6, 3, 0), at(12, 31, 0)),
    title="Выставка «Луна»",
    categories=["exhibition"],
    favorites_count=5,
    place_title="Галерея современного искусства",
)
DAILY_TOUR = row(3, *[Session(at(11, d, 11), at(11, d, 13)) for d in range(10, 22)], categories=["tour"])


def fill(dsn: str, rows: list[EventRow], *, loaded: bool = True) -> None:
    with connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS kudago CASCADE")
        conn.commit()
        apply_migrations(conn)
        load_id = start_load(
            conn, started_at=datetime(2026, 10, 28, 9, tzinfo=UTC), window_from=TODAY, window_to=date(2026, 12, 27)
        )
        replace_events(conn, rows)
        finish_load(conn, load_id, status="ok" if loaded else "failed", events=len(rows))
        conn.commit()


@pytest.fixture
def dsn() -> str:
    dsn = os.environ.get("OSTROVOK_TEST_DSN")
    if not dsn:
        pytest.skip("OSTROVOK_TEST_DSN не задан")
    fill(dsn, [CONCERT, EXHIBITION, DAILY_TOUR])
    return dsn


async def test_events_for_stay_picks_one_off_events_and_exhibitions(dsn):
    result = await EventsClient(dsn).events_for_stay(CHECKIN, CHECKOUT)

    assert result.period == "2026-11-19 — 2026-11-22"
    [concert] = result.events
    assert (concert.id, concert.title, concert.sessions) == (1, "Концерт «Пикник»", ["2026-11-20 19:00"])
    assert (concert.place, concert.price, concert.age) == ("Татнефть Арена", "от 2500 рублей", "16+")
    assert concert.url == "https://kzn.kudago.com/event/e1/"
    [exhibition] = result.exhibitions
    assert (exhibition.id, exhibition.until, exhibition.place) == (
        2,
        date(2026, 12, 31),
        "Галерея современного искусства",
    )
    assert result.note is None
    assert "2026-10-28" in result.data and "url" in result.data


async def test_visit_beyond_loaded_window_gets_a_note(dsn):
    result = await EventsClient(dsn).events_for_stay(date(2026, 12, 25), date(2027, 1, 3))
    assert "2026-12-27" in result.note


async def test_nothing_loaded_yet_is_an_events_error(dsn):
    fill(dsn, [CONCERT], loaded=False)
    with pytest.raises(EventsError, match="не загружены"):
        await EventsClient(dsn).events_for_stay(CHECKIN, CHECKOUT)


async def test_missing_schema_means_not_loaded(dsn):
    with connect(dsn) as conn:
        conn.execute("DROP SCHEMA kudago CASCADE")
    with pytest.raises(EventsError, match="не загружены"):
        await EventsClient(dsn).events_for_stay(CHECKIN, CHECKOUT)


async def test_unreachable_database_is_an_events_error():
    client = EventsClient("postgresql://nobody:nobody@127.0.0.1:1/none", connect_timeout=1)
    with pytest.raises(EventsError, match="недоступны"):
        await client.events_for_stay(CHECKIN, CHECKOUT)


async def test_event_details_for_visit(dsn):
    details = await EventsClient(dsn).event_details(1, checkin=CHECKIN, checkout=CHECKOUT, today=TODAY)

    assert (details.id, details.title, details.categories) == (1, "Концерт «Пикник»", ["concert"])
    assert details.description == "Полное описание концерта."
    assert details.sessions == ["2026-11-20 19:00"]
    assert details.until is None
    assert (details.place.title, details.place.address, details.place.lat) == (
        "Татнефть Арена",
        "ул. Чистопольская, 42",
        55.82,
    )
    assert (details.price, details.is_free, details.age) == ("от 2500 рублей", False, "16+")
    assert details.url == "https://kzn.kudago.com/event/e1/"


async def test_event_details_without_dates_lists_upcoming_sessions(dsn):
    details = await EventsClient(dsn).event_details(1, today=date(2026, 11, 21))
    assert details.sessions == ["2026-12-05 19:00"]


async def test_event_details_of_exhibition_has_until(dsn):
    details = await EventsClient(dsn).event_details(2, checkin=CHECKIN, checkout=CHECKOUT, today=TODAY)
    assert details.until == date(2026, 12, 31)
    assert details.sessions == ["2026-06-03 — 2026-12-31"]


async def test_event_details_upcoming_sessions_are_limited_to_ten(dsn):
    details = await EventsClient(dsn).event_details(3, today=TODAY)
    assert len(details.sessions) == 10 and details.sessions[0] == "2026-11-10 11:00"


async def test_long_description_is_shortened(dsn):
    fill(dsn, [row(9, Session(at(11, 20), at(11, 20, 21)), body_text="а" * 5000)])
    details = await EventsClient(dsn).event_details(9, today=TODAY)
    assert 1900 < len(details.description) <= 2001 and details.description.endswith("…")


async def test_unknown_event_is_an_events_error(dsn):
    with pytest.raises(EventsError, match="не найдено"):
        await EventsClient(dsn).event_details(404, today=TODAY)
