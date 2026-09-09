# Dystopic port

This directory holds everything the Dystopic platform needs to benchmark the
travel concierge:

| File | What it is |
|---|---|
| `main.py` | Code-mode entrypoint (`run(task_input, *, proxy_url, run_token)`). Worker-thread ADK runner, wire↔ADK transcript conversion, harness-variant knobs, telemetry. |
| `world/tools_schema.json` | The 18 declared world tools: `ledger_read` projections, simulated mutations with adapters + preconditions, an approval-gated `process_payment`, an Executed `trip_expense_report`, a context-store-bound `lookup_destination_info`. |
| `world/ledger_schema.json` | The domain ontology: traveler, destination, flight, hotel, poi, reservation, payment, advisory_doc, itinerary. |
| `world/topology.json` | The declared multi-agent topology (8 actors). |
| `world/build_worlds.py` | Emits the three World-variant `initial_state` files (inventory-base, seattle-in-trip, hanoi-pre-trip). |

The port's thesis: the upstream sample fakes its backend inside prompts
("Under a simulation scenario, you are a travel booking reservation agent…").
To benchmark the agent, those fakes move into a **simulated world** the
platform owns — deterministic, seeded, and graded — while the agent's own
reasoning, prompts, and multi-agent structure stay intact.

Key upstream deltas (all documented in the port commit):

* Gemini/Vertex → LiteLLM (`CONCIERGE_MODEL`, default `openai/gpt-4.1`).
* `google_search` grounding → `lookup_destination_info` over a seeded
  advisory corpus (context-plane recorded).
* Maps MCP toolset → `find_place` ledger read.
* Prompt-faked flight/hotel/seat/room "search agents" → real `search_flights`
  / `get_seat_availability` / `search_hotels` reads over seeded inventory.
* Prompt-faked booking/payment agents → `book_*` + `process_payment` tools
  with ledger adapters, preconditions, and a human-approval gate.
* The in-trip monitor's hardcoded "Space Needle is closed" mock → a world
  fact (`poi.open_status = "closed"`).
* Arize tracing → Dystopic trace/telemetry lanes (handoffs, guardrails,
  state snapshots).
