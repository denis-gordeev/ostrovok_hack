import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_transit import fixture

from geo_api import app
from geo_tools import walking_route
from scripts.bustime_to_gtfs import convert
from scripts.regional_schedules import merge, parse_innopolis


class RegionalTests(unittest.TestCase):
    def test_regional_endpoints_pass_validation_without_expensive_long_walk(self):
        client = TestClient(app)
        with patch("geo_api.street_route", return_value={"status": "ok"}) as route:
            response = client.post(
                "/drive/route",
                json={"start": {"lat": 55.7961, "lon": 49.1064}, "end": {"lat": 55.8477605, "lon": 48.5097031}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(route.call_args.args[1], "drive")
        with patch("geo_tools.requests.post") as network:
            with self.assertRaisesRegex(ValueError, "10 km"):
                walking_route.invoke(
                    {"start_lat": 55.7961, "start_lon": 49.1064, "end_lat": 55.8477605, "end_lon": 48.5097031}
                )
        network.assert_not_called()

    def test_railway_type_is_exported_and_departures_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            data = fixture()
            data["bus"]["1"][-1] = 6
            source = p / "source.json"
            source.write_text(json.dumps(data))
            convert(source, p / "base.zip", date(2026, 10, 26))
            with zipfile.ZipFile(p / "base.zip") as z:
                routes = list(csv.DictReader(io.StringIO(z.read("routes.txt").decode())))
                self.assertEqual(routes[0]["route_type"], "2")
                self.assertIn("25:10:00", z.read("stop_times.txt").decode())

    def test_published_html_parser_handles_midnight_and_refuses_changed_layout(self):
        html = """<div class="schedule_item"><div class="schedule_item__autobus"><span>108</span></div>
        <div class="schedule_item__station"><div class="schedule_item__time">23:30</div><span>Казань, A</span></div>
        <div class="schedule_item__station"><div class="schedule_item__time">00:30</div><span>B</span></div></div>"""
        record = parse_innopolis(html, "1111100", "https://example.org")[0]
        self.assertEqual(record["from"], "A")
        self.assertEqual(record["arrival"], "24:30:00")
        self.assertFalse(record["arrival_estimated"])
        with self.assertRaises(ValueError):
            parse_innopolis("<html>no timetable</html>", "1111100", "https://example.org")

    def test_merge_requires_verified_coordinates_and_keeps_published_endpoint_times(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            source = p / "source.json"
            source.write_text(json.dumps(fixture()))
            convert(source, p / "base.zip", date(2026, 10, 26))
            supplement = {
                "records": [
                    {
                        "route_id": "official-inno-108",
                        "ref": "108",
                        "route_type": 3,
                        "from": "A",
                        "to": "B",
                        "mask": "1111100",
                        "departure": "09:30:00",
                        "arrival": "10:30:00",
                        "arrival_estimated": False,
                        "source_url": "https://example.org",
                    }
                ],
                "sources": [],
            }
            locations = {"A": {"id": "oa", "lat": 55.79, "lon": 49.1}, "B": {"id": "ob", "lat": 55.79, "lon": 49.11}}
            shapes = {"A|B": {"coordinates": [[49.1, 55.79], [49.11, 55.79]], "length_m": 1000}}
            with self.assertRaisesRegex(ValueError, "coordinates"):
                merge(p / "base.zip", p / "out.zip", supplement, {}, shapes)
            report = merge(p / "base.zip", p / "out.zip", supplement, locations, shapes)
            self.assertEqual(report["route_sources"]["official-inno-108"], "https://example.org")
            with zipfile.ZipFile(p / "out.zip") as z:
                times = list(csv.DictReader(io.StringIO(z.read("stop_times.txt").decode())))
                official = [t for t in times if t["trip_id"].startswith("official-")]
                self.assertEqual([t["arrival_time"] for t in official], ["09:30:00", "10:30:00"])
                self.assertTrue(all(t["timepoint"] == "1" for t in official))


if __name__ == "__main__":
    unittest.main()
