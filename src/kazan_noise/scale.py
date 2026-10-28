"""Шкала 0–100: доля застроенных ячеек города, которые строго тише данной точки."""

from typing import TYPE_CHECKING

import numpy as np

from kazan_noise.config import DAY

if TYPE_CHECKING:
    from kazan_noise.grid import Grid

# (нижняя граница балла, подпись); от тихого к шумному.
_LABELS = ((0, "очень тихо"), (20, "тихо"), (40, "средне"), (60, "шумно"), (80, "очень шумно"))


def label(score: int) -> str:
    """Словесная оценка балла 0–100."""
    return [text for low, text in _LABELS if score >= low][-1]


class Scale:
    """Перцентильная шкала по уровням застроенных ячеек (отдельно день и ночь)."""

    def __init__(self, day_levels: np.ndarray, night_levels: np.ndarray):
        self._day = np.sort(np.asarray(day_levels, dtype=float))
        self._night = np.sort(np.asarray(night_levels, dtype=float))

    @classmethod
    def from_grid(cls, grid: "Grid") -> "Scale":
        """Шкала по ячейкам с built_up; без них — ValueError."""
        if not grid.built_up.any():
            raise ValueError("в сетке нет застроенных ячеек для шкалы")
        return cls(grid.day_db[grid.built_up], grid.night_db[grid.built_up])

    def score(self, level: float, mode: str) -> int:
        """round(100 · доля ячеек строго тише level) для режима суток."""
        levels = self._day if mode == DAY else self._night
        return round(100 * int(np.searchsorted(levels, level, side="left")) / len(levels))
