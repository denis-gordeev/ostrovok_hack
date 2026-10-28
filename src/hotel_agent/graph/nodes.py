from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from hotel_agent.graph.context import AgentContext
from hotel_agent.graph.prompt import AGENT_SYSTEM_PROMPT
from hotel_agent.graph.state import AgentState
from hotel_agent.tools import TOOLS


async def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    ctx = runtime.context
    # Плейсхолдер промпта: {current_date} — сегодняшняя дата (Москва).
    system_prompt = AGENT_SYSTEM_PROMPT.replace("{current_date}", ctx.today().isoformat())
    response = await ctx.llm.bind_tools(TOOLS).ainvoke([SystemMessage(system_prompt), *state["messages"]])
    return {"messages": [response]}
