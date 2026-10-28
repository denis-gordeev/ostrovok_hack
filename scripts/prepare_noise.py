"""Build the full Kazan noise scale from our static, area-less Overpass.
Run inside the app image with CACHE_DIR mounted; raw responses are resumable.
No object revision timestamps: stale construction cannot be removed by age.
"""

import gzip, json, os
from pathlib import Path
import httpx
from kazan_noise import queries
from kazan_noise.features import build_features
from kazan_noise.snapshot import save_features, write_meta
from kazan_noise.config import NoiseConfig
from kazan_noise.grid import compute_grid, save_grid
from datetime import date

folder = Path(os.getenv("KAZAN_NOISE_DATA_DIR", "/app/.cache/kazan_noise-v1"))
raw = folder / "raw"
raw.mkdir(parents=True, exist_ok=True)


def query(name, ql):
    path = raw / (name + ".json.gz")
    if path.exists():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    with httpx.Client(timeout=400, trust_env=False) as c:
        r = c.post(os.getenv("NOISE_OVERPASS_URL", "http://overpass/api/interpreter"), data={"data": ql})
        r.raise_for_status()
        d = r.json()
    if d.get("remark"):
        raise RuntimeError(d["remark"])
    if name in ("boundary", "buildings") and not d.get("elements"):
        raise RuntimeError("Empty " + name)
    with gzip.open(path, "wt") as f:
        json.dump(d, f)
    print(name, len(d["elements"]), flush=True)
    return d


b = query("boundary", queries.boundary_query(367666))
bounds = next(e["bounds"] for e in b["elements"] if "bounds" in e)
bbox = "(" + ",".join(str(bounds[k]) for k in ("minlat", "minlon", "maxlat", "maxlon")) + ")"
ql = queries.buildings_query(367666)
ql = "\n".join(l for l in ql.splitlines() if not l.startswith("area(")).replace("(area.city)", bbox)
all_raw = {"boundary": b, "buildings": query("buildings", ql)}
for name in ("lines", "areas"):
    q = getattr(queries, name + "_query")(queries.bbox_with_buffer(bounds, 3000)).replace("out meta geom", "out geom")
    all_raw[name] = query(name, q)
features, report = build_features(all_raw, today=date.today())
save_features(features, folder / "features.npz")
write_meta(folder / "meta.json", features, report)
print("features", dict(report.counts), flush=True)
cfg = NoiseConfig(data_dir=folder)
print("grid started", flush=True)
grid = compute_grid(cfg.features_path, cfg, workers=1)
save_grid(grid, cfg.grid_path)
print("complete", len(grid.cells), int(grid.built_up.sum()), flush=True)

import hashlib

manifest = {
    "source": os.getenv("NOISE_OVERPASS_URL", "http://overpass/api/interpreter"),
    "osm_timestamp": features.osm_timestamp,
    "h3_resolution": cfg.h3_res,
    "cells": len(grid.cells),
    "built_up_cells": int(grid.built_up.sum()),
    "limitations": [
        "No object revision timestamps; construction age is unknown.",
        "Buildings fetched by boundary bbox; receivers restricted to Kazan.",
    ],
    "sha256": {},
}
for path in [*raw.glob("*.json.gz"), cfg.features_path, cfg.grid_path]:
    manifest["sha256"][str(path.relative_to(folder))] = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
(folder / "provenance.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
