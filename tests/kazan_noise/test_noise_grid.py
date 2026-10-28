import h3
import numpy as np
import pytest
from noise_helpers import kazan_snapshot
from shapely.geometry import box

from kazan_noise.config import DAY, NoiseConfig
from kazan_noise.grid import Grid, compute_grid, grid_cells, load_grid, save_grid
from kazan_noise.scale import Scale, label
from kazan_noise.snapshot import SnapshotMissingError


def test_score_is_share_of_strictly_quieter_cells():
    s = Scale(np.array([40.0, 50, 60, 70]), np.array([40.0, 50, 60, 70]))
    assert [s.score(v, DAY) for v in (30, 50, 55, 80)] == [0, 25, 50, 100]


@pytest.mark.parametrize(
    "score, text",
    [
        (0, "очень тихо"),
        (19, "очень тихо"),
        (20, "тихо"),
        (59, "средне"),
        (60, "шумно"),
        (80, "очень шумно"),
        (100, "очень шумно"),
    ],
)
def test_labels(score, text):
    assert label(score) == text


def _grid(built_up, day, night):
    n = len(built_up)
    cells, stamp = np.array(list("abcdef")[:n]), "2026-10-26T18:41:36Z"
    return Grid(cells, np.zeros(n), np.zeros(n), np.array(built_up), np.array(day), np.array(night), stamp)


def test_scale_ignores_cells_that_are_not_built_up():
    g = _grid([True, False], [50.0, 10], [40.0, 5])
    assert Scale.from_grid(g).score(20, DAY) == 0


def test_scale_without_built_up_cells_raises():
    with pytest.raises(ValueError):
        Scale.from_grid(_grid([False, False], [50.0, 10], [40.0, 5]))


def test_grid_cells_cover_1_km2_at_res_10():
    cells = grid_cells(box(49.10, 55.79, 49.116, 55.799), 10)  # ≈1 км²
    assert 50 <= len(cells) <= 80 and {h3.get_resolution(c) for c in cells} == {10}
    assert cells == sorted(cells)


def test_compute_grid_marks_built_up_and_round_trips(tmp_path):
    kazan_snapshot(tmp_path)
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    grid = compute_grid(cfg.features_path, cfg, workers=1, limit=40)
    assert len(grid.cells) == 40 and grid.built_up.any() and not grid.built_up.all()
    assert (grid.day_db >= 35.0).all()
    save_grid(grid, cfg.grid_path)
    loaded = load_grid(cfg.grid_path)
    assert loaded.cells.tolist() == grid.cells.tolist()
    assert loaded.osm_timestamp == grid.osm_timestamp == "2026-10-26T18:41:36Z"


def test_parallel_workers_match_in_process_result(tmp_path):
    kazan_snapshot(tmp_path)
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    one = compute_grid(cfg.features_path, cfg, workers=1, limit=300)
    two = compute_grid(cfg.features_path, cfg, workers=2, limit=300)
    assert one.cells.tolist() == two.cells.tolist()
    assert np.allclose(one.day_db, two.day_db) and (one.built_up == two.built_up).all()


def test_load_grid_missing_or_stale_asks_to_rebuild(tmp_path):
    with pytest.raises(SnapshotMissingError, match="запустите kazan-noise grid"):
        load_grid(tmp_path / "grid.npz")
    path = tmp_path / "grid.npz"
    g = _grid([True], [50.0], [40.0])
    save_grid(g, path)
    with np.load(path, allow_pickle=False) as z:
        data = dict(z)
    data["weights_hash"] = np.asarray("stale", dtype=np.str_)
    np.savez_compressed(path, **data)
    with pytest.raises(SnapshotMissingError, match="запустите kazan-noise grid"):
        load_grid(path)


def test_truncated_or_old_grid_asks_to_rebuild(tmp_path):
    path = tmp_path / "grid.npz"
    save_grid(_grid([True], [50.0], [40.0]), path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(SnapshotMissingError, match="повреждена.*запустите kazan-noise grid"):
        load_grid(path)
    # Сетка старого формата без osm_timestamp.
    path.write_bytes(data)
    with np.load(path, allow_pickle=False) as z:
        old = {k: z[k] for k in z.files if k != "osm_timestamp"}
    np.savez_compressed(path, **old)
    with pytest.raises(SnapshotMissingError, match="запустите kazan-noise grid"):
        load_grid(path)
