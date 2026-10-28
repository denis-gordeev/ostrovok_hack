import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

import psycopg

from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import MSK, ScraperConfig
from ostrovok_scraper.http import BlockedError, OstrovokHttp
from ostrovok_scraper.normalize import NormalizeReport, normalize
from ostrovok_scraper.steps import StepReport
from ostrovok_scraper.steps.hotels import run_hotels
from ostrovok_scraper.steps.rates import run_child_age, run_rates
from ostrovok_scraper.steps.reviews import run_reviews
from ostrovok_scraper.steps.search import run_serp, run_serp_guests
from ostrovok_scraper.store import RawStore, apply_migrations, connect

COMMANDS = ("db-init", "serp", "serp-guests", "hotels", "child-age", "rates", "reviews", "normalize", "report", "all")
RAW_STEPS = ("serp", "serp_guests", "hotels", "child_age", "rates", "reviews")
OSTROVOK_TABLES = ("regions", "hotels", "rooms", "night_prices", "rates", "reviews")


def build_parser() -> argparse.ArgumentParser:
    """Парсер команд: подкоманда на шаг плюс общие флаги."""
    parser = argparse.ArgumentParser(prog="ostrovok-scrape", description="Сбор данных ostrovok.ru в Postgres")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        p.add_argument("--limit-hotels", type=int, default=None, metavar="N", help="не больше N отелей")
        p.add_argument("--nights", type=int, default=None, metavar="N", help="только первые N ночей окна")
        p.add_argument("--retry-failed", action="store_true", help="повторить ключи с ошибкой и неполные")
        p.add_argument(
            "--today", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
            help="день сбора (по умолчанию сегодня по Москве)",
        )
    return parser


@contextmanager
def open_resources(cfg: ScraperConfig) -> Iterator[tuple[OstrovokApi, RawStore, psycopg.Connection]]:
    """HTTP-клиент, API, хранилище raw и соединение с БД; всё закрывается на выходе."""
    conn = connect(cfg.db_dsn)
    try:
        with OstrovokHttp(cfg) as http:
            yield OstrovokApi(http, cfg), RawStore(conn), conn
    finally:
        conn.close()


def _print_report(report: StepReport) -> None:
    print(f"{report.step}: сделано {report.done}, пропущено {report.skipped}, ошибок {report.errors}")


def _print_normalize(report: NormalizeReport) -> None:
    print("normalize:")
    for name, count in report.counts.items():
        print(f"  {name}: {count}")
    for guests, count in sorted(report.rates_by_guests.items()):
        print(f"  тарифов на {guests} гостей: {count}")
    for step, count in report.skipped.items():
        print(f"  пропущено строк {step}: {count}")
    for name, items in report.anomalies.items():
        print(f"  {name}: {len(items)}")


def _print_db_report(conn: psycopg.Connection) -> None:
    """Сводка по raw (всего / с ошибкой / неполных) и числу строк в ostrovok."""
    rows = conn.execute(
        """
        SELECT step, count(*) AS total,
               count(*) FILTER (WHERE error IS NOT NULL) AS failed,
               count(*) FILTER (WHERE incomplete) AS incomplete
        FROM raw.responses GROUP BY step
        """
    ).fetchall()
    by_step = {r["step"]: r for r in rows}
    print("raw:")
    for step in RAW_STEPS:
        r = by_step.get(step, {"total": 0, "failed": 0, "incomplete": 0})
        print(f"  {step}: всего {r['total']}, с ошибкой {r['failed']}, неполных {r['incomplete']}")
    print("ostrovok:")
    for table in OSTROVOK_TABLES:
        n = conn.execute(f"SELECT count(*) AS n FROM ostrovok.{table}").fetchone()["n"]
        print(f"  ostrovok.{table}: {n}")


def _run(args: argparse.Namespace, cfg: ScraperConfig) -> None:
    today = args.today or datetime.now(MSK).date()
    cmd = args.command
    with open_resources(cfg) as (api, store, conn):
        if cmd == "db-init":
            for name in apply_migrations(conn):
                print(f"применена миграция {name}")
            return
        if cmd == "report":
            _print_db_report(conn)
            return
        all_steps = cmd == "all"
        if all_steps or cmd == "serp":
            _print_report(run_serp(api, store, cfg, today=today, nights=args.nights, retry_failed=args.retry_failed))
        if all_steps or cmd == "serp-guests":
            _print_report(run_serp_guests(api, store, cfg, retry_failed=args.retry_failed))
        for name, run in (
            ("hotels", run_hotels), ("child-age", run_child_age), ("rates", run_rates), ("reviews", run_reviews),
        ):
            if all_steps or cmd == name:
                _print_report(run(api, store, cfg, limit_hotels=args.limit_hotels, retry_failed=args.retry_failed))
        if all_steps or cmd == "normalize":
            _print_normalize(normalize(conn, cfg))


def main(argv: list[str] | None = None) -> int:
    """Точка входа ostrovok-scrape; код 2 — похоже на блокировку."""
    args = build_parser().parse_args(argv)
    try:
        _run(args, ScraperConfig())
    except BlockedError as e:
        print(
            f"Похоже на блокировку островком: {e}. Сохранённое не потеряно, перезапустите позже.",
            file=sys.stderr,
        )
        return 2
    return 0
