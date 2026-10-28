from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from hotel_agent.graph.context import AgentContext
from hotel_agent.graph.nodes import agent
from hotel_agent.graph.state import AgentState
from hotel_agent.tools import TOOLS


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """START → agent ⇄ tools; agent без вызова tools → END."""
    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node(agent)
    builder.add_node("tools", ToolNode(TOOLS))

    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)
