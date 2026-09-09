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

"""Booking agent: reserves itinerary items and takes payment against the booking backend."""

from google.adk.agents import Agent
from google.genai.types import GenerateContentConfig

from travel_concierge import MODEL
from travel_concierge.sub_agents.booking import prompt
from travel_concierge.world import (
    book_activity,
    book_flight,
    book_hotel,
    get_reservations,
    process_payment,
    track_agent,
    trip_expense_report,
)

booking_agent = Agent(
    model=MODEL,
    name="booking_agent",
    description="Given an itinerary, complete the bookings of items by creating reservations and processing payment.",
    instruction=prompt.BOOKING_AGENT_INSTR,
    tools=[
        book_flight,
        book_hotel,
        book_activity,
        process_payment,
        get_reservations,
        trip_expense_report,
    ],
    # temperature only: Anthropic rejects temperature+top_p set together, and
    # the model here is a harness knob that can point at either provider.
    generate_content_config=GenerateContentConfig(temperature=0.0),
    before_agent_callback=track_agent,
)
