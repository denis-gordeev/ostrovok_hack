from typing import ClassVar

import pytest
from kudago_helpers import api_event

from kudago_events import cli, load
from kudago_events.api import KudagoApiError

pytestmark = pytest.mark.db


class FakeKudagoApi:
    """Подменяет KudagoApi в load: отдаёт заданные события или ошибку."""

    items: ClassVar[list] = []

    def __init__(self, cfg):
        pass

    def events(self, since, until):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def fake_api(dsn, monkeypatch):
    monkeypatch.setenv("KUDAGO_DB_DSN", dsn)
    monkeypatch.setattr(load, "KudagoApi", FakeKudagoApi)
    return FakeKudagoApi


def test_load_command_reloads_even_fresh_data(fake_api, capsys):
    fake_api.items = [api_event(1)]
    assert cli.main(["load"]) == 0
    fake_api.items = [api_event(1), api_event(2)]
    assert cli.main(["load"]) == 0
    assert "загружено событий: 2" in capsys.readouterr().out


def test_load_command_reports_failure_with_exit_code_1(fake_api, capsys):
    fake_api.items = [KudagoApiError("403 Forbidden: нужен российский IP или HTTPS_PROXY.", status_code=403)]
    assert cli.main(["load"]) == 1
    assert "403 Forbidden" in capsys.readouterr().err


def test_report_shows_counts_and_last_loads(fake_api, capsys):
    fake_api.items = [api_event(1), api_event(2)]
    cli.main(["load"])
    capsys.readouterr()

    assert cli.main(["report"]) == 0

    out = capsys.readouterr().out
    assert "событий: 2" in out and "сеансов: 2" in out
    assert "ok" in out and "2026-" in out
