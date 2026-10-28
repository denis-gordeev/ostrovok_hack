"""Подбор и оценка цены отелей по снимку островка. Чистые функции: данные приходят из OstrovokClient.

Модель цены (см. docs/superpowers/specs/2026-10-26-ostrovok-scrape-design.md, раздел 12): отель свободен,
если на каждую ночь есть минимальная цена для 2 гостей; цена тарифа на ночь = эта цена × price_ratio тарифа
опорной ночи того же типа (будни/выходные) и того же числа гостей в номере; цена номера — сумма по ночам.
"""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from hotel_agent.apis.ostrovok.models import (
    Amenity,
    City,
    DataWindow,
    HotelCandidate,
    HotelRecord,
    OstrovokError,
    RateTemplate,
    RoomOffer,
    SearchResult,
    StaySearch,
)

MSK = ZoneInfo("Europe/Moscow")
SITE_URL = "https://ostrovok.ru"

# Лимиты островка на номер (иначе HTTP 400 invalid_paxes) и максимум гостей в номере, для которого есть тарифы.
MAX_ADULTS_PER_ROOM = 6
MAX_CHILDREN_PER_ROOM = 4
MAX_GUESTS_PER_ROOM = 8

MEAL_LEVEL = {"nomeal": 0, "dinner": 0, "breakfast": 1, "half-board-dinner": 2, "full-board": 3, "all-inclusive": 4}
REQUIRED_MEAL_LEVEL = {"breakfast": 1, "half_board": 2, "full_board": 3, "all_inclusive": 4}

AMENITY_FLAGS: dict[Amenity, str] = {
    "internet": "has_internet",
    "parking": "has_parking",
    "air_conditioning": "air_conditioning",
    "kitchen": "kitchen",
    "washer": "has_washer",
    "kids": "has_kids",
    "pets": "has_pets",
    "pool": "has_pool",
    "spa": "has_spa",
    "jacuzzi": "has_jacuzzi",
    "fitness": "has_fitness",
    "airport_transfer": "has_airport_transfer",
    "business": "has_business",
    "disabled_support": "has_disabled_support",
    "smoking": "has_smoking",
    "beach": "beach",
    "skiing": "has_skiing",
    "ecar_charger": "has_ecar_charger",
}


def validate_search(request: StaySearch, *, today: date, window: DataWindow) -> list[str]:
    """Проверяет запрос до подбора. Возвращает список проблем."""
    errors: list[str] = []
    last_checkout = window.last_night + timedelta(days=1)
    if request.checkout <= request.checkin:
        errors.append("Дата выезда должна быть позже даты заезда.")
    if request.checkin < today:
        errors.append("Дата заезда уже прошла.")
    elif request.checkin < window.first_night or request.checkout > last_checkout:
        first = max(window.first_night, today)
        errors.append(
            f"Цены есть только для заезда не раньше {first:%d.%m.%Y} и выезда не позже {last_checkout:%d.%m.%Y}."
        )
    if request.adults < request.rooms:
        errors.append("В каждом номере должен быть хотя бы один взрослый.")
    if request.adults > MAX_ADULTS_PER_ROOM * request.rooms:
        errors.append(f"В номере не больше {MAX_ADULTS_PER_ROOM} взрослых — нужно больше номеров.")
    if len(request.children_ages) > MAX_CHILDREN_PER_ROOM * request.rooms:
        errors.append(f"В номере не больше {MAX_CHILDREN_PER_ROOM} детей — нужно больше номеров.")
    return errors


def resolve_city(query: str, cities: Sequence[City]) -> City:
    """Город по названию (русскому или английскому); сначала точное совпадение, потом единственное частичное."""
    wanted = _normalize(query)
    names = {c.region_id: (_normalize(c.name), _normalize(c.name_en or "")) for c in cities}
    exact = [c for c in cities if wanted in names[c.region_id]]
    partial = [c for c in cities if any(wanted in name for name in names[c.region_id] if name)]
    for found in (exact, partial):
        if len(found) == 1:
            return found[0]
    known = ", ".join(c.name for c in cities if c.parent_id is not None)
    raise OstrovokError(f"Город «{query}» не найден. Отели есть в городах: {known}.")


def split_guests(guests: int, rooms: int) -> list[int]:
    """Гости поровну по номерам, большие номера первыми: 5 гостей на 2 номера → [3, 2]."""
    base, extra = divmod(guests, rooms)
    return [base + 1] * extra + [base] * (rooms - extra)


def room_occupancies(request: StaySearch, free_child_age: int) -> list[int]:
    """Гостей в каждом номере для отеля: дети младше free_child_age живут бесплатно и не считаются."""
    counted = request.adults + sum(age >= free_child_age for age in request.children_ages)
    return split_guests(counted, request.rooms)


def stay_nights(checkin: date, checkout: date) -> list[date]:
    return [checkin + timedelta(days=i) for i in range((checkout - checkin).days)]


def night_type(night: date) -> Literal["weekday", "weekend"]:
    """Как в сборщике: ночи с пятницы и субботы — выходные."""
    return "weekend" if night.weekday() in (4, 5) else "weekday"


def free_cancellation_until(checkin: date, hours: Decimal | None, today: date) -> datetime | None:
    """Срок бесплатной отмены: 00:00 МСК дня заезда минус hours. Уже прошедший срок — как его отсутствие."""
    if hours is None:
        return None
    deadline = datetime.combine(checkin, time(0), MSK) - timedelta(hours=float(hours))
    return deadline if deadline.date() >= today else None


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по большому кругу."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def rank_hotels(
    request: StaySearch,
    hotels: Iterable[HotelRecord],
    templates: Iterable[RateTemplate],
    *,
    today: date,
    data_as_of: date | None = None,
) -> SearchResult:
    """Фильтрует отели, свободные на все ночи, считает цену на весь период и сортирует.

    hotels — отели с минимальной ценой на каждую ночь проживания; templates — их тарифы опорных ночей.
    """
    nights = stay_nights(request.checkin, request.checkout)
    templates_by_hotel: dict[str, dict[int, list[RateTemplate]]] = defaultdict(lambda: defaultdict(list))
    for template in templates:
        templates_by_hotel[template.hotel_id][template.guests].append(template)

    candidates: list[HotelCandidate] = []
    without_rates = 0
    for hotel in hotels:
        flags = {f.strip() for f in hotel.flags}
        distance = _distance(hotel, request)
        if not _hotel_matches(hotel, request, flags, distance):
            continue
        occupancies = room_occupancies(request, hotel.free_child_age)
        rooms = _cheapest_rooms(hotel, templates_by_hotel[hotel.hotel_id], occupancies, request, nights, today)
        if rooms is None and not _min_price_allowed(request, occupancies):
            without_rates += 1
            continue
        if rooms == []:
            continue
        if rooms is None:
            total = sum(hotel.night_prices[n] for n in nights) * len(occupancies)
        else:
            total = sum(offer.price for _, offer in rooms)
        per_night = _rubles(total / len(nights))
        if request.max_price_per_night is not None and per_night > request.max_price_per_night:
            continue
        candidates.append(_candidate(hotel, flags, distance, total, per_night, rooms))

    candidates.sort(key=_SORT_KEYS[request.sort_by])
    return SearchResult(
        checkin=request.checkin,
        checkout=request.checkout,
        nights=len(nights),
        found=len(candidates),
        without_rates=without_rates,
        data_as_of=data_as_of,
        hotels=candidates[: request.limit],
    )


@dataclass(frozen=True)
class _Offer:
    """Тариф одного номера на весь период. template — тариф с типом ночи заезда: от него питание, отмена, оплата."""

    template: RateTemplate
    price: Decimal
    free_cancellation_until: datetime | None


def _cheapest_rooms(
    hotel: HotelRecord,
    templates: dict[int, list[RateTemplate]],
    occupancies: list[int],
    request: StaySearch,
    nights: list[date],
    today: date,
) -> list[tuple[int, _Offer]] | None:
    """Самый дешёвый подходящий тариф для каждого номера.

    None — у отеля нет тарифов на такое число гостей в номере; [] — тарифы есть, но ни один не подходит.
    """
    chosen: list[tuple[int, _Offer]] = []
    for guests, count in Counter(occupancies).items():
        if guests > MAX_GUESTS_PER_ROOM or not templates.get(guests):
            return None
        offers = [
            offer
            for offer in _offers(hotel, templates[guests], nights, request.checkin, today)
            if _offer_matches(offer, request) and (offer.template.allotment or 0) >= count
        ]
        if not offers:
            return []
        best = min(offers, key=lambda o: (o.price, o.template.rg_hash))
        chosen += [(guests, best)] * count
    return chosen


def _offers(
    hotel: HotelRecord, templates: list[RateTemplate], nights: list[date], checkin: date, today: date
) -> list[_Offer]:
    """Тарифы опорных ночей → цены на весь период. Один тариф — это номер + питание + тип оплаты."""
    by_key: dict[tuple[str, str, str], dict[str, RateTemplate]] = defaultdict(dict)
    for t in templates:
        by_key[(t.rg_hash, t.meal, t.payment_type)][t.night_type] = t
    offers = []
    for by_type in by_key.values():
        main = by_type.get(night_type(checkin)) or next(iter(by_type.values()))
        # Нет тарифа на такой тип ночи — берём долю с другого типа: она всё равно от минимальной цены этой ночи.
        price = sum(hotel.night_prices[n] * by_type.get(night_type(n), main).price_ratio for n in nights)
        offers.append(_Offer(main, price, free_cancellation_until(checkin, main.free_cancellation_hours, today)))
    return offers


def _offer_matches(offer: _Offer, request: StaySearch) -> bool:
    t = offer.template
    if request.meal and MEAL_LEVEL.get(t.meal, int(t.has_breakfast)) < REQUIRED_MEAL_LEVEL[request.meal]:
        return False
    if request.pay_at_hotel and t.payment_type != "hotel":
        return False
    return not (request.free_cancellation and offer.free_cancellation_until is None)


def _min_price_allowed(request: StaySearch, occupancies: list[int]) -> bool:
    """Без тарифов цену можно взять из минимальной цены ночи (2 гостя), если к тарифу нет требований."""
    no_rate_filters = not (request.meal or request.free_cancellation or request.pay_at_hotel)
    return no_rate_filters and max(occupancies) <= 2


def _hotel_matches(hotel: HotelRecord, request: StaySearch, flags: set[str], distance: float | None) -> bool:
    rating = _rating(hotel)
    metro_m = _nearest_metro(hotel)[1]
    checks = (
        request.min_stars is None or (hotel.stars or 0) >= request.min_stars,
        request.min_rating is None or (rating is not None and rating >= request.min_rating),
        not request.kinds or hotel.kind in request.kinds,
        {AMENITY_FLAGS[a] for a in request.amenities} <= flags,
        request.max_metro_distance_m is None or (metro_m is not None and metro_m <= request.max_metro_distance_m),
        request.max_distance_km is None or (distance is not None and distance <= request.max_distance_km),
        not request.name or _name_matches(request.name, hotel),
    )
    return all(checks)


def _name_matches(query: str, hotel: HotelRecord) -> bool:
    """Подстрока названия или транслита в id: «корстон» находит «Отель Korston Royal» (korston_royal_hotel)."""
    wanted = _normalize(query)
    haystack = f"{_normalize(hotel.name)} {hotel.hotel_id.replace('_', ' ')}"
    return wanted in haystack or wanted.translate(_TRANSLIT) in haystack


def _distance(hotel: HotelRecord, request: StaySearch) -> float | None:
    if request.near:
        return distance_km(request.near.lat, request.near.lon, hotel.lat, hotel.lon)
    if hotel.city_lat is None or hotel.city_lon is None:
        return None
    return distance_km(hotel.city_lat, hotel.city_lon, hotel.lat, hotel.lon)


def _candidate(
    hotel: HotelRecord,
    flags: set[str],
    distance: float | None,
    total: Decimal,
    per_night: int,
    rooms: list[tuple[int, _Offer]] | None,
) -> HotelCandidate:
    metro, metro_m = _nearest_metro(hotel)
    url = None
    if hotel.catalog_slug:
        url = f"{SITE_URL}/hotel/{hotel.catalog_slug}/mid{hotel.master_id}/{hotel.hotel_id}/"
    return HotelCandidate(
        hotel_id=hotel.hotel_id,
        name=hotel.name,
        kind=hotel.kind,
        stars=hotel.stars or None,
        city=hotel.city,
        address=hotel.address,
        # В базе float4: 55.78399658203125 → 55.784
        lat=round(hotel.lat, 5),
        lon=round(hotel.lon, 5),
        distance_km=None if distance is None else round(distance, 1),
        metro=metro,
        metro_distance_m=metro_m,
        rating=_rating(hotel),
        reviews_count=hotel.rating_count or 0,
        check_in_from=hotel.check_in_from,
        check_out_until=hotel.check_out_until,
        amenities=[a for a, flag in AMENITY_FLAGS.items() if flag in flags],
        free_child_age=hotel.free_child_age,
        price_total=_rubles(total),
        price_per_night=per_night,
        price_source="min_price" if rooms is None else "rates",
        rooms=[
            RoomOffer(
                room_name=offer.template.room_name or "Номер",
                guests=guests,
                meal=offer.template.meal,
                price_total=_rubles(offer.price),
                free_cancellation_until=offer.free_cancellation_until,
                pay_at_hotel=offer.template.payment_type == "hotel",
            )
            for guests, offer in rooms or []
        ],
        url=url,
    )


def _rating(hotel: HotelRecord) -> float | None:
    """Оценка 0 у островка значит «отзывов нет»."""
    return float(hotel.rating) if hotel.rating and hotel.rating_count else None


def _nearest_metro(hotel: HotelRecord) -> tuple[str | None, int | None]:
    """Станции в metro отсортированы по расстоянию."""
    if not hotel.metro:
        return None, None
    return hotel.metro[0].get("name"), hotel.metro[0].get("distance_m")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()


# Транслит, которым островок строит id отелей: «Джан на Павлюхина» → dzhan_na_pavlyukhina.
_TRANSLIT = str.maketrans(
    {
        **dict(zip("абвгдезийклмнопрстуфыэ", "abvgdeziiklmnoprstufye", strict=True)),
        "ж": "zh", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ю": "yu", "я": "ya", "ъ": "", "ь": "",
    }
)


def _rubles(amount: Decimal) -> int:
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


_SORT_KEYS = {
    "price": lambda c: (c.price_total, c.hotel_id),
    "rating": lambda c: (-(c.rating or 0), -c.reviews_count, c.price_total, c.hotel_id),
    "distance": lambda c: (c.distance_km is None, c.distance_km or 0, c.price_total, c.hotel_id),
}
