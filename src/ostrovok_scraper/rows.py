from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal


@dataclass(frozen=True)
class RegionRow:
    """Строка ostrovok.regions."""

    region_id: int
    name: str
    name_en: str | None = None
    type: str | None = None
    parent_id: int | None = None
    country_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    catalog_slug: str | None = None


@dataclass(frozen=True)
class HotelRow:
    """Строка ostrovok.hotels; jsonb-поля — dict/list (оборачиваются в Jsonb при записи)."""

    hotel_id: str
    master_id: int
    region_id: int | None
    name: str
    kind: str | None
    stars: int | None
    address: str | None
    lat: float | None
    lon: float | None
    rating: Decimal | None
    rating_count: int | None
    rating_details: dict | None
    check_in_from: time | None
    check_out_until: time | None
    description: list | None
    amenities: list | None  # группы удобств как есть (hotel.amenityGroups)
    flags: list[str] = field(default_factory=list)
    payment_methods: list[str] = field(default_factory=list)
    policies: dict | None = None
    metro: list | None = None
    images: list[str] = field(default_factory=list)
    reviews_total: int | None = None
    free_child_age: int = 3
    free_child_age_source: str = "default"


@dataclass(frozen=True)
class RoomRow:
    """Строка ostrovok.rooms."""

    hotel_id: str
    rg_hash: str
    parent_rg_hash: str | None
    name: str
    bedding_type: str | None
    bathroom: str | None
    size_m2: Decimal | None
    amenities: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RateRow:
    """Строка ostrovok.rates (без id и price_ratio — их считает БД)."""

    hotel_id: str
    rg_hash: str
    night: date
    night_type: str
    requested_guests: int
    occupancy: int | None
    room_name: str | None
    meal: str
    has_breakfast: bool
    payment_type: str
    price: Decimal
    discount_pct: Decimal | None
    free_cancellation_before: datetime | None
    free_cancellation_hours: Decimal | None
    penalty_after_ratio: Decimal | None
    cancellation_policies: list | None
    no_show_penalty: Decimal | None
    no_show_nights: int | None
    allotment: int | None
    bed_places: list | dict | None
    source_rate_hash: str | None


@dataclass(frozen=True)
class ReviewRow:
    """Строка ostrovok.reviews."""

    review_id: int
    hotel_id: str
    review_date: datetime
    rating: Decimal | None
    details: dict | None
    plus: str | None
    minus: str | None
    hotel_reply: str | None
    traveller_type: str | None
    trip_type: str | None
    stay_check_in: date | None
    stay_check_out: date | None
    adults: int | None
    room_name: str | None
    likes: int | None
    images: list[str] = field(default_factory=list)
