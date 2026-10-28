from datetime import date

import pytest
from ostrovok_helpers import load_fixture

from ostrovok_scraper.api import Fetched
from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.http import BlockedError, OstrovokHttpError
from ostrovok_scraper.steps import Item, run_items
from ostrovok_scraper.steps.inputs import available_nights, guests_hotels, selected_hotels, serp_hotels
from ostrovok_scraper.steps.rates import child_age_night, plan_rate_requests
from ostrovok_scraper.steps.reviews import review_pages
from ostrovok_scraper.steps.search import serp_guests_items, serp_items

CFG = ScraperConfig(_env_file=None)
REF_WD, REF_WE = date(2026, 11, 17), date(2026, 11, 20)


class FakeApi:
    pass


class FakeStore:
    """Словарь (step, key) -> (body, error, incomplete) с семантикой RawStore."""

    def __init__(self):
        self.rows = {}

    def save(self, step, key, *, request, status_code, body, error=None, incomplete=False):
        self.rows[(step, key)] = (body, error, incomplete)

    def existing_keys(self, step, *, include_failed=True):
        return {
            k for (s, k), (_, err, inc) in self.rows.items()
            if s == step and (include_failed or (err is None and not inc))
        }

    def bodies(self, step):
        return sorted((k, b) for (s, k), (b, err, _) in self.rows.items() if s == step and err is None)

    def incomplete_keys(self, step):
        return sorted(k for (s, k), (_, _, inc) in self.rows.items() if s == step and inc)

    def error(self, step, key):
        return self.rows[(step, key)][1]


def test_run_items_skips_existing_records_errors_and_retries_on_flag():
    store = FakeStore()
    store.save("rates", "done", request={}, status_code=200, body={})
    store.save("rates", "failed", request={}, status_code=500, body=None, error="boom")
    calls = []

    def ok(key):
        return Item(key, {"k": key}, lambda: calls.append(key) or Fetched(200, {"rates": []}))

    def bad(key):
        def fetch():
            raise OstrovokHttpError("bad request", status_code=400)
        return Item(key, {"k": key}, fetch)

    report = run_items(store, "rates", [ok("done"), ok("failed"), ok("new"), bad("broken")])
    assert (report.done, report.skipped, report.errors) == (1, 2, 1)
    assert calls == ["new"] and store.error("rates", "broken") == "bad request"
    report = run_items(store, "rates", [ok("done"), ok("failed"), bad("broken")], retry_failed=True)
    assert calls == ["new", "failed"] and (report.done, report.skipped, report.errors) == (1, 1, 1)


def test_run_items_records_key_error_and_continues():
    def broken():
        raise KeyError("session_info")

    store = FakeStore()
    report = run_items(store, "serp", [Item("a", {}, broken), Item("b", {}, lambda: Fetched(200, {}))])
    assert (report.done, report.errors) == (1, 1)
    assert "session_info" in store.error("serp", "a") and store.error("serp", "b") is None


def test_run_items_stops_on_block_without_saving():
    def blocked():
        raise BlockedError("403 x5")

    store = FakeStore()
    with pytest.raises(BlockedError):
        run_items(store, "serp", [Item("k", {}, blocked)])
    assert store.existing_keys("serp") == set()


def test_run_items_saves_incomplete_flag():
    store = FakeStore()
    run_items(store, "serp", [Item("k", {}, lambda: Fetched(200, {"x": 1}, incomplete=True))])
    assert store.rows[("serp", "k")] == ({"x": 1}, None, True)


def test_plan_rate_requests():
    available = {"a": {REF_WD, REF_WE}, "b": {date(2026, 11, 18)}}
    guests = {(4, REF_WD): {"a", "b"}, (8, REF_WE): {"a"}}
    assert sorted(plan_rate_requests(["a", "b"], available, guests, CFG)) == sorted([
        ("a", REF_WD, 2), ("a", REF_WE, 2), ("a", REF_WD, 4), ("a", REF_WE, 8),
        ("b", date(2026, 11, 18), 2), ("b", REF_WD, 4),
    ])


def test_plan_rate_requests_when_window_misses_reference_nights():
    # пробный прогон --nights 2 с 27.10 (вт) и 28.10 (ср): выходных ночей нет, 28.10 ближе к 17.11
    plan = plan_rate_requests(["a"], {"a": {date(2026, 10, 27), date(2026, 10, 28)}}, {}, CFG)
    assert plan == [("a", date(2026, 10, 28), 2)]


def test_child_age_night():
    assert child_age_night({REF_WD, REF_WE}, CFG) == REF_WD
    assert child_age_night({REF_WE}, CFG) == REF_WE
    assert child_age_night(set(), CFG) is None


@pytest.mark.parametrize(("total", "pages"), [(0, 0), (10, 0), (11, 1), (500, 1), (501, 2), (1386, 3)])
def test_review_pages(total, pages):
    assert review_pages(total) == pages


def test_serp_items_keys():
    items = serp_items(FakeApi(), CFG, today=date(2026, 11, 28), nights=None)
    assert [i.key for i in items] == ["night=2026-11-28;guests=2", "night=2026-11-29;guests=2"]
    assert len(serp_guests_items(FakeApi(), CFG)) == 14


def test_inputs_from_serp_bodies():
    store = FakeStore()
    store.save("serp", "night=2026-11-17;guests=2", request={}, status_code=200, body=load_fixture("serp_map.json"))
    store.save("serp_guests", "night=2026-11-20;guests=4", request={}, status_code=200,
               body={"map_hotels": [load_fixture("serp_map.json")["map_hotels"][0]]})
    assert set(serp_hotels(store)) == {"don_kikhot_hotel_", "gostinitsa_amsterdam_na_tukaya_hotel"}
    assert available_nights(store)["don_kikhot_hotel_"] == {REF_WD}
    assert guests_hotels(store) == {(4, REF_WE): {"don_kikhot_hotel_"}}
    assert selected_hotels(["b", "a", "c"], 2) == ["a", "b"]


def test_retry_of_incomplete_key_keeps_partial_body_on_failure():
    store = FakeStore()
    store.save("serp", "k", request={}, status_code=200, body={"map_hotels": [1]}, incomplete=True)

    def fail():
        raise OstrovokHttpError("HTTP 500", status_code=500)

    report = run_items(store, "serp", [Item("k", {}, fail)], retry_failed=True)
    assert report.errors == 1 and report.done == 0
    assert store.rows[("serp", "k")] == ({"map_hotels": [1]}, None, True)


def test_page_facts_tolerates_missing_reviews():
    from ostrovok_scraper.steps.inputs import page_facts

    store = FakeStore()
    store.save("hotels", "hotel=a", request={}, status_code=200,
               body={"hotel": {"location": {"regionId": 1993}}, "reviews": None})
    store.save("hotels", "hotel=b", request={}, status_code=200,
               body={"hotel": {"location": {"regionId": 7}}, "reviews": {}})
    store.save("hotels", "hotel=c", request={}, status_code=200, body={"hotel": {"location": {}}})
    assert page_facts(store) == {"a": (1993, 0), "b": (7, 0)}
