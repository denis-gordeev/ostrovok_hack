from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки агента. Читаются из переменных окружения и файла .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Строка модели в формате init_chat_model: "<провайдер>:<модель>".
    llm_model: str = "anthropic:claude-opus-5-5"
    # Метод with_structured_output: json_schema | function_calling | json_mode.
    llm_structured_output_method: str = "json_schema"

    # Postgres со снимком островка (uv run ostrovok-scrape) — источник поиска по Татарстану.
    ostrovok_db_dsn: str = "postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"
    # Postgres с событиями Казани (схема kudago, загружает kudago-events); та же переменная, что у загрузки.
    kudago_db_dsn: str = "postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"

    # Трасса агента в терминале: вызовы LLM (токены, в т.ч. на размышление) и tools, итог каждого хода.
    agent_trace: bool = True
    # Папка для JSONL-трасс, файл на запуск; пусто — трасса только в терминале.
    agent_trace_dir: str = "traces"
