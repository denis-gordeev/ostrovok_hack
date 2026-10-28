# Сбор данных ostrovok.ru по Татарстану в локальный Postgres

Дата: 2026-10-26. Статус: дизайн согласован по разделам, ждёт проверки спека.

## 1. Цель и рамки

Собрать с ostrovok.ru реальные данные об отелях Республики Татарстан и сохранить их в локальный
Postgres. Позже на этих данных будет построен мок тула бронирования (он ходит в Postgres вместо API
островка), но **мок и тулы агента в этот этап не входят** — см. раздел 12.

Входит:

- Postgres в Docker и схема БД.
- Сборщик внутреннего JSON-API сайта: сырые ответы → схема `raw`.
- Нормализация: `raw` → схема `ostrovok` (справочник отелей, номеров, цен, тарифов, отзывов).
- Тесты без сети и пробный прогон.

Не входит: мок `OstrovokAPI`, досчёт цен под произвольные даты, брони, LangChain-тулы, изменения графа
агента и промптов.

## 2. Принятые решения

| Вопрос | Решение |
|---|---|
| Регион | Республика Татарстан, `region_id = 6288539` (Казань 1993, Набережные Челны 2522 и др. — внутри) |
| Типы объектов | Только отельные: `hotel, mini_hotel, hostel, apart_hotel, guesthouse, sanatorium, boutique, resort`. Квартиры, коттеджи, кемпинги, глэмпинги не берём |
| Окно дат | Ночи заезда с дня сбора по 29.11.2026 включительно, выезд не позже 30.11.2026 («31 ноября» не существует). Ночи раньше дня сбора получить нельзя |
| Цены | Вариант Б: реальная минимальная цена и доступность по каждой ночи из выдачи + полные тарифы на опорные ночи. Досчёт остального — забота будущего мока |
| Опорные ночи | Будни — вт 17.11 → ср 18.11, выходные — пт 20.11 → сб 21.11 |
| Гости | Тарифы для 1–8 гостей в номере (многодетные семьи), дети учитываются по возрасту (раздел 5.4) |
| Отзывы | Все русские отзывы, английские не берём. Имя и страну автора не сохраняем |
| Хранение | Сырые ответы в `raw` + идемпотентная нормализация в `ostrovok` |
| Скорость | Один поток, не чаще 1 запроса в секунду, опрос поиска раз в 2 с |
| Валюта и язык | RUB, `ru` |
| Правила сайта | Пользовательское соглашение островка не проверялось; `robots.txt` используемые пути не закрывает. Данные остаются внутри проекта |

## 3. Источник: внутренний API ostrovok.ru

Все запросы работают обычным HTTP без браузера и cookies (проверено 2026-10-26), достаточно
браузерного `User-Agent`. Это внутренний API сайта, он может меняться без предупреждения.

| Что | Запрос | Примечания |
|---|---|---|
| Сессия поиска | `GET /hotel/search/v2/site/serp?body={"session_params": SP}` | Возвращает `session_info.session.id` и данные региона |
| Выдача | `POST /hotel/search/v2/site/serp?session=<id>`, тело `{session_params: SP, page: 1, filters: {kinds: [...]}, session_id, map_hotels: true}` | Поиск асинхронный: опрашивать до `search_finished: true` (обычно 4–6 опросов). `map_hotels` — **все** доступные объекты с минимальной ценой одним ответом: `ota_hotel_id, master_id, region_catalog_slug, name, stars, rating, price, latitude, longitude` |
| Тарифы отеля | `GET /hotel/search/v1/site/hp/search?body={arrival_date, departure_date, hotel, currency, lang, region_id, paxes, search_uuid}` | Все тарифы на даты и состав гостей; повторные запросы дают тот же результат |
| Страница отеля | `GET /hotel/{region_catalog_slug}/mid{master_id}/{ota_hotel_id}/` | HTML; данные в `<script id="__NEXT_DATA__">`, путь `props.pageProps`: `hotel` (статика, правила, номера `roomGroups` с `rgHash`), `hotelRegionData`, `reviews` (10 самых полезных отзывов + `total`) |
| Отзывы | `GET /hotel/search/v1/site/reviews?hotel=<id>&lang=ru&sort=relevance_desc&page=N&page_size=500` | Поле `total`; `page_size` до 500 работает |

`SP` (session_params): `{arrival_date, departure_date, currency: "RUB", language: "ru", paxes, region_id, search_uuid}`.

**Гости (`paxes`)** — список, по элементу на номер: `{"adults": 2, "child_ages": [5, 12]}`. Ключ именно
`child_ages`: под ключом `children` островок молча игнорирует детей. Лимиты на номер: до 6 взрослых и
до 4 детей (иначе HTTP 400 `invalid_paxes`).

## 4. Компоненты

```
ostrovok.ru (внутренний API)
   │  ostrovok_scraper: один поток, ≤1 запрос/с, докачка
   ▼
Postgres, схема raw       — ответы как есть + параметры запроса + время
   │  normalize: одна транзакция, идемпотентно
   ▼
Postgres, схема ostrovok  — регионы, отели, номера, цены по ночам, тарифы, отзывы
```

| Путь | Что |
|---|---|
| `docker-compose.yml` | Postgres 17: БД `ostrovok`, пользователь `ostrovok`, порт хоста 5433 (5432 занят другим Postgres), именованный volume |
| `src/ostrovok_scraper/` | Отдельный пакет; агент `hotel_agent` от него не зависит |
| `src/ostrovok_scraper/config.py` | Настройки из окружения: `OSTROVOK_DB_DSN` (по умолчанию `postgresql://ostrovok:ostrovok@localhost:5433/ostrovok`), окно дат, опорные ночи, лимиты скорости |
| `src/ostrovok_scraper/http.py` | Клиент островка на `httpx` (синхронный): пауза между запросами, повторы, остановка при блокировке |
| `src/ostrovok_scraper/store.py` | Работа с `raw.responses`: ключи, сохранение, «уже скачано?» |
| `src/ostrovok_scraper/steps/` | Шаги сбора (раздел 5) |
| `src/ostrovok_scraper/normalize.py` | `raw` → `ostrovok` (раздел 7) |
| `src/ostrovok_scraper/migrations/*.sql` | Схема БД, пронумерованные файлы; применяет команда `db-init` |
| `src/ostrovok_scraper/cli.py` | `uv run ostrovok-scrape <команда>` (раздел 10) |

Зависимости: добавить `psycopg[binary]` (v3, синхронный режим — параллелизм не нужен); `httpx` уже есть.
В `pyproject.toml`: пакет `src/ostrovok_scraper` в сборку, скрипт `ostrovok-scrape`.

## 5. Порядок сбора

Константы: `KINDS` — список отельных типов из раздела 2; `REGION = 6288539`; все поиски с
`filters: {kinds: KINDS}` и `map_hotels: true`.

Тип ночи: ночи заезда в пятницу и субботу — выходные, остальные — будни.

### 5.1 `serp` — доступность и минимальная цена по ночам (2 гостя)

Для каждой ночи `n` окна: поиск `n → n+1`, `paxes = [{"adults": 2}]`, опрос до `search_finished`.
Сохраняется последний ответ. Ключ: `night=<n>;guests=2`. ~35 ночей × ~6 запросов ≈ 200.

### 5.2 `serp_guests` — у кого есть номера на 1 и 3–8 гостей

Для каждого `k ∈ {1, 3, 4, 5, 6, 7, 8}` и каждой опорной ночи — такой же поиск с `paxes` из таблицы
ниже. Ключ: `night=<n>;guests=<k>`. 14 поисков ≈ 85 запросов.

| Гостей `k` | `paxes` |
|---|---|
| 1–6 | `[{"adults": k}]` |
| 7 | `[{"adults": 6, "child_ages": [16]}]` |
| 8 | `[{"adults": 6, "child_ages": [16, 16]}]` |

Подростки 16 лет считаются гостями в любом отеле. Фактическое число гостей всегда берётся из поля
`occupancy` тарифа, а не из запроса.

### 5.3 `hotels` — статика

Для каждого отеля, встретившегося хотя бы в одной выдаче 5.1–5.2: страница отеля по
`region_catalog_slug` и `master_id` из выдачи. Из HTML извлекается `__NEXT_DATA__` и сохраняется только
`{hotel, hotelRegionData, reviews}` из `props.pageProps` (не весь HTML ~380 КБ). Ключ: `hotel=<id>`.
~450 запросов.

### 5.4 `child_age` — с какого возраста ребёнок считается гостем

Для каждого отеля один запрос тарифов на его будничную опорную ночь (5.5), а если её нет — на
выходную, с `paxes = [{"adults": 2, "child_ages": [4]}]`. Ключ: `hotel=<id>;night=<n>`. ~450 запросов.

Правило (применяет нормализация, раздел 7):

1. Есть тарифы с `occupancy = 2` → ребёнок 4 лет бесплатный, `free_child_age = 5`, источник `probe`.
2. `occupancy = 3` или тарифов нет → `free_child_age = 3`, источник `probe`.
3. Пробы нет (у отеля нет опорной ночи или ошибка) → `free_child_age = 3`, источник `default`.

Проверено на 4 отелях Казани: «Дон Кихот» считает гостем с 3 лет, Korston и «Никанас» — с 5 лет.
`policies.children` со страницы отеля для этого не годится: обычно пустое, а у Korston там «0–0 лет»,
хотя по факту дети до 5 лет бесплатные. Оно сохраняется в `hotels.policies` только для справки.

### 5.5 `rates` — полные тарифы

Опорная ночь отеля для типа `t` (будни/выходные): опорная ночь `t`, если отель есть в выдаче 5.1 на эту
ночь; иначе ближайшая ночь того же типа, где он есть; если такой нет — отель без тарифов этого типа.

| Гостей | Какие ночи | Кому |
|---|---|---|
| 2 | Опорная ночь отеля для будней и для выходных | Всем отелям |
| 1, 3–8 | Общая опорная ночь (17.11 и 20.11) | Только отелям, которые есть в выдаче 5.2 для этого `k` и этой ночи |

`paxes` — как в 5.2. Ключ: `hotel=<id>;night=<n>;guests=<k>`. ≈ 900 (2 гостя) + ≈ 1000 (остальные).

Известное ограничение: отели, занятые в общие опорные ночи, получают тарифы только для 2 гостей
(с запасной ночи).

### 5.6 `reviews` — отзывы

Только отелям, у которых на странице `reviews.total > 10`: страницы `lang=ru`, `page_size=500`, пока не
собрано `total`. У остальных все отзывы уже есть на странице отеля. Ключ: `hotel=<id>;page=<p>`.
~350 запросов.

### 5.7 Объём

Итого ≈ 3,4 тыс. запросов, около часа. Сырые данные ~200 МБ.

## 6. Схема БД

### 6.1 `raw`

```sql
CREATE TABLE raw.responses (
    id           bigserial PRIMARY KEY,
    step         text        NOT NULL,           -- serp | serp_guests | hotels | child_age | rates | reviews
    key          text        NOT NULL,           -- см. раздел 5
    request      jsonb       NOT NULL,           -- метод, путь, параметры, тело
    status_code  int,
    body         jsonb,                          -- ответ (для hotels — выжимка __NEXT_DATA__)
    error        text,
    incomplete   boolean     NOT NULL DEFAULT false,  -- поиск не дошёл до search_finished
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (step, key)
);
```

Повторная загрузка того же `(step, key)` перезаписывает строку.

### 6.2 `ostrovok`

Все таблицы схемы пересобирает `normalize`.

| Таблица | Ключ | Колонки |
|---|---|---|
| `meta` | `key` | `value`: `scraped_on`, `window_from`, `window_to`, `ref_weekday`, `ref_weekend` |
| `regions` | `region_id` | `name, name_en, type, parent_id, country_code, lat, lon, catalog_slug` |
| `hotels` | `hotel_id` (`ota_hotel_id`) | `master_id` (unique), `region_id, name, kind, stars` (0–5), `address, lat, lon, rating, rating_count, rating_details` (jsonb, по категориям), `check_in_from, check_out_until` (time), `description` (jsonb, разделы), `amenities` (jsonb, группы), `flags` (text[]: `has_parking`, `has_pool`…), `payment_methods` (text[]), `policies` (jsonb: доп. кровать, кроватка, животные, депозит, дети, документы), `free_child_age, free_child_age_source` (`probe` / `default`), `metro` (jsonb: название, расстояние), `images` (text[], до 5 шаблонов URL), `reviews_total` |
| `rooms` | `(hotel_id, rg_hash)` | `parent_rg_hash, name, bedding_type, bathroom, size_m2, amenities` (text[]), `images` (text[], до 3) |
| `night_prices` | `(hotel_id, night)` | `min_price` — минимальная цена ночи для 2 гостей. Строка есть ⇔ отель доступен в эту ночь |
| `rates` | `id` | `hotel_id, rg_hash, night, night_type` (`weekday`/`weekend`), `requested_guests, occupancy, room_name, meal, has_breakfast, payment_type` (`now`/`hotel`), `price, price_ratio` (= `price / night_prices.min_price` той же ночи; null, если строки `night_prices` на эту ночь нет), `discount_pct, free_cancellation_before` (timestamptz), `free_cancellation_hours` (часов до 00:00 МСК дня заезда; оба поля null — тариф невозвратный), `penalty_after_ratio` (штраф после срока / `price`), `cancellation_policies` (jsonb как есть), `no_show_penalty, no_show_nights, allotment, bed_places` (jsonb), `source_rate_hash`. Unique: `(hotel_id, night, requested_guests, rg_hash, meal, payment_type)` — из дублей остаётся самый дешёвый |
| `reviews` | `review_id` | `hotel_id, review_date, rating, details` (jsonb), `plus, minus, hotel_reply, traveller_type, trip_type, stay_check_in, stay_check_out, adults, room_name, likes, images` (text[]). Полей автора нет |

Деньги — `numeric(12,2)`, всё в RUB. Индексы: `night_prices (night)`, `hotels (region_id)`,
`hotels USING gin (flags)`, `rates (hotel_id, requested_guests, night)`,
`reviews (hotel_id, review_date DESC)`.

## 7. Нормализация

Читает последнюю удачную версию каждого `(step, key)` и пересобирает `ostrovok` одной транзакцией
(`TRUNCATE` + вставка). Повторный запуск даёт тот же результат.

Источники полей:

- **regions** — Татарстан из `session_info.region_related.region` выдачи; города из `hotelRegionData`
  страниц отелей (`regionId, name, nameEn, type, center, catalogSlug, countryCode`), родитель —
  Татарстан (`parentRegionData` на странице — это страна, он не используется).
- **hotels** — страница отеля (`hotel.*`):
  - `region_id, address, lat, lon` — `hotel.location` (`regionId, address, latitude, longitude`),
    `metro` — `hotel.location.nearestSubways`;
  - `kind` — `hotel.facts.kind` в нижнем регистре; `stars` — `map_hotels[].stars` из выдачи (на
    странице звёзд нет);
  - `rating` — `hotel.rating.total`, `rating_count` — `hotel.rating.reviewsCount`, `rating_details` —
    `hotel.rating.ostrovok.detailedRatings`;
  - `description` — `hotel.descriptionStruct`, `amenities` — `hotel.amenityGroups`, `flags` —
    `hotel.serpFilters` в нижнем регистре, `payment_methods` — `hotel.paymentMethods`, `images` — первые
    5 `hotel.images[].tmpl`;
  - `check_in_from` / `check_out_until` — `hotel.policies.checkInCheckOut`, остальные правила — в
    `policies`;
  - `reviews_total` — `reviews.total`. Контакты отеля не сохраняются.
- **rooms** — `hotel.roomGroups` (`rgHash`, `parentRgHash`, `nameStruct`, `size`, `roomAmenityData`,
  `images`).
- **night_prices** — `map_hotels[].price` из выдачи 5.1.
- **rates** — тарифы 5.5: сумма и тип оплаты — `payment_options.payment_types[0]`; питание — `meal`;
  отмена — `cancellation_info`; незаезд — `no_show`; остаток — `allotment`; номер — `rooms[0].rg_hash`.
- **reviews** — ответы 5.6, а для отелей с `total ≤ 10` — `reviews.list` со страницы отеля (там поля в
  camelCase, нормализация приводит оба вида к одной форме). Поля `author_name`, `author_residency`
  (`authorName`, `authorResidency`) отбрасываются.
- **free_child_age** — по правилу из 5.4.

Если обязательного поля нет, строка пропускается и учитывается в отчёте. В конце — отчёт: число отелей,
номеров, ночей, тарифов по каждому `requested_guests`, отзывов; аномалии: отели без тарифов, тарифы с
`rg_hash`, которого нет в `rooms`, отели без страницы, неполные поиски.

## 8. Ошибки

| Ситуация | Поведение |
|---|---|
| Таймаут, 429, 5xx | До 3 повторов с паузами 2, 5, 15 с |
| Прочие 4xx (например, `invalid_paxes`) | Без повторов; ошибка записывается в `raw.responses.error`, сбор продолжается |
| 5 ответов 403/429 подряд | Шаг останавливается с сообщением «похоже на блокировку»; перезапуск продолжает с места остановки |
| `search_finished` не наступил за 15 опросов | Сохраняется последний ответ с `incomplete = true` |
| Нет `__NEXT_DATA__` или другая структура страницы | Ошибка по этому отелю, остальные собираются |
| Ctrl+C | Каждый ответ сохраняется отдельной транзакцией; перезапуск продолжает |
| Повторный запуск шага | Пропускает ключи с удачным ответом; `--retry-failed` перезапрашивает ошибочные и `incomplete` |

В конце каждого шага — сводка: сделано, пропущено (уже скачано), ошибки.

## 9. Тесты

pytest, без сети — как остальные тесты проекта.

- **Фикстуры** — реальные ответы островка, сохранённые в `tests/fixtures/ostrovok/`: выдача с
  `map_hotels`, выжимка `__NEXT_DATA__`, тарифы (2 гостя, 4 гостя, проба ребёнка), страница отзывов.
  Имена и страны авторов из фикстур вычищены.
- **Разбор**: выдача → `night_prices`; страница → `hotels`, `rooms`, правила; тарифы → `price`,
  `free_cancellation_hours`, `penalty_after_ratio`, дедупликация; отзывы → без полей автора, обе формы
  (API и страница); проба → `free_child_age`.
- **Логика сбора**: `paxes` для 1–8 гостей; выбор опорной и запасной ночи; повторы, паузы и остановка при
  блокировке на `httpx.MockTransport`; докачка (пропуск уже скачанных ключей).
- **С БД** (`@pytest.mark.db`, пропускаются без `OSTROVOK_TEST_DSN`): миграции и нормализация на
  фикстурах, повторная нормализация даёт тот же результат.
- **Пробный прогон** перед полным сбором: `--limit-hotels 5 --nights 2`, сверка отчёта.

## 10. Запуск

```bash
docker compose up -d                          # Postgres
uv run ostrovok-scrape db-init                # миграции
uv run ostrovok-scrape all --limit-hotels 5 --nights 2   # пробный прогон
uv run ostrovok-scrape all                    # полный сбор (≈1 ч), шаги по порядку раздела 5
uv run ostrovok-scrape normalize              # пересобрать ostrovok из raw
uv run ostrovok-scrape report                 # сводка по raw и ostrovok
```

Отдельные шаги: `serp`, `serp-guests`, `hotels`, `child-age`, `rates`, `reviews`. Общие флаги:
`--retry-failed`, `--limit-hotels N`, `--nights N`.

## 11. Ограничения

- Снимок на день сбора; ночей раньше дня сбора нет; цены — те, что были в день сбора.
- Отели, занятые в общие опорные ночи, — только тарифы на 2 гостей (5.5).
- `free_child_age` различает только «гость с 3 лет» и «гость с 5 лет»: проба одна, на ребёнке 4 лет.
- Внутренний API может измениться — тогда сборщик падает по отдельным ключам, а не целиком, и это видно
  в отчёте.

## 12. Следующий этап (не в этом спеке)

Согласовано в обсуждении, понадобится при проектировании мока:

- **Досчёт цен**: отель доступен, если есть `night_prices` на каждую ночь; цена тарифа на ночь =
  `min_price` ночи × `price_ratio` шаблона того же типа ночи и числа гостей; сумма по ночам (проверено:
  цена 2 ночей = сумма ночей). Доли между ночами сдвигаются обычно на 0–2%, в худшем случае до 14%.
  Отмена — «00:00 МСК дня заезда минус `free_cancellation_hours`». Остаток — максимум `allotment` по
  типу номера минус пересекающиеся брони. Ребёнок младше `free_child_age` бесплатный и не считается
  гостем; до 6 взрослых и 4 детей в номере, до 8 гостей в брони, несколько номеров — сумма. «Сегодня»
  мока — дата сбора, переопределяется настройкой.
- **Контракт**: интерфейс `OstrovokAPI` по шагам ETG API v3 (поиск, страница отеля, prebook, booking
  form/finish/status, order info, cancel, отзывы), таблицы `offers` и `bookings`, шесть тулов агента
  рядом с Booking.
