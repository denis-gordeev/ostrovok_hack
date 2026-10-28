import json
import unittest
from unittest.mock import Mock, patch

import networkx as nx
from fastapi.testclient import TestClient
from shapely.geometry import LineString

from geo_api import app, route_result
from geo_tools import driving_route


def graph():
    g = nx.MultiDiGraph()
    for node, point in {1: (49.1, 55.8), 2: (49.11, 55.8), 3: (49.105, 55.805)}.items():
        g.add_node(node, x=point[0], y=point[1])
    g.add_edge(1, 2, length=1000, travel_time=300)
    g.add_edge(1, 3, length=800, travel_time=30)
    g.add_edge(3, 2, length=500, travel_time=40)
    g.add_edge(
        3, 2, length=600, travel_time=10, geometry=LineString([(49.11, 55.8), (49.109, 55.803), (49.105, 55.805)])
    )
    return g


class DrivingTests(unittest.TestCase):
    def test_fastest_path_and_parallel_edge_match_reported_time_and_geometry(self):
        result = route_result(graph(), 1, 2, "drive", 12, 20)
        self.assertEqual(result["length_m"], 1400)
        self.assertEqual(result["estimated_minutes"], 0.7)
        self.assertEqual(
            result["geometry"]["coordinates"], [(49.1, 55.8), (49.105, 55.805), (49.109, 55.803), (49.11, 55.8)]
        )
        self.assertFalse(result["traffic_included"])
        self.assertFalse(result["turn_restrictions_included"])
        self.assertEqual(result["mode"], "CAR")
        walking = route_result(graph(), 1, 2, "walk", 12, 20)
        self.assertEqual(walking["length_m"], 1000)
        self.assertEqual(walking["estimated_minutes"], 13.3)

    def test_one_way_graph_is_not_made_bidirectional(self):
        with self.assertRaises(nx.NetworkXNoPath):
            route_result(graph(), 2, 1, "drive", 0, 0)

    def test_drive_endpoint_rejects_out_of_bounds_and_excessive_requests(self):
        client = TestClient(app)
        self.assertEqual(
            client.post(
                "/drive/route", json={"start": {"lat": 0, "lon": 0}, "end": {"lat": 55.8, "lon": 49.1}}
            ).status_code,
            422,
        )
        response = client.post(
            "/drive/route", json={"start": {"lat": 55.3, "lon": 48.2}, "end": {"lat": 56.35, "lon": 49.85}}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("150 km", response.json()["detail"])

    def test_car_geometry_is_an_artifact_for_the_map_not_llm_text(self):
        result = route_result(graph(), 1, 2, "drive", 0, 0)
        response = Mock(status_code=200, from_cache=False)
        response.json.return_value = result
        with patch("geo_tools.requests.post", return_value=response) as post:
            message = driving_route.invoke(
                {
                    "type": "tool_call",
                    "id": "car-test",
                    "name": "driving_route",
                    "args": {"start_lat": 55.8, "start_lon": 49.1, "end_lat": 55.8, "end_lon": 49.11},
                }
            )
        self.assertTrue(post.call_args.args[0].endswith("/drive/route"))
        self.assertNotIn("geometry", json.loads(message.content))
        features = message.artifact["collections"]["default"]["features"]
        self.assertEqual(features[0]["properties"]["mode"], "CAR")
        self.assertEqual(features[0]["geometry"]["type"], "LineString")


if __name__ == "__main__":
    unittest.main()
