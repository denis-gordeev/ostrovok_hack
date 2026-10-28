from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Protocol

import psycopg

from kudago_events.api import KudagoApi
from kudago_events.config import MSK, KudagoConfig
from kudago_events.parse import EventRow, parse_event
from kudago_events.store import (apply_migrations, connect, finish_load,
                                 last_ok_load, replace_events, start_load)

# Ключ pg_advisory_lock: одну загрузку за раз, даже если запущено несколько агентов.
LOCK_KEY = 2026102801


class EventsSource(Protocol):
    def events(self, since: datetime, until: datetime): ...


@dataclass(frozen=True)
class RefreshResult:
    """loaded — загружено заново; fresh — данные свежие; busy — грузит другой процесс; failed — ошибка (в error)."""

    status: Literal["loaded", "fresh", "busy", "failed"]
    events: int = 0
    error: str | None = None


def load_events(conn: psycopg.Connection, api: EventsSource, cfg: KudagoConfig, *, now: datetime) -> int:
    """Загружает события окна [сегодня, +horizon_days] и заменяет ими таблицы; сбой → прежние данные, запись failed."""
    window_from = now.astimezone(MSK).date()
    window_to = window_from + timedelta(days=cfg.horizon_days)
    start = datetime.combine(window_from, time(), MSK)
    end = datetime.combine(window_to + timedelta(days=1), time(), MSK)
    load_id = start_load(conn, started_at=now, window_from=window_from, window_to=window_to)
    try:
        rows: dict[int, EventRow] = {}
        for raw in api.events(start, end):
            if row := parse_event(raw, window_start=start, window_end=end):
                rows[row.event_id] = row
        replace_events(conn, list(rows.values()))
        finish_load(conn, load_id, status="ok", events=len(rows))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        finish_load(conn, load_id, status="failed", error=str(exc)[:1000] or type(exc).__name__)
        conn.commit()
        raise
    return len(rows)


def refresh_if_stale(
    cfg: KudagoConfig, *, now: datetime | None = None, api: EventsSource | None = None
) -> RefreshResult:
    """Загружает события, если удачной загрузки не было max_age_hours. Не бросает исключений: ошибка — в результате."""
    now = now or datetime.now(UTC)
    try:
        with connect(cfg.db_dsn) as conn:
            # Блокировка сессионная: снимается вместе с закрытием соединения.
            if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
                return RefreshResult("busy")
            apply_migrations(conn)
            last = last_ok_load(conn)
            if last and now - last["started_at"] < timedelta(hours=cfg.max_age_hours):
                return RefreshResult("fresh", events=last["events"])
            if api is not None:
                return RefreshResult("loaded", events=load_events(conn, api, cfg, now=now))
            with KudagoApi(cfg) as own_api:
                return RefreshResult("loaded", events=load_events(conn, own_api, cfg, now=now))
    except Exception as exc:
        return RefreshResult("failed", error=str(exc)[:1000] or type(exc).__name__)
