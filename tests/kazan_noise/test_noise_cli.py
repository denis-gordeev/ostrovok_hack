import gzip
import json
import shutil

import pytest
from noise_helpers import NEAR_ROAD, osm_relation, osm_way, raw_response

import kazan_noise.cli as cli
from kazan_noise.cli import main
from kazan_noise.grid import load_grid
from kazan_noise.snapshot import load_features


def test_query_json(service_dir, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(service_dir))
    main(["query", "--lat", str(NEAR_ROAD[0]), "--lon", str(NEAR_ROAD[1]), "--json"])
    captured = capsys.readouterr()
    assert "model_level_day" in json.loads(captured.out)
    assert "загрузка снимка" in captured.err


def test_query_text(service_dir, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(service_dir))
    main(["query", "--lat", str(NEAR_ROAD[0]), "--lon", str(NEAR_ROAD[1])])
    assert "День:" in capsys.readouterr().out


def test_query_outside_kazan_exits_1(service_dir, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(service_dir))
    with pytest.raises(SystemExit) as exc:
        main(["query", "--lat", "55.0", "--lon", "49.0"])
    assert exc.value.code == 1 and "вне городского округа" in capsys.readouterr().err


def test_query_without_snapshot_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["query", "--lat", "55.79", "--lon", "49.11"])
    assert exc.value.code == 1 and "kazan-noise build" in capsys.readouterr().err


def test_build_without_raw_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path / "d"))
    with pytest.raises(SystemExit) as exc:
        main(["build"])
    assert exc.value.code == 1 and "kazan-noise fetch" in capsys.readouterr().err


def test_grid_limit_is_a_dry_run(service_dir, tmp_path, monkeypatch, capsys):
    shutil.copy(service_dir / "features.npz", tmp_path / "features.npz")
    shutil.copy(service_dir / "grid.npz", tmp_path / "grid.npz")
    before = (tmp_path / "grid.npz").read_bytes()
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    main(["grid", "--workers", "1", "--limit", "5"])
    out = capsys.readouterr().out
    assert "ячеек: 5" in out and "не сохранена" in out
    assert (tmp_path / "grid.npz").read_bytes() == before


def test_grid_command_saves_full_grid(service_dir, tmp_path, monkeypatch, capsys):
    shutil.copy(service_dir / "features.npz", tmp_path / "features.npz")
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    main(["grid", "--workers", "1"])
    assert "ячеек:" in capsys.readouterr().out
    assert len(load_grid(tmp_path / "grid.npz").cells) == len(load_grid(service_dir / "grid.npz").cells)


def _box(lon, lat, d=0.001):
    return [(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d), (lon, lat)]


def _tiny_raw() -> dict[str, dict]:
    """Крошечная сцена: граница, одно здание, одна primary-дорога."""
    return {
        "boundary": raw_response([osm_relation(1, [("outer", _box(49.0, 55.7, 0.2))], {"boundary": "administrative"})]),
        "buildings": raw_response([osm_way(10, _box(49.10, 55.75), {"building": "yes", "height": "12"})]),
        "lines": raw_response([osm_way(20, [(49.1, 55.8), (49.11, 55.8)], {"highway": "primary"})]),
        "areas": raw_response([]),
    }


def _write_raw(data_dir, raw: dict[str, dict]) -> None:
    raw_dir = data_dir / "raw"
    raw_dir.mkdir()
    for name, data in raw.items():
        with gzip.open(raw_dir / f"{name}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(data, f)


def test_build_happy_path(tmp_path, monkeypatch, capsys):
    _write_raw(tmp_path, _tiny_raw())
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    main(["build", "--today", "2026-10-27"])
    out = capsys.readouterr().out
    assert "counts" in out and "source:road" in out and "skipped" in out
    assert "дальше: uv run kazan-noise grid" in out
    assert (tmp_path / "meta.json").exists()
    features = load_features(tmp_path / "features.npz")
    assert int(features.obstacles.is_building.sum()) == 1
    assert any(s.kind == "road" for s in features.sources)


def test_build_with_bad_boundary_exits_1(tmp_path, monkeypatch, capsys):
    raw = _tiny_raw()
    # Граница без relation: только way.
    raw["boundary"] = raw_response([osm_way(1, _box(49.0, 55.7, 0.2), {"boundary": "administrative"})])
    _write_raw(tmp_path, raw)
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["build"])
    assert exc.value.code == 1 and "boundary" in capsys.readouterr().err
    assert not (tmp_path / "features.npz").exists()


def test_fetch_with_fake_client(tmp_path, monkeypatch, capsys):
    raw = _tiny_raw()

    class FakeClient:
        def __init__(self, cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def query(self, ql):
            # Порядок запросов фиксирован: boundary, buildings, lines, areas.
            return raw[next(names)]

    names = iter(raw)
    monkeypatch.setattr(cli, "OverpassClient", FakeClient)
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    main(["fetch"])
    assert "boundary" in capsys.readouterr().out
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == [
        "areas.json.gz",
        "boundary.json.gz",
        "buildings.json.gz",
        "lines.json.gz",
    ]
