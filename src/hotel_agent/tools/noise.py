import asyncio
import json

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from hotel_agent.graph.context import AgentContext
from hotel_agent.graph.state import AgentState
from kazan_noise.service import NoiseReport, NoiseService, OutsideKazanError

NOISE_UNAVAILABLE = "Оценка шума недоступна: данные не подготовлены (uv run kazan-noise fetch, build, grid)."
SCALE_NOTE = (
    "0–100: доля застроенных точек Казани, где тише (100 — громче всех). "
    "Относительная оценка по модели OSM, не децибелы."
)
# Столько же, сколько отелей максимум отдаёт поиск; 30 точек (почти все на зданиях) — ~6,5 с на 10 ядрах.
MAX_POINTS = 30


class NoisePoint(BaseModel):
    lat: float = Field(description="Широта точки в Казани (WGS84), например 55.7963")
    lon: float = Field(description="Долгота точки в Казани (WGS84), например 49.1088")


class NoiseArgs(BaseModel):
    """Аргументы tool шума. Эту схему видит LLM агента, описания полей — подсказки для неё."""

    points: list[NoisePoint] = Field(
        min_length=1,
        max_length=MAX_POINTS,
        description=f"Точки для оценки, до {MAX_POINTS} за вызов: например, все отели из поиска сразу",
    )


@tool(args_schema=NoiseArgs)
async def noise_in_kazan(runtime: ToolRuntime[AgentContext, AgentState], **params) -> str:
    """Оценивает относительный уровень шума в точках Казани по данным OpenStreetMap.

    Принимает список точек и в points возвращает оценку для каждой в том же порядке, с её lat и lon.
    Баллы 0–100 для дня (07–23) и ночи (23–07): доля застроенных точек города, где тише
    (100 — громче всех). Это не децибелы, а сравнение с остальным городом. Для точки на здании
    (inside_building) day и night даны по самому шумному фасаду (днём и ночью он может быть разным),
    а в quiet_side — баллы самой тихой стороны, обычно окон во двор. В main_sources — главные
    источники шума по доле энергии: дороги, рельсы, бары, промзоны, аэропорт и т.п.
    Точка вне Казани получает error вместо оценки, остальные точки оцениваются как обычно.
    """
    args = NoiseArgs(**params)
    noise = runtime.context.noise
    if noise is None:
        return NOISE_UNAVAILABLE
    return await asyncio.to_thread(assess_points, noise, args.points)


def assess_points(noise: NoiseService, points: list[NoisePoint]) -> str:
    """Оценки точек → JSON для агента: шкала и атрибуция OSM один раз на весь ответ.

    Точки считаются одним пакетом (noise_many): фасады всех точек — параллельно.
    """
    results = []
    osm_timestamp = None
    reports = noise.noise_many([(point.lat, point.lon) for point in points])
    for point, report in zip(points, reports, strict=True):
        if isinstance(report, OutsideKazanError):
            results.append({"lat": point.lat, "lon": point.lon, "error": f"Оценка шума не выполнена: {report}"})
            continue
        results.append(point_for_llm(report))
        osm_timestamp = report.osm_timestamp
    payload = {"points": results, "scale": SCALE_NOTE}
    if osm_timestamp is not None:
        payload["data"] = f"OpenStreetMap на {osm_timestamp[:10]}, © OpenStreetMap contributors"
    return json.dumps(payload, ensure_ascii=False)


def point_for_llm(report: NoiseReport) -> dict:
    """Отчёт модели по точке → данные для агента, без внутренних уровней модели."""
    payload = {
        "lat": report.lat,
        "lon": report.lon,
        "day": report.day.model_dump(),
        "night": report.night.model_dump(),
        "inside_building": report.inside_building,
    }
    if report.quiet_side is not None:
        payload["quiet_side"] = report.quiet_side.model_dump()
    payload["main_sources"] = [s.model_dump(exclude_none=True) for s in report.main_sources]
    return payload
