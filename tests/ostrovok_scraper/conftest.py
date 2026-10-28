import os

import psycopg
import pytest

from ostrovok_scraper.store import apply_migrations, connect


@pytest.fixture
def db_conn() -> psycopg.Connection:
    """Соединение с тестовой БД с чистыми схемами raw и ostrovok."""
    dsn = os.environ.get("OSTROVOK_TEST_DSN")
    if not dsn:
        pytest.skip("OSTROVOK_TEST_DSN не задан")
    conn = connect(dsn)
    try:
        conn.execute("DROP SCHEMA IF EXISTS raw, ostrovok CASCADE")
        conn.commit()
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()
