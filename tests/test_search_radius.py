import unittest
from unittest.mock import patch

from pydantic import ValidationError

from geo_api import FeatureRequest
from geo_tools import ox, search_osm, validate_point


class SearchRadiusTests(unittest.TestCase):
    def test_reported_request_reaches_osm(self):
        request = FeatureRequest(lat=55.7961, lon=49.1064, tags={"name": "Теплоконтроль"}, radius_m=15000, limit=10)
        with (
            patch(
                "geo_tools.ox.features_from_point", side_effect=ox._errors.InsufficientResponseError("No data")
            ) as fetch,
            patch("geo_tools.cached_result", side_effect=lambda kind, params, compute: compute()),
            patch("geo_tools.map_result", side_effect=lambda content, variants: (content, variants)),
        ):
            content, _ = search_osm.func(**request.model_dump())
        fetch.assert_called_once_with((55.7961, 49.1064), {"name": "Теплоконтроль"}, dist=15000)
        self.assertEqual(content["total"], 0)

    def test_tool_and_api_reject_outside_radius_limits(self):
        for radius in (0, -1):
            with self.subTest(radius=radius):
                with self.assertRaises(ValueError):
                    validate_point(55.7961, 49.1064, radius)
                with self.assertRaises(ValidationError):
                    FeatureRequest(lat=55.7961, lon=49.1064, tags={"name": "Теплоконтроль"}, radius_m=radius)

    def test_large_radius_is_capped_before_query_and_cache(self):
        request = dict(lat=55.7961, lon=49.1064, tags={"name": "Теплоконтроль"}, radius_m=50000, limit=10)
        self.assertEqual(FeatureRequest(**request).radius_m, 15000)
        with (
            patch(
                "geo_tools.ox.features_from_point", side_effect=ox._errors.InsufficientResponseError("No data")
            ) as fetch,
            patch("geo_tools.cached_result", side_effect=lambda kind, params, compute: compute()) as cache,
            patch("geo_tools.map_result", side_effect=lambda content, variants: (content, variants)),
        ):
            content, _ = search_osm.func(**request)
        fetch.assert_called_once_with((55.7961, 49.1064), request["tags"], dist=15000)
        self.assertEqual(cache.call_args.args[1][4], 15000)
        self.assertEqual(content["radius_m"], 15000)
