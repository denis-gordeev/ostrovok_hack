from dataclasses import astuple, dataclass, field, fields, replace
from datetime import date
from decimal import InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ostrovok_scraper.config import MSK, ScraperConfig
from ostrovok_scraper.domain import parse_key
from ostrovok_scraper.parsing.hotel_page import parse_city_region, parse_hotel, parse_rooms
from ostrovok_scraper.parsing.rates import free_child_age, parse_rates
from ostrovok_scraper.parsing.reviews import parse_api_reviews, parse_page_reviews
from ostrovok_scraper.parsing.serp import parse_map_hotels, parse_session_region
from ostrovok_scraper.rows import HotelRow, RateRow, RegionRow, ReviewRow, RoomRow
from ostrovok_scraper.store import RawStore

_COUNT_KEYS = ("regions", "hotels", "rooms", "night_prices", "rates", "reviews")
_SKIP_STEPS = ("serp", "hotels", "rates", "reviews")
_API_REVIEWS_THRESHOLD = 10
# Ошибки разбора страницы отеля: такой отель пропускается целиком.
_PAGE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, InvalidOperation)
_TABLES = ("meta", "regions", "hotels", "rooms", "night_prices", "rates", "reviews")
# jsonb-поля строк, которые нужно обернуть в Jsonb при записи.
_JSONB = {
    "hotels": {"rating_details", "description", "amenities", "policies", "metro"},
    "rates": {"cancellation_policies", "bed_places"},
    "reviews": {"details"},
}


@dataclass
class NormalizeReport:
    """Итог нормализации: числа, пропуски и аномалии."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_COUNT_KEYS, 0))
    rates_by_guests: dict[int, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_SKIP_STEPS, 0))
    anomalies: dict[str, list[str]] = field(
        default_factory=lambda: {
            "hotels_without_page": [],
            "hotels_without_rates": [],
            "rates_unknown_rg": [],
            "incomplete_searches": [],
        }
    )


def _insert(conn: psycopg.Connection, table: str, rows: list[Any]) -> None:
    """Вставка dataclass-строк; поля берутся в порядке колонок таблицы."""
    if not rows:
        return
    names = [f.name for f in fields(rows[0])]
    jsonb_idx = {i for i, name in enumerate(names) if name in _JSONB.get(table, ())}
    values = [
        tuple(Jsonb(v) if i in jsonb_idx and v is not None else v for i, v in enumerate(astuple(row))) for row in rows
    ]
    placeholders = ", ".join(["%s"] * len(names))
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO ostrovok.{table} ({', '.join(names)}) VALUES ({placeholders})", values)


def normalize(conn: psycopg.Connection, cfg: ScraperConfig) -> NormalizeReport:
    """Пересобирает схему ostrovok из raw одной транзакцией."""
    store = RawStore(conn)
    report = NormalizeReport()

    # Выдача: каждое тело разбирается один раз.
    stars: dict[str, int] = {}
    night_prices: dict[tuple[str, date], Any] = {}
    session_regions: dict[int, RegionRow] = {}
    serp_nights: list[date] = []
    for step in ("serp", "serp_guests"):
        for key, body in store.bodies(step):
            hotels, skipped = parse_map_hotels(body)
            report.skipped["serp"] += skipped
            parts = parse_key(key)
            night = date.fromisoformat(parts["night"])
            if step == "serp":
                serp_nights.append(night)
            for mh in hotels:
                stars[mh.hotel_id] = mh.stars
                if step == "serp":
                    night_prices[(mh.hotel_id, night)] = mh.price
            if body.get("session_region"):
                region = parse_session_region(body["session_region"])
                session_regions[region.region_id] = region
        report.anomalies["incomplete_searches"] += [f"{step}:{k}" for k in store.incomplete_keys(step)]

    # Отели со страницами.
    pages = {parse_key(key)["hotel"]: body for key, body in store.bodies("hotels")}
    probes = {parse_key(key)["hotel"]: body for key, body in store.bodies("child_age")}
    report.anomalies["hotels_without_page"] = sorted(set(stars) - set(pages))

    regions = dict(session_regions)
    hotel_rows: list[HotelRow] = []
    room_rows: list[RoomRow] = []
    page_reviews: dict[str, tuple[list[ReviewRow], int]] = {}
    seen_ids: set[str] = set()
    seen_masters: set[int] = set()
    for hotel_id in sorted(pages):
        page = pages[hotel_id]
        try:
            row = parse_hotel(page, stars=stars.get(hotel_id))
            if hotel_id in probes:
                row = _with_child_age(row, free_child_age(probes[hotel_id]))
            rooms = parse_rooms(page)
            region = parse_city_region(page, parent_id=cfg.region_id)
            own_reviews = parse_page_reviews(page.get("reviews") or {}, hotel_id=row.hotel_id)
        except _PAGE_ERRORS:
            report.skipped["hotels"] += 1
            continue
        # Страница может вести на склеенный отель: берём первый по ключу.
        if row.hotel_id in seen_ids or row.master_id in seen_masters:
            report.skipped["hotels"] += 1
            continue
        seen_ids.add(row.hotel_id)
        seen_masters.add(row.master_id)
        hotel_rows.append(row)
        page_reviews[row.hotel_id] = own_reviews
        room_rows += rooms
        regions.setdefault(region.region_id, region)
    hotel_ids = {r.hotel_id for r in hotel_rows}
    rooms_by_hotel = {(r.hotel_id, r.rg_hash) for r in room_rows}

    price_rows = [(h, n, p) for (h, n), p in sorted(night_prices.items()) if h in hotel_ids]

    # Тарифы.
    rate_rows: list[RateRow] = []
    unknown_rg: set[str] = set()
    for key, body in store.bodies("rates"):
        parts = parse_key(key)
        hotel_id = parts["hotel"]
        if hotel_id not in hotel_ids:
            continue
        rows, skipped = parse_rates(
            body, hotel_id=hotel_id, night=date.fromisoformat(parts["night"]), requested_guests=int(parts["guests"])
        )
        report.skipped["rates"] += skipped
        rate_rows += rows
        unknown_rg |= {f"{hotel_id}:{r.rg_hash}" for r in rows if (hotel_id, r.rg_hash) not in rooms_by_hotel}
    report.anomalies["rates_unknown_rg"] = sorted(unknown_rg)
    with_rates = {r.hotel_id for r in rate_rows}
    report.anomalies["hotels_without_rates"] = sorted(hotel_ids - with_rates)
    for r in rate_rows:
        report.rates_by_guests[r.requested_guests] = report.rates_by_guests.get(r.requested_guests, 0) + 1

    # Отзывы.
    api_bodies: dict[str, list[dict]] = {}
    for key, body in store.bodies("reviews"):
        api_bodies.setdefault(parse_key(key)["hotel"], []).append(body)
    reviews: dict[int, ReviewRow] = {}
    for row in hotel_rows:
        hotel_id = row.hotel_id
        if (row.reviews_total or 0) > _API_REVIEWS_THRESHOLD and hotel_id in api_bodies:
            parsed = [parse_api_reviews(b, hotel_id=hotel_id) for b in api_bodies[hotel_id]]
        else:
            # Отзывов до 10 или шаг reviews не отработал: берём отзывы со страницы.
            parsed = [page_reviews[hotel_id]]
        for rows, skipped in parsed:
            report.skipped["reviews"] += skipped
            for review in rows:
                reviews[review.review_id] = review

    # Запись. Чтения выше открыли неявную транзакцию: закрываем её, чтобы блок ниже
    # был внешней транзакцией и коммитился, а не savepoint'ом.
    conn.commit()
    with conn.transaction():
        conn.execute(f"TRUNCATE {', '.join('ostrovok.' + t for t in _TABLES)} RESTART IDENTITY")
        region_rows = [regions[k] for k in sorted(regions)]
        _insert(conn, "regions", region_rows)
        _insert(conn, "hotels", hotel_rows)
        _insert(conn, "rooms", room_rows)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ostrovok.night_prices (hotel_id, night, min_price) VALUES (%s, %s, %s)", price_rows
            )
        _insert(conn, "rates", rate_rows)
        conn.execute(
            """
            UPDATE ostrovok.rates r SET price_ratio = round(r.price / np.min_price, 4)
            FROM ostrovok.night_prices np
            WHERE np.hotel_id = r.hotel_id AND np.night = r.night AND np.min_price > 0
            """
        )
        _insert(conn, "reviews", [reviews[k] for k in sorted(reviews)])
        _write_meta(conn, store, cfg, serp_nights)

    report.counts = {
        "regions": len(region_rows),
        "hotels": len(hotel_rows),
        "rooms": len(room_rows),
        "night_prices": len(price_rows),
        "rates": len(rate_rows),
        "reviews": len(reviews),
    }
    return report


def _with_child_age(row: HotelRow, age: int) -> HotelRow:
    return replace(row, free_child_age=age, free_child_age_source="probe")


def _write_meta(conn: psycopg.Connection, store: RawStore, cfg: ScraperConfig, serp_nights: list[date]) -> None:
    meta = {
        "window_to": cfg.window_to.isoformat(),
        "ref_weekday": cfg.ref_weekday.isoformat(),
        "ref_weekend": cfg.ref_weekend.isoformat(),
    }
    first = store.first_fetched_at("serp")
    if first is not None:
        meta["scraped_on"] = first.astimezone(MSK).date().isoformat()
    if serp_nights:
        meta["window_from"] = min(serp_nights).isoformat()
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO ostrovok.meta (key, value) VALUES (%s, %s)", list(meta.items()))
