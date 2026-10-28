"""Рантайм-сервис: относительный шум в точке (lat, lon) для инструмента агента."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from pydantic import BaseModel
from shapely.geometry import Point
from shapely.prepared import prep

from kazan_noise.config import DAY, NIGHT
from kazan_noise.grid import load_grid
from kazan_noise.model import Contribution, NoiseModel, PointResult, ReceiverResult
from kazan_noise.osm import project_point
from kazan_noise.propagation import energy_sum
from kazan_noise.scale import Scale, label
from kazan_noise.snapshot import SnapshotMissingError, load_features
from kazan_noise.weights import MIN_SHARE, SHIELDED_DB, TOP_SOURCES


class OutsideKazanError(Exception):
    """Точка вне городского округа Казань."""


class ModeScore(BaseModel):
    """Балл 0–100 и словесная оценка для режима суток."""

    score: int
    label: str


class QuietSide(BaseModel):
    """Оценка тихой стороны здания."""

    day: ModeScore
    night: ModeScore


class SourceInfo(BaseModel):
    """Источник шума, вносящий заметный вклад."""

    kind: str
    subtype: str
    name: str | None
    distance_m: int
    share_day: float
    share_night: float
    shielded: bool


class NoiseReport(BaseModel):
    """Отчёт о шуме в точке."""

    lat: float
    lon: float
    inside_building: bool
    day: ModeScore
    night: ModeScore
    quiet_side: QuietSide | None
    main_sources: list[SourceInfo]
    model_level_day: float
    model_level_night: float
    osm_timestamp: str


def _share(contrib_db: float, total_db: float) -> float:
    """Доля вклада в суммарной энергии: 10^(contrib/10) / 10^(total/10); для -inf — 0."""
    return 10 ** ((contrib_db - total_db) / 10)


class NoiseService:
    """Считает шум в точке по снимку объектов и шкале города."""

    def __init__(self, model: NoiseModel, scale: Scale, osm_timestamp: str):
        self.model = model
        self.scale = scale
        self.osm_timestamp = osm_timestamp
        self._boundary = prep(model.features.boundary)

    @classmethod
    def load(cls, data_dir: Path, *, receiver_height: float = 4.0) -> "NoiseService":
        """Загружает снимок и сетку из data_dir.

        Нет файлов, они повреждены, сетка посчитана по другому снимку или без застроенных ячеек →
        SnapshotMissingError.
        """
        features = load_features(data_dir / "features.npz")
        grid_path = data_dir / "grid.npz"
        grid = load_grid(grid_path)
        if grid.osm_timestamp != features.osm_timestamp:
            raise SnapshotMissingError(f"Сетка {grid_path} построена по другому снимку: запустите kazan-noise grid")
        try:
            scale = Scale.from_grid(grid)
        except ValueError as exc:
            raise SnapshotMissingError(f"Сетка {grid_path}: {exc}, запустите kazan-noise grid") from exc
        model = NoiseModel(features, receiver_height=receiver_height)
        return cls(model, scale, features.osm_timestamp)

    def _inside(self, lat: float, lon: float) -> bool:
        return bool(self._boundary.covers(Point(project_point(lat, lon))))

    def _mode_score(self, day_db: float, night_db: float) -> tuple[ModeScore, ModeScore]:
        day = self.scale.score(day_db, DAY)
        night = self.scale.score(night_db, NIGHT)
        return ModeScore(score=day, label=label(day)), ModeScore(score=night, label=label(night))

    def _groups(self, contributions: list[Contribution]) -> dict[tuple, list[Contribution]]:
        """Вклады по источникам: OSM-пути одной именованной улицы (вид + название) — одна группа,
        безымянные источники — каждый своя."""
        groups: dict[tuple, list[Contribution]] = {}
        for c in contributions:
            src = self.model.features.sources[c.source_idx]
            key = (src.kind, src.name) if src.name is not None else (src.kind, None, c.source_idx)
            groups.setdefault(key, []).append(c)
        return groups

    def _main_sources(self, loud_day: ReceiverResult, loud_night: ReceiverResult) -> list[SourceInfo]:
        """Топ-TOP_SOURCES источников по доле энергии (max из дня и ночи, не меньше MIN_SHARE).

        Доля дня считается у фасада, шумного днём, доля ночи — у шумного ночью.
        """
        by_day, by_night = self._groups(loud_day.contributions), self._groups(loud_night.contributions)
        ranked = []
        for key in dict.fromkeys([*by_day, *by_night]):
            day, night = by_day.get(key, []), by_night.get(key, [])
            share_day = _share(energy_sum(np.array([c.day_db for c in day])), loud_day.day_db)
            share_night = _share(energy_sum(np.array([c.night_db for c in night])), loud_night.night_db)
            ranked.append((max(share_day, share_night), share_day, share_night, day, night))
        ranked.sort(key=lambda r: r[0], reverse=True)
        top = [r for r in ranked if r[0] >= MIN_SHARE][:TOP_SOURCES]
        return [self._source_info(*r[1:]) for r in top]

    def _source_info(
        self, share_day: float, share_night: float, day: list[Contribution], night: list[Contribution]
    ) -> SourceInfo:
        """Описание группы вкладов у приёмника режима, где её доля больше: класс самого громкого вклада,
        мин. расстояние, экран — среднее A_shield вкладов, взвешенное по их энергии в этом режиме."""
        if share_night > share_day:
            group, levels = night, np.array([c.night_db for c in night])
        else:
            group, levels = day, np.array([c.day_db for c in day])
        loudest = group[int(np.argmax(levels))]
        src = self.model.features.sources[loudest.source_idx]
        shield = np.average([c.mean_shield_db for c in group], weights=10 ** ((levels - levels.max()) / 10))
        return SourceInfo(
            kind=src.kind,
            subtype=src.subtype,
            name=src.name,
            distance_m=round(min(c.min_distance_m for c in group)),
            share_day=round(share_day, 2),
            share_night=round(share_night, 2),
            shielded=bool(shield > SHIELDED_DB),
        )

    def noise_at(self, lat: float, lon: float) -> NoiseReport:
        """Отчёт о шуме в точке; вне границы Казани — OutsideKazanError."""
        if (error := self._outside_error(lat, lon)) is not None:
            raise error
        return self._report(lat, lon, self.model.at(*project_point(lat, lon)))

    def noise_many(self, points: Sequence[tuple[float, float]]) -> list[NoiseReport | OutsideKazanError]:
        """Отчёты для точек (lat, lon) в их порядке; точка вне Казани получает OutsideKazanError вместо отчёта.

        Приёмники всех точек считаются вместе (NoiseModel.at_many), отчёты те же, что у noise_at по одной.
        """
        errors = [self._outside_error(lat, lon) for lat, lon in points]
        inside = [p for p, error in zip(points, errors, strict=True) if error is None]
        computed = iter(self.model.at_many([project_point(lat, lon) for lat, lon in inside]))
        return [
            error if error is not None else self._report(lat, lon, next(computed))
            for (lat, lon), error in zip(points, errors, strict=True)
        ]

    def _outside_error(self, lat: float, lon: float) -> OutsideKazanError | None:
        """OutsideKazanError для точки вне границы Казани (с подсказкой о перепутанных lat/lon), иначе None."""
        if self._inside(lat, lon):
            return None
        message = "Точка вне городского округа Казань"
        if self._inside(lon, lat):
            message += "; похоже, перепутаны lat и lon"
        return OutsideKazanError(message)

    def _report(self, lat: float, lon: float, p: PointResult) -> NoiseReport:
        """Отчёт по результату модели в точке."""
        # Балл и доли каждого режима — у фасада, шумного именно в этом режиме.
        level_day, level_night = p.loud_day.day_db, p.loud_night.night_db
        day, night = self._mode_score(level_day, level_night)
        quiet_side = None
        if p.quiet_day is not None and p.quiet_night is not None:
            q_day, q_night = self._mode_score(p.quiet_day.day_db, p.quiet_night.night_db)
            quiet_side = QuietSide(day=q_day, night=q_night)
        return NoiseReport(
            lat=lat,
            lon=lon,
            inside_building=p.inside_building,
            day=day,
            night=night,
            quiet_side=quiet_side,
            main_sources=self._main_sources(p.loud_day, p.loud_night),
            model_level_day=round(level_day, 1),
            model_level_night=round(level_night, 1),
            osm_timestamp=self.osm_timestamp,
        )
