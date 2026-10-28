import logging

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from hotel_agent.apis.ostrovok.models import OstrovokError, SearchResult, StaySearch
from hotel_agent.graph.context import AgentContext
from hotel_agent.graph.state import AgentState

logger = logging.getLogger(__name__)


@tool(args_schema=StaySearch)
async def search_hotels_in_tatarstan(runtime: ToolRuntime[AgentContext, AgentState], **params) -> Command | str:
    """Подбирает отели в Татарстане (Казань, Набережные Челны и др.), свободные на указанные даты.

    Учитывает состав гостей, число номеров, бюджет и пожелания. Возвращает кандидатов с ценой в рублях
    за весь период и самым дешёвым подходящим тарифом каждого номера. Цены — по снимку ostrovok.ru.
    """
    request = StaySearch(**params)
    ctx = runtime.context
    if ctx.ostrovok is None:
        return "Поиск по Татарстану сейчас недоступен."
    try:
        result = await ctx.ostrovok.search_hotels(request, today=ctx.today())
    except OstrovokError as exc:
        logger.info("Tatarstan search rejected: %s", exc)
        return f"Поиск не выполнен: {exc}"

    return Command(
        update={
            "hotel_candidates": result.hotels,
            "messages": [ToolMessage(describe_for_llm(result), tool_call_id=runtime.tool_call_id)],
        }
    )


def describe_for_llm(result: SearchResult) -> str:
    return result.model_dump_json(exclude_none=True)
