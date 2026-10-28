import json
from datetime import date

import httpx
from ostrovok_helpers import FakeTime

from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.http import OstrovokHttp


def make_api(handler):
    cfg = ScraperConfig(_env_file=None)
    t = FakeTime()
    http = OstrovokHttp(cfg, transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock)
    return OstrovokApi(http, cfg, new_uuid=lambda: "u1"), t


def test_search_polls_until_finished_and_keeps_session_region():
    posts = []

    def handler(request):
        if request.method == "GET":
            body = json.loads(request.url.params["body"])
            assert body["session_params"]["paxes"] == [{"adults": 6, "child_ages": [16]}]
            assert body["session_params"]["region_id"] == 6288539
            return httpx.Response(
                200,
                json={"session_info": {"session": {"id": "s1"}, "region_related": {"region": {"region_id": 6288539}}}},
            )
        posts.append(json.loads(request.content))
        assert request.url.params["session"] == "s1"
        return httpx.Response(200, json={"search_finished": len(posts) == 2, "map_hotels": [{"ota_hotel_id": "x"}]})

    api, t = make_api(handler)
    fetched = api.search(date(2026, 11, 17), [{"adults": 6, "child_ages": [16]}])
    assert fetched.incomplete is False
    assert fetched.status_code == 200
    assert len(posts) == 2
    assert posts[0]["map_hotels"] is True and posts[0]["filters"]["kinds"][0] == "hotel"
    assert posts[0]["session_params"]["departure_date"] == "2026-11-18"
    assert fetched.body["session_region"] == {"region_id": 6288539}
    assert t.sleeps.count(2.0) >= 2


def test_search_marks_incomplete_after_max_polls():
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"session_info": {"session": {"id": "s1"}, "region_related": {"region": {}}}}
            )
        posts.append(1)
        return httpx.Response(200, json={"search_finished": False})

    api, _ = make_api(handler)
    fetched = api.search(date(2026, 11, 17), [{"adults": 2}])
    assert len(posts) == 15
    assert fetched.incomplete is True


def test_hotel_page_builds_url_and_extracts_next_data():
    data = {"hotel": {"id": "don_kikhot_hotel_"}, "hotelRegionData": {"x": 1}, "reviews": {"total": 3}}
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": data}})
        + "</script></html>"
    )
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text=html)

    api, _ = make_api(handler)
    fetched = api.hotel_page("russia/kazan", 6479652, "don_kikhot_hotel_")
    assert seen == ["/hotel/russia/kazan/mid6479652/don_kikhot_hotel_/"]
    assert fetched.body == data
    assert fetched.status_code == 200


def test_rates_and_reviews_params():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    api, _ = make_api(handler)
    paxes = [{"adults": 2}]
    api.rates("x", 1993, date(2026, 11, 17), paxes)
    body = json.loads(seen[0].url.params["body"])
    assert seen[0].url.path == "/hotel/search/v1/site/hp/search"
    assert body["hotel"] == "x" and body["region_id"] == 1993
    assert body["arrival_date"] == "2026-11-17" and body["departure_date"] == "2026-11-18"
    assert body["paxes"] == paxes

    api.reviews("x", 2)
    assert seen[1].url.path == "/hotel/search/v1/site/reviews"
    assert dict(seen[1].url.params) == {
        "hotel": "x",
        "lang": "ru",
        "sort": "relevance_desc",
        "page": "2",
        "page_size": "500",
    }


def test_rates_null_is_incomplete():
    """`rates: null` — островок не отдал тарифы (бывает временно), ответ помечается неполным для --retry-failed."""
    bodies = iter([{"rates": None}, {"rates": []}, {"rates": [{"hash": "sr-a"}]}])

    def handler(request):
        return httpx.Response(200, json=next(bodies))

    api, _ = make_api(handler)
    paxes = [{"adults": 2}]
    assert api.rates("x", 1993, date(2026, 11, 17), paxes).incomplete is True
    assert api.rates("x", 1993, date(2026, 11, 17), paxes).incomplete is False
    assert api.rates("x", 1993, date(2026, 11, 17), paxes).incomplete is False
