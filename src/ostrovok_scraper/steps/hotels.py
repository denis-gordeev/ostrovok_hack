from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.domain import hotel_key
from ostrovok_scraper.parsing.serp import MapHotel
from ostrovok_scraper.steps import Item, StepReport, run_items
from ostrovok_scraper.steps.inputs import selected_hotels, serp_hotels
from ostrovok_scraper.store import RawStore


def hotel_items(api: OstrovokApi, hotels: dict[str, MapHotel], hotel_ids: list[str]) -> list[Item]:
    """Запросы страниц отелей по slug региона и master_id из выдачи."""
    items = []
    for hotel_id in hotel_ids:
        h = hotels[hotel_id]
        items.append(Item(
            hotel_key(hotel_id),
            {"region_slug": h.region_slug, "master_id": h.master_id},
            lambda h=h: api.hotel_page(h.region_slug, h.master_id, h.hotel_id),
        ))
    return items


def run_hotels(
    api: OstrovokApi, store: RawStore, cfg: ScraperConfig, *,
    limit_hotels: int | None = None, retry_failed: bool = False,
) -> StepReport:
    hotels = serp_hotels(store)
    ids = selected_hotels(hotels, limit_hotels)
    return run_items(store, "hotels", hotel_items(api, hotels, ids), retry_failed=retry_failed)
