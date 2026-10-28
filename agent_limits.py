"""Logical call budgets per user turn; network retries share one reservation."""

import os
from collections import Counter
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@dataclass(frozen=True)
class CallLimits:
    model: int
    tools: int
    per_tool: int

    @classmethod
    def from_env(cls):
        limits = cls(int(os.getenv('AGENT_MAX_MODEL_CALLS', '8')),
                     int(os.getenv('AGENT_MAX_TOOL_CALLS', '16')),
                     int(os.getenv('AGENT_MAX_CALLS_PER_TOOL', '6')))
        if min(limits.model, limits.tools, limits.per_tool) < 1:
            raise ValueError('Agent call limits must be positive integers')
        return limits


def start_turn(state):
    latest = next((m for m in reversed(state['messages']) if isinstance(m, HumanMessage)), None)
    turn_id = latest.id if latest else None
    previous = state.get('call_usage', {})
    if previous and previous.get('turn_id') == turn_id:
        return {}  # Restarting the same turn must not grant a fresh budget.
    return {'call_usage': {'turn_id': turn_id, 'model_calls': 0, 'tool_calls': 0,
                           'by_tool': {}, 'stop_reason': None}}


def reserve_model(state, limits):
    usage = dict(state['call_usage'])
    if usage['model_calls'] >= limits.model:
        usage['stop_reason'] = 'model'
    else:
        usage['model_calls'] += 1
    return {'call_usage': usage}


def reserve_tools(state, limits):
    usage = dict(state['call_usage'])
    calls = state['messages'][-1].tool_calls
    batch = Counter(call['name'] for call in calls)
    if usage['tool_calls'] + len(calls) > limits.tools:
        usage['stop_reason'] = 'tools'
    elif any(usage['by_tool'].get(name, 0) + count > limits.per_tool
             for name, count in batch.items()):
        usage['stop_reason'] = 'per_tool'
    else:
        usage['tool_calls'] += len(calls)
        usage['by_tool'] = dict(Counter(usage['by_tool']) + batch)
    # Reserve the entire parallel batch before starting any tool. No shared
    # mutable counters in worker threads, and no partially executed batch.
    return {'call_usage': usage}


def limit_result(state):
    reason = state['call_usage']['stop_reason']
    label = {'model': 'шагов обработки', 'tools': 'проверок и действий',
             'per_tool': 'однотипных проверок'}[reason]
    pending = getattr(state['messages'][-1], 'tool_calls', [])
    messages = [ToolMessage(content='Вызов не выполнен: достигнут лимит текущего запроса.',
                            tool_call_id=call['id'], name=call['name'], status='error')
                for call in pending]
    messages.append(AIMessage(content=(
        f'Достигнут лимит {label} для одного запроса. Выполнение остановлено; '
        'задача может быть выполнена не полностью. Уже полученные результаты '
        'и состояние карты сохранены. Уточни запрос или разбей его на несколько шагов.')))
    return {'messages': messages}
