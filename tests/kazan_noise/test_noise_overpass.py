import httpx
import pytest
from noise_helpers import FakeTime

from kazan_noise.config import NoiseConfig
from kazan_noise.fetch import fetch_all, load_raw
from kazan_noise.overpass import OverpassClient, OverpassError

BOUNDS = {"minlat": 55.6031357, "minlon": 48.8205846, "maxlat": 55.9382194, "maxlon": 49.3812469}


def make_client(responses):
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
    client = OverpassClient(
        NoiseConfig(_env_file=None), transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock
    )
    return client, seen, t


# Признак текста каждого запроса: по нему фейк узнаёт, какой запрос пришёл.
_MARKERS = {"boundary": "rel(", "buildings": "area(id:", "lines": '"highway"', "areas": 'nwr["landuse"'}


class FakeOverpass:
    """Подменяет клиент: граница с bounds, одно здание, остальные запросы — пустые elements."""

    def __init__(self, fail_on: str | None = None, empty: str | None = None):
        self.queries: list[str] = []
        self.fail_on = fail_on
        self.empty = empty

    def query(self, ql: str) -> dict:
        self.queries.append(ql)
        if self.fail_on and _MARKERS[self.fail_on] in ql:
            raise OverpassError("boom")
        if self.empty and _MARKERS[self.empty] in ql:
            return {"elements": []}
        if _MARKERS["boundary"] in ql:
            return {"elements": [{"type": "relation", "id": 367666, "bounds": BOUNDS}]}
        if _MARKERS["buildings"] in ql:
            return {"elements": [{"type": "way", "id": 1, "tags": {"building": "yes"}}]}
        return {"elements": []}


def test_retries_capacity_html_then_returns_json():
    client, seen, t = make_client(
        [
            httpx.Response(502),
            httpx.Response(200, text="<html>at capacity</html>"),
            httpx.Response(200, json={"elements": []}),
        ]
    )
    assert client.query("[out:json];") == {"elements": []}
    assert t.sleeps == [15.0, 30.0]


def test_timeout_remark_with_partial_data_is_retried():
    partial = {
        "elements": [{"type": "way", "id": 1}],
        "remark": 'runtime error: Query timed out in "query" at line 3 after 301 seconds.',
    }
    client, seen, _ = make_client([httpx.Response(200, json=partial), httpx.Response(200, json={"elements": []})])
    assert client.query("q") == {"elements": []} and len(seen) == 2


def test_bad_query_fails_without_retry():
    client, seen, _ = make_client([httpx.Response(400, text="parse error: line 1")])
    with pytest.raises(OverpassError, match="parse error"):
        client.query("q")
    assert len(seen) == 1


@pytest.mark.parametrize("status", [403, 404, 500])
def test_mirror_error_switches_to_next_mirror_without_retries(status):
    client, seen, t = make_client([httpx.Response(status), httpx.Response(200, json={"elements": []})])
    assert client.query("q") == {"elements": []}
    assert [r.url.host for r in seen] == ["overpass-api.de", "maps.mail.ru"] and t.sleeps == [10.0]


def test_mirror_errors_on_all_mirrors_raise():
    client, seen, _ = make_client([httpx.Response(404), httpx.Response(500)])
    with pytest.raises(OverpassError, match="HTTP 500"):
        client.query("q")
    assert len(seen) == 2


def test_non_dict_json_is_retried():
    client, seen, _ = make_client([httpx.Response(200, json=[1, 2]), httpx.Response(200, json={"elements": []})])
    assert client.query("q") == {"elements": []} and len(seen) == 2


def test_switches_to_mirror_after_retries_exhausted():
    client, seen, _ = make_client([httpx.Response(504)] * 5 + [httpx.Response(200, json={"elements": []})])
    client.query("q")
    assert [r.url.host for r in seen] == ["overpass-api.de"] * 5 + ["maps.mail.ru"]


def test_raises_when_all_mirrors_exhausted():
    client, seen, _ = make_client([httpx.ConnectError("down")] * 10)
    with pytest.raises(OverpassError):
        client.query("q")
    assert len(seen) == 10


def test_waits_min_interval_between_queries():
    client, _, t = make_client([httpx.Response(200, json={})] * 2)
    client.query("a")
    client.query("b")
    assert t.sleeps == [10.0]


def test_posts_form_with_user_agent():
    client, seen, _ = make_client([httpx.Response(200, json={})])
    client.query("[out:json];")
    assert seen[0].method == "POST" and seen[0].content == b"data=%5Bout%3Ajson%5D%3B"
    assert seen[0].headers["User-Agent"] == "hotel-agent-kazan-noise/0.1"


def test_fetch_all_saves_files_and_skips_existing(tmp_path):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    fake = FakeOverpass()
    assert fetch_all(fake, cfg) == ["boundary", "buildings", "lines", "areas"]
    assert "(55.5761" in fake.queries[2]  # bbox = bounds границы − 3 км
    assert fetch_all(fake, cfg) == [] and len(fake.queries) == 4
    assert load_raw(cfg, "boundary")["elements"][0]["bounds"] == BOUNDS


def test_fetch_all_refresh_redownloads(tmp_path):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    fetch_all(FakeOverpass(), cfg)
    assert fetch_all(FakeOverpass(), cfg, refresh=True) == ["boundary", "buildings", "lines", "areas"]


def test_failed_query_leaves_no_partial_file(tmp_path):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    with pytest.raises(OverpassError):
        fetch_all(FakeOverpass(fail_on="lines"), cfg)
    assert sorted(p.name for p in cfg.raw_dir.iterdir()) == ["boundary.json.gz", "buildings.json.gz"]
    assert fetch_all(FakeOverpass(), cfg) == ["lines", "areas"]


@pytest.mark.parametrize("name", ["boundary", "buildings"])
def test_empty_boundary_or_buildings_is_an_error_and_not_saved(tmp_path, name):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    with pytest.raises(OverpassError, match=f"{name}.*пуст"):
        fetch_all(FakeOverpass(empty=name), cfg)
    assert f"{name}.json.gz" not in [p.name for p in cfg.raw_dir.iterdir()]
