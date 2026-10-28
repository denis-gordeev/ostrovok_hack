"""Generic OSM tools. All geographic HTTP responses are cached on disk."""

import hashlib
import json
import os
import tempfile
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from datetime import datetime, timezone
from pathlib import Path

import osmnx as ox
import requests
import requests_cache
from dotenv import load_dotenv
from langchain_core.tools import tool
from shapely.geometry import Point, shape

from map_tools import collection, decode_polyline, feature, map_result, read_geo_result, update_map
from wiki_retrieval import wikipedia_search, wikipedia_article
from region_config import BOUNDS, DATA_VERSION, MAX_WALK_DISTANCE_M, MAX_SEARCH_RADIUS_M
from retry_utils import geo_post, retry_remote_call

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
    str(CACHE_DIR / f"http-{DATA_VERSION}"),
    backend="sqlite",
    allowable_methods=("GET", "POST"),
    expire_after=-1,
    filter_fn=cacheable_response,
)
ox.settings.use_cache = True
ox.settings.cache_folder = str(CACHE_DIR / 'osmnx' / DATA_VERSION)
ox.settings.overpass_url = OVERPASS_URL
ox.settings.requests_timeout = TIMEOUT
ox.settings.overpass_rate_limit = os.getenv("OVERPASS_RATE_LIMIT", "true").lower() == "true"


def cached_result(kind, params, compute):
    """Persist successful derived results, including the original fetch date."""
    key = hashlib.sha256(json.dumps([DATA_VERSION, params], sort_keys=True).encode()).hexdigest()
    folder = CACHE_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.json"
    if path.exists():
        return {**json.loads(path.read_text()), "cache_hit": True}
    result = {
        **compute(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": DATA_VERSION,
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
    if not 1 <= radius_m <= MAX_SEARCH_RADIUS_M:
        raise ValueError(f"radius_m must be between 1 and {MAX_SEARCH_RADIUS_M}")


def error_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}. No geographic conclusion can be drawn."


@tool(response_format='content_and_artifact')
def overpass_query(query: str, limit: int = 30) -> dict:
    """Run a read-only Overpass QL query. Use [out:json][timeout:60]; and
    out center tags; for geometry centers, or out body; for full OSM fields.
    Always restrict queries to a small bbox or around radius in Kazan.
    Bbox syntax is (south,west,north,east), e.g. (55.30,48.20,56.35,49.85).
    These are positional numbers; never write south= or west= inside a bbox.
    Example: [out:json][timeout:60];nwr(around:500,55.7961,49.1064)[leisure=park];out center tags;
    Returns OSM elements, total count, truncation flag, source and fetch date.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if re.search(r'\(\s*(?:south|west|north|east)\s*=\s*-?\d', query):
        raise ValueError('Invalid Overpass bbox: use positional numbers (south,west,north,east), '
                         'for example (55.30,48.20,56.35,49.85), without parameter names or = signs.')

    def fetch():
        response = geo_post(
            f"{OVERPASS_URL}/interpreter", data={"data": query}, timeout=TIMEOUT,
            headers={"User-Agent": "ostrovok-geo-agent/0.1 (OSM research prototype)"},
            max_attempts=1,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("remark") and any(word in data['remark'].lower() for word in ('timed out', 'timeout')):
            raise requests.Timeout('Overpass query timed out; incomplete results discarded')
        if data.get("remark"):
            raise ValueError(f"Overpass returned incomplete data: {data['remark']}")
        if "elements" not in data:
            raise ValueError("Expected JSON with elements; use [out:json]")
        return {"elements": data["elements"], "osm_base": data.get("osm3s", {}).get("timestamp_osm_base")}

    result = cached_result("overpass", [OVERPASS_URL, query],
                           lambda: retry_remote_call(fetch, max_attempts=int(os.getenv("OVERPASS_MAX_ATTEMPTS", "6"))))
    elements = result.pop("elements")
    shown = elements[:limit]
    points = []
    for element in shown:
        center = element if 'lat' in element else element.get('center')
        if center:
            points.append(feature({'type': 'Point', 'coordinates': [center['lon'], center['lat']]},
                                  name=element.get('tags', {}).get('name', 'Объект без названия'),
                                  location_kind='point' if 'lat' in element else 'osm_center',
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
    radius_m must be positive; values above 15000 meters are capped at 15000.
    Missing OSM features do not prove absence in reality.
    """
    radius_m = min(radius_m, MAX_SEARCH_RADIUS_M)
    validate_point(lat, lon, radius_m)
    if not tags or not 1 <= limit <= 100:
        raise ValueError("Provide tags and limit between 1 and 100")

    def fetch():
        try:
            features = retry_remote_call(lambda: ox.features_from_point((lat, lon), tags, dist=radius_m))
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
                       "total": len(features), "truncated": len(features) > limit,
                       "radius_m": radius_m}, {'default': data})


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
    return _street_route('/route', 'Пеший маршрут', 'WALK', start_lat, start_lon, end_lat, end_lon)


@tool(response_format='content_and_artifact')
def driving_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict:
    """Find a car route in Kazan on the directed OSMnx drive network.
    Use known user/OSM coordinates; endpoints must be within the Kazan bbox and
    at most 150 km apart in a straight line. Includes Sviyazhsk village/station,
    Innopolis and Zelenodolsk. Minimizes estimated free-flow time
    using OSM maxspeed or assumed speeds by road class. NO live traffic, signal
    delays, parking, turn-restriction relations or conditional restrictions.
    This is a geographic prototype, not turn-by-turn driving navigation.
    Endpoint snap offsets (max 300 m) are excluded and do not guarantee car access.
    Returns length_m, estimated_minutes, limitations and data_ref for update_map.
    Always disclose estimated time without traffic; do not claim all turns legal.
    Endpoint street/district names and parking are NOT returned or verified.
    Do not infer them from coordinates: describe travel between the given points.
    """
    return _street_route('/drive/route', 'Автомобильный маршрут', 'CAR', start_lat, start_lon, end_lat, end_lon)


def _street_route(path, title, mode, start_lat, start_lon, end_lat, end_lon):
    if mode == 'WALK' and ox.distance.great_circle(start_lat,start_lon,end_lat,end_lon) > MAX_WALK_DISTANCE_M:
        raise ValueError('Walking route over 10 km is outside prototype limits; use driving/transit for regional trips.')
    response = geo_post(
        f"{OSMNX_API_URL}{path}",
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
        *([feature(geometry, name=title, mode=mode)] if geometry else []),
        feature({'type':'Point', 'coordinates':[start_lon,start_lat]}, name='Начало маршрута'),
        feature({'type':'Point', 'coordinates':[end_lon,end_lat]}, name='Конец маршрута'),
    ])})


@tool(response_format='content_and_artifact')
def transit_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float,
                  departure_time: str | None = None) -> dict:
    """Plan public transport, regional rail and walking in the Kazan agglomeration.
    Coordinates must come from the user or OSM. Optional departure_time is ISO
    8601 with timezone, e.g. 2026-10-27T09:00:00+03:00; omit for now.
    Returns routes, boarding/alighting stops, times and transfers. This is a
    MODELLED prototype: source departures plus ESTIMATED intermediate/final
    times, incomplete coverage, no public-holiday calendar or realtime data.
    Always state these limits. If contains_transit=false, it is a walking-only
    alternative, not public transport. Empty results do not prove no service.
    Regional 106/108/407 use published endpoint schedules, without intermediate
    boarding. Their geometry is road-modelled; 407 operating days are unconfirmed.
    Always explain any returned per-leg limitations and cite the route source URL.
    """
    response = geo_post(f'{OSMNX_API_URL}/transit/route', json={
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


@tool(response_format='content_and_artifact')
def plan_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float,
               modes: list[Literal['walk', 'car', 'transit']] | None = None,
               departure_time: str | None = None) -> dict:
    """Compare routes between verified user/OSM coordinates in Kazan region.
    Omit modes for automatic walk+transit (car+transit beyond 10 km straight-line).
    For an explicit user choice pass only the requested modes: walk, car, transit.
    departure_time is optional ISO 8601 with timezone; omit for now.
    Returns independent results/errors by mode, primary_mode and primary_variant.
    Walking is primary only below 1000 m path length; transit must contain actual
    transit and is ranked by total_minutes_from_requested_departure including wait.
    Use returned map_variants with update_map; geometry stays in the artifact.
    Preserve all per-mode/per-leg sources and limitations: car time excludes traffic
    and turn restrictions; transit is modelled, incomplete, no realtime/holidays,
    and bus 407 operating days are unconfirmed. Failure does not prove no service.
    """
    selected = list(dict.fromkeys(modes if modes is not None else
        (['car', 'transit'] if ox.distance.great_circle(
            start_lat, start_lon, end_lat, end_lon) > MAX_WALK_DISTANCE_M
         else ['walk', 'transit'])))
    if not selected or any(mode not in ('walk', 'car', 'transit') for mode in selected):
        raise ValueError('Provide at least one valid route mode')
    args = dict(start_lat=start_lat, start_lon=start_lon, end_lat=end_lat, end_lon=end_lon)

    def calculate(mode):
        try:
            tool = {'walk': walking_route, 'car': driving_route, 'transit': transit_route}[mode]
            content, artifact = tool.func(**args, **({'departure_time': departure_time} if mode == 'transit' else {}))
            return content, artifact
        except Exception as exc:
            return {'error': error_message(exc)}, None

    results, variants = {}, {}
    with ThreadPoolExecutor(max_workers=min(5, len(selected))) as executor:
        for mode, (content, artifact) in zip(selected, executor.map(calculate, selected)):
            content = {k: v for k, v in content.items()
                       if k not in ('data_ref', 'map_variants', 'map_feature_count')}
            results[mode] = content
            if artifact:
                variants.update({f'{mode}-{key}': value for key, value in artifact['collections'].items()})
    transit = [(i, item) for i, item in enumerate(results.get('transit', {}).get('itineraries', []), 1)
               if item.get('contains_transit') is True]
    best = min(transit, key=lambda pair: pair[1]['total_minutes_from_requested_departure']) if transit else None
    walk = results.get('walk', {})
    primary = None
    if len(selected) == 1:
        mode = selected[0]
        if mode == 'transit':
            primary = 'transit' if best else None
        elif 'error' not in results[mode]:
            primary = mode
    elif walk.get('length_m', float('inf')) < 1000:
        primary = 'walk'
    elif best:
        primary = 'transit'
    elif 'car' in results and 'error' not in results['car']:
        primary = 'car'
    elif 'length_m' in walk:
        primary = 'walk'
    primary_variant = (f'transit-option-{best[0]}' if primary == 'transit'
                       else f'{primary}-default' if primary else None)
    return map_result({'results': results, 'primary_mode': primary,
                       'primary_variant': primary_variant,
                       'transit_available': bool(best),
                       'best_transit_variant': f'transit-option-{best[0]}' if best else None}, variants)


TOOLS = [overpass_query, search_osm, plan_route, wikipedia_search, wikipedia_article, read_geo_result, update_map]
