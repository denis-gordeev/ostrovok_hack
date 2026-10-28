from datetime import date, datetime
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kudago_events.parse import EventRow


def connect(dsn: str) -> psycopg.Connection:
    """Соединение с Postgres: строки-словари, транзакции вручную."""
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False, connect_timeout=5)


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Выполняет все migrations/*.sql по имени; SQL идемпотентный."""
    sql_files = sorted(
        (f for f in (files("kudago_events") / "migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    for sql_file in sql_files:
        conn.execute(sql_file.read_text(encoding="utf-8"))
    conn.commit()
    return [f.name for f in sql_files]


def last_ok_load(conn: psycopg.Connection) -> dict | None:
    """Последняя удачная загрузка: started_at, window_from, window_to, events."""
    return conn.execute(
        "SELECT started_at, window_from, window_to, events FROM kudago.loads"
        " WHERE status = 'ok' ORDER BY started_at DESC, load_id DESC LIMIT 1"
    ).fetchone()


def start_load(conn: psycopg.Connection, *, started_at: datetime, window_from: date, window_to: date) -> int:
    """Запись о начатой загрузке; коммитится сразу, чтобы остаться и при сбое."""
    row = conn.execute(
        "INSERT INTO kudago.loads (started_at, status, window_from, window_to)"
        " VALUES (%s, 'running', %s, %s) RETURNING load_id",
        (started_at, window_from, window_to),
    ).fetchone()
    conn.commit()
    return row["load_id"]


def finish_load(conn: psycopg.Connection, load_id: int, *, status: str, events: int = 0, error: str | None = None):
    """Итог загрузки; коммитит вызывающий — вместе с данными."""
    conn.execute(
        "UPDATE kudago.loads SET status = %s, events = %s, error = %s, finished_at = now() WHERE load_id = %s",
        (status, events, error, load_id),
    )


def replace_events(conn: psycopg.Connection, rows: list[EventRow]) -> None:
    """Заменяет все события и сеансы; коммитит вызывающий, поэтому читатели не видят полузагруженных данных."""
    conn.execute("DELETE FROM kudago.events")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO kudago.events (event_id, title, slug, site_url, description, body_text, categories, tags,"
            " age_restriction, price, is_free, favorites_count, place_title, place_address, place_lat, place_lon,"
            " place_url, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    r.event_id, r.title, r.slug, r.site_url, r.description, r.body_text, r.categories, r.tags,
                    r.age_restriction, r.price, r.is_free, r.favorites_count, r.place_title, r.place_address,
                    r.place_lat, r.place_lon, r.place_url, Jsonb(r.raw),
                )
                for r in rows
            ],
        )
        cur.executemany(
            "INSERT INTO kudago.event_dates (event_id, start_at, end_at) VALUES (%s, %s, %s)",
            [(r.event_id, s.start_at, s.end_at) for r in rows for s in r.sessions],
        )
