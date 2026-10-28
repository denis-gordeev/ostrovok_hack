from hotel_agent.tools.events import events_in_kazan, kazan_event_details
from hotel_agent.tools.noise import noise_in_kazan
from hotel_agent.tools.ostrovok import search_hotels_in_tatarstan

# Tools агента: новые API подключаются сюда.
TOOLS = [search_hotels_in_tatarstan, noise_in_kazan, events_in_kazan, kazan_event_details]
