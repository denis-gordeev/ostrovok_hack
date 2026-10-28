import json
import threading
from datetime import date

import pytest
from agent_helpers import TODAY, FakeEvents, ScriptedChatModel, tool_result
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from hotel_agent import cli
from hotel_agent.apis.kudago.models import EventDetails, EventItem, ExhibitionItem, StayEvents
from hotel_agent.graph.builder import build_graph
from hotel_agent.graph.context import AgentContext
from kudago_events.load import RefreshResult

STAY = StayEvents(
    period="2026-11-19 — 2026-11-22",
    events=[
        EventItem(id=1, title="Концерт «Пикник»", sessions=["2026-11-20 19:00"], url="https://kzn.kudago.com/event/e1/")
    ],
    exhibitions=[
        ExhibitionItem(id=2, title="Выставка «Луна»", until=date(2026, 12, 31), url="https://kzn.kudago.com/event/e2/")
    ],
    data="KudaGo, загружено 2026-10-28. У каждого упомянутого события указывай ссылку url.",
)
DETAILS = EventDetails(
    id=1, title="Концерт «Пикник»", categories=["concert"], sessions=["2026-11-20 19:00"],
    url="https://kzn.kudago.com/event/e1/", data="KudaGo, загружено 2026-10-28.",
)


def call(name: str, **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": "call_e"}])


@pytest.fixture
def run(settings):
    graph = build_graph(checkpointer=InMemorySaver())

    async def _run(text, *, llm: ScriptedChatModel, events=None):
        context = AgentContext(llm=llm, settings=settings, events=events, today=lambda: TODAY)
        return await graph.ainvoke(
            {"messages": [HumanMessage(text)]}, config={"configurable": {"thread_id": "t"}}, context=context
        )

    return _run


async def test_events_tool_returns_selection_for_stay_dates(run):
    llm = ScriptedChatModel(
        responses=[call("events_in_kazan", checkin="2026-11-19", checkout="2026-11-22"), AIMessage("Вот события")]
    )
    events = FakeEvents(stay=STAY)

    await run("Что будет в Казани 19–22 ноября?", llm=llm, events=events)

    assert events.stay_requests == [(date(2026, 11, 19), date(2026, 11, 22))]
    payload = json.loads(tool_result(llm).content)
    assert payload["events"][0]["url"] == "https://kzn.kudago.com/event/e1/"
    assert payload["exhibitions"][0]["until"] == "2026-12-31"
    assert "note" not in payload


async def test_events_tool_passes_errors_to_agent(run):
    llm = ScriptedChatModel(
        responses=[call("events_in_kazan", checkin="2026-11-19", checkout="2026-11-22"), AIMessage("Без событий")]
    )
    await run("Казань", llm=llm, events=FakeEvents(error="События Казани пока не загружены."))
    assert tool_result(llm).content == "События Казани пока не загружены."


async def test_events_tool_without_client_says_events_are_unavailable(run):
    llm = ScriptedChatModel(
        responses=[call("events_in_kazan", checkin="2026-11-19", checkout="2026-11-22"), AIMessage("Ок")]
    )
    await run("Казань", llm=llm)
    assert tool_result(llm).content == "События Казани недоступны."


async def test_events_tool_rejects_checkout_before_checkin(run):
    llm = ScriptedChatModel(
        responses=[call("events_in_kazan", checkin="2026-11-22", checkout="2026-11-19"), AIMessage("Ок")]
    )
    events = FakeEvents(stay=STAY)
    await run("Казань", llm=llm, events=events)
    assert "раньше" in tool_result(llm).content
    assert events.stay_requests == []


async def test_details_tool_passes_id_dates_and_today(run):
    llm = ScriptedChatModel(
        responses=[
            call("kazan_event_details", event_id=1, checkin="2026-11-19", checkout="2026-11-22"),
            AIMessage("Подробности"),
        ]
    )
    events = FakeEvents(details=DETAILS)

    await run("Расскажи про концерт", llm=llm, events=events)

    assert events.detail_requests == [
        {"event_id": 1, "checkin": date(2026, 11, 19), "checkout": date(2026, 11, 22), "today": TODAY}
    ]
    assert json.loads(tool_result(llm).content)["title"] == "Концерт «Пикник»"


async def test_details_tool_ignores_half_given_dates(run):
    llm = ScriptedChatModel(responses=[call("kazan_event_details", event_id=1, checkin="2026-11-19"), AIMessage("Ок")])
    events = FakeEvents(details=DETAILS)
    await run("Расскажи про концерт", llm=llm, events=events)
    assert (events.detail_requests[0]["checkin"], events.detail_requests[0]["checkout"]) == (None, None)


def test_startup_refresh_reports_loaded_events(monkeypatch, capsys):
    monkeypatch.setattr(cli, "refresh_if_stale", lambda cfg: RefreshResult("loaded", events=812))
    cli._start_events_refresh().join(timeout=5)
    assert "загружено 812" in capsys.readouterr().err


def test_startup_refresh_failure_is_one_line_and_does_not_raise(monkeypatch, capsys):
    monkeypatch.setattr(cli, "refresh_if_stale", lambda cfg: RefreshResult("failed", error="403 Forbidden"))
    cli._start_events_refresh().join(timeout=5)
    err = capsys.readouterr().err
    assert "События KudaGo не обновлены: 403 Forbidden" in err and err.count("\n") == 1


def test_startup_refresh_is_silent_when_data_is_fresh(monkeypatch, capsys):
    monkeypatch.setattr(cli, "refresh_if_stale", lambda cfg: RefreshResult("fresh", events=812))
    cli._start_events_refresh().join(timeout=5)
    assert capsys.readouterr().err == ""


def test_startup_refresh_runs_in_daemon_thread(monkeypatch):
    seen = []

    def refresh(cfg):
        seen.append(threading.current_thread())
        return RefreshResult("fresh")

    monkeypatch.setattr(cli, "refresh_if_stale", refresh)
    thread = cli._start_events_refresh()
    thread.join(timeout=5)
    assert seen == [thread] and thread is not threading.main_thread() and thread.daemon
