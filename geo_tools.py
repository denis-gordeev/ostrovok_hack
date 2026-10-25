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
from shapely.geometry import Point

load_dotenv()
CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache")).resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api").rstrip("/")
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))

def cacheable_response(response):
    url = response.url.split("?")[0]
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


@tool
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
    return {**result, "elements": elements[:limit], "total": len(elements), "truncated": len(elements) > limit}


@tool
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
                "tags": {k: v for k, v in item["properties"].items() if v is not None},
            })
        return {"features": sorted(rows, key=lambda r: r["distance_m"])}

    result = cached_result("features", [OVERPASS_URL, lat, lon, tags, radius_m], fetch)
    features = result.pop("features")
    return {**result, "features": features[:limit], "total": len(features), "truncated": len(features) > limit}


TOOLS = [overpass_query, search_osm]
