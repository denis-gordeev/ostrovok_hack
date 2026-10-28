from collections.abc import Callable, Sequence
from datetime import date

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage,
                                     SystemMessage)
from pydantic import BaseModel

from hotel_agent.dates import today_in_moscow
from hotel_agent.intents.prompt import INTENT_SYSTEM_PROMPT
from hotel_agent.intents.schema import HotelSearchIntent


class IntentExtractor:
    """Извлекает интенты пользователя из истории диалога через structured output LLM."""

    def __init__(
        self,
        llm: BaseChatModel,
        *,
        system_prompt: str = INTENT_SYSTEM_PROMPT,
        schema: type[BaseModel] = HotelSearchIntent,
        today: Callable[[], date] = today_in_moscow,
        # json_schema работает у Anthropic и OpenAI; forced tool calling claude-opus-5-5 не поддерживает.
        structured_output_method: str = "json_schema",
    ):
        self._structured_llm = llm.with_structured_output(schema, method=structured_output_method)
        self._system_prompt = system_prompt
        self._today = today

    async def extract(self, messages: Sequence[BaseMessage]) -> BaseModel:
        # Извлекаем из всей истории: так данные из разных ходов диалога складываются.
        prompt = self._system_prompt.replace("{today}", self._today().isoformat())
        dialogue = [m for m in messages if _is_dialogue(m)]
        return await self._structured_llm.ainvoke([SystemMessage(prompt), *dialogue])


def _is_dialogue(message: BaseMessage) -> bool:
    """Реплики, которые видел пользователь: без вызовов tools и их результатов."""
    if isinstance(message, AIMessage):
        return not message.tool_calls
    return isinstance(message, HumanMessage)
