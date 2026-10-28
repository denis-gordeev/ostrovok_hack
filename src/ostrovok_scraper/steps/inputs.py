from collections.abc import Iterable
from datetime import date

from ostrovok_scraper.domain import parse_key
from ostrovok_scraper.parsing.serp import MapHotel, parse_map_hotels
from ostrovok_scraper.store import RawStore


def serp_hotels(store: RawStore) -> dict[str, MapHotel]:
    """Отели из всех выдач шагов serp и serp_guests."""
    found: dict[str, MapHotel] = {}
    for step in ("serp", "serp_guests"):
        for _, body in store.bodies(step):
            for hotel in parse_map_hotels(body)[0]:
                found.setdefault(hotel.hotel_id, hotel)
    return found


def available_nights(store: RawStore) -> dict[str, set[date]]:
    """Ночи, на которые отель есть в выдаче на 2 гостей."""
    nights: dict[str, set[date]] = {}
    for key, body in store.bodies("serp"):
        night = date.fromisoformat(parse_key(key)["night"])
        for hotel in parse_map_hotels(body)[0]:
            nights.setdefault(hotel.hotel_id, set()).add(night)
    return nights


def guests_hotels(store: RawStore) -> dict[tuple[int, date], set[str]]:
    """Отели из выдач на (число гостей, опорная ночь)."""
    found: dict[tuple[int, date], set[str]] = {}
    for key, body in store.bodies("serp_guests"):
        parts = parse_key(key)
        slot = (int(parts["guests"]), date.fromisoformat(parts["night"]))
        found.setdefault(slot, set()).update(h.hotel_id for h in parse_map_hotels(body)[0])
    return found


def page_facts(store: RawStore) -> dict[str, tuple[int, int]]:
    """hotel_id -> (region_id, reviews_total) по сохранённым страницам отелей."""
    facts: dict[str, tuple[int, int]] = {}
    for key, body in store.bodies("hotels"):
        try:
            facts[parse_key(key)["hotel"]] = (
                int(body["hotel"]["location"]["regionId"]),
                int((body.get("reviews") or {}).get("total") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return facts


def selected_hotels(hotel_ids: Iterable[str], limit: int | None) -> list[str]:
    """Отсортированные id, первые limit."""
    ids = sorted(hotel_ids)
    return ids if limit is None else ids[:limit]
