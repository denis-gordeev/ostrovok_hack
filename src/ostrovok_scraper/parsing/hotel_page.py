import json
import re
from datetime import time
from decimal import Decimal

from ostrovok_scraper.rows import HotelRow, RegionRow, RoomRow

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_PAGE_KEYS = ("hotel", "hotelRegionData", "reviews")
_HOTEL_IMAGES = 5
_ROOM_IMAGES = 3


def extract_next_data(html: str) -> dict:
    """Нужные части props.pageProps из __NEXT_DATA__ страницы отеля."""
    match = _NEXT_DATA.search(html)
    if match is None:
        raise ValueError("__NEXT_DATA__ not found")
    props = json.loads(match.group(1))["props"]["pageProps"]
    return {key: props[key] for key in _PAGE_KEYS if key in props}


def _time(value: dict | None) -> time | None:
    if not value:
        return None
    try:
        return time(int(value["hours"]), int(value.get("minutes") or 0))
    except (KeyError, TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _images(items: list | None, limit: int) -> list[str]:
    return [img["tmpl"] for img in items or [] if isinstance(img, dict) and img.get("tmpl")][:limit]


def parse_hotel(page: dict, *, stars: int | None) -> HotelRow:
    """Строка отеля; контакты не копируются."""
    hotel = page["hotel"]
    if not hotel.get("name"):
        raise ValueError("hotel name is empty")
    location = hotel.get("location") or {}
    rating = hotel.get("rating") or {}
    policies = hotel.get("policies") or {}
    check = (policies.get("checkInCheckOut") or {}).get("value") or {}
    kind = (hotel.get("facts") or {}).get("kind")
    return HotelRow(
        hotel_id=hotel["id"],
        master_id=int(hotel["masterId"]),
        region_id=location.get("regionId"),
        name=hotel["name"],
        kind=kind.lower() if kind else None,
        stars=stars,
        address=location.get("address"),
        lat=location.get("latitude"),
        lon=location.get("longitude"),
        rating=_decimal(rating.get("total")),
        rating_count=rating.get("reviewsCount"),
        rating_details=((rating.get("ostrovok") or {}).get("detailedRatings")),
        check_in_from=_time(check.get("checkInTimeStart")),
        check_out_until=_time(check.get("checkOutTimeEnd")),
        description=hotel.get("descriptionStruct"),
        amenities=hotel.get("amenityGroups") or [],
        flags=[f.lower() for f in hotel.get("serpFilters") or []],
        payment_methods=list(hotel.get("paymentMethods") or []),
        policies={
            name: p.get("value") for name, p in policies.items() if name != "checkInCheckOut" and isinstance(p, dict)
        },
        metro=[
            {"name": s["name"], "distance_m": s["distance"]}
            for s in location.get("nearestSubways") or []
            if s.get("name") and s.get("distance") is not None
        ],
        images=_images(hotel.get("images"), _HOTEL_IMAGES),
        reviews_total=(page.get("reviews") or {}).get("total"),
    )


def parse_rooms(page: dict) -> list[RoomRow]:
    """Группы номеров отеля; дубли rgHash отбрасываются."""
    hotel = page["hotel"]
    rooms: list[RoomRow] = []
    seen: set[str] = set()
    for group in hotel.get("roomGroups") or []:
        rg_hash = str(group["rgHash"])
        if rg_hash in seen:
            continue
        seen.add(rg_hash)
        name = group.get("nameStruct") or {}
        parent = group.get("parentRgHash")
        rooms.append(
            RoomRow(
                hotel_id=hotel["id"],
                rg_hash=rg_hash,
                parent_rg_hash=None if parent is None else str(parent),
                name=name.get("mainName") or "",
                bedding_type=name.get("beddingType") or None,
                bathroom=name.get("bathroom") or None,
                size_m2=_decimal(group.get("size")),
                amenities=list(group.get("roomAmenityData") or []),
                images=_images(group.get("images"), _ROOM_IMAGES),
            )
        )
    return rooms


def parse_city_region(page: dict, *, parent_id: int) -> RegionRow:
    """Город отеля из hotelRegionData."""
    region = page["hotelRegionData"]
    center = region.get("center") or {}
    return RegionRow(
        region_id=int(region["regionId"]),
        name=region["name"],
        name_en=region.get("nameEn"),
        type=region.get("type"),
        parent_id=parent_id,
        country_code=region.get("countryCode"),
        lat=center.get("lat"),
        lon=center.get("lng"),
        catalog_slug=region.get("catalogSlug"),
    )
