import asyncio
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from hotel_agent.apis.kudago.client import EventsClient
from hotel_agent.apis.ostrovok.client import OstrovokClient
from hotel_agent.config import Settings
from hotel_agent.graph.builder import build_graph
from hotel_agent.graph.context import AgentContext
from hotel_agent.tracing import AgentTracer
from kazan_noise.config import NoiseConfig
from kazan_noise.service import NoiseService
from kazan_noise.snapshot import SnapshotMissingError
from kudago_events.config import KudagoConfig
from kudago_events.load import refresh_if_stale


def _load_noise() -> NoiseService | None:
    """Загружает модель шума Казани; без подготовленных данных или при любой ошибке загрузки
    оценка шума отключается, а агент (поиск отелей) всё равно запускается."""
    cfg = NoiseConfig()
    try:
        return NoiseService.load(cfg.data_dir, receiver_height=cfg.receiver_height_m)
    except SnapshotMissingError as exc:
        print(f"Оценка шума отключена: {exc}")
    except Exception as exc:
        print(f"Оценка шума отключена: {type(exc).__name__}: {exc}")
    return None


def _refresh_events(cfg: KudagoConfig) -> None:
    result = refresh_if_stale(cfg)
    if result.status == "loaded":
        print(f"События KudaGo: загружено {result.events}.", file=sys.stderr)
    elif result.status == "failed":
        print(f"События KudaGo не обновлены: {result.error}", file=sys.stderr)


def _start_events_refresh() -> threading.Thread:
    """Загружает события Казани в фоне, если удачной загрузке больше суток; чат её не ждёт.

    Поток, а не asyncio-задача: чат ждёт ввод в блокирующем input(), и задача в цикле событий стояла бы.
    """
    thread = threading.Thread(target=_refresh_events, args=(KudagoConfig(),), name="kudago-refresh", daemon=True)
    thread.start()
    return thread


def _make_tracer(settings: Settings) -> AgentTracer | None:
    """Трасса вызовов LLM и tools: в терминал (stderr) и, если задана папка, в JSONL-файл на запуск."""
    if not settings.agent_trace:
        return None
    jsonl_path = None
    if settings.agent_trace_dir:
        jsonl_path = Path(settings.agent_trace_dir) / f"{datetime.now():%Y-%m-%dT%H-%M-%S}.jsonl"
        print(f"Трасса пишется в {jsonl_path}")
    return AgentTracer(jsonl_path=jsonl_path)


async def ask(graph, text: str, *, config: dict, context: AgentContext, tracer: AgentTracer | None = None) -> str:
    """Один ход диалога → ответ агента. С tracer вызовы LLM и tools за ход попадают в трассу."""
    if tracer is None:
        state = await graph.ainvoke({"messages": [HumanMessage(text)]}, config=config, context=context)
        return state["messages"][-1].text
    tracer.start_turn(text)
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(text)]}, config={**config, "callbacks": [tracer]}, context=context
        )
    finally:
        # И при ошибке LLM или tool: иначе вызовы этого хода попали бы в итог следующего.
        tracer.end_turn()
    return state["messages"][-1].text


async def chat(settings: Settings) -> None:
    graph = build_graph(checkpointer=InMemorySaver())
    llm = init_chat_model(settings.llm_model)
    # Один thread_id на запуск: история диалога живёт в checkpointer.
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    print("Агент подбора отелей в Татарстане. Пустая строка или Ctrl+C — выход.")
    _start_events_refresh()
    context = AgentContext(
        llm=llm,
        settings=settings,
        ostrovok=OstrovokClient.from_settings(settings),
        noise=_load_noise(),
        events=EventsClient.from_settings(settings),
    )
    tracer = _make_tracer(settings)
    while text := input("> ").strip():
        print(await ask(graph, text, config=config, context=context, tracer=tracer))


def main() -> None:
    # Ключи провайдера LLM (ANTHROPIC_API_KEY и т.п.) langchain читает из окружения.
    load_dotenv()
    try:
        settings = Settings()
    except ValidationError as exc:
        sys.exit(f"Заполните .env по образцу .env.example:\n{exc}")
    try:
        asyncio.run(chat(settings))
    except (KeyboardInterrupt, EOFError):
        pass
