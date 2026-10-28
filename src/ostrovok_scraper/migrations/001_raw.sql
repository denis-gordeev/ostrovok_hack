CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.responses (
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
