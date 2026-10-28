import json
import threading
from unittest.mock import patch

import pytest
import requests
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from agent import build_agent
from geo_tools import overpass_query
from map_tools import collection, compose_routes, feature, map_result
from web_tools import search_web


def response(status, data):
    r = requests.Response()
    r.status_code = status
    r.url = "https://example.test/api/interpreter"
    r._content = json.dumps(data).encode()
    return r


def test_overpass_six_attempts_share_one_budget_and_retry_incomplete_timeout():
    failures = [response(504, {}), response(200, {"elements": [], "remark": "runtime error: Query timed out"})]
    with (
        patch("geo_tools.cached_result", side_effect=lambda k, p, f: f()),
        patch("geo_tools.map_result", side_effect=lambda content, variants: (content, variants)),
        patch(
            "retry_utils.requests.post",
            side_effect=[*failures, *failures, response(504, {}), response(200, {"elements": []})],
        ) as post,
    ):
        result, _ = overpass_query.func("[out:json];node(1);out;")
    assert post.call_count == 6 and result["total"] == 0


def test_overpass_invalid_query_http400_is_not_retried():
    with (
        patch("geo_tools.cached_result", side_effect=lambda k, p, f: f()),
        patch("retry_utils.requests.post", return_value=response(400, {})) as post,
    ):
        with pytest.raises(requests.HTTPError):
            overpass_query.func("[out:json];bad;")
    assert post.call_count == 1


def test_web_cache_uses_original_date_and_does_not_turn_snippets_into_coordinates(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    with patch("web_tools.DDGS") as ddgs:
        ddgs.return_value.text.return_value = [
            {"title": "Источник", "href": "https://example.test/history", "body": "Текст"},
            {"title": "bad", "href": "javascript:alert(1)", "body": "bad"},
        ]
        first = search_web.invoke({"query": "Тяп-Ляп Казань"})
        second = search_web.invoke({"query": "Тяп-Ляп Казань"})
    assert ddgs.return_value.text.call_count <= 2
    assert second["cache_hit"] and second["retrieved_at"] == first["retrieved_at"]
    assert len(second["results"]) == 1 and "lat" not in second["results"][0]


def test_web_failure_is_not_cached_and_provider_error_not_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    with patch("web_tools.DDGS", side_effect=RuntimeError("private-token")) as ddgs:
        first = search_web.invoke({"query": "Казань история"})
        search_web.invoke({"query": "Казань история"})
    assert ddgs.call_count == 4 and not first["available"] and "private-token" not in str(first)


def line(start, end):
    return collection([feature({"type": "LineString", "coordinates": [start, end]}, name="Проверенный путь")])


def test_composed_route_retains_all_segments_and_rejects_disconnected_or_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    a, aa = map_result({"source": "OSM", "length_m": 650}, {"default": line([49.1, 55.79], [49.11, 55.79])})
    b, bb = map_result({"source": "OSM", "length_m": 650}, {"default": line([49.11, 55.79], [49.12, 55.79])})
    state = {"geo_results": {a["data_ref"]: aa, b["data_ref"]: bb}}
    segments = [{"data_ref": a["data_ref"], "variant": "default"}, {"data_ref": b["data_ref"], "variant": "default"}]
    result, artifact = compose_routes.func(segments, state)
    assert result["segment_count"] == 2 and len(artifact["collections"]["default"]["features"]) == 2
    road, road_artifact = map_result({"features": []}, {"default": line([49.1, 55.79], [49.11, 55.79])})
    state["geo_results"][road["data_ref"]] = road_artifact
    with pytest.raises(ValueError, match="not a calculated route"):
        compose_routes.func([{"data_ref": road["data_ref"], "variant": "default"}], state)
    with pytest.raises(ValueError, match="Unknown"):
        compose_routes.func([{"data_ref": "made-up", "variant": "default"}], state)
    with pytest.raises(ValueError, match="disconnected"):
        compose_routes.func([segments[1], segments[0]], state)


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_independent_tools_really_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(3)

    def work():
        barrier.wait(timeout=4)
        return "Проверено"

    @tool
    def wiki_check() -> str:
        """Test independent encyclopedia lookup."""
        return work()

    @tool
    def osm_check() -> str:
        """Test independent map lookup."""
        return work()

    @tool
    def web_check() -> str:
        """Test independent web lookup."""
        return work()

    tools = [wiki_check, osm_check, web_check]
    calls = [{"name": t.name, "args": {}, "id": str(i)} for i, t in enumerate(tools)]
    with patch("agent.TOOLS", tools):
        graph = build_agent(model=Model(responses=[AIMessage("", tool_calls=calls), AIMessage("Готово")]))
        result = graph.invoke({"messages": [("user", "Найди всё параллельно")]})
    assert result["messages"][-1].content == "Готово"
    assert all("Проверено" == m.content for m in result["messages"] if m.type == "tool")


def test_exhausted_tool_budget_still_synthesizes_useful_partial_answer(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "1")
    model = Model(
        responses=[
            AIMessage("", tool_calls=[{"name": "a", "args": {}, "id": "1"}, {"name": "b", "args": {}, "id": "2"}]),
            AIMessage("Источники найдены; маршрут пока не рассчитан. Для отелей нужны даты."),
        ]
    )
    result = build_agent(model=model).invoke({"messages": [("user", "Маршрут и отели")]})
    assert "Для отелей нужны даты" in result["messages"][-1].content
    assert result["call_usage"]["model_calls"] == 2


def test_noise_cache_reuses_individual_hotels_across_different_batches(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hotel_agent.tools.noise import NoisePoint
    from hotel_geo_tools import noise_payload

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    features = tmp_path / "features.npz"
    features.write_bytes(b"identity")
    grid = tmp_path / "grid.npz"
    grid.write_bytes(b"identity")
    cfg = SimpleNamespace(features_path=features, grid_path=grid)
    points = [NoisePoint(lat=55.79 + i * 0.001, lon=49.1) for i in range(3)]

    def assess(service, points):
        return json.dumps(
            {
                "points": [{"lat": p.lat, "lon": p.lon, "day": {"score": 30}, "night": {"score": 40}} for p in points],
                "data": "OSM snapshot",
                "scale": "relative",
            }
        )

    with (
        patch("hotel_geo_tools.noise_service", return_value=object()),
        patch("hotel_geo_tools.NoiseConfig", return_value=cfg),
        patch("hotel_geo_tools.assess_points", side_effect=assess) as calc,
    ):
        noise_payload(points[:2])
        result = noise_payload(points[1:])
    assert [len(call.args[1]) for call in calc.call_args_list] == [2, 1]
    assert len(result["points"]) == 2 and result["data"] == "OSM snapshot"


def test_route_research_switches_to_calculation_only_after_verified_pois():
    from agent import route_research_ready
    from langchain_core.messages import HumanMessage, ToolMessage

    state = {
        "call_usage": {"by_tool": {"overpass_query": 8}},
        "messages": [HumanMessage("Построй туристический маршрут")],
    }
    assert not route_research_ready(state)
    state["messages"].append(
        ToolMessage(
            "source",
            tool_call_id="1",
            artifact={
                "result": {
                    "elements": [
                        {"type": "way", "id": 1, "tags": {"name": "Садовое товарищество", "landuse": "allotments"}},
                        {"type": "way", "id": 2, "tags": {"name": "Школа 1", "amenity": "school"}},
                        {"type": "way", "id": 3, "tags": {"name": "Школа 2", "amenity": "school"}},
                    ]
                }
            },
        )
    )
    assert route_research_ready(state)
    state["messages"].append(HumanMessage("Теперь найди другой район"))
    assert not route_research_ready(state)


def test_empty_history_summary_retains_current_task_and_tool_pairs():
    from context_memory import summarize_safely
    from langchain_core.messages import HumanMessage, ToolMessage
    from unittest.mock import Mock

    messages = [
        HumanMessage("Туристический маршрут по Тяп-Ляп и отели", id="user"),
        AIMessage("", tool_calls=[{"name": "search_web", "id": "call", "args": {"query": "Казань"}}]),
        ToolMessage(json.dumps({"text": "Источник " * 1000}), tool_call_id="call"),
    ]
    summarizer = Mock()
    summarizer.invoke.return_value = {"context": {"running_summary": {"summary": ""}}, "summarized_messages": []}
    update = summarize_safely(summarizer, {"messages": messages})
    assert update["context"]["deterministic_summary"]
    assert update["summarized_messages"][0].content == messages[0].content
    assert update["summarized_messages"][-1].tool_call_id == "call"
    assert update["summarized_messages"][-2].tool_calls[0]["id"] == "call"
    summarize_safely(summarizer, {"messages": messages, **update})
    assert summarizer.invoke.call_count == 1
