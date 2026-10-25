# Геоагент для Казани

Минимальный агент на LangGraph: модель выбирает инструменты, получает реальные
данные OpenStreetMap и формулирует ответ с источниками. Специальных инструментов
для отелей пока нет.

## Запуск

Нужны Python 3.11+ и [uv](https://docs.astral.sh/uv/).

```bash
uv sync --python 3.12
cp .env.example .env
# Заполнить LLM_API_KEY в .env
uv run python agent.py
uv run python agent.py 'Найди парки и крупные дороги рядом с 55.7961, 49.1064' --stream
```

В `.env` можно изменить `LLM_MODEL` (по умолчанию `gpt-4.1`),
`LLM_API_URL` для совместимого API (по умолчанию `https://api.openai.com/v1`,
без `/chat/completions`), `OVERPASS_URL` (базовый URL с `/api`,
без `/interpreter`), `REQUEST_TIMEOUT` в секундах и `CACHE_DIR`.
Модель должна поддерживать tool calling. Внешним API нужен доступ в интернет.

## LangGraph CLI и Studio

После настройки `.env` запустите локальный сервер:

```bash
uv run langgraph dev
```

Откройте ссылку на Studio из вывода команды и выберите граф `geo_agent`.
Конфигурация находится в `langgraph.json`, точка входа — `langgraph_app.py`.
Локальное состояние сервера хранится в `.langgraph_api/` и не коммитится.

## Инструменты

- `search_osm(lat, lon, tags, radius_m, limit)` — OSMnx: объекты в радиусе
  до 5 км, OSM-теги, ссылки, расстояния до геометрии по прямой в метрах.
  Например, `{"leisure": "park"}` или `{"highway": ["primary", "secondary"]}`.
  Несколько ключей тегов объединяются через **OR**.
- `overpass_query(query, limit)` — произвольный Overpass QL с JSON-выводом.
  Можно искать объекты и получать информацию по OSM ID. Запросы ограничивайте
  небольшим радиусом или bbox; `limit` ограничивает выдачу модели, а не серверный
  объём запроса. При наличии `remark` результат считается неполным и возвращается ошибка.

Прямой вызов без LLM и ключа:

```bash
uv run python - <<'PY'
from geo_tools import search_osm, overpass_query
print(search_osm.invoke({
    "lat": 55.7961, "lon": 49.1064,
    "tags": {"leisure": "park"}, "radius_m": 500,
}))
print(overpass_query.invoke({
    "query": '[out:json][timeout:60];nwr(around:500,55.7961,49.1064)[highway=primary];out center tags;'
}))
PY
```

Граф: `START → agent → tools → agent → … → END`. Есть `invoke` и `stream`;
программно агент доступен через `from agent import build_agent`.
Ошибки инструментов передаются модели, успешные результаты содержат
`source`, `endpoint`, `retrieved_at`, `cache_hit`. Выдача ограничена 30 объектами
по умолчанию (до 100); поля `total` и `truncated` показывают усечение.

## Файловый кэш

Всё хранится в `.cache/` (или `CACHE_DIR`), не коммитится:

- `http.sqlite` — успешные HTTP GET/POST через requests, включая Overpass;
- `osmnx/` — штатный кэш сырых ответов OSMnx;
- `features/`, `overpass/` — обработанные результаты с исходной датой получения.

Повторный идентичный геозапрос берётся с диска, включая после перезапуска.
Ключ включает сервер, параметры и теги. Ошибки не сохраняются как результаты.
Автоматического срока истечения пока нет: дата в ответе позволяет увидеть возраст
данных. Для полного обновления удалить каталог `CACHE_DIR` при остановленном агенте.
Вызовы LLM не кэшируются. Подложки карты пока нет, тайлы не скачиваются;
будущие загрузки через requests будут попадать в общий HTTP-кэш.

Расстояния не являются пешими маршрутами. Пустая выдача означает отсутствие
данных в OSM, а не отсутствие объекта. Близость дороги не подтверждает шум в номере.

Документация: [LangGraph](https://docs.langchain.com/oss/python/langgraph/quickstart),
[OSMnx](https://osmnx.readthedocs.io/en/stable/user-reference.html).
Данные © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
