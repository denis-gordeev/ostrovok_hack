from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ostrovok_scraper.rows import RegionRow

_SKIP = (KeyError, IndexError, TypeError, ValueError, InvalidOperation)


@dataclass(frozen=True)
class MapHotel:
    """Отель из карточек выдачи на карте."""

    hotel_id: str
    master_id: int
    region_slug: str
    name: str
    stars: int
    price: Decimal


def _stars(value: object) -> int:
    stars = int(value or 0)
    return stars // 10 if stars > 5 else stars


def parse_map_hotels(body: dict) -> tuple[list[MapHotel], int]:
    """Отели выдачи и число пропущенных (без id, master_id или цены)."""
    hotels: list[MapHotel] = []
    skipped = 0
    for item in body.get("map_hotels") or []:
        try:
            hotels.append(
                MapHotel(
                    hotel_id=item["ota_hotel_id"],
                    master_id=int(item["master_id"]),
                    region_slug=item.get("region_catalog_slug") or "",
                    name=item.get("name") or "",
                    stars=_stars(item.get("stars")),
                    price=Decimal(str(item["price"])),
                )
            )
        except _SKIP:
            skipped += 1
    return hotels, skipped


def parse_session_region(region: dict) -> RegionRow:
    """Регион поиска из session_region."""
    center = region.get("center") or {}
    return RegionRow(
        region_id=int(region["region_id"]),
        name=region["name"],
        name_en=region.get("name_en"),
        type=region.get("type"),
        parent_id=None,
        country_code=region.get("country_code"),
        lat=center.get("lat"),
        lon=center.get("lng"),
    )
