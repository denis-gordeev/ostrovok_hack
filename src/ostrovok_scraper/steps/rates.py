from datetime import date

from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import OTHER_GUESTS, ScraperConfig
from ostrovok_scraper.domain import (child_age_key, paxes_for_guests,
                                     pick_reference_night, rates_key)
from ostrovok_scraper.steps import Item, StepReport, run_items
from ostrovok_scraper.steps.inputs import (available_nights, guests_hotels,
                                           page_facts, selected_hotels,
                                           serp_hotels)
from ostrovok_scraper.store import RawStore

CHILD_PROBE_PAXES = [{"adults": 2, "child_ages": [4]}]


def child_age_night(available: set[date], cfg: ScraperConfig) -> date | None:
    """Ночь пробы возраста ребёнка: опорная будней, иначе выходных."""
    return pick_reference_night(available, cfg.ref_weekday) or pick_reference_night(available, cfg.ref_weekend)


def plan_rate_requests(
    hotel_ids: list[str],
    available: dict[str, set[date]],
    guests_hotels: dict[tuple[int, date], set[str]],
    cfg: ScraperConfig,
) -> list[tuple[str, date, int]]:
    """Запросы тарифов (отель, ночь, гостей): 2 гостя всем, остальные — по выдаче serp_guests."""
    refs = (cfg.ref_weekday, cfg.ref_weekend)
    plan: list[tuple[str, date, int]] = []
    for hotel_id in hotel_ids:
        for ref in refs:
            night = pick_reference_night(available.get(hotel_id, set()), ref)
            if night is not None:
                plan.append((hotel_id, night, 2))
        for guests in OTHER_GUESTS:
            for ref in refs:
                if hotel_id in guests_hotels.get((guests, ref), set()):
                    plan.append((hotel_id, ref, guests))
    return plan


def _rates_item(api: OstrovokApi, key: str, hotel_id: str, region_id: int, night: date, paxes: list[dict]) -> Item:
    return Item(
        key,
        {"hotel": hotel_id, "night": night.isoformat(), "region_id": region_id, "paxes": paxes},
        lambda: api.rates(hotel_id, region_id, night, paxes),
    )


def child_age_items(
    api: OstrovokApi, hotel_ids: list[str], regions: dict[str, int], available: dict[str, set[date]], cfg: ScraperConfig
) -> list[Item]:
    items = []
    for hotel_id in hotel_ids:
        night = child_age_night(available.get(hotel_id, set()), cfg)
        if night is not None:
            items.append(
                _rates_item(api, child_age_key(hotel_id, night), hotel_id, regions[hotel_id], night, CHILD_PROBE_PAXES)
            )
    return items


def rates_items(api: OstrovokApi, plan: list[tuple[str, date, int]], regions: dict[str, int]) -> list[Item]:
    return [_rates_item(api, rates_key(h, night, k), h, regions[h], night, paxes_for_guests(k)) for h, night, k in plan]


def _with_pages(store: RawStore, limit_hotels: int | None) -> tuple[list[str], dict[str, int]]:
    """Отобранные отели со страницей и их region_id; печатает число пропущенных без страницы."""
    facts = page_facts(store)
    ids = selected_hotels(serp_hotels(store), limit_hotels)
    with_page = [h for h in ids if h in facts]
    if len(with_page) < len(ids):
        print(f"Пропущено отелей без страницы: {len(ids) - len(with_page)}")
    return with_page, {h: facts[h][0] for h in with_page}


def run_child_age(
    api: OstrovokApi,
    store: RawStore,
    cfg: ScraperConfig,
    *,
    limit_hotels: int | None = None,
    retry_failed: bool = False,
) -> StepReport:
    ids, regions = _with_pages(store, limit_hotels)
    items = child_age_items(api, ids, regions, available_nights(store), cfg)
    return run_items(store, "child_age", items, retry_failed=retry_failed)


def run_rates(
    api: OstrovokApi,
    store: RawStore,
    cfg: ScraperConfig,
    *,
    limit_hotels: int | None = None,
    retry_failed: bool = False,
) -> StepReport:
    ids, regions = _with_pages(store, limit_hotels)
    plan = plan_rate_requests(ids, available_nights(store), guests_hotels(store), cfg)
    return run_items(store, "rates", rates_items(api, plan, regions), retry_failed=retry_failed)
