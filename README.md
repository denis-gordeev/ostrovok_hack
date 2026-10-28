# hotel-agent

LangGraph-агент подбора отелей в Татарстане по снимку ostrovok.ru с оценкой шума в Казани по OpenStreetMap.

## Запуск

```bash
uv sync
cp .env.example .env                  # заполнить ключ LLM
uv run hotel-agent                    # чат в терминале
uv run pytest                         # тесты (без сети)
```

Агенту нужны данные: снимок островка в Postgres и снимок шума — см. «Данные островка» и «Шум в Казани» ниже.
Без них агент запускается, но соответствующий tool отвечает, что поиск или оценка недоступны.

## Граф

```
START → agent ─┬─ вызов tool → tools (ToolNode) → agent
               └─ ответ пользователю → END
```

- `agent` — LLM (`init_chat_model(LLM_MODEL)`) с привязанными tools; сегодняшняя дата (по Москве)
  попадает в её системный промпт. Агент сам решает: уточнить у пользователя или вызвать tool.
- Извлечение интентов (`src/hotel_agent/intents/`, LLM со structured output) сейчас отключено: код и тесты
  остались, но в граф узел не подключён. Включить обратно — узел `extract_intent` перед `agent`
  (`graph/builder.py`, `graph/nodes.py`), поле `intent` в `AgentState`, `intent_extractor` в `AgentContext`
  и `IntentExtractor` в `cli.py`.
- `search_hotels_in_tatarstan` — tool: подбор отелей Татарстана по снимку островка в Postgres
  (`OSTROVOK_DB_DSN`, см. «Данные островка» ниже). На вход — даты, город, взрослые, возрасты детей, номера,
  бюджет за ночь, звёзды, оценка, типы размещения, питание, бесплатная отмена, оплата на месте, удобства,
  точка и радиус, расстояние до метро, часть названия, сортировка. На выход — отели, свободные на все ночи,
  с ценой за период и самым дешёвым подходящим тарифом каждого номера; кандидаты кладутся в
  `state["hotel_candidates"]`. Цена номера на ночь = минимальная цена ночи × `price_ratio` тарифа опорной
  ночи того же типа (будни/выходные) и того же числа гостей; дети младше `free_child_age` отеля бесплатны;
  гости делятся по номерам поровну. Схемы запроса и ответа — `src/hotel_agent/apis/ostrovok/models.py`.
- История хранится в checkpointer по `thread_id`.

## Трасса агента

`uv run hotel-agent` печатает в терминал (stderr) каждый вызов LLM и tool, а в конце хода — итог:

```
  [llm 1] deepseek/deepseek-v4-flash-0731  3.2s  in 6 410 · out 412 (размышл. 318) → search_hotels_in_tatarstan
  [tool] search_hotels_in_tatarstan  0.4s  {"checkin": "2026-11-01", "checkout": "2026-11-03", "city": "Казань"} → 18 342 симв.
  [llm 2] deepseek/deepseek-v4-flash-0731  4.1s  in 12 300 · out 1 020 (размышл. 260) → ответ
  ── ход: LLM ×2, tools ×1 · in 18 710 · out 1 432 (размышл. 578) · 7.9s
```

- `in` / `out` — токены входа и выхода вызова; `размышл.` — сколько из выходных ушло на размышление
  (reasoning). `н/д` — провайдер это число не сообщил (не путать с 0).
- После `→` у LLM — какие tools она вызвала (`×N` — несколько вызовов одного tool) или `ответ`.
- У tool — аргументы от LLM (в терминале обрезаны до 200 символов), время и размер ответа в символах:
  этот текст уйдёт во вход следующего вызова LLM. Ошибка tool показывается вместо размера.

Полная трасса пишется в `traces/<время запуска>.jsonl`, по JSON-строке на событие: `llm` (модель, время,
`input_tokens`, `output_tokens`, `reasoning_tokens`, `cache_read_tokens`, `tool_calls`), `tool` (`name`, `args`
целиком, `status`, `result_chars` или `error`) и `turn` (итог хода с текстом пользователя). Номер хода — поле `turn`.
Настройки: `AGENT_TRACE=false` выключает трассу, пустой `AGENT_TRACE_DIR` оставляет её только в терминале.
Код — `src/hotel_agent/tracing.py` (callback LangChain, подключается в `cli.ask`).

## Куда вносить своё

| Что | Где |
|---|---|
| Промпт агента | `src/hotel_agent/graph/prompt.py` (`{current_date}`) |
| Pydantic-схема интентов (сейчас отключено) | `src/hotel_agent/intents/schema.py` |
| Промпт извлечения интентов (сейчас отключено) | `src/hotel_agent/intents/prompt.py` (`{today}`) |
| Фильтры поиска по Татарстану | поле в `StaySearch` (`src/hotel_agent/apis/ostrovok/models.py`) + проверка в `_hotel_matches` или `_offer_matches` (`search.py`) |
| Новый API | клиент в `src/hotel_agent/apis/<name>/`, tool в `src/hotel_agent/tools/`, добавить в `TOOLS`, зависимость в `graph/context.py` |

## Данные островка (Татарстан)

Отдельный пакет `src/ostrovok_scraper/`: сбор отелей Татарстана с внутреннего API ostrovok.ru (статика, цены по ночам, тарифы на 1–8 гостей, отзывы) в локальный Postgres. Сырые ответы лежат в схеме `raw`, нормализованные таблицы — в `ostrovok`. Дизайн: `docs/superpowers/specs/2026-10-26-ostrovok-scrape-design.md`.

```bash
docker compose up -d                                       # Postgres (порт 5433)
uv run ostrovok-scrape db-init                             # миграции
uv run ostrovok-scrape all --limit-hotels 5 --nights 2     # пробный прогон
uv run ostrovok-scrape all                                 # полный сбор (≈1 ч)
uv run ostrovok-scrape normalize                           # пересобрать ostrovok из raw
uv run ostrovok-scrape report                              # сводка по raw и ostrovok
```

Отдельные шаги: `serp`, `serp-guests`, `hotels`, `child-age`, `rates`, `reviews`. Общие флаги: `--retry-failed`, `--limit-hotels N`, `--nights N`, `--today YYYY-MM-DD`. Сбор докачивается: перезапуск пропускает уже сохранённые ключи. При блокировке сайтом процесс завершается с кодом 2, сохранённое остаётся.

Островок иногда временно отдаёт тарифы отеля пустыми (`rates: null`) — такие ответы сохраняются как неполные и видны в `report` в колонке «неполных». Перезапросить их: `uv run ostrovok-scrape child-age --retry-failed`, `uv run ostrovok-scrape rates --retry-failed`, затем `normalize`. Часть `null` законна (у отеля нет номера на такой состав гостей) и останется неполной после повтора.

Тесты с БД (без переменной они пропускаются):

```bash
OSTROVOK_TEST_DSN=postgresql://ostrovok:ostrovok@localhost:5433/ostrovok_test uv run pytest
```

## Шум в Казани

Отдельный пакет `src/kazan_noise/`: относительная модель шума Казани по OpenStreetMap (дороги, рельсы, промзоны, аэропорт; экранирование зданиями и зеленью). Дизайн: `docs/superpowers/specs/2026-10-26-kazan-noise-design.md`. Агенту она доступна как tool `noise_in_kazan(points)`: до 30 точек `{lat, lon}` за вызов, ответ по каждой в том же порядке, точка вне Казани получает `error`.

```bash
uv run kazan-noise fetch      # скачать сырые данные OSM (публичные серверы Overpass: медленно, может понадобиться повтор)
uv run kazan-noise build      # собрать снимок объектов
uv run kazan-noise grid       # посчитать сетку H3 для шкалы баллов (--limit N — пробный прогон, сетка не сохраняется)
uv run kazan-noise query --lat 55.7963 --lon 49.1088 [--json]   # шум в точке
```

Данные лежат в `data/kazan_noise/` (путь — `KAZAN_NOISE_DATA_DIR`). Без снимка и сетки агент запускается, но tool отвечает, что оценка шума недоступна. Проверку модели запускает `uv run kazan-noise validate`: санити-проверки по снимку (у магистрали шумно, во дворах тише, в лесу тихо) и ранговая корреляция ночного балла с долей жалоб на внешний шум в отзывах островка; для второй части нужны `docker compose up -d` и собранный снимок островка. Код выхода 1, если санити-проверка не пройдена.

**Балл** — число 0–100: доля застроенных точек Казани, где тише (100 — громче всех), отдельно для дня (07–23) и ночи (23–07), с меткой от «очень тихо» до «очень шумно». Это относительная оценка, не децибелы. Для точки на здании (координаты отелей обычно стоят на здании) основной балл считается по самому шумному фасаду, а в `quiet_side` приходит оценка самой тихой стороны; днём и ночью эти фасады выбираются отдельно (ночью громче может быть двор с баром). Главные источники (`main_sources`) — до трёх с наибольшей долей энергии днём или ночью; участки одной улицы считаются одним источником.

Ограничения модели: уровни относительные, не дБ(А); потоки — значения по умолчанию для класса дороги, а не реальные замеры; нет отражений и метеоусловий; полнота OSM неравномерна по городу.

Атрибуция: данные © OpenStreetMap contributors, лицензия ODbL; tool указывает дату выгрузки в поле `data`.

