from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from ostrovok_scraper.config import MSK
from ostrovok_scraper.domain import night_type
from ostrovok_scraper.rows import RateRow

_SKIP_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, InvalidOperation)
_RATIO_STEP = Decimal("0.0001")
_PROBE_FREE_AGE = 5
_PROBE_DEFAULT_AGE = 3


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_rate(rate: dict, *, hotel_id: str, night: date, requested_guests: int) -> RateRow:
    """Одна строка тарифа; на неполных данных бросает исключение из _SKIP_ERRORS."""
    payment = rate["payment_options"]["payment_types"][0]
    price = Decimal(str(payment["amount"]))
    if not price.is_finite():
        raise ValueError("price is not finite")
    payment_type = payment["type"]
    if not payment_type:
        raise ValueError("payment type is empty")
    rg_hash = str(rate["rooms"][0]["rg_hash"])
    meals = rate.get("meal") or ["nomeal"]
    meal_data = (rate.get("meal_data") or {}).get("meals") or []
    cancellation = rate.get("cancellation_info") or {}
    policies = cancellation.get("policies")
    deadline = _datetime(cancellation.get("free_cancellation_before"))
    hours = None
    if deadline is not None:
        delta = datetime.combine(night, time(0), MSK) - deadline
        hours = round(Decimal(str(delta.total_seconds())) / 3600, 1)
    ratio = None
    if policies and price:
        worst = max(Decimal(str(p["penalty"]["amount"])) for p in policies)
        ratio = (worst / price).quantize(_RATIO_STEP)
    no_show = (rate.get("no_show") or {}).get("penalty") or {}
    return RateRow(
        hotel_id=hotel_id,
        rg_hash=rg_hash,
        night=night,
        night_type=night_type(night),
        requested_guests=requested_guests,
        occupancy=rate.get("occupancy"),
        room_name=rate.get("room_name"),
        meal=",".join(meals),
        has_breakfast=any(m.get("has_breakfast") for m in meal_data),
        payment_type=payment_type,
        price=price,
        discount_pct=_decimal((payment.get("discounts_info") or {}).get("total_percent")),
        free_cancellation_before=deadline,
        free_cancellation_hours=hours,
        penalty_after_ratio=ratio,
        cancellation_policies=policies,
        no_show_penalty=_decimal(no_show.get("amount")),
        no_show_nights=no_show.get("night_count"),
        allotment=rate.get("allotment"),
        bed_places=rate.get("bed_places"),
        source_rate_hash=rate.get("hash"),
    )


def parse_rates(body: dict, *, hotel_id: str, night: date, requested_guests: int) -> tuple[list[RateRow], int]:
    """Тарифы ответа: по одной строке на (rg_hash, meal, payment_type), самая дешёвая; и число пропущенных."""
    best: dict[tuple[str, str, str], RateRow] = {}
    skipped = 0
    for rate in body.get("rates") or []:
        try:
            row = _parse_rate(rate, hotel_id=hotel_id, night=night, requested_guests=requested_guests)
        except _SKIP_ERRORS:
            skipped += 1
            continue
        key = (row.rg_hash, row.meal, row.payment_type)
        if key not in best or row.price < best[key].price:
            best[key] = row
    return list(best.values()), skipped


def free_child_age(probe_body: dict) -> int:
    """Возраст бесплатного ребёнка: 5, если на пробе (2 взрослых + ребёнок 4 года) есть тариф на 2 места; иначе 3."""
    rates = probe_body.get("rates") or []
    if any(rate.get("occupancy") == 2 for rate in rates):
        return _PROBE_FREE_AGE
    return _PROBE_DEFAULT_AGE
