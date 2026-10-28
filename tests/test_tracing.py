import io
import json

import pytest
from agent_helpers import (KAZAN_RESULT, TODAY, FakeOstrovok,
                           ScriptedChatModel, tatarstan_call, tool_result)
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from hotel_agent.cli import ask
from hotel_agent.graph.builder import build_graph
from hotel_agent.graph.context import AgentContext
from hotel_agent.tracing import AgentTracer

SEARCH_ARGS = {"checkin": "2026-11-19", "checkout": "2026-11-22", "city": "Казань"}


def with_usage(message: AIMessage, input_tokens: int, output_tokens: int, reasoning: int | None = None) -> AIMessage:
    """Ответ LLM с usage_metadata, как его заполняет интеграция провайдера; reasoning=None — провайдер
    не сообщил токены размышления."""
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
    if reasoning is not None:
        usage["output_token_details"] = {"reasoning": reasoning}
    return message.model_copy(update={"usage_metadata": usage})


def search_then_answer() -> ScriptedChatModel:
    """Агент ищет отели, потом отвечает: 2 вызова LLM, 1 tool."""
    return ScriptedChatModel(
        responses=[
            with_usage(tatarstan_call(**SEARCH_ARGS), 1000, 300, reasoning=250),
            with_usage(AIMessage("Нашёл Korston"), 1800, 120, reasoning=40),
        ]
    )


@pytest.fixture
def trace_file(tmp_path):
    return tmp_path / "trace.jsonl"


@pytest.fixture
def terminal():
    return io.StringIO()


@pytest.fixture
def turn(settings, trace_file, terminal):
    """Ход диалога через cli.ask с трассировкой в terminal и trace_file."""
    graph = build_graph(checkpointer=InMemorySaver())
    tracer = AgentTracer(out=terminal, jsonl_path=trace_file)
    config = {"configurable": {"thread_id": "t1"}}

    async def _turn(text, *, llm, ostrovok=None):
        context = AgentContext(llm=llm, settings=settings, ostrovok=ostrovok, today=lambda: TODAY)
        return await ask(graph, text, config=config, context=context, tracer=tracer)

    return _turn


def events(trace_file, kind: str) -> list[dict]:
    lines = trace_file.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == kind]


async def test_turn_totals_add_up_tokens_of_every_llm_call(turn, trace_file):
    answer = await turn("Казань, 19–22 ноября", llm=search_then_answer(), ostrovok=FakeOstrovok(KAZAN_RESULT))

    assert answer == "Нашёл Korston"
    [total] = events(trace_file, "turn")
    assert total["user"] == "Казань, 19–22 ноября"
    assert (total["llm_calls"], total["tool_calls"]) == (2, 1)
    assert (total["input_tokens"], total["output_tokens"], total["reasoning_tokens"]) == (2800, 420, 290)


async def test_each_llm_call_is_traced_with_its_tokens_and_requested_tools(turn, trace_file):
    await turn("Казань, 19–22 ноября", llm=search_then_answer(), ostrovok=FakeOstrovok(KAZAN_RESULT))

    first, second = events(trace_file, "llm")
    assert (first["input_tokens"], first["output_tokens"], first["reasoning_tokens"]) == (1000, 300, 250)
    assert first["tool_calls"] == ["search_hotels_in_tatarstan"]
    assert (second["input_tokens"], second["reasoning_tokens"], second["tool_calls"]) == (1800, 40, [])


# С клиентом tool отвечает Command с ToolMessage, без клиента — строкой.
@pytest.mark.parametrize("ostrovok", [FakeOstrovok(KAZAN_RESULT), None], ids=["command", "text"])
async def test_tool_call_is_traced_with_args_and_result_size(turn, trace_file, ostrovok):
    llm = ScriptedChatModel(responses=[tatarstan_call(**SEARCH_ARGS), AIMessage("Готово")])

    await turn("Казань, 19–22 ноября", llm=llm, ostrovok=ostrovok)

    [call] = events(trace_file, "tool")
    assert (call["name"], call["args"], call["status"]) == ("search_hotels_in_tatarstan", SEARCH_ARGS, "success")
    assert call["result_chars"] == len(tool_result(llm).content)


async def test_failed_tool_is_traced_as_error(turn, trace_file):
    llm = ScriptedChatModel(
        responses=[tatarstan_call(checkin="19 ноября", checkout="2026-11-22"), AIMessage("Уточните даты")]
    )

    await turn("Казань", llm=llm, ostrovok=FakeOstrovok(KAZAN_RESULT))

    [call] = events(trace_file, "tool")
    assert call["status"] == "error"
    assert "checkin" in call["error"]


async def test_reasoning_not_reported_by_provider_stays_unknown(turn, trace_file, terminal):
    llm = ScriptedChatModel(responses=[with_usage(AIMessage("На какие даты?"), 900, 20)])

    await turn("Хочу отель", llm=llm)

    [call] = events(trace_file, "llm")
    [total] = events(trace_file, "turn")
    assert call["reasoning_tokens"] is None and total["reasoning_tokens"] is None
    assert "размышл. н/д" in terminal.getvalue()


async def test_each_turn_counts_only_its_own_calls(turn, trace_file):
    llm = ScriptedChatModel(
        responses=[
            with_usage(AIMessage("На какие даты?"), 900, 20, reasoning=5),
            with_usage(AIMessage("Ищу"), 1000, 30, reasoning=7),
        ]
    )

    await turn("Хочу отель в Казани", llm=llm)
    await turn("19–22 ноября", llm=llm)

    _, second = events(trace_file, "turn")
    assert (second["llm_calls"], second["input_tokens"], second["reasoning_tokens"]) == (1, 1000, 7)


async def test_terminal_shows_tool_calls_and_reasoning_tokens(turn, terminal):
    await turn("Казань, 19–22 ноября", llm=search_then_answer(), ostrovok=FakeOstrovok(KAZAN_RESULT))

    out = terminal.getvalue()
    assert "[tool] search_hotels_in_tatarstan" in out
    assert '"city": "Казань"' in out
    assert "размышл. 250" in out  # первый вызов LLM
    assert "размышл. 290" in out  # итог хода


async def test_tracer_without_file_prints_only_to_terminal(settings, terminal):
    graph = build_graph(checkpointer=InMemorySaver())
    llm = ScriptedChatModel(responses=[with_usage(AIMessage("На какие даты?"), 900, 20, reasoning=5)])
    context = AgentContext(llm=llm, settings=settings, today=lambda: TODAY)

    await ask(
        graph,
        "Хочу отель",
        config={"configurable": {"thread_id": "t1"}},
        context=context,
        tracer=AgentTracer(out=terminal, jsonl_path=None),
    )

    assert "размышл. 5" in terminal.getvalue()
