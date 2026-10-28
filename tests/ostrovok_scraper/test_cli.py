from contextlib import contextmanager
from datetime import date

from ostrovok_scraper import cli
from ostrovok_scraper.cli import main
from ostrovok_scraper.http import BlockedError
from ostrovok_scraper.normalize import NormalizeReport
from ostrovok_scraper.steps import StepReport

STEPS = ["serp", "serp_guests", "hotels", "child_age", "rates", "reviews"]


def install_fakes(monkeypatch, fail_on=None, error=None) -> list:
    """Подменяет ресурсы, шаги и normalize; пишет вызовы (имя, kwargs)."""
    calls: list = []

    @contextmanager
    def fake_open(cfg):
        yield None, None, None

    def make(name):
        def fake(api, store, cfg, **kwargs):
            calls.append((name, kwargs))
            if name == fail_on:
                raise error
            return StepReport(name)

        return fake

    monkeypatch.setattr(cli, "open_resources", fake_open)
    for name in STEPS:
        monkeypatch.setattr(cli, f"run_{name}", make(name))

    def fake_normalize(conn, cfg):
        calls.append(("normalize", {}))
        return NormalizeReport({}, {}, {}, {})

    monkeypatch.setattr(cli, "normalize", fake_normalize)
    return calls


def test_all_runs_steps_in_order_with_flags(monkeypatch):
    calls = install_fakes(monkeypatch)
    assert main(["all", "--limit-hotels", "5", "--nights", "2", "--today", "2026-10-27"]) == 0
    assert [c[0] for c in calls] == [*STEPS, "normalize"]
    assert calls[0][1]["today"] == date(2026, 10, 27) and calls[0][1]["nights"] == 2
    assert calls[2][1]["limit_hotels"] == 5


def test_blocked_exits_with_code_2_and_message(monkeypatch, capsys):
    install_fakes(monkeypatch, fail_on="hotels", error=BlockedError("403 x5"))
    assert main(["all"]) == 2
    assert "Похоже на блокировку" in capsys.readouterr().err


def test_single_step_with_retry_failed(monkeypatch):
    calls = install_fakes(monkeypatch)
    assert main(["rates", "--retry-failed"]) == 0
    assert calls == [("rates", {"limit_hotels": None, "retry_failed": True})]


def test_step_report_line_printed(monkeypatch, capsys):
    install_fakes(monkeypatch)
    assert main(["serp-guests"]) == 0
    assert "serp_guests: сделано 0, пропущено 0, ошибок 0" in capsys.readouterr().out


def test_db_init_and_report_with_db(db_conn, monkeypatch, capsys):
    @contextmanager
    def fake_open(cfg):
        yield None, None, db_conn

    monkeypatch.setattr(cli, "open_resources", fake_open)
    assert main(["db-init"]) == 0
    assert "001_raw.sql" in capsys.readouterr().out
    db_conn.execute("INSERT INTO raw.responses (step, key, request, status_code) VALUES ('hotels', 'k', '{}', 200)")
    db_conn.commit()
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "hotels: всего 1, с ошибкой 0, неполных 0" in out
    assert "ostrovok.hotels: 0" in out
