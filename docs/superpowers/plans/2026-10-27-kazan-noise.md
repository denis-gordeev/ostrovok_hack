# Относительный шум в точке Казани — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tool агента `noise_in_kazan(lat, lon)`: балл шума 0–100 днём и ночью и главные источники, по модели на данных OSM, скачанных через Overpass.

**Architecture:** Отдельный пакет `src/kazan_noise/`. Офлайн-конвейер `fetch → build → grid` складывает в `data/kazan_noise/` сырые ответы Overpass, признаки в UTM 39N (`features.npz`) и H3-сетку со шкалой (`grid.npz`). В рантайме `NoiseService` грузит снимок и считает модель прямо в точке: подисточники, экранирование 2.5D, листва, энергетическая сумма. Перцентиль берётся по застроенным ячейкам сетки. Tool в `src/hotel_agent/tools/noise.py` получает `NoiseService` через `AgentContext`.

**Tech Stack:** Python 3.13 (uv), shapely 2.2, pyproj 3.8, h3 4.5, numpy 2.5, httpx (sync), pydantic(-settings), LangChain tools, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-kazan-noise-design.md`

## Global Constraints

- Новые зависимости только эти: `uv add "shapely>=2.2" "pyproj>=3.8" "h3>=4.5" "numpy>=2.5"`. Без geopandas и scipy.
- Тесты без сети. Overpass нужен только в задаче 12 (живой прогон).
- Проект не под git: вместо коммита каждая задача заканчивается зелёным `uv run pytest -q`.
- В `tests/` нет `__init__.py` (import mode prepend): имена тестовых файлов должны быть уникальны во всём `tests/`. Новые файлы называются `test_noise_*.py`, хелперы лежат в `tests/kazan_noise/noise_helpers.py` (импорт `from noise_helpers import …`).
- Граница — relation `367666`, area id `3600367666`. Зеркала: `https://overpass-api.de/api/interpreter`, затем `https://maps.mail.ru/osm/tools/overpass/api/interpreter`. Между запросами ≥10 с, повторы через 15, 30, 60, 120 с, `[timeout:300]` в QL, HTTP-таймаут 400 с.
- Проекция: `EPSG:4326 → EPSG:32639`, `always_xy=True`. Центр Казани (55.7963, 49.1088) ↦ (381438.3, 6185027.2).
- День 07–23, ночь 23–07. Высота приёмника 4 м. H3 res 10.
- `score = round(100 · доля застроенных ячеек, где строго тише)`. Метки: [0,20) «очень тихо», [20,40) «тихо», [40,60) «средне», [60,80) «шумно», [80,100] «очень шумно».
- Ответ tool никогда не содержит модельных уровней (`model_level_*`) и всегда содержит «© OpenStreetMap contributors».
- Не трогать `src/hotel_agent/graph/prompt.py` и `src/hotel_agent/intents/*` (их пишет пользователь).
- Все настраиваемые числа модели живут только в `src/kazan_noise/weights.py`.
- Стиль как в `src/hotel_agent` и `src/ostrovok_scraper`: аннотации типов, докстринги и комментарии по-русски.

## Review Focus

1. **Overpass отвечает 200 с частичными данными** и `"remark": "runtime error: Query timed out…"` (или out of memory). Это ошибка: повторить, ничего не сохранять. Тест — задача 2.
2. **`fetch` прервали посреди записи.** Недописанный файл не должен считаться готовым: запись через `.tmp` и `os.replace`, перезапуск докачивает только недостающее. Тест — задача 2.
3. **Битая геометрия OSM** (мультиполигон с незамкнутым кольцом, член без геометрии, `null` в координатах, way из одной точки). Объект пропускается и считается в `BuildReport.skipped`, сборка не падает. Тесты — задачи 3 и 4.
4. **LLM перепутала lat и lon** (`lat=49.11, lon=55.80`). Возвращается «вне Казани» с подсказкой «похоже, перепутаны lat и lon». Точка на здании даёт фасады, а не самоэкранирование. Тесты — задачи 6 и 9.
5. **Снимок устарел** (`weights.py` правили после `build`) или нет `grid.npz` при наличии `features.npz`. Нужна явная ошибка «пересоберите: kazan-noise build && kazan-noise grid», а не тихо неверные баллы. Тесты — задачи 7 и 9.

---

## Структура файлов

```
src/kazan_noise/
  __init__.py
  config.py        NoiseConfig, DAY/NIGHT/MODES, WGS84/UTM
  queries.py       area_id, bbox_with_buffer, *_query, QUERY_NAMES
  overpass.py      OverpassClient, OverpassError
  fetch.py         fetch_all, load_raw
  osm.py           element_geometry, project, project_point, unproject_xy
  weights.py       все числа модели
  features.py      Source, Obstacles, Green, Features, BuildReport, парсеры, build_features
  propagation.py   SubSources, discretize, divergence_db, air_db, path_difference, shield_db, foliage_db, energy_sum
  model.py         Contribution, ReceiverResult, PointResult, NoiseModel
  snapshot.py      pack/unpack_geoms, weights_hash, save/load_features, write/read_meta, SnapshotMissingError
  scale.py         Scale, label
  grid.py          Grid, grid_cells, compute_grid, save_grid, load_grid
  service.py       OutsideKazanError, ModeScore, QuietSide, SourceInfo, NoiseReport, NoiseService
  validate.py      is_noise_complaint, spearman, load_hotels, validate_hotels, sanity_checks
  cli.py           kazan-noise fetch | build | grid | query | validate
src/hotel_agent/tools/noise.py   NoiseArgs, noise_in_kazan, report_for_llm
tests/kazan_noise/
  noise_helpers.py   FakeTime, KAZAN_*, osm_node/osm_way/osm_relation, raw_response, local_scene, kazan_snapshot
  test_noise_queries.py, test_noise_overpass.py, test_noise_osm.py, test_noise_features.py,
  test_noise_propagation.py, test_noise_model.py, test_noise_snapshot.py, test_noise_grid.py,
  test_noise_service.py, test_noise_cli.py, test_noise_validate.py
tests/test_noise_tool.py         report_for_llm
tests/test_graph.py              + сценарии tool шума
```

---

### Task 1: Каркас пакета, настройки, тексты запросов

**Files:**
- Modify: `pyproject.toml` (зависимости через `uv add`; `kazan-noise = "kazan_noise.cli:main"` в `[project.scripts]`; `"src/kazan_noise"` в `packages`), `.env.example`, `.gitignore` (+ `data/`)
- Create: `src/kazan_noise/__init__.py`, `src/kazan_noise/config.py`, `src/kazan_noise/queries.py`, `tests/kazan_noise/noise_helpers.py`
- Test: `tests/kazan_noise/test_noise_queries.py`

**Interfaces:**
- Produces:
  - `config.NoiseConfig(BaseSettings)`, `env_prefix="KAZAN_NOISE_"`, как `ostrovok_scraper/config.py`. Поля и значения по умолчанию: `data_dir: Path = Path("data/kazan_noise")`, `overpass_urls: tuple[str, ...]` (два URL из Global Constraints), `user_agent: str = "hotel-agent-kazan-noise/0.1"`, `min_interval_s = 10.0`, `retry_delays_s = (15.0, 30.0, 60.0, 120.0)`, `timeout_s = 400.0`, `boundary_relation = 367666`, `source_buffer_m = 3000.0`, `receiver_height_m = 4.0`, `h3_res = 10`. Свойства: `raw_dir = data_dir / "raw"`, `features_path = data_dir / "features.npz"`, `grid_path = data_dir / "grid.npz"`, `meta_path = data_dir / "meta.json"`.
  - `config.DAY = "day"`, `NIGHT = "night"`, `MODES = (DAY, NIGHT)`, `WGS84 = "EPSG:4326"`, `UTM = "EPSG:32639"`.
  - `queries.Bbox = tuple[float, float, float, float]` в порядке (south, west, north, east); `area_id(relation_id: int) -> int`; `bbox_with_buffer(bounds: dict, buffer_m: float) -> Bbox`, где `bounds` — Overpass-словарь `{"minlat","minlon","maxlat","maxlon"}`, а градусы долготы считаются по `cos` средней широты; `boundary_query(relation_id) -> str`, `buildings_query(relation_id) -> str`, `lines_query(bbox) -> str`, `areas_query(bbox) -> str`; `QUERY_NAMES = ("boundary", "buildings", "lines", "areas")`.
  - `noise_helpers.FakeTime` (копия из `tests/ostrovok_scraper/ostrovok_helpers.py`), `KAZAN_LAT, KAZAN_LON = 55.7963, 49.1088`, `KAZAN_X, KAZAN_Y = 381438.3, 6185027.2`.

Состав запросов — раздел «Этап 1» спецификации, плюс `wr["building"="construction"]` в `areas_query`: там есть `out meta`, по которому стройка отсеивается как устаревшая. Каждый запрос начинается с `[out:json][timeout:300];`.

- [ ] **Step 1: Write the failing tests**

```python
def test_area_id_for_kazan():
    assert area_id(367666) == 3600367666

def test_buildings_query_uses_kazan_area_and_relations():
    q = buildings_query(367666)
    assert q.startswith("[out:json][timeout:300];")
    assert "area(id:3600367666)" in q and 'wr["building"]' in q
    assert 'way["barrier"~"^(wall|noise_barrier|city_wall)$"]' in q and q.rstrip().endswith("out tags geom;")

def test_lines_query_is_limited_by_bbox():
    q = lines_query((55.5, 48.7, 56.0, 49.5))
    assert "(55.5,48.7,56.0,49.5)" in q
    assert 'way["railway"~"^(rail|tram|subway|light_rail)$"]' in q and 'way["aeroway"="runway"]' in q

def test_areas_query_requests_meta_and_construction_buildings():
    q = areas_query((55.5, 48.7, 56.0, 49.5))
    assert 'wr["building"="construction"]' in q and q.rstrip().endswith("out meta geom;")

def test_bbox_buffer_is_3_km():
    bounds = {"minlat": 55.6031357, "minlon": 48.8205846, "maxlat": 55.9382194, "maxlon": 49.3812469}
    s, w, n, e = bbox_with_buffer(bounds, 3000)
    assert s == pytest.approx(55.6031357 - 3000 / 111320, abs=1e-6)
    assert w == pytest.approx(48.8205846 - 3000 / (111320 * math.cos(math.radians(55.7706776))), abs=1e-6)

def test_config_defaults():
    cfg = NoiseConfig(_env_file=None)
    assert cfg.boundary_relation == 367666 and cfg.overpass_urls[1].startswith("https://maps.mail.ru")
    assert cfg.features_path == Path("data/kazan_noise/features.npz")
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_queries.py -q`. Expected: FAIL (`ModuleNotFoundError: kazan_noise`)
- [ ] **Step 3: Implement** `uv add …`, правки `pyproject.toml`, `config.py`, `queries.py`, `noise_helpers.py`. В `.env.example` добавить блок `# Шум Казани (uv run kazan-noise)` с `KAZAN_NOISE_DATA_DIR=data/kazan_noise`.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: всё PASS

---

### Task 2: Overpass-клиент и `fetch`

**Files:**
- Create: `src/kazan_noise/overpass.py`, `src/kazan_noise/fetch.py`
- Test: `tests/kazan_noise/test_noise_overpass.py`

**Interfaces:**
- Consumes: `NoiseConfig`, `queries.*` (Task 1).
- Produces:
  - `overpass.OverpassError(Exception)`.
  - `overpass.OverpassClient(cfg: NoiseConfig, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep, clock=time.monotonic)` с методами `query(ql: str) -> dict` и `close()`, а также контекстным менеджером.
  - `fetch.fetch_all(client, cfg: NoiseConfig, *, refresh: bool = False) -> list[str]`: имена, скачанные в этом запуске. `client` — любой объект с `query(ql) -> dict`.
  - `fetch.load_raw(cfg: NoiseConfig, name: str) -> dict`.

Поведение `query`: `POST url` с формой `data=<ql>` и заголовком `User-Agent`. Перед каждой попыткой ждать, пока с прошлой пройдёт `min_interval_s`, по образцу `OstrovokHttp._wait_interval` (`src/ostrovok_scraper/http.py`).
- Повторяем: `httpx.TransportError`, ответы 429/502/503/504, тело не JSON (HTML «at capacity»), JSON с `remark`, содержащим `"runtime error"`.
- Код 400 сразу даёт `OverpassError` с первыми 300 символами тела, без повторов.
- На один URL приходится `1 + len(retry_delays_s)` попыток с паузами `retry_delays_s`, потом следующий URL с новыми паузами. Когда кончились все URL — `OverpassError`.

`fetch_all` идёт по `QUERY_NAMES` по порядку. Bbox строится из `bounds` элемента-границы (читается из уже сохранённого `boundary`) через `bbox_with_buffer(…, cfg.source_buffer_m)`. Файл `raw_dir/<name>.json.gz` пишется в `<name>.json.gz.tmp` и переименовывается через `os.replace`. Существующие файлы пропускаются, если не задан `refresh`.

- [ ] **Step 1: Write the failing tests** (`make_client(responses)` по образцу `make_http` из `tests/ostrovok_scraper/test_http.py`, `FakeTime` из `noise_helpers`)

```python
def test_retries_capacity_html_then_returns_json():
    client, seen, t = make_client([httpx.Response(502), httpx.Response(200, text="<html>at capacity</html>"),
                                   httpx.Response(200, json={"elements": []})])
    assert client.query("[out:json];") == {"elements": []}
    assert t.sleeps == [15.0, 30.0]

def test_timeout_remark_with_partial_data_is_retried():
    partial = {"elements": [{"type": "way", "id": 1}], "remark": 'runtime error: Query timed out in "query" at line 3 after 301 seconds.'}
    client, seen, _ = make_client([httpx.Response(200, json=partial), httpx.Response(200, json={"elements": []})])
    assert client.query("q") == {"elements": []} and len(seen) == 2

def test_bad_query_fails_without_retry():
    client, seen, _ = make_client([httpx.Response(400, text="parse error: line 1")])
    with pytest.raises(OverpassError, match="parse error"):
        client.query("q")
    assert len(seen) == 1

def test_switches_to_mirror_after_retries_exhausted():
    client, seen, _ = make_client([httpx.Response(504)] * 5 + [httpx.Response(200, json={"elements": []})])
    client.query("q")
    assert [r.url.host for r in seen] == ["overpass-api.de"] * 5 + ["maps.mail.ru"]

def test_waits_min_interval_between_queries():
    client, _, t = make_client([httpx.Response(200, json={})] * 2)
    client.query("a"); client.query("b")
    assert t.sleeps == [10.0]

def test_fetch_all_saves_files_and_skips_existing(tmp_path):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    fake = FakeOverpass()            # граница с bounds из test_bbox_buffer_is_3_km, остальные — пустые elements; запоминает ql
    assert fetch_all(fake, cfg) == ["boundary", "buildings", "lines", "areas"]
    assert "(55.5761" in fake.queries[2]                   # bbox = bounds границы − 3 км
    assert fetch_all(fake, cfg) == [] and len(fake.queries) == 4

def test_failed_query_leaves_no_partial_file(tmp_path):
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    with pytest.raises(OverpassError):
        fetch_all(FakeOverpass(fail_on="lines"), cfg)
    assert sorted(p.name for p in cfg.raw_dir.iterdir()) == ["boundary.json.gz", "buildings.json.gz"]
    assert fetch_all(FakeOverpass(), cfg) == ["lines", "areas"]
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_overpass.py -q`. Expected: FAIL (`ImportError`)
- [ ] **Step 3: Implement** `overpass.py`, `fetch.py`
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 3: Геометрия OSM и проекция

**Files:**
- Create: `src/kazan_noise/osm.py`
- Modify: `tests/kazan_noise/noise_helpers.py`
- Test: `tests/kazan_noise/test_noise_osm.py`

**Interfaces:**
- Produces:
  - `osm.element_geometry(el: dict, *, area: bool) -> BaseGeometry | None`. Работает в WGS84, координаты (lon, lat).
  - `osm.project(geoms) -> np.ndarray` (WGS84 → UTM); `osm.project_point(lat: float, lon: float) -> tuple[float, float]`; `osm.unproject_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]` возвращает `(lat, lon)`. Один модульный `pyproj.Transformer` на каждое направление, геометрия проецируется через `shapely.transform`.
  - Хелперы `noise_helpers` возвращают словари в формате Overpass `out geom`: `osm_node(id, lat, lon, tags=None)`, `osm_way(id, lonlat: list[tuple[float, float]], tags=None, timestamp=None)`, `osm_relation(id, members: list[tuple[str, list[tuple[float, float]]]], tags=None, timestamp=None)`, `raw_response(elements, timestamp="2026-10-26T18:41:36Z") -> dict` (с `osm3s.timestamp_osm_base`).

Правила:
- Node даёт `Point`.
- У way выбрасываются `null`-координаты. Меньше 2 точек — `None`.
- При `area=True`: замкнутый way из ≥4 точек даёт `Polygon`. Невалидный проходит через `make_valid`, от результата берётся полигональная часть. Незамкнутый — `None`.
- При `area=False` way даёт `LineString`.
- Relation: члены-ways с ролью `outer`/`""` и `inner` по отдельности идут в `shapely.polygonize`, результат — `outer − union(inner)`. Пустой результат — `None`.

- [ ] **Step 1: Write the failing tests**

```python
SQUARE = [(49.10, 55.79), (49.11, 55.79), (49.11, 55.80), (49.10, 55.80), (49.10, 55.79)]

def test_closed_way_with_area_becomes_polygon():
    assert element_geometry(osm_way(1, SQUARE), area=True).geom_type == "Polygon"

def test_open_way_for_area_is_skipped():
    assert element_geometry(osm_way(1, SQUARE[:-1]), area=True) is None

def test_way_with_null_coords_or_single_point_is_skipped():
    el = osm_way(1, SQUARE[:1]); el["geometry"].append(None)
    assert element_geometry(el, area=False) is None

def test_multipolygon_with_inner_ring_has_hole():
    inner = [(49.103, 55.793), (49.106, 55.793), (49.106, 55.796), (49.103, 55.793)]
    geom = element_geometry(osm_relation(1, [("outer", SQUARE), ("inner", inner)], {"type": "multipolygon"}), area=True)
    assert geom.geom_type == "Polygon" and len(geom.interiors) == 1

def test_multipolygon_outer_split_into_two_ways():
    rel = osm_relation(1, [("outer", SQUARE[:3]), ("outer", SQUARE[2:])], {"type": "multipolygon"})
    assert element_geometry(rel, area=True).area == pytest.approx(0.0001)

def test_broken_multipolygon_returns_none():
    rel = osm_relation(1, [("outer", SQUARE[:3])], {"type": "multipolygon"})
    assert element_geometry(rel, area=True) is None

def test_projection_round_trip():
    x, y = project_point(KAZAN_LAT, KAZAN_LON)
    assert (x, y) == (pytest.approx(KAZAN_X, abs=0.1), pytest.approx(KAZAN_Y, abs=0.1))
    lat, lon = unproject_xy(np.array([x]), np.array([y]))
    assert lat[0] == pytest.approx(KAZAN_LAT, abs=1e-7) and lon[0] == pytest.approx(KAZAN_LON, abs=1e-7)
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_osm.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `osm.py` и хелперы
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 4: Веса и классификация объектов

**Files:**
- Create: `src/kazan_noise/weights.py`, `src/kazan_noise/features.py`
- Test: `tests/kazan_noise/test_noise_features.py`

**Interfaces:**
- Consumes: `element_geometry`, `project` (Task 3); `load_raw`-формат ответов (Task 2).
- Produces:
  - `features.Source`, frozen dataclass: `kind: str` (`road|tram|rail|aircraft|construction|industrial|bar|worship`), `osm_id: str` (`"way/123"`), `name: str | None`, `subtype: str` (класс highway, тип railway, amenity, `runway|corridor`), `geom: BaseGeometry` (UTM), `height: float`, `w_day: float`, `w_night: float`, `radius: float`. Единицы `w_*`: дБ/м у линий, дБ/м² у площадей, дБ у точек.
  - `features.Obstacles(geoms: np.ndarray, heights: np.ndarray, is_building: np.ndarray)`, `features.Green(geoms: np.ndarray, factors: np.ndarray)`.
  - `features.Features(boundary: BaseGeometry, boundary_wgs84: BaseGeometry, obstacles: Obstacles, green: Green, sources: list[Source], osm_timestamp: str)`.
  - `features.BuildReport(counts: Counter[str], skipped: Counter[str])`.
  - `parse_height(tags: dict) -> float | None`, `building_height(tags: dict, area_m2: float) -> float | None` (`None` — не экран), `parse_maxspeed(value: str | None) -> float | None`.
  - `road_source(tags, geom, osm_id) -> Source | None`, `rail_source(tags, geom, osm_id) -> Source | None`, `runway_sources(runway: LineString, aerodrome_tags: dict, osm_id) -> list[Source]`.
  - `build_features(raw: dict[str, dict], *, today: date) -> tuple[Features, BuildReport]`. `raw` — словарь по `QUERY_NAMES`.

`weights.py` (значения из спецификации, раздел «Этап 2», с комментариями):
- `OFF = -100.0` (источник выключен в этом режиме). `C_ROAD = 14.0`: даёт дорогу ≈95 дБ на автомобиль при 50 км/ч в форме `10·lg(Q) + 20·lg(v) + C`. `C_TRAM = C_ROAD + 12`, `C_RAIL = C_ROAD + 18`, `C_AIR = 86.0` (`W' = 10·lg(Q) + C_AIR`).
- `ROADS: dict[str, RoadClass(q_day, q_night, speed, lanes, heavy_db, radius)]`: motorway (3000, 600, 110, 4, 3, 1000), trunk (3000, 600, 80, 4, 3, 1000), primary (1500, 250, 60, 4, 0, 1000), secondary (800, 120, 60, 2, 0, 1000), tertiary (400, 50, 60, 2, 0, 1000), residential и unclassified (60, 8, 40, 2, 0, 300), living_street и service (15, 2, 20, 1, 0, 300). `LINK_FLOW_FACTOR = 0.3` для `*_link`. `MIN_SPEED = 20`.
- `RAILS`: tram, light_rail и subway (10, 2, 40), rail (4, 3, 80), все с radius 1000. `RAIL_MINOR_DB = -10` для `service=siding|yard|spur` или `usage=industrial`.
- `AERODROMES = {"UWKD": (6.0, 2.0), "UWKG": (0.3, 0.0)}`, `AERODROME_DEFAULT = (0.2, 0.0)`, `CORRIDOR_M = 8000`, `GLIDE_TAN = math.tan(math.radians(3))`, `AIRCRAFT_STEP_M = 100`, `AIRCRAFT_RADIUS = 3000`.
- `AREAS` (дБ/м², DIN 18005): construction (65, 45), industrial (60, 55), radius 1000. `AREA_LW_CAP = 115`, `AREA_STEP_M = 20`, `STALE_CONSTRUCTION_DAYS = 3 * 365`.
- `POINTS`: nightclub (85, 95), bar и pub (75, 85), все с radius 150; place_of_worship (80, OFF), radius 300, высота 20.
- Высоты источников: дорога и рельсы 0.5, `BRIDGE_EXTRA_M = 6`, бар 1.5, стройка 3, промзона 5.
- Здания: `LEVEL_M = 3.0`, `ROOF_M = 1.0`, `TYPE_HEIGHTS` по спецификации, `AREA_HEIGHTS = ((60, 3.0), (300, 7.0), (1500, 15.0))`, `AREA_HEIGHT_MAX = 21.0`, `NON_SHIELD = {"roof", "carport", "ruins", "no"}`, `CONSTRUCTION_HEIGHT_M = 3.0`. Стены: noise_barrier 4.0, wall и city_wall 2.5.
- Зелень: forest и wood 1.0, park 0.5. `FOLIAGE_DB_PER_M = 0.05`, `FOLIAGE_MAX_DB = 10.0`, `FOLIAGE_MIN_PATH_M = 10.0`.
- Распространение: `D_MIN_M = 5.0`, `AIR_DB_PER_M = 0.005`, `WAVELENGTH_M = 0.68`, `SHIELD_MAX_SINGLE = 20.0`, `SHIELD_MAX_MULTI = 25.0`, `BACKGROUND_DB = 35.0`, `BASE_STEP_M = 5.0`, `STEPS = ((200, 10), (500, 25), (math.inf, 50))`.
- Модель и сетка: `MAX_FACADE_POINTS = 16`, `FACADE_OFFSET_M = 2.0`, `FACADE_STEP_M = 10.0`, `BUILT_UP_RADIUS_M = 100.0`, `SHIELDED_DB = 5.0`, `MIN_SHARE = 0.01`, `TOP_SOURCES = 3`.

Правила классификации:
- Исключаются: `tunnel` не из (`None`, `"no"`), `covered=yes`, `layer < 0`, `subway` в тоннеле. Неизвестный класс highway пропускается со счётчиком `highway:other`.
- `bridge` не из (`None`, `"no"`) добавляет `BRIDGE_EXTRA_M` к высоте.
- Имя: `name`, а для рельсов, если `name` нет, — `ref`.
- Скорость по `maxspeed`: число; `"N mph"` ×1.609; `RU:urban` 60, `RU:rural` 90, `RU:motorway` 110; `walk` и мусор дают `None` (тогда берётся значение по классу).
- `building=construction` из buildings — экран 3 м. Стройкой-источником он становится из areas, где есть `timestamp`.
- Стройка старше `STALE_CONSTRUCTION_DAYS` пропускается (`construction:stale`). Если `timestamp` нет — остаётся.
- Ways баров и культовых сооружений сводятся к центроиду.
- Runway относится к аэродрому, чей полигон содержит центроид runway; интенсивность берётся по `icao`. Получается три источника: runway (высота 0) и два коридора по `CORRIDOR_M` наружу от торцов. Геометрия коридора начинается у торца, а высоту по глиссаде даёт дискретизация (Task 5).
- `osm_timestamp` берётся из `raw["buildings"]["osm3s"]["timestamp_osm_base"]`. Граница — из элемента relation в `raw["boundary"]`.
- `element_geometry` вернул `None` → счётчик `skipped["geometry"]`. Препятствия идут в порядке ответа: сначала здания, потом стены.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("tags, expected", [({"height": "12"}, 12.0), ({"height": "12 m"}, 12.0), ({"height": "12,5"}, 12.5),
    ({"building:levels": "5"}, 16.0), ({"height": "abc", "building:levels": "2"}, 7.0), ({}, None)])
def test_parse_height(tags, expected): assert parse_height(tags) == expected

@pytest.mark.parametrize("tags, area, expected", [({"building": "garage"}, 20, 3.0), ({"building": "apartments"}, 900, 27.0),
    ({"building": "yes"}, 50, 3.0), ({"building": "yes"}, 200, 7.0), ({"building": "yes"}, 1000, 15.0),
    ({"building": "yes"}, 5000, 21.0), ({"building": "roof"}, 500, None), ({"building": "construction"}, 500, 3.0),
    ({"building": "yes", "building:levels": "9"}, 50, 28.0)])
def test_building_height(tags, area, expected): assert building_height(tags, area) == expected

@pytest.mark.parametrize("value, expected", [("60", 60.0), ("RU:urban", 60.0), ("RU:rural", 90.0), ("RU:motorway", 110.0),
    ("40 mph", pytest.approx(64.36)), ("walk", None), (None, None)])
def test_parse_maxspeed(value, expected): assert parse_maxspeed(value) == expected

def test_trunk_weight_uses_lanes_speed_and_heavy_traffic():
    s = road_source({"highway": "trunk", "lanes": "6", "maxspeed": "80", "name": "проспект Победы"}, LINE, "way/1")
    assert s.w_day == pytest.approx(10 * log10(3000 * 6 / 4) + 20 * log10(80) + 14 + 3)
    assert s.w_night == pytest.approx(10 * log10(600 * 6 / 4) + 20 * log10(80) + 14 + 3)
    assert (s.kind, s.subtype, s.name, s.radius, s.height) == ("road", "trunk", "проспект Победы", 1000, 0.5)

@pytest.mark.parametrize("extra", [{"tunnel": "yes"}, {"layer": "-1"}, {"covered": "yes"}])
def test_underground_roads_are_not_sources(extra):
    assert road_source({"highway": "primary", **extra}, LINE, "way/1") is None

def test_bridge_raises_source_height():
    assert road_source({"highway": "primary", "bridge": "yes"}, LINE, "way/1").height == 6.5

def test_rail_siding_is_10_db_quieter():
    main, siding = (rail_source({"railway": "rail", **t}, LINE, "way/1") for t in ({}, {"service": "siding"}))
    assert main.w_day - siding.w_day == pytest.approx(10.0)

def test_runway_gives_runway_and_two_glide_corridors():
    sources = runway_sources(LineString([(0, 0), (2500, 0)]), {"icao": "UWKD"}, "way/9")
    assert [s.subtype for s in sources] == ["runway", "corridor", "corridor"]
    assert [round(s.geom.length) for s in sources[1:]] == [8000, 8000]
    assert sources[0].w_day == pytest.approx(10 * log10(6) + 86) and sources[0].radius == 3000

def test_build_features_drops_stale_construction_and_counts_layers():
    raw = scene_raw()  # граница; здание+стена+битый мультиполигон-здание; trunk+tram; парк, бар-node, стройки 2022-01-01 и 2026-05-01
    features, report = build_features(raw, today=date(2026, 10, 27))
    assert [s.kind for s in features.sources].count("construction") == 1 and report.skipped["construction:stale"] == 1
    assert report.skipped["geometry"] == 1
    assert features.obstacles.is_building.tolist() == [True, False] and features.osm_timestamp == "2026-10-26T18:41:36Z"
    assert features.boundary.contains(features.obstacles.geoms[0])   # всё в UTM
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_features.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `weights.py`, `features.py` (`scene_raw` — в тестовом файле, на хелперах Task 3)
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 5: Распространение звука

**Files:**
- Create: `src/kazan_noise/propagation.py`
- Test: `tests/kazan_noise/test_noise_propagation.py`

**Interfaces:**
- Consumes: `Source` (Task 4), `weights`.
- Produces:
  - `SubSources`, frozen dataclass из numpy-массивов одной длины: `x, y, z, w_day, w_night, source_idx`; `SubSources.empty()`.
  - `discretize(sources: Sequence[Source], indices: Iterable[int], rx: float, ry: float, *, steps=STEPS) -> SubSources`. Подисточники дальше `source.radius` по горизонтали отбрасываются.
  - `divergence_db(d: np.ndarray) -> np.ndarray` = `20·lg(max(d, D_MIN_M)) + 11`; `air_db(d) = AIR_DB_PER_M · d`.
  - `path_difference(hs: float, hr: float, dist: float, profile: Sequence[tuple[float, float]]) -> tuple[float, int]`: δ в метрах и число вершин оболочки над лучом; `profile` — точки (расстояние от источника, высота препятствия).
  - `shield_db(delta: float, vertices: int) -> float`, `foliage_db(path_m: float) -> float` (путь уже умножен на фактор), `energy_sum(levels: np.ndarray) -> float` (для пустого массива `-inf`).

Дискретизация:
- **Линия:** середины кусков по `BASE_STEP_M`. Для каждой точки шаг полосы Δ выбирается по её расстоянию до приёмника из `steps`. Точка `i` остаётся, если `i % (Δ / BASE_STEP_M) == 0`, а её мощность `W_i = W' + 10·lg(Δ)`.
- **Коридор самолёта:** фиксированный шаг `AIRCRAFT_STEP_M`, `z = height + s · GLIDE_TAN`, где `s` — расстояние от начала линии. У runway `z = 0`.
- **Площадь:** сетка `AREA_STEP_M` со сдвигом на половину шага от `bounds`, отбор `shapely.contains_xy`, `W_i = W'' + 10·lg(AREA_STEP_M²)`. Если в полигон не попала ни одна точка — центроид с `W'' + 10·lg(S)`. Если `W'' + 10·lg(S) > AREA_LW_CAP`, все `W_i` сдвигаются вниз на превышение.
- **Точка:** один подисточник.

`path_difference`: строится верхняя выпуклая оболочка (monotone chain) точек `(0, hs)`, профиля, отсортированного по x, и `(dist, hr)`. δ — длина оболочки минус прямая, вершины — `len(hull) − 2`. `shield_db`: 0, если вершин 0, иначе `min(10·lg(3 + 20·δ/λ), 20 при одной вершине | 25 при ≥2)`.

- [ ] **Step 1: Write the failing tests** (эталоны посчитаны заранее)

```python
def test_divergence():
    assert divergence_db(np.array([10.0, 20.0, 1.0])) == pytest.approx([31.0, 37.0206, 24.9794], abs=1e-3)

def test_energy_sum():
    assert energy_sum(np.array([60.0, 60.0])) == pytest.approx(63.0103, abs=1e-3)
    assert energy_sum(np.array([])) == -math.inf

@pytest.mark.parametrize("profile, delta, vertices, shield", [
    ([(50, 10)], 1.1920, 1, 15.80), ([(50, 1)], 0.0, 0, 0.0), ([(50, 50)], 38.2379, 1, 20.0),
    ([(45, 20), (55, 20)], 6.7419, 2, 23.04), ([(45, 60), (55, 60)], None, 2, 25.0)])
def test_path_difference_and_shield(profile, delta, vertices, shield):
    d, n = path_difference(0.5, 4.0, 100.0, profile)
    assert n == vertices and (delta is None or d == pytest.approx(delta, abs=1e-3))
    assert shield_db(d, n) == pytest.approx(shield, abs=0.01)

@pytest.mark.parametrize("path, expected", [(5.0, 0.0), (100.0, 5.0), (400.0, 10.0)])
def test_foliage(path, expected): assert foliage_db(path) == pytest.approx(expected)

def road(w=0.0, radius=1e9):
    return Source("road", "way/1", None, "primary", LineString([(-4000, 0), (4000, 0)]), 0.0, w, w, radius)

def level(sub, rx, ry):
    d = np.hypot(sub.x - rx, sub.y - ry)
    return energy_sum(sub.w_day - divergence_db(d))

def test_line_source_loses_3_db_per_distance_doubling_and_ignores_step():
    at = lambda ry, steps: level(discretize([road()], [0], 0, ry, steps=steps), 0, ry)
    assert at(20, ((math.inf, 5),)) - at(40, ((math.inf, 5),)) == pytest.approx(3.024, abs=0.05)
    assert at(20, ((math.inf, 5),)) == pytest.approx(at(20, ((math.inf, 10),)), abs=0.2)

def test_default_steps_coarsen_with_distance():
    sub = discretize([road()], [0], 0, 20)
    xs = np.sort(sub.x)
    assert np.diff(xs[np.abs(xs) < 150]).max() == pytest.approx(10) and np.diff(xs[xs > 600]).min() == pytest.approx(50)

def test_radius_drops_far_subsources():
    assert np.abs(discretize([road(radius=300)], [0], 0, 0).x).max() <= 300

def test_area_source_grid_and_cap():
    small = Source("construction", "way/2", None, "construction", box(0, 0, 100, 100), 3.0, 65, 45, 1000)
    big = Source("construction", "way/3", None, "construction", box(0, 0, 1000, 1000), 3.0, 65, 45, 1e9)
    s = discretize([small], [0], 0, 0)
    assert len(s.x) == 25 and s.w_day == pytest.approx(65 + 10 * log10(400))
    assert energy_sum(discretize([big], [0], 0, 0).w_day) == pytest.approx(115.0, abs=0.01)

def test_corridor_rises_on_glide_slope():
    c = Source("aircraft", "way/9", None, "corridor", LineString([(0, 0), (8000, 0)]), 0.0, 90, 85, 1e9)
    sub = discretize([c], [0], 0, 0)
    assert sub.z[np.argmin(np.abs(sub.x - 1000))] == pytest.approx(1000 * math.tan(math.radians(3)), abs=3)
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_propagation.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `propagation.py` (векторно на numpy/shapely; Python-цикл только в `path_difference`)
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 6: Модель в точке

**Files:**
- Create: `src/kazan_noise/model.py`
- Modify: `tests/kazan_noise/noise_helpers.py` (`local_scene(...) -> Features`: граница `box(-5000, -5000, 5000, 5000)`, слои из аргументов)
- Test: `tests/kazan_noise/test_noise_model.py`

**Interfaces:**
- Consumes: `Features` (Task 4), `propagation.*` (Task 5).
- Produces:
  - `Contribution(source_idx: int, day_db: float, night_db: float, min_distance_m: float, mean_shield_db: float)`.
  - `ReceiverResult(x: float, y: float, day_db: float, night_db: float, contributions: list[Contribution])`. Уровни включают `BACKGROUND_DB`; вклады отсортированы по `max(day_db, night_db)` по убыванию.
  - `PointResult(inside_building: bool, loud: ReceiverResult, quiet: ReceiverResult | None)`.
  - `NoiseModel(features: Features, *, receiver_height: float = 4.0)` с атрибутом `features` и методами `receiver(x, y) -> ReceiverResult`, `at(x, y) -> PointResult`, `building_at(x, y) -> int | None` (индекс в `obstacles`, только здания), `facade_points(idx: int) -> np.ndarray` (N×2, N ≤ `MAX_FACADE_POINTS`), `nudge_outside(x, y) -> tuple[float, float]`.

`receiver`:
1. Источники в радиусе `max(source.radius)` через `STRtree.query(Point, predicate="dwithin", distance=…)`.
2. `discretize`.
3. Лучи `shapely.linestrings` от подисточника к приёмнику, пары «луч–препятствие» через `STRtree.query(rays, predicate="intersects")`, `shapely.intersection`.
4. Все вершины пересечений (вход и выход из здания, точки пересечения стен) дают профиль `(расстояние от подисточника, высота)`, затем `path_difference` и `shield_db`.
5. Длина лучей внутри зелени × фактор даёт `foliage_db`.
6. `L_i = W_i − divergence(d3d) − air(d3d) − A_shield − A_green` по каждому режиму. Вклады группируются по `source_idx`, `mean_shield_db` — среднее арифметическое по подисточникам.

`at`: если точка внутри здания — приёмники в `facade_points`. Это кольца `polygon.buffer(FACADE_OFFSET_M)`, включая дворы-дыры, с шагом `max(FACADE_STEP_M, периметр / MAX_FACADE_POINTS)`. `loud` — argmax по `day_db`, `quiet` — argmin. **Собственное здание остаётся экраном**: именно оно даёт тихую сторону (так исправлено в спецификации). `nudge_outside` возвращает ближайшую точку кольца `buffer(FACADE_OFFSET_M)`.

- [ ] **Step 1: Write the failing tests**

```python
PRIMARY = Source("road", "way/1", "ул. Тестовая", "primary", LineString([(-2000, 0), (2000, 0)]), 0.5, 80.0, 72.0, 1000)
TERTIARY = Source("road", "way/2", None, "tertiary", LineString([(-2000, 400), (2000, 400)]), 0.5, 70.0, 60.0, 1000)

def test_no_sources_gives_background():
    r = NoiseModel(local_scene()).receiver(0, 0)
    assert (r.day_db, r.night_db, r.contributions) == (35.0, 35.0, [])

def test_building_shields_road():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 60)
    shielded = NoiseModel(local_scene(sources=[PRIMARY], buildings=[(box(-300, 20, 300, 40), 15.0)])).receiver(0, 60)
    assert shielded.day_db <= open_.day_db - 6

def test_forest_strip_takes_10_db():
    open_ = NoiseModel(local_scene(sources=[PRIMARY])).receiver(0, 220)
    forest = NoiseModel(local_scene(sources=[PRIMARY], green=[(box(-5000, 10, 5000, 210), 1.0)])).receiver(0, 220)
    assert open_.day_db - forest.day_db == pytest.approx(10.0, abs=0.1)

def test_contributions_grouped_sorted_and_sum_to_total():
    r = NoiseModel(local_scene(sources=[PRIMARY, TERTIARY])).receiver(0, 50)
    assert [c.source_idx for c in r.contributions] == [0, 1]
    assert energy_sum(np.array([c.day_db for c in r.contributions] + [35.0])) == pytest.approx(r.day_db, abs=0.01)

def test_point_inside_building_gives_loud_and_quiet_facades():
    m = NoiseModel(local_scene(sources=[PRIMARY], buildings=[(box(-20, 20, 20, 60), 20.0)]))
    p = m.at(0, 40)
    assert p.inside_building and p.loud.y < 25 and p.quiet.y > 55
    assert p.loud.day_db > p.quiet.day_db + 10

def test_facade_points_are_capped_and_outside():
    m = NoiseModel(local_scene(buildings=[(box(0, 0, 400, 400), 20.0)]))
    pts = m.facade_points(0)
    assert len(pts) <= 16 and shapely.distance(box(0, 0, 400, 400), shapely.points(pts)).min() > 1.9

def test_nudge_outside_moves_point_2_m_off_facade():
    m = NoiseModel(local_scene(buildings=[(box(0, 0, 40, 40), 10.0)]))
    x, y = m.nudge_outside(5, 20)
    assert box(0, 0, 40, 40).distance(Point(x, y)) == pytest.approx(2.0, abs=0.05)
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_model.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `model.py`; в спецификации заменить фразу «собственное здание исключается из экранов» на «собственное здание остаётся экраном — оно и даёт тихую сторону»
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 7: Снимок на диске

**Files:**
- Create: `src/kazan_noise/snapshot.py`
- Test: `tests/kazan_noise/test_noise_snapshot.py`

**Interfaces:**
- Consumes: `Features`, `BuildReport` (Task 4).
- Produces:
  - `SnapshotMissingError(Exception)`; `FORMAT_VERSION = 1`.
  - `pack_geoms(geoms) -> tuple[np.ndarray, np.ndarray]` (WKB одним `uint8`-буфером + `int64`-смещения), `unpack_geoms(buf, offsets) -> np.ndarray`.
  - `weights_hash() -> str`: первые 16 hex от sha256 байтов файла `weights.py`.
  - `save_features(features: Features, path: Path) -> None`, `load_features(path: Path) -> Features`, `write_meta(path: Path, features: Features, report: BuildReport) -> None`, `read_meta(path: Path) -> dict`.

Ключи `features.npz`: `obstacles_{wkb,off,height,is_building}`, `green_{wkb,off,factor}`, `src_{wkb,off,kind,osm_id,name,subtype,height,w_day,w_night,radius}`, `boundary_wkb`, `boundary_wgs84_wkb`, `osm_timestamp`, `weights_hash`, `format_version`. Строки хранятся как `np.str_`-массивы, `name=None` ↔ `""`. Загрузка идёт с `allow_pickle=False`. Нет файла, другой `format_version` или `weights_hash` ≠ текущему → `SnapshotMissingError("Снимок шума … пересоберите: kazan-noise build && kazan-noise grid")`. `meta.json` содержит `format_version`, `osm_timestamp`, `weights_hash`, `built_at` (ISO), `counts`, `skipped`.

- [ ] **Step 1: Write the failing tests**

```python
def test_features_round_trip(tmp_path):
    road = Source("road", "way/1", None, "primary", LineString([(-2000, 0), (2000, 0)]), 0.5, 80.0, 72.0, 1000)
    f = local_scene(sources=[road], buildings=[(box(0, 0, 10, 10), 9.0)], green=[(box(50, 50, 90, 90), 0.5)])
    save_features(f, tmp_path / "f.npz")
    g = load_features(tmp_path / "f.npz")
    assert shapely.equals_exact(g.obstacles.geoms, f.obstacles.geoms, 1e-6).all()
    assert g.sources == f.sources and g.sources[0].name is None and g.osm_timestamp == f.osm_timestamp

def test_missing_snapshot(tmp_path):
    with pytest.raises(SnapshotMissingError, match="kazan-noise build"):
        load_features(tmp_path / "nope.npz")

def test_weights_changed_after_build(tmp_path, monkeypatch):
    save_features(local_scene(), tmp_path / "f.npz")
    monkeypatch.setattr(snapshot, "weights_hash", lambda: "другой")
    with pytest.raises(SnapshotMissingError, match="пересоберите"):
        load_features(tmp_path / "f.npz")
```

(`Source` — frozen dataclass с shapely-геометрией. Для `==` в тесте `__eq__` сравнивает геометрию через `equals_exact(…, 1e-6)`, либо тест сравнивает поля по отдельности.)

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_snapshot.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `snapshot.py`
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 8: Шкала и H3-сетка

**Files:**
- Create: `src/kazan_noise/scale.py`, `src/kazan_noise/grid.py`
- Modify: `tests/kazan_noise/noise_helpers.py` (`kazan_snapshot(data_dir) -> Features`: сцена 2×2 км вокруг `KAZAN_X, KAZAN_Y` с primary-дорогой и парой зданий; `boundary_wgs84` через `unproject_xy`; сохраняет `features.npz`. Константы (lat, lon) для Task 9: `NEAR_ROAD` — 15 м от дороги, `FAR_FROM_ROAD` — 800 м, `ON_BUILDING` — центр здания у дороги)
- Test: `tests/kazan_noise/test_noise_grid.py`

**Interfaces:**
- Consumes: `NoiseModel` (Task 6), `load_features`, `save_features`, `weights_hash`, `SnapshotMissingError` (Task 7), `unproject_xy`, `project` (Task 3).
- Produces:
  - `scale.label(score: int) -> str`; `scale.Scale(day_levels: np.ndarray, night_levels: np.ndarray)` с методами `score(level: float, mode: str) -> int` и `Scale.from_grid(grid: Grid) -> Scale` (только `built_up`; если их нет — `ValueError`).
  - `grid.Grid(cells: np.ndarray, x: np.ndarray, y: np.ndarray, built_up: np.ndarray, day_db: np.ndarray, night_db: np.ndarray)`.
  - `grid.grid_cells(boundary_wgs84, res: int) -> list[str]` (`h3.geo_to_cells`, отсортировано).
  - `grid.compute_grid(features_path: Path, cfg: NoiseConfig, *, workers: int, limit: int | None = None) -> Grid`.
  - `grid.save_grid(grid: Grid, path: Path) -> None`, `grid.load_grid(path: Path) -> Grid`. Хранит `weights_hash`; при расхождении, как и при отсутствии файла, — `SnapshotMissingError` с текстом «запустите kazan-noise grid».

`score`: `round(100 · searchsorted(sorted, level, side="left") / N)`. `compute_grid`:
- Центры ячеек (`h3.cell_to_latlng`) проецируются в UTM.
- `built_up` — здание в пределах `BUILT_UP_RADIUS_M`.
- Центр внутри здания сдвигается через `nudge_outside`.
- Расчёт идёт кусками по 256 точек в `ProcessPoolExecutor(max_workers=workers, initializer=…)`: initializer один раз грузит `load_features` и создаёт `NoiseModel` в процессе. При `workers=1` — в текущем процессе.

- [ ] **Step 1: Write the failing tests**

```python
def test_score_is_share_of_strictly_quieter_cells():
    s = Scale(np.array([40.0, 50, 60, 70]), np.array([40.0, 50, 60, 70]))
    assert [s.score(v, DAY) for v in (30, 50, 55, 80)] == [0, 25, 50, 100]

@pytest.mark.parametrize("score, text", [(0, "очень тихо"), (19, "очень тихо"), (20, "тихо"), (59, "средне"),
                                         (60, "шумно"), (80, "очень шумно"), (100, "очень шумно")])
def test_labels(score, text): assert label(score) == text

def test_scale_ignores_cells_that_are_not_built_up():
    g = Grid(np.array(["a", "b"]), np.zeros(2), np.zeros(2), np.array([True, False]), np.array([50.0, 10]), np.array([40.0, 5]))
    assert Scale.from_grid(g).score(20, DAY) == 0

def test_grid_cells_cover_1_km2_at_res_10():
    cells = grid_cells(box(49.10, 55.79, 49.116, 55.799), 10)      # ≈1 км²
    assert 50 <= len(cells) <= 80 and {h3.get_resolution(c) for c in cells} == {10}

def test_compute_grid_marks_built_up_and_round_trips(tmp_path):
    kazan_snapshot(tmp_path)
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    grid = compute_grid(cfg.features_path, cfg, workers=1, limit=40)
    assert len(grid.cells) == 40 and grid.built_up.any() and not grid.built_up.all()
    assert (grid.day_db >= 35.0).all()
    save_grid(grid, cfg.grid_path)
    assert load_grid(cfg.grid_path).cells.tolist() == grid.cells.tolist()
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_grid.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `scale.py`, `grid.py`. При `limit` берутся первые `limit` отсортированных ячеек; в `kazan_snapshot` здания расставлены так, чтобы среди первых 40 были и застроенные, и нет.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 9: Сервис и CLI `kazan-noise`

**Files:**
- Create: `src/kazan_noise/service.py`, `src/kazan_noise/cli.py`
- Test: `tests/kazan_noise/test_noise_service.py`, `tests/kazan_noise/test_noise_cli.py`

**Interfaces:**
- Consumes: всё выше; `fetch_all`, `load_raw`, `OverpassClient` (Task 2); `build_features` (Task 4).
- Produces:
  - `OutsideKazanError(Exception)`; pydantic-модели `ModeScore(score: int, label: str)`, `QuietSide(day: ModeScore, night: ModeScore)`, `SourceInfo(kind: str, subtype: str, name: str | None, distance_m: int, share_day: float, share_night: float, shielded: bool)`, `NoiseReport(lat: float, lon: float, inside_building: bool, day: ModeScore, night: ModeScore, quiet_side: QuietSide | None, main_sources: list[SourceInfo], model_level_day: float, model_level_night: float, osm_timestamp: str)`.
  - `NoiseService(model: NoiseModel, scale: Scale, osm_timestamp: str)` с методами `NoiseService.load(data_dir: Path, *, receiver_height: float = 4.0) -> NoiseService` и `noise_at(lat: float, lon: float) -> NoiseReport`.
  - `cli.build_parser() -> argparse.ArgumentParser`, `cli.main(argv: list[str] | None = None) -> None`. Подкоманды: `fetch [--refresh]`, `build [--today YYYY-MM-DD]`, `grid [--workers N] [--limit N]`, `query --lat --lon [--json]`; `validate` добавит Task 11.

`noise_at`:
- Точка проверяется по `features.boundary.covers` (точка на линии границы считается внутри).
- Вне границы — `OutsideKazanError("Точка вне городского округа Казань")`. Если с переставленными lat/lon точка попадает внутрь, к тексту добавляется «; похоже, перепутаны lat и lon».
- Доли: `share = 10^(contrib/10) / 10^(total/10)`, округление до 0.01.
- `main_sources` — до `TOP_SOURCES` вкладов с `max(share_day, share_night) ≥ MIN_SHARE`.
- `shielded = mean_shield_db > SHIELDED_DB`.

CLI:
- `build` печатает `counts` и `skipped`.
- `grid` печатает число ячеек, застроенных и время; по умолчанию `--workers` = `os.cpu_count()`.
- `query` печатает человекочитаемую сводку или `model_dump_json(indent=2)`, а в stderr — время загрузки снимка и расчёта точки.
- `SnapshotMissingError`, `OverpassError` → сообщение в stderr и `sys.exit(1)`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def service(tmp_path):
    kazan_snapshot(tmp_path)
    cfg = NoiseConfig(_env_file=None, data_dir=tmp_path)
    save_grid(compute_grid(cfg.features_path, cfg, workers=1), cfg.grid_path)
    return NoiseService.load(tmp_path)

def test_point_near_road_is_louder_and_explained(service):
    near, far = service.noise_at(*NEAR_ROAD), service.noise_at(*FAR_FROM_ROAD)   # из noise_helpers
    assert near.day.score > far.day.score and near.main_sources[0].kind == "road"
    assert 0 < near.main_sources[0].share_day <= 1 and json.loads(near.model_dump_json())["day"]["label"]

def test_outside_kazan(service):
    with pytest.raises(OutsideKazanError, match="вне городского округа"):
        service.noise_at(55.0, 49.0)

def test_swapped_lat_lon_hint(service):
    with pytest.raises(OutsideKazanError, match="перепутаны lat и lon"):
        service.noise_at(KAZAN_LON, KAZAN_LAT)

def test_point_on_building_has_quiet_side(service):
    r = service.noise_at(*ON_BUILDING)
    assert r.inside_building and r.quiet_side is not None and r.quiet_side.day.score <= r.day.score

def test_missing_grid_is_reported(tmp_path):
    kazan_snapshot(tmp_path)
    with pytest.raises(SnapshotMissingError, match="kazan-noise grid"):
        NoiseService.load(tmp_path)

# test_noise_cli.py
def test_query_json(service_dir, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(service_dir))
    main(["query", "--lat", str(NEAR_ROAD[0]), "--lon", str(NEAR_ROAD[1]), "--json"])
    assert "model_level_day" in json.loads(capsys.readouterr().out)

def test_query_without_snapshot_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["query", "--lat", "55.79", "--lon", "49.11"])
    assert exc.value.code == 1 and "kazan-noise build" in capsys.readouterr().err
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_service.py tests/kazan_noise/test_noise_cli.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `service.py`, `cli.py`. Фикстуры вынести в `tests/kazan_noise/conftest.py`: `service_dir` (снимок + сетка в `tmp_path`), `service` (`NoiseService.load(service_dir)`), `service_grid` (`load_grid(service_dir / "grid.npz")`) — их использует Task 11.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 10: Tool агента и README

**Files:**
- Create: `src/hotel_agent/tools/noise.py`, `tests/test_noise_tool.py`
- Modify: `src/hotel_agent/tools/__init__.py`, `src/hotel_agent/graph/context.py`, `src/hotel_agent/cli.py`, `tests/test_graph.py`, `README.md`

**Interfaces:**
- Consumes: `NoiseService`, `NoiseReport`, `OutsideKazanError` (Task 9), `SnapshotMissingError` (Task 7), `NoiseConfig` (Task 1).
- Produces:
  - `NoiseArgs(lat: float, lon: float)` с описаниями «Широта точки в Казани (WGS84), например 55.7963» / «Долгота…, например 49.1088».
  - `noise_in_kazan`: `@tool(args_schema=NoiseArgs)`, `async (runtime: ToolRuntime[AgentContext, AgentState], **params) -> str`. Докстринг — описание для LLM: что такое балл, что это не децибелы, день 07–23 и ночь 23–07, тихая сторона для точки на здании.
  - `report_for_llm(report: NoiseReport) -> str`.
  - `NOISE_UNAVAILABLE = "Оценка шума недоступна: данные не подготовлены (uv run kazan-noise fetch, build, grid)."`.
  - `AgentContext.noise: NoiseService | None = None`; `TOOLS = [search_hotels_in_nyc, noise_in_kazan]`.

`report_for_llm` отдаёт JSON (`ensure_ascii=False`) с ключами `day`, `night`, `inside_building`, `quiet_side` (только если не `None`), `main_sources`, `scale` и `data`:
- `scale`: «0–100: доля застроенных точек Казани, где тише (100 — громче всех). Относительная оценка по модели OSM, не децибелы.»
- `data`: `f"OpenStreetMap на {дата из osm_timestamp}, © OpenStreetMap contributors"`.

Tool вызывает `await asyncio.to_thread(ctx.noise.noise_at, args.lat, args.lon)`. `ctx.noise is None` → `NOISE_UNAVAILABLE`; `OutsideKazanError` → `f"Оценка шума не выполнена: {exc}"`.

В `hotel_agent/cli.py` функция `_load_noise() -> NoiseService | None` пробует `NoiseService.load(NoiseConfig().data_dir)`. При `SnapshotMissingError` печатает `Оценка шума отключена: …` и возвращает `None`; результат передаётся в `AgentContext(noise=…)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_noise_tool.py
def test_report_for_llm_hides_model_levels_and_credits_osm():
    out = json.loads(report_for_llm(REPORT))          # NoiseReport без quiet_side
    assert out["day"] == {"score": 81, "label": "очень шумно"} and "quiet_side" not in out
    assert not any(k.startswith("model_level") for k in out) and "© OpenStreetMap contributors" in out["data"]

# tests/test_graph.py: в _run(...) добавить параметр noise=None → AgentContext(..., noise=noise)
def noise_call(lat=55.7963, lon=49.1088):
    return AIMessage("", tool_calls=[{"name": "noise_in_kazan", "args": {"lat": lat, "lon": lon}, "id": "call_n"}])

async def test_noise_tool_returns_scores_to_agent(run):
    llm = ScriptedChatModel(responses=[noise_call(), AIMessage("Ночью шумно")])
    await run("Шумно ли тут?", llm=llm, extractor=FakeExtractor(NO_DATES), api=BookingAPI(), noise=FakeNoise())
    assert json.loads(tool_result(llm).content)["night"]["score"] == 64

async def test_noise_tool_without_snapshot(run):
    llm = ScriptedChatModel(responses=[noise_call(), AIMessage("Не могу оценить")])
    await run("Шумно ли тут?", llm=llm, extractor=FakeExtractor(NO_DATES), api=BookingAPI())
    assert tool_result(llm).content.startswith("Оценка шума недоступна")

async def test_noise_tool_outside_kazan(run):
    llm = ScriptedChatModel(responses=[noise_call(55.0, 37.6), AIMessage("Это не Казань")])
    await run("А в Москве?", llm=llm, extractor=FakeExtractor(NO_DATES), api=BookingAPI(), noise=FakeNoise(outside=True))
    assert "вне городского округа" in tool_result(llm).content
```

`REPORT` (в `test_noise_tool.py`) и `FakeNoise(outside=False)` (в `test_graph.py`) — `NoiseReport` с `day=ModeScore(81, "очень шумно")`, `night=ModeScore(64, "шумно")`, одной дорогой в `main_sources`, `osm_timestamp="2026-10-26T18:41:36Z"`. `FakeNoise.noise_at` отдаёт его или, при `outside=True`, бросает `OutsideKazanError("Точка вне городского округа Казань")`.

Обновить существующий `test_agent_gets_today_extracted_intent_and_booking_tool` (`tests/test_graph.py:141`): `assert llm.bound_tools == ["search_hotels_in_nyc", "noise_in_kazan"]`.

- [ ] **Step 2: Run** `uv run pytest tests/test_noise_tool.py tests/test_graph.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** tool, контекст, `TOOLS`, `_load_noise`. В README добавить раздел «Шум в Казани»: команды `fetch/build/grid/query/validate`, что такое балл, ограничения модели из спецификации, атрибуция ODbL, напоминание, что в промпт агента (`graph/prompt.py`, сейчас «New York City only») пользователь сам добавляет Казань и tool шума.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 11: Проверка по отзывам островка и санити-проверки

**Files:**
- Create: `src/kazan_noise/validate.py`
- Modify: `src/kazan_noise/cli.py` (подкоманда `validate`)
- Test: `tests/kazan_noise/test_noise_validate.py`

**Interfaces:**
- Consumes: `NoiseService`, `OutsideKazanError` (Task 9), `load_grid` (Task 8); `ostrovok_scraper.config.ScraperConfig().db_dsn`, `ostrovok_scraper.store.connect`.
- Produces:
  - `EXTERNAL_NOISE`, `INTERNAL_NOISE: re.Pattern`; `is_noise_complaint(minus: str | None) -> bool` = внешний шум найден **и** внутреннего нет.
  - `spearman(a: Sequence[float], b: Sequence[float]) -> float` (средние ранги для равных значений, numpy).
  - `HotelRow(hotel_id: str, name: str, lat: float, lon: float, reviews: int, complaints: int)`; `load_hotels(conn, *, min_reviews: int = 10) -> list[HotelRow]`.
  - `ValidationReport(n: int, rho_day: float, rho_night: float, worst: list[tuple[HotelRow, int]])`; `validate_hotels(service, rows: list[HotelRow]) -> ValidationReport`. `worst` — топ-10 по |ранг ночного балла − ранг доли жалоб|.
  - `SanityCheck(name: str, n: int, median_score: float | None, expected: str, ok: bool | None)`; `sanity_checks(service, grid: Grid, *, sample: int = 50, seed: int = 0) -> list[SanityCheck]`.

Регэкспы — из спецификации (Этап 6): `EXTERNAL_NOISE` ловит «шум… улиц/дорог/трасс/окн» в пределах 30 символов в любом порядке, «трамва», «машин», «музык», «бар», «клуб», «стройк», «поезд», «электрич», «самол»; `INTERNAL_NOISE` — «стен», «сосед», «коридор», «лифт». SQL: `ostrovok.hotels JOIN ostrovok.reviews USING (hotel_id)`, `lat/lon IS NOT NULL`, `GROUP BY hotel_id HAVING count(*) >= %s`, `array_agg(r.minus)`. Жалобы считаются в Python, отели вне Казани пропускаются.

Санити (точки выбираются программно, `numpy.random.default_rng(seed)`):
- «у магистрали»: середина случайного куска trunk/motorway/primary, сдвиг на 15 м по нормали, вне здания. Ожидание: медиана дневного балла ≥ 75.
- «двор»: застроенные ячейки сетки, где ближайший road/tram/rail класса tertiary и выше дальше 150 м, а здание высотой ≥15 м ближе 30 м. Ожидание: ≤ 50.
- «лес»: `representative_point` у `green` с фактором 1 и непустым `buffer(-300)`. Ожидание: ≤ 20.
- Меньше 5 точек → `ok=None` («мало точек»).

`kazan-noise validate` сначала печатает санити. Затем, если БД доступна и подходящие отели есть, — ρ день/ночь и `worst`; иначе — «Отзывы островка недоступны: …». Код выхода 1, если какая-то санити-проверка дала `ok is False`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text, expected", [("Шумно, окна на дорогу", True), ("Слышно трамвай с утра", True),
    ("Музыка из бара до 3 ночи", True), ("Тонкие стены, слышно соседей", False),
    ("Шумно от соседей сверху и трамваев", False), ("Всё отлично", False), (None, False), ("", False)])
def test_is_noise_complaint(text, expected): assert is_noise_complaint(text) is expected

def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.8660, abs=1e-4)

def test_validate_hotels_skips_points_outside_kazan():
    rows = [HotelRow("a", "A", 55.79, 49.11, 20, 8), HotelRow("b", "B", 55.80, 49.12, 30, 1),
            HotelRow("c", "C", 55.81, 49.13, 15, 3), HotelRow("x", "X", 55.0, 37.6, 40, 0)]
    report = validate_hotels(FakeScores({(55.79, 49.11): 90, (55.80, 49.12): 10, (55.81, 49.13): 50}), rows)   # прочие → OutsideKazanError
    assert report.n == 3 and report.rho_night == pytest.approx(1.0)

def test_sanity_checks_return_three_named_checks(service, service_grid):
    assert [c.name for c in sanity_checks(service, service_grid)] == ["у магистрали", "двор", "лес"]
```

- [ ] **Step 2: Run** `uv run pytest tests/kazan_noise/test_noise_validate.py -q`. Expected: FAIL
- [ ] **Step 3: Implement** `validate.py` и подкоманду (`sanity_checks` берёт сцену из `service.model.features`; `NoiseService` хранит `model` атрибутом)
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS

---

### Task 12: Живой прогон и подстройка весов

**Files:**
- Modify: `src/kazan_noise/weights.py` (если санити или ρ плохие), `docs/superpowers/specs/2026-10-26-kazan-noise-design.md` (раздел «Итоги прогона»)

- [ ] **Step 1:** `uv run kazan-noise fetch`. Expected: 4 файла в `data/kazan_noise/raw/`, это 5–20 мин при занятом overpass-api.de. При обрыве перезапуск докачивает остальное.
- [ ] **Step 2:** `uv run kazan-noise build`. Expected: зданий ≈102 тыс., источников-дорог ≈34 тыс. ways; `skipped` — единицы процентов. Если пропущено больше 5 % зданий — разобрать причины до продолжения.
- [ ] **Step 3:** `uv run kazan-noise grid`. Expected: ≈40 тыс. ячеек за <30 мин. Если медленнее — `KAZAN_NOISE_H3_RES=9` и записать это в итоги.
- [ ] **Step 4:** `time uv run kazan-noise query --lat 55.7963 --lon 49.1088 --json`. Expected: загрузка снимка <5 с, расчёт точки <1 с (`query` печатает оба времени в stderr), главные источники — реальные улицы рядом.
- [ ] **Step 5:** `docker compose up -d && uv run kazan-noise validate`. Expected: все три санити `ok`, ρ_night > 0. Иначе править `weights.py` по `worst`, затем `build` → `grid` → `validate`.
- [ ] **Step 6:** `uv run hotel-agent`, вопрос «Насколько шумно ночью в точке 55.7963, 49.1088?». Expected: агент вызывает `noise_in_kazan` и отвечает баллом, меткой и источниками (если промпт пользователя ещё про Нью-Йорк, агент может отказаться — это отметить, промпт не править).
- [ ] **Step 7:** Записать в спецификацию «Итоги прогона»: дата OSM, размеры, время шагов, ρ, правки весов. `uv run pytest -q` → PASS.
