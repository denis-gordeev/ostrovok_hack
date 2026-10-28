from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from kudago_helpers import WINDOW_END, WINDOW_START, api_date, api_event, msk

from kudago_events.api import KudagoApiError
from kudago_events.config import KudagoConfig
from kudago_events.load import LOCK_KEY, refresh_if_stale

pytestmark = pytest.mark.db

# 28.10.2026 15:00 по Москве.
NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


class FakeApi:
    """Вместо KudagoApi: отдаёт события по порядку; исключение в списке выбрасывается на его месте."""

    def __init__(self, *items):
        self.items = items
        self.calls: list[tuple[datetime, datetime]] = []

    def events(self, since: datetime, until: datetime):
        self.calls.append((since, until))
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item


def config(dsn: str) -> KudagoConfig:
    return KudagoConfig(_env_file=None, db_dsn=dsn)


def stored(dsn: str) -> dict[int, list[tuple]]:
    """event_id → сеансы (start_at, end_at) из kudago."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT e.event_id, d.start_at, d.end_at FROM kudago.events e"
            " JOIN kudago.event_dates d USING (event_id) ORDER BY e.event_id, d.start_at NULLS FIRST"
        ).fetchall()
    result: dict[int, list[tuple]] = {}
    for event_id, start, end in rows:
        result.setdefault(event_id, []).append((start, end))
    return result


def loads(dsn: str) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT status, window_from, window_to, events, error FROM kudago.loads ORDER BY load_id"
        ).fetchall()


TWO_SESSIONS = api_event(
    2,
    title="Спектакль «Гроза»",
    dates=[
        api_date(msk(2026, 11, 20, 19), msk(2026, 11, 20, 22)),
        api_date(msk(2026, 11, 21, 19), msk(2026, 11, 21, 22)),
    ],
)


def test_first_start_creates_schema_and_loads_events_in_window(dsn):
    api = FakeApi(api_event(1), TWO_SESSIONS, api_event(3, site_url=None))

    result = refresh_if_stale(config(dsn), now=NOW, api=api)

    assert (result.status, result.events) == ("loaded", 2)
    assert api.calls == [(WINDOW_START, WINDOW_END)]
    events = stored(dsn)
    assert sorted(events) == [1, 2]
    assert events[2] == [
        (datetime(2026, 11, 20, 16, tzinfo=UTC), datetime(2026, 11, 20, 19, tzinfo=UTC)),
        (datetime(2026, 11, 21, 16, tzinfo=UTC), datetime(2026, 11, 21, 19, tzinfo=UTC)),
    ]
    assert loads(dsn) == [("ok", date(2026, 10, 28), date(2026, 12, 27), 2, None)]


def test_fresh_data_is_not_reloaded(dsn):
    refresh_if_stale(config(dsn), now=NOW, api=FakeApi(api_event(1)))
    api = FakeApi(api_event(5))

    result = refresh_if_stale(config(dsn), now=NOW + timedelta(hours=23), api=api)

    assert (result.status, result.events) == ("fresh", 1)
    assert api.calls == []
    assert sorted(stored(dsn)) == [1]


def test_stale_data_is_replaced(dsn):
    refresh_if_stale(config(dsn), now=NOW, api=FakeApi(api_event(1), TWO_SESSIONS))

    result = refresh_if_stale(config(dsn), now=NOW + timedelta(hours=25), api=FakeApi(api_event(5)))

    assert (result.status, result.events) == ("loaded", 1)
    assert sorted(stored(dsn)) == [5]


def test_failed_load_keeps_previous_events_and_is_logged(dsn):
    refresh_if_stale(config(dsn), now=NOW, api=FakeApi(api_event(1), TWO_SESSIONS))
    api = FakeApi(api_event(5), KudagoApiError("403 Forbidden: нужен российский IP", status_code=403))

    result = refresh_if_stale(config(dsn), now=NOW + timedelta(days=2), api=api)

    assert result.status == "failed" and "403" in result.error
    assert sorted(stored(dsn)) == [1, 2]
    assert [row[0] for row in loads(dsn)] == ["ok", "failed"]
    assert "403" in loads(dsn)[-1][4]


def test_failed_load_does_not_count_as_fresh(dsn):
    refresh_if_stale(config(dsn), now=NOW, api=FakeApi(KudagoApiError("сбой")))
    api = FakeApi(api_event(1))

    result = refresh_if_stale(config(dsn), now=NOW + timedelta(minutes=5), api=api)

    assert result.status == "loaded"


def test_load_is_skipped_while_another_process_holds_the_lock(dsn):
    api = FakeApi(api_event(1))
    with psycopg.connect(dsn) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        result = refresh_if_stale(config(dsn), now=NOW, api=api)
    assert result.status == "busy"
    assert api.calls == []


def test_unreachable_database_is_reported_not_raised():
    cfg = KudagoConfig(_env_file=None, db_dsn="postgresql://nobody:nobody@127.0.0.1:1/none")
    result = refresh_if_stale(cfg, now=NOW, api=FakeApi(api_event(1)))
    assert result.status == "failed" and result.error
