import os
from decimal import Decimal

import psycopg
import pytest
from ostrovok_helpers import load_fixture

from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.normalize import normalize
from ostrovok_scraper.store import RawStore

pytestmark = pytest.mark.db

CFG = ScraperConfig(_env_file=None)


def seed(store: RawStore) -> None:
    store.save("serp", "night=2026-11-17;guests=2", request={}, status_code=200, body=load_fixture("serp_map.json"))
    store.save("hotels", "hotel=don_kikhot_hotel_", request={}, status_code=200, body=load_fixture("hotel_page.json"))
    store.save(
        "hotels",
        "hotel=gostinitsa_amsterdam_na_tukaya_hotel",
        request={},
        status_code=None,
        body=None,
        error="HTTP 500",
    )
    store.save(
        "child_age",
        "hotel=don_kikhot_hotel_;night=2026-11-17",
        request={},
        status_code=200,
        body={"rates": [{"occupancy": 3}]},
    )
    store.save(
        "rates",
        "hotel=don_kikhot_hotel_;night=2026-11-17;guests=2",
        request={},
        status_code=200,
        body=load_fixture("rates_2.json"),
    )


def fetch(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_normalize_builds_tables(db_conn):
    seed(RawStore(db_conn))
    report = normalize(db_conn, CFG)
    assert report.counts == {"regions": 2, "hotels": 1, "rooms": 2, "night_prices": 1, "rates": 3, "reviews": 2}
    assert report.rates_by_guests == {2: 3}
    assert report.anomalies["hotels_without_page"] == ["gostinitsa_amsterdam_na_tukaya_hotel"]
    assert report.anomalies["rates_unknown_rg"] == ["don_kikhot_hotel_:999999"]
    assert report.skipped["rates"] == 2 and report.skipped["serp"] == 1

    [hotel] = fetch(db_conn, "SELECT * FROM ostrovok.hotels")
    assert (hotel["stars"], hotel["free_child_age"], hotel["free_child_age_source"]) == (3, 3, "probe")
    assert "contacts" not in hotel
    assert {r["region_id"]: r["parent_id"] for r in fetch(db_conn, "SELECT * FROM ostrovok.regions")} == {
        6288539: None,
        1993: 6288539,
    }

    ratios = {r["source_rate_hash"]: r["price_ratio"] for r in fetch(db_conn, "SELECT * FROM ostrovok.rates")}
    assert ratios == {"sr-a": Decimal("1.0000"), "sr-c": Decimal("1.1054"), "sr-d": Decimal("1.2480")}

    reviews = fetch(db_conn, "SELECT * FROM ostrovok.reviews ORDER BY review_id")
    assert [r["review_id"] for r in reviews] == [1315975, 1952999]
    meta = {m["key"]: m["value"] for m in fetch(db_conn, "SELECT * FROM ostrovok.meta")}
    assert meta["window_from"] == "2026-11-17" and meta["ref_weekend"] == "2026-11-20"


def test_normalize_is_idempotent(db_conn):
    seed(RawStore(db_conn))
    assert normalize(db_conn, CFG).counts == normalize(db_conn, CFG).counts


def test_reviews_come_from_api_when_more_than_ten(db_conn):
    store = RawStore(db_conn)
    seed(store)
    page = load_fixture("hotel_page.json")
    page["reviews"]["total"] = 12
    store.save("hotels", "hotel=don_kikhot_hotel_", request={}, status_code=200, body=page)
    store.save(
        "reviews", "hotel=don_kikhot_hotel_;page=1", request={}, status_code=200, body=load_fixture("reviews_api.json")
    )
    normalize(db_conn, CFG)
    replies = {r["review_id"]: r["hotel_reply"] for r in fetch(db_conn, "SELECT * FROM ostrovok.reviews")}
    assert replies == {2164782: None, 378605: "Спасибо за отзыв!", 555001: None}


def test_broken_page_is_skipped(db_conn):
    store = RawStore(db_conn)
    seed(store)
    store.save(
        "hotels",
        "hotel=gostinitsa_amsterdam_na_tukaya_hotel",
        request={},
        status_code=200,
        body={"hotel": {}, "hotelRegionData": {}, "reviews": {"total": 0, "list": []}},
    )
    report = normalize(db_conn, CFG)
    assert report.skipped["hotels"] == 1 and report.counts["hotels"] == 1
    assert report.anomalies["hotels_without_page"] == []
    for table in ("hotels", "rooms", "night_prices", "rates", "reviews"):
        rows = fetch(db_conn, f"SELECT * FROM ostrovok.{table}")
        assert all("amsterdam" not in str(r) for r in rows), table


def test_normalize_commits(db_conn):
    seed(RawStore(db_conn))
    report = normalize(db_conn, CFG)
    assert db_conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(os.environ["OSTROVOK_TEST_DSN"]) as other:
        n = other.execute("SELECT count(*) FROM ostrovok.hotels").fetchone()[0]
    assert n == report.counts["hotels"] == 1


def test_page_without_reviews_still_gives_hotel(db_conn):
    store = RawStore(db_conn)
    seed(store)
    page = load_fixture("hotel_page.json")
    page["reviews"] = None
    store.save("hotels", "hotel=don_kikhot_hotel_", request={}, status_code=200, body=page)
    report = normalize(db_conn, CFG)
    assert report.counts["hotels"] == 1 and report.counts["reviews"] == 0
    [hotel] = fetch(db_conn, "SELECT * FROM ostrovok.hotels")
    assert hotel["reviews_total"] in (0, None)


def test_duplicate_master_id_keeps_first_hotel(db_conn):
    store = RawStore(db_conn)
    seed(store)
    twin = load_fixture("hotel_page.json")
    twin["hotel"]["id"] = "zz_twin"
    store.save("hotels", "hotel=zz_twin", request={}, status_code=200, body=twin)
    report = normalize(db_conn, CFG)
    assert report.skipped["hotels"] == 1 and report.counts["hotels"] == 1
    assert [h["hotel_id"] for h in fetch(db_conn, "SELECT * FROM ostrovok.hotels")] == ["don_kikhot_hotel_"]


def test_duplicate_hotel_id_in_other_page_is_skipped(db_conn):
    store = RawStore(db_conn)
    seed(store)
    twin = load_fixture("hotel_page.json")
    twin["hotel"]["masterId"] = 1
    store.save("hotels", "hotel=zz_other", request={}, status_code=200, body=twin)
    report = normalize(db_conn, CFG)
    assert report.skipped["hotels"] == 1 and report.counts["hotels"] == 1


def test_reviews_fall_back_to_page_when_api_step_missing(db_conn):
    store = RawStore(db_conn)
    seed(store)
    page = load_fixture("hotel_page.json")
    page["reviews"]["total"] = 12
    store.save("hotels", "hotel=don_kikhot_hotel_", request={}, status_code=200, body=page)
    report = normalize(db_conn, CFG)
    assert report.counts["reviews"] == 2
