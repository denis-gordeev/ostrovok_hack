from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

HotelKind = Literal[
    "hotel",
    "mini_hotel",
    "boutique",
    "apart_hotel",
    "hostel",
    "guesthouse",
    "sanatorium",
    "resort",
    "lodging_houses",
    "living_quarters",
]
# Минимальный уровень питания: каждый следующий включает предыдущие.
MealPlan = Literal["breakfast", "half_board", "full_board", "all_inclusive"]
Amenity = Literal[
    "internet",
    "parking",
    "air_conditioning",
    "kitchen",
    "washer",
    "kids",
    "pets",
    "pool",
    "spa",
    "jacuzzi",
    "fitness",
    "airport_transfer",
    "business",
    "disabled_support",
    "smoking",
    "beach",
    "skiing",
    "ecar_charger",
]
SortBy = Literal["price", "rating", "distance"]


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90, description="Широта")
    lon: float = Field(ge=-180, le=180, description="Долгота")


class StaySearch(BaseModel):
    """Запрос подбора отелей Татарстана. Эту схему видит LLM агента, описания полей — подсказки для неё."""

    checkin: date = Field(description="Дата заезда, YYYY-MM-DD")
    checkout: date = Field(description="Дата выезда, YYYY-MM-DD")
    city: str | None = Field(
        default=None,
        description="Город в Татарстане в именительном падеже, например «Казань»; не задан — весь Татарстан",
    )
    quiet: bool
    adults: int = Field(default=2, ge=1, le=60, description="Количество взрослых")
    children_ages: list[int] = Field(
        default_factory=list, description="Возраст каждого ребёнка на дату заезда (0–17), если есть дети"
    )
    rooms: int = Field(default=1, ge=1, le=10, description="Количество номеров; гости делятся между ними поровну")

    max_price_per_night: int | None = Field(
        default=None, gt=0, description="Бюджет в рублях за ночь за все номера вместе"
    )
    min_stars: int | None = Field(
        default=None, ge=1, le=5, description="Минимум звёзд. У большинства мини-отелей и хостелов звёзд нет"
    )
    min_rating: float | None = Field(
        default=None, ge=0, le=10, description="Минимальная оценка гостей из 10; отели без отзывов отсеются"
    )
    kinds: list[HotelKind] = Field(
        default_factory=list,
        description="Типы размещения (hotel, mini_hotel, boutique, apart_hotel, hostel, guesthouse, sanatorium, "
        "resort; lodging_houses и living_quarters — гостевые комнаты и квартиры); пусто — любые",
    )
    meal: MealPlan | None = Field(
        default=None, description="Минимальное питание в тарифе: breakfast < half_board < full_board < all_inclusive"
    )
    free_cancellation: bool = Field(default=False, description="Только тарифы с бесплатной отменой")
    pay_at_hotel: bool = Field(default=False, description="Только тарифы с оплатой на месте")
    amenities: list[Amenity] = Field(
        default_factory=list,
        description="Обязательные удобства отеля: kids — подходит для детей, pets — можно с животными, "
        "business — конференц-зал, smoking — можно курить",
    )
    near: GeoPoint | None = Field(
        default=None,
        description="Точка, к которой нужно быть поближе (достопримечательность, адрес); от неё считается distance_km",
    )
    max_distance_km: float | None = Field(
        default=None, gt=0, description="Не дальше этого расстояния от near, а без near — от центра города отеля"
    )
    max_metro_distance_m: int | None = Field(
        default=None, gt=0, description="Не дальше этого расстояния до метро (метро есть только в Казани)"
    )
    name: str | None = Field(
        default=None, description="Часть названия отеля, если пользователь спрашивает о конкретном"
    )
    sort_by: SortBy = Field(
        default="price", description="price — дешевле выше, rating — оценка выше, distance — ближе к near или центру"
    )
    limit: int = Field(default=10, ge=1, le=30, description="Сколько отелей вернуть")

    @model_validator(mode="after")
    def _children_are_minors(self):
        if any(not 0 <= age <= 17 for age in self.children_ages):
            raise ValueError("возраст ребёнка должен быть от 0 до 17 лет")
        return self


class RoomOffer(BaseModel):
    """Тариф одного номера на весь период."""

    room_name: str
    guests: int = Field(description="Гостей в номере; бесплатные малыши не считаются")
    meal: str = Field(description="nomeal, breakfast, half-board-dinner, dinner, full-board, all-inclusive")
    price_total: int
    free_cancellation_until: datetime | None = Field(default=None, description="null — без бесплатной отмены")
    pay_at_hotel: bool


class HotelCandidate(BaseModel):
    hotel_id: str
    name: str
    kind: str | None = None
    stars: int | None = None
    city: str | None = None
    address: str | None = None
    lat: float
    lon: float
    distance_km: float | None = Field(default=None, description="До near, а без него — до центра города")
    metro: str | None = None
    metro_distance_m: int | None = None
    rating: float | None = Field(default=None, description="Оценка гостей из 10; null — отзывов нет")
    reviews_count: int = 0
    check_in_from: time | None = None
    check_out_until: time | None = None
    amenities: list[Amenity] = Field(default_factory=list)
    free_child_age: int = Field(description="С этого возраста ребёнок считается гостем и оплачивается")
    price_total: int = Field(description="Рубли за все номера и все ночи")
    price_per_night: int
    price_source: Literal["rates", "min_price"] = Field(
        description="rates — оценка по тарифам номеров; "
        "min_price — минимальная цена ночи на 2 гостей, без деталей тарифа (для 1 гостя бывает дешевле)"
    )
    rooms: list[RoomOffer] = Field(default_factory=list)
    url: str | None = None


class SearchResult(BaseModel):
    checkin: date
    checkout: date
    nights: int
    found: int = Field(description="Сколько отелей подходит; в hotels — первые limit")
    without_rates: int = Field(
        default=0, description="Сколько свободных отелей пропущено: нет тарифов на такой состав гостей"
    )
    data_as_of: date | None = Field(default=None, description="Дата снимка цен островка")
    hotels: list[HotelCandidate]


class OstrovokError(Exception):
    """Запрос не выполнен: неверные параметры или недоступна база. Текст — для агента."""


@dataclass(frozen=True)
class DataWindow:
    """Ночи заезда, на которые в базе есть цены, и дата снимка."""

    first_night: date
    last_night: date
    scraped_on: date | None = None


@dataclass(frozen=True)
class City:
    """Регион из ostrovok.regions; parent_id = None у самого Татарстана."""

    region_id: int
    name: str
    name_en: str | None = None
    parent_id: int | None = None


@dataclass
class HotelRecord:
    """Отель, свободный на все ночи проживания, с минимальной ценой каждой ночи (2 гостя)."""

    hotel_id: str
    master_id: int
    name: str
    kind: str | None
    stars: int | None
    city: str | None
    city_lat: float | None
    city_lon: float | None
    catalog_slug: str | None
    address: str | None
    lat: float
    lon: float
    rating: Decimal | None
    rating_count: int | None
    check_in_from: time | None
    check_out_until: time | None
    flags: list[str]
    free_child_age: int
    metro: list[dict] | None
    night_prices: dict[date, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class RateTemplate:
    """Тариф опорной ночи: цена = min_price ночи × price_ratio."""

    hotel_id: str
    rg_hash: str
    room_name: str | None
    night_type: Literal["weekday", "weekend"]
    guests: int
    meal: str
    has_breakfast: bool
    payment_type: str
    price_ratio: Decimal
    free_cancellation_hours: Decimal | None
    allotment: int | None
