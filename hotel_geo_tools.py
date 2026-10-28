"""Hotel, noise and event tools for the shared map-aware agent."""

import asyncio
import hashlib
import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock

from langchain_core.tools import tool

from hotel_agent.apis.kudago.client import EventsClient
from hotel_agent.apis.kudago.models import EventsError
from hotel_agent.apis.ostrovok.client import OstrovokClient
from hotel_agent.apis.ostrovok.models import OstrovokError, StaySearch
from hotel_agent.config import Settings
from hotel_agent.dates import today_in_moscow
from hotel_agent.tools.events import EventDetailsArgs, StayDates
from hotel_agent.tools.noise import NoiseArgs, assess_points
from kazan_noise.config import NoiseConfig
from kazan_noise.service import NoiseService
from kazan_noise.snapshot import SnapshotMissingError
from map_tools import collection, feature, map_result

NOISE_LIMIT = 50
_noise_lock = RLock()


@lru_cache(maxsize=1)
def _load_noise(path, features_mtime, grid_mtime):
    return NoiseService.load(Path(path))


def noise_service():
    cfg = NoiseConfig()
    try:
        with _noise_lock:
            return _load_noise(
                str(cfg.data_dir.resolve()), cfg.features_path.stat().st_mtime_ns, cfg.grid_path.stat().st_mtime_ns
            )
    except (OSError, ValueError, SnapshotMissingError):
        return None


def noise_payload(points):
    """Cache validated reports against both model files; never cache failures."""
    service = noise_service()
    if service is None:
        return {"available": False, "points": [], "note": "Модель шума пока не подготовлена; тишина не проверена."}
    cfg = NoiseConfig()
    identity = [(str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns) for p in (cfg.features_path, cfg.grid_path)]
    key = hashlib.sha256(json.dumps([identity, [p.model_dump() for p in points]], sort_keys=True).encode()).hexdigest()
    folder = Path(os.getenv("CACHE_DIR", ".cache")) / "noise-reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + ".json")
    with _noise_lock:
        if path.exists():
            return json.loads(path.read_text())
        point_files = [
            folder
            / (
                "point-"
                + hashlib.sha256(json.dumps([identity, p.model_dump()], sort_keys=True).encode()).hexdigest()
                + ".json"
            )
            for p in points
        ]
        missing = [p for p, path in zip(points, point_files) if not path.exists()]
        if missing:
            fresh = json.loads(assess_points(service, missing))
            for p, report in zip(missing, fresh["points"], strict=True):
                point_path = folder / (
                    "point-"
                    + hashlib.sha256(json.dumps([identity, p.model_dump()], sort_keys=True).encode()).hexdigest()
                    + ".json"
                )
                value = {**{k: v for k, v in fresh.items() if k != "points"}, "point": report}
                temporary = point_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False))
                temporary.replace(point_path)
        values = [json.loads(p.read_text()) for p in point_files]
        result = {
            "available": True,
            "points": [v["point"] for v in values],
            **({k: v for k, v in values[0].items() if k != "point"} if values else {}),
        }
        # Serialized under lock; the rename keeps interrupted writes out of readers.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        temporary.replace(path)
        return result


def select_quiet_hotels(hotels, assessment, quiet=True):
    """Kazan requires verified day AND night <=50; ties retain original ranking."""
    reports = assessment.get("points", [])
    by_point = {(p["lat"], p["lon"]): p for p in reports}
    accepted, unknown, rejected = [], 0, 0
    for hotel in hotels:
        data = hotel.model_dump(mode="json", exclude_none=True)
        if (hotel.city or "").casefold() != "казань":
            # Do not extrapolate the Kazan scale to other towns.
            if not hotel.city:
                unknown += 1
                continue
            data["noise_status"] = "outside_kazan_coverage"
            accepted.append((101, data))
            continue
        report = by_point.get((hotel.lat, hotel.lon))
        if not report or "error" in report:
            unknown += 1
            continue
        score = max(report["day"]["score"], report["night"]["score"])
        if quiet and score > NOISE_LIMIT:
            rejected += 1
            continue
        data["noise"] = report
        data["noise_score"] = score
        accepted.append((score, data))
    accepted.sort(key=lambda item: item[0])
    return [data for _, data in accepted], {"rejected_noise": rejected, "unverified_noise": unknown}


async def find_hotels(request, client=None):
    print("request", request)
    client = client or OstrovokClient.from_settings(Settings())
    expanded = request.model_copy(update={"limit": 30})
    result = await client.search_hotels(expanded, today=today_in_moscow())
    from hotel_agent.tools.noise import NoisePoint

    async def assess(result):
        points = [NoisePoint(lat=h.lat, lon=h.lon) for h in result.hotels if (h.city or "").casefold() == "казань"]
        assessment = await asyncio.to_thread(noise_payload, points) if points else {"points": []}
        selected, counts = select_quiet_hotels(result.hotels, assessment, params.quiet)
        return selected, counts, assessment

    hotels, counts, assessment = await assess(result)
    broadened = False
    # One bounded fallback; never silently increase budget, dates or change the city.
    if not hotels and result.hotels and assessment.get("available") and request.max_distance_km is not None:
        expanded = expanded.model_copy(update={"max_distance_km": max(3.0, request.max_distance_km * 2)})
        result = await client.search_hotels(expanded, today=today_in_moscow())
        hotels, counts, assessment = await assess(result)
        broadened = True
    payload = result.model_dump(mode="json", exclude={"hotels"})
    payload.update(
        hotels=hotels[: request.limit],
        returned=len(hotels[: request.limit]),
        matched_in_checked_batch=len(hotels),
        checked=len(result.hotels),
        noise_limit=NOISE_LIMIT,
        noise_rule="max(day.score, night.score) <= 50 for Kazan",
        noise_data=assessment.get("data"),
        noise_note=assessment.get("note"),
        geographic_filter_broadened=broadened,
        effective_max_distance_km=expanded.max_distance_km,
        **counts,
    )
    payload["note"] = (
        "Цены и наличие по снимку ostrovok.ru, не подтверждение бронирования. "
        "Проверено до 30 кандидатов; found — число до фильтра шума. "
        "Шум — относительная модель OSM, не децибелы и не замер в номере. "
        "Для Казани кандидаты без проверки шума не рекомендуются; вне Казани шум не оценён."
    )
    payload["source"] = (
        "Снимок ostrovok.ru от " + result.data_as_of.isoformat()
        if result.data_as_of
        else "Снимок ostrovok.ru, дата неизвестна"
    )
    payload["retrieved_at"] = datetime.now().astimezone().isoformat()
    features = [
        feature(
            {"type": "Point", "coordinates": [h["lon"], h["lat"]]},
            name=h["name"],
            hotel_id=h["hotel_id"],
            price_per_night=h["price_per_night"],
            noise_score=h.get("noise_score"),
            source=h.get("url"),
        )
        for h in payload["hotels"]
    ]
    return map_result(payload, {"default": collection(features)})


@tool(args_schema=StaySearch, response_format="content_and_artifact")
def search_hotels_in_tatarstan(**params):
    """Подбирает отели по датам и гостям из снимка ostrovok.ru. В Казани автоматически
    оставляет только проверенные отели с дневным И ночным шумом не выше 50/100,
    сортирует по тишине. Возвращает data_ref для карты. В других городах шум не оценён.
    """
    try:
        return asyncio.run(find_hotels(StaySearch(**params)))
    except OstrovokError as exc:
        note = str(exc) if exc.__cause__ is None else "Поиск отелей временно недоступен."
        return map_result({"hotels": [], "available": False, "note": note}, {"default": collection([])})


@tool(args_schema=NoiseArgs)
def noise_in_kazan(**params):
    """Оценивает относительный шум OSM в точках Казани: день и ночь, шкала 0–100,
    не децибелы. Меньше — тише. Рекомендательный порог: оба значения <=50.
    """
    return noise_payload(NoiseArgs(**params).points)


@tool(args_schema=StayDates)
def events_in_kazan(**params):
    """События Казани из снимка KudaGo на даты поездки. Сохраняй ссылки url и дату данных."""
    args = StayDates(**params)
    try:
        return asyncio.run(
            EventsClient.from_settings(Settings()).events_for_stay(args.checkin, args.checkout)
        ).model_dump(mode="json")
    except EventsError:
        return {
            "available": False,
            "events": [],
            "exhibitions": [],
            "note": "События Казани недоступны в сохранённых данных; это не означает, что событий нет.",
        }


@tool(args_schema=EventDetailsArgs)
def kazan_event_details(**params):
    """Подробности события KudaGo по id из events_in_kazan; даты поездки необязательны."""
    args = EventDetailsArgs(**params)
    try:
        return asyncio.run(
            EventsClient.from_settings(Settings()).event_details(
                args.event_id,
                checkin=args.checkin if args.checkout else None,
                checkout=args.checkout if args.checkin else None,
                today=today_in_moscow(),
            )
        ).model_dump(mode="json")
    except EventsError:
        return {"available": False, "note": "Сведения об этом событии недоступны в сохранённых данных."}


HOTEL_TOOLS = [search_hotels_in_tatarstan, noise_in_kazan, events_in_kazan, kazan_event_details]
