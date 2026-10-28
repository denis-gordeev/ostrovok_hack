from math import ceil

from ostrovok_scraper.api import OstrovokApi
from ostrovok_scraper.config import ScraperConfig
from ostrovok_scraper.domain import reviews_key
from ostrovok_scraper.steps import Item, StepReport, run_items
from ostrovok_scraper.steps.inputs import (page_facts, selected_hotels,
                                           serp_hotels)
from ostrovok_scraper.store import RawStore

PAGE_SIZE = 500
# Столько отзывов уже есть на странице отеля.
ON_PAGE = 10


def review_pages(total: int, page_size: int = PAGE_SIZE) -> int:
    """Число страниц API отзывов; 0, если все отзывы уже на странице отеля."""
    return 0 if total <= ON_PAGE else ceil(total / page_size)


def reviews_items(api: OstrovokApi, totals: dict[str, int]) -> list[Item]:
    items = []
    for hotel_id, total in totals.items():
        for page in range(1, review_pages(total) + 1):
            items.append(
                Item(
                    reviews_key(hotel_id, page),
                    {"hotel": hotel_id, "page": page, "page_size": PAGE_SIZE},
                    lambda h=hotel_id, p=page: api.reviews(h, p, PAGE_SIZE),
                )
            )
    return items


def run_reviews(
    api: OstrovokApi,
    store: RawStore,
    cfg: ScraperConfig,
    *,
    limit_hotels: int | None = None,
    retry_failed: bool = False,
) -> StepReport:
    facts = page_facts(store)
    ids = selected_hotels(serp_hotels(store), limit_hotels)
    missing = [h for h in ids if h not in facts]
    if missing:
        print(f"Пропущено отелей без страницы: {len(missing)}")
    totals = {h: facts[h][1] for h in ids if h in facts}
    return run_items(store, "reviews", reviews_items(api, totals), retry_failed=retry_failed)
