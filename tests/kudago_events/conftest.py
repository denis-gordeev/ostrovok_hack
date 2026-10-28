import os

import psycopg
import pytest


@pytest.fixture
def dsn() -> str:
    """Тестовая БД без схемы kudago: её создают миграции загрузки."""
    dsn = os.environ.get("OSTROVOK_TEST_DSN")
    if not dsn:
        pytest.skip("OSTROVOK_TEST_DSN не задан")
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS kudago CASCADE")
    return dsn
