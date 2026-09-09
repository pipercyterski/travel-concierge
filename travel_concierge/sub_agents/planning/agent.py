# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Planning agent. A pre-booking agent covering the planning part of the trip."""

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool
from google.genai.types import GenerateContentConfig

from travel_concierge import MODEL
from travel_concierge.shared_libraries import types
from travel_concierge.sub_agents.planning import prompt
from travel_concierge.tools.memory import memorize
from travel_concierge.world import (
    get_seat_availability,
    search_flights,
    search_hotels,
    track_agent,
)

itinerary_agent = Agent(
    model=MODEL,
    name="itinerary_agent",
    description="Create and persist a structured JSON representation of the itinerary",
    instruction=prompt.ITINERARY_AGENT_INSTR,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_schema=types.Itinerary,
    output_key="itinerary",
    generate_content_config=types.json_response_config,
)


planning_agent = Agent(
    model=MODEL,
    description="""Helps users with travel planning, complete a full itinerary for their vacation, finding best deals for flights and hotels.""",
    name="planning_agent",
    instruction=prompt.PLANNING_AGENT_INSTR,
    tools=[
        search_flights,
        get_seat_availability,
        search_hotels,
        AgentTool(agent=itinerary_agent),
        memorize,
    ],
    # temperature only: Anthropic rejects temperature+top_p set together.
    generate_content_config=GenerateContentConfig(temperature=0.1),
    before_agent_callback=track_agent,
)
