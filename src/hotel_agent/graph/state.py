from langgraph.graph import MessagesState

from hotel_agent.apis.ostrovok.models import HotelCandidate


class AgentState(MessagesState):
    # Кандидаты последнего подбора по Татарстану (пишет tool search_hotels_in_tatarstan).
    hotel_candidates: list[HotelCandidate]
