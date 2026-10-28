"""Офлайн-скачивание сырых данных OSM в raw_dir/<имя>.json.gz."""

import gzip
import json
import os
from typing import Protocol

from kazan_noise import queries
from kazan_noise.config import NoiseConfig
from kazan_noise.overpass import OverpassError

# Запросы, пустой ответ на которые — сбой сервера, а не данные: без границы и зданий модели нет.
_REQUIRED = ("boundary", "buildings")


class _Querier(Protocol):
    def query(self, ql: str) -> dict: ...


def _raw_path(cfg: NoiseConfig, name: str):
    return cfg.raw_dir / f"{name}.json.gz"


def load_raw(cfg: NoiseConfig, name: str) -> dict:
    """Читает сохранённый ответ Overpass."""
    with gzip.open(_raw_path(cfg, name), "rt", encoding="utf-8") as f:
        return json.load(f)


def _save(cfg: NoiseConfig, name: str, data: dict) -> None:
    """Пишет во временный файл и переименовывает: оборванная запись не оставит битый файл."""
    path = _raw_path(cfg, name)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _query_text(cfg: NoiseConfig, name: str) -> str:
    if name == "boundary":
        return queries.boundary_query(cfg.boundary_relation)
    if name == "buildings":
        return queries.buildings_query(cfg.boundary_relation)
    # lines/areas ограничены bbox границы с запасом; граница уже скачана.
    elements = load_raw(cfg, "boundary")["elements"]
    bounds = next(e["bounds"] for e in elements if "bounds" in e)
    bbox = queries.bbox_with_buffer(bounds, cfg.source_buffer_m)
    return queries.lines_query(bbox) if name == "lines" else queries.areas_query(bbox)


def fetch_all(client: _Querier, cfg: NoiseConfig, *, refresh: bool = False) -> list[str]:
    """Скачивает недостающие запросы по порядку; возвращает имена, скачанные в этом запуске.

    Пустой ответ для boundary или buildings → OverpassError, файл не сохраняется.
    """
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    fetched: list[str] = []
    for name in queries.QUERY_NAMES:
        if _raw_path(cfg, name).exists() and not refresh:
            continue
        data = client.query(_query_text(cfg, name))
        if name in _REQUIRED and not data.get("elements"):
            raise OverpassError(f"ответ на запрос {name} пуст: повторите kazan-noise fetch позже")
        _save(cfg, name, data)
        fetched.append(name)
    return fetched
