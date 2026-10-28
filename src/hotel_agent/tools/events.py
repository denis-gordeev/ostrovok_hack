from datetime import date

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from hotel_agent.apis.kudago.models import EventsError
from hotel_agent.graph.context import AgentContext
from hotel_agent.graph.state import AgentState

EVENTS_UNAVAILABLE = "События Казани недоступны."


class StayDates(BaseModel):
    """Аргументы tool событий. Эту схему видит LLM агента, описания полей — подсказки для неё."""

    checkin: date = Field(description="Дата заезда, YYYY-MM-DD — та же, что в поиске отелей")
    checkout: date = Field(description="Дата выезда, YYYY-MM-DD — та же, что в поиске отелей")


class EventDetailsArgs(BaseModel):
    event_id: int = Field(description="id события из ответа events_in_kazan")
    checkin: date | None = Field(default=None, description="Дата заезда, если известна: покажет сеансы в дни поездки")
    checkout: date | None = Field(default=None, description="Дата выезда, если известна")


@tool(args_schema=StayDates)
async def events_in_kazan(runtime: ToolRuntime[AgentContext, AgentState], **params) -> str:
    """Подбирает события в Казани на даты поездки по афише KudaGo. Только Казань.

    Возвращает в events до 5 разовых событий в дни поездки (концерты, спектакли, фестивали и т. п.) с сеансами
    по Москве и в exhibitions до 2 самых популярных выставок, которые идут в это время (until — последний день).
    У каждого события есть id (для kazan_event_details) и url — ссылка на KudaGo, её нужно показывать пользователю.
    """
    args = StayDates(**params)
    if args.checkout < args.checkin:
        return "Дата выезда раньше даты заезда."
    events = runtime.context.events
    if events is None:
        return EVENTS_UNAVAILABLE
    try:
        result = await events.events_for_stay(args.checkin, args.checkout)
    except EventsError as exc:
        return str(exc)
    return result.model_dump_json(exclude_none=True)


@tool(args_schema=EventDetailsArgs)
async def kazan_event_details(runtime: ToolRuntime[AgentContext, AgentState], **params) -> str:
    """Подробности о событии Казани по его id из events_in_kazan.

    Описание, сеансы (в дни поездки, если даны даты, иначе ближайшие), место с адресом, цена, возраст и url —
    ссылка на KudaGo, её нужно показывать пользователю.
    """
    args = EventDetailsArgs(**params)
    ctx = runtime.context
    if ctx.events is None:
        return EVENTS_UNAVAILABLE
    # Сеансы в дни поездки — только когда известны обе даты.
    checkin, checkout = (args.checkin, args.checkout) if args.checkin and args.checkout else (None, None)
    try:
        result = await ctx.events.event_details(args.event_id, checkin=checkin, checkout=checkout, today=ctx.today())
    except EventsError as exc:
        return str(exc)
    return result.model_dump_json(exclude_none=True)
