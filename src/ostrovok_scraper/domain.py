from datetime import date, timedelta
from typing import Literal

MAX_ADULTS = 6
MAX_GUESTS = 8
CHILD_AGE = 16


def night_type(night: date) -> Literal["weekday", "weekend"]:
    """Заезд в пятницу или субботу — выходная ночь."""
    return "weekend" if night.weekday() in (4, 5) else "weekday"


def paxes_for_guests(guests: int) -> list[dict]:
    """Состав гостей для запроса: до 6 взрослых, остальные — дети 16 лет."""
    if not 1 <= guests <= MAX_GUESTS:
        raise ValueError(f"guests должно быть от 1 до {MAX_GUESTS}, получено {guests}")
    if guests <= MAX_ADULTS:
        return [{"adults": guests}]
    return [{"adults": MAX_ADULTS, "child_ages": [CHILD_AGE] * (guests - MAX_ADULTS)}]


def window_nights(start: date, end: date, limit: int | None = None) -> list[date]:
    """Ночи заезда от start до end включительно, не больше limit."""
    days = (end - start).days + 1
    nights = [start + timedelta(days=i) for i in range(max(days, 0))]
    return nights if limit is None else nights[:limit]


def pick_reference_night(available: set[date], ref: date) -> date | None:
    """Опорная ночь, а если её нет — ближайшая доступная того же типа."""
    if ref in available:
        return ref
    same_type = [n for n in available if night_type(n) == night_type(ref)]
    return min(same_type, key=lambda n: (abs((n - ref).days), n), default=None)


def _key(**parts: object) -> str:
    return ";".join(f"{name}={value}" for name, value in parts.items())


def serp_key(night: date, guests: int) -> str:
    return _key(night=night.isoformat(), guests=guests)


def hotel_key(hotel_id: str) -> str:
    return _key(hotel=hotel_id)


def child_age_key(hotel_id: str, night: date) -> str:
    return _key(hotel=hotel_id, night=night.isoformat())


def rates_key(hotel_id: str, night: date, guests: int) -> str:
    return _key(hotel=hotel_id, night=night.isoformat(), guests=guests)


def reviews_key(hotel_id: str, page: int) -> str:
    return _key(hotel=hotel_id, page=page)


def parse_key(key: str) -> dict[str, str]:
    """Разбирает ключ raw вида `a=1;b=2` в словарь."""
    return dict(part.split("=", 1) for part in key.split(";"))
