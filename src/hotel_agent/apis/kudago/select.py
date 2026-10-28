from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from hotel_agent.apis.kudago.models import EventSession, StoredEvent
from hotel_agent.dates import MOSCOW_TZ

ONE_OFF_LIMIT = 5
EXHIBITION_LIMIT = 2
# Больше стольких коротких сеансов в окне загрузки — регулярное событие (ежедневные экскурсии и т. п.).
REGULAR_SESSIONS = 10
# Сеанс дольше суток или с открытым концом — «длинный» (выставка, постоянная экспозиция).
LONG_SESSION = timedelta(hours=24)
# Акции и деловые мероприятия — не то, что предлагают туристу.
EXCLUDED_CATEGORIES = frozenset({"stock", "business-events"})


@dataclass
class Selection:
    """Разовые события (с сеансами в дни визита) и выставки — по убыванию популярности."""

    one_off: list[tuple[StoredEvent, list[EventSession]]]
    exhibitions: list[StoredEvent]


def visit_bounds(checkin: date, checkout: date) -> tuple[datetime, datetime]:
    """Визит по Москве: с полуночи дня заезда до конца дня выезда (в день выезда гость ещё в городе)."""
    return (
        datetime.combine(checkin, time(), MOSCOW_TZ),
        datetime.combine(checkout + timedelta(days=1), time(), MOSCOW_TZ),
    )


def overlaps(session: EventSession, start: datetime, end: datetime) -> bool:
    """Сеанс пересекает [start, end); у точечного сеанса end_at = start_at."""
    return (session.end_at is None or session.end_at >= start) and (session.start_at is None or session.start_at < end)


def is_long(session: EventSession) -> bool:
    if session.start_at is None or session.end_at is None:
        return True
    return session.end_at - session.start_at > LONG_SESSION


def select_events(events: list[StoredEvent], *, checkin: date, checkout: date) -> Selection:
    """До 5 разовых событий в дни визита и до 2 выставок; регулярные и прочие длинные события отбрасываются."""
    start, end = visit_bounds(checkin, checkout)
    one_off: list[tuple[StoredEvent, list[EventSession]]] = []
    exhibitions: list[StoredEvent] = []
    for event in events:
        if EXCLUDED_CATEGORIES & set(event.categories):
            continue
        in_visit = sorted(
            (s for s in event.sessions if overlaps(s, start, end)), key=lambda s: s.start_at or start
        )
        if not in_visit:
            continue
        if any(is_long(s) for s in in_visit):
            if "exhibition" in event.categories:
                exhibitions.append(event)
        elif len(event.sessions) <= REGULAR_SESSIONS:
            one_off.append((event, in_visit))

    one_off.sort(key=lambda item: (-item[0].favorites_count, item[1][0].start_at or start, item[0].event_id))
    exhibitions.sort(key=lambda e: (-e.favorites_count, e.event_id))
    return Selection(
        one_off=_unique_titles(one_off, key=lambda item: item[0].title)[:ONE_OFF_LIMIT],
        exhibitions=_unique_titles(exhibitions, key=lambda e: e.title)[:EXHIBITION_LIMIT],
    )


def _unique_titles(items: list, *, key) -> list:
    """Первое (самое популярное) из событий с одинаковым названием без учёта регистра и пробелов по краям."""
    seen: set[str] = set()
    unique = []
    for item in items:
        title = key(item).strip().casefold()
        if title not in seen:
            seen.add(title)
            unique.append(item)
    return unique


def format_session(session: EventSession) -> str:
    """Начало сеанса по Москве: «2026-11-20 19:00», в полночь (время не указано) — только дата."""
    if session.start_at is None:
        return "идёт постоянно"
    local = session.start_at.astimezone(MOSCOW_TZ)
    return f"{local:%Y-%m-%d}" if local.time() == time() else f"{local:%Y-%m-%d %H:%M}"


def until_date(event: StoredEvent) -> date | None:
    """Последний день событий по Москве; None, если какой-то сеанс без конца."""
    ends = [s.end_at for s in event.sessions]
    if not ends or any(e is None for e in ends):
        return None
    return max(ends).astimezone(MOSCOW_TZ).date()
