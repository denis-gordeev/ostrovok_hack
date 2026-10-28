"""Снимок классифицированных объектов на диске: npz без pickle и защита от устаревших снимков."""

import hashlib
import json
import os
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import numpy as np
import shapely

from kazan_noise import weights
from kazan_noise.features import (BuildReport, Features, Green, Obstacles,
                                  Source)

FORMAT_VERSION = 1
_REBUILD_HINT = "пересоберите: kazan-noise build && kazan-noise grid"


# Ошибки чтения повреждённого или недописанного npz.
DAMAGED_ERRORS = (zipfile.BadZipFile, zlib.error, KeyError, OSError, EOFError, ValueError)


class SnapshotMissingError(Exception):
    """Снимка нет, он повреждён либо устарел (другой формат или веса)."""


@contextmanager
def atomic_write(path: Path) -> Iterator[BinaryIO]:
    """Файл для записи рядом с path: после успешной записи он заменяет path, при ошибке удаляется.

    Оборванная запись не портит прежний файл; np.savez* получает открытый файл и не дописывает «.npz».
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pack_geoms(geoms) -> tuple[np.ndarray, np.ndarray]:
    """Геометрии → (WKB одним uint8-буфером, int64-смещения длиной n+1)."""
    blobs = [shapely.to_wkb(g) for g in geoms]
    offsets = np.zeros(len(blobs) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum([len(b) for b in blobs], dtype=np.int64)
    buf = np.frombuffer(b"".join(blobs), dtype=np.uint8).copy()
    return buf, offsets


def unpack_geoms(buf: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Обратное к pack_geoms: object-массив геометрий."""
    raw = buf.tobytes()
    out = np.empty(len(offsets) - 1, dtype=object)
    for i in range(len(out)):
        out[i] = shapely.from_wkb(raw[offsets[i] : offsets[i + 1]])
    return out


def weights_hash() -> str:
    """Первые 16 hex от sha256 байтов weights.py: смена весов делает снимок устаревшим."""
    return hashlib.sha256(Path(weights.__file__).read_bytes()).hexdigest()[:16]


def save_features(features: Features, path: Path) -> None:
    """Сохраняет слои в npz (без pickle) атомарно."""
    obs, green, sources = features.obstacles, features.green, features.sources
    obs_wkb, obs_off = pack_geoms(obs.geoms)
    green_wkb, green_off = pack_geoms(green.geoms)
    src_wkb, src_off = pack_geoms([s.geom for s in sources])
    with atomic_write(path) as f:
        np.savez_compressed(
            f,
            obstacles_wkb=obs_wkb,
            obstacles_off=obs_off,
            obstacles_height=np.asarray(obs.heights, dtype=float),
            obstacles_is_building=np.asarray(obs.is_building, dtype=bool),
            green_wkb=green_wkb,
            green_off=green_off,
            green_factor=np.asarray(green.factors, dtype=float),
            src_wkb=src_wkb,
            src_off=src_off,
            src_kind=np.asarray([s.kind for s in sources], dtype=np.str_),
            src_osm_id=np.asarray([s.osm_id for s in sources], dtype=np.str_),
            src_name=np.asarray([s.name or "" for s in sources], dtype=np.str_),
            src_subtype=np.asarray([s.subtype for s in sources], dtype=np.str_),
            src_height=np.asarray([s.height for s in sources], dtype=float),
            src_w_day=np.asarray([s.w_day for s in sources], dtype=float),
            src_w_night=np.asarray([s.w_night for s in sources], dtype=float),
            src_radius=np.asarray([s.radius for s in sources], dtype=float),
            boundary_wkb=np.asarray(shapely.to_wkb(features.boundary).hex(), dtype=np.str_),
            boundary_wgs84_wkb=np.asarray(shapely.to_wkb(features.boundary_wgs84).hex(), dtype=np.str_),
            osm_timestamp=np.asarray(features.osm_timestamp, dtype=np.str_),
            weights_hash=np.asarray(weights_hash(), dtype=np.str_),
            format_version=np.asarray(FORMAT_VERSION, dtype=np.int64),
        )


def _stale(path: Path, reason: str) -> SnapshotMissingError:
    return SnapshotMissingError(f"Снимок шума {path} {reason}, {_REBUILD_HINT}")


def load_features(path: Path) -> Features:
    """Читает слои из npz; нет файла, он повреждён, другой формат или веса → SnapshotMissingError."""
    if not path.exists():
        raise _stale(path, "не найден")
    try:
        return _read_features(path)
    except DAMAGED_ERRORS as exc:
        raise _stale(path, f"повреждён ({type(exc).__name__})") from exc


def _read_features(path: Path) -> Features:
    with np.load(path, allow_pickle=False) as z:
        if int(z["format_version"]) != FORMAT_VERSION:
            raise _stale(path, "в другом формате")
        if str(z["weights_hash"]) != weights_hash():
            raise _stale(path, "построен при других весах модели")
        sources = [
            Source(
                kind=str(kind),
                osm_id=str(osm_id),
                name=str(name) or None,
                subtype=str(subtype),
                geom=geom,
                height=float(height),
                w_day=float(w_day),
                w_night=float(w_night),
                radius=float(radius),
            )
            for kind, osm_id, name, subtype, geom, height, w_day, w_night, radius in zip(
                z["src_kind"],
                z["src_osm_id"],
                z["src_name"],
                z["src_subtype"],
                unpack_geoms(z["src_wkb"], z["src_off"]),
                z["src_height"],
                z["src_w_day"],
                z["src_w_night"],
                z["src_radius"],
                strict=True,
            )
        ]
        return Features(
            boundary=shapely.from_wkb(bytes.fromhex(str(z["boundary_wkb"]))),
            boundary_wgs84=shapely.from_wkb(bytes.fromhex(str(z["boundary_wgs84_wkb"]))),
            obstacles=Obstacles(
                geoms=unpack_geoms(z["obstacles_wkb"], z["obstacles_off"]),
                heights=z["obstacles_height"],
                is_building=z["obstacles_is_building"],
            ),
            green=Green(geoms=unpack_geoms(z["green_wkb"], z["green_off"]), factors=z["green_factor"]),
            sources=sources,
            osm_timestamp=str(z["osm_timestamp"]),
        )


def write_meta(path: Path, features: Features, report: BuildReport) -> None:
    """Пишет meta.json атомарно: версия, метка OSM, хэш весов, время сборки и счётчики отчёта."""
    meta = {
        "format_version": FORMAT_VERSION,
        "osm_timestamp": features.osm_timestamp,
        "weights_hash": weights_hash(),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": dict(report.counts),
        "skipped": dict(report.skipped),
    }
    with atomic_write(path) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))


def read_meta(path: Path) -> dict:
    """Читает meta.json."""
    return json.loads(path.read_text(encoding="utf-8"))
