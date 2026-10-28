from datetime import date, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
# Окно загрузки при «сегодня» 2026-10-28 и горизонте 60 дней: [28.10 00:00, 28.12 00:00) по Москве.
TODAY = date(2026, 10, 28)
WINDOW_START = datetime(2026, 10, 28, tzinfo=MSK)
WINDOW_END = datetime(2026, 12, 28, tzinfo=MSK)
# Заглушки KudaGo: «без начала» — 1-й год, «без конца» — 9999-й.
STARTLESS = -62135433000
ENDLESS = 253370754000


def msk(*args) -> datetime:
    return datetime(*args, tzinfo=MSK)


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


def api_date(start: datetime | int | None, end: datetime | int | None, **flags) -> dict:
    """Элемент dates[] ответа API с expand=dates; datetime → unix-время."""
    def unix(v):
        return ts(v) if isinstance(v, datetime) else v

    return {
        "start": unix(start),
        "end": unix(end),
        "is_continuous": False,
        "is_endless": False,
        "is_startless": False,
        "schedules": [],
        "use_place_schedule": False,
        **flags,
    }


def api_event(event_id: int = 1, **overrides) -> dict:
    """Событие из ответа /public-api/v1.4/events/ в формате документации KudaGo."""
    event = {
        "id": event_id,
        "title": "концерт группы «Пикник»",
        "slug": f"event-{event_id}",
        "site_url": f"https://kzn.kudago.com/event/event-{event_id}/",
        "dates": [api_date(msk(2026, 11, 20, 19), msk(2026, 11, 20, 21))],
        "place": {
            "id": 745,
            "title": "Татнефть Арена",
            "slug": "tatneft-arena",
            "address": "ул. Чистопольская, 42",
            "site_url": "https://kzn.kudago.com/place/tatneft-arena/",
            "coords": {"lat": 55.8205, "lon": 49.1268},
            "subway": "",
            "is_closed": False,
            "location": "kzn",
        },
        "categories": ["concert"],
        "tags": ["рок"],
        "age_restriction": "16+",
        "price": "от 2500 до 9000 рублей",
        "is_free": False,
        "favorites_count": 42,
        "description": "Легендарная рок-группа.",
        "body_text": "Полное описание концерта.",
    }
    event.update(overrides)
    return event



class FakeTime:
    """Фейковые часы: двигаются только на sleep, паузы копятся в списке."""

    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now
