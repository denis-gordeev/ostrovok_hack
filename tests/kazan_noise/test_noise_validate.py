"""Тесты проверки модели: регэкспы жалоб, Спирмен, отели, санити-проверки."""

from types import SimpleNamespace

import numpy as np
import psycopg
import pytest
from noise_helpers import local_scene
from shapely.geometry import Point, box

from kazan_noise import cli
from kazan_noise.cli import main
from kazan_noise.service import ModeScore, NoiseReport, OutsideKazanError
from kazan_noise.validate import (
    HotelRow,
    _forest_points,
    is_noise_complaint,
    load_hotels,
    sanity_checks,
    spearman,
    validate_hotels,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Шумно, окна на дорогу", True),
        ("Слышно трамвай с утра", True),
        ("Музыка из бара до 3 ночи", True),
        ("шумно, бар внизу", True),
        ("громко гудят машины под окном", True),
        ("По ночам грохот поездов", True),
        ("Окна во двор, но всё равно шумно", True),
        ("кофемашина не работала", False),
        ("стиральная машина в номере сломана", False),
        ("некуда поставить машину", False),
        ("Бар закрывается рано", False),
        ("Клубничное варенье на завтрак", False),
        ("Тонкие стены, слышно соседей", False),
        ("Музыка в номере не работала", False),
        ("Шумно от соседей сверху и трамваев", False),
        ("Дорого и шумно", False),
        ("Удобно для прогулок по улицам", False),
        ("барьер у дороги", False),
        ("бархатные шторы", False),
        ("Всё отлично", False),
        (None, False),
        ("", False),
    ],
)
def test_is_noise_complaint(text, expected):
    assert is_noise_complaint(text) is expected


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.8660, abs=1e-4)


class FakeScores:
    """Сервис-заглушка: фиксированные баллы по координатам, прочие точки вне Казани."""

    def __init__(self, scores):
        self.scores = scores

    def noise_at(self, lat, lon):
        if (lat, lon) not in self.scores:
            raise OutsideKazanError("вне")
        mode = ModeScore(score=self.scores[(lat, lon)], label="x")
        return NoiseReport(
            lat=lat,
            lon=lon,
            inside_building=False,
            day=mode,
            night=mode,
            quiet_side=None,
            main_sources=[],
            model_level_day=0.0,
            model_level_night=0.0,
            osm_timestamp="t",
        )


def test_validate_hotels_skips_points_outside_kazan():
    rows = [
        HotelRow("a", "A", 55.79, 49.11, 20, 8),
        HotelRow("b", "B", 55.80, 49.12, 30, 1),
        HotelRow("c", "C", 55.81, 49.13, 15, 3),
        HotelRow("x", "X", 55.0, 37.6, 40, 0),
    ]
    report = validate_hotels(FakeScores({(55.79, 49.11): 90, (55.80, 49.12): 10, (55.81, 49.13): 50}), rows)
    assert report.n == 3 and report.rho_night == pytest.approx(1.0)
    assert len(report.worst) == 3


def test_sanity_checks_return_three_named_checks(service, service_grid):
    checks = sanity_checks(service, service_grid)
    assert [c.name for c in checks] == ["у магистрали", "двор", "лес"]
    assert checks[0].n >= 5 and checks[0].ok is not None


def test_validate_command_exit_code(monkeypatch, capsys):
    """Без снимка команда падает с кодом 1 и сообщением в stderr."""
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", "/nonexistent-kazan-noise")
    with pytest.raises(SystemExit) as exc:
        main(["validate"])
    assert exc.value.code == 1
    assert capsys.readouterr().err


class FakeConn:
    def execute(self, sql, params):
        assert params == (10,)
        return self

    def fetchall(self):
        return [
            {
                "hotel_id": "1",
                "name": "A",
                "lat": 55.79,
                "lon": 49.11,
                "minuses": ["Шумно, окна на дорогу", None, "Всё отлично", "Тонкие стены, трамвай"],
            },
        ]


def test_load_hotels_counts_complaints():
    (row,) = load_hotels(FakeConn())
    assert (row.reviews, row.complaints) == (4, 1)


def test_validate_command_survives_missing_schema(monkeypatch, capsys, service, service_grid, service_dir):
    """Нет схемы ostrovok: сообщение «недоступны», код выхода зависит только от санити."""

    def boom(dsn):
        raise psycopg.errors.UndefinedTable("нет таблицы")

    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(service_dir))
    monkeypatch.setattr(cli, "connect", boom)
    try:
        main(["validate"])
    except SystemExit as exc:
        assert exc.code == 1  # провал санити на синтетической сцене допустим
    assert "Отзывы островка недоступны" in capsys.readouterr().out


def test_forest_point_is_300_m_inside():
    # Большой лес с длинным узким выступом: representative_point() самого полигона попадает в выступ.
    forest = box(0, 2000, 2000, 4000).union(box(0, -2000, 100, 2000))
    service = SimpleNamespace(model=SimpleNamespace(features=local_scene(green=[(forest, 1.0)])))
    ((x, y),) = _forest_points(service, np.random.default_rng(0), sample=50)
    assert forest.exterior.distance(Point(x, y)) >= 300
