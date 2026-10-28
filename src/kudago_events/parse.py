from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Заглушки KudaGo для открытых дат (1-й и 9999-й год): всё вне этих границ — «без начала» / «без конца».
_MIN_TS = int(datetime(1900, 1, 1, tzinfo=UTC).timestamp())
_MAX_TS = int(datetime(3000, 1, 1, tzinfo=UTC).timestamp())


@dataclass(frozen=True)
class Session:
    """Сеанс события; None — открытый конец (постоянная экспозиция и т. п.)."""

    start_at: datetime | None
    end_at: datetime | None


@dataclass
class EventRow:
    """Событие KudaGo в виде строки kudago.events плюс его сеансы в окне загрузки."""

    event_id: int
    title: str
    slug: str | None
    site_url: str
    description: str | None
    body_text: str | None
    categories: list[str]
    tags: list[str]
    age_restriction: str | None
    price: str | None
    is_free: bool | None
    favorites_count: int
    place_title: str | None
    place_address: str | None
    place_lat: float | None
    place_lon: float | None
    place_url: str | None
    raw: dict
    sessions: list[Session] = field(default_factory=list)


def parse_event(raw: dict, *, window_start: datetime, window_end: datetime) -> EventRow | None:
    """Событие из ответа API → строка; None, если показывать нечего: нет ссылки или сеансов в окне [start, end)."""
    site_url = (raw.get("site_url") or "").strip()
    title = _text(raw.get("title"))
    if not site_url or not title:
        return None
    sessions = [s for s in map(_session, raw.get("dates") or []) if _touches(s, window_start, window_end)]
    if not sessions:
        return None
    place = raw.get("place") or {}
    coords = place.get("coords") or {}
    return EventRow(
        event_id=int(raw["id"]),
        title=title[0].upper() + title[1:],
        slug=raw.get("slug"),
        site_url=site_url,
        description=_text(raw.get("description")),
        body_text=_text(raw.get("body_text")),
        categories=list(raw.get("categories") or []),
        tags=[t if isinstance(t, str) else t.get("name") or t.get("slug") for t in raw.get("tags") or []],
        age_restriction=_age(raw.get("age_restriction")),
        price=_text(raw.get("price")),
        is_free=raw.get("is_free"),
        favorites_count=raw.get("favorites_count") or 0,
        place_title=_text(place.get("title")),
        place_address=_text(place.get("address")),
        place_lat=coords.get("lat"),
        place_lon=coords.get("lon"),
        place_url=_text(place.get("site_url")),
        raw=raw,
        sessions=sessions,
    )


def _session(date: dict) -> Session:
    start = None if date.get("is_startless") else _moment(date.get("start"))
    end = None if date.get("is_endless") else _moment(date.get("end"))
    if start is not None and end is not None and end < start:
        end = start
    return Session(start, end)


def _moment(value: Any) -> datetime | None:
    if value is None or not _MIN_TS < value < _MAX_TS:
        return None
    return datetime.fromtimestamp(value, UTC)


def _touches(s: Session, window_start: datetime, window_end: datetime) -> bool:
    return (s.end_at is None or s.end_at >= window_start) and (s.start_at is None or s.start_at < window_end)


def _text(value: Any) -> str | None:
    text = (value or "").strip() if isinstance(value, str) or value is None else str(value)
    return text or None


def _age(value: Any) -> str | None:
    """18 → «18+», «6» → «6+»; пусто → None."""
    if isinstance(value, int):
        return f"{value}+"
    text = _text(value)
    if text is None:
        return None
    return f"{text}+" if text.isdigit() else text
