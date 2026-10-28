import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent import build_agent
from agent_limits import CallLimits


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        # Real providers return a new message ID on every call. The stock fake
        # reuses its response object when cycling and would replace chat history.
        result.generations[0].message = result.generations[0].message.model_copy(
            update={'id': str(uuid4())})
        return result


def calls(*names):
    return AIMessage(content='', tool_calls=[
        {'id': f'call-{index}', 'name': name, 'args': {}}
        for index, name in enumerate(names)])


class CallLimitTests(unittest.TestCase):
    def setUp(self):
        self.executed = []

        @tool
        def check_place() -> str:
            """Check a place in a local test."""
            self.executed.append('place')
            return 'Проверено'

        @tool
        def check_route() -> str:
            """Check a route in a local test."""
            self.executed.append('route')
            return 'Маршрут проверен'

        self.tools = [check_place, check_route]
        self.environment = patch.dict(os.environ, {
            'AGENT_MAX_MODEL_CALLS': '4', 'AGENT_MAX_TOOL_CALLS': '4',
            'AGENT_MAX_CALLS_PER_TOOL': '3'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def build(self, responses, checkpointer=None):
        with patch('agent.TOOLS', self.tools):
            return build_agent(model=ToolModel(responses=responses), checkpointer=checkpointer)

    def test_model_budget_stops_loop_and_preserves_completed_tools(self):
        with patch.dict(os.environ, {'AGENT_MAX_MODEL_CALLS': '2'}):
            graph = self.build([calls('check_place')])
        result = graph.invoke({'messages': [('user', 'Проверяй')]})
        self.assertEqual(result['call_usage']['model_calls'], 2)
        self.assertEqual(result['call_usage']['tool_calls'], 2)
        self.assertEqual(result['call_usage']['stop_reason'], 'model')
        self.assertEqual(self.executed, ['place', 'place'])
        self.assertIn('Достигнут лимит', result['messages'][-1].content)

    def test_parallel_batch_is_blocked_atomically_and_all_call_ids_closed(self):
        with patch.dict(os.environ, {'AGENT_MAX_TOOL_CALLS': '1'}):
            graph = self.build([calls('check_place', 'check_route')])
        result = graph.invoke({'messages': [('user', 'Проверь всё')]})
        self.assertEqual(self.executed, [])
        self.assertEqual(result['call_usage']['tool_calls'], 0)
        self.assertEqual(result['call_usage']['stop_reason'], 'tools')
        errors = [m for m in result['messages'] if isinstance(m, ToolMessage)]
        self.assertEqual({m.tool_call_id for m in errors}, {'call-0', 'call-1'})
        self.assertTrue(all(m.status == 'error' for m in errors))
        self.assertIsInstance(result['messages'][-1], AIMessage)

    def test_per_tool_limit_includes_duplicates_within_batch(self):
        with patch.dict(os.environ, {'AGENT_MAX_CALLS_PER_TOOL': '1'}):
            graph = self.build([calls('check_place', 'check_place')])
        result = graph.invoke({'messages': [('user', 'Проверь дважды')]})
        self.assertEqual(self.executed, [])
        self.assertEqual(result['call_usage']['stop_reason'], 'per_tool')

    def test_new_user_turn_resets_budget_and_same_turn_does_not(self):
        with patch.dict(os.environ, {'AGENT_MAX_MODEL_CALLS': '1'}):
            graph = self.build([calls('check_place')], InMemorySaver())
        config = {'configurable': {'thread_id': 'budget-test'}}
        first = graph.invoke({'messages': [HumanMessage(content='Проверь', id='turn-1')]}, config)
        repeated = graph.invoke({'messages': []}, config)
        self.assertEqual(repeated['call_usage'], first['call_usage'])
        self.assertEqual(self.executed, ['place'])
        next_turn = graph.invoke({'messages': [HumanMessage(content='Ещё раз', id='turn-2')]}, config)
        self.assertEqual(next_turn['call_usage']['turn_id'], 'turn-2')
        self.assertEqual(next_turn['call_usage']['model_calls'], 1)
        self.assertEqual(next_turn['call_usage']['tool_calls'], 1)
        self.assertEqual(self.executed, ['place', 'place'])

    def test_exact_budget_can_finish_normally_and_threads_are_independent(self):
        with patch.dict(os.environ, {'AGENT_MAX_MODEL_CALLS': '2', 'AGENT_MAX_TOOL_CALLS': '1'}):
            graph = self.build([calls('check_route'), AIMessage(content='Готово')], InMemorySaver())
        for thread_id in ('one', 'two'):
            result = graph.invoke({'messages': [('user', 'Проверь маршрут')]},
                                  {'configurable': {'thread_id': thread_id}})
            self.assertEqual(result['messages'][-1].content, 'Готово')
            self.assertIsNone(result['call_usage']['stop_reason'])
            self.assertEqual(result['call_usage']['by_tool'], {'check_route': 1})

    def test_invalid_limits_rejected(self):
        for value in ('0', '-1', 'bad'):
            with patch.dict(os.environ, {'AGENT_MAX_MODEL_CALLS': value}):
                with self.assertRaises(ValueError):
                    CallLimits.from_env()


if __name__ == '__main__':
    unittest.main()
