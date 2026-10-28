from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ostrovok_scraper.rows import ReviewRow

_SKIP_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, InvalidOperation)


def parse_review_date(value: str) -> datetime:
    """Дата отзыва; смещение вида `+00` дополняется до `+00:00`."""
    head, sign, offset = value.rpartition("+")
    if sign and ":" not in offset:
        value = f"{head}+{offset}:00"
    return datetime.fromisoformat(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _lower(value: str | None) -> str | None:
    return None if value is None else value.lower()


def _date(value: str | None) -> date | None:
    """Дата заезда/выезда: `2023-05-27` или timestamp `2023-05-27 00:00:00+00`; мусор даёт None."""
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _rating(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _images(items: list | None) -> list[str]:
    return [image["tmpl"] for image in items or []]


def _api_review(item: dict, *, hotel_id: str) -> ReviewRow:
    """Отзыв из API; поля автора не читаются."""
    context = item.get("order_context")
    if context is None:
        context = {}
    reply = item.get("reply")
    return ReviewRow(
        review_id=int(item["id"]),
        hotel_id=hotel_id,
        review_date=parse_review_date(item["review_date"]),
        rating=_rating(item.get("rating")),
        details=item.get("detailed"),
        plus=_text(item.get("review_plus")),
        minus=_text(item.get("review_minus")),
        hotel_reply=None if reply is None else _text(reply.get("text")),
        traveller_type=_lower(item.get("traveller_type")),
        trip_type=_lower(item.get("trip_type")),
        stay_check_in=_date(context.get("check_in")),
        stay_check_out=_date(context.get("check_out")),
        adults=context.get("adult_num"),
        room_name=context.get("author_room"),
        likes=item.get("review_likes"),
        images=_images(item.get("images")),
    )


def _page_reply(replies: list | None) -> str | None:
    """Ответ отеля со страницы: на живых страницах — список строк, встречается и форма {"text": ...}."""
    if not replies:
        return None
    first = replies[0]
    return _text(first if isinstance(first, str) else first.get("text"))


def _page_review(item: dict, *, hotel_id: str) -> ReviewRow:
    """Отзыв со страницы отеля (camelCase); поля автора не читаются."""
    context = item.get("orderContext")
    if context is None:
        context = {}
    replies = item.get("reply")
    return ReviewRow(
        review_id=int(item["id"]),
        hotel_id=hotel_id,
        review_date=parse_review_date(item["reviewDate"]),
        rating=_rating(item.get("rating")),
        details=item.get("detailed"),
        plus=_text(item.get("reviewPlus")),
        minus=_text(item.get("reviewMinus")),
        hotel_reply=_page_reply(replies),
        traveller_type=_lower(item.get("travellerType")),
        trip_type=_lower(item.get("tripType")),
        stay_check_in=_date(context.get("checkIn")),
        stay_check_out=_date(context.get("checkOut")),
        adults=context.get("adultNum"),
        room_name=context.get("authorRoom"),
        likes=item.get("reviewLikes"),
        images=_images(item.get("images")),
    )


def parse_api_reviews(body: dict, *, hotel_id: str) -> tuple[list[ReviewRow], int]:
    """Отзывы из API отзывов: (строки, число пропущенных)."""
    rows: list[ReviewRow] = []
    skipped = 0
    for item in body.get("reviews") or []:
        try:
            rows.append(_api_review(item, hotel_id=hotel_id))
        except _SKIP_ERRORS:
            skipped += 1
    return rows, skipped


def parse_page_reviews(reviews: dict, *, hotel_id: str) -> tuple[list[ReviewRow], int]:
    """Отзывы со страницы отеля (`pageProps.reviews`): (строки, число пропущенных)."""
    rows: list[ReviewRow] = []
    skipped = 0
    for item in reviews.get("list") or []:
        try:
            rows.append(_page_review(item, hotel_id=hotel_id))
        except _SKIP_ERRORS:
            skipped += 1
    return rows, skipped
