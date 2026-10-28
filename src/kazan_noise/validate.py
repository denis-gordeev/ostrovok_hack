"""Проверка модели: санити-проверки по снимку и корреляция с жалобами на шум в отзывах островка."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from shapely import STRtree, points
from shapely.geometry import LineString, Point

from kazan_noise import weights as w
from kazan_noise.grid import Grid
from kazan_noise.osm import unproject_xy
from kazan_noise.service import NoiseService, OutsideKazanError

# Слова о шуме; «музыка» тоже: музыка снаружи — уже шум. «Гул» без «гулять», «рёв» отдельным словом.
_NOISE = r"шум|слышн|громк|грохо[тч]|\bгул(?!я)|гуд(?:ок|к)|\bр[её]в(?:а|е|у|ом)?\b|музык"
# Окончания существительных: «бар», но не «барьер»; «клуб», но не «клубничный».
_NOUN = r"(?:а|е|у|ом|ы|ов|ах|ами)?\b"
# Внешние источники: «дорога», но не «дорого»; «машина», но не «кофемашина»; «поезд», но не «поездка».
_SOURCE = (
    r"улиц|дорог(?:а|и|е|у|ой|ам|ами|ах)?\b|трасс|окн|трамва|(?<!кофе)машин|автомоб"
    rf"|\bбар{_NOUN}|\bклуб{_NOUN}|стройк|поезд(?!к)|электрич(?:к|ек)|самол|вокзал|рельс|\bдвор{_NOUN}"
)
# Слово о шуме и источник в одном отзыве не дальше ~40 символов друг от друга, в любом порядке.
_NEAR = r".{0,40}"
EXTERNAL_NOISE = re.compile(
    rf"(?:{_NOISE}){_NEAR}(?:{_SOURCE})|(?:{_SOURCE}){_NEAR}(?:{_NOISE})",
    re.IGNORECASE | re.DOTALL,
)
INTERNAL_NOISE = re.compile(r"стен|сосед|коридор|лифт", re.IGNORECASE)


def is_noise_complaint(minus: str | None) -> bool:
    """Жалоба на внешний шум: слово о шуме рядом с внешним источником, а внутренний (стены, соседи) не упомянут."""
    if not minus:
        return False
    return bool(EXTERNAL_NOISE.search(minus)) and not INTERNAL_NOISE.search(minus)


def _ranks(values: Sequence[float]) -> np.ndarray:
    """Ранги с 1; равным значениям — средний ранг."""
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(len(arr))
    sorted_vals = arr[order]
    i = 0
    while i < len(arr):
        j = i
        while j + 1 < len(arr) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Ранговая корреляция Спирмена (Пирсон по средним рангам); nan при постоянном ряде."""
    ra, rb = _ranks(a), _ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


@dataclass
class HotelRow:
    """Отель с числом отзывов и жалоб на внешний шум."""

    hotel_id: str
    name: str
    lat: float
    lon: float
    reviews: int
    complaints: int


_HOTELS_SQL = """
SELECT h.hotel_id, h.name, h.lat, h.lon, array_agg(r.minus) AS minuses
FROM ostrovok.hotels h JOIN ostrovok.reviews r USING (hotel_id)
WHERE h.lat IS NOT NULL AND h.lon IS NOT NULL
GROUP BY h.hotel_id, h.name, h.lat, h.lon
HAVING count(*) >= %s
"""


def load_hotels(conn, *, min_reviews: int = 10) -> list[HotelRow]:
    """Отели с координатами и не менее min_reviews отзывов; жалобы считаются в Python."""
    rows = conn.execute(_HOTELS_SQL, (min_reviews,)).fetchall()
    return [
        HotelRow(
            hotel_id=r["hotel_id"],
            name=r["name"],
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            reviews=len(r["minuses"]),
            complaints=sum(is_noise_complaint(m) for m in r["minuses"]),
        )
        for r in rows
    ]


@dataclass
class ValidationReport:
    """Корреляция баллов модели с долей жалоб; worst — самые расходящиеся отели."""

    n: int
    rho_day: float
    rho_night: float
    worst: list[tuple[HotelRow, int]]


def validate_hotels(service: NoiseService, rows: list[HotelRow]) -> ValidationReport:
    """Спирмен «балл шума — доля жалоб» по отелям внутри Казани."""
    used: list[HotelRow] = []
    day: list[float] = []
    night: list[float] = []
    for row in rows:
        try:
            report = service.noise_at(row.lat, row.lon)
        except OutsideKazanError:
            continue
        used.append(row)
        day.append(report.day.score)
        night.append(report.night.score)
    share = [r.complaints / r.reviews for r in used]
    gap = np.abs(_ranks(night) - _ranks(share)) if used else np.array([])
    top = np.argsort(-gap, kind="stable")[:10]
    worst = [(used[i], int(round(gap[i]))) for i in top]
    return ValidationReport(
        n=len(used),
        rho_day=spearman(day, share) if used else float("nan"),
        rho_night=spearman(night, share) if used else float("nan"),
        worst=worst,
    )


@dataclass
class SanityCheck:
    """Санити-проверка: медиана дневного балла по набору точек против ожидания."""

    name: str
    n: int
    median_score: float | None
    expected: str
    ok: bool | None


def _median_day_score(service: NoiseService, xy: list[tuple[float, float]]) -> tuple[int, float | None]:
    """Число посчитанных точек и медиана дневного балла (точки вне Казани пропускаются)."""
    scores = []
    for x, y in xy:
        lat, lon = unproject_xy(np.float64(x), np.float64(y))
        try:
            scores.append(service.noise_at(float(lat), float(lon)).day.score)
        except OutsideKazanError:
            continue
    return len(scores), (float(np.median(scores)) if scores else None)


def _verdict(name: str, xy, service, expected: str, passes) -> SanityCheck:
    n, median = _median_day_score(service, xy)
    ok = None if n < w.SANITY_MIN_POINTS or median is None else bool(passes(median))
    return SanityCheck(name, n, median, expected, ok)


def _road_points(service: NoiseService, rng: np.random.Generator, sample: int) -> list[tuple[float, float]]:
    """Точки в 15 м от магистралей по нормали, вне зданий."""
    model = service.model
    lines = [
        s.geom
        for s in model.features.sources
        if s.kind == "road" and s.subtype in w.SANITY_MAJOR_ROADS and isinstance(s.geom, LineString)
    ]
    if not lines:
        return []
    lengths = np.array([g.length for g in lines])
    out: list[tuple[float, float]] = []
    for _ in range(sample * w.SANITY_ATTEMPTS_PER_POINT):
        if len(out) >= sample:
            break
        line = lines[rng.choice(len(lines), p=lengths / lengths.sum())]
        d = rng.uniform(0, line.length)
        p = line.interpolate(d)
        a = line.interpolate(max(d - 1.0, 0.0))
        b = line.interpolate(min(d + 1.0, line.length))
        dx, dy = b.x - a.x, b.y - a.y
        norm = float(np.hypot(dx, dy))
        if norm == 0:
            continue
        sign = 1.0 if rng.random() < 0.5 else -1.0
        x = p.x - sign * dy / norm * w.SANITY_ROAD_OFFSET_M
        y = p.y + sign * dx / norm * w.SANITY_ROAD_OFFSET_M
        if model.building_at(x, y) is None:
            out.append((x, y))
    return out


def _courtyard_points(
    service: NoiseService, grid: Grid, rng: np.random.Generator, sample: int
) -> list[tuple[float, float]]:
    """Застроенные ячейки далеко от дорог tertiary+/трамвая/рельсов и рядом с высоким зданием."""
    feats = service.model.features
    busy = {*w.SANITY_BUSY_ROADS, *(f"{c}_link" for c in w.SANITY_BUSY_ROADS)}
    roads = [
        s.geom for s in feats.sources if s.kind in ("tram", "rail") or (s.kind == "road" and s.subtype in busy)
    ]
    obst = feats.obstacles
    tall = obst.geoms[obst.is_building & (obst.heights >= w.SANITY_COURTYARD_BUILDING_H_M)]
    idx = np.flatnonzero(grid.built_up)
    pts = points(grid.x[idx], grid.y[idx])
    keep = np.ones(len(idx), dtype=bool)
    if roads:
        near_road = STRtree(roads).query(pts, predicate="dwithin", distance=w.SANITY_COURTYARD_ROAD_DIST_M)[0]
        keep[near_road] = False
    near_tall = np.zeros(len(idx), dtype=bool)
    if len(tall):
        near_tall[STRtree(tall).query(pts, predicate="dwithin", distance=w.SANITY_COURTYARD_BUILDING_DIST_M)[0]] = True
    chosen = idx[keep & near_tall]
    if len(chosen) > sample:
        chosen = rng.choice(chosen, size=sample, replace=False)
    return [(float(grid.x[i]), float(grid.y[i])) for i in chosen]


def _forest_points(service: NoiseService, rng: np.random.Generator, sample: int) -> list[tuple[float, float]]:
    """Точки крупных лесов (green с фактором 1 и непустым buffer(-300)) не ближе 300 м к опушке."""
    green = service.model.features.green
    inner = []
    for geom in green.geoms[green.factors == 1.0]:
        core = geom.buffer(-w.SANITY_FOREST_INNER_BUFFER_M)
        if not core.is_empty:
            p = core.representative_point()
            inner.append((p.x, p.y))
    if len(inner) > sample:
        inner = [inner[i] for i in rng.choice(len(inner), size=sample, replace=False)]
    return inner


def sanity_checks(service: NoiseService, grid: Grid, *, sample: int = 50, seed: int = 0) -> list[SanityCheck]:
    """Три проверки здравого смысла: у магистрали шумно, во дворе тише, в лесу тихо."""
    rng = np.random.default_rng(seed)
    return [
        _verdict(
            "у магистрали", _road_points(service, rng, sample), service,
            f"медиана ≥ {w.SANITY_ROAD_MIN_SCORE}", lambda m: m >= w.SANITY_ROAD_MIN_SCORE,
        ),
        _verdict(
            "двор", _courtyard_points(service, grid, rng, sample), service,
            f"медиана ≤ {w.SANITY_COURTYARD_MAX_SCORE}", lambda m: m <= w.SANITY_COURTYARD_MAX_SCORE,
        ),
        _verdict(
            "лес", _forest_points(service, rng, sample), service,
            f"медиана ≤ {w.SANITY_FOREST_MAX_SCORE}", lambda m: m <= w.SANITY_FOREST_MAX_SCORE,
        ),
    ]
