"""Общие фикстуры сервиса шума: снимок и сетка строятся один раз на сессию."""

import pytest
from noise_helpers import kazan_snapshot

from kazan_noise.config import NoiseConfig
from kazan_noise.grid import compute_grid, load_grid, save_grid
from kazan_noise.service import NoiseService


@pytest.fixture(scope="session")
def service_dir(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("kazan_noise_service")
    kazan_snapshot(data_dir)
    cfg = NoiseConfig(_env_file=None, data_dir=data_dir)
    save_grid(compute_grid(cfg.features_path, cfg, workers=1), cfg.grid_path)
    return data_dir


@pytest.fixture(scope="session")
def service(service_dir):
    return NoiseService.load(service_dir)


@pytest.fixture(scope="session")
def service_grid(service_dir):
    return load_grid(service_dir / "grid.npz")
