"""Все настраиваемые числа модели шума: источники, здания, распространение, сетка.

Уровни относительные (не дБ(А)); значения взяты из спецификации, этап 2.
"""

import math
from typing import NamedTuple

# Источник выключен в этом режиме суток.
OFF = -100.0

# --- Мощность линейных источников: W' = 10·lg(Q) + 20·lg(v) + C ---
# C_ROAD даёт ≈95 дБ на автомобиль при 50 км/ч.
C_ROAD = 14.0
C_TRAM = C_ROAD + 12
C_RAIL = C_ROAD + 18
# Для самолётов: W' = 10·lg(Q) + C_AIR.
C_AIR = 86.0


class RoadClass(NamedTuple):
    """Параметры класса дороги: потоки авто/ч, скорость км/ч, полосы, грузовики, радиус м."""

    q_day: float
    q_night: float
    speed: float
    lanes: int
    heavy_db: float
    radius: float


ROADS: dict[str, RoadClass] = {
    "motorway": RoadClass(3000, 600, 110, 4, 3, 1000),
    "trunk": RoadClass(3000, 600, 80, 4, 3, 1000),
    "primary": RoadClass(1500, 250, 60, 4, 0, 1000),
    "secondary": RoadClass(800, 120, 60, 2, 0, 1000),
    "tertiary": RoadClass(400, 50, 60, 2, 0, 1000),
    "residential": RoadClass(60, 8, 40, 2, 0, 300),
    "unclassified": RoadClass(60, 8, 40, 2, 0, 300),
    "living_street": RoadClass(15, 2, 20, 1, 0, 300),
    "service": RoadClass(15, 2, 20, 1, 0, 300),
}
# Съезды (*_link) пропускают долю потока основной дороги.
LINK_FLOW_FACTOR = 0.3
# Нижняя граница скорости в формуле, км/ч.
MIN_SPEED = 20.0

# Рельсы: (q_day, q_night, speed) — составов/ч и км/ч.
RAILS: dict[str, tuple[float, float, float]] = {
    "tram": (10, 2, 40),
    "light_rail": (10, 2, 40),
    "subway": (10, 2, 40),
    "rail": (4, 3, 80),
}
RAIL_RADIUS = 1000.0
# Поправка для веток/депо/подъездных путей, дБ.
RAIL_MINOR_DB = -10.0

# Аэродромы по ICAO: (q_day, q_night) движений/ч. Заглушка до данных OpenSky.
AERODROMES: dict[str, tuple[float, float]] = {"UWKD": (6.0, 2.0), "UWKG": (0.3, 0.0)}
AERODROME_DEFAULT = (0.2, 0.0)
# Длина коридора захода от торца ВПП, м.
CORRIDOR_M = 8000.0
GLIDE_TAN = math.tan(math.radians(3))
AIRCRAFT_STEP_M = 100.0
AIRCRAFT_RADIUS = 3000.0

# Площадные источники: (w_day, w_night) дБ/м² (DIN 18005), радиус м.
AREAS: dict[str, tuple[float, float]] = {"construction": (65, 45), "industrial": (60, 55)}
AREA_RADIUS = 1000.0
# Предел суммарной мощности площади, дБ.
AREA_LW_CAP = 115.0
AREA_STEP_M = 20.0
# Стройки без правок дольше этого срока отбрасываются.
STALE_CONSTRUCTION_DAYS = 3 * 365

# Точечные источники: (w_day, w_night) дБ.
POINTS: dict[str, tuple[float, float]] = {
    "nightclub": (85, 95),
    "bar": (75, 85),
    "pub": (75, 85),
    "place_of_worship": (80, OFF),
}
POINT_RADII: dict[str, float] = {"nightclub": 150, "bar": 150, "pub": 150, "place_of_worship": 300}

# Высота источников над землёй, м.
ROAD_HEIGHT_M = 0.5
RAIL_HEIGHT_M = 0.5
BRIDGE_EXTRA_M = 6.0
BAR_HEIGHT_M = 1.5
WORSHIP_HEIGHT_M = 20.0
CONSTRUCTION_HEIGHT_M = 3.0
CONSTRUCTION_SOURCE_HEIGHT_M = 3.0
INDUSTRIAL_HEIGHT_M = 5.0

# --- Здания-экраны ---
LEVEL_M = 3.0
ROOF_M = 1.0
TYPE_HEIGHTS: dict[str, float] = {
    "garage": 3.0,
    "garages": 3.0,
    "shed": 3.0,
    "kiosk": 3.0,
    "house": 7.0,
    "detached": 7.0,
    "industrial": 9.0,
    "warehouse": 9.0,
    "apartments": 27.0,
}
# (площадь м² строго меньше, высота м); больше последней границы — AREA_HEIGHT_MAX.
AREA_HEIGHTS = ((60, 3.0), (300, 7.0), (1500, 15.0))
AREA_HEIGHT_MAX = 21.0
# Значения building=*, которые шум не экранируют.
NON_SHIELD = {"roof", "carport", "ruins", "no"}

# Стены: высота по умолчанию, м (ключ — значение barrier, historic=citywalls -> city_wall).
WALLS: dict[str, float] = {"noise_barrier": 4.0, "wall": 2.5, "city_wall": 2.5}

# --- Зелень: фактор затухания листвой ---
GREEN: dict[str, float] = {"forest": 1.0, "wood": 1.0, "park": 0.5}
FOLIAGE_DB_PER_M = 0.05
FOLIAGE_MAX_DB = 10.0
FOLIAGE_MIN_PATH_M = 10.0

# --- Распространение ---
D_MIN_M = 5.0
AIR_DB_PER_M = 0.005
WAVELENGTH_M = 0.68
SHIELD_MAX_SINGLE = 20.0
SHIELD_MAX_MULTI = 25.0
BACKGROUND_DB = 35.0
BASE_STEP_M = 5.0
# (расстояние до, шаг дискретизации линии), м.
STEPS = ((200, 10), (500, 25), (math.inf, 50))

# Отсечение самых тихих подисточников до расчёта экранов и листвы: в каждом режиме отбрасываются самые тихие,
# чья суммарная энергия без экранов (W − расходимость − воздух) не больше этой доли от общей; подисточник
# уходит, только если отброшен и днём, и ночью. Экран снимает не больше 25 дБ (×316), поэтому итог
# меняется не больше чем на ≈0,15 дБ: 10·lg(1 + 316·1e-4). Листва (до 10 дБ) в худшем теоретическом случае
# поднимает оценку до ≈1,2 дБ; на реальных точках Казани расхождение ≤ 0,002 дБ.
PRUNE_ENERGY_FRACTION = 1e-4

# --- Модель и сетка ---
MAX_FACADE_POINTS = 8
FACADE_OFFSET_M = 2.0
FACADE_STEP_M = 10.0
# Шаг поиска свободной точки на кольце фасада, если ближайшая попала в соседнее здание, м.
NUDGE_STEP_M = 1.0
BUILT_UP_RADIUS_M = 100.0
SHIELDED_DB = 5.0
MIN_SHARE = 0.01
TOP_SOURCES = 3

# --- Санити-проверки (kazan-noise validate) ---
SANITY_MIN_POINTS = 5
SANITY_ROAD_OFFSET_M = 15.0
SANITY_ROAD_MIN_SCORE = 75
SANITY_COURTYARD_ROAD_DIST_M = 150.0
SANITY_COURTYARD_BUILDING_DIST_M = 30.0
SANITY_COURTYARD_BUILDING_H_M = 15.0
SANITY_COURTYARD_MAX_SCORE = 50
SANITY_FOREST_INNER_BUFFER_M = 300.0
SANITY_FOREST_MAX_SCORE = 20
# Классы дорог, считающиеся магистралями, и классы tertiary и выше (с _link).
SANITY_MAJOR_ROADS = ("motorway", "trunk", "primary")
SANITY_BUSY_ROADS = ("motorway", "trunk", "primary", "secondary", "tertiary")
# Попыток подобрать точку у магистрали на одну нужную точку.
SANITY_ATTEMPTS_PER_POINT = 10
