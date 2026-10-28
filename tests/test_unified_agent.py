import json
from datetime import date
from unittest.mock import patch

import pytest
from agent_helpers import KAZAN_RESULT, FakeOstrovok
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from hotel_agent.apis.ostrovok.models import StaySearch
from hotel_geo_tools import find_hotels, select_quiet_hotels


def hotels():
    base = KAZAN_RESULT.hotels[0]
    return [
        base.model_copy(
            update={
                "hotel_id": str(i),
                "name": "Отель " + str(i),
                "city": "Казань",
                "lat": 55.79 + i * 0.001,
                "lon": 49.1,
            }
        )
        for i in range(5)
    ]


def assessment(items, scores):
    return {
        "available": True,
        "points": [
            {
                "lat": h.lat,
                "lon": h.lon,
                "day": {"score": day, "label": "тест"},
                "night": {"score": night, "label": "тест"},
            }
            for h, (day, night) in zip(items, scores)
        ],
    }


def test_threshold_includes_50_excludes_day_or_night_51_and_preserves_ties():
    items = hotels()
    selected, counts = select_quiet_hotels(items, assessment(items, [(50, 50), (51, 10), (10, 51), (20, 30), (30, 30)]))
    assert [h["hotel_id"] for h in selected] == ["3", "4", "0"]
    assert counts == {"rejected_noise": 2, "unverified_noise": 0}


def test_missing_noise_does_not_become_a_quiet_recommendation():
    selected, counts = select_quiet_hotels(hotels(), {"points": [], "available": False})
    assert selected == [] and counts["unverified_noise"] == 5


async def test_search_filters_before_publishing_map_and_keeps_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    items = hotels()
    client = FakeOstrovok(
        KAZAN_RESULT.model_copy(update={"hotels": items, "found": 5, "data_as_of": date(2026, 10, 26)})
    )
    with patch(
        "hotel_geo_tools.noise_payload",
        return_value=assessment(items, [(51, 5), (5, 51), (50, 50), (10, 20), (20, 30)]),
    ):
        payload, artifact = await find_hotels(
            StaySearch(checkin=date(2026, 11, 19), checkout=date(2026, 11, 22), city="Казань", limit=2), client
        )
    assert [h["hotel_id"] for h in payload["hotels"]] == ["3", "4"]
    assert [f["properties"]["hotel_id"] for f in artifact["collections"]["default"]["features"]] == ["3", "4"]
    assert payload["noise_limit"] == 50 and payload["data_as_of"]
    assert client.requests[0][0].limit == 30


async def test_empty_noise_result_broadens_geography_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    items = hotels()
    client = FakeOstrovok(
        KAZAN_RESULT.model_copy(update={"hotels": items, "found": 5, "data_as_of": date(2026, 10, 26)})
    )
    request = StaySearch(
        checkin=date(2026, 11, 19),
        checkout=date(2026, 11, 22),
        city="Казань",
        max_distance_km=0.2,
        max_price_per_night=7000,
    )
    with patch("hotel_geo_tools.noise_payload", return_value=assessment(items, [(80, 90)] * 5)):
        payload, _ = await find_hotels(request, client)
    assert len(client.requests) == 2 and payload["hotels"] == []
    assert payload["geographic_filter_broadened"]
    assert client.requests[1][0].max_distance_km == 3
    assert client.requests[1][0].max_price_per_night == 7000


async def test_no_noise_does_not_trigger_pointless_repeated_search(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    client = FakeOstrovok(KAZAN_RESULT)
    with patch("hotel_geo_tools.noise_payload", return_value={"available": False, "points": []}):
        payload, _ = await find_hotels(
            StaySearch(checkin=date(2026, 11, 19), checkout=date(2026, 11, 22), city="Казань", max_distance_km=0.2),
            client,
        )
    assert len(client.requests) == 1 and payload["hotels"] == []


class HotelThenMap(BaseChatModel):
    @property
    def _llm_type(self):
        return "test"

    def bind_tools(self, tools, **kwargs):
        assert {"search_hotels_in_tatarstan", "plan_route", "wikipedia_search", "update_map"} <= {t.name for t in tools}
        return self

    def _generate(self, messages, **kwargs):
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        if not tools:
            response = AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "search_hotels_in_tatarstan",
                        "args": {"checkin": "2026-11-19", "checkout": "2026-11-22", "city": "Казань"},
                        "id": "hotels",
                    }
                ],
            )
        elif len(tools) == 1:
            data = json.loads(tools[0].content)
            response = AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "update_map",
                        "args": {
                            "operation": "upsert",
                            "layer_id": "hotels",
                            "title": "Отели",
                            "data_ref": data["data_ref"],
                            "kind": "points",
                        },
                        "id": "map",
                    }
                ],
            )
        else:
            response = AIMessage("Подходящий отель показан на карте.")
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_shared_agent_search_updates_map_with_same_history(tmp_path, monkeypatch):
    from agent import build_agent
    from map_tools import read_geo_result

    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    items = hotels()
    client = FakeOstrovok(
        KAZAN_RESULT.model_copy(update={"hotels": items, "found": 5, "data_as_of": date(2026, 10, 26)})
    )
    with (
        patch("hotel_geo_tools.OstrovokClient.from_settings", return_value=client),
        patch(
            "hotel_geo_tools.noise_payload",
            return_value=assessment(items, [(50, 50), (70, 80), (20, 30), (51, 10), (20, 51)]),
        ),
    ):
        graph = build_agent(model=HotelThenMap(), checkpointer=InMemorySaver())
        state = graph.invoke(
            {"messages": [HumanMessage("Подбери отель с 19 по 22 ноября")]},
            config={"configurable": {"thread_id": "combined"}},
        )
    layer = state["map_layers"]["hotels"]
    exact = read_geo_result.func(layer["data_ref"], state)
    assert len(exact["locations"]) == 2
    assert exact["locations"][0]["name"] == "Отель 2"
    assert state["messages"][-1].content == "Подходящий отель показан на карте."


def test_missing_events_returns_explicit_unavailability_not_no_events():
    from unittest.mock import AsyncMock

    from hotel_agent.apis.kudago.models import EventsError
    from hotel_geo_tools import events_in_kazan

    client = AsyncMock()
    client.events_for_stay.side_effect = EventsError("not loaded")
    with patch("hotel_geo_tools.EventsClient.from_settings", return_value=client):
        result = events_in_kazan.invoke({"checkin": "2026-11-19", "checkout": "2026-11-22"})
    assert result["available"] is False
    assert "не означает" in result["note"]
