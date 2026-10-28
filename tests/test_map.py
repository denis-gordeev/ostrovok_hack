import json
import unittest

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.graph import START, END, StateGraph

from map_tools import GeoState, collection, decode_polyline, feature, map_result, merge_layers, update_map


class MapTests(unittest.TestCase):
    def setUp(self):
        self.geometry = collection([feature({"type": "Point", "coordinates": [49.1, 55.8]}, name="Парк")])
        self.content, artifact = map_result({"source": "OSM"}, {"default": self.geometry})
        self.state = {
            "messages": [ToolMessage(content=json.dumps(self.content), artifact=artifact, tool_call_id="search")],
            "map_layers": {},
        }

    def invoke(self, operation, **kwargs):
        call = {
            "id": "map-call",
            "name": "update_map",
            "args": {"operation": operation, "layer_id": "parks", **kwargs},
            "type": "tool_call",
        }
        state = {**self.state, "messages": [*self.state["messages"], AIMessage(content="", tool_calls=[call])]}
        builder = StateGraph(GeoState)
        builder.add_node("tools", ToolNode([update_map], handle_tool_errors=False))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        return next(builder.compile().stream(state, stream_mode="updates"))["tools"]

    def test_tool_artifact_keeps_geometry_out_of_model_content(self):
        self.assertNotIn("coordinates", json.dumps(self.content))
        self.assertEqual(
            self.content["data_ref"], map_result({"source": "OSM"}, {"default": self.geometry})[0]["data_ref"]
        )
        schema = update_map.tool_call_schema.model_json_schema()["properties"]
        self.assertNotIn("state", schema)
        self.assertNotIn("tool_call_id", schema)
        result = self.invoke("upsert", title="Парки", data_ref=self.content["data_ref"], kind="points")
        self.assertEqual(result["map_layers"]["parks"]["data"], self.geometry)
        self.assertEqual(result["map_camera"]["layer_id"], "parks")
        self.assertEqual(result["messages"][0].tool_call_id, "map-call")

    def test_unknown_ref_rejected_and_layer_operations_preserve_others(self):
        with self.assertRaisesRegex(ValueError, "data_ref"):
            self.invoke("upsert", data_ref="invented")
        update = self.invoke("upsert", data_ref=self.content["data_ref"])
        self.state["map_layers"] = merge_layers({"other": {"id": "other"}}, update["map_layers"])
        hidden = self.invoke("hide")
        self.assertFalse(hidden["map_layers"]["parks"]["visible"])
        self.state["map_layers"] = merge_layers(self.state["map_layers"], hidden["map_layers"])
        self.assertIn("other", self.state["map_layers"])
        removed = self.invoke("remove")
        self.assertEqual(list(merge_layers(self.state["map_layers"], removed["map_layers"])), ["other"])
        cleared = self.invoke("clear")
        self.assertFalse(merge_layers(self.state["map_layers"], cleared["map_layers"]))

    def test_cones_are_explicitly_illustrative_and_sizes_validated(self):
        update = self.invoke("upsert", data_ref=self.content["data_ref"], kind="cones", height_m=75, radius_m=200)
        self.assertTrue(update["map_layers"]["parks"]["illustrative"])
        self.assertIn("not terrain visibility", update["messages"][0].content)
        with self.assertRaises(ToolInvocationError):
            self.invoke("upsert", data_ref=self.content["data_ref"], kind="cones", height_m=-1)

    def test_labels_toggle_preserves_verified_layer_and_does_not_move_camera(self):
        update = self.invoke("upsert", data_ref=self.content["data_ref"], feature_indices=[0])
        original = update["map_layers"]["parks"]
        self.assertTrue(original["show_labels"])
        self.state["map_layers"] = {"parks": {**original, "visible": False}}
        hidden = self.invoke("set_labels", show_labels=False)
        self.assertEqual(hidden["map_layers"]["parks"], {**original, "visible": False, "show_labels": False})
        self.assertNotIn("map_camera", hidden)
        self.assertFalse(json.loads(hidden["messages"][0].content)["show_labels"])
        self.state["map_layers"] = hidden["map_layers"]
        shown = self.invoke("set_labels", show_labels=True)
        self.assertTrue(shown["map_layers"]["parks"]["show_labels"])
        disabled = self.invoke("upsert", data_ref=self.content["data_ref"], show_labels=False)
        self.assertFalse(disabled["map_layers"]["parks"]["show_labels"])

    def test_route_polyline_coordinate_order(self):
        self.assertEqual(
            decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]
        )
        with self.assertRaises(ValueError):
            decode_polyline("_p~iF~")

    def test_map_subset_is_not_silently_expanded(self):
        content, artifact = map_result(
            {},
            {
                "default": collection(
                    [feature({"type": "Point", "coordinates": [49.1 + i / 100, 55.8]}, name=str(i)) for i in range(5)]
                )
            },
        )
        self.state["messages"] = [ToolMessage(content=json.dumps(content), artifact=artifact, tool_call_id="search")]
        update = self.invoke("upsert", data_ref=content["data_ref"], feature_indices=[0, 2, 4])
        self.assertEqual(len(update["map_layers"]["parks"]["data"]["features"]), 3)
        self.assertEqual(json.loads(update["messages"][0].content)["feature_names"], ["0", "2", "4"])
        with self.assertRaises(ValueError):
            self.invoke("upsert", data_ref=content["data_ref"], feature_indices=[7])


if __name__ == "__main__":
    unittest.main()
