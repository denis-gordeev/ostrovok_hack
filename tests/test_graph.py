import json

import pytest
from agent_helpers import KAZAN_RESULT, TODAY, FakeOstrovok, ScriptedChatModel, tatarstan_call, tool_result
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from hotel_agent.graph.builder import build_graph
from hotel_agent.graph.context import AgentContext
from kazan_noise.service import ModeScore, NoiseReport, OutsideKazanError, SourceInfo


@pytest.fixture
def run(settings):
    graph = build_graph(checkpointer=InMemorySaver())

    async def _run(text, *, llm: ScriptedChatModel, ostrovok=None, noise=None, thread_id="t1"):
        context = AgentContext(
            llm=llm,
            settings=settings,
            ostrovok=ostrovok,
            noise=noise,
            today=lambda: TODAY,
        )
        return await graph.ainvoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": thread_id}},
            context=context,
        )

    return _run


async def test_agent_can_answer_without_calling_tools(run):
    llm = ScriptedChatModel(responses=[AIMessage("На какие даты ищем?")])
    ostrovok = FakeOstrovok(KAZAN_RESULT)

    state = await run("Хочу отель в Казани", llm=llm, ostrovok=ostrovok)

    assert state["messages"][-1].content == "На какие даты ищем?"
    assert ostrovok.requests == []


@pytest.fixture
def template_prompt(monkeypatch):
    """Промпт агента пишет пользователь: тесты проверяют подстановку плейсхолдеров, а не его текст."""
    monkeypatch.setattr("hotel_agent.graph.nodes.AGENT_SYSTEM_PROMPT", "Today is {current_date}.")


async def test_agent_gets_today_and_tools(run, template_prompt):
    llm = ScriptedChatModel(responses=[AIMessage("Ок")])

    await run("Отель с 19 по 22 ноября", llm=llm)

    system = llm.received[0][0]
    assert isinstance(system, SystemMessage)
    assert system.content == "Today is 2026-10-24."
    assert llm.bound_tools == ["search_hotels_in_tatarstan", "noise_in_kazan", "events_in_kazan", "kazan_event_details"]


async def test_dates_given_in_a_later_turn_lead_to_search_with_full_history(run):
    llm = ScriptedChatModel(
        responses=[
            AIMessage("На какие даты ищем?"),
            tatarstan_call(checkin="2026-11-19", checkout="2026-11-22", city="Казань"),
            AIMessage("Нашёл Korston"),
        ]
    )
    ostrovok = FakeOstrovok(KAZAN_RESULT)

    await run("Хочу отель в Казани", llm=llm, ostrovok=ostrovok)
    state = await run("С 19 по 22 ноября", llm=llm, ostrovok=ostrovok)

    second_turn_input = llm.received[1]
    assert [m.content for m in second_turn_input if isinstance(m, HumanMessage)] == [
        "Хочу отель в Казани",
        "С 19 по 22 ноября",
    ]
    assert state["hotel_candidates"] == KAZAN_RESULT.hotels


async def test_tatarstan_tool_passes_request_and_returns_candidates(run):
    llm = ScriptedChatModel(
        responses=[
            tatarstan_call(
                checkin="2026-11-19",
                checkout="2026-11-22",
                city="Казань",
                children_ages=[4],
                near={"lat": 55.7986, "lon": 49.1056},
                meal="breakfast",
            ),
            AIMessage("Нашёл Korston"),
        ]
    )
    ostrovok = FakeOstrovok(KAZAN_RESULT)

    state = await run("Казань, 19–22 ноября", llm=llm, ostrovok=ostrovok)

    [(request, today)] = ostrovok.requests
    assert today == TODAY
    assert (request.city, request.adults, request.children_ages, request.meal) == ("Казань", 2, [4], "breakfast")
    assert (request.near.lat, request.near.lon) == (55.7986, 49.1056)
    result = tool_result(llm)
    assert result.tool_call_id == "call_t"
    payload = json.loads(result.content)
    assert (payload["found"], payload["hotels"][0]["price_total"]) == (1, 39450)
    assert "distance_km" not in payload["hotels"][0]
    assert state["hotel_candidates"] == KAZAN_RESULT.hotels


async def test_tatarstan_tool_returns_errors_to_agent(run):
    llm = ScriptedChatModel(
        responses=[tatarstan_call(checkin="2026-12-10", checkout="2026-12-12"), AIMessage("Таких дат нет")]
    )
    ostrovok = FakeOstrovok(error="Цены есть только для заезда не раньше 27.10.2026")

    state = await run("Казань", llm=llm, ostrovok=ostrovok)

    assert tool_result(llm).content == "Поиск не выполнен: Цены есть только для заезда не раньше 27.10.2026"
    assert "hotel_candidates" not in state


async def test_tatarstan_tool_without_client_says_search_is_unavailable(run):
    llm = ScriptedChatModel(
        responses=[tatarstan_call(checkin="2026-11-19", checkout="2026-11-22"), AIMessage("Недоступно")]
    )

    await run("Казань", llm=llm)

    assert tool_result(llm).content == "Поиск по Татарстану сейчас недоступен."


NOISE_REPORT = NoiseReport(
    lat=55.7963,
    lon=49.1088,
    inside_building=False,
    day=ModeScore(score=81, label="очень шумно"),
    night=ModeScore(score=64, label="шумно"),
    quiet_side=None,
    main_sources=[
        SourceInfo(
            kind="road",
            subtype="primary",
            name="улица Баумана",
            distance_m=35,
            share_day=0.7,
            share_night=0.5,
            shielded=False,
        )
    ],
    model_level_day=63.2,
    model_level_night=55.1,
    osm_timestamp="2026-10-26T18:41:36Z",
)


KREMLIN = (55.7963, 49.1088)
ARENA = (55.8209, 49.1606)
MOSCOW = (55.0, 37.6)


class FakeNoise:
    """Вместо NoiseService: отчёт с координатами точки, для точек из outside — OutsideKazanError."""

    def __init__(self, outside: list[tuple[float, float]] = ()):
        self.outside = set(outside)
        self.calls: list[tuple[float, float]] = []

    def noise_many(self, points: list[tuple[float, float]]) -> list[NoiseReport | OutsideKazanError]:
        self.calls.extend(points)
        return [
            (
                OutsideKazanError("Точка вне городского округа Казань")
                if (lat, lon) in self.outside
                else NOISE_REPORT.model_copy(update={"lat": lat, "lon": lon})
            )
            for lat, lon in points
        ]


def noise_call(*points: tuple[float, float]) -> AIMessage:
    args = {"points": [{"lat": lat, "lon": lon} for lat, lon in points or [KREMLIN]]}
    return AIMessage("", tool_calls=[{"name": "noise_in_kazan", "args": args, "id": "call_n"}])


async def test_noise_tool_scores_every_point_in_order(run):
    noise = FakeNoise()
    llm = ScriptedChatModel(responses=[noise_call(ARENA, KREMLIN), AIMessage("Ночью шумно")])

    await run("Где тише?", llm=llm, noise=noise)

    out = json.loads(tool_result(llm).content)
    assert [(p["lat"], p["lon"]) for p in out["points"]] == [ARENA, KREMLIN]
    assert out["points"][1]["night"] == {"score": 64, "label": "шумно"}
    assert noise.calls == [ARENA, KREMLIN]
    assert "2026-10-26" in out["data"] and "© OpenStreetMap contributors" in out["data"]
    assert "не децибелы" in out["scale"]


async def test_noise_tool_reports_point_outside_kazan_and_scores_the_rest(run):
    llm = ScriptedChatModel(responses=[noise_call(MOSCOW, KREMLIN), AIMessage("Москва — не Казань")])

    await run("А в Москве?", llm=llm, noise=FakeNoise(outside=[MOSCOW]))

    moscow, kremlin = json.loads(tool_result(llm).content)["points"]
    assert moscow == {"lat": 55.0, "lon": 37.6, "error": "Оценка шума не выполнена: Точка вне городского округа Казань"}
    assert kremlin["day"]["score"] == 81


async def test_noise_tool_with_every_point_outside_kazan(run):
    llm = ScriptedChatModel(responses=[noise_call(MOSCOW), AIMessage("Это не Казань")])

    await run("А в Москве?", llm=llm, noise=FakeNoise(outside=[MOSCOW]))

    out = json.loads(tool_result(llm).content)
    assert [p["error"] for p in out["points"]] == ["Оценка шума не выполнена: Точка вне городского округа Казань"]


async def test_noise_tool_rejects_more_than_30_points(run):
    noise = FakeNoise()
    llm = ScriptedChatModel(responses=[noise_call(*[KREMLIN] * 31), AIMessage("Слишком много точек")])

    await run("Шумно ли тут?", llm=llm, noise=noise)

    assert tool_result(llm).status == "error"
    assert noise.calls == []


async def test_noise_tool_without_snapshot(run):
    llm = ScriptedChatModel(responses=[noise_call(), AIMessage("Не могу оценить")])
    await run("Шумно ли тут?", llm=llm)
    assert tool_result(llm).content.startswith("Оценка шума недоступна")
