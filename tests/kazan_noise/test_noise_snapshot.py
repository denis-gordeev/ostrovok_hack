import json
from collections import Counter

import numpy as np
import pytest
import shapely
from noise_helpers import local_scene
from shapely.geometry import LineString, Point, box

from kazan_noise import snapshot
from kazan_noise.features import BuildReport, Source
from kazan_noise.snapshot import (FORMAT_VERSION, SnapshotMissingError,
                                  load_features, pack_geoms, read_meta,
                                  save_features, unpack_geoms, write_meta)


def test_features_round_trip(tmp_path):
    road = Source("road", "way/1", None, "primary", LineString([(-2000, 0), (2000, 0)]), 0.5, 80.0, 72.0, 1000)
    bar = Source("bar", "node/2", "Бар", "bar", Point(10, 20), 3.0, 60.0, 65.0, 300)
    f = local_scene(
        sources=[road, bar],
        buildings=[(box(0, 0, 10, 10), 9.0)],
        walls=[(LineString([(0, 0), (5, 5)]), 2.0)],
        green=[(box(50, 50, 90, 90), 0.5)],
    )
    save_features(f, tmp_path / "f.npz")
    g = load_features(tmp_path / "f.npz")
    assert shapely.equals_exact(g.obstacles.geoms, f.obstacles.geoms, 1e-6).all()
    assert (g.obstacles.heights == f.obstacles.heights).all()
    assert (g.obstacles.is_building == f.obstacles.is_building).all()
    assert shapely.equals_exact(g.green.geoms, f.green.geoms, 1e-6).all()
    assert (g.green.factors == f.green.factors).all()
    assert g.sources == f.sources and g.sources[0].name is None and g.sources[1].name == "Бар"
    assert g.boundary.equals_exact(f.boundary, 1e-6)
    assert g.boundary_wgs84.equals_exact(f.boundary_wgs84, 1e-6)
    assert g.osm_timestamp == f.osm_timestamp


def test_empty_layers_round_trip(tmp_path):
    save_features(local_scene(), tmp_path / "f.npz")
    g = load_features(tmp_path / "f.npz")
    assert len(g.obstacles.geoms) == 0 and len(g.green.geoms) == 0 and g.sources == []
    assert g.obstacles.heights.shape == (0,) and g.obstacles.is_building.dtype == bool


def test_pack_unpack_geoms():
    geoms = [box(0, 0, 1, 1), Point(3, 4)]
    buf, off = pack_geoms(geoms)
    assert buf.dtype == np.uint8 and off.dtype == np.int64 and len(off) == 3
    assert shapely.equals_exact(unpack_geoms(buf, off), np.asarray(geoms, dtype=object), 0).all()


def test_missing_snapshot(tmp_path):
    with pytest.raises(SnapshotMissingError, match="kazan-noise build"):
        load_features(tmp_path / "nope.npz")


def test_weights_changed_after_build(tmp_path, monkeypatch):
    save_features(local_scene(), tmp_path / "f.npz")
    monkeypatch.setattr(snapshot, "weights_hash", lambda: "другой")
    with pytest.raises(SnapshotMissingError, match="пересоберите"):
        load_features(tmp_path / "f.npz")


def test_format_version_mismatch(tmp_path, monkeypatch):
    save_features(local_scene(), tmp_path / "f.npz")
    monkeypatch.setattr(snapshot, "FORMAT_VERSION", FORMAT_VERSION + 1)
    with pytest.raises(SnapshotMissingError, match="пересоберите"):
        load_features(tmp_path / "f.npz")


def test_weights_hash_is_stable_hex16():
    h = snapshot.weights_hash()
    assert h == snapshot.weights_hash() and len(h) == 16 and int(h, 16) >= 0


def test_meta_round_trip(tmp_path):
    f = local_scene()
    report = BuildReport(counts=Counter({"green": 2}), skipped=Counter({"geometry": 1}))
    write_meta(tmp_path / "meta.json", f, report)
    meta = read_meta(tmp_path / "meta.json")
    assert meta["format_version"] == FORMAT_VERSION
    assert meta["osm_timestamp"] == f.osm_timestamp
    assert meta["weights_hash"] == snapshot.weights_hash()
    assert meta["counts"] == {"green": 2} and meta["skipped"] == {"geometry": 1}
    assert meta["built_at"].endswith("+00:00")
    assert json.loads((tmp_path / "meta.json").read_text(encoding="utf-8")) == meta


@pytest.mark.parametrize("keep", [0.5, 0.0])
def test_truncated_snapshot_asks_to_rebuild(tmp_path, keep):
    path = tmp_path / "f.npz"
    save_features(local_scene(), path)
    data = path.read_bytes()
    path.write_bytes(data[: int(len(data) * keep)])
    with pytest.raises(SnapshotMissingError, match="повреждён.*kazan-noise build"):
        load_features(path)


def test_failed_save_keeps_previous_files(tmp_path, monkeypatch):
    path = tmp_path / "f.npz"
    save_features(local_scene(), path)
    before = path.read_bytes()

    def broken_savez(file, **arrays):
        file.write(b"half")
        raise OSError("диск переполнен")

    monkeypatch.setattr(np, "savez_compressed", broken_savez)
    with pytest.raises(OSError, match="диск переполнен"):
        save_features(local_scene(sources=[]), path)
    assert path.read_bytes() == before and sorted(p.name for p in tmp_path.iterdir()) == ["f.npz"]
