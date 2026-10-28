from dataclasses import dataclass, field
from datetime import date, datetime

from pydantic import BaseModel


class EventsError(Exception):
    """Событий нет или база недоступна; текст — для агента."""


@dataclass(frozen=True)
class EventSession:
    """Сеанс события; None — открытый конец (постоянная экспозиция и т. п.)."""

    start_at: datetime | None
    end_at: datetime | None


@dataclass
class StoredEvent:
    """Событие из схемы kudago со всеми сеансами в окне загрузки."""

    event_id: int
    title: str
    site_url: str
    categories: list[str] = field(default_factory=list)
    favorites_count: int = 0
    place_title: str | None = None
    place_address: str | None = None
    place_lat: float | None = None
    place_lon: float | None = None
    price: str | None = None
    is_free: bool | None = None
    age_restriction: str | None = None
    description: str | None = None
    body_text: str | None = None
    sessions: list[EventSession] = field(default_factory=list)


@dataclass(frozen=True)
class LoadInfo:
    """Последняя удачная загрузка: когда и на какое окно дат."""

    loaded_at: datetime
    window_from: date
    window_to: date


class EventItem(BaseModel):
    """Разовое событие для абзаца агента."""

    id: int
    title: str
    # Сеансы в дни визита по Москве: «2026-11-20 19:00» или «2026-11-20», если время не указано.
    sessions: list[str]
    place: str | None = None
    price: str | None = None
    age: str | None = None
    url: str


class ExhibitionItem(BaseModel):
    """Выставка, идущая во время визита."""

    id: int
    title: str
    until: date | None = None
    place: str | None = None
    url: str


class StayEvents(BaseModel):
    """Ответ tool events_in_kazan."""

    period: str
    events: list[EventItem]
    exhibitions: list[ExhibitionItem]
    note: str | None = None
    data: str


class PlaceInfo(BaseModel):
    title: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None


class EventDetails(BaseModel):
    """Ответ tool kazan_event_details."""

    id: int
    title: str
    categories: list[str]
    description: str | None = None
    sessions: list[str]
    until: date | None = None
    place: PlaceInfo | None = None
    price: str | None = None
    is_free: bool | None = None
    age: str | None = None
    url: str
    data: str
