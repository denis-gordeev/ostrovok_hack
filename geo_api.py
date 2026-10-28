"""HTTP API for the local Kazan OSM snapshot: uvicorn geo_api:app --port 8080."""

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import networkx as nx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from shapely.geometry import Point

from geo_tools import CACHE_DIR, OVERPASS_URL, cached_result, ox, search_osm
from transit_routing import plan_transit

# The imported extract includes surroundings and Kazan airport.
BOUNDS = (48.75, 55.55, 49.55, 56.10)  # west, south, east, north
LOCK = threading.Lock()
app = FastAPI(title="Kazan OSMnx API", version="0.1.0")


class Coordinates(BaseModel):
    lat: float = Field(ge=BOUNDS[1], le=BOUNDS[3])
    lon: float = Field(ge=BOUNDS[0], le=BOUNDS[2])


class FeatureRequest(Coordinates):
    tags: dict[str, str | list[str] | bool]
    radius_m: int = Field(default=1000, ge=1, le=5000)
    limit: int = Field(default=30, ge=1, le=100)


class RouteRequest(BaseModel):
    start: Coordinates
    end: Coordinates


class TransitRequest(RouteRequest):
    departure_time: datetime | None = None


@app.post('/transit/route')
def transit_route(request: TransitRequest):
    try:
        with LOCK:
            return plan_transit(request.start.lat, request.start.lon,
                                request.end.lat, request.end.lon, request.departure_time)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f'Transit routing unavailable: {type(exc).__name__}: {exc}') from exc


@app.get("/health")
def health():
    return {"status": "ok", "region": "Kazan", "bbox": BOUNDS, "overpass_url": OVERPASS_URL}


@app.post("/features")
def features(request: FeatureRequest):
    try:
        with LOCK:
            return search_osm.invoke(request.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Geodata unavailable: {type(exc).__name__}: {exc}") from exc


@app.post("/route")
def walking_route(request: RouteRequest):
    start, end = request.start, request.end
    # Bound download/CPU costs. Longer routes can be added as a separate feature.
    distance = ox.distance.great_circle(start.lat, start.lon, end.lat, end.lon)
    if distance > 10_000:
        raise HTTPException(422, "Maximum straight-line distance between endpoints is 10 km")
    bbox = (
        max(BOUNDS[0], min(start.lon, end.lon) - 0.02),
        max(BOUNDS[1], min(start.lat, end.lat) - 0.015),
        min(BOUNDS[2], max(start.lon, end.lon) + 0.02),
        min(BOUNDS[3], max(start.lat, end.lat) + 0.015),
    )

    def compute():
        key = hashlib.sha256(json.dumps([OVERPASS_URL, bbox, "walk"]).encode()).hexdigest()
        folder = CACHE_DIR / "graphs"
        folder.mkdir(parents=True, exist_ok=True)
        graph_path = folder / f"{key}.graphml"
        if graph_path.exists():
            graph = ox.load_graphml(graph_path)
        else:
            graph = ox.graph_from_bbox(bbox, network_type="walk", retain_all=True)
            ox.save_graphml(graph, graph_path)
        projected = ox.project_graph(graph)
        nodes = ox.graph_to_gdfs(projected, edges=False)

        def snap(coordinates):
            point = ox.projection.project_geometry(
                Point(coordinates.lon, coordinates.lat), to_crs=nodes.crs
            )[0]
            distances = nodes.geometry.distance(point)
            node = distances.idxmin()
            offset = float(distances.loc[node])
            if offset > 300:
                raise HTTPException(422, "No walking graph node within 300 m of an endpoint")
            return node, offset

        origin, start_offset = snap(start)
        destination, end_offset = snap(end)
        try:
            route = nx.shortest_path(graph, origin, destination, weight="length")
        except nx.NetworkXNoPath as exc:
            raise HTTPException(404, "No walking path found within the downloaded graph") from exc
        edges = [min(graph[u][v].values(), key=lambda edge: edge["length"]) for u, v in zip(route, route[1:])]
        length = sum(edge["length"] for edge in edges)
        geometry = []
        for (u, v), edge in zip(zip(route, route[1:]), edges):
            line = edge.get("geometry")
            points = list(line.coords) if line is not None else [(graph.nodes[u]["x"], graph.nodes[u]["y"]), (graph.nodes[v]["x"], graph.nodes[v]["y"])]
            # OSM edge geometry may run opposite to the traversal direction.
            u_point = Point(graph.nodes[u]["x"], graph.nodes[u]["y"])
            if Point(points[-1]).distance(u_point) < Point(points[0]).distance(u_point):
                points.reverse()
            geometry.extend(points if not geometry else points[1:])
        return {
            "length_m": round(length, 1),
            "estimated_minutes": round(length / (4500 / 60), 1),
            "assumed_speed_kmh": 4.5,
            "start_snap_distance_m": round(start_offset, 1),
            "end_snap_distance_m": round(end_offset, 1),
            "geometry": {"type": "LineString", "coordinates": geometry} if geometry else None,
            "note": "Route between nearest graph nodes; endpoint connectors excluded. No wheelchair-accessibility guarantee. Graph is limited to the local bbox; a route outside it may be shorter.",
        }

    try:
        with LOCK:
            return cached_result("routes", [OVERPASS_URL, request.model_dump(), bbox], compute)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Geodata unavailable: {type(exc).__name__}: {exc}") from exc
