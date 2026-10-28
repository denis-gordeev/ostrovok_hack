from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.affinity import translate
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.base import BaseGeometry

from kazan_noise.features import Features, Green, Obstacles, Source
from kazan_noise.model import NoiseModel
from kazan_noise.osm import unproject_xy
from kazan_noise.scale import Scale
from kazan_noise.service import NoiseService
from kazan_noise.snapshot import save_features

# Центр Казани в WGS84 и в проекции EPSG:32639 (UTM 39N).
KAZAN_LAT, KAZAN_LON = 55.7963, 49.1088
KAZAN_X, KAZAN_Y = 381438.3, 6185027.2


class FakeTime:
    """Фейковые часы: двигаются только на sleep, паузы копятся в списке."""

    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _geom(lonlat: list[tuple[float, float]]) -> list[dict]:
    """Список (lon, lat) → геометрия в формате Overpass out geom."""
    return [{"lat": lat, "lon": lon} for lon, lat in lonlat]


def _bounds(lonlat: list[tuple[float, float]]) -> dict:
    lons = [p[0] for p in lonlat]
    lats = [p[1] for p in lonlat]
    return {"minlat": min(lats), "minlon": min(lons), "maxlat": max(lats), "maxlon": max(lons)}


def osm_node(id: int, lat: float, lon: float, tags: dict | None = None) -> dict:
    """Node в формате Overpass."""
    return {"type": "node", "id": id, "lat": lat, "lon": lon, "tags": tags or {}}


def osm_way(
    id: int,
    lonlat: list[tuple[float, float]],
    tags: dict | None = None,
    timestamp: str | None = None,
) -> dict:
    """Way в формате Overpass out geom; точки задаются как (lon, lat)."""
    el = {
        "type": "way",
        "id": id,
        "bounds": _bounds(lonlat),
        "nodes": list(range(id * 1000, id * 1000 + len(lonlat))),
        "geometry": _geom(lonlat),
        "tags": tags or {},
    }
    if timestamp:
        el["timestamp"] = timestamp
    return el


def osm_relation(
    id: int,
    members: list[tuple[str, list[tuple[float, float]]]],
    tags: dict | None = None,
    timestamp: str | None = None,
) -> dict:
    """Relation в формате Overpass out geom; члены — (роль, [(lon, lat), ...]), все ways."""
    allpts = [p for _, pts in members for p in pts]
    el = {
        "type": "relation",
        "id": id,
        "bounds": _bounds(allpts),
        "members": [
            {"type": "way", "ref": id * 100 + i, "role": role, "geometry": _geom(pts)}
            for i, (role, pts) in enumerate(members)
        ],
        "tags": tags or {},
    }
    if timestamp:
        el["timestamp"] = timestamp
    return el


def raw_response(elements: list[dict], timestamp: str = "2026-10-26T18:41:36Z") -> dict:
    """Ответ Overpass целиком с меткой времени базы OSM."""
    return {"version": 0.6, "osm3s": {"timestamp_osm_base": timestamp}, "elements": elements}


def local_scene(
    sources: Sequence[Source] = (),
    buildings: Sequence[tuple[BaseGeometry, float]] = (),
    walls: Sequence[tuple[BaseGeometry, float]] = (),
    green: Sequence[tuple[BaseGeometry, float]] = (),
) -> Features:
    """Локальная сцена в метрах: граница box(-5000, -5000, 5000, 5000), здания (геометрия, высота)
    идут перед стенами, зелень — (геометрия, фактор)."""
    obstacles = list(buildings) + list(walls)
    return Features(
        boundary=box(-5000, -5000, 5000, 5000),
        boundary_wgs84=box(KAZAN_LON - 0.08, KAZAN_LAT - 0.045, KAZAN_LON + 0.08, KAZAN_LAT + 0.045),
        obstacles=Obstacles(
            geoms=np.asarray([g for g, _ in obstacles], dtype=object),
            heights=np.asarray([h for _, h in obstacles], dtype=float),
            is_building=np.asarray([True] * len(buildings) + [False] * len(walls), dtype=bool),
        ),
        green=Green(
            geoms=np.asarray([g for g, _ in green], dtype=object),
            factors=np.asarray([f for _, f in green], dtype=float),
        ),
        sources=list(sources),
        osm_timestamp="2026-10-26T18:41:36Z",
    )


def _latlon(x: float, y: float) -> tuple[float, float]:
    lat, lon = unproject_xy(np.asarray([x]), np.asarray([y]))
    return float(lat[0]), float(lon[0])


# Точки для проверок сервиса (lat, lon): у дороги, вдали от неё и центр здания у дороги.
NEAR_ROAD = _latlon(KAZAN_X + 300, KAZAN_Y - 15)
FAR_FROM_ROAD = _latlon(KAZAN_X, KAZAN_Y - 800)
ON_BUILDING = _latlon(KAZAN_X, KAZAN_Y + 30)


def _to_kazan(geom: BaseGeometry) -> BaseGeometry:
    return translate(geom, KAZAN_X, KAZAN_Y)


def scene_service(
    sources: Sequence[Source] = (),
    buildings: Sequence[tuple[BaseGeometry, float]] = (),
) -> NoiseService:
    """Сервис над локальной сценой (метры от центра Казани, как в local_scene), сдвинутой в UTM;
    шкала — равномерная, 30–90 дБ через 0,5 дБ."""
    scene = local_scene(
        sources=[replace(s, geom=_to_kazan(s.geom)) for s in sources],
        buildings=[(_to_kazan(g), h) for g, h in buildings],
    )
    scene = replace(scene, boundary=_to_kazan(scene.boundary))
    levels = np.arange(30.0, 90.0, 0.5)
    return NoiseService(NoiseModel(scene), Scale(levels, levels), scene.osm_timestamp)


def scene_latlon(x: float, y: float) -> tuple[float, float]:
    """(lat, lon) точки локальной сцены scene_service."""
    return _latlon(KAZAN_X + x, KAZAN_Y + y)


def kazan_snapshot(data_dir: Path) -> Features:
    """Сцена 2×2 км вокруг центра Казани: primary-дорога по оси x, ряд зданий вдоль неё
    и скопление в юго-западном углу. Сохраняет features.npz в data_dir."""
    half = 1000.0
    boundary = box(KAZAN_X - half, KAZAN_Y - half, KAZAN_X + half, KAZAN_Y + half)
    xs, ys = (np.asarray(c) for c in zip(*boundary.exterior.coords, strict=True))
    lats, lons = unproject_xy(xs, ys)
    boundary_wgs84 = Polygon(zip(lons.tolist(), lats.tolist(), strict=True))
    road = Source(
        "road", "way/1", "ул. Тестовая", "primary",
        LineString([(KAZAN_X - 1500, KAZAN_Y), (KAZAN_X + 1500, KAZAN_Y)]), 0.5, 80.0, 72.0, 1000,
    )
    buildings: list[tuple] = [(box(KAZAN_X - 10, KAZAN_Y + 20, KAZAN_X + 10, KAZAN_Y + 40), 15.0)]
    # Ряд домов вдоль дороги с севера.
    for dx in range(-900, 901, 60):
        if dx:
            buildings.append((box(KAZAN_X + dx - 5, KAZAN_Y + 25, KAZAN_X + dx + 5, KAZAN_Y + 35), 9.0))
    # Скопление в юго-западном углу: среди первых по порядку ячеек есть и застроенные, и нет.
    for i in range(6):
        for j in range(6):
            x0, y0 = KAZAN_X - 980 + i * 40, KAZAN_Y - 800 + j * 40
            buildings.append((box(x0, y0, x0 + 20, y0 + 20), 12.0))
    features = Features(
        boundary=boundary,
        boundary_wgs84=boundary_wgs84,
        obstacles=Obstacles(
            geoms=np.asarray([g for g, _ in buildings], dtype=object),
            heights=np.asarray([h for _, h in buildings], dtype=float),
            is_building=np.ones(len(buildings), dtype=bool),
        ),
        green=Green(geoms=np.asarray([], dtype=object), factors=np.asarray([], dtype=float)),
        sources=[road],
        osm_timestamp="2026-10-26T18:41:36Z",
    )
    save_features(features, data_dir / "features.npz")
    return features
