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
