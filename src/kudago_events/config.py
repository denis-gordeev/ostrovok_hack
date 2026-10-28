from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

MSK = ZoneInfo("Europe/Moscow")


class KudagoConfig(BaseSettings):
    """Настройки загрузки событий KudaGo. Читаются из KUDAGO_* и файла .env."""

    model_config = SettingsConfigDict(env_prefix="KUDAGO_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # По умолчанию та же Postgres, что и снимок островка; события — в схеме kudago.
    db_dsn: str = "postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"
    base_url: str = "https://kudago.com"
    location: str = "kzn"
    # Окно загрузки: от сегодняшнего дня (по Москве) на столько дней вперёд.
    horizon_days: int = 60
    # При запуске агента события перезагружаются, если последней удачной загрузке больше стольких часов.
    max_age_hours: float = 24.0
    page_size: int = 100
    # Страховка от бесконечной пагинации.
    max_pages: int = 100
    min_interval_s: float = 1.0
    retry_delays_s: tuple[float, ...] = (2.0, 5.0, 15.0)
    timeout_s: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    )
