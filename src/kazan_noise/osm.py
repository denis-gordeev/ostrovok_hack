"""Геометрия элементов Overpass (out geom) и проекция WGS84 <-> UTM 39N."""

from collections.abc import Iterable

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

from kazan_noise.config import UTM, WGS84

# always_xy: порядок координат (lon, lat) / (x, y) в обе стороны.
_TO_UTM = Transformer.from_crs(WGS84, UTM, always_xy=True)
_FROM_UTM = Transformer.from_crs(UTM, WGS84, always_xy=True)


def _coords(geometry: Iterable[dict | None] | None) -> list[tuple[float, float]]:
    """Точки (lon, lat) из geometry way; null-записи выбрасываются."""
    return [(p["lon"], p["lat"]) for p in geometry or [] if p]


def _polygonal(geom: BaseGeometry) -> BaseGeometry | None:
    """Полигональная часть геометрии (Polygon/MultiPolygon) или None."""
    if geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in getattr(geom, "geoms", ()) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return None
    return shapely.unary_union(parts)


def _way_geometry(el: dict, *, area: bool) -> BaseGeometry | None:
    pts = _coords(el.get("geometry"))
    if len(pts) < 2:
        return None
    if not area:
        return LineString(pts)
    if len(pts) < 4 or pts[0] != pts[-1]:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        return _polygonal(make_valid(poly))
    return None if poly.is_empty else poly


def _rings(members: Iterable[dict], roles: tuple[str, ...]) -> BaseGeometry | None:
    """Собирает полигоны из ways-членов с заданными ролями через polygonize."""
    lines = []
    for m in members:
        if m.get("type") != "way" or m.get("role", "") not in roles:
            continue
        pts = _coords(m.get("geometry"))
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        return None
    return _polygonal(shapely.polygonize(lines))


def _relation_geometry(el: dict) -> BaseGeometry | None:
    members = el.get("members") or []
    outer = _rings(members, ("outer", ""))
    if outer is None:
        return None
    inner = _rings(members, ("inner",))
    geom = outer.difference(inner) if inner is not None else outer
    return _polygonal(geom)


def element_geometry(el: dict, *, area: bool) -> BaseGeometry | None:
    """Геометрия элемента Overpass в WGS84, координаты (lon, lat).

    Node -> Point; way -> Polygon (area=True, только замкнутый) или LineString;
    relation -> полигон(ы) multipolygon (линейные relation не поддерживаются, None).
    """
    kind = el.get("type")
    if kind == "node":
        return Point(el["lon"], el["lat"])
    if kind == "way":
        return _way_geometry(el, area=area)
    if kind == "relation" and area:
        return _relation_geometry(el)
    return None


def project(geoms: Iterable[BaseGeometry]) -> np.ndarray:
    """Проецирует геометрии WGS84 -> UTM 39N, возвращает массив геометрий."""
    arr = np.asarray(list(geoms), dtype=object)
    return shapely.transform(arr, lambda c: np.column_stack(_TO_UTM.transform(c[:, 0], c[:, 1])))


def project_point(lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) WGS84 -> (x, y) в метрах UTM 39N."""
    x, y = _TO_UTM.transform(lon, lat)
    return x, y


def unproject_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Координаты UTM 39N -> (lat, lon) WGS84."""
    lon, lat = _FROM_UTM.transform(x, y)
    return lat, lon
