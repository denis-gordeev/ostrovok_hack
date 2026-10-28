"""Командная строка kazan-noise: fetch | build | grid | query | validate."""

import argparse
import math
import os
import sys
import time
from datetime import date

import psycopg

from kazan_noise import queries
from kazan_noise.config import NoiseConfig
from kazan_noise.features import build_features
from kazan_noise.fetch import fetch_all, load_raw
from kazan_noise.grid import compute_grid, load_grid, save_grid
from kazan_noise.overpass import OverpassClient, OverpassError
from kazan_noise.service import NoiseReport, NoiseService, OutsideKazanError
from kazan_noise.snapshot import SnapshotMissingError, save_features, write_meta
from kazan_noise.validate import SanityCheck, load_hotels, sanity_checks, validate_hotels
from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.store import connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kazan-noise", description="Относительный шум Казани по OSM")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="скачать сырые данные OSM")
    p_fetch.add_argument("--refresh", action="store_true", help="перекачать уже скачанное")

    p_build = sub.add_parser("build", help="собрать снимок объектов из сырых данных")
    p_build.add_argument("--today", type=date.fromisoformat, help="дата отсчёта устаревших строек, YYYY-MM-DD")

    p_grid = sub.add_parser("grid", help="посчитать сетку H3 для шкалы")
    p_grid.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    p_grid.add_argument("--limit", type=int, help="пробный прогон: только первые N ячеек, сетка не сохраняется")

    p_query = sub.add_parser("query", help="шум в точке")
    p_query.add_argument("--lat", type=float, required=True)
    p_query.add_argument("--lon", type=float, required=True)
    p_query.add_argument("--json", action="store_true", help="вывести JSON")

    sub.add_parser("validate", help="санити-проверки и сверка с жалобами на шум в отзывах островка")
    return parser


def _cmd_fetch(args: argparse.Namespace, cfg: NoiseConfig) -> None:
    with OverpassClient(cfg) as client:
        fetched = fetch_all(client, cfg, refresh=args.refresh)
    print(f"скачано: {', '.join(fetched) if fetched else 'ничего нового'}")


def _cmd_build(args: argparse.Namespace, cfg: NoiseConfig) -> None:
    try:
        raw = {name: load_raw(cfg, name) for name in queries.QUERY_NAMES}
    except FileNotFoundError:
        print("нет сырых данных: запустите kazan-noise fetch", file=sys.stderr)
        sys.exit(1)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        features, report = build_features(raw, today=args.today or date.today())
    except ValueError as exc:
        # Например, в boundary нет relation с геометрией: сырые данные неполные.
        print(f"не удалось собрать снимок: {exc}; перекачайте: kazan-noise fetch --refresh", file=sys.stderr)
        sys.exit(1)
    save_features(features, cfg.features_path)
    write_meta(cfg.meta_path, features, report)
    print(f"counts: {dict(report.counts)}")
    print(f"skipped: {dict(report.skipped)}")
    print("дальше: uv run kazan-noise grid")


def _cmd_grid(args: argparse.Namespace, cfg: NoiseConfig) -> None:
    started = time.perf_counter()
    grid = compute_grid(cfg.features_path, cfg, workers=args.workers, limit=args.limit)
    # Урезанная сетка дала бы неверную шкалу: --limit только считает.
    if args.limit is None:
        save_grid(grid, cfg.grid_path)
    elapsed = time.perf_counter() - started
    print(f"ячеек: {len(grid.cells)}, застроенных: {int(grid.built_up.sum())}, время: {elapsed:.1f} с")
    if args.limit is not None:
        print("пробный прогон (--limit): сетка не сохранена")


def _format_report(r: NoiseReport) -> str:
    lines = [
        f"Точка {r.lat:.5f}, {r.lon:.5f}" + (" (внутри здания)" if r.inside_building else ""),
        f"День:  {r.day.score}/100 ({r.day.label}), модель {r.model_level_day} дБ",
        f"Ночь:  {r.night.score}/100 ({r.night.label}), модель {r.model_level_night} дБ",
    ]
    if r.quiet_side:
        q = r.quiet_side
        lines.append(
            f"Тихая сторона: день {q.day.score} ({q.day.label}), ночь {q.night.score} ({q.night.label})"
        )
    if r.main_sources:
        lines.append("Основные источники:")
    for s in r.main_sources:
        lines.append(
            f"  - {s.kind}/{s.subtype} {s.name or '(без названия)'}: {s.distance_m} м, "
            f"день {s.share_day:.0%}, ночь {s.share_night:.0%}" + (", экранирован" if s.shielded else "")
        )
    lines.append(f"Данные OSM на {r.osm_timestamp}")
    return "\n".join(lines)


def _cmd_query(args: argparse.Namespace, cfg: NoiseConfig) -> None:
    started = time.perf_counter()
    service = NoiseService.load(cfg.data_dir, receiver_height=cfg.receiver_height_m)
    loaded = time.perf_counter()
    report = service.noise_at(args.lat, args.lon)
    done = time.perf_counter()
    print(report.model_dump_json(indent=2) if args.json else _format_report(report))
    print(f"загрузка снимка: {loaded - started:.2f} с, расчёт точки: {done - loaded:.2f} с", file=sys.stderr)


def _print_sanity(checks: list[SanityCheck]) -> None:
    print("Санити-проверки (дневной балл):")
    for c in checks:
        verdict = "мало точек" if c.ok is None else ("ок" if c.ok else "НЕ ПРОЙДЕНА")
        median = "—" if c.median_score is None else f"{c.median_score:.0f}"
        print(f"  {c.name}: точек {c.n}, медиана {median}, ожидание {c.expected}: {verdict}")


def _rho(value: float) -> str:
    """Коэффициент корреляции для вывода; nan (постоянный ряд) → «н/д»."""
    return "н/д (ряд постоянен)" if math.isnan(value) else f"{value:.2f}"


def _print_hotels(service: NoiseService) -> None:
    """Корреляция с жалобами; недоступная БД или отсутствие отелей не роняют команду."""
    try:
        with connect(ScraperConfig().db_dsn) as conn:
            rows = load_hotels(conn)
    except psycopg.Error as exc:
        print(f"Отзывы островка недоступны: {exc}")
        return
    report = validate_hotels(service, rows)
    if report.n == 0:
        print("Отзывы островка недоступны: нет отелей с отзывами внутри Казани")
        return
    print(
        f"Отели: {report.n}; Спирмен «балл шума — доля жалоб»: "
        f"день {_rho(report.rho_day)}, ночь {_rho(report.rho_night)}"
    )
    print("Наибольшие расхождения рангов (ночь):")
    for hotel, gap in report.worst:
        print(f"  {hotel.name}: жалоб {hotel.complaints}/{hotel.reviews}, разница рангов {gap}")


def _cmd_validate(args: argparse.Namespace, cfg: NoiseConfig) -> None:
    service = NoiseService.load(cfg.data_dir, receiver_height=cfg.receiver_height_m)
    checks = sanity_checks(service, load_grid(cfg.grid_path))
    _print_sanity(checks)
    _print_hotels(service)
    if any(c.ok is False for c in checks):
        sys.exit(1)


_COMMANDS = {
    "fetch": _cmd_fetch,
    "build": _cmd_build,
    "grid": _cmd_grid,
    "query": _cmd_query,
    "validate": _cmd_validate,
}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _COMMANDS[args.command](args, NoiseConfig())
    except (SnapshotMissingError, OverpassError, OutsideKazanError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
