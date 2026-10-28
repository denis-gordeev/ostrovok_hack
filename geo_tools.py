"""Generic OSM tools. All geographic HTTP responses are cached on disk."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import osmnx as ox
import requests
import requests_cache
from dotenv import load_dotenv
from langchain_core.tools import tool
from shapely.geometry import Point, shape

from map_tools import collection, decode_polyline, feature, map_result, update_map

load_dotenv()
CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache")).resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api").rstrip("/")
OSMNX_API_URL = os.getenv("OSMNX_API_URL", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))

def cacheable_response(response):
    url = response.url.split("?")[0]
    if url.endswith('/transit/route'):
        # The transit service resolves departure time and owns its versioned cache.
        return False
    # Rate-limit status is transient control data, never a reusable geo result.
    if url.endswith("/status"):
        return False
    if url.endswith("/interpreter"):
        try:
            data = response.json()
            return "elements" in data and not data.get("remark")
        except ValueError:
            return False
    return True


# Includes GET/POST responses (Overpass, OSMnx and any requests-based tile
# downloads). Never caches LLM requests: langchain-openai uses httpx.
requests_cache.install_cache(
    str(CACHE_DIR / "http"),
    backend="sqlite",
    allowable_methods=("GET", "POST"),
    expire_after=-1,
    filter_fn=cacheable_response,
)
ox.settings.use_cache = True
ox.settings.cache_folder = str(CACHE_DIR / "osmnx")
ox.settings.overpass_url = OVERPASS_URL
ox.settings.requests_timeout = TIMEOUT
ox.settings.overpass_rate_limit = os.getenv("OVERPASS_RATE_LIMIT", "true").lower() == "true"


def cached_result(kind, params, compute):
    """Persist successful derived results, including the original fetch date."""
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    folder = CACHE_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.json"
    if path.exists():
        return {**json.loads(path.read_text()), "cache_hit": True}
    result = {
        **compute(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap / Overpass API",
        "endpoint": OVERPASS_URL,
    }
    # Atomic replacement: readers cannot observe partially written JSON.
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as f:
        json.dump(result, f, ensure_ascii=False)
        temp = Path(f.name)
    temp.replace(path)
    return {**result, "cache_hit": False}


def validate_point(lat, lon, radius_m):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid latitude/longitude")
    if not 1 <= radius_m <= 5000:
        raise ValueError("radius_m must be between 1 and 5000")


def error_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}. No geographic conclusion can be drawn."


@tool(response_format='content_and_artifact')
def overpass_query(query: str, limit: int = 30) -> dict:
    """Run a read-only Overpass QL query. Use [out:json][timeout:60]; and
    out center tags; for geometry centers, or out body; for full OSM fields.
    Always restrict queries to a small bbox or around radius in Kazan.
    Example: [out:json][timeout:60];nwr(around:500,55.7961,49.1064)[leisure=park];out center tags;
    Returns OSM elements, total count, truncation flag, source and fetch date.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    def fetch():
        response = requests.post(
            f"{OVERPASS_URL}/interpreter", data={"data": query}, timeout=TIMEOUT,
            headers={"User-Agent": "ostrovok-geo-agent/0.1 (OSM research prototype)"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("remark"):
            raise ValueError(f"Overpass returned incomplete data: {data['remark']}")
        if "elements" not in data:
            raise ValueError("Expected JSON with elements; use [out:json]")
        return {"elements": data["elements"], "osm_base": data.get("osm3s", {}).get("timestamp_osm_base")}

    result = cached_result("overpass", [OVERPASS_URL, query], fetch)
    elements = result.pop("elements")
    shown = elements[:limit]
    points = []
    for element in shown:
        center = element if 'lat' in element else element.get('center')
        if center:
            points.append(feature({'type': 'Point', 'coordinates': [center['lon'], center['lat']]},
                                  name=element.get('tags', {}).get('name', 'Объект без названия'),
                                  url=f"https://www.openstreetmap.org/{element['type']}/{element['id']}"))
    return map_result({**result, "elements": shown, "total": len(elements), "truncated": len(elements) > limit},
                      {'default': collection(points)})


@tool(response_format='content_and_artifact')
def search_osm(
    lat: float, lon: float, tags: dict[str, str | list[str] | bool],
    radius_m: int = 1000, limit: int = 30,
) -> dict:
    """Find OSM features around coordinates using OSMnx, sorted by straight-line
    distance to their geometry. tags examples: {"leisure":"park"},
    {"highway":["primary","secondary"]}, {"amenity":"pharmacy"}.
    Multiple tag keys use OR, not AND. Search is a square bbox with radius_m
    half-side, then filtered to the radius. Distances are NOT walking routes.
    Missing OSM features do not prove absence in reality.
    """
    validate_point(lat, lon, radius_m)
    if not tags or not 1 <= limit <= 100:
        raise ValueError("Provide tags and limit between 1 and 100")

    def fetch():
        try:
            features = ox.features_from_point((lat, lon), tags, dist=radius_m)
        except ox._errors.InsufficientResponseError:
            return {"features": [], "coverage_note": "No matching data returned by OSM."}
        projected = ox.projection.project_gdf(features)
        point = ox.projection.project_geometry(Point(lon, lat), to_crs=projected.crs)[0]
        distances = projected.geometry.distance(point)
        rows = []
        # JSON serialization handles NaN/list fields without leaking pandas types.
        geojson = json.loads(features.to_json())
        for (element_type, osm_id), item, distance in zip(features.index, geojson["features"], distances):
            if distance > radius_m:
                continue
            rows.append({
                "osm_type": element_type, "osm_id": int(osm_id),
                "url": f"https://www.openstreetmap.org/{element_type}/{osm_id}",
                "distance_m": round(float(distance), 1),
                "geometry": item['geometry'],
                "center": list(shape(item['geometry']).representative_point().coords)[0],
                "tags": {k: v for k, v in item["properties"].items() if v is not None},
            })
        return {"features": sorted(rows, key=lambda r: r["distance_m"])}

    result = cached_result("features", [OVERPASS_URL, lat, lon, tags, radius_m, 'map-v1'], fetch)
    features = result.pop("features")
    shown = features[:limit]
    data = collection([feature(row['geometry'], name=row['tags'].get('name', 'Объект без названия'),
                               center=row['center'], url=row['url'], distance_m=row['distance_m']) for row in shown])
    return map_result({**result, "features": [{k:v for k,v in row.items() if k != 'geometry'} for row in shown],
                       "total": len(features), "truncated": len(features) > limit}, {'default': data})


@tool(response_format='content_and_artifact')
def walking_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict:
    """Find a walking route between known coordinates in Kazan via the OSMnx API.
    Endpoints must be within the Kazan bbox and at most 10 km apart in a straight
    line. Returns route length in meters, estimated minutes at 4.5 km/h, endpoint
    snap distances, source and fetch date. Endpoint connectors are excluded from
    the length. No wheelchair-accessibility guarantee. Geometry is omitted from
    the tool response; POST /route returns full GeoJSON for map clients.
    Use coordinates provided by the user or obtained from OSM, never invent them.
    """
    response = requests.post(
        f"{OSMNX_API_URL}/route",
        json={
            "start": {"lat": start_lat, "lon": start_lon},
            "end": {"lat": end_lat, "lon": end_lon},
        },
        timeout=TIMEOUT,
    )
    if response.status_code in (404, 422, 503):
        raise ValueError(f"OSMnx route API ({response.status_code}): {response.json().get('detail')}")
    response.raise_for_status()
    result = response.json()
    geometry = result.pop("geometry", None)
    result["http_cache_hit"] = bool(getattr(response, "from_cache", False))
    return map_result(result, {'default': collection([
        *([feature(geometry, name='Пеший маршрут', mode='WALK')] if geometry else []),
        feature({'type':'Point', 'coordinates':[start_lon,start_lat]}, name='Начало маршрута'),
        feature({'type':'Point', 'coordinates':[end_lon,end_lat]}, name='Конец маршрута'),
    ])})


@tool(response_format='content_and_artifact')
def transit_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float,
                  departure_time: str | None = None) -> dict:
    """Plan public transport and walking in Kazan with local OpenTripPlanner.
    Coordinates must come from the user or OSM. Optional departure_time is ISO
    8601 with timezone, e.g. 2026-10-27T09:00:00+03:00; omit for now.
    Returns routes, boarding/alighting stops, times and transfers. This is a
    MODELLED prototype: source departures plus ESTIMATED intermediate/final
    times, incomplete coverage, no public-holiday calendar or realtime data.
    Always state these limits. If contains_transit=false, it is a walking-only
    alternative, not public transport. Empty results do not prove no service.
    """
    response = requests.post(f'{OSMNX_API_URL}/transit/route', json={
        'start':{'lat':start_lat,'lon':start_lon},
        'end':{'lat':end_lat,'lon':end_lon},'departure_time':departure_time,
    },timeout=TIMEOUT)
    if response.status_code in (404,422,503):
        raise ValueError(f'Transit API ({response.status_code}): {response.json().get("detail")}')
    response.raise_for_status()
    result = response.json()
    variants = {}
    for index, itinerary in enumerate(result.get('itineraries',[]), 1):
        features = []
        for leg in itinerary['legs']:
            geometry = leg.pop('geometry',None)
            if geometry and geometry.get('points'):
                features.append(feature({'type':'LineString', 'coordinates':decode_polyline(geometry['points'])},
                                        name=f"{leg['from']['name']} → {leg['to']['name']}",
                                        mode=leg['mode'], route=leg['route']))
            for stop in (leg['from'], leg['to']):
                features.append(feature({'type':'Point', 'coordinates':[stop['lon'],stop['lat']]}, name=stop['name']))
        variants[f'option-{index}'] = collection(features)
    return map_result(result, variants)


TOOLS = [overpass_query, search_osm, walking_route, transit_route, update_map]
