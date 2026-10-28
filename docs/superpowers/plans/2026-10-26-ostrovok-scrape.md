# Сбор данных ostrovok.ru в Postgres — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать с внутреннего API ostrovok.ru отели Татарстана (статика, цены по ночам, тарифы на 1–8 гостей, отзывы) в локальный Postgres: сырые ответы в `raw`, нормализованные таблицы в `ostrovok`.

**Architecture:** Отдельный пакет `src/ostrovok_scraper/`. Синхронный HTTP-клиент с паузами и повторами → шаги сбора, каждый пишет ответы в `raw.responses` по ключу (докачка) → `normalize` одной транзакцией пересобирает схему `ostrovok` чистыми парсерами. CLI `ostrovok-scrape`.

**Tech Stack:** Python 3.13 (uv), httpx (sync), psycopg 3, pydantic-settings, Postgres 17 в Docker, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-ostrovok-scrape-design.md`

## Global Constraints

- Новая зависимость только одна: `psycopg[binary]>=3.2`. `httpx`, `pydantic-settings` уже есть.
- Тесты без сети. Сеть нужна только в задаче 11 (живой сбор).
- Один поток; не чаще 1 запроса в секунду; опрос поиска раз в 2 с, не больше 15 опросов; повторы на таймаут/429/5xx с паузами 2, 5, 15 с; остановка после 5 ответов 403/429 подряд.
- Регион `6288539`. `KINDS = ("hotel", "hostel", "apart_hotel", "guesthouse", "sanatorium", "boutique", "resort", "mini_hotel")`.
- Окно: ночи заезда с дня сбора по `2026-11-29` включительно. Опорные ночи: будни `2026-11-17`, выходные `2026-11-20`. Выходные ночи — заезд в пятницу или субботу.
- Валюта `RUB`, язык `ru`. Часовой пояс для «00:00 дня заезда» — `Europe/Moscow`.
- Дети в `paxes` — ключ `child_ages` (не `children`). В номере до 6 взрослых и до 4 детей.
- Отзывы только `lang=ru`. Никогда не сохранять `author_name`, `author_residency`, `authorName`, `authorResidency` и контакты отеля (`hotel.contacts`).
- Деньги — `numeric(12,2)` / `Decimal`.
- Проект не под git: вместо коммита каждая задача заканчивается зелёным `uv run pytest -q`.
- Стиль как в `src/hotel_agent`: аннотации типов, докстринги и комментарии по-русски.

## Review Focus

1. **Реальные ответы неполнее фикстур** (`rates: null`, тариф без `payment_types` или `rooms`, отзыв без `id`) — строка пропускается и считается в `skipped`, сбор и нормализация не падают. Тест — задачи 5 и 6.
2. **Отель не из Казани и редиректы** (`/hotel/russia/naberezhnye_chelny/mid…/…/` отвечает 301) — клиент идёт по редиректу. Тест — задача 2.
3. **Пробный прогон, где окно не накрывает опорные ночи** (`--nights 2` с 27.10) — тарифы на 2 гостей берутся с ближайшей ночи того же типа, а если её нет — у отеля нет тарифов этого типа, без ошибок. Тест — задача 8.
4. **Граница пагинации отзывов** (`total` = 10, 11, 500, 501) — 0, 1, 1, 2 страницы. Тест — задача 8.
5. **Блокировка посреди `all`** — процесс завершается с кодом 2 и сообщением, всё сохранённое остаётся, перезапуск продолжает. Тест — задача 10.

---

## Структура файлов

```
docker-compose.yml                          Postgres 17 + тестовая БД
docker/initdb/01-test-db.sql                CREATE DATABASE ostrovok_test
src/ostrovok_scraper/
  __init__.py
  config.py        ScraperConfig, KINDS, OTHER_GUESTS, MSK
  domain.py        тип ночи, paxes, окно, опорные ночи, ключи raw
  http.py          OstrovokHttp: паузы, повторы, блокировка
  api.py           OstrovokApi: поиск с опросом, страница отеля, тарифы, отзывы
  store.py         connect, apply_migrations, RawStore
  migrations/001_raw.sql, 002_ostrovok.sql
  rows.py          dataclass-строки таблиц ostrovok
  parsing/__init__.py
  parsing/serp.py        MapHotel, parse_map_hotels, parse_session_region
  parsing/hotel_page.py  extract_next_data, parse_hotel, parse_rooms, parse_city_region
  parsing/rates.py       parse_rates, free_child_age
  parsing/reviews.py     parse_api_reviews, parse_page_reviews
  steps/__init__.py      Item, StepReport, run_items
  steps/inputs.py        чтение результатов прошлых шагов из raw
  steps/search.py        serp, serp_guests
  steps/hotels.py        hotels
  steps/rates.py         child_age, rates
  steps/reviews.py       reviews
  normalize.py     raw → ostrovok, отчёт
  cli.py           ostrovok-scrape
tests/ostrovok_scraper/
  conftest.py          фикстура db_conn
  ostrovok_helpers.py  load_fixture, FakeTime (импорт `from ostrovok_helpers import …`:
                       в tests/ нет __init__.py, относительный импорт не работает, а имя
                       conftest занято корневым tests/conftest.py)
  fixtures/        serp_map.json, hotel_page.json, rates_2.json, reviews_api.json
  test_domain.py, test_http.py, test_store_db.py, test_parsing_serp_hotel.py,
  test_parsing_rates.py, test_parsing_reviews.py, test_api.py, test_steps.py,
  test_normalize_db.py, test_cli.py
```

---

### Task 1: Каркас пакета, настройки, доменные правила

**Files:**
- Modify: `pyproject.toml`, `.env.example`
- Create: `src/ostrovok_scraper/__init__.py`, `src/ostrovok_scraper/config.py`, `src/ostrovok_scraper/domain.py`
- Test: `tests/ostrovok_scraper/ostrovok_helpers.py`, `tests/ostrovok_scraper/test_domain.py`

**Interfaces:**
- Produces:
  - `config.ScraperConfig(BaseSettings)`, `env_prefix="OSTROVOK_"`, `env_file=".env"`, `extra="ignore"`. Поля и значения по умолчанию: `db_dsn="postgresql://ostrovok:ostrovok@localhost:5433/ostrovok"`, `base_url="https://ostrovok.ru"`, `region_id=6288539`, `window_to=date(2026,11,29)`, `ref_weekday=date(2026,11,17)`, `ref_weekend=date(2026,11,20)`, `min_interval_s=1.0`, `poll_interval_s=2.0`, `max_polls=15`, `retry_delays_s=(2.0, 5.0, 15.0)`, `block_threshold=5`, `timeout_s=30.0`, `user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"`.
  - `config.KINDS` (см. Global Constraints), `config.OTHER_GUESTS = (1, 3, 4, 5, 6, 7, 8)`, `config.MSK = ZoneInfo("Europe/Moscow")`.
  - `domain.night_type(night: date) -> Literal["weekday", "weekend"]`
  - `domain.paxes_for_guests(guests: int) -> list[dict]`
  - `domain.window_nights(start: date, end: date, limit: int | None = None) -> list[date]`
  - `domain.pick_reference_night(available: set[date], ref: date) -> date | None`
  - `domain.serp_key(night, guests)`, `hotel_key(hotel_id)`, `child_age_key(hotel_id, night)`, `rates_key(hotel_id, night, guests)`, `reviews_key(hotel_id, page)` → `str`; `domain.parse_key(key: str) -> dict[str, str]`.

- [ ] **Step 1: Обновить `pyproject.toml` и окружение**

`uv add 'psycopg[binary]>=3.2'`. В `[project.scripts]` добавить `ostrovok-scrape = "ostrovok_scraper.cli:main"`; в `[tool.hatch.build.targets.wheel] packages` добавить `"src/ostrovok_scraper"`; в `[tool.pytest.ini_options]` добавить `markers = ["db: нужен Postgres (OSTROVOK_TEST_DSN)"]`. В `.env.example` — блок:

```
# Сбор данных островка (uv run ostrovok-scrape)
OSTROVOK_DB_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok
# Тесты с БД; без переменной они пропускаются
OSTROVOK_TEST_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok_test
```

Создать пустые `src/ostrovok_scraper/__init__.py` и `src/ostrovok_scraper/cli.py` с `def main() -> None: ...` (заглушка, чтобы `uv sync` собрал скрипт; наполняется в задаче 10).

- [ ] **Step 2: Написать падающие тесты `tests/ostrovok_scraper/test_domain.py`**

`tests/ostrovok_scraper/ostrovok_helpers.py` пока содержит только `load_fixture(name: str) -> dict` (читает JSON из `tests/ostrovok_scraper/fixtures/<name>`; каждый вызов — свежая копия).

```python
from datetime import date

import pytest

from ostrovok_scraper import domain


@pytest.mark.parametrize(
    ("night", "expected"),
    [(date(2026, 11, 17), "weekday"), (date(2026, 11, 19), "weekday"), (date(2026, 11, 20), "weekend"),
     (date(2026, 11, 21), "weekend"), (date(2026, 11, 22), "weekday")],
)
def test_night_type_friday_and_saturday_are_weekend(night, expected):
    assert domain.night_type(night) == expected


@pytest.mark.parametrize(
    ("guests", "paxes"),
    [(1, [{"adults": 1}]), (6, [{"adults": 6}]),
     (7, [{"adults": 6, "child_ages": [16]}]), (8, [{"adults": 6, "child_ages": [16, 16]}])],
)
def test_paxes_for_guests(guests, paxes):
    assert domain.paxes_for_guests(guests) == paxes


@pytest.mark.parametrize("guests", [0, 9])
def test_paxes_for_guests_rejects_out_of_range(guests):
    with pytest.raises(ValueError):
        domain.paxes_for_guests(guests)


def test_window_nights_inclusive_and_limited():
    assert domain.window_nights(date(2026, 11, 28), date(2026, 11, 29)) == [date(2026, 11, 28), date(2026, 11, 29)]
    assert domain.window_nights(date(2026, 10, 27), date(2026, 11, 29), limit=2) == [date(2026, 10, 27), date(2026, 10, 28)]
    assert domain.window_nights(date(2026, 12, 1), date(2026, 11, 29)) == []


def test_pick_reference_night_prefers_ref_then_nearest_same_type():
    ref = date(2026, 11, 17)  # вторник
    assert domain.pick_reference_night({ref, date(2026, 11, 18)}, ref) == ref
    # 16.11 (пн) и 18.11 (ср) одинаково близко — берём более раннюю; 20.11 (пт) другого типа
    assert domain.pick_reference_night({date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 20)}, ref) == date(2026, 11, 16)
    assert domain.pick_reference_night({date(2026, 11, 20), date(2026, 11, 21)}, ref) is None
    assert domain.pick_reference_night(set(), ref) is None


def test_keys_roundtrip():
    key = domain.rates_key("don_kikhot_hotel_", date(2026, 11, 17), 4)
    assert key == "hotel=don_kikhot_hotel_;night=2026-11-17;guests=4"
    assert domain.parse_key(key) == {"hotel": "don_kikhot_hotel_", "night": "2026-11-17", "guests": "4"}
    assert domain.serp_key(date(2026, 11, 17), 2) == "night=2026-11-17;guests=2"
    assert domain.hotel_key("x") == "hotel=x"
    assert domain.child_age_key("x", date(2026, 11, 17)) == "hotel=x;night=2026-11-17"
    assert domain.reviews_key("x", 2) == "hotel=x;page=2"
```

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_domain.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'ostrovok_scraper.domain'`

- [ ] **Step 4: Реализовать `config.py` и `domain.py` по Interfaces**

`paxes_for_guests`: 1–6 → `[{"adults": k}]`, 7–8 → 6 взрослых + `k - 6` детей по 16 лет; иначе `ValueError`. `pick_reference_night`: если `ref` в `available` — он; иначе среди ночей того же `night_type` минимум по `(abs((n - ref).days), n)`.

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_domain.py -v` → PASS; затем `uv run pytest -q` → все зелёные.

---

### Task 2: HTTP-клиент с паузами, повторами и остановкой при блокировке

**Files:**
- Create: `src/ostrovok_scraper/http.py`
- Test: `tests/ostrovok_scraper/test_http.py`

**Interfaces:**
- Consumes: `ScraperConfig`.
- Produces:
  - `class OstrovokHttpError(Exception)`: `__init__(self, message: str, *, status_code: int | None = None)`, `str(e) == message`, атрибут `status_code`; `class BlockedError(Exception)`.
  - `class OstrovokHttp`: `__init__(self, cfg: ScraperConfig, *, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic)`; `request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> httpx.Response`; `pause(self, seconds: float) -> None` (через внедрённый `sleep`); `close()`, контекстный менеджер.

- [ ] **Step 1: Написать падающие тесты**

Сначала добавить в `ostrovok_helpers.py` фейковые часы (пригодятся и в задаче 7): двигаются только на `sleep`, паузы копятся в списке.

```python
class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now
```

`test_http.py`:

```python
import httpx
import pytest
from ostrovok_helpers import FakeTime

from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.http import BlockedError, OstrovokHttp, OstrovokHttpError


def make_http(responses):
    """responses — список httpx.Response или исключений, отдаются по порядку."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    t = FakeTime()
    http = OstrovokHttp(ScraperConfig(_env_file=None), transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock)
    return http, seen, t


def test_waits_min_interval_between_requests():
    http, _, t = make_http([httpx.Response(200, json={}), httpx.Response(200, json={})])
    http.request("GET", "/a")
    http.request("GET", "/b")
    assert t.sleeps == [1.0]


def test_retries_429_and_5xx_with_backoff_then_succeeds():
    http, seen, t = make_http([httpx.Response(429), httpx.Response(502), httpx.Response(200, json={"ok": 1})])
    assert http.request("GET", "/a").json() == {"ok": 1}
    assert len(seen) == 3
    assert t.sleeps == [2.0, 5.0]


def test_gives_up_after_three_retries():
    http, seen, t = make_http([httpx.Response(500)] * 4)
    with pytest.raises(OstrovokHttpError) as exc:
        http.request("GET", "/a")
    assert exc.value.status_code == 500
    assert len(seen) == 4
    assert t.sleeps == [2.0, 5.0, 15.0]


def test_retries_timeout():
    http, seen, _ = make_http([httpx.ReadTimeout("slow"), httpx.Response(200, json={})])
    assert http.request("GET", "/a").status_code == 200
    assert len(seen) == 2


def test_other_4xx_is_not_retried():
    http, seen, _ = make_http([httpx.Response(400, json={"error": "too many adults. max: 6"})])
    with pytest.raises(OstrovokHttpError) as exc:
        http.request("GET", "/a")
    assert exc.value.status_code == 400
    assert "too many adults" in str(exc.value)
    assert len(seen) == 1


def test_blocked_after_five_consecutive_403():
    http, _, _ = make_http([httpx.Response(403)] * 5)
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):
            http.request("GET", "/a")
    with pytest.raises(BlockedError):
        http.request("GET", "/a")


def test_success_resets_block_counter():
    http, _, _ = make_http([httpx.Response(403)] * 4 + [httpx.Response(200, json={})] + [httpx.Response(403)] * 4)
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):
            http.request("GET", "/a")
    http.request("GET", "/ok")
    for _ in range(4):
        with pytest.raises(OstrovokHttpError):  # не BlockedError: счётчик сброшен
            http.request("GET", "/a")


def test_follows_redirects_and_sends_browser_user_agent():
    http, seen, _ = make_http([
        httpx.Response(301, headers={"Location": "https://ostrovok.ru/hotel/russia/naberezhnye_chelny/mid1/x/"}),
        httpx.Response(200, text="<html></html>"),
    ])
    assert http.request("GET", "/hotel/russia/kazan/mid1/x/").status_code == 200
    assert seen[1].url.path == "/hotel/russia/naberezhnye_chelny/mid1/x/"
    assert seen[0].headers["User-Agent"].startswith("Mozilla/5.0")
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_http.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Реализовать `OstrovokHttp`**

`httpx.Client(base_url=cfg.base_url, timeout=cfg.timeout_s, follow_redirects=True, headers={"User-Agent": cfg.user_agent, "Accept-Language": "ru"}, transport=transport)`. Перед каждой попыткой (включая повторы) — дождаться `min_interval_s` от прошлой попытки по `clock`, через `sleep`. Ответ 403/429 увеличивает счётчик подряд идущих, любой другой ответ сбрасывает; счётчик ≥ `block_threshold` → `BlockedError` сразу. Повторяются только таймауты (`httpx.TimeoutException`), 429 и 5xx — с паузами из `retry_delays_s`; когда паузы кончились — `OstrovokHttpError(текст ответа[:300], status_code=код)` (у таймаута — текст исключения и `status_code=None`). Прочие не-2xx — `OstrovokHttpError` без повтора.

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_http.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 3: Postgres, миграции, хранилище сырых ответов

**Files:**
- Create: `docker-compose.yml`, `docker/initdb/01-test-db.sql`, `src/ostrovok_scraper/migrations/001_raw.sql`, `src/ostrovok_scraper/migrations/002_ostrovok.sql`, `src/ostrovok_scraper/store.py`
- Modify: `tests/ostrovok_scraper/conftest.py`
- Test: `tests/ostrovok_scraper/test_store_db.py`

**Interfaces:**
- Produces:
  - `store.connect(dsn: str) -> psycopg.Connection` (`row_factory=dict_row`, `autocommit=False`).
  - `store.apply_migrations(conn) -> list[str]` — выполняет все `migrations/*.sql` по имени (через `importlib.resources`), возвращает имена; SQL идемпотентный (`IF NOT EXISTS`), повторный запуск безопасен.
  - `class RawStore(conn)`:
    - `save(step: str, key: str, *, request: dict, status_code: int | None, body: dict | None, error: str | None = None, incomplete: bool = False) -> None` — upsert по `(step, key)`, обновляет `fetched_at`, сразу `commit`.
    - `existing_keys(step: str, *, include_failed: bool = True) -> set[str]` — `True`: все ключи шага; `False`: только удачные (`error IS NULL AND NOT incomplete`).
    - `bodies(step: str) -> list[tuple[str, dict]]` — ключ и тело удачных строк (`error IS NULL`, в том числе `incomplete`), по ключу.
    - `incomplete_keys(step: str) -> list[str]`; `first_fetched_at(step: str) -> datetime | None`.
  - `conftest.db_conn` — фикстура: без `OSTROVOK_TEST_DSN` → `pytest.skip`; иначе соединение, `DROP SCHEMA IF EXISTS raw, ostrovok CASCADE`, `apply_migrations`, yield, close.

- [ ] **Step 1: Docker**

`docker-compose.yml`: сервис `ostrovok-db`, образ `postgres:17`, `POSTGRES_USER/PASSWORD/DB = ostrovok`, порт `5433:5432`, volume `ostrovok-pgdata:/var/lib/postgresql/data`, `./docker/initdb:/docker-entrypoint-initdb.d:ro`, healthcheck `pg_isready -U ostrovok`. `docker/initdb/01-test-db.sql`: `CREATE DATABASE ostrovok_test;`.

Run: `docker compose up -d && docker compose exec ostrovok-db psql -U ostrovok -lqt | cut -d'|' -f1`
Expected: в списке есть `ostrovok` и `ostrovok_test`.

- [ ] **Step 2: Миграции**

`001_raw.sql` — `CREATE SCHEMA IF NOT EXISTS raw;` и таблица `raw.responses` ровно как в разделе 6.1 спека (с `IF NOT EXISTS`).

`002_ostrovok.sql`:

```sql
CREATE SCHEMA IF NOT EXISTS ostrovok;

CREATE TABLE IF NOT EXISTS ostrovok.meta (key text PRIMARY KEY, value text NOT NULL);

CREATE TABLE IF NOT EXISTS ostrovok.regions (
    region_id bigint PRIMARY KEY, name text NOT NULL, name_en text, type text, parent_id bigint,
    country_code text, lat double precision, lon double precision, catalog_slug text
);

CREATE TABLE IF NOT EXISTS ostrovok.hotels (
    hotel_id text PRIMARY KEY, master_id bigint NOT NULL UNIQUE, region_id bigint, name text NOT NULL,
    kind text, stars smallint, address text, lat double precision, lon double precision,
    rating numeric(3,1), rating_count integer, rating_details jsonb,
    check_in_from time, check_out_until time, description jsonb, amenities jsonb,
    flags text[] NOT NULL DEFAULT '{}', payment_methods text[] NOT NULL DEFAULT '{}', policies jsonb,
    free_child_age smallint NOT NULL, free_child_age_source text NOT NULL,
    metro jsonb, images text[] NOT NULL DEFAULT '{}', reviews_total integer
);

CREATE TABLE IF NOT EXISTS ostrovok.rooms (
    hotel_id text NOT NULL, rg_hash text NOT NULL, parent_rg_hash text, name text NOT NULL,
    bedding_type text, bathroom text, size_m2 numeric(6,1),
    amenities text[] NOT NULL DEFAULT '{}', images text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (hotel_id, rg_hash)
);

CREATE TABLE IF NOT EXISTS ostrovok.night_prices (
    hotel_id text NOT NULL, night date NOT NULL, min_price numeric(12,2) NOT NULL,
    PRIMARY KEY (hotel_id, night)
);

CREATE TABLE IF NOT EXISTS ostrovok.rates (
    id bigserial PRIMARY KEY, hotel_id text NOT NULL, rg_hash text NOT NULL, night date NOT NULL,
    night_type text NOT NULL, requested_guests smallint NOT NULL, occupancy smallint, room_name text,
    meal text NOT NULL, has_breakfast boolean NOT NULL, payment_type text NOT NULL,
    price numeric(12,2) NOT NULL, price_ratio numeric(8,4), discount_pct numeric(5,2),
    free_cancellation_before timestamptz, free_cancellation_hours numeric(7,1),
    penalty_after_ratio numeric(8,4), cancellation_policies jsonb,
    no_show_penalty numeric(12,2), no_show_nights smallint, allotment smallint, bed_places jsonb,
    source_rate_hash text,
    UNIQUE (hotel_id, night, requested_guests, rg_hash, meal, payment_type)
);

CREATE TABLE IF NOT EXISTS ostrovok.reviews (
    review_id bigint PRIMARY KEY, hotel_id text NOT NULL, review_date timestamptz NOT NULL,
    rating numeric(3,1), details jsonb, plus text, minus text, hotel_reply text,
    traveller_type text, trip_type text, stay_check_in date, stay_check_out date, adults smallint,
    room_name text, likes integer, images text[] NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS night_prices_night ON ostrovok.night_prices (night);
CREATE INDEX IF NOT EXISTS hotels_region ON ostrovok.hotels (region_id);
CREATE INDEX IF NOT EXISTS hotels_flags ON ostrovok.hotels USING gin (flags);
CREATE INDEX IF NOT EXISTS rates_lookup ON ostrovok.rates (hotel_id, requested_guests, night);
CREATE INDEX IF NOT EXISTS reviews_hotel_date ON ostrovok.reviews (hotel_id, review_date DESC);
```

- [ ] **Step 3: Написать падающие тесты `test_store_db.py`** (все с `pytestmark = pytest.mark.db`)

```python
def test_apply_migrations_is_idempotent(db_conn):
    assert apply_migrations(db_conn) == ["001_raw.sql", "002_ostrovok.sql"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'ostrovok'")
        assert cur.fetchone()["n"] == 7


def test_save_upserts_and_existing_keys_respect_failures(db_conn):
    store = RawStore(db_conn)
    store.save("rates", "k1", request={"a": 1}, status_code=200, body={"rates": []})
    store.save("rates", "k2", request={"a": 2}, status_code=500, body=None, error="boom")
    store.save("serp", "k3", request={}, status_code=200, body={"map_hotels": []}, incomplete=True)
    assert store.existing_keys("rates") == {"k1", "k2"}
    assert store.existing_keys("rates", include_failed=False) == {"k1"}
    assert store.existing_keys("serp", include_failed=False) == set()
    store.save("rates", "k2", request={"a": 2}, status_code=200, body={"rates": [1]})
    assert store.existing_keys("rates", include_failed=False) == {"k1", "k2"}
    assert dict(store.bodies("rates")) == {"k1": {"rates": []}, "k2": {"rates": [1]}}
    assert store.incomplete_keys("serp") == ["k3"]
    assert store.first_fetched_at("rates") is not None
```

(Создать `tests/ostrovok_scraper/conftest.py` с фикстурой `db_conn` по Interfaces.)

- [ ] **Step 4: Запустить — падает**

Run: `OSTROVOK_TEST_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok_test uv run pytest tests/ostrovok_scraper/test_store_db.py -v` → FAIL (`ModuleNotFoundError: ostrovok_scraper.store`).

- [ ] **Step 5: Реализовать `store.py` по Interfaces** (`jsonb` — через `psycopg.types.json.Jsonb`).

- [ ] **Step 6: Запустить — проходит**

Run: та же команда → PASS. `uv run pytest -q` без переменной → db-тесты `skipped`, остальные зелёные.

---

### Task 4: Строки таблиц, разбор выдачи и страницы отеля

**Files:**
- Create: `src/ostrovok_scraper/rows.py`, `src/ostrovok_scraper/parsing/__init__.py`, `src/ostrovok_scraper/parsing/serp.py`, `src/ostrovok_scraper/parsing/hotel_page.py`, `tests/ostrovok_scraper/fixtures/serp_map.json`, `tests/ostrovok_scraper/fixtures/hotel_page.json`
- Test: `tests/ostrovok_scraper/test_parsing_serp_hotel.py`

**Interfaces:**
- Produces:
  - `rows.RegionRow`, `rows.HotelRow`, `rows.RoomRow`, `rows.RateRow`, `rows.ReviewRow` — `@dataclass(frozen=True)`, поля совпадают с колонками таблиц задачи 3 и идут в том же порядке, кроме `rates.id` и `rates.price_ratio` (их считает БД). У `HotelRow` поля `free_child_age: int = 3`, `free_child_age_source: str = "default"` — последние.
  - `parsing.serp.MapHotel(hotel_id: str, master_id: int, region_slug: str, name: str, stars: int, price: Decimal)` (frozen dataclass).
  - `parsing.serp.parse_map_hotels(body: dict) -> tuple[list[MapHotel], int]` — строки и число пропущенных.
  - `parsing.serp.parse_session_region(region: dict) -> RegionRow`.
  - `parsing.hotel_page.extract_next_data(html: str) -> dict` — `{"hotel", "hotelRegionData", "reviews"}` из `props.pageProps`; нет `__NEXT_DATA__` → `ValueError("__NEXT_DATA__ not found")`.
  - `parsing.hotel_page.parse_hotel(page: dict, *, stars: int | None) -> HotelRow`.
  - `parsing.hotel_page.parse_rooms(page: dict) -> list[RoomRow]`.
  - `parsing.hotel_page.parse_city_region(page: dict, *, parent_id: int) -> RegionRow`.

- [ ] **Step 1: Фикстуры** (сокращённые реальные ответы от 2026-10-26; контакты и авторы заменены)

`fixtures/serp_map.json`:

```json
{
  "session_region": {"region_id": 6288539, "name": "Республика Татарстан", "name_en": "Republic of Tatarstan",
                     "type": "Province (State)", "country_code": "RU", "center": {"lat": 55.35, "lng": 50.91}},
  "search_finished": true,
  "total_av_hotels": 3,
  "map_hotels": [
    {"ota_hotel_id": "don_kikhot_hotel_", "master_id": 6479652, "region_catalog_slug": "russia/kazan",
     "name": "Отель Дон Кихот", "stars": 3, "rating": 8.9, "price": 5609, "latitude": 55.787327, "longitude": 49.105083},
    {"ota_hotel_id": "gostinitsa_amsterdam_na_tukaya_hotel", "master_id": 13127332, "region_catalog_slug": "russia/kazan",
     "name": "Отель Амстердам на Тукая", "stars": 0, "rating": 8.2, "price": 2414, "latitude": 55.770264, "longitude": 49.122334},
    {"ota_hotel_id": "broken_hotel", "name": "Без master_id", "stars": 0, "price": 1000}
  ]
}
```

`fixtures/hotel_page.json`:

```json
{
  "hotel": {
    "id": "don_kikhot_hotel_", "masterId": "6479652", "name": "Отель Дон Кихот",
    "facts": {"kind": "HOTEL"},
    "contacts": {"email": "hotel@example.com", "phone": "+7 000 000-00-00"},
    "location": {"address": "улица Бурхана Шахиди, 11, Казань", "latitude": 55.78732681274414,
                 "longitude": 49.10508346557617, "regionId": 1993,
                 "nearestSubways": [{"distance": 877, "name": "Кремлёвская", "cityId": 1993}]},
    "rating": {"total": 8.9, "reviewsCount": 336,
               "ostrovok": {"detailedRatings": {"cleanness": 9.23, "location": 9.38}}},
    "descriptionStruct": [{"title": "Расположение", "paragraphs": ["Семейный отель «Дон Кихот» расположен в Казани."]}],
    "amenityGroups": [{"amenities": [{"amenityId": 23, "name": "Кондиционер", "isFree": 0}]}],
    "serpFilters": ["HAS_INTERNET", "HAS_PARKING", "HAS_PETS"],
    "paymentMethods": ["VISA", "CASH", "MIR"],
    "images": [{"tmpl": "https://cdn.ostrovok.ru/t/{size}/h1.JPEG"}, {"tmpl": "https://cdn.ostrovok.ru/t/{size}/h2.JPEG"},
               {"tmpl": "https://cdn.ostrovok.ru/t/{size}/h3.JPEG"}, {"tmpl": "https://cdn.ostrovok.ru/t/{size}/h4.JPEG"},
               {"tmpl": "https://cdn.ostrovok.ru/t/{size}/h5.JPEG"}, {"tmpl": "https://cdn.ostrovok.ru/t/{size}/h6.JPEG"}],
    "policies": {
      "checkInCheckOut": {"type": "CHECK_IN_CHECK_OUT", "value": {
        "checkInTimeStart": {"hours": 14, "minutes": 0}, "checkInTimeEnd": null,
        "checkOutTimeStart": null, "checkOutTimeEnd": {"hours": 12, "minutes": 0}}},
      "extraBed": {"type": "EXTRA_BED", "value": [{"price": 1600, "priceUnit": "PER_GUEST_PER_NIGHT"}]},
      "children": {"type": "CHILDREN", "value": []}
    },
    "roomGroups": [
      {"rgHash": "3202302", "parentRgHash": "3202", "size": 25, "roomAmenityData": ["desk", "private-bathroom"],
       "nameStruct": {"mainName": "Двухместный номер Первой категории", "beddingType": "двуспальная кровать", "bathroom": ""},
       "images": [{"tmpl": "https://cdn.ostrovok.ru/t/{size}/r1.JPEG"}]},
      {"rgHash": "320240200004", "parentRgHash": "3202", "size": 25, "roomAmenityData": [],
       "nameStruct": {"mainName": "Двухместный номер Первой категории Мансарда", "beddingType": "2 отдельные кровати", "bathroom": ""},
       "images": []}
    ]
  },
  "hotelRegionData": {"regionId": 1993, "name": "Казань", "nameEn": "Kazan", "type": "CITY",
                      "center": {"lat": 55.79612731933594, "lng": 49.106414794921875},
                      "countryCode": "RU", "catalogSlug": "russia/kazan"},
  "reviews": {"total": 2, "list": [
    {"id": 1952999, "reviewDate": "2025-08-01 09:00:00+00", "rating": 9, "detailed": {"cleanness": 9},
     "reviewPlus": "Удобное расположение.", "reviewMinus": "", "reply": [], "travellerType": "COUPLE",
     "tripType": "LEISURE", "reviewLikes": 0, "images": [],
     "orderContext": {"adultNum": 2, "childNum": 0, "authorName": "Автор", "authorResidency": "xx-xx",
                      "authorRoom": "Двухместный номер Первой категории"}},
    {"id": 1315975, "reviewDate": "2024-02-11 18:30:00.5+00", "rating": 8, "detailed": {"cleanness": 8},
     "reviewPlus": "Вкусные завтраки.", "reviewMinus": "Шумно.", "reply": [{"text": "Спасибо!"}],
     "travellerType": "FAMILY", "tripType": "LEISURE", "reviewLikes": 1,
     "images": [{"tmpl": "https://cdn.ostrovok.ru/t/{size}/rv1.JPEG"}],
     "orderContext": {"adultNum": 2, "childNum": 1, "authorName": "Автор", "authorResidency": "xx-xx"}}
  ]}
}
```

- [ ] **Step 2: Написать падающие тесты**

```python
from datetime import time
from decimal import Decimal

import pytest

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.hotel_page import extract_next_data, parse_city_region, parse_hotel, parse_rooms
from ostrovok_scraper.parsing.serp import MapHotel, parse_map_hotels, parse_session_region


def test_parse_map_hotels_skips_broken_items():
    hotels, skipped = parse_map_hotels(load_fixture("serp_map.json"))
    assert skipped == 1
    assert hotels[0] == MapHotel("don_kikhot_hotel_", 6479652, "russia/kazan", "Отель Дон Кихот", 3, Decimal("5609"))
    assert [h.stars for h in hotels] == [3, 0]


def test_parse_map_hotels_handles_missing_list():
    assert parse_map_hotels({"search_finished": True, "map_hotels": None}) == ([], 0)


def test_parse_session_region():
    region = parse_session_region(load_fixture("serp_map.json")["session_region"])
    assert (region.region_id, region.name, region.name_en, region.type, region.parent_id) == (
        6288539, "Республика Татарстан", "Republic of Tatarstan", "Province (State)", None)


def test_extract_next_data_keeps_only_needed_parts():
    html = '<html><script id="__NEXT_DATA__" type="application/json">' \
           '{"props": {"pageProps": {"hotel": {"id": "x"}, "hotelRegionData": {}, "reviews": {"total": 0}, "uid": "u"}}}' \
           '</script></html>'
    assert extract_next_data(html) == {"hotel": {"id": "x"}, "hotelRegionData": {}, "reviews": {"total": 0}}
    with pytest.raises(ValueError, match="__NEXT_DATA__ not found"):
        extract_next_data("<html></html>")


def test_parse_hotel():
    row = parse_hotel(load_fixture("hotel_page.json"), stars=3)
    assert (row.hotel_id, row.master_id, row.region_id, row.kind, row.stars) == ("don_kikhot_hotel_", 6479652, 1993, "hotel", 3)
    assert (row.check_in_from, row.check_out_until) == (time(14, 0), time(12, 0))
    assert (row.rating, row.rating_count, row.reviews_total) == (Decimal("8.9"), 336, 2)
    assert row.flags == ["has_internet", "has_parking", "has_pets"]
    assert row.metro == [{"name": "Кремлёвская", "distance_m": 877}]
    assert len(row.images) == 5
    assert "checkInCheckOut" not in row.policies and row.policies["extraBed"][0]["price"] == 1600
    assert "hotel@example.com" not in repr(row)
    assert (row.free_child_age, row.free_child_age_source) == (3, "default")


def test_parse_rooms_and_city_region():
    rooms = parse_rooms(load_fixture("hotel_page.json"))
    assert [(r.rg_hash, r.name, r.bedding_type, r.size_m2) for r in rooms] == [
        ("3202302", "Двухместный номер Первой категории", "двуспальная кровать", Decimal("25")),
        ("320240200004", "Двухместный номер Первой категории Мансарда", "2 отдельные кровати", Decimal("25")),
    ]
    city = parse_city_region(load_fixture("hotel_page.json"), parent_id=6288539)
    assert (city.region_id, city.name, city.name_en, city.parent_id, city.catalog_slug) == (
        1993, "Казань", "Kazan", 6288539, "russia/kazan")
```

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_serp_hotel.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Реализовать `rows.py`, `parsing/serp.py`, `parsing/hotel_page.py`**

Правила, которых нет в тестах: `MapHotel.stars` — `int(item.get("stars") or 0)`, значения больше 5 делить на 10. Пропуск элемента выдачи — когда нет `ota_hotel_id`, `master_id` или `price` (`KeyError`/`TypeError`/`ValueError`). `extract_next_data` — регулярка `<script id="__NEXT_DATA__"[^>]*>(.*?)</script>` с `re.S`. `metro` — `[{"name", "distance_m"}]` из `nearestSubways`. `policies` — `{имя: policy["value"]}` для всех политик, кроме `checkInCheckOut`. Время заезда/выезда — `checkInTimeStart` / `checkOutTimeEnd` (`None`, если нет). `rooms`: до 3 картинок, дубли `rgHash` отбрасываются.

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_serp_hotel.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 5: Разбор тарифов и пробы возраста ребёнка

**Files:**
- Create: `src/ostrovok_scraper/parsing/rates.py`, `tests/ostrovok_scraper/fixtures/rates_2.json`
- Test: `tests/ostrovok_scraper/test_parsing_rates.py`

**Interfaces:**
- Consumes: `rows.RateRow`, `domain.night_type`, `config.MSK`.
- Produces:
  - `parse_rates(body: dict, *, hotel_id: str, night: date, requested_guests: int) -> tuple[list[RateRow], int]` — строки (по одной на `(rg_hash, meal, payment_type)`, самая дешёвая) и число пропущенных.
  - `free_child_age(probe_body: dict) -> int` — 5, если есть тариф с `occupancy == 2`; иначе 3.

- [ ] **Step 1: Фикстура `fixtures/rates_2.json`** (Дон Кихот, 17.11, 2 гостя; сокращено)

```json
{"ota_hotel_id": "don_kikhot_hotel_", "rates": [
  {"hash": "sr-a", "occupancy": 2, "allotment": 12, "room_name": "Двухместный номер Первой категории (двуспальная кровать)",
   "rooms": [{"rg_hash": "3202302"}], "meal": ["nomeal"], "meal_data": {"meals": [{"value": "nomeal", "has_breakfast": false}]},
   "bed_places": {"main_count": 2, "extra_count": 0, "child_cot_count": 0, "shared_with_children_count": 0},
   "payment_options": {"payment_types": [{"type": "now", "amount": "5609.00", "currency_code": "RUB",
                                          "discounts_info": {"total_percent": 2}}]},
   "cancellation_info": {"free_cancellation_before": "2026-11-15T11:00:00Z", "policies": [
     {"start_at": null, "end_at": "2026-11-15T11:00:00Z", "penalty": {"amount": "0.00", "currency_code": "RUB"}},
     {"start_at": "2026-11-15T11:00:00Z", "end_at": null, "penalty": {"amount": "5609.00", "currency_code": "RUB"}}]},
   "no_show": {"penalty": {"amount": "5609.00", "night_count": 1}}},
  {"hash": "sr-b", "occupancy": 2, "allotment": 12, "room_name": "Двухместный номер Первой категории (двуспальная кровать)",
   "rooms": [{"rg_hash": "3202302"}], "meal": ["nomeal"], "meal_data": {"meals": []},
   "payment_options": {"payment_types": [{"type": "now", "amount": "5800.00", "currency_code": "RUB"}]},
   "cancellation_info": {"free_cancellation_before": null, "policies": []}, "no_show": null},
  {"hash": "sr-c", "occupancy": 2, "allotment": 12, "room_name": "Двухместный номер Первой категории (двуспальная кровать)",
   "rooms": [{"rg_hash": "3202302"}], "meal": ["breakfast"], "meal_data": {"meals": [{"value": "breakfast", "has_breakfast": true}]},
   "payment_options": {"payment_types": [{"type": "hotel", "amount": "6200.00", "currency_code": "RUB"}]},
   "cancellation_info": {"free_cancellation_before": null, "policies": [
     {"start_at": null, "end_at": null, "penalty": {"amount": "6200.00", "currency_code": "RUB"}}]},
   "no_show": {"penalty": {"amount": "6200.00", "night_count": 1}}},
  {"hash": "sr-d", "occupancy": 2, "allotment": 3, "room_name": "Неизвестный номер",
   "rooms": [{"rg_hash": "999999"}], "meal": ["nomeal"], "meal_data": {"meals": []},
   "payment_options": {"payment_types": [{"type": "now", "amount": "7000.00", "currency_code": "RUB"}]},
   "cancellation_info": {"free_cancellation_before": null, "policies": []}, "no_show": null},
  {"hash": "sr-e", "occupancy": 2, "rooms": [{"rg_hash": "3202302"}], "meal": ["nomeal"],
   "payment_options": {"payment_types": []}},
  {"hash": "sr-f", "occupancy": 2, "rooms": [], "meal": ["nomeal"],
   "payment_options": {"payment_types": [{"type": "now", "amount": "100.00"}]}}
]}
```

- [ ] **Step 2: Написать падающие тесты**

```python
from datetime import date, datetime, timezone
from decimal import Decimal

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.rates import free_child_age, parse_rates


def rates_by_hash():
    rows, skipped = parse_rates(load_fixture("rates_2.json"), hotel_id="don_kikhot_hotel_",
                                night=date(2026, 11, 17), requested_guests=2)
    return {r.source_rate_hash: r for r in rows}, skipped


def test_parse_rates_dedupes_and_skips_broken():
    rows, skipped = rates_by_hash()
    assert set(rows) == {"sr-a", "sr-c", "sr-d"}  # sr-b дороже sr-a с тем же ключом
    assert skipped == 2  # sr-e без payment_types, sr-f без rooms


def test_parse_rates_refundable_rate():
    r = rates_by_hash()[0]["sr-a"]
    assert (r.rg_hash, r.meal, r.has_breakfast, r.payment_type, r.price) == ("3202302", "nomeal", False, "now", Decimal("5609.00"))
    assert (r.night_type, r.requested_guests, r.occupancy, r.allotment) == ("weekday", 2, 2, 12)
    assert r.free_cancellation_before == datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)
    assert r.free_cancellation_hours == Decimal("34.0")  # 00:00 МСК 17.11 = 16.11 21:00 UTC
    assert r.penalty_after_ratio == Decimal("1.0000")
    assert (r.no_show_penalty, r.no_show_nights, r.discount_pct) == (Decimal("5609.00"), 1, Decimal("2"))


def test_parse_rates_non_refundable_rate():
    r = rates_by_hash()[0]["sr-c"]
    assert (r.meal, r.has_breakfast, r.payment_type) == ("breakfast", True, "hotel")
    assert r.free_cancellation_before is None and r.free_cancellation_hours is None
    assert r.penalty_after_ratio == Decimal("1.0000")


def test_parse_rates_null_rates():
    assert parse_rates({"rates": None}, hotel_id="x", night=date(2026, 11, 20), requested_guests=8) == ([], 0)


def test_free_child_age():
    assert free_child_age({"rates": [{"occupancy": 2}, {"occupancy": 3}]}) == 5
    assert free_child_age({"rates": [{"occupancy": 3}]}) == 3
    assert free_child_age({"rates": None}) == 3
```

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_rates.py -v` → FAIL.

- [ ] **Step 4: Реализовать `parsing/rates.py`**

Сумма и тип оплаты — `payment_options.payment_types[0]`; `rg_hash` — `rooms[0].rg_hash`; без них тариф пропускается (ловить `KeyError`, `IndexError`, `TypeError`, `ValueError`, `decimal.InvalidOperation`). `meal` — `",".join(rate["meal"] or ["nomeal"])`. `free_cancellation_hours` = `(datetime.combine(night, time(0), MSK) - deadline)` в часах, `round(…, 1)`. `penalty_after_ratio` = максимальный `penalty.amount` среди `cancellation_info.policies` / `price`, `quantize(Decimal("0.0001"))`; без политик — `None`. `cancellation_policies` — список политик как есть.

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_rates.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 6: Разбор отзывов без данных автора

**Files:**
- Create: `src/ostrovok_scraper/parsing/reviews.py`, `tests/ostrovok_scraper/fixtures/reviews_api.json`
- Test: `tests/ostrovok_scraper/test_parsing_reviews.py`

**Interfaces:**
- Consumes: `rows.ReviewRow`.
- Produces:
  - `parse_review_date(value: str) -> datetime` — понимает смещение `+00` без минут.
  - `parse_api_reviews(body: dict, *, hotel_id: str) -> tuple[list[ReviewRow], int]` (snake_case из API отзывов).
  - `parse_page_reviews(reviews: dict, *, hotel_id: str) -> tuple[list[ReviewRow], int]` (`pageProps.reviews` со страницы, camelCase).

- [ ] **Step 1: Фикстура `fixtures/reviews_api.json`**

```json
{"total": 12, "ta_total": 2, "reviews": [
  {"id": 2164782, "review_type": "ost", "review_date": "2026-06-10 10:19:32.134689+00", "rating": 10,
   "detailed": {"cleanness": 10, "location": 10}, "review_plus": "Отель расположен в удобном месте.", "review_minus": "",
   "order_context": {"adult_num": 2, "author_name": "Автор", "author_residency": "xx-xx",
                     "author_room": "Двухместный номер на мансарде", "bedding_types": ["twin"],
                     "check_in": "2026-06-07", "check_out": "2026-06-08"},
   "traveller_type": "family", "trip_type": "leisure", "review_likes": 2,
   "images": [{"tmpl": "https://cdn.ostrovok.ru/t/{size}/hotel_review/c3.JPEG"}], "reply": {"text": ""}},
  {"id": 378605, "review_type": "ost", "review_date": "2021-05-12 06:42:23.080211+00", "rating": 7.3,
   "detailed": {"cleanness": 7}, "review_plus": "Чисто, свежо, комфортно.", "review_minus": "Забыли убраться в номере.",
   "order_context": {"adult_num": 2, "author_name": "Автор", "author_residency": "xx-xx"},
   "traveller_type": "couple", "trip_type": "leisure", "review_likes": 0, "images": [],
   "reply": {"text": "Спасибо за отзыв!"}},
  {"review_type": "ost", "review_date": "2021-01-01 00:00:00+00", "rating": 5}
]}
```

- [ ] **Step 2: Написать падающие тесты**

```python
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal

from ostrovok_helpers import load_fixture

from ostrovok_scraper.parsing.reviews import parse_api_reviews, parse_page_reviews, parse_review_date

AUTHOR_FIELDS = {"author_name", "author_residency", "authorName", "authorResidency"}


def test_parse_review_date_short_offset():
    assert parse_review_date("2021-05-12 06:42:23.080211+00") == datetime(2021, 5, 12, 6, 42, 23, 80211, tzinfo=timezone.utc)


def test_parse_api_reviews():
    rows, skipped = parse_api_reviews(load_fixture("reviews_api.json"), hotel_id="don_kikhot_hotel_")
    assert skipped == 1  # без id
    first, second = rows
    assert (first.review_id, first.rating, first.traveller_type, first.likes) == (2164782, Decimal("10"), "family", 2)
    assert (first.stay_check_in, first.stay_check_out, first.adults) == (date(2026, 6, 7), date(2026, 6, 8), 2)
    assert first.room_name == "Двухместный номер на мансарде"
    assert first.hotel_reply is None and first.minus is None
    assert second.hotel_reply == "Спасибо за отзыв!"
    for row in rows:
        assert not AUTHOR_FIELDS & set(asdict(row)) and "Автор" not in repr(row) and "xx-xx" not in repr(row)


def test_parse_page_reviews():
    page = load_fixture("hotel_page.json")
    rows, skipped = parse_page_reviews(page["reviews"], hotel_id="don_kikhot_hotel_")
    assert skipped == 0
    assert [(r.review_id, r.traveller_type, r.trip_type, r.hotel_reply) for r in rows] == [
        (1952999, "couple", "leisure", None), (1315975, "family", "leisure", "Спасибо!")]
    assert rows[1].images == ["https://cdn.ostrovok.ru/t/{size}/rv1.JPEG"]
    assert all("Автор" not in repr(r) for r in rows)
```

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_reviews.py -v` → FAIL.

- [ ] **Step 4: Реализовать `parsing/reviews.py`**

Пустые строки `plus`/`minus`/ответа → `None`. `traveller_type`, `trip_type` — в нижнем регистре. Ответ отеля: в API — `reply.text`, на странице — `reply[0].text` (список может быть пуст). Без `id` или даты — пропуск. Поля автора не читаются вовсе.

- [ ] **Step 5: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_parsing_reviews.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 7: Запросы к островку (`OstrovokApi`)

**Files:**
- Create: `src/ostrovok_scraper/api.py`
- Test: `tests/ostrovok_scraper/test_api.py`

**Interfaces:**
- Consumes: `OstrovokHttp` (`request`, `pause`), `ScraperConfig`, `KINDS`, `extract_next_data`.
- Produces:
  - `@dataclass(frozen=True) class Fetched: status_code: int; body: dict; incomplete: bool = False`.
  - `class OstrovokApi(http: OstrovokHttp, cfg: ScraperConfig, *, new_uuid: Callable[[], str] = lambda: str(uuid.uuid4()))`:
    - `search(night: date, paxes: list[dict]) -> Fetched` — `GET /hotel/search/v2/site/serp?body=…` за сессией, затем до `max_polls` раз: `http.pause(poll_interval_s)` и `POST /hotel/search/v2/site/serp?session=<id>` с `{"session_params": SP, "page": 1, "filters": {"kinds": list(KINDS)}, "session_id": id, "map_hotels": true}`, пока `search_finished`. Тело результата — последний ответ POST плюс `"session_region": session_info.region_related.region`; `incomplete = not search_finished`.
    - `hotel_page(region_slug: str, master_id: int, hotel_id: str) -> Fetched` — `GET /hotel/{region_slug}/mid{master_id}/{hotel_id}/`, тело — `extract_next_data(html)`.
    - `rates(hotel_id: str, region_id: int, night: date, paxes: list[dict]) -> Fetched` — `GET /hotel/search/v1/site/hp/search?body=…` с `arrival_date=night`, `departure_date=night+1`.
    - `reviews(hotel_id: str, page: int, page_size: int = 500) -> Fetched` — `GET /hotel/search/v1/site/reviews` с `hotel, lang=ru, sort=relevance_desc, page, page_size`.
  - `SP = {"arrival_date", "departure_date", "currency": "RUB", "language": "ru", "paxes", "region_id": cfg.region_id, "search_uuid": new_uuid()}`. Тело `hp/search`: `{"arrival_date", "departure_date", "hotel", "currency": "RUB", "lang": "ru", "region_id", "paxes", "search_uuid"}`.

- [ ] **Step 1: Написать падающие тесты**

Помощник в тесте: `make_api(handler) -> tuple[OstrovokApi, FakeTime]` — `OstrovokHttp(ScraperConfig(_env_file=None), transport=httpx.MockTransport(handler), sleep=t.sleep, clock=t.clock)` с `FakeTime` из `ostrovok_helpers`, и `OstrovokApi(http, cfg, new_uuid=lambda: "u1")`.

```python
def test_search_polls_until_finished_and_keeps_session_region():
    posts = []

    def handler(request):
        if request.method == "GET":
            body = json.loads(request.url.params["body"])
            assert body["session_params"]["paxes"] == [{"adults": 6, "child_ages": [16]}]
            assert body["session_params"]["region_id"] == 6288539
            return httpx.Response(200, json={"session_info": {"session": {"id": "s1"},
                                                              "region_related": {"region": {"region_id": 6288539}}}})
        posts.append(json.loads(request.content))
        assert request.url.params["session"] == "s1"
        return httpx.Response(200, json={"search_finished": len(posts) == 2, "map_hotels": [{"ota_hotel_id": "x"}]})

    api, t = make_api(handler)
    fetched = api.search(date(2026, 11, 17), [{"adults": 6, "child_ages": [16]}])
    assert fetched.incomplete is False
    assert len(posts) == 2
    assert posts[0]["map_hotels"] is True and posts[0]["filters"]["kinds"][0] == "hotel"
    assert posts[0]["session_params"]["departure_date"] == "2026-11-18"
    assert fetched.body["session_region"] == {"region_id": 6288539}
    assert t.sleeps.count(2.0) >= 2


def test_search_marks_incomplete_after_max_polls():
    # handler всегда отвечает search_finished=False → ровно 15 POST, incomplete=True


def test_hotel_page_builds_url_and_extracts_next_data():
    # GET /hotel/russia/kazan/mid6479652/don_kikhot_hotel_/ → HTML со скриптом __NEXT_DATA__
    # fetched.body == {"hotel": {...}, "hotelRegionData": {...}, "reviews": {...}}


def test_rates_and_reviews_params():
    # rates: params body содержит hotel, region_id=1993, arrival_date=2026-11-17, departure_date=2026-11-18, paxes
    # reviews: params == {"hotel": "x", "lang": "ru", "sort": "relevance_desc", "page": "2", "page_size": "500"}
```

Три последних теста дописать по комментариям — ассерты в них ровно те, что перечислены.

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_api.py -v` → FAIL.

- [ ] **Step 3: Реализовать `api.py` по Interfaces.** JSON в `body=` — `json.dumps(..., ensure_ascii=False)`.

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_api.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 8: Шаги сбора и докачка

**Files:**
- Create: `src/ostrovok_scraper/steps/__init__.py`, `steps/inputs.py`, `steps/search.py`, `steps/hotels.py`, `steps/rates.py`, `steps/reviews.py`
- Test: `tests/ostrovok_scraper/test_steps.py`

**Interfaces:**
- Consumes: `OstrovokApi`, `Fetched`, `RawStore` (только `save`, `existing_keys`, `bodies`), `domain.*`, `parse_map_hotels`, `MapHotel`, `BlockedError`, `OstrovokHttpError`.
- Produces:
  - `steps.Item(key: str, request: dict, fetch: Callable[[], Fetched])`; `steps.StepReport(step: str, done: int = 0, skipped: int = 0, errors: int = 0)`.
  - `steps.run_items(store, step: str, items: Iterable[Item], *, retry_failed: bool = False) -> StepReport` — пропускает `store.existing_keys(step, include_failed=not retry_failed)`; удача → `save(step, key, request=item.request, status_code=f.status_code, body=f.body, incomplete=f.incomplete)`, `done += 1`; `OstrovokHttpError` и `ValueError` → `save(..., status_code=getattr(e, "status_code", None), body=None, error=str(e))`, `errors += 1`; `BlockedError` пробрасывается без сохранения.
  - `inputs.serp_hotels(store) -> dict[str, MapHotel]` (шаги `serp` и `serp_guests`), `inputs.available_nights(store) -> dict[str, set[date]]` (шаг `serp`), `inputs.guests_hotels(store) -> dict[tuple[int, date], set[str]]` (шаг `serp_guests`), `inputs.page_facts(store) -> dict[str, tuple[int, int]]` (`hotel_id → (region_id, reviews_total)` из шага `hotels`), `inputs.selected_hotels(hotel_ids: Iterable[str], limit: int | None) -> list[str]` (сортировка, первые `limit`).
  - `search.serp_items(api, cfg, *, today: date, nights: int | None) -> list[Item]` (2 гостя по окну), `search.serp_guests_items(api, cfg) -> list[Item]` (`OTHER_GUESTS` × обе опорные ночи), `hotels.hotel_items(api, hotels: dict[str, MapHotel], hotel_ids: list[str]) -> list[Item]`.
  - `rates.child_age_night(available: set[date], cfg) -> date | None` — опорная ночь будней, иначе выходных.
  - `rates.plan_rate_requests(hotel_ids: list[str], available: dict[str, set[date]], guests_hotels: dict[tuple[int, date], set[str]], cfg) -> list[tuple[str, date, int]]`.
  - `reviews.review_pages(total: int, page_size: int = 500) -> int` — 0 при `total <= 10`, иначе `ceil(total / page_size)`.
  - `run_serp(api, store, cfg, *, today, nights=None, retry_failed=False)`, `run_serp_guests(api, store, cfg, *, retry_failed=False)`; `run_hotels`, `run_child_age`, `run_rates`, `run_reviews` — все `(api, store, cfg, *, limit_hotels=None, retry_failed=False)`; каждый `-> StepReport`. Шаги, которым нужен `region_id`, пропускают отели без страницы.

- [ ] **Step 1: Написать падающие тесты**

В тесте: `CFG = ScraperConfig(_env_file=None)`; `FakeApi` — пустой класс (сборщики `Item` не вызывают `fetch`); `FakeStore` — словарь `(step, key) → (body, error, incomplete)` с методами `save`, `existing_keys`, `bodies` той же семантики, что у `RawStore`, и `error(step, key) -> str | None`.

```python
REF_WD, REF_WE = date(2026, 11, 17), date(2026, 11, 20)


def test_run_items_skips_existing_records_errors_and_retries_on_flag():
    store = FakeStore()
    store.save("rates", "done", request={}, status_code=200, body={})
    store.save("rates", "failed", request={}, status_code=500, body=None, error="boom")
    calls = []

    def ok(key):
        return Item(key, {"k": key}, lambda: calls.append(key) or Fetched(200, {"rates": []}))

    def bad(key):
        def fetch():
            raise OstrovokHttpError("bad request", status_code=400)
        return Item(key, {"k": key}, fetch)

    report = run_items(store, "rates", [ok("done"), ok("failed"), ok("new"), bad("broken")])
    assert (report.done, report.skipped, report.errors) == (1, 2, 1)
    assert calls == ["new"] and store.error("rates", "broken") == "bad request"
    report = run_items(store, "rates", [ok("done"), ok("failed"), bad("broken")], retry_failed=True)
    assert calls == ["new", "failed"] and (report.done, report.skipped, report.errors) == (1, 1, 1)


def test_run_items_stops_on_block_without_saving():
    def blocked():
        raise BlockedError("403 x5")
    store = FakeStore()
    with pytest.raises(BlockedError):
        run_items(store, "serp", [Item("k", {}, blocked)])
    assert store.existing_keys("serp") == set()


def test_plan_rate_requests():
    available = {"a": {REF_WD, REF_WE}, "b": {date(2026, 11, 18)}}
    guests = {(4, REF_WD): {"a", "b"}, (8, REF_WE): {"a"}}
    assert sorted(plan_rate_requests(["a", "b"], available, guests, CFG)) == sorted([
        ("a", REF_WD, 2), ("a", REF_WE, 2), ("a", REF_WD, 4), ("a", REF_WE, 8),
        ("b", date(2026, 11, 18), 2), ("b", REF_WD, 4),
    ])


def test_plan_rate_requests_when_window_misses_reference_nights():
    # пробный прогон --nights 2 с 27.10 (вт) и 28.10 (ср): выходных ночей нет, 28.10 ближе к 17.11
    plan = plan_rate_requests(["a"], {"a": {date(2026, 10, 27), date(2026, 10, 28)}}, {}, CFG)
    assert plan == [("a", date(2026, 10, 28), 2)]


def test_child_age_night():
    assert child_age_night({REF_WD, REF_WE}, CFG) == REF_WD
    assert child_age_night({REF_WE}, CFG) == REF_WE
    assert child_age_night(set(), CFG) is None


@pytest.mark.parametrize(("total", "pages"), [(0, 0), (10, 0), (11, 1), (500, 1), (501, 2), (1386, 3)])
def test_review_pages(total, pages):
    assert review_pages(total) == pages


def test_serp_items_keys():
    items = serp_items(FakeApi(), CFG, today=date(2026, 11, 28), nights=None)
    assert [i.key for i in items] == ["night=2026-11-28;guests=2", "night=2026-11-29;guests=2"]
    assert len(serp_guests_items(FakeApi(), CFG)) == 14


def test_inputs_from_serp_bodies():
    store = FakeStore()
    store.save("serp", "night=2026-11-17;guests=2", request={}, status_code=200, body=load_fixture("serp_map.json"))
    store.save("serp_guests", "night=2026-11-20;guests=4", request={}, status_code=200,
               body={"map_hotels": [load_fixture("serp_map.json")["map_hotels"][0]]})
    assert set(serp_hotels(store)) == {"don_kikhot_hotel_", "gostinitsa_amsterdam_na_tukaya_hotel"}
    assert available_nights(store)["don_kikhot_hotel_"] == {REF_WD}
    assert guests_hotels(store) == {(4, REF_WE): {"don_kikhot_hotel_"}}
    assert selected_hotels(["b", "a", "c"], 2) == ["a", "b"]
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_steps.py -v` → FAIL.

- [ ] **Step 3: Реализовать `steps/*` по Interfaces**

`plan_rate_requests`: для каждого отеля — `(h, pick_reference_night(available[h], ref), 2)` для `ref` из (будни, выходные), если ночь нашлась; плюс `(h, ref, k)` для каждого `k` из `OTHER_GUESTS` и `ref` из опорных ночей, если `h in guests_hotels[(k, ref)]`. `run_rates` строит `Item` с ключом `rates_key(h, night, k)` и `paxes_for_guests(k)`; `run_child_age` — `child_age_key(h, night)` и `paxes = [{"adults": 2, "child_ages": [4]}]`; `run_reviews` — для отелей с `reviews_total > 10` страницы `1..review_pages(total)`. `run_*` возвращает отчёт `run_items` и печатает, сколько отелей пропущено без страницы.

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_steps.py -v` → PASS; `uv run pytest -q` → зелёные.

---

### Task 9: Нормализация `raw` → `ostrovok`

**Files:**
- Create: `src/ostrovok_scraper/normalize.py`
- Test: `tests/ostrovok_scraper/test_normalize_db.py`

**Interfaces:**
- Consumes: `RawStore`, все парсеры задач 4–6, `free_child_age`, `domain.parse_key`, `night_type`.
- Produces:
  - `@dataclass class NormalizeReport: counts: dict[str, int]; rates_by_guests: dict[int, int]; skipped: dict[str, int]; anomalies: dict[str, list[str]]`. Ключи `counts` — ровно `regions, hotels, rooms, night_prices, rates, reviews`; ключи `skipped` — имена шагов `serp, hotels, rates, reviews` (0 по умолчанию); ключи `anomalies`: `hotels_without_page`, `hotels_without_rates`, `rates_unknown_rg` (элементы `"<hotel_id>:<rg_hash>"`), `incomplete_searches` (ключи `serp`/`serp_guests`).
  - `normalize(conn, cfg: ScraperConfig) -> NormalizeReport` — одна транзакция: `TRUNCATE` всех таблиц `ostrovok`, вставка, затем `UPDATE ostrovok.rates SET price_ratio = round(price / np.min_price, 4) FROM ostrovok.night_prices np WHERE …` по `(hotel_id, night)`, затем `meta`.

Правила сборки: в `ostrovok` попадают только отели со страницей (остальные — в `hotels_without_page`, их цены и тарифы не пишутся). `stars` — из последнего `MapHotel` отеля. `free_child_age` — из пробы (`source="probe"`), иначе 3/`default`. Отзывы: при `reviews_total > 10` — из шага `reviews`, иначе со страницы. `meta`: `scraped_on` — дата (МСК) `first_fetched_at("serp")`, `window_from` — минимальная ночь ключей `serp`, `window_to`, `ref_weekday`, `ref_weekend` — из `cfg`; все значения ISO-строки.

- [ ] **Step 1: Написать падающие тесты** (`pytestmark = pytest.mark.db`; помощник `seed(store)` кладёт в raw:)
  - `serp` / `night=2026-11-17;guests=2` → `serp_map.json`;
  - `hotels` / `hotel=don_kikhot_hotel_` → `hotel_page.json`; `hotels` / `hotel=gostinitsa_amsterdam_na_tukaya_hotel` → `error="HTTP 500"`;
  - `child_age` / `hotel=don_kikhot_hotel_;night=2026-11-17` → `{"rates": [{"occupancy": 3}]}`;
  - `rates` / `hotel=don_kikhot_hotel_;night=2026-11-17;guests=2` → `rates_2.json`.

```python
def fetch(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_normalize_builds_tables(db_conn):
    seed(RawStore(db_conn))
    report = normalize(db_conn, CFG)
    assert report.counts == {"regions": 2, "hotels": 1, "rooms": 2, "night_prices": 1, "rates": 3, "reviews": 2}
    assert report.rates_by_guests == {2: 3}
    assert report.anomalies["hotels_without_page"] == ["gostinitsa_amsterdam_na_tukaya_hotel"]
    assert report.anomalies["rates_unknown_rg"] == ["don_kikhot_hotel_:999999"]
    assert report.skipped["rates"] == 2 and report.skipped["serp"] == 1

    [hotel] = fetch(db_conn, "SELECT * FROM ostrovok.hotels")
    assert (hotel["stars"], hotel["free_child_age"], hotel["free_child_age_source"]) == (3, 3, "probe")
    assert "contacts" not in hotel
    assert {r["region_id"]: r["parent_id"] for r in fetch(db_conn, "SELECT * FROM ostrovok.regions")} == {6288539: None, 1993: 6288539}

    ratios = {r["source_rate_hash"]: r["price_ratio"] for r in fetch(db_conn, "SELECT * FROM ostrovok.rates")}
    assert ratios == {"sr-a": Decimal("1.0000"), "sr-c": Decimal("1.1054"), "sr-d": Decimal("1.2480")}

    reviews = fetch(db_conn, "SELECT * FROM ostrovok.reviews ORDER BY review_id")
    assert [r["review_id"] for r in reviews] == [1315975, 1952999]
    meta = {m["key"]: m["value"] for m in fetch(db_conn, "SELECT * FROM ostrovok.meta")}
    assert meta["window_from"] == "2026-11-17" and meta["ref_weekend"] == "2026-11-20"


def test_normalize_is_idempotent(db_conn):
    seed(RawStore(db_conn))
    assert normalize(db_conn, CFG).counts == normalize(db_conn, CFG).counts


def test_reviews_come_from_api_when_more_than_ten(db_conn):
    store = RawStore(db_conn)
    seed(store)
    page = load_fixture("hotel_page.json")
    page["reviews"]["total"] = 12
    store.save("hotels", "hotel=don_kikhot_hotel_", request={}, status_code=200, body=page)
    store.save("reviews", "hotel=don_kikhot_hotel_;page=1", request={}, status_code=200, body=load_fixture("reviews_api.json"))
    normalize(db_conn, CFG)
    replies = {r["review_id"]: r["hotel_reply"] for r in fetch(db_conn, "SELECT * FROM ostrovok.reviews")}
    assert replies == {2164782: None, 378605: "Спасибо за отзыв!"}
```

- [ ] **Step 2: Запустить — падает**

Run: `OSTROVOK_TEST_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok_test uv run pytest tests/ostrovok_scraper/test_normalize_db.py -v` → FAIL.

- [ ] **Step 3: Реализовать `normalize.py`.** Вставка — `cursor.executemany` по полям dataclass (`dataclasses.astuple`), `jsonb`-поля через `Jsonb`.

- [ ] **Step 4: Запустить — проходит**

Run: та же команда → PASS; `uv run pytest -q` → зелёные.

---

### Task 10: CLI `ostrovok-scrape` и README

**Files:**
- Modify: `src/ostrovok_scraper/cli.py`, `README.md`
- Test: `tests/ostrovok_scraper/test_cli.py`

**Interfaces:**
- Consumes: всё выше.
- Produces:
  - `cli.build_parser() -> argparse.ArgumentParser` — подкоманды `db-init`, `serp`, `serp-guests`, `hotels`, `child-age`, `rates`, `reviews`, `normalize`, `report`, `all`; общие флаги `--limit-hotels N`, `--nights N`, `--retry-failed`, `--today YYYY-MM-DD` (по умолчанию `datetime.now(MSK).date()`).
  - `cli.open_resources(cfg) -> ContextManager[tuple[OstrovokApi, RawStore, psycopg.Connection]]` — отдельная функция, чтобы тесты её подменяли.
  - Вызовы шагов из CLI — строго именованными аргументами: `run_serp(api, store, cfg, today=…, nights=…, retry_failed=…)`, `run_serp_guests(api, store, cfg, retry_failed=…)`, остальные `run_*(api, store, cfg, limit_hotels=…, retry_failed=…)`, `normalize(conn, cfg)`.
  - `cli.main(argv: list[str] | None = None) -> int` — `all` = `serp → serp-guests → hotels → child-age → rates → reviews → normalize`; печатает `StepReport` строкой `"<step>: сделано N, пропущено N, ошибок N"` и отчёт нормализации; `BlockedError` → сообщение `"Похоже на блокировку островком: <текст>. Сохранённое не потеряно, перезапустите позже."` в stderr и код 2; иначе 0. `report` — для каждого шага `raw`: всего / с ошибкой / неполных, и `count(*)` по таблицам `ostrovok`.

- [ ] **Step 1: Написать падающие тесты**

Помощник в тесте `install_fakes(monkeypatch, fail_on=None, error=None) -> list`: подменяет `cli.open_resources` контекстом, отдающим `(None, None, None)`; каждую `cli.run_<шаг>` — функцией, которая дописывает в список `(шаг, kwargs)` и возвращает `StepReport(шаг)` (или бросает `error`, если `шаг == fail_on`); `cli.normalize` — дописывает `("normalize", {})` и возвращает пустой `NormalizeReport({}, {}, {}, {})`.

```python
def test_all_runs_steps_in_order_with_flags(monkeypatch):
    calls = install_fakes(monkeypatch)  # подменяет open_resources, run_* и normalize, пишет (имя, kwargs)
    assert main(["all", "--limit-hotels", "5", "--nights", "2", "--today", "2026-10-27"]) == 0
    assert [c[0] for c in calls] == ["serp", "serp_guests", "hotels", "child_age", "rates", "reviews", "normalize"]
    assert calls[0][1]["today"] == date(2026, 10, 27) and calls[0][1]["nights"] == 2
    assert calls[2][1]["limit_hotels"] == 5


def test_blocked_exits_with_code_2_and_message(monkeypatch, capsys):
    install_fakes(monkeypatch, fail_on="hotels", error=BlockedError("403 x5"))
    assert main(["all"]) == 2
    assert "Похоже на блокировку" in capsys.readouterr().err


def test_single_step_with_retry_failed(monkeypatch):
    calls = install_fakes(monkeypatch)
    assert main(["rates", "--retry-failed"]) == 0
    assert calls == [("rates", {"limit_hotels": None, "retry_failed": True})]
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/ostrovok_scraper/test_cli.py -v` → FAIL.

- [ ] **Step 3: Реализовать `cli.py`; дописать в `README.md` раздел «Данные островка (Татарстан)»** — команды из раздела 10 спека и `OSTROVOK_TEST_DSN=… uv run pytest` для тестов с БД.

- [ ] **Step 4: Запустить — проходит**

Run: `uv run pytest tests/ostrovok_scraper/test_cli.py -v` → PASS; `OSTROVOK_TEST_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok_test uv run pytest -q` → все зелёные, ничего не пропущено.

---

### Task 11: Пробный и полный сбор

**Files:** — (только запуск; сеть)

- [ ] **Step 1: База**

Run: `docker compose up -d && uv run ostrovok-scrape db-init`
Expected: напечатаны `001_raw.sql`, `002_ostrovok.sql`.

- [ ] **Step 2: Пробный прогон**

Run: `uv run ostrovok-scrape all --limit-hotels 5 --nights 2`
Expected: `serp: сделано 2`, `serp_guests: сделано 14`, `hotels: сделано 5`, ошибок 0 или единицы; в отчёте нормализации `hotels` = 5, `rates` > 0, `reviews` > 0; нет «Похоже на блокировку». Повторный запуск той же команды: все шаги «сделано 0», только пропуски.

- [ ] **Step 3: Глазами проверить данные пробы**

```bash
docker compose exec ostrovok-db psql -U ostrovok -c "SELECT hotel_id, name, stars, rating, free_child_age, reviews_total FROM ostrovok.hotels"
docker compose exec ostrovok-db psql -U ostrovok -c "SELECT hotel_id, night, requested_guests, room_name, meal, payment_type, price, price_ratio, free_cancellation_hours FROM ostrovok.rates ORDER BY hotel_id, requested_guests, price LIMIT 20"
```

Expected: названия и цены совпадают с сайтом для пары отелей; `price_ratio` около 1 для самых дешёвых тарифов 2 гостей.

- [ ] **Step 4: Полный сбор** (≈1 ч, запускать в фоне)

Run: `uv run ostrovok-scrape all`
Expected: шаги завершаются, «Похоже на блокировку» нет; ошибок — единицы процентов. При остановке — перезапуск той же командой, затем `uv run ostrovok-scrape all --retry-failed`.

- [ ] **Step 5: Сверить итог**

Run: `uv run ostrovok-scrape report`
Expected (ориентиры из проб 2026-10-26): `hotels` 350–500; `night_prices` — тысячи строк; `rates` для `requested_guests` 2 — у большинства отелей, для 5–8 — десятки отелей; `reviews` 20–35 тыс.; `incomplete_searches` пусто или единицы. Цифры и аномалии показать пользователю.
