from datetime import date, datetime, time
from typing import Self

import psycopg
from psycopg.rows import dict_row

from hotel_agent.apis.kudago.models import (
    EventDetails,
    EventItem,
    EventsError,
    EventSession,
    ExhibitionItem,
    LoadInfo,
    PlaceInfo,
    StayEvents,
    StoredEvent,
)
from hotel_agent.apis.kudago.select import (
    format_session,
    is_long,
    overlaps,
    select_events,
    until_date,
    visit_bounds,
)
from hotel_agent.config import Settings
from hotel_agent.dates import MOSCOW_TZ

NOT_LOADED = "События Казани пока не загружены: они загружаются при запуске агента (или командой kudago-events load)."
LINK_NOTE = "У каждого упомянутого события указывай ссылку url."
MAX_DETAIL_SESSIONS = 10
MAX_ONE_OFF_SESSIONS = 3
MAX_DESCRIPTION = 2000

_LOAD_SQL = """
SELECT started_at, window_from, window_to FROM kudago.loads
WHERE status = 'ok' ORDER BY started_at DESC, load_id DESC LIMIT 1
"""

_EVENTS_SQL = """
SELECT e.event_id, e.title, e.site_url, e.categories, e.favorites_count, e.place_title, e.place_address,
       e.place_lat, e.place_lon, e.price, e.is_free, e.age_restriction, e.description, e.body_text,
       array_agg(d.start_at ORDER BY d.start_at NULLS FIRST) AS starts,
       array_agg(d.end_at ORDER BY d.start_at NULLS FIRST) AS ends
FROM kudago.events e
JOIN kudago.event_dates d USING (event_id)
WHERE {where}
GROUP BY e.event_id
"""

# События, у которых есть сеанс, пересекающий визит [start, end).
_IN_VISIT = """e.event_id IN (
    SELECT event_id FROM kudago.event_dates
    WHERE (end_at IS NULL OR end_at >= %(start)s) AND (start_at IS NULL OR start_at < %(end)s))"""


class EventsClient:
    """События Казани из схемы kudago (их загружает kudago-events). Соединение открывается на каждый вызов."""

    def __init__(self, dsn: str, *, connect_timeout: int = 5):
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.kudago_db_dsn)

    async def events_for_stay(self, checkin: date, checkout: date) -> StayEvents:
        """Отобранные разовые события и выставки на дни визита. Нет данных или сбой базы → EventsError."""
        start, end = visit_bounds(checkin, checkout)
        load, events = await self._query(_IN_VISIT, {"start": start, "end": end})
        selection = select_events(events, checkin=checkin, checkout=checkout)
        note = None
        if checkin < load.window_from or checkout > load.window_to:
            note = f"События известны только с {load.window_from} по {load.window_to}."
        return StayEvents(
            period=f"{checkin} — {checkout}",
            events=[
                EventItem(
                    id=e.event_id,
                    title=e.title,
                    sessions=[format_session(s) for s in sessions[:MAX_ONE_OFF_SESSIONS]],
                    place=e.place_title,
                    price=e.price,
                    age=e.age_restriction,
                    url=e.site_url,
                )
                for e, sessions in selection.one_off
            ],
            exhibitions=[
                ExhibitionItem(id=e.event_id, title=e.title, until=until_date(e), place=e.place_title, url=e.site_url)
                for e in selection.exhibitions
            ],
            note=note,
            data=_data_note(load),
        )

    async def event_details(
        self, event_id: int, *, checkin: date | None = None, checkout: date | None = None, today: date
    ) -> EventDetails:
        """Подробности события: сеансы в дни визита или, без дат, ближайшие от today. Нет события → EventsError."""
        load, events = await self._query("e.event_id = %(event_id)s", {"event_id": event_id})
        if not events:
            raise EventsError(f"Событие {event_id} не найдено среди событий Казани.")
        [event] = events
        if checkin and checkout:
            start, end = visit_bounds(checkin, checkout)
            sessions = [s for s in event.sessions if overlaps(s, start, end)]
        else:
            today_start = datetime.combine(today, time(), MOSCOW_TZ)
            sessions = [s for s in event.sessions if s.end_at is None or s.end_at >= today_start]
        has_place = any(v is not None for v in (event.place_title, event.place_address, event.place_lat))
        return EventDetails(
            id=event.event_id,
            title=event.title,
            categories=event.categories,
            description=_shorten(event.body_text or event.description),
            sessions=[_format_any(s) for s in sessions[:MAX_DETAIL_SESSIONS]],
            until=until_date(event) if any(is_long(s) for s in event.sessions) else None,
            place=PlaceInfo(
                title=event.place_title, address=event.place_address, lat=event.place_lat, lon=event.place_lon
            )
            if has_place
            else None,
            price=event.price,
            is_free=event.is_free,
            age=event.age_restriction,
            url=event.site_url,
            data=_data_note(load),
        )

    async def _query(self, where: str, params: dict) -> tuple[LoadInfo, list[StoredEvent]]:
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row
            ) as conn:
                load_row = await (await conn.execute(_LOAD_SQL)).fetchone()
                if load_row is None:
                    raise EventsError(NOT_LOADED)
                rows = await (await conn.execute(_EVENTS_SQL.format(where=where), params)).fetchall()
        except psycopg.errors.UndefinedTable as exc:
            raise EventsError(NOT_LOADED) from exc
        except psycopg.Error as exc:
            raise EventsError(f"События Казани недоступны: {str(exc)[:200]}") from exc
        load = LoadInfo(
            loaded_at=load_row["started_at"], window_from=load_row["window_from"], window_to=load_row["window_to"]
        )
        return load, [_stored_event(r) for r in rows]


def _stored_event(row: dict) -> StoredEvent:
    sessions = [EventSession(s, e) for s, e in zip(row.pop("starts"), row.pop("ends"), strict=True)]
    return StoredEvent(**row, sessions=sessions)


def _format_any(session: EventSession) -> str:
    """Короткий сеанс — время начала; длинный — период по датам («…» — открытый конец)."""
    if not is_long(session):
        return format_session(session)
    start = f"{session.start_at.astimezone(MOSCOW_TZ):%Y-%m-%d}" if session.start_at else "…"
    end = f"{session.end_at.astimezone(MOSCOW_TZ):%Y-%m-%d}" if session.end_at else "…"
    return f"{start} — {end}"


def _shorten(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_DESCRIPTION:
        return text
    return text[:MAX_DESCRIPTION].rstrip() + "…"


def _data_note(load: LoadInfo) -> str:
    return f"KudaGo, загружено {load.loaded_at.astimezone(MOSCOW_TZ):%Y-%m-%d}. {LINK_NOTE}"
