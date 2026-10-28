import httpx
import pytest
from ostrovok_helpers import FakeTime

from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.http import BlockedError, OstrovokHttp, OstrovokHttpError


def make_http(responses):
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
    http = OstrovokHttp(
        ScraperConfig(_env_file=None), transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock
    )
    return http, seen, t


def test_waits_min_interval_between_requests():
    http, _, t = make_http([httpx.Response(200, json={}), httpx.Response(200, json={})])
    http.request("GET", "/a")
    http.request("GET", "/b")
    assert t.sleeps == [1.0]


def test_retries_429_and_5xx_with_backoff_then_succeeds():
    http, seen, t = make_http([httpx.Response(429), httpx.Response(502), httpx.Response(200, json={"ok": 1})])
    assert http.request("GET", "/a").json() == {"ok": 1}
    assert len(seen) == 3
    assert t.sleeps == [2.0, 5.0]


def test_gives_up_after_three_retries():
    http, seen, t = make_http([httpx.Response(500)] * 4)
    with pytest.raises(OstrovokHttpError) as exc:
        http.request("GET", "/a")
    assert exc.value.status_code == 500
    assert len(seen) == 4
    assert t.sleeps == [2.0, 5.0, 15.0]


def test_retries_timeout():
    http, seen, _ = make_http([httpx.ReadTimeout("slow"), httpx.Response(200, json={})])
    assert http.request("GET", "/a").status_code == 200
    assert len(seen) == 2


def test_other_4xx_is_not_retried():
    http, seen, _ = make_http([httpx.Response(400, json={"error": "too many adults. max: 6"})])
    with pytest.raises(OstrovokHttpError) as exc:
        http.request("GET", "/a")
    assert exc.value.status_code == 400
    assert "too many adults" in str(exc.value)
    assert len(seen) == 1


def test_blocked_after_five_consecutive_403():
    http, _, _ = make_http([httpx.Response(403)] * 5)
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):
            http.request("GET", "/a")
    with pytest.raises(BlockedError):
        http.request("GET", "/a")


def test_success_resets_block_counter():
    http, _, _ = make_http([httpx.Response(403)] * 4 + [httpx.Response(200, json={})] + [httpx.Response(403)] * 4)
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):
            http.request("GET", "/a")
    http.request("GET", "/ok")
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):  # не BlockedError: счётчик сброшен
            http.request("GET", "/a")


def test_follows_redirects_and_sends_browser_user_agent():
    http, seen, _ = make_http(
        [
            httpx.Response(301, headers={"Location": "https://ostrovok.ru/hotel/russia/naberezhnye_chelny/mid1/x/"}),
            httpx.Response(200, text="<html></html>"),
        ]
    )
    assert http.request("GET", "/hotel/russia/kazan/mid1/x/").status_code == 200
    assert seen[1].url.path == "/hotel/russia/naberezhnye_chelny/mid1/x/"
    assert seen[0].headers["User-Agent"].startswith("Mozilla/5.0")


def test_retries_connect_error_then_succeeds():
    http, seen, t = make_http([httpx.ConnectError("down"), httpx.Response(200, json={"ok": 1})])
    assert http.request("GET", "/a").json() == {"ok": 1}
    assert len(seen) == 2 and t.sleeps == [2.0]


def test_transport_error_gives_up_with_empty_status_code():
    http, seen, t = make_http([httpx.RemoteProtocolError("reset")] * 4)
    with pytest.raises(OstrovokHttpError) as info:
        http.request("GET", "/a")
    assert info.value.status_code is None and len(seen) == 4
    assert t.sleeps == [2.0, 5.0, 15.0]


def test_too_many_redirects_is_wrapped_without_retry():
    http, seen, t = make_http([httpx.TooManyRedirects("loop")])
    with pytest.raises(OstrovokHttpError) as info:
        http.request("GET", "/a")
    assert info.value.status_code is None and len(seen) == 1 and t.sleeps == []
