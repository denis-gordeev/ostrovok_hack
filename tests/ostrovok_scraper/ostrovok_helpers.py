import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Читает JSON-фикстуру; каждый вызов возвращает свежую копию."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
