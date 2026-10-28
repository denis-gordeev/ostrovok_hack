"""Тексты запросов Overpass QL: четыре отдельных запроса (один общий слишком тяжёл)."""

import math

# (south, west, north, east) — порядок Overpass.
Bbox = tuple[float, float, float, float]

QUERY_NAMES = ("boundary", "buildings", "lines", "areas")

# Метров в градусе широты.
_M_PER_DEG = 111320.0
_HEADER = "[out:json][timeout:300];"


def area_id(relation_id: int) -> int:
    """Id области Overpass для relation: 3600000000 + id."""
    return 3_600_000_000 + relation_id


def bbox_with_buffer(bounds: dict, buffer_m: float) -> Bbox:
    """Расширяет bounds ответа Overpass на buffer_m метров; долгота считается по cos средней широты."""
    dlat = buffer_m / _M_PER_DEG
    mid_lat = (bounds["minlat"] + bounds["maxlat"]) / 2
    dlon = buffer_m / (_M_PER_DEG * math.cos(math.radians(mid_lat)))
    return (
        bounds["minlat"] - dlat,
        bounds["minlon"] - dlon,
        bounds["maxlat"] + dlat,
        bounds["maxlon"] + dlon,
    )


def _bbox_str(bbox: Bbox) -> str:
    return "(" + ",".join(str(v) for v in bbox) + ")"


def boundary_query(relation_id: int) -> str:
    """Полигон границы города."""
    return f"{_HEADER}\nrel({relation_id});\nout geom;\n"


def buildings_query(relation_id: int) -> str:
    """Здания и стены внутри города."""
    # out tags geom опускает members у relation: без них сборка мультиполигонов пуста.
    return (
        f"{_HEADER}\n"
        f"area(id:{area_id(relation_id)})->.city;\n"
        "(\n"
        '  wr["building"](area.city);\n'
        '  way["barrier"~"^(wall|noise_barrier|city_wall)$"](area.city);\n'
        '  way["historic"="citywalls"](area.city);\n'
        ");\n"
        "out geom;\n"
    )


def lines_query(bbox: Bbox) -> str:
    """Дороги, железные дороги, трамвай и ВПП в bbox."""
    b = _bbox_str(bbox)
    return (
        f"{_HEADER}\n"
        "(\n"
        f'  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]{b};\n'
        f'  way["highway"~"^(residential|unclassified|living_street|service)$"]{b};\n'
        f'  way["railway"~"^(rail|tram|subway|light_rail)$"]{b};\n'
        f'  way["aeroway"="runway"]{b};\n'
        ");\n"
        "out tags geom;\n"
    )


def areas_query(bbox: Bbox) -> str:
    """Площадные и точечные источники и зелень в bbox; meta даёт дату правки строек."""
    b = _bbox_str(bbox)
    return (
        f"{_HEADER}\n"
        "(\n"
        f'  nwr["landuse"~"^(construction|industrial)$"]{b};\n'
        f'  wr["building"="construction"]{b};\n'
        f'  nwr["amenity"~"^(bar|pub|nightclub|place_of_worship)$"]{b};\n'
        f'  wr["aeroway"="aerodrome"]{b};\n'
        f'  wr["landuse"="forest"]{b};\n'
        f'  wr["natural"="wood"]{b};\n'
        f'  wr["leisure"="park"]{b};\n'
        ");\n"
        "out meta geom;\n"
    )
