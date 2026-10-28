"""Same-origin web UI and a disk cache for on-demand map/terrain tiles."""

import os
from pathlib import Path

import requests_cache
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI()
tile_cache = requests_cache.CachedSession(
    str(Path(os.getenv("CACHE_DIR", ".cache")) / "map-tiles"),
    backend="sqlite",
    expire_after=7 * 86400,
    cache_control=True,
    allowable_methods=("GET",),
    allowable_codes=(200,),
)


@app.get("/")
def home():
    return RedirectResponse("/ui/")


@app.get("/ui/tiles/{kind}/{z}/{x}/{y}.png")
def tile(kind: str, z: int, x: int, y: int):
    if kind not in ("osm", "terrain") or not 0 <= z <= (19 if kind == "osm" else 15):
        raise HTTPException(404)
    if not (0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404)
    template = (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        if kind == "osm"
        else "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    )
    try:
        response = tile_cache.get(
            template.format(z=z, x=x, y=y),
            timeout=20,
            headers={
                "User-Agent": "ostrovok-kazan-map/0.1 (+https://188.241.197.121:8443/ui/)",
                "Referer": "https://188.241.197.121:8443/ui/",
            },
        )
        response.raise_for_status()
        return Response(
            response.content,
            media_type="image/png",
            headers={"Cache-Control": response.headers.get("Cache-Control", "public, max-age=604800")},
        )
    except Exception as exc:
        raise HTTPException(502, "Не удалось загрузить тайл карты") from exc


app.mount(
    "/ui", StaticFiles(directory=Path(__file__).parent / "web" / "dist", html=True, check_dir=False), name="map-ui"
)


@app.get("/knowledge/status")
def knowledge_status():
    import json
    from wiki_store import connect, metadata

    folder = Path(
        os.getenv("WIKIPEDIA_DATA_DIR", str(Path(os.getenv("CACHE_DIR", ".cache")) / "wikipedia" / "ruwiki-20261001"))
    )
    result = {"corpus_ready": (folder / "corpus.sqlite").exists(), "semantic_ready": False}
    for filename, key in [("status.json", "corpus_build"), ("embedding-status.json", "embedding_build")]:
        path = folder / filename
        if path.exists():
            result[key] = json.loads(path.read_text())
    result["semantic_ready"] = (folder / "index.json").exists() and result.get("embedding_build", {}).get(
        "phase"
    ) == "complete"
    if result["corpus_ready"]:
        with connect(folder / "corpus.sqlite", readonly=True) as con:
            info = metadata(con)
        result.update({k: info.get(k) for k in ("snapshot", "counts", "build_status")})
    return result
