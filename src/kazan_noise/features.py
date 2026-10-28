"""Классификация объектов OSM: препятствия, зелень и источники шума с мощностью день/ночь."""

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from kazan_noise import weights as w
from kazan_noise.osm import element_geometry, project

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_HEIGHT_RE = re.compile(rf"^\s*{_NUMBER}\s*(?:m|м)?\s*$", re.IGNORECASE)
_SPEED_RE = re.compile(rf"^\s*{_NUMBER}\s*(mph|km/h|kmh)?\s*$", re.IGNORECASE)
_MPH_TO_KMH = 1.609
_NAMED_SPEEDS = {"RU:urban": 60.0, "RU:rural": 90.0, "RU:motorway": 110.0}
_GREEN_TAGS = (("landuse", "forest"), ("natural", "wood"), ("leisure", "park"))
_MINOR_SERVICES = {"siding", "yard", "spur"}
_POINT_KINDS = {"bar": "bar", "pub": "bar", "nightclub": "bar", "place_of_worship": "worship"}


@dataclass(frozen=True)
class Source:
    """Источник шума. w_*: дБ/м у линий, дБ/м² у площадей, дБ у точек; geom — в UTM."""

    kind: str  # road|tram|rail|aircraft|construction|industrial|bar|worship
    osm_id: str  # "way/123"
    name: str | None
    subtype: str  # класс highway, тип railway, amenity, runway|corridor
    geom: BaseGeometry
    height: float
    w_day: float
    w_night: float
    radius: float


@dataclass(frozen=True)
class Obstacles:
    """Экраны: здания и стены (геометрии в UTM), высоты в метрах."""

    geoms: np.ndarray
    heights: np.ndarray
    is_building: np.ndarray


@dataclass(frozen=True)
class Green:
    """Зелёные полигоны (UTM) и фактор затухания листвой."""

    geoms: np.ndarray
    factors: np.ndarray


@dataclass(frozen=True)
class Features:
    """Все слои модели; boundary — в UTM, boundary_wgs84 — в (lon, lat)."""

    boundary: BaseGeometry
    boundary_wgs84: BaseGeometry
    obstacles: Obstacles
    green: Green
    sources: list[Source]
    osm_timestamp: str


@dataclass
class BuildReport:
    """Счётчики принятых объектов и причин пропуска ("<слой>:<причина>")."""

    counts: Counter[str]
    skipped: Counter[str]


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _explicit_height(tags: dict) -> float | None:
    """Тег height в метрах ("12", "12 m", "12,5") или None."""
    m = _HEIGHT_RE.match(str(tags.get("height", "")))
    return _number(m.group(1)) if m else None


def parse_height(tags: dict) -> float | None:
    """Высота по тегам: height, иначе этажи × LEVEL_M + ROOF_M, иначе None."""
    height = _explicit_height(tags)
    if height is not None:
        return height
    for key in ("building:levels", "levels"):
        m = _HEIGHT_RE.match(str(tags.get(key, "")))
        if m and _number(m.group(1)) > 0:
            return _number(m.group(1)) * w.LEVEL_M + w.ROOF_M
    return None


def building_height(tags: dict, area_m2: float) -> float | None:
    """Высота здания-экрана; None — здание шум не экранирует."""
    kind = tags.get("building")
    if kind in w.NON_SHIELD:
        return None
    if kind == "construction":
        return w.CONSTRUCTION_HEIGHT_M
    explicit = parse_height(tags)
    if explicit is not None:
        return explicit
    if kind in w.TYPE_HEIGHTS:
        return w.TYPE_HEIGHTS[kind]
    for limit, height in w.AREA_HEIGHTS:
        if area_m2 < limit:
            return height
    return w.AREA_HEIGHT_MAX


def parse_maxspeed(value: str | None) -> float | None:
    """maxspeed в км/ч: число, "N mph", RU:*; остальное (walk, мусор) — None."""
    if value is None:
        return None
    if value in _NAMED_SPEEDS:
        return _NAMED_SPEEDS[value]
    m = _SPEED_RE.match(value)
    if not m:
        return None
    speed = _number(m.group(1)) * (_MPH_TO_KMH if (m.group(2) or "").lower() == "mph" else 1.0)
    return speed if speed > 0 else None


def _is_underground(tags: dict) -> bool:
    """Тоннель, covered=yes или layer < 0."""
    if tags.get("tunnel") not in (None, "no") or tags.get("covered") == "yes":
        return True
    try:
        return float(tags.get("layer", 0)) < 0
    except ValueError:
        return False


def _bridge_extra(tags: dict) -> float:
    return w.BRIDGE_EXTRA_M if tags.get("bridge") not in (None, "no") else 0.0


def _db(flow: float, *terms: float) -> float:
    """10·lg(flow) + terms; нулевой поток выключает источник."""
    return w.OFF if flow <= 0 else 10 * math.log10(flow) + sum(terms)


def _road_class(highway: str | None) -> tuple[w.RoadClass, bool] | None:
    """Параметры класса и признак съезда; None для неизвестного класса."""
    is_link = bool(highway) and highway.endswith("_link")
    cls = w.ROADS.get(highway[: -len("_link")] if is_link else highway)
    return (cls, is_link) if cls else None


def road_source(tags: dict, geom: BaseGeometry, osm_id: str) -> Source | None:
    """Линейный источник дороги; None для неизвестного класса и подземных участков."""
    highway = tags.get("highway")
    found = _road_class(highway)
    if found is None or _is_underground(tags):
        return None
    cls, is_link = found
    try:
        lanes = float(tags.get("lanes", cls.lanes))
    except ValueError:
        lanes = cls.lanes
    if not math.isfinite(lanes) or lanes <= 0:
        lanes = cls.lanes
    factor = lanes / cls.lanes * (w.LINK_FLOW_FACTOR if is_link else 1.0)
    speed = max(parse_maxspeed(tags.get("maxspeed")) or cls.speed, w.MIN_SPEED)
    const = 20 * math.log10(speed) + w.C_ROAD + cls.heavy_db
    return Source(
        kind="road",
        osm_id=osm_id,
        name=tags.get("name"),
        subtype=highway,
        geom=geom,
        height=w.ROAD_HEIGHT_M + _bridge_extra(tags),
        w_day=_db(cls.q_day * factor, const),
        w_night=_db(cls.q_night * factor, const),
        radius=cls.radius,
    )


def rail_source(tags: dict, geom: BaseGeometry, osm_id: str) -> Source | None:
    """Линейный источник трамвая/ЖД/метро на поверхности; None для неизвестного и подземного."""
    railway = tags.get("railway")
    if railway not in w.RAILS or _is_underground(tags):
        return None
    q_day, q_night, default_speed = w.RAILS[railway]
    speed = max(parse_maxspeed(tags.get("maxspeed")) or default_speed, w.MIN_SPEED)
    minor = tags.get("service") in _MINOR_SERVICES or tags.get("usage") == "industrial"
    const = 20 * math.log10(speed) + (w.C_RAIL if railway == "rail" else w.C_TRAM) + (w.RAIL_MINOR_DB if minor else 0.0)
    return Source(
        kind="rail" if railway == "rail" else "tram",
        osm_id=osm_id,
        name=tags.get("name") or tags.get("ref"),
        subtype=railway,
        geom=geom,
        height=w.RAIL_HEIGHT_M + _bridge_extra(tags),
        w_day=_db(q_day, const),
        w_night=_db(q_night, const),
        radius=w.RAIL_RADIUS,
    )


def runway_sources(runway: LineString, aerodrome_tags: dict, osm_id: str) -> list[Source]:
    """ВПП и два коридора захода (от торцов наружу) с интенсивностью по ICAO аэродрома."""
    coords = list(runway.coords)
    (x0, y0), (x1, y1) = coords[0][:2], coords[-1][:2]
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return []
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    q_day, q_night = w.AERODROMES.get(aerodrome_tags.get("icao"), w.AERODROME_DEFAULT)
    parts = [
        ("runway", runway),
        ("corridor", LineString([(x0, y0), (x0 - ux * w.CORRIDOR_M, y0 - uy * w.CORRIDOR_M)])),
        ("corridor", LineString([(x1, y1), (x1 + ux * w.CORRIDOR_M, y1 + uy * w.CORRIDOR_M)])),
    ]
    return [
        Source(
            kind="aircraft",
            osm_id=osm_id,
            name=aerodrome_tags.get("name"),
            subtype=subtype,
            geom=geom,
            height=0.0,
            w_day=_db(q_day, w.C_AIR),
            w_night=_db(q_night, w.C_AIR),
            radius=w.AIRCRAFT_RADIUS,
        )
        for subtype, geom in parts
    ]


def _osm_id(el: dict) -> str:
    return f"{el['type']}/{el['id']}"


def _project_one(geom: BaseGeometry) -> BaseGeometry:
    return project([geom])[0]


def _is_stale(el: dict, today: date) -> bool:
    """Правка старше STALE_CONSTRUCTION_DAYS; без timestamp объект не считается устаревшим."""
    stamp = el.get("timestamp")
    if not stamp:
        return False
    edited = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
    return (today - edited).days > w.STALE_CONSTRUCTION_DAYS


def _wall_height(tags: dict) -> float | None:
    """Высота стены (height или по умолчанию); None — это не стена."""
    kind = "city_wall" if tags.get("historic") == "citywalls" else tags.get("barrier")
    if kind not in w.WALLS:
        return None
    explicit = _explicit_height(tags)
    return explicit if explicit is not None else w.WALLS[kind]


def _boundary(raw: dict) -> tuple[BaseGeometry, BaseGeometry]:
    for el in raw["boundary"]["elements"]:
        if el.get("type") == "relation":
            geom = element_geometry(el, area=True)
            if geom is not None:
                return _project_one(geom), geom
    raise ValueError("в ответе boundary нет relation с корректной геометрией")


def _obstacles(raw: dict, report: BuildReport) -> Obstacles:
    wgs, tags_list, is_building = [], [], []
    for el in raw["buildings"]["elements"]:
        tags = el.get("tags") or {}
        building = "building" in tags
        if not building and _wall_height(tags) is None:
            continue
        geom = element_geometry(el, area=building)
        if geom is None:
            report.skipped["geometry"] += 1
            continue
        wgs.append(geom)
        tags_list.append(tags)
        is_building.append(building)
    geoms = project(wgs)
    heights = []
    for geom, tags, building in zip(geoms, tags_list, is_building, strict=True):
        heights.append(building_height(tags, geom.area) if building else _wall_height(tags))
    # Здания идут перед стенами; порядок ответа сохраняется внутри групп.
    order = sorted(range(len(geoms)), key=lambda i: not is_building[i])
    keep = [i for i in order if heights[i] is not None]
    report.skipped["buildings:non_shield"] += sum(1 for i in order if heights[i] is None)
    report.counts["buildings"] = sum(is_building[i] for i in keep)
    report.counts["barriers"] = len(keep) - report.counts["buildings"]
    return Obstacles(
        geoms=np.asarray([geoms[i] for i in keep], dtype=object),
        heights=np.asarray([heights[i] for i in keep], dtype=float),
        is_building=np.asarray([is_building[i] for i in keep], dtype=bool),
    )


def _green_factor(tags: dict) -> float | None:
    """Фактор затухания листвой для forest/wood/park или None."""
    for key, value in _GREEN_TAGS:
        if tags.get(key) == value:
            return w.GREEN[value]
    return None


def _line_sources(raw: dict, report: BuildReport) -> tuple[list[Source], list[tuple[str, LineString]]]:
    sources: list[Source] = []
    runways: list[tuple[str, LineString]] = []
    for el in raw["lines"]["elements"]:
        tags = el.get("tags") or {}
        if "aeroway" in tags and tags["aeroway"] == "runway":
            geom = element_geometry(el, area=False)
            if geom is None:
                report.skipped["geometry"] += 1
            else:
                runways.append((_osm_id(el), _project_one(geom)))
            continue
        if "highway" in tags:
            if _road_class(tags["highway"]) is None:
                report.skipped["highway:other"] += 1
                continue
            make = road_source
        elif "railway" in tags:
            make = rail_source
        else:
            continue
        geom = element_geometry(el, area=False)
        if geom is None:
            report.skipped["geometry"] += 1
            continue
        source = make(tags, _project_one(geom), _osm_id(el))
        if source is None:
            report.skipped["lines:underground"] += 1
        else:
            sources.append(source)
    return sources, runways


def _area_source(el: dict, tags: dict, subtype: str, geom: BaseGeometry) -> Source:
    day, night = w.AREAS[subtype]
    return Source(
        kind=subtype,
        osm_id=_osm_id(el),
        name=tags.get("name"),
        subtype=subtype,
        geom=_project_one(geom),
        height=w.CONSTRUCTION_SOURCE_HEIGHT_M if subtype == "construction" else w.INDUSTRIAL_HEIGHT_M,
        w_day=day,
        w_night=night,
        radius=w.AREA_RADIUS,
    )


def _point_source(el: dict, tags: dict, geom: BaseGeometry) -> Source:
    amenity = tags["amenity"]
    day, night = w.POINTS[amenity]
    return Source(
        kind=_POINT_KINDS[amenity],
        osm_id=_osm_id(el),
        name=tags.get("name"),
        subtype=amenity,
        geom=_project_one(geom),
        height=w.WORSHIP_HEIGHT_M if amenity == "place_of_worship" else w.BAR_HEIGHT_M,
        w_day=day,
        w_night=night,
        radius=w.POINT_RADII[amenity],
    )


def _area_layers(raw: dict, report: BuildReport, today: date):
    """Источники из ответа areas, зелень и аэродромы (UTM-полигон, теги)."""
    sources: list[Source] = []
    green_geoms, green_factors = [], []
    aerodromes: list[tuple[BaseGeometry, dict]] = []
    for el in raw["areas"]["elements"]:
        tags = el.get("tags") or {}
        amenity = tags.get("amenity")
        if amenity in w.POINTS:
            geom = element_geometry(el, area=True)
            if geom is not None and geom.geom_type != "Point":
                geom = geom.centroid
            if geom is None:
                report.skipped["geometry"] += 1
            else:
                sources.append(_point_source(el, tags, geom))
            continue
        if tags.get("aeroway") == "aerodrome":
            geom = element_geometry(el, area=True)
            if geom is None:
                report.skipped["geometry"] += 1
            else:
                aerodromes.append((_project_one(geom), tags))
            continue
        if tags.get("landuse") == "construction" or tags.get("building") == "construction":
            subtype = "construction"
        elif tags.get("landuse") == "industrial":
            subtype = "industrial"
        else:
            subtype = None
        if subtype:
            geom = element_geometry(el, area=True)
            if geom is None:
                report.skipped["geometry"] += 1
            elif geom.geom_type not in ("Polygon", "MultiPolygon"):
                report.skipped["areas:not_polygon"] += 1
            elif subtype == "construction" and _is_stale(el, today):
                report.skipped["construction:stale"] += 1
            else:
                sources.append(_area_source(el, tags, subtype, geom))
            continue
        factor = _green_factor(tags)
        if factor is not None:
            geom = element_geometry(el, area=True)
            if geom is None:
                report.skipped["geometry"] += 1
            else:
                green_geoms.append(geom)
                green_factors.append(factor)
    green = Green(
        geoms=project(green_geoms),
        factors=np.asarray(green_factors, dtype=float),
    )
    return sources, green, aerodromes


def build_features(raw: dict[str, dict], *, today: date) -> tuple[Features, BuildReport]:
    """Строит слои модели из сырых ответов Overpass (ключи — QUERY_NAMES)."""
    report = BuildReport(counts=Counter(), skipped=Counter())
    boundary, boundary_wgs84 = _boundary(raw)
    obstacles = _obstacles(raw, report)
    sources, green, aerodromes = _area_layers(raw, report, today)
    line_sources, runways = _line_sources(raw, report)
    sources = line_sources + sources
    for osm_id, runway in runways:
        centre = runway.centroid
        tags = next((t for poly, t in aerodromes if poly.contains(centre)), {})
        sources.extend(runway_sources(runway, tags, osm_id))
    report.counts["green"] = len(green.factors)
    for source in sources:
        report.counts[f"source:{source.kind}"] += 1
    features = Features(
        boundary=boundary,
        boundary_wgs84=boundary_wgs84,
        obstacles=obstacles,
        green=green,
        sources=sources,
        osm_timestamp=raw["buildings"]["osm3s"]["timestamp_osm_base"],
    )
    return features, report
