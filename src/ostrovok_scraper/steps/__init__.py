from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ostrovok_scraper.api import Fetched
from ostrovok_scraper.http import OstrovokHttpError
from ostrovok_scraper.store import RawStore


@dataclass(frozen=True)
class Item:
    """Один запрос шага: ключ в raw, описание запроса и вызов."""

    key: str
    request: dict
    fetch: Callable[[], Fetched]


@dataclass
class StepReport:
    """Итог шага: сколько запросов сделано, пропущено (уже есть) и упало."""

    step: str
    done: int = 0
    skipped: int = 0
    errors: int = 0


def run_items(store: RawStore, step: str, items: Iterable[Item], *, retry_failed: bool = False) -> StepReport:
    """Выполняет запросы, которых ещё нет в raw; ошибка ключа не останавливает шаг.

    BlockedError не перехватывается: блокировка прерывает сбор, сохранённое остаётся.
    """
    existing = store.existing_keys(step, include_failed=not retry_failed)
    # Неполные ключи хранят частичное тело: неудачный повтор его не затирает.
    partial = set(store.incomplete_keys(step)) if retry_failed else set()
    report = StepReport(step)
    for item in items:
        if item.key in existing:
            report.skipped += 1
            continue
        try:
            f = item.fetch()
        except (OstrovokHttpError, ValueError, KeyError, TypeError) as e:
            if item.key in partial:
                report.errors += 1
                continue
            store.save(
                step, item.key, request=item.request,
                status_code=getattr(e, "status_code", None), body=None, error=str(e),
            )
            report.errors += 1
            continue
        store.save(
            step, item.key, request=item.request,
            status_code=f.status_code, body=f.body, incomplete=f.incomplete,
        )
        report.done += 1
    return report
