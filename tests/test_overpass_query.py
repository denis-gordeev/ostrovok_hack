import unittest
from unittest.mock import Mock, patch

from geo_tools import overpass_query


class OverpassQueryTests(unittest.TestCase):
    def test_named_bbox_rejected_before_http_request(self):
        query = (
            '[out:json];nwr["name"~"Теплоконтроль",i](south=55.30,west=48.20,north=56.35,east=49.85);out center tags;'
        )
        with patch("geo_tools.geo_post") as post:
            with self.assertRaisesRegex(ValueError, "positional numbers"):
                overpass_query.invoke({"query": query})
            post.assert_not_called()

    def test_positional_bbox_is_sent_unchanged_and_empty_result_is_valid(self):
        query = '[out:json][timeout:60];nwr["name"~"Теплоконтроль",i](55.30,48.20,56.35,49.85);out center tags;'
        response = Mock()
        response.json.return_value = {"elements": []}
        with (
            patch("geo_tools.geo_post", return_value=response) as post,
            patch("geo_tools.cached_result", side_effect=lambda kind, params, compute: compute()),
            patch("geo_tools.map_result", side_effect=lambda content, variants: (content, variants)),
        ):
            content, _ = overpass_query.func(query=query)
        self.assertEqual(post.call_args.kwargs["data"]["data"], query)
        self.assertEqual(content["total"], 0)


if __name__ == "__main__":
    unittest.main()
