"""Распространение звука: подисточники, расходимость, воздух, экраны, зелень, сумма энергий."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from kazan_noise import weights as w
from kazan_noise.features import Source


@dataclass(frozen=True)
class SubSources:
    """Подисточники одного приёмника: координаты, высота, мощность день/ночь, индекс источника."""

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    w_day: np.ndarray
    w_night: np.ndarray
    source_idx: np.ndarray

    @classmethod
    def empty(cls) -> "SubSources":
        """Пустой набор подисточников."""
        e = np.empty(0)
        return cls(e, e, e, e, e, np.empty(0, dtype=int))

    def take(self, mask: np.ndarray) -> "SubSources":
        """Подисточники, отобранные маской (или индексами)."""
        return SubSources(
            self.x[mask], self.y[mask], self.z[mask], self.w_day[mask], self.w_night[mask], self.source_idx[mask]
        )


def _line_parts(geom: BaseGeometry) -> list[BaseGeometry]:
    """Линейные части геометрии (MultiLineString разбирается на части)."""
    return list(geom.geoms) if hasattr(geom, "geoms") else [geom]


class _Base(NamedTuple):
    """Подисточники источника без привязки к приёмнику, по частям геометрии.

    Части линий — точки базового шага BASE_STEP_M: i — номер точки в части, n — число точек части, p — шаг
    части; приёмник прореживает их по расстоянию (steps). Готовые подисточники (площади, точки, коридоры
    захода) — части из одной точки с p = 1: прореживание их не трогает, а поправка 10·lg(1·1) = 0.
    """

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    w_day: np.ndarray
    w_night: np.ndarray
    i: np.ndarray
    n: np.ndarray
    p: np.ndarray


def _fixed(x: np.ndarray, y: np.ndarray, z: np.ndarray, w_day: np.ndarray, w_night: np.ndarray) -> _Base:
    """Готовые подисточники, каждый — своя часть из одной точки."""
    k = len(x)
    return _Base(x, y, z, w_day, w_night, np.zeros(k, dtype=int), np.ones(k, dtype=int), np.ones(k))


def _concat(bases: Sequence[_Base]) -> _Base:
    """Подисточники нескольких частей подряд."""
    if not bases:
        e = np.empty(0)
        return _fixed(e, e, e, e, e)
    return _Base(*map(np.concatenate, zip(*bases)))


def _line_base(part: BaseGeometry, source: Source) -> _Base:
    """Подисточники одной линии до прореживания."""
    length = part.length
    if length <= 0:
        return _concat([])
    if source.kind == "aircraft" and source.subtype == "corridor":
        n = max(1, math.ceil(length / w.AIRCRAFT_STEP_M))
        s = w.AIRCRAFT_STEP_M * (np.arange(n) + 0.5)
        s = s[s < length] if (s < length).any() else np.array([length / 2])
        gain = np.full(len(s), 10 * math.log10(w.AIRCRAFT_STEP_M))
        pts = shapely.line_interpolate_point(part, s)
        z = source.height + s * w.GLIDE_TAN
        return _fixed(shapely.get_x(pts), shapely.get_y(pts), z, source.w_day + gain, source.w_night + gain)
    n = max(1, math.ceil(length / w.BASE_STEP_M))
    p = length / n
    i = np.arange(n)
    pts = shapely.line_interpolate_point(part, p * (i + 0.5))
    return _Base(
        shapely.get_x(pts),
        shapely.get_y(pts),
        np.full(n, source.height),
        np.full(n, source.w_day),
        np.full(n, source.w_night),
        i,
        np.full(n, n),
        np.full(n, p),
    )


def _capped(day: np.ndarray, night: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Сдвигает мощности обоих режимов вниз на одну величину, если сумма громкого режима превышает AREA_LW_CAP.

    Общий сдвиг сохраняет разницу день/ночь из AREAS.
    """
    excess = max(energy_sum(day), energy_sum(night)) - w.AREA_LW_CAP
    return (day - excess, night - excess) if excess > 0 else (day, night)


def _area_points(source: Source) -> tuple[np.ndarray, ...]:
    """Подисточники площади: сетка AREA_STEP_M, суммарная мощность громкого режима ограничена AREA_LW_CAP."""
    geom = source.geom
    minx, miny, maxx, maxy = geom.bounds
    xs = np.arange(minx + w.AREA_STEP_M / 2, maxx, w.AREA_STEP_M)
    ys = np.arange(miny + w.AREA_STEP_M / 2, maxy, w.AREA_STEP_M)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    # contains_xy готовит геометрию (prepare) на месте, а подготовленную геометрию нельзя делить между потоками:
    # GEOS лениво строит её индексы, и dwithin в NoiseModel._candidates из потоков at_many падал с segfault.
    # Поэтому готовится копия, а геометрия источника остаётся неподготовленной.
    inside = shapely.contains_xy(shapely.from_wkb(shapely.to_wkb(geom)), gx, gy)
    gx, gy = gx[inside], gy[inside]
    if len(gx):
        gain = 10 * math.log10(geom.area / len(gx))
    else:
        c = geom.centroid
        gx, gy = np.array([c.x]), np.array([c.y])
        gain = 10 * math.log10(geom.area)
    n = len(gx)
    day, night = _capped(np.full(n, source.w_day + gain), np.full(n, source.w_night + gain))
    return gx, gy, np.full(n, source.height), day, night


def _source_base(source: Source) -> _Base:
    """Подисточники источника до прореживания."""
    if source.kind in ("construction", "industrial"):
        return _fixed(*_area_points(source))
    if source.geom.geom_type in ("Point", "MultiPoint"):
        c = np.array([[g.x, g.y] for g in _line_parts(source.geom)]).reshape(-1, 2)
        n = len(c)
        return _fixed(c[:, 0], c[:, 1], np.full(n, source.height), np.full(n, source.w_day), np.full(n, source.w_night))
    return _concat([_line_base(part, source) for part in _line_parts(source.geom)])


def discretize(
    sources: Sequence[Source],
    indices: Iterable[int],
    rx: float,
    ry: float,
    *,
    steps: Sequence[tuple[float, float]] = w.STEPS,
    cache: dict[int, _Base] | None = None,
) -> SubSources:
    """Разбивает источники с данными индексами на подисточники для приёмника (rx, ry).

    Подисточники дальше source.radius по горизонтали отбрасываются. cache — подисточники источников
    до прореживания по индексу источника, общие для приёмников одной пачки (у них почти те же источники).
    """
    indices = list(indices)
    if not indices:
        return SubSources.empty()
    bases = []
    for idx in indices:
        base = cache.get(idx) if cache is not None else None
        if base is None:
            base = _source_base(sources[idx])
            if cache is not None:
                cache[idx] = base
        bases.append(base)
    b = _concat(bases)
    counts = [len(base.x) for base in bases]
    radius = np.repeat([sources[idx].radius for idx in indices], counts)
    source_idx = np.repeat(np.asarray(indices, dtype=int), counts)
    d = np.hypot(b.x - rx, b.y - ry)
    limits = np.array([limit for limit, _ in steps], dtype=float)
    deltas = np.array([step for _, step in steps], dtype=float)
    delta = deltas[np.minimum(np.searchsorted(limits, d, side="left"), len(deltas) - 1)]
    k = np.maximum(1, np.round(delta / w.BASE_STEP_M)).astype(int)
    kept = np.flatnonzero(b.i % k == 0)
    # Каждая оставленная точка отвечает за фактическое число кусков до следующей в своей части (или до её конца);
    # первая точка части (i = 0) остаётся всегда, так что следующая с i = 0 — уже из новой части.
    i = b.i[kept]
    following = np.append(i[1:], 0)
    cnt = np.where(following == 0, b.n[kept], following) - i
    gain = 10 * np.log10(cnt * b.p[kept])
    near = d[kept] <= radius[kept]
    sel, gain = kept[near], gain[near]
    return SubSources(b.x[sel], b.y[sel], b.z[sel], b.w_day[sel] + gain, b.w_night[sel] + gain, source_idx[sel])


def divergence_db(d: np.ndarray) -> np.ndarray:
    """Геометрическая расходимость от точечного источника: 20·lg(max(d, D_MIN)) + 11."""
    return 20 * np.log10(np.maximum(d, w.D_MIN_M)) + 11


def air_db(d: np.ndarray) -> np.ndarray:
    """Поглощение в воздухе, дБ."""
    return w.AIR_DB_PER_M * d


def path_difference(hs: float, hr: float, dist: float, profile: Sequence[tuple[float, float]]) -> tuple[float, int]:
    """Разность хода δ (м) и число вершин верхней выпуклой оболочки над лучом.

    profile — точки (расстояние от источника, высота препятствия) строго между источником и приёмником.
    """
    pts = [(0.0, hs)] + sorted((x, h) for x, h in profile if 0 < x < dist) + [(dist, hr)]
    hull: list[tuple[float, float]] = []
    for p in pts:
        while len(hull) >= 2:
            (ax, ay), (bx, by) = hull[-2], hull[-1]
            if (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) >= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    path = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(hull, hull[1:]))
    return path - math.hypot(dist, hr - hs), len(hull) - 2


def shield_db(delta: float, vertices: int) -> float:
    """Ослабление экраном по разности хода; 0 без вершин, потолок 20 дБ (одна) / 25 дБ (несколько)."""
    if vertices <= 0:
        return 0.0
    cap = w.SHIELD_MAX_SINGLE if vertices == 1 else w.SHIELD_MAX_MULTI
    return min(10 * math.log10(3 + 20 * delta / w.WAVELENGTH_M), cap)


def foliage_db(path_m: float) -> float:
    """Затухание листвой по пути в зелени (уже умноженному на фактор), дБ."""
    if path_m < w.FOLIAGE_MIN_PATH_M:
        return 0.0
    return min(w.FOLIAGE_DB_PER_M * path_m, w.FOLIAGE_MAX_DB)


def energy_sum(levels: np.ndarray) -> float:
    """Энергетическая сумма уровней, дБ; для пустого массива -inf."""
    if len(levels) == 0:
        return -math.inf
    m = float(np.max(levels))
    if m == -math.inf:
        return -math.inf
    return m + 10 * math.log10(float(np.sum(10 ** ((levels - m) / 10))))
