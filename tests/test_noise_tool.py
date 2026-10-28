from hotel_agent.cli import _load_noise
from hotel_agent.tools.noise import point_for_llm
from kazan_noise.service import ModeScore, NoiseReport, NoiseService, QuietSide, SourceInfo

ROAD = SourceInfo(
    kind="road", subtype="primary", name="улица Баумана", distance_m=35, share_day=0.7, share_night=0.5, shielded=False
)
REPORT = NoiseReport(
    lat=55.7963,
    lon=49.1088,
    inside_building=False,
    day=ModeScore(score=81, label="очень шумно"),
    night=ModeScore(score=64, label="шумно"),
    quiet_side=None,
    main_sources=[ROAD],
    model_level_day=63.2,
    model_level_night=55.1,
    osm_timestamp="2026-10-26T18:41:36Z",
)


def test_point_for_llm_keeps_coordinates_and_hides_model_levels():
    out = point_for_llm(REPORT)
    assert (out["lat"], out["lon"]) == (55.7963, 49.1088)
    assert out["day"] == {"score": 81, "label": "очень шумно"} and "quiet_side" not in out
    assert not any(k.startswith("model_level") or k == "osm_timestamp" for k in out)
    assert out["main_sources"][0]["name"] == "улица Баумана"


def test_point_for_llm_includes_quiet_side_and_skips_unnamed_source_name():
    quiet = QuietSide(day=ModeScore(score=30, label="тихо"), night=ModeScore(score=20, label="очень тихо"))
    unnamed = ROAD.model_copy(update={"name": None})
    report = REPORT.model_copy(update={"inside_building": True, "quiet_side": quiet, "main_sources": [unnamed]})
    out = point_for_llm(report)
    assert out["inside_building"] is True
    assert out["quiet_side"]["night"] == {"score": 20, "label": "очень тихо"}
    assert "name" not in out["main_sources"][0]


def test_load_noise_with_corrupt_snapshot_disables_noise(tmp_path, monkeypatch, capsys):
    (tmp_path / "features.npz").write_bytes(b"PK\x03\x04 truncated")
    monkeypatch.setenv("KAZAN_NOISE_DATA_DIR", str(tmp_path))
    assert _load_noise() is None
    assert "Оценка шума отключена" in capsys.readouterr().out


def test_load_noise_survives_unexpected_error(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("неожиданно")

    monkeypatch.setattr(NoiseService, "load", boom)
    assert _load_noise() is None
    out = capsys.readouterr().out
    assert "Оценка шума отключена" in out and "неожиданно" in out
