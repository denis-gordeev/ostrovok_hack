import argparse
import sys

from kudago_events.config import MSK, KudagoConfig
from kudago_events.load import refresh_if_stale
from kudago_events.store import apply_migrations, connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kudago-events", description="События Казани с KudaGo в Postgres")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("db-init", help="применить миграции схемы kudago")
    sub.add_parser("load", help="загрузить события заново, даже если данные свежие")
    sub.add_parser("report", help="число событий и последние загрузки")
    return parser


def _report(cfg: KudagoConfig) -> None:
    with connect(cfg.db_dsn) as conn:
        apply_migrations(conn)
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM kudago.events) AS events, (SELECT count(*) FROM kudago.event_dates) AS dates"
        ).fetchone()
        loads = conn.execute(
            "SELECT started_at, status, window_from, window_to, events, error FROM kudago.loads"
            " ORDER BY load_id DESC LIMIT 5"
        ).fetchall()
    print(f"событий: {counts['events']}, сеансов: {counts['dates']}")
    print("последние загрузки:")
    for row in loads:
        line = f"  {row['started_at'].astimezone(MSK):%Y-%m-%d %H:%M} {row['status']}"
        line += f" окно {row['window_from']}…{row['window_to']}, событий {row['events'] or 0}"
        if row["error"]:
            line += f" — {row['error'][:200]}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    """Точка входа kudago-events; код 1 — загрузка не удалась."""
    args = build_parser().parse_args(argv)
    cfg = KudagoConfig()
    if args.command == "db-init":
        with connect(cfg.db_dsn) as conn:
            for name in apply_migrations(conn):
                print(f"применена миграция {name}")
        return 0
    if args.command == "report":
        _report(cfg)
        return 0
    # load: max_age_hours=0 — любые данные считаются устаревшими.
    result = refresh_if_stale(cfg.model_copy(update={"max_age_hours": 0}))
    if result.status == "loaded":
        print(f"загружено событий: {result.events}")
        return 0
    if result.status == "busy":
        print("События уже загружает другой процесс.", file=sys.stderr)
        return 1
    print(f"События KudaGo не загружены: {result.error}", file=sys.stderr)
    return 1
