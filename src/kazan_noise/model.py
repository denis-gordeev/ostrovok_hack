"""Модель в точке: уровни день/ночь у приёмника, вклады источников, шумный и тихий фасады здания."""

import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point
from shapely.ops import nearest_points

from kazan_noise import weights as w
from kazan_noise.features import Features
from kazan_noise.propagation import (
    air_db,
    discretize,
    divergence_db,
    energy_sum,
    path_difference,
    shield_db,
)


@dataclass(frozen=True)
class Contribution:
    """Вклад одного источника у приёмника.

    min_distance_m — минимальное расстояние до его подисточников по горизонтали,
    mean_shield_db — среднее арифметическое A_shield по всем его подисточникам (нули включены).
    """

    source_idx: int
    day_db: float
    night_db: float
    min_distance_m: float
    mean_shield_db: float


@dataclass(frozen=True)
class ReceiverResult:
    """Уровни у приёмника с учётом фона; вклады отсортированы по max(day_db, night_db) по убыванию."""

    x: float
    y: float
    day_db: float
    night_db: float
    contributions: list[Contribution]


@dataclass(frozen=True)
class PointResult:
    """Результат в точке; фасады выбираются отдельно для дня и ночи.

    Внутри здания: loud_day/quiet_day — фасады с max/min day_db, loud_night/quiet_night — с max/min night_db.
    Снаружи: один приёмник в loud_day и loud_night, quiet_day и quiet_night — None.
    """

    inside_building: bool
    loud_day: ReceiverResult
    loud_night: ReceiverResult
    quiet_day: ReceiverResult | None
    quiet_night: ReceiverResult | None


class NoiseModel:
    """Относительная модель шума над слоями Features (всё в метрах UTM)."""

    def __init__(
        self, features: Features, *, receiver_height: float = 4.0, prune: bool = True, threads: int | None = None
    ):
        """prune=False отключает отсечение самых тихих подисточников (PRUNE_ENERGY_FRACTION), для проверок.

        threads — потоков на приёмники в at/at_many (по умолчанию по числу ядер): почти всё время приёмника
        уходит в запросы и пересечения GEOS, а shapely на них отпускает GIL.
        """
        self.features = features
        self.receiver_height = receiver_height
        self.prune = prune
        self.threads = threads or os.cpu_count() or 1
        self._source_geoms = np.asarray([s.geom for s in features.sources], dtype=object)
        self._source_radii = np.asarray([s.radius for s in features.sources], dtype=float)
        self._max_radius = float(self._source_radii.max()) if len(self._source_radii) else 0.0
        self._source_tree = STRtree(self._source_geoms)
        self._obstacle_tree = STRtree(features.obstacles.geoms)
        self._green_tree = STRtree(features.green.geoms)

    def receiver(self, x: float, y: float, *, cache: dict | None = None) -> ReceiverResult:
        """Уровни день/ночь и вклады источников в приёмнике (x, y) на высоте receiver_height.

        cache — общий кэш подисточников пачки приёмников (см. discretize), на результат не влияет.
        """
        subs = discretize(self.features.sources, self._candidates(x, y), x, y, cache=cache)
        if len(subs.x) == 0:
            return ReceiverResult(x, y, w.BACKGROUND_DB, w.BACKGROUND_DB, [])
        d3 = np.hypot(np.hypot(subs.x - x, subs.y - y), subs.z - self.receiver_height)
        if self.prune:
            # Экраны и листва — самая дорогая часть: их не считают для заведомо неслышных подисточников.
            spread = divergence_db(d3) + air_db(d3)
            keep = _audible(subs.w_day - spread, subs.w_night - spread)
            subs, d3 = subs.take(keep), d3[keep]
        n = len(subs.x)
        dist = np.hypot(subs.x - x, subs.y - y)
        a_shield = np.zeros(n)
        a_green = np.zeros(n)
        # Подисточник точно над/под приёмником луча в плане не имеет: без экранов и листвы.
        ray = np.flatnonzero(dist > 0)
        if len(ray):
            coords = np.empty((len(ray), 2, 2))
            coords[:, 0, 0], coords[:, 0, 1] = subs.x[ray], subs.y[ray]
            coords[:, 1] = (x, y)
            rays = shapely.linestrings(coords)
            a_shield[ray] = self._shielding(rays, coords[:, 0], subs.z[ray], dist[ray])
            a_green[ray] = self._foliage(rays)
        loss = divergence_db(d3) + air_db(d3) + a_shield + a_green
        day, night = subs.w_day - loss, subs.w_night - loss
        return ReceiverResult(
            x=x,
            y=y,
            day_db=energy_sum(np.append(day, w.BACKGROUND_DB)),
            night_db=energy_sum(np.append(night, w.BACKGROUND_DB)),
            contributions=_contributions(subs.source_idx, day, night, dist, a_shield),
        )

    def at(self, x: float, y: float) -> PointResult:
        """Точка: внутри здания считаются приёмники на его фасадах; шумный (max) и тихий (min) фасады
        выбираются отдельно по дню и по ночи — ночью громче может быть двор с баром.

        Собственное здание остаётся экраном — оно и даёт тихую сторону.
        """
        return self.at_many([(x, y)])[0]

    def at_many(self, points: Sequence[tuple[float, float]]) -> list[PointResult]:
        """at для каждой точки, в том же порядке; приёмники всех точек считаются вместе в threads потоках.

        Каждый приёмник считается тем же receiver, что и по одному, поэтому результат от числа потоков не зависит;
        подисточники источников (до прореживания) у приёмников пачки общие.
        """
        buildings = [self.building_at(x, y) for x, y in points]
        receivers = [
            [(x, y)] if idx is None else self.facade_points(idx).tolist()
            for (x, y), idx in zip(points, buildings, strict=True)
        ]
        flat = [xy for group in receivers for xy in group]
        cache: dict = {}
        workers = min(self.threads, len(flat))
        if workers <= 1:
            done = [self.receiver(x, y, cache=cache) for x, y in flat]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                done = list(pool.map(lambda xy: self.receiver(*xy, cache=cache), flat))
        results = iter(done)
        return [
            _point_result(idx is not None, [next(results) for _ in group])
            for idx, group in zip(buildings, receivers, strict=True)
        ]

    def building_at(self, x: float, y: float) -> int | None:
        """Индекс здания в obstacles, содержащего точку (первый по индексу), иначе None; стены не считаются."""
        # query(..., "within") проверяет point.within(geom), то есть geom.contains(point).
        hits = self._obstacle_tree.query(Point(x, y), predicate="within")
        hits = hits[self.features.obstacles.is_building[hits]]
        return int(hits.min()) if len(hits) else None

    def facade_points(self, idx: int) -> np.ndarray:
        """Приёмники на фасадах здания idx (N×2, N ≤ MAX_FACADE_POINTS).

        Точки стоят на всех кольцах buffer(FACADE_OFFSET_M), включая дворы, с шагом
        max(FACADE_STEP_M, суммарная длина колец / MAX_FACADE_POINTS). Каждое кольцо получает
        хотя бы одну точку (двор — типичная тихая сторона); лишние снимаются с самых длинных колец.
        Точки внутри других зданий (сплошная застройка, общие стены) отбрасываются; если так
        пропадают все, возвращается полный набор.
        """
        buffered = self.features.obstacles.geoms[idx].buffer(w.FACADE_OFFSET_M)
        rings = shapely.get_rings(shapely.get_parts(buffered))
        lengths = shapely.length(rings)
        spacing = max(w.FACADE_STEP_M, float(lengths.sum()) / w.MAX_FACADE_POINTS)
        keep = np.argsort(-lengths, kind="stable")[: w.MAX_FACADE_POINTS]
        rings, lengths = rings[keep], lengths[keep]
        counts = np.maximum(1, np.floor(lengths / spacing).astype(int))
        while counts.sum() > w.MAX_FACADE_POINTS:
            counts[np.argmax(counts)] -= 1
        ring_of = np.repeat(np.arange(len(rings)), counts)
        k = np.arange(len(ring_of)) - np.repeat(np.cumsum(counts) - counts, counts)
        along = (k + 0.5) * lengths[ring_of] / counts[ring_of]
        pts = shapely.get_coordinates(shapely.line_interpolate_point(rings[ring_of], along))
        free = ~self._in_building(pts, exclude=idx)
        return pts[free] if free.any() else pts

    def nudge_outside(self, x: float, y: float) -> tuple[float, float]:
        """Точку внутри здания переносит на кольцо buffer(FACADE_OFFSET_M); снаружи зданий не меняет.

        Буфер строится по зданию вместе с касающимися его зданиями. Берётся ближайшая точка кольца;
        если она внутри другого здания (щель оцифровки OSM), — ближайшая свободная из точек кольца
        через NUDGE_STEP_M; если свободных нет, остаётся ближайшая.
        """
        idx = self.building_at(x, y)
        if idx is None:
            return x, y
        obstacles = self.features.obstacles
        block = self._obstacle_tree.query(obstacles.geoms[idx], predicate="intersects")
        block = block[obstacles.is_building[block]]
        boundary = shapely.union_all(obstacles.geoms[block]).buffer(w.FACADE_OFFSET_M).boundary
        nearest = nearest_points(boundary, Point(x, y))[0]
        if not self._in_building(np.array([[nearest.x, nearest.y]]))[0]:
            return nearest.x, nearest.y
        lines = shapely.get_parts(boundary)
        counts = np.ceil(shapely.length(lines) / w.NUDGE_STEP_M).astype(int)
        line_of = np.repeat(np.arange(len(lines)), counts)
        k = np.arange(len(line_of)) - np.repeat(np.cumsum(counts) - counts, counts)
        pts = shapely.get_coordinates(shapely.line_interpolate_point(lines[line_of], k * w.NUDGE_STEP_M))
        pts = pts[~self._in_building(pts)]
        if not len(pts):
            return nearest.x, nearest.y
        best = pts[np.argmin(np.hypot(pts[:, 0] - x, pts[:, 1] - y))]
        return float(best[0]), float(best[1])

    def _in_building(self, pts: np.ndarray, *, exclude: int = -1) -> np.ndarray:
        """Маска точек (N×2), лежащих внутри какого-либо здания, кроме здания exclude."""
        pt_i, obs_i = self._obstacle_tree.query(shapely.points(pts), predicate="within")
        hit = pt_i[self.features.obstacles.is_building[obs_i] & (obs_i != exclude)]
        mask = np.zeros(len(pts), dtype=bool)
        mask[hit] = True
        return mask

    def _candidates(self, x: float, y: float) -> list[int]:
        """Индексы источников, до геометрии которых не дальше их собственного радиуса (по возрастанию)."""
        p = Point(x, y)
        idx = self._source_tree.query(p, predicate="dwithin", distance=self._max_radius)
        idx = idx[shapely.dwithin(self._source_geoms[idx], p, self._source_radii[idx])]
        return np.sort(idx).tolist()

    def _shielding(self, rays: np.ndarray, starts: np.ndarray, hs: np.ndarray, dist: np.ndarray) -> np.ndarray:
        """A_shield по каждому лучу: профиль из всех вершин пересечений с экранами, затем path_difference.

        Экран, внутри которого стоит сам подисточник (бар или храм в своём здании, стройка), для него
        не преграда: звук выходит через двери, окна и крышу.
        """
        obstacles = self.features.obstacles
        out = np.zeros(len(rays))
        ray_i, obs_i = self._obstacle_tree.query(rays, predicate="intersects")
        own_r, own_o = self._obstacle_tree.query(shapely.points(starts), predicate="within")
        if len(own_r):
            n = len(obstacles.heights)
            keep = ~np.isin(ray_i * n + obs_i, own_r * n + own_o)
            ray_i, obs_i = ray_i[keep], obs_i[keep]
        if not len(ray_i):
            return out
        inter = shapely.intersection(rays[ray_i], obstacles.geoms[obs_i])
        coords, pair = shapely.get_coordinates(inter, return_index=True)
        r = ray_i[pair]
        px = np.hypot(coords[:, 0] - starts[r, 0], coords[:, 1] - starts[r, 1])
        ph = obstacles.heights[obs_i[pair]]
        # Точки на линии видимости и ниже не бывают вершинами верхней оболочки: такие лучи не экранированы.
        hr = self.receiver_height
        above = (px > 0) & (px < dist[r]) & (ph > hs[r] + (hr - hs[r]) * px / dist[r])
        r, px, ph = r[above], px[above], ph[above]
        if not len(r):
            return out
        order = np.argsort(r, kind="stable")
        r, px, ph = r[order], px[order].tolist(), ph[order].tolist()
        hit, first = np.unique(r, return_index=True)
        ends = np.append(first[1:], len(r))
        for ri, a, b in zip(hit.tolist(), first.tolist(), ends.tolist(), strict=True):
            delta, vertices = path_difference(float(hs[ri]), hr, float(dist[ri]), list(zip(px[a:b], ph[a:b])))
            out[ri] = shield_db(delta, vertices)
        return out

    def _foliage(self, rays: np.ndarray) -> np.ndarray:
        """A_green по каждому лучу: длина в зелени × фактор, просуммированная по полигонам."""
        green = self.features.green
        ray_i, green_i = self._green_tree.query(rays, predicate="intersects")
        inside = shapely.length(shapely.intersection(rays[ray_i], green.geoms[green_i])) * green.factors[green_i]
        path = np.bincount(ray_i, weights=inside, minlength=len(rays))
        # Векторная форма foliage_db.
        return np.where(path < w.FOLIAGE_MIN_PATH_M, 0.0, np.minimum(w.FOLIAGE_DB_PER_M * path, w.FOLIAGE_MAX_DB))


def _point_result(inside_building: bool, results: list[ReceiverResult]) -> PointResult:
    """PointResult по приёмникам точки: снаружи здания — один, внутри — по фасадам."""
    if not inside_building:
        (r,) = results
        return PointResult(inside_building=False, loud_day=r, loud_night=r, quiet_day=None, quiet_night=None)
    days = [r.day_db for r in results]
    nights = [r.night_db for r in results]
    return PointResult(
        inside_building=True,
        loud_day=results[int(np.argmax(days))],
        loud_night=results[int(np.argmax(nights))],
        quiet_day=results[int(np.argmin(days))],
        quiet_night=results[int(np.argmin(nights))],
    )


def _audible(day: np.ndarray, night: np.ndarray) -> np.ndarray:
    """Маска подисточников, оставшихся после отсечения самых тихих (уровни без экранов, по режимам).

    В каждом режиме отбрасываются самые тихие, чья накопленная энергия не больше PRUNE_ENERGY_FRACTION
    от общей; подисточник уходит, только если отброшен в обоих режимах.
    """
    keep = np.zeros(len(day), dtype=bool)
    for levels in (day, night):
        order = np.argsort(levels, kind="stable")
        energy = np.cumsum(10 ** ((levels[order] - levels.max()) / 10))
        keep[order[energy > w.PRUNE_ENERGY_FRACTION * energy[-1]]] = True
    return keep


def _contributions(
    source_idx: np.ndarray, day: np.ndarray, night: np.ndarray, dist: np.ndarray, a_shield: np.ndarray
) -> list[Contribution]:
    """Группирует уровни подисточников по источнику; сортирует по max(день, ночь) по убыванию."""
    groups, inv, counts = np.unique(source_idx, return_inverse=True, return_counts=True)
    order = np.argsort(inv, kind="stable")
    bounds = np.cumsum(counts)[:-1]
    days, nights = np.split(day[order], bounds), np.split(night[order], bounds)
    min_dist = np.full(len(groups), np.inf)
    np.minimum.at(min_dist, inv, dist)
    mean_shield = np.bincount(inv, weights=a_shield) / counts
    result = [
        Contribution(int(s), energy_sum(d), energy_sum(nt), float(md), float(ms))
        for s, d, nt, md, ms in zip(groups, days, nights, min_dist, mean_shield, strict=True)
    ]
    result.sort(key=lambda c: max(c.day_db, c.night_db), reverse=True)
    return result
