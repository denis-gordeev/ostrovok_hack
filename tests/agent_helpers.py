from datetime import date

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from hotel_agent.apis.kudago.models import EventsError
from hotel_agent.apis.ostrovok.models import (HotelCandidate, OstrovokError,
                                              SearchResult, StaySearch)

TODAY = date(2026, 10, 24)


class ScriptedChatModel(BaseChatModel):
    """Вместо LLM агента: по очереди отдаёт заготовленные ответы и запоминает входы."""

    responses: list[AIMessage]
    received: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [t.name for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responses[len(self.received) - 1])])


def tool_result(llm: ScriptedChatModel, call: int = 1) -> ToolMessage:
    """ToolMessage, которое агент получил на вход в call-м вызове LLM."""
    message = llm.received[call][-1]
    assert isinstance(message, ToolMessage)
    return message


class FakeOstrovok:
    """Вместо OstrovokClient: отдаёт заданный результат или ошибку и запоминает запросы."""

    def __init__(self, result: SearchResult | None = None, error: str | None = None):
        self.result, self.error = result, error
        self.requests: list[tuple[StaySearch, date]] = []

    async def search_hotels(self, request: StaySearch, *, today: date) -> SearchResult:
        self.requests.append((request, today))
        if self.error:
            raise OstrovokError(self.error)
        return self.result


def tatarstan_call(**args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": "search_hotels_in_tatarstan", "args": args, "id": "call_t"}])


KAZAN_RESULT = SearchResult(
    checkin=date(2026, 11, 19),
    checkout=date(2026, 11, 22),
    nights=3,
    found=1,
    hotels=[
        HotelCandidate(
            hotel_id="korston_royal_hotel",
            name="Отель Korston Royal",
            lat=55.78,
            lon=49.15,
            free_child_age=5,
            price_total=39450,
            price_per_night=13150,
            price_source="rates",
        )
    ],
)


class FakeEvents:
    """Вместо EventsClient: отдаёт заданные ответы или ошибку и запоминает запросы."""

    def __init__(self, stay=None, details=None, error: str | None = None):
        self.stay, self.details, self.error = stay, details, error
        self.stay_requests: list[tuple[date, date]] = []
        self.detail_requests: list[dict] = []

    async def events_for_stay(self, checkin: date, checkout: date):
        self.stay_requests.append((checkin, checkout))
        if self.error:
            raise EventsError(self.error)
        return self.stay

    async def event_details(self, event_id: int, *, checkin=None, checkout=None, today: date):
        self.detail_requests.append({"event_id": event_id, "checkin": checkin, "checkout": checkout, "today": today})
        if self.error:
            raise EventsError(self.error)
        return self.details
