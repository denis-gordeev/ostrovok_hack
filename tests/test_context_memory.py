"""Verified facts survive history compaction without calling an external LLM."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from map_tools import GeoState, collect_geo_results, collection, feature, map_result, read_geo_result, update_map


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"CACHE_DIR": self.directory.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.values = {
            "retrieved_at": "2026-10-01T12:34:56Z",
            "snapshot_id": "test-v1",
            "source": "OSM",
            "cache_hit": True,
        }
        self.geometry = collection(
            [
                feature(
                    {"type": "Point", "coordinates": [49.123456789, 55.987654321]},
                    name="Проверенный объект",
                    url="https://www.openstreetmap.org/node/1",
                )
            ]
        )
        self.content, self.artifact = map_result(self.values, {"default": self.geometry})

    def compacted_state(self):
        old = {
            "messages": [ToolMessage(content=json.dumps(self.content), artifact=self.artifact, tool_call_id="search")]
        }
        remembered = collect_geo_results(old)
        # Simulate the old tool messages being entirely unavailable to the model.
        return {**remembered, "messages": [HumanMessage(content="Покажи снова")]}

    def test_exact_values_and_sources_survive_without_original_messages(self):
        state = self.compacted_state()
        result = read_geo_result.func(self.content["data_ref"], state)
        self.assertEqual(result["locations"][0]["location"]["lat"], 55.987654321)
        self.assertEqual(result["locations"][0]["location"]["lon"], 49.123456789)
        self.assertEqual(result["result"]["retrieved_at"], self.values["retrieved_at"])
        path = Path(self.directory.name) / "verified-results" / f"{self.content['data_ref']}.json"
        self.assertEqual(json.loads(path.read_text()), self.artifact)
        self.assertEqual(collect_geo_results({**state, "messages": []}), {"geo_results": {}})
        with self.assertRaisesRegex(ValueError, "Unknown data_ref"):
            read_geo_result.func(self.content["data_ref"], {"messages": []})

    def test_map_can_reuse_result_after_compaction(self):
        state = self.compacted_state()
        call = {
            "id": "map",
            "name": "update_map",
            "type": "tool_call",
            "args": {"operation": "upsert", "layer_id": "places", "data_ref": self.content["data_ref"]},
        }
        state["messages"].append(AIMessage(content="", tool_calls=[call]))
        builder = StateGraph(GeoState)
        builder.add_node("tools", ToolNode([update_map], handle_tool_errors=False))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        result = builder.compile().invoke(state)
        self.assertEqual(result["map_layers"]["places"]["data"], self.geometry)

    def test_identical_geometry_with_new_provenance_gets_new_reference(self):
        newer = {**self.values, "retrieved_at": "2026-10-28T12:34:56Z"}
        content, _ = map_result(newer, {"default": self.geometry})
        self.assertNotEqual(content["data_ref"], self.content["data_ref"])
        self.assertEqual(self.artifact["result"]["retrieved_at"], self.values["retrieved_at"])

    @unittest.skipUnless(importlib.util.find_spec("langmem"), "langmem is not installed")
    def test_agent_cycle_summarizes_and_uses_verified_result(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
        from langgraph.checkpoint.memory import InMemorySaver
        from agent import build_agent

        class ToolModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        def call(name, args, call_id):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])

        ref = self.content["data_ref"]
        model = ToolModel(
            responses=[
                call("read_geo_result", {"data_ref": ref}, "read"),
                call("update_map", {"operation": "upsert", "layer_id": "places", "data_ref": ref}, "map"),
                AIMessage(content="Объект показан на карте"),
            ]
        )
        state = self.compacted_state()
        state["messages"] = [HumanMessage(content="Старое сообщение " * 60, id=f"old-{i}") for i in range(5)] + [
            HumanMessage(content="Покажи снова", id="latest")
        ]
        with patch.dict(os.environ, {"CONTEXT_MAX_TOKENS": "1500", "CONTEXT_SUMMARY_TOKENS": "100"}):
            graph = build_agent(
                model=model,
                summary_model=FakeListChatModel(responses=["Пользователь просит снова показать выбранный объект."]),
                checkpointer=InMemorySaver(),
            )
            result = graph.invoke(state, {"configurable": {"thread_id": "test"}, "recursion_limit": 30})
        self.assertIn("running_summary", result["context"])
        self.assertEqual(result["messages"][-1].content, "Объект показан на карте")
        self.assertEqual(result["map_layers"]["places"]["data"], self.geometry)
        self.assertEqual(result["geo_results"][ref]["result"]["retrieved_at"], self.values["retrieved_at"])

    @unittest.skipUnless(importlib.util.find_spec("langmem"), "langmem is not installed")
    def test_real_summarization_node_preserves_tool_pairs_and_registry(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langmem.short_term import SummarizationNode

        state = self.compacted_state()
        state["messages"] = [
            HumanMessage(content="Старый запрос " * 100, id="human-1"),
            AIMessage(content="", id="ai-1", tool_calls=[{"id": "search", "name": "search_osm", "args": {}}]),
            ToolMessage(content=json.dumps(self.content), artifact=self.artifact, id="tool-1", tool_call_id="search"),
            HumanMessage(content="Покажи снова", id="human-2"),
        ]
        node = SummarizationNode(
            model=FakeListChatModel(responses=["Сводка запроса"]),
            max_tokens=200,
            max_tokens_before_summary=200,
            max_summary_tokens=50,
        )
        update = node.invoke(state)
        self.assertIn("running_summary", update["context"])
        self.assertIn("geo_results", state)
        pending = set()
        for message in update["summarized_messages"]:
            if isinstance(message, AIMessage):
                pending.update(call["id"] for call in message.tool_calls)
            if isinstance(message, ToolMessage):
                self.assertIn(message.tool_call_id, pending)
                pending.remove(message.tool_call_id)
        self.assertFalse(pending)


if __name__ == "__main__":
    unittest.main()
