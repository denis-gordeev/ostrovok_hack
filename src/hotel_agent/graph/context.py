from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from langchain_core.language_models import BaseChatModel

from hotel_agent.apis.kudago.client import EventsClient
from hotel_agent.apis.ostrovok.client import OstrovokClient
from hotel_agent.config import Settings
from hotel_agent.dates import today_in_moscow
from kazan_noise.service import NoiseService


@dataclass
class AgentContext:
    """Зависимости узлов и tools. Передаётся в graph.ainvoke(..., context=...)."""

    # LLM агента; tools к ней привязывает узел agent.
    llm: BaseChatModel
    settings: Settings
    # Поиск по Татарстану (снимок островка в Postgres); None — tool отвечает, что поиск недоступен.
    ostrovok: OstrovokClient | None = None
    # Модель шума Казани (снимок OSM); None — tool отвечает, что оценка шума недоступна.
    noise: NoiseService | None = None
    # События Казани (схема kudago в Postgres); None — tools событий отвечают, что они недоступны.
    events: EventsClient | None = None
    today: Callable[[], date] = today_in_moscow
