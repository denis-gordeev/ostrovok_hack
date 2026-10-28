from datetime import date, datetime
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def today_in_moscow() -> date:
    """«Сегодня» по времени отелей Татарстана: от него считаются допустимые даты заезда."""
    return datetime.now(MOSCOW_TZ).date()
