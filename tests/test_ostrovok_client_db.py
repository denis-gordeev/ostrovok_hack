import os
from datetime import date, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from hotel_agent.apis.ostrovok.client import OstrovokClient
from hotel_agent.apis.ostrovok.models import OstrovokError, StaySearch
from ostrovok_scraper.store import apply_migrations, connect

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 27)
CHECKIN, CHECKOUT = date(2026, 11, 19), date(2026, 11, 22)
NIGHTS = [CHECKIN + timedelta(days=i) for i in range(3)]


@pytest.fixture
def dsn() -> str:
    """Тестовая БД с чистой схемой ostrovok: два отеля в Казани (один занят в пятницу) и один в Челнах."""
    dsn = os.environ.get("OSTROVOK_TEST_DSN")
    if not dsn:
        pytest.skip("OSTROVOK_TEST_DSN не задан")
    with connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS raw, ostrovok CASCADE")
        conn.commit()
        apply_migrations(conn)
        _fill(conn)
        conn.commit()
    return dsn


def _fill(conn: psycopg.Connection) -> None:
    conn.cursor().executemany(
        "INSERT INTO ostrovok.meta (key, value) VALUES (%s, %s)",
        [("window_from", "2026-10-26"), ("window_to", "2026-11-29"), ("scraped_on", "2026-10-26")],
    )
    conn.cursor().executemany(
        "INSERT INTO ostrovok.regions (region_id, name, name_en, type, parent_id, lat, lon, catalog_slug)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (6288539, "Республика Татарстан", "Republic of Tatarstan", "Province (State)", None, 55.79, 49.10, None),
            (1993, "Казань", "Kazan", "CITY", 6288539, 55.796, 49.106, "russia/kazan"),
            (2522, "Набережные Челны", "Naberezhnye Chelny", "CITY", 6288539, 55.742, 52.399, "russia/chelny"),
        ],
    )
    hotels = [
        ("kazan_hotel", 1, 1993, "Отель Казань", 55.79, 49.11),
        ("busy_hotel", 2, 1993, "Отель Занятый", 55.80, 49.12),
        ("chelny_hotel", 3, 2522, "Отель Челны", 55.74, 52.40),
    ]
    conn.cursor().executemany(
        "INSERT INTO ostrovok.hotels (hotel_id, master_id, region_id, name, kind, stars, lat, lon, rating,"
        " rating_count, flags, free_child_age, free_child_age_source, metro)"
        " VALUES (%s, %s, %s, %s, 'hotel', 3, %s, %s, 8.5, 10, '{has_parking}', 3, 'probe', %s)",
        [(*h[:4], *h[4:], Jsonb([{"name": "Кремлёвская", "distance_m": 400}])) for h in hotels],
    )
    prices = [(h, n, 1000 * (i + 1)) for h in ("kazan_hotel", "chelny_hotel") for i, n in enumerate(NIGHTS)]
    prices += [("busy_hotel", NIGHTS[0], 900), ("busy_hotel", NIGHTS[2], 900)]
    conn.cursor().executemany(
        "INSERT INTO ostrovok.night_prices (hotel_id, night, min_price) VALUES (%s, %s, %s)", prices
    )
    rates = [
        ("kazan_hotel", "std", date(2026, 11, 17), "weekday", 2, "nomeal", "now", 1.2, 24),
        ("kazan_hotel", "std", date(2026, 11, 20), "weekend", 2, "nomeal", "now", 1.5, 24),
        ("kazan_hotel", "lux", date(2026, 11, 17), "weekday", 2, "nomeal", "now", None, None),
        ("kazan_hotel", "fam", date(2026, 11, 17), "weekday", 3, "breakfast", "hotel", 2.0, None),
    ]
    conn.cursor().executemany(
        "INSERT INTO ostrovok.rates (hotel_id, rg_hash, night, night_type, requested_guests, room_name, meal,"
        " has_breakfast, payment_type, price, price_ratio, free_cancellation_hours, allotment)"
        " VALUES (%s, %s, %s, %s, %s, 'Номер', %s, false, %s, 1, %s, %s, 3)",
        rates,
    )


async def search(dsn: str, **overrides):
    request = StaySearch(**{"checkin": CHECKIN, "checkout": CHECKOUT, **overrides})
    return await OstrovokClient(dsn).search_hotels(request, today=TODAY)


async def test_finds_hotels_free_on_every_night_of_the_city(dsn):
    result = await search(dsn, city="Казань")

    assert (result.nights, result.found, result.data_as_of) == (3, 1, date(2026, 10, 26))
    [found] = result.hotels
    # чт 1000 × 1.2 + пт 2000 × 1.5 + сб 3000 × 1.5; тариф без price_ratio не участвует
    assert (found.hotel_id, found.price_total, found.city) == ("kazan_hotel", 8700, "Казань")
    assert found.url == "https://ostrovok.ru/hotel/russia/kazan/mid1/kazan_hotel/"
    assert (found.metro, found.amenities) == ("Кремлёвская", ["parking"])


@pytest.mark.parametrize("city", [None, "Татарстан"])
async def test_without_city_searches_all_tatarstan(dsn, city):
    result = await search(dsn, city=city)

    assert {h.hotel_id for h in result.hotels} == {"kazan_hotel", "chelny_hotel"}
    assert next(h for h in result.hotels if h.hotel_id == "chelny_hotel").price_source == "min_price"


async def test_rates_for_other_party_size_are_loaded(dsn):
    result = await search(dsn, city="Kazan", adults=3)

    assert ([h.price_total for h in result.hotels], result.without_rates) == ([12000], 0)


async def test_unknown_city_and_bad_dates_raise(dsn):
    with pytest.raises(OstrovokError, match="Город «Москва» не найден. Отели есть в городах: Казань, Набережные Челны"):
        await search(dsn, city="Москва")
    with pytest.raises(OstrovokError, match="выезда не позже 30.11.2026"):
        await search(dsn, checkout=date(2026, 12, 2))


async def test_empty_database_raises(dsn):
    with connect(dsn) as conn:
        conn.execute("TRUNCATE ostrovok.meta")
        conn.commit()

    with pytest.raises(OstrovokError, match="нет цен"):
        await search(dsn)


async def test_unreachable_database_raises():
    request = StaySearch(checkin=CHECKIN, checkout=CHECKOUT)

    client = OstrovokClient("postgresql://nobody@127.0.0.1:1/none", connect_timeout=1)

    with pytest.raises(OstrovokError, match="база островка недоступна"):
        await client.search_hotels(request, today=TODAY)
