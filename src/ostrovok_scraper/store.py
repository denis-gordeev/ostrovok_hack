from datetime import datetime
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect(dsn: str) -> psycopg.Connection:
    """Соединение с Postgres: строки-словари, транзакции вручную."""
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Выполняет все migrations/*.sql по имени; SQL идемпотентный."""
    sql_files = sorted(
        (f for f in (files("ostrovok_scraper") / "migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    for sql_file in sql_files:
        conn.execute(sql_file.read_text(encoding="utf-8"))
    conn.commit()
    return [f.name for f in sql_files]


def _strip_nul(value: Any) -> Any:
    """Убирает \\u0000 из всех строк: jsonb их не принимает."""
    if isinstance(value, str):
        return value.replace("\u0000", "")
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


class RawStore:
    """Сырые ответы в raw.responses, ключ — (шаг, ключ)."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def save(
        self,
        step: str,
        key: str,
        *,
        request: dict,
        status_code: int | None,
        body: dict | None,
        error: str | None = None,
        incomplete: bool = False,
    ) -> None:
        """Upsert ответа; каждая запись коммитится сразу. Данные, которые БД не принимает, дают ValueError."""
        request = _strip_nul(request)
        body = _strip_nul(body)
        try:
            self._upsert(step, key, request, status_code, body, error, incomplete)
        except psycopg.DataError as exc:
            self._conn.rollback()
            raise ValueError(str(exc)[:300]) from exc
        self._conn.commit()

    def _upsert(
        self, step: str, key: str, request: dict, status_code: int | None, body: dict | None,
        error: str | None, incomplete: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO raw.responses (step, key, request, status_code, body, error, incomplete)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (step, key) DO UPDATE SET
                request = EXCLUDED.request, status_code = EXCLUDED.status_code,
                body = EXCLUDED.body, error = EXCLUDED.error,
                incomplete = EXCLUDED.incomplete, fetched_at = now()
            """,
            (step, key, Jsonb(request), status_code, None if body is None else Jsonb(body), error, incomplete),
        )

    def existing_keys(self, step: str, *, include_failed: bool = True) -> set[str]:
        """Ключи шага; без include_failed — только удачные и полные."""
        where = "" if include_failed else " AND error IS NULL AND NOT incomplete"
        rows = self._conn.execute(f"SELECT key FROM raw.responses WHERE step = %s{where}", (step,)).fetchall()
        return {r["key"] for r in rows}

    def bodies(self, step: str) -> list[tuple[str, dict]]:
        """Ключ и тело строк без ошибки (включая неполные), по ключу."""
        rows = self._conn.execute(
            "SELECT key, body FROM raw.responses WHERE step = %s AND error IS NULL ORDER BY key", (step,)
        ).fetchall()
        return [(r["key"], r["body"]) for r in rows]

    def incomplete_keys(self, step: str) -> list[str]:
        """Ключи ответов, у которых поиск не дошёл до конца."""
        rows = self._conn.execute(
            "SELECT key FROM raw.responses WHERE step = %s AND incomplete ORDER BY key", (step,)
        ).fetchall()
        return [r["key"] for r in rows]

    def first_fetched_at(self, step: str) -> datetime | None:
        """Время самой ранней записи шага."""
        row = self._conn.execute("SELECT min(fetched_at) AS t FROM raw.responses WHERE step = %s", (step,)).fetchone()
        return row["t"]
