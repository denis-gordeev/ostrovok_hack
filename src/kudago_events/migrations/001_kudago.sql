CREATE SCHEMA IF NOT EXISTS kudago;

CREATE TABLE IF NOT EXISTS kudago.events (
    event_id bigint PRIMARY KEY, title text NOT NULL, slug text, site_url text NOT NULL,
    description text, body_text text,
    categories text[] NOT NULL DEFAULT '{}', tags text[] NOT NULL DEFAULT '{}',
    age_restriction text, price text, is_free boolean, favorites_count integer NOT NULL DEFAULT 0,
    place_title text, place_address text, place_lat double precision, place_lon double precision, place_url text,
    raw jsonb NOT NULL
);

-- Сеансы в окне загрузки; NULL — открытый конец (постоянная экспозиция и т. п.).
CREATE TABLE IF NOT EXISTS kudago.event_dates (
    event_id bigint NOT NULL REFERENCES kudago.events ON DELETE CASCADE,
    start_at timestamptz, end_at timestamptz
);
CREATE INDEX IF NOT EXISTS event_dates_event_idx ON kudago.event_dates (event_id);
CREATE INDEX IF NOT EXISTS event_dates_period_idx ON kudago.event_dates (start_at, end_at);

-- Журнал загрузок: свежесть данных считается от started_at последней удачной.
CREATE TABLE IF NOT EXISTS kudago.loads (
    load_id bigserial PRIMARY KEY, started_at timestamptz NOT NULL, finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
    window_from date NOT NULL, window_to date NOT NULL, events integer, error text
);
