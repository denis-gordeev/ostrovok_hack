import httpx
import pytest
from kudago_helpers import WINDOW_END, WINDOW_START, FakeTime, api_event, ts

from kudago_events.api import KudagoApi, KudagoApiError
from kudago_events.config import KudagoConfig

PAGE_2 = "https://kudago.com/public-api/v1.4/events/?location=kzn&page=2"


def make_api(responses, **cfg):
    """responses — список httpx.Response или исключений, отдаются по порядку."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    t = FakeTime()
    config = KudagoConfig(_env_file=None, **cfg)
    api = KudagoApi(config, transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock)
    return api, seen, t


def page(*events, next_url=None):
    return httpx.Response(200, json={"count": len(events), "next": next_url, "previous": None, "results": list(events)})


def fetch(api):
    return list(api.events(WINDOW_START, WINDOW_END))


def test_first_request_asks_for_kazan_events_in_window_with_dates_and_links():
    api, seen, _ = make_api([page(api_event(1))])
    fetch(api)
    [request] = seen
    assert request.url.host == "kudago.com" and request.url.path == "/public-api/v1.4/events/"
    params = request.url.params
    assert params["location"] == "kzn"
    assert (params["actual_since"], params["actual_until"]) == (str(ts(WINDOW_START)), str(ts(WINDOW_END)))
    assert params["page_size"] == "100"
    assert set(params["expand"].split(",")) == {"place", "dates"}
    assert {"id", "title", "site_url", "dates", "place", "categories", "favorites_count"} <= set(
        params["fields"].split(",")
    )
    assert (params["text_format"], params["order_by"]) == ("text", "id")


def test_follows_next_pages_until_null():
    api, seen, _ = make_api([page(api_event(1), api_event(2), next_url=PAGE_2), page(api_event(3))])
    assert [e["id"] for e in fetch(api)] == [1, 2, 3]
    assert str(seen[1].url) == PAGE_2


def test_waits_min_interval_between_requests():
    api, _, t = make_api([page(api_event(1), next_url=PAGE_2), page(api_event(2))])
    fetch(api)
    assert t.sleeps == [1.0]


def test_retries_5xx_429_and_timeouts_then_succeeds():
    api, seen, t = make_api([httpx.Response(502), httpx.ReadTimeout("slow"), httpx.Response(429), page(api_event(1))])
    assert [e["id"] for e in fetch(api)] == [1]
    assert len(seen) == 4
    assert t.sleeps == [2.0, 5.0, 15.0]


def test_gives_up_after_retries():
    api, seen, _ = make_api([httpx.Response(500)] * 4)
    with pytest.raises(KudagoApiError) as exc:
        fetch(api)
    assert exc.value.status_code == 500
    assert len(seen) == 4


def test_forbidden_stops_at_once_and_hints_at_ip():
    """С иностранных IP API отвечает 403: повторы не помогут."""
    api, seen, t = make_api([httpx.Response(403, text="<html>403 Forbidden</html>")])
    with pytest.raises(KudagoApiError) as exc:
        fetch(api)
    assert exc.value.status_code == 403
    assert "HTTPS_PROXY" in str(exc.value)
    assert len(seen) == 1 and t.sleeps == []


def test_refuses_next_link_to_another_host():
    api, seen, _ = make_api([page(api_event(1), next_url="https://evil.example/page2")])
    with pytest.raises(KudagoApiError):
        fetch(api)
    assert len(seen) == 1


def test_stops_after_max_pages():
    api, seen, _ = make_api([page(api_event(1), next_url=PAGE_2)] * 3, max_pages=2)
    with pytest.raises(KudagoApiError):
        fetch(api)
    assert len(seen) == 2
