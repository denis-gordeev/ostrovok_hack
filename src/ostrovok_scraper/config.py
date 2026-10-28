from datetime import date
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

KINDS = ("hotel", "hostel", "apart_hotel", "guesthouse", "sanatorium", "boutique", "resort", "mini_hotel")
# Числа гостей, для которых собираются тарифы помимо базового (2 гостя).
OTHER_GUESTS = (1, 3, 4, 5, 6, 7, 8)
MSK = ZoneInfo("Europe/Moscow")


class ScraperConfig(BaseSettings):
    """Настройки сбора данных ostrovok.ru. Читаются из OSTROVOK_* и файла .env."""

    model_config = SettingsConfigDict(
        env_prefix="OSTROVOK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_dsn: str = "postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"
    base_url: str = "https://ostrovok.ru"
    region_id: int = 6288539
    # Последняя ночь заезда в окне сбора.
    window_to: date = date(2026, 11, 29)
    # Опорные ночи для тарифов на 1-8 гостей.
    ref_weekday: date = date(2026, 11, 17)
    ref_weekend: date = date(2026, 11, 20)
    min_interval_s: float = 1.0
    poll_interval_s: float = 2.0
    max_polls: int = 15
    retry_delays_s: tuple[float, ...] = (2.0, 5.0, 15.0)
    # Остановка после стольких ответов 403/429 подряд.
    block_threshold: int = 5
    timeout_s: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    )
