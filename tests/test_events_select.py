from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from hotel_agent.apis.kudago.models import EventSession, StoredEvent
from hotel_agent.apis.kudago.select import format_session, select_events, until_date

MSK = ZoneInfo("Europe/Moscow")
# Визит 19–22 ноября: дни с заезда по выезд включительно.
CHECKIN, CHECKOUT = date(2026, 11, 19), date(2026, 11, 22)


def at(day: int, hour: int = 19, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


def show(day: int, hour: int = 19, month: int = 11) -> EventSession:
    """Короткий сеанс на 2 часа."""
    start = at(day, hour, month=month)
    return EventSession(start, start + timedelta(hours=2))


def event(event_id: int, *sessions: EventSession, title: str | None = None, categories=("concert",), favorites=0):
    return StoredEvent(
        event_id=event_id,
        title=title or f"Событие {event_id}",
        site_url=f"https://kzn.kudago.com/event/e{event_id}/",
        categories=list(categories),
        favorites_count=favorites,
        sessions=list(sessions),
    )


def pick(*events):
    return select_events(list(events), checkin=CHECKIN, checkout=CHECKOUT)


def one_off_ids(selection) -> list[int]:
    return [e.event_id for e, _ in selection.one_off]


EXHIBITION = (EventSession(at(3, 0, month=6), at(31, 0, month=12)),)


def test_one_off_event_in_visit_is_picked_with_its_sessions_in_visit():
    concert = event(1, show(18), show(20), show(25))
    [(picked, sessions)] = pick(concert).one_off
    assert picked.event_id == 1
    assert sessions == [show(20)]


def test_visit_covers_checkin_to_checkout_day_inclusive():
    selection = pick(
        event(1, show(18, 20)),  # накануне заезда
        event(2, show(19, 0)),  # в полночь дня заезда
        event(3, show(22, 19)),  # вечер дня выезда
        event(4, show(23, 0)),  # следующий день после выезда
    )
    assert sorted(one_off_ids(selection)) == [2, 3]


def test_long_exhibition_goes_to_exhibitions():
    selection = pick(event(1, *EXHIBITION, categories=["exhibition"]))
    assert [e.event_id for e in selection.exhibitions] == [1]
    assert selection.one_off == []


def test_long_event_that_is_not_an_exhibition_is_dropped():
    """Постоянный квест или акция на месяцы — не «событие во время визита»."""
    selection = pick(event(1, *EXHIBITION, categories=["quest"]))
    assert (selection.one_off, selection.exhibitions) == ([], [])


def test_open_ended_session_counts_as_long():
    selection = pick(event(1, EventSession(None, None), categories=["exhibition"]))
    assert [e.event_id for e in selection.exhibitions] == [1]


def test_regular_event_with_many_sessions_is_dropped():
    daily_tour = event(1, *[show(d, 11) for d in range(12, 23)], categories=["tour"])
    weekly_play = event(2, *[show(d, 19) for d in (5, 12, 19, 26)], categories=["theater"])
    assert one_off_ids(pick(daily_tour, weekly_play)) == [2]


def test_ten_sessions_still_count_as_one_off():
    assert one_off_ids(pick(event(1, *[show(d) for d in range(13, 23)]))) == [1]


def test_promotions_and_business_events_are_dropped():
    selection = pick(event(1, show(20), categories=["stock"]), event(2, show(20), categories=["business-events"]))
    assert selection.one_off == []


def test_one_off_events_ranked_by_favorites_then_date_and_limited_to_five():
    events = [event(i, show(20), favorites=i * 10) for i in range(1, 7)]
    events.append(event(7, show(19), favorites=30))  # столько же, сколько у 3, но раньше
    assert one_off_ids(pick(*events)) == [6, 5, 4, 7, 3]


def test_exhibitions_ranked_by_favorites_and_limited_to_two():
    selection = pick(*[event(i, *EXHIBITION, categories=["exhibition"], favorites=i) for i in range(1, 4)])
    assert [e.event_id for e in selection.exhibitions] == [3, 2]


def test_same_title_is_shown_once():
    selection = pick(
        event(1, show(20), title="Щелкунчик", favorites=5),
        event(2, show(21), title="щелкунчик ", favorites=9),
    )
    assert one_off_ids(selection) == [2]


def test_format_session_uses_moscow_time_and_hides_midnight():
    assert format_session(EventSession(datetime(2026, 11, 20, 16, tzinfo=ZoneInfo("UTC")), None)) == "2026-11-20 19:00"
    assert format_session(EventSession(at(20, 0), at(20, 0))) == "2026-11-20"


def test_until_date_is_last_end_in_moscow_or_none_when_open():
    assert until_date(event(1, *EXHIBITION)) == date(2026, 12, 31)
    assert until_date(event(1, EventSession(at(1), None))) is None
