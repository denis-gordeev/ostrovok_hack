from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

DAY = "day"
NIGHT = "night"
# Режимы суток: день 07-23, ночь 23-07.
MODES = (DAY, NIGHT)
WGS84 = "EPSG:4326"
# UTM zone 39N: метрическая проекция для Казани.
UTM = "EPSG:32639"


class NoiseConfig(BaseSettings):
    """Настройки модели шума Казани. Читаются из KAZAN_NOISE_* и файла .env."""

    model_config = SettingsConfigDict(
        env_prefix="KAZAN_NOISE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Path("data/kazan_noise")
    # Зеркала Overpass по порядку: при отказе берётся следующее.
    overpass_urls: tuple[str, ...] = (
        "https://overpass-api.de/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )
    user_agent: str = "hotel-agent-kazan-noise/0.1"
    # Пауза между запросами к Overpass.
    min_interval_s: float = 10.0
    retry_delays_s: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)
    timeout_s: float = 400.0
    # Relation границы Казани в OSM.
    boundary_relation: int = 367666
    # Запас вокруг города для источников шума, м.
    source_buffer_m: float = 3000.0
    # Высота приёмника, м (как в карте шума ЕС).
    receiver_height_m: float = 4.0
    h3_res: int = 10

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def features_path(self) -> Path:
        return self.data_dir / "features.npz"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grid_path(self) -> Path:
        return self.data_dir / "grid.npz"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def meta_path(self) -> Path:
        return self.data_dir / "meta.json"
