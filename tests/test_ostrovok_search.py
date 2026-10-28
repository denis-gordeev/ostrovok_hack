from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from hotel_agent.apis.ostrovok.models import (
    City,
    DataWindow,
    HotelRecord,
    OstrovokError,
    RateTemplate,
    StaySearch,
)
from hotel_agent.apis.ostrovok.search import (
    MSK,
    rank_hotels,
    resolve_city,
    room_occupancies,
    split_guests,
    validate_search,
)

TODAY = date(2026, 10, 27)
WINDOW = DataWindow(first_night=date(2026, 10, 26), last_night=date(2026, 11, 29), scraped_on=date(2026, 10, 26))
# Чт 19.11 → вс 22.11: ночи чт (будни), пт и сб (выходные).
CHECKIN, CHECKOUT = date(2026, 11, 19), date(2026, 11, 22)
KREMLIN = {"lat": 55.7986, "lon": 49.1056}


def stay(**overrides) -> StaySearch:
    return StaySearch(**{"checkin": CHECKIN, "checkout": CHECKOUT, **overrides})


def hotel(hotel_id="h1", prices=(1000, 2000, 3000), **overrides) -> HotelRecord:
    fields = {
        "hotel_id": hotel_id,
        "master_id": 100,
        "name": f"Отель {hotel_id}",
        "kind": "hotel",
        "stars": 3,
        "city": "Казань",
        "city_lat": 55.796,
        "city_lon": 49.106,
        "catalog_slug": "russia/kazan",
        "address": "улица Баумана, 1",
        "lat": 55.79,
        "lon": 49.11,
        "rating": Decimal("8.5"),
        "rating_count": 40,
        "check_in_from": None,
        "check_out_until": None,
        "flags": ["has_parking"],
        "free_child_age": 3,
        "metro": [{"name": "Кремлёвская", "distance_m": 400}, {"name": "Тукая", "distance_m": 900}],
        "night_prices": {CHECKIN + timedelta(days=i): Decimal(p) for i, p in enumerate(prices)},
    }
    return HotelRecord(**{**fields, **overrides})


def rate(hotel_id="h1", ratio="1.0", **overrides) -> RateTemplate:
    fields = {
        "hotel_id": hotel_id,
        "rg_hash": "std",
        "room_name": "Стандарт",
        "night_type": "weekday",
        "guests": 2,
        "meal": "nomeal",
        "has_breakfast": False,
        "payment_type": "now",
        "price_ratio": Decimal(ratio),
        "free_cancellation_hours": None,
        "allotment": 5,
    }
    return RateTemplate(**{**fields, **overrides})


def both_types(**overrides) -> list[RateTemplate]:
    return [rate(night_type="weekday", **overrides), rate(night_type="weekend", **overrides)]


def search(request: StaySearch, hotels, templates, today=TODAY):
    return rank_hotels(request, hotels, templates, today=today)


# --- проверка запроса ---


def test_valid_request_has_no_errors():
    assert validate_search(stay(adults=4, rooms=2, children_ages=[3, 7]), today=TODAY, window=WINDOW) == []


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"checkout": CHECKIN}, "Дата выезда должна быть позже даты заезда."),
        ({"checkin": date(2026, 10, 26)}, "Дата заезда уже прошла."),
        ({"checkout": date(2026, 12, 1)}, "выезда не позже 30.11.2026"),
        ({"adults": 1, "rooms": 2}, "хотя бы один взрослый"),
        ({"adults": 7}, "не больше 6 взрослых"),
        ({"children_ages": [1, 2, 3, 4, 5]}, "не больше 4 детей"),
    ],
)
def test_invalid_request_is_explained(overrides, error):
    errors = validate_search(stay(**overrides), today=TODAY, window=WINDOW)

    assert any(error in e for e in errors), errors


def test_window_error_names_the_first_night_not_in_the_past():
    errors = validate_search(stay(checkin=date(2026, 12, 1), checkout=date(2026, 12, 2)), today=TODAY, window=WINDOW)

    assert errors == ["Цены есть только для заезда не раньше 27.10.2026 и выезда не позже 30.11.2026."]


def test_children_ages_must_be_minor():
    with pytest.raises(ValueError, match="от 0 до 17"):
        stay(children_ages=[18])


# --- города ---

CITIES = [
    City(6288539, "Республика Татарстан", "Republic of Tatarstan"),
    City(1993, "Казань", "Kazan", 6288539),
    City(2522, "Набережные Челны", "Naberezhnye Chelny", 6288539),
    City(965896439, "Зелёный Бор", None, 6288539),
]


@pytest.mark.parametrize(
    ("query", "region_id"),
    [("Казань", 1993), ("  KAZAN ", 1993), ("челны", 2522), ("Зеленый Бор", 965896439), ("Татарстан", 6288539)],
)
def test_city_is_found_by_russian_english_or_partial_name(query, region_id):
    assert resolve_city(query, CITIES).region_id == region_id


def test_unknown_city_lists_known_ones():
    with pytest.raises(OstrovokError, match="«Москва» не найден.*Казань, Набережные Челны, Зелёный Бор"):
        resolve_city("Москва", CITIES)


# --- гости и номера ---


def test_guests_are_split_evenly_bigger_rooms_first():
    assert split_guests(5, 2) == [3, 2]
    assert split_guests(4, 2) == [2, 2]
    assert split_guests(7, 3) == [3, 2, 2]


def test_children_below_free_age_are_not_counted():
    request = stay(adults=2, children_ages=[2, 4, 10])

    assert room_occupancies(request, free_child_age=3) == [4]
    assert room_occupancies(request, free_child_age=5) == [3]


def test_rates_are_taken_for_counted_guests_per_room():
    request = stay(adults=2, children_ages=[4])
    free_from_5 = hotel("free", free_child_age=5)
    paid_from_3 = hotel("paid", free_child_age=3)
    templates = [*both_types(hotel_id="free", guests=2), *both_types(hotel_id="paid", guests=3, ratio="1.5")]

    result = search(request, [free_from_5, paid_from_3], templates)

    assert [(h.hotel_id, h.rooms[0].guests, h.price_total) for h in result.hotels] == [
        ("free", 2, 6000),
        ("paid", 3, 9000),
    ]


# --- цена ---


def test_price_is_night_price_times_ratio_of_the_same_night_type():
    templates = [rate(night_type="weekday", ratio="1.2"), rate(night_type="weekend", ratio="1.5")]

    [found] = search(stay(), [hotel()], templates).hotels

    # чт 1000 × 1.2 + пт 2000 × 1.5 + сб 3000 × 1.5
    assert (found.price_total, found.price_per_night, found.price_source) == (8700, 2900, "rates")
    assert found.rooms[0].price_total == 8700


def test_missing_night_type_takes_ratio_of_the_other_type():
    [found] = search(stay(), [hotel()], [rate(night_type="weekday", ratio="1.2")]).hotels

    assert found.price_total == 7200


def test_rooms_are_summed_and_split_by_guests():
    templates = [*both_types(guests=3, ratio="1.5", room_name="Трёхместный"), *both_types(guests=2)]

    [found] = search(stay(adults=5, rooms=2), [hotel()], templates).hotels

    assert [(r.room_name, r.guests, r.price_total) for r in found.rooms] == [
        ("Трёхместный", 3, 9000),
        ("Стандарт", 2, 6000),
    ]
    assert found.price_total == 15000


def test_offer_without_enough_rooms_left_is_skipped():
    templates = [*both_types(rg_hash="last", allotment=1), *both_types(rg_hash="many", ratio="1.1", allotment=3)]

    [found] = search(stay(adults=4, rooms=2), [hotel()], templates).hotels

    assert found.price_total == 13200


# --- требования к тарифу ---

OFFERS = [
    *both_types(rg_hash="a", meal="nomeal", ratio="1.0"),
    *both_types(rg_hash="b", meal="breakfast", has_breakfast=True, ratio="1.3", free_cancellation_hours=Decimal(36)),
    *both_types(rg_hash="c", meal="breakfast", has_breakfast=True, ratio="1.4", payment_type="hotel"),
]


@pytest.mark.parametrize(
    ("overrides", "expected_rg"),
    [
        ({}, "a"),
        ({"meal": "breakfast"}, "b"),
        ({"free_cancellation": True}, "b"),
        ({"meal": "breakfast", "pay_at_hotel": True}, "c"),
    ],
)
def test_cheapest_offer_meeting_requirements_is_chosen(overrides, expected_rg):
    names = {"a": 6000, "b": 7800, "c": 8400}

    [found] = search(stay(**overrides), [hotel()], OFFERS).hotels

    assert found.price_total == names[expected_rg]


def test_hotel_without_matching_offer_is_dropped_but_not_counted_as_without_rates():
    result = search(stay(meal="half_board"), [hotel()], OFFERS)

    assert (result.found, result.without_rates) == (0, 0)


def test_free_cancellation_deadline_is_counted_back_from_checkin_midnight_msk():
    [found] = search(stay(free_cancellation=True), [hotel()], OFFERS).hotels

    assert found.rooms[0].free_cancellation_until == datetime(2026, 11, 17, 12, 0, tzinfo=MSK)


def test_expired_free_cancellation_does_not_count():
    result = search(stay(free_cancellation=True), [hotel()], OFFERS, today=date(2026, 11, 18))

    assert result.found == 0


# --- отели без тарифов ---


def test_two_guests_without_rates_get_min_price_of_each_night():
    [found] = search(stay(rooms=2, adults=4), [hotel()], []).hotels

    assert (found.price_total, found.price_source, found.rooms) == (12000, "min_price", [])


@pytest.mark.parametrize("overrides", [{"adults": 3}, {"meal": "breakfast"}, {"pay_at_hotel": True}])
def test_hotel_without_rates_for_the_request_is_only_counted(overrides):
    result = search(stay(**overrides), [hotel()], [])

    assert (result.found, result.without_rates) == (0, 1)


# --- фильтры отеля ---


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"min_stars": 4}, ["five"]),
        ({"min_rating": 9}, ["five"]),
        ({"kinds": ["hostel"]}, ["hostel"]),
        ({"amenities": ["pool", "ecar_charger"]}, ["five"]),
        ({"max_metro_distance_m": 500}, ["hostel"]),
        ({"name": "корстон"}, ["five"]),
        ({"name": "Хостел"}, ["hostel"]),
        ({"near": KREMLIN, "max_distance_km": 1}, ["hostel"]),
        ({"max_distance_km": 5}, ["hostel", "five"]),
        ({"max_price_per_night": 1500}, ["hostel"]),
    ],
)
def test_hotel_filters(overrides, expected):
    hotels = [
        hotel(
            "hostel",
            name="Хостел У Кремля",
            kind="hostel",
            stars=0,
            rating=Decimal(0),
            rating_count=0,
            lat=55.7990,
            lon=49.1060,
            prices=(500, 500, 500),
        ),
        hotel(
            "korston_royal_hotel",
            name="Отель Korston Royal",
            stars=5,
            rating=Decimal("9.1"),
            lat=55.78,
            lon=49.15,
            flags=["has_pool", "has_ecar_charger "],
            metro=[{"name": "Козья слобода", "distance_m": 1800}],
        ),
    ]
    templates = [*both_types(hotel_id="hostel"), *both_types(hotel_id="korston_royal_hotel")]

    result = search(stay(**overrides), hotels, templates)

    assert [h.hotel_id.replace("korston_royal_hotel", "five") for h in result.hotels] == expected


# --- сортировка и ответ ---


def test_sorting_and_limit():
    hotels = [
        hotel("cheap", prices=(500, 500, 500), rating=Decimal("7.0"), lat=55.70),
        hotel("best", prices=(900, 900, 900), rating=Decimal("9.5"), lat=55.85),
        hotel("near", prices=(700, 700, 700), rating=Decimal("8.0"), lat=55.80),
    ]
    templates = [t for h in hotels for t in both_types(hotel_id=h.hotel_id)]

    def order(**overrides):
        result = search(stay(**overrides), hotels, templates)
        return result.found, [h.hotel_id for h in result.hotels]

    assert order() == (3, ["cheap", "near", "best"])
    assert order(sort_by="rating", limit=2) == (3, ["best", "near"])
    assert order(sort_by="distance", near=KREMLIN) == (3, ["near", "best", "cheap"])


def test_candidate_describes_hotel_for_the_agent():
    record = hotel(stars=0, rating=Decimal(0), rating_count=0, flags=["has_internet", "has_ecar_charger ", "has_meal"])

    [found] = search(stay(), [record], both_types()).hotels

    assert found.stars is None
    assert found.rating is None
    assert found.amenities == ["internet", "ecar_charger"]
    assert (found.metro, found.metro_distance_m) == ("Кремлёвская", 400)
    assert found.distance_km == 0.7
    assert found.url == "https://ostrovok.ru/hotel/russia/kazan/mid100/h1/"
