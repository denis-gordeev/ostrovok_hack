from datetime import date

from pydantic import BaseModel, Field


class HotelSearchIntent(BaseModel):
    """ЗАГЛУШКА: замените своей схемой интентов.

    Граф не зависит от конкретных полей: интент целиком (JSON) попадает в системный
    промпт агента, а агент сам заполняет аргументы tools. Поля ниже — пример.
    """

    checkin: date | None = Field(default=None, description="Дата заезда")
    checkout: date | None = Field(default=None, description="Дата выезда")
    adults: int = Field(default=2, ge=1, description="Количество взрослых")
    rooms: int = Field(default=1, ge=1, description="Количество номеров")
    children_ages: list[int] = Field(default_factory=list, description="Возрасты детей")
