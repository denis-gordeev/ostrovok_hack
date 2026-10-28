from datetime import date

from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import OTHER_GUESTS, ScraperConfig
from ostrovok_scraper.domain import paxes_for_guests, serp_key, window_nights
from ostrovok_scraper.steps import Item, StepReport, run_items
from ostrovok_scraper.store import RawStore


def _item(api: OstrovokApi, night: date, guests: int) -> Item:
    paxes = paxes_for_guests(guests)
    return Item(
        serp_key(night, guests),
        {"night": night.isoformat(), "paxes": paxes},
        lambda: api.search(night, paxes),
    )


def serp_items(api: OstrovokApi, cfg: ScraperConfig, *, today: date, nights: int | None) -> list[Item]:
    """Поиск на 2 гостей по каждой ночи окна."""
    return [_item(api, n, 2) for n in window_nights(today, cfg.window_to, nights)]


def serp_guests_items(api: OstrovokApi, cfg: ScraperConfig) -> list[Item]:
    """Поиск на остальное число гостей в обе опорные ночи."""
    return [_item(api, n, k) for k in OTHER_GUESTS for n in (cfg.ref_weekday, cfg.ref_weekend)]


def run_serp(
    api: OstrovokApi,
    store: RawStore,
    cfg: ScraperConfig,
    *,
    today: date,
    nights: int | None = None,
    retry_failed: bool = False,
) -> StepReport:
    return run_items(store, "serp", serp_items(api, cfg, today=today, nights=nights), retry_failed=retry_failed)


def run_serp_guests(api: OstrovokApi, store: RawStore, cfg: ScraperConfig, *, retry_failed: bool = False) -> StepReport:
    return run_items(store, "serp_guests", serp_guests_items(api, cfg), retry_failed=retry_failed)
