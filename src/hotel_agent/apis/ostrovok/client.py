from datetime import date
from typing import Self

import psycopg
from psycopg.rows import dict_row

from hotel_agent.apis.ostrovok.models import (City, DataWindow, HotelRecord,
                                              OstrovokError, RateTemplate,
                                              SearchResult, StaySearch)
from hotel_agent.apis.ostrovok.search import (rank_hotels, resolve_city,
                                              room_occupancies,
                                              validate_search)
from hotel_agent.config import Settings

_WINDOW_SQL = "SELECT key, value FROM ostrovok.meta WHERE key IN ('window_from', 'window_to', 'scraped_on')"

# Сам Татарстан и города, где есть отели; самые крупные первыми.
_CITIES_SQL = """
SELECT r.region_id, r.name, r.name_en, r.parent_id
FROM ostrovok.regions r
LEFT JOIN ostrovok.hotels h USING (region_id)
GROUP BY r.region_id
HAVING r.parent_id IS NULL OR count(h.hotel_id) > 0
ORDER BY count(h.hotel_id) DESC, r.name
"""

# Отель свободен на период, если минимальная цена есть на каждую ночь.
_HOTELS_SQL = """
SELECT h.hotel_id, h.master_id, h.name, h.kind, h.stars, h.address, h.lat, h.lon, h.rating, h.rating_count,
       h.check_in_from, h.check_out_until, h.flags, h.free_child_age, h.metro,
       r.name AS city, r.lat AS city_lat, r.lon AS city_lon, r.catalog_slug,
       array_agg(p.night ORDER BY p.night) AS nights, array_agg(p.min_price ORDER BY p.night) AS prices
FROM ostrovok.hotels h
JOIN ostrovok.night_prices p ON p.hotel_id = h.hotel_id AND p.night >= %(checkin)s AND p.night < %(checkout)s
LEFT JOIN ostrovok.regions r ON r.region_id = h.region_id
WHERE %(region_id)s::bigint IS NULL OR h.region_id = %(region_id)s::bigint
GROUP BY h.hotel_id, r.region_id
HAVING count(*) = %(nights)s
"""

_TEMPLATES_SQL = """
SELECT hotel_id, rg_hash, room_name, night_type, requested_guests AS guests, meal, has_breakfast, payment_type,
       price_ratio, free_cancellation_hours, allotment
FROM ostrovok.rates
WHERE hotel_id = ANY(%(hotel_ids)s) AND requested_guests = ANY(%(guests)s) AND price_ratio IS NOT NULL
"""


class OstrovokClient:
    """Мок поиска островка: подбирает отели Татарстана по снимку сайта в Postgres (схема ostrovok).

    Соединение открывается на каждый запрос, поэтому агент запускается и без поднятой базы.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 5):
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.ostrovok_db_dsn)

    async def search_hotels(self, request: StaySearch, *, today: date) -> SearchResult:
        """Отели, свободные на весь период, с оценкой цены. Неверный запрос или сбой базы → OstrovokError."""
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row
            ) as conn:
                window = await _data_window(conn)
                if errors := validate_search(request, today=today, window=window):
                    raise OstrovokError(" ".join(errors))
                region_id = None
                if request.city:
                    city = resolve_city(request.city, await _cities(conn))
                    region_id = None if city.parent_id is None else city.region_id
                hotels = await _available_hotels(conn, request, region_id)
                guests = sorted({k for h in hotels for k in room_occupancies(request, h.free_child_age)})
                templates = await _rate_templates(conn, [h.hotel_id for h in hotels], guests)
        except psycopg.Error as exc:
            raise OstrovokError(f"база островка недоступна: {exc}") from exc
        return rank_hotels(request, hotels, templates, today=today, data_as_of=window.scraped_on)


async def _data_window(conn: psycopg.AsyncConnection) -> DataWindow:
    meta = {row["key"]: date.fromisoformat(row["value"]) for row in await (await conn.execute(_WINDOW_SQL)).fetchall()}
    if "window_from" not in meta or "window_to" not in meta:
        raise OstrovokError("в базе островка нет цен: сначала соберите данные (uv run ostrovok-scrape all).")
    return DataWindow(meta["window_from"], meta["window_to"], meta.get("scraped_on"))


async def _cities(conn: psycopg.AsyncConnection) -> list[City]:
    return [City(**row) for row in await (await conn.execute(_CITIES_SQL)).fetchall()]


async def _available_hotels(
    conn: psycopg.AsyncConnection, request: StaySearch, region_id: int | None
) -> list[HotelRecord]:
    params = {
        "checkin": request.checkin,
        "checkout": request.checkout,
        "nights": (request.checkout - request.checkin).days,
        "region_id": region_id,
    }
    hotels = []
    for row in await (await conn.execute(_HOTELS_SQL, params)).fetchall():
        nights, prices = row.pop("nights"), row.pop("prices")
        hotels.append(HotelRecord(**row, night_prices=dict(zip(nights, prices, strict=True))))
    return hotels


async def _rate_templates(conn: psycopg.AsyncConnection, hotel_ids: list[str], guests: list[int]) -> list[RateTemplate]:
    if not hotel_ids or not guests:
        return []
    rows = await (await conn.execute(_TEMPLATES_SQL, {"hotel_ids": hotel_ids, "guests": guests})).fetchall()
    return [RateTemplate(**row) for row in rows]
