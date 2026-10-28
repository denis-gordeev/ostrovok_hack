"""HTTP API for the local Kazan OSM snapshot: uvicorn geo_api:app --port 8080."""

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import networkx as nx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import Point

from geo_tools import CACHE_DIR, OVERPASS_URL, cached_result, ox, search_osm
from transit_routing import plan_transit
from region_config import BOUNDS, DATA_VERSION, MAX_DRIVE_DISTANCE_M, MAX_WALK_DISTANCE_M, MAX_SEARCH_RADIUS_M

# The imported extract includes surroundings and Kazan airport.
LOCK = threading.Lock()
app = FastAPI(title="Kazan OSMnx API", version="0.1.0")


class Coordinates(BaseModel):
    lat: float = Field(ge=BOUNDS[1], le=BOUNDS[3])
    lon: float = Field(ge=BOUNDS[0], le=BOUNDS[2])


class FeatureRequest(Coordinates):
    tags: dict[str, str | list[str] | bool]
    radius_m: int = Field(default=1000, ge=1, description='Search radius in meters; capped at 15000.')
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator('radius_m')
    @classmethod
    def cap_radius(cls, value: int) -> int:
        return min(value, MAX_SEARCH_RADIUS_M)


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
    return {"status": "ok", "region": "Kazan agglomeration", "data_version": DATA_VERSION, "bbox": BOUNDS, "overpass_url": OVERPASS_URL}


@app.post("/features")
def features(request: FeatureRequest):
    try:
        with LOCK:
            return search_osm.invoke(request.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Geodata unavailable: {type(exc).__name__}: {exc}") from exc


@app.post("/route")
def walking_route(request: RouteRequest):
    return street_route(request, 'walk')


@app.post('/drive/route')
def driving_route(request: RouteRequest):
    return street_route(request, 'drive')


# Fallbacks are modelling assumptions, not legal speed limits.
DRIVE_SPEEDS = {'motorway':80, 'motorway_link':40, 'trunk':60, 'trunk_link':35,
                'primary':40, 'primary_link':30, 'secondary':35, 'secondary_link':25,
                'tertiary':30, 'tertiary_link':20, 'residential':20,
                'unclassified':25, 'living_street':10, 'service':15}


def route_result(graph, origin, destination, mode, start_offset, end_offset):
    weight = 'travel_time' if mode == 'drive' else 'length'
    route = nx.shortest_path(graph, origin, destination, weight=weight)
    edges = [min(graph[u][v].values(), key=lambda edge: edge[weight]) for u,v in zip(route,route[1:])]
    length = sum(edge['length'] for edge in edges)
    minutes = sum(edge['travel_time'] for edge in edges) / 60 if mode == 'drive' else length / 75
    geometry = []
    for (u,v),edge in zip(zip(route,route[1:]),edges):
        line = edge.get('geometry')
        points = list(line.coords) if line is not None else [(graph.nodes[u]['x'],graph.nodes[u]['y']), (graph.nodes[v]['x'],graph.nodes[v]['y'])]
        u_point = Point(graph.nodes[u]['x'],graph.nodes[u]['y'])
        if Point(points[-1]).distance(u_point) < Point(points[0]).distance(u_point):
            points.reverse()
        geometry.extend(points if not geometry else points[1:])
    result = {
        'mode': 'CAR' if mode == 'drive' else 'WALK',
        'length_m':round(length,1), 'estimated_minutes':round(minutes,1),
        'start_snap_distance_m':round(start_offset,1), 'end_snap_distance_m':round(end_offset,1),
        'geometry':{'type':'LineString','coordinates':geometry} if geometry else None,
        'note':'Route between nearest graph nodes; endpoint connectors excluded. Graph is limited to the local bbox; a route outside it may be better.',
    }
    if mode == 'drive':
        result.update({
            'traffic_included':False, 'turn_restrictions_included':False,
            'speed_model':'OSM maxspeed where available, otherwise assumed speeds by road class.',
            'fallback_speeds_kmh':DRIVE_SPEEDS, 'fallback_speed_kmh':30,
            'limitations':[
                'Estimated free-flow time: no live traffic, signal delays, parking or temporary closures.',
                'OSM drive network respects mapped one-way streets and its access filter; turn-restriction relations and conditional restrictions are not applied. Not turn-by-turn navigation.',
                'Endpoint offsets are not driving instructions or guaranteed car access to the destination.',
            ],
        })
    else:
        result['assumed_speed_kmh'] = 4.5
        result['note'] += ' No wheelchair-accessibility guarantee.'
    return result


def street_route(request: RouteRequest, mode: str):
    start, end = request.start, request.end
    distance = ox.distance.great_circle(start.lat, start.lon, end.lat, end.lon)
    maximum = MAX_DRIVE_DISTANCE_M if mode == 'drive' else MAX_WALK_DISTANCE_M
    if distance > maximum:
        raise HTTPException(422, f'Maximum straight-line distance between endpoints is {maximum // 1000} km')
    lon_padding,lat_padding = (0.05,0.035) if mode == 'drive' else (0.02,0.015)
    bbox = (
        max(BOUNDS[0], min(start.lon, end.lon) - lon_padding),
        max(BOUNDS[1], min(start.lat, end.lat) - lat_padding),
        min(BOUNDS[2], max(start.lon, end.lon) + lon_padding),
        min(BOUNDS[3], max(start.lat, end.lat) + lat_padding),
    )
    if mode == 'drive':
        # Regional detours and Volga crossings must not be cut by endpoint padding.
        bbox = BOUNDS

    def compute():
        key = hashlib.sha256(json.dumps([DATA_VERSION, OVERPASS_URL, bbox, mode]).encode()).hexdigest()
        folder = CACHE_DIR / "graphs"
        folder.mkdir(parents=True, exist_ok=True)
        graph_path = folder / f"{key}.graphml"
        if graph_path.exists():
            graph = ox.load_graphml(graph_path)
        else:
            graph = ox.graph_from_bbox(bbox, network_type=mode, retain_all=True)
            ox.save_graphml(graph, graph_path)
        if mode == 'drive':
            graph = ox.routing.add_edge_speeds(graph, hwy_speeds=DRIVE_SPEEDS, fallback=30)
            graph = ox.routing.add_edge_travel_times(graph)
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
                raise HTTPException(422, f'No {mode} graph node within 300 m of an endpoint')
            return node, offset

        origin, start_offset = snap(start)
        destination, end_offset = snap(end)
        try:
            return route_result(graph, origin, destination, mode, start_offset, end_offset)
        except nx.NetworkXNoPath as exc:
            raise HTTPException(404, f'No {mode} path found within the downloaded graph') from exc

    try:
        with LOCK:
            params = [OVERPASS_URL, request.model_dump(), bbox]
            if mode == 'drive':
                params += [mode, 'free-flow-v1', DRIVE_SPEEDS]
            return cached_result('driving-routes' if mode == 'drive' else 'routes', params, compute)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Geodata unavailable: {type(exc).__name__}: {exc}") from exc
