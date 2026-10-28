import pytest

from ostrovok_scraper.store import RawStore, apply_migrations

pytestmark = pytest.mark.db


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


def test_save_strips_nul_characters(db_conn):
    store = RawStore(db_conn)
    store.save("hotels", "k", request={}, status_code=200, body={"a": ["x\u0000y", {"b": "a\u0000b"}]})
    assert dict(store.bodies("hotels")) == {"k": {"a": ["xy", {"b": "ab"}]}}


def test_save_data_error_becomes_value_error_and_connection_stays_usable(db_conn):
    store = RawStore(db_conn)
    with pytest.raises(ValueError):
        store.save("hotels", "k", request={}, status_code=200, body={"a": 1}, incomplete="not-a-bool")
    store.save("hotels", "k", request={}, status_code=None, body=None, error="boom")
    assert store.existing_keys("hotels") == {"k"}
