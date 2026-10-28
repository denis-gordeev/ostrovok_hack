from datetime import date

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from hotel_agent.intents.extractor import IntentExtractor


class FakeIntent(BaseModel):
    city_mood: str


class FakeStructuredLLM:
    """Имитирует chat-модель: запоминает схему и сообщения, возвращает готовый интент."""

    def __init__(self, result: BaseModel):
        self.result = result
        self.schema = None
        self.method = None
        self.received: list = []

    def with_structured_output(self, schema, *, method=None):
        self.schema = schema
        self.method = method
        return self

    async def ainvoke(self, messages):
        self.received = messages
        return self.result


async def test_extract_returns_structured_output_for_the_given_schema():
    llm = FakeStructuredLLM(FakeIntent(city_mood="quiet"))
    extractor = IntentExtractor(llm, system_prompt="prompt", schema=FakeIntent)

    intent = await extractor.extract([HumanMessage("тихий отель")])

    assert intent == FakeIntent(city_mood="quiet")
    assert llm.schema is FakeIntent


async def test_structured_output_uses_json_schema_by_default():
    # Forced tool calling (дефолт langchain-anthropic) не поддерживается на claude-opus-5-5.
    llm = FakeStructuredLLM(FakeIntent(city_mood="quiet"))
    IntentExtractor(llm, schema=FakeIntent)
    assert llm.method == "json_schema"


async def test_structured_output_method_can_be_overridden():
    llm = FakeStructuredLLM(FakeIntent(city_mood="quiet"))
    IntentExtractor(llm, schema=FakeIntent, structured_output_method="function_calling")
    assert llm.method == "function_calling"


async def test_extract_prepends_system_prompt_with_today_and_keeps_full_history():
    llm = FakeStructuredLLM(FakeIntent(city_mood="quiet"))
    extractor = IntentExtractor(
        llm,
        system_prompt='Today is {today}. Output JSON like {"checkin": "..."}',
        schema=FakeIntent,
        today=lambda: date(2026, 10, 24),
    )
    history = [HumanMessage("хочу отель"), AIMessage("Укажите даты"), HumanMessage("10-14 декабря")]

    await extractor.extract(history)

    system, *rest = llm.received
    assert isinstance(system, SystemMessage)
    assert system.content == 'Today is 2026-10-24. Output JSON like {"checkin": "..."}'
    assert rest == history


async def test_extract_skips_tool_calls_and_tool_results_from_history():
    # Интенты берутся из того, что видел пользователь; служебные сообщения tools — шум.
    llm = FakeStructuredLLM(FakeIntent(city_mood="quiet"))
    extractor = IntentExtractor(llm, system_prompt="prompt", schema=FakeIntent)
    asked = HumanMessage("отель на 10-14 декабря")
    tool_call = AIMessage("", tool_calls=[{"name": "search_hotels_in_tatarstan", "args": {}, "id": "c1"}])
    tool_result = ToolMessage("[...]", tool_call_id="c1")
    answer = AIMessage("Вот отели: ...")
    follow_up = HumanMessage("а подешевле?")

    await extractor.extract([asked, tool_call, tool_result, answer, follow_up])

    assert llm.received[1:] == [asked, answer, follow_up]
