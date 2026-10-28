"""H3-сетка модельных уровней по городу: расчёт в процессах, хранение в npz."""

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import h3
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from kazan_noise import weights as w
from kazan_noise.config import NoiseConfig
from kazan_noise.model import NoiseModel
from kazan_noise.osm import project
from kazan_noise.snapshot import DAMAGED_ERRORS, SnapshotMissingError, atomic_write, load_features, weights_hash

# Точек на задачу процесса.
CHUNK = 256
_REBUILD_HINT = "запустите kazan-noise grid"

# Модель процесса-воркера: грузится один раз в initializer.
_model: NoiseModel | None = None


@dataclass
class Grid:
    """Ячейки H3 с позициями приёмников (UTM), признаком застройки и уровнями день/ночь.

    osm_timestamp — метка снимка объектов, по которому посчитана сетка.
    """

    cells: np.ndarray
    x: np.ndarray
    y: np.ndarray
    built_up: np.ndarray
    day_db: np.ndarray
    night_db: np.ndarray
    osm_timestamp: str


def grid_cells(boundary_wgs84: BaseGeometry, res: int) -> list[str]:
    """Ячейки H3 заданного разрешения, покрывающие границу (lon/lat), по возрастанию."""
    return sorted(h3.geo_to_cells(boundary_wgs84, res))


def _init_worker(features_path: str, receiver_height: float) -> None:
    global _model
    _model = NoiseModel(load_features(Path(features_path)), receiver_height=receiver_height)


def _levels(xy: np.ndarray) -> np.ndarray:
    """Уровни (день, ночь) для массива позиций (n, 2) в модели процесса."""
    assert _model is not None
    out = np.empty((len(xy), 2))
    for i, (x, y) in enumerate(xy):
        r = _model.receiver(float(x), float(y))
        out[i] = r.day_db, r.night_db
    return out


def _prepare(model: NoiseModel, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Признак застройки (по исходным центрам) и позиции приёмников со сдвигом из зданий."""
    obs = model.features.obstacles
    buildings = obs.geoms[obs.is_building]
    built_up = np.zeros(len(xy), dtype=bool)
    if len(buildings):
        pts = shapely.points(xy)
        built_up[STRtree(buildings).query(pts, predicate="dwithin", distance=w.BUILT_UP_RADIUS_M)[0]] = True
    pos = xy.copy()
    for i, (x, y) in enumerate(xy):
        if model.building_at(float(x), float(y)) is not None:
            pos[i] = model.nudge_outside(float(x), float(y))
    return built_up, pos


def compute_grid(features_path: Path, cfg: NoiseConfig, *, workers: int, limit: int | None = None) -> Grid:
    """Считает уровни на H3-сетке города; limit берёт первые limit ячеек по порядку."""
    features = load_features(features_path)
    model = NoiseModel(features, receiver_height=cfg.receiver_height_m)
    cells = grid_cells(features.boundary_wgs84, cfg.h3_res)[:limit]
    latlng = np.asarray([h3.cell_to_latlng(c) for c in cells]).reshape(-1, 2)
    xy = shapely.get_coordinates(project(shapely.points(latlng[:, 1], latlng[:, 0])))
    built_up, pos = _prepare(model, xy)
    chunks = [pos[i : i + CHUNK] for i in range(0, len(pos), CHUNK)]
    args = (str(features_path), cfg.receiver_height_m)
    if workers <= 1:
        _init_worker(*args)
        parts = [_levels(c) for c in chunks]
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=args) as pool:
            parts = list(pool.map(_levels, chunks))
    levels = np.concatenate(parts) if parts else np.empty((0, 2))
    cells = np.asarray(cells, dtype=np.str_)
    return Grid(cells, pos[:, 0], pos[:, 1], built_up, levels[:, 0], levels[:, 1], features.osm_timestamp)


def save_grid(grid: Grid, path: Path) -> None:
    """Сохраняет сетку в npz атомарно, вместе с хэшем весов и меткой снимка."""
    with atomic_write(path) as f:
        np.savez_compressed(
            f,
            cells=np.asarray(grid.cells, dtype=np.str_),
            x=grid.x,
            y=grid.y,
            built_up=grid.built_up,
            day_db=grid.day_db,
            night_db=grid.night_db,
            osm_timestamp=np.asarray(grid.osm_timestamp, dtype=np.str_),
            weights_hash=np.asarray(weights_hash(), dtype=np.str_),
        )


def load_grid(path: Path) -> Grid:
    """Читает сетку; нет файла, он повреждён или другие веса → SnapshotMissingError."""
    if not path.exists():
        raise SnapshotMissingError(f"Сетка {path} не найдена, {_REBUILD_HINT}")
    try:
        with np.load(path, allow_pickle=False) as z:
            if str(z["weights_hash"]) != weights_hash():
                raise SnapshotMissingError(f"Сетка {path} построена при других весах, {_REBUILD_HINT}")
            return Grid(z["cells"], z["x"], z["y"], z["built_up"], z["day_db"], z["night_db"], str(z["osm_timestamp"]))
    except DAMAGED_ERRORS as exc:
        raise SnapshotMissingError(f"Сетка {path} повреждена ({type(exc).__name__}), {_REBUILD_HINT}") from exc
