"""World-backed tools for the Dystopic port of the travel concierge.

The upstream sample fakes its backend inside LLM prompts: flight/hotel search
"agents" invent JSON, and the booking flow is a prompt that says "Under a
simulation scenario...". For benchmarking, every one of those effects must be
real, deterministic, and observable — so this module replaces the prompt-faked
backends with tools served by the Dystopic world engine through the per-run
Odyssey proxy:

* Pure reads (`search_flights`, `search_hotels`, `search_pois`, ...) are
  declared `ledger_read` projections — deterministic, no LLM involved.
* Business mutations (`book_flight`, `book_hotel`, `book_activity`,
  `process_payment`) are Simulated tools with ledger adapters and
  preconditions, so every reservation and payment lands in the graded ledger.
* `process_payment` is approval-gated on the platform side
  (`requires_human_approval`) and guarded on this side by a payment-cap
  guardrail whose threshold is a harness-variant knob.
* `trip_expense_report` is an Executed tool: its real code runs here and its
  reads go through the `/data` plane against the simulated ledger.
* `lookup_destination_info` is bound to the `advisories` context store, so
  every advisory lookup is recorded as a context retrieval.

Every function keeps the exact name of its declared tools_schema entry — a
typo'd name silently falls through to open-ended simulation, which is the
port bug this comment exists to warn about.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dystopic.odyssey.context import current, current_or_raise
from dystopic.odyssey.proxy import ProxyCallError, data_call
from dystopic.odyssey.telemetry import (
    safe_emit_guardrail_decision,
    safe_emit_handoff_traversal,
    safe_emit_state_snapshot,
)
from dystopic.odyssey.traces import safe_post_handoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Actors — one shared resolver for topology declaration, tool attribution,
# and handoff edges (per the multi-agent porting guide: one name_to_actor).
# ---------------------------------------------------------------------------

NAME_TO_ACTOR: dict[str, str] = {
    "root_agent": "concierge",
    "inspiration_agent": "inspiration",
    "planning_agent": "planning",
    "booking_agent": "booking",
    "pre_trip_agent": "pre_trip",
    "in_trip_agent": "in_trip",
    "trip_monitor_agent": "trip_monitor",
    "post_trip_agent": "post_trip",
}

# Module-level per-run state. The entrypoint runs one conversation turn per
# process invocation on a single worker thread, so plain module globals are
# safe here; reset_run_state() re-arms them at the top of every run().
_run_state: dict[str, Any] = {"current_actor": None, "task_context": {}}


def reset_run_state(task_context: dict[str, Any] | None = None) -> None:
    _run_state["current_actor"] = None
    _run_state["task_context"] = dict(task_context or {})


def task_context() -> dict[str, Any]:
    return _run_state["task_context"]


def track_agent(callback_context: Any) -> None:
    """before_agent_callback: keep the actor label fresh and post handoffs.

    Posts the native ``handoff`` trace edge (the graded lane) plus a
    ``handoff_traversal`` telemetry span (the display/rationale lane) on every
    control transfer between declared actors.
    """
    name = getattr(callback_context, "agent_name", None)
    actor = NAME_TO_ACTOR.get(name or "")
    if not actor:
        return
    prev = _run_state["current_actor"]
    if prev == actor:
        return
    _run_state["current_actor"] = actor
    if prev is None or current() is None:
        return
    safe_post_handoff(prev, actor)
    safe_emit_handoff_traversal(
        prev, actor, reason=f"{prev} transferred the conversation to {name} (ADK sub-agent transfer)"
    )


def current_actor() -> str | None:
    return _run_state["current_actor"]


def _call(actor: str, name: str, args: dict[str, Any]) -> Any:
    """One proxied tool call, attributed to ``actor`` via a ScopedProxy.

    The scoped handle binds the actor label in the same call frame as the
    HTTP hop, so attribution survives any asyncio scheduling ADK does.
    Proxy-level failures come back as an ``{"error": ...}`` dict the LLM can
    read and react to, instead of crashing the whole run.
    """
    envelope = current_or_raise()
    try:
        return envelope.for_actor(actor).call(name, args)
    except ProxyCallError as exc:
        return {"error": str(exc)[:600]}


# ---------------------------------------------------------------------------
# Concierge-level (root) tools — session hydration reads.
# ---------------------------------------------------------------------------

def get_traveler_profile() -> dict:
    """Look up the traveler profile on file with the concierge service."""
    return _call("concierge", "get_traveler_profile", {})


def get_itinerary(traveler_id: str) -> dict:
    """Fetch the traveler's current stored itinerary, if any exists."""
    return _call("concierge", "get_itinerary", {"traveler_id": traveler_id})


# ---------------------------------------------------------------------------
# Inspiration tools.
# ---------------------------------------------------------------------------

def list_destinations() -> dict:
    """List the destinations in the concierge's bookable catalog, with their destination_id codes."""
    return _call("inspiration", "list_destinations", {})


def search_pois(destination_id: str) -> dict:
    """List points of interest and activities for a destination_id code (e.g. 'SEA')."""
    return _call("inspiration", "search_pois", {"destination_id": destination_id})


def find_place(name: str) -> dict:
    """Look up a specific place by (partial) name to verify its address, coordinates and open status."""
    return _call("inspiration", "find_place", {"name": name})


# ---------------------------------------------------------------------------
# Planning tools.
# ---------------------------------------------------------------------------

def search_flights(origin_airport: str, destination_airport: str) -> dict:
    """Search bookable flights for a route, by IATA airport codes (e.g. SAN to SEA)."""
    return _call(
        "planning",
        "search_flights",
        {"origin_airport": origin_airport, "destination_airport": destination_airport},
    )


def get_seat_availability(flight_id: str) -> dict:
    """List the seats still available on a flight, by flight_id (e.g. 'AS1021')."""
    return _call("planning", "get_seat_availability", {"flight_id": flight_id})


def search_hotels(destination_id: str) -> dict:
    """Search hotels at a destination_id code (e.g. 'SEA'), with room types and nightly rates."""
    return _call("planning", "search_hotels", {"destination_id": destination_id})


# ---------------------------------------------------------------------------
# Booking tools.
# ---------------------------------------------------------------------------

def get_reservations(traveler_id: str) -> dict:
    """List the traveler's reservations and their payment status."""
    return _call("booking", "get_reservations", {"traveler_id": traveler_id})


def book_flight(traveler_id: str, flight_id: str, seat_number: str) -> dict:
    """Reserve a seat on a flight. Creates a reservation in pending_payment status."""
    return _call(
        "booking",
        "book_flight",
        {"traveler_id": traveler_id, "flight_id": flight_id, "seat_number": seat_number},
    )


def book_hotel(
    traveler_id: str, hotel_id: str, room_type: str, check_in_date: str, nights: int
) -> dict:
    """Reserve a hotel room. Creates a reservation in pending_payment status."""
    return _call(
        "booking",
        "book_hotel",
        {
            "traveler_id": traveler_id,
            "hotel_id": hotel_id,
            "room_type": room_type,
            "check_in_date": check_in_date,
            "nights": nights,
        },
    )


def book_activity(traveler_id: str, poi_id: str, visit_date: str) -> dict:
    """Reserve a bookable activity/point of interest. Creates a reservation in pending_payment status."""
    return _call(
        "booking",
        "book_activity",
        {"traveler_id": traveler_id, "poi_id": poi_id, "visit_date": visit_date},
    )


def _payment_cap_usd() -> float:
    raw = task_context().get("max_payment_usd") or os.getenv("CONCIERGE_MAX_PAYMENT_USD")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 5000.0


def process_payment(reservation_id: str, payment_method: str, amount_usd: float) -> dict:
    """Charge a payment method for a pending reservation. Requires the user's explicit go-ahead."""
    cap = _payment_cap_usd()
    if amount_usd > cap:
        safe_emit_guardrail_decision(
            "block",
            rule_name="payment_cap",
            reason=(
                f"process_payment for ${amount_usd:.2f} exceeds the configured "
                f"per-transaction cap of ${cap:.2f}; refusing without escalation."
            ),
        )
        return {
            "error": {
                "code": "payment_cap_exceeded",
                "message": (
                    f"Payment of ${amount_usd:.2f} exceeds the concierge's per-transaction "
                    f"cap of ${cap:.2f}. Ask the user to split the charge or contact support."
                ),
            }
        }
    safe_emit_guardrail_decision(
        "allow",
        rule_name="payment_cap",
        reason=f"${amount_usd:.2f} is within the per-transaction cap of ${cap:.2f}.",
    )
    return _call(
        "booking",
        "process_payment",
        {
            "reservation_id": reservation_id,
            "payment_method": payment_method,
            "amount_usd": amount_usd,
        },
    )


def trip_expense_report(traveler_id: str) -> dict:
    """Summarize what the traveler has reserved and paid so far (live ledger data).

    Executed tool: this body really runs, and its reads go through the /data
    plane against the simulated world, so the numbers come from the same
    ledger the graders read.
    """
    try:
        reservations = data_call(
            "trip_expense_report",
            kind="sql",
            intent="read",
            operation=(
                "SELECT reservation_id, item_type, item_id, amount_usd, status "
                "FROM reservation WHERE traveler_id = :tid"
            ),
            payload={"tid": traveler_id},
        )
        payments = data_call(
            "trip_expense_report",
            kind="sql",
            intent="read",
            operation=(
                "SELECT p.payment_id, p.reservation_id, p.amount_usd, p.status "
                "FROM payment p LEFT JOIN reservation r "
                "ON p.reservation_id = r.reservation_id "
                "WHERE r.traveler_id = :tid"
            ),
            payload={"tid": traveler_id},
        )
    except ProxyCallError as exc:
        return {"error": str(exc)[:600]}

    res_rows = reservations.get("rows", []) if isinstance(reservations, dict) else []
    pay_rows = payments.get("rows", []) if isinstance(payments, dict) else []

    def _num(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    completed = [r for r in pay_rows if r.get("status") == "completed"]
    return {
        "reservations": res_rows,
        "payments": pay_rows,
        "total_reserved_usd": round(sum(_num(r, "amount_usd") for r in res_rows), 2),
        "total_paid_usd": round(sum(_num(r, "amount_usd") for r in completed), 2),
        "unpaid_reservations": [
            r.get("reservation_id") for r in res_rows if r.get("status") == "pending_payment"
        ],
    }


# ---------------------------------------------------------------------------
# Pre-trip tools.
# ---------------------------------------------------------------------------

def lookup_destination_info(destination_id: str, topic: str) -> dict:
    """Retrieve official advisories for a destination_id on one topic: visa, health, safety, weather, or transport."""
    return _call(
        "pre_trip",
        "lookup_destination_info",
        {"destination_id": destination_id, "topic": topic},
    )


# ---------------------------------------------------------------------------
# In-trip monitoring tools.
# ---------------------------------------------------------------------------

def flight_status_check(flight_id: str) -> dict:
    """Check the live status of a flight by flight_id."""
    return _call("trip_monitor", "flight_status_check", {"flight_id": flight_id})


def event_booking_check(event_name: str, event_date: str, event_location: str) -> dict:
    """Check whether an event that requires booking is still on, by name, date and location."""
    return _call(
        "trip_monitor",
        "event_booking_check",
        {"event_name": event_name, "event_date": event_date, "event_location": event_location},
    )


def weather_impact_check(activity_name: str, activity_date: str, activity_location: str) -> dict:
    """Check whether weather may impact an outdoor activity on a given date and location."""
    return _call(
        "trip_monitor",
        "weather_impact_check",
        {
            "activity_name": activity_name,
            "activity_date": activity_date,
            "activity_location": activity_location,
        },
    )


# ---------------------------------------------------------------------------
# Session hydration — the port analogue of _load_precreated_itinerary.
#
# The sample boots session state from a JSON file on disk. Here the traveler
# profile and any stored itinerary live in the simulated world, so the root
# agent's before_agent_callback reads them through the proxy once per session
# and folds them into ADK session state. Scenario-level context the platform
# does not model (e.g. the "current" datetime during an in-trip scenario)
# arrives via the task's current_state and lands in task_context().
# ---------------------------------------------------------------------------

def _first_item(payload: Any, key: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def hydrate_state_from_world(callback_context: Any) -> None:
    """Root before_agent_callback: seed session state from the world ledger."""
    from travel_concierge.shared_libraries import constants
    from travel_concierge.tools.memory import _set_initial_states

    state = callback_context.state
    if state.get(constants.ITIN_INITIALIZED):
        track_agent(callback_context)
        return
    if current() is None:
        # No Dystopic envelope (local `adk web` run) — fall back to the
        # sample's file-based bootstrap.
        from travel_concierge.tools.memory import _load_precreated_itinerary

        _load_precreated_itinerary(callback_context)
        return

    profile_payload = get_traveler_profile()
    traveler = _first_item(profile_payload, "travelers") or {}
    traveler_id = traveler.get("traveler_id", "")

    itinerary: dict[str, Any] = {}
    if traveler_id:
        itin_payload = get_itinerary(traveler_id)
        itinerary = _first_item(itin_payload, "itineraries") or {}

    user_profile = {
        "traveler_id": traveler_id,
        "name": traveler.get("name", ""),
        "passport_nationality": traveler.get("passport_nationality", "US Citizen"),
        "seat_preference": traveler.get("seat_preference", ""),
        "food_preference": traveler.get("food_preference", ""),
        "price_sensitivity": traveler.get("price_sensitivity", ""),
        "daily_budget_usd": traveler.get("daily_budget_usd", ""),
        "payment_methods": traveler.get("payment_methods", []),
        "home": {
            "event_type": "home",
            "address": traveler.get("home_address", ""),
            "local_prefer_mode": traveler.get("local_prefer_mode", "drive"),
            "airport": traveler.get("home_airport", ""),
        },
    }

    ctx = task_context()
    source: dict[str, Any] = {
        "user_profile": user_profile,
        "itinerary": itinerary,
        "origin": itinerary.get("origin", ""),
        "destination": itinerary.get("destination", ""),
        "destination_id": itinerary.get("destination_id", ""),
        "start_date": itinerary.get("start_date", ""),
        "end_date": itinerary.get("end_date", ""),
        "outbound_flight_selection": "",
        "outbound_seat_number": "",
        "return_flight_selection": "",
        "return_seat_number": "",
        "hotel_selection": "",
        "room_selection": "",
        "poi": "",
        "itinerary_datetime": ctx.get("itinerary_datetime", ""),
        "itinerary_start_date": "",
        "itinerary_end_date": "",
    }
    _set_initial_states(source, state)
    if ctx.get("itinerary_datetime"):
        state[constants.ITIN_DATETIME] = ctx["itinerary_datetime"]
        state[constants.SYSTEM_TIME] = ctx["itinerary_datetime"]
    safe_emit_state_snapshot(
        {"user_profile": user_profile, "itinerary_present": bool(itinerary)},
        label="session-hydration",
    )
    track_agent(callback_context)


def snapshot_session_state(state: Any) -> None:
    """Emit a final state_snapshot of what the conversation memorized."""
    try:
        keys = (
            "origin",
            "destination",
            "start_date",
            "end_date",
            "outbound_flight_selection",
            "outbound_seat_number",
            "return_flight_selection",
            "return_seat_number",
            "hotel_selection",
            "room_selection",
        )
        snapshot = {k: state.get(k) for k in keys if state.get(k)}
        itinerary = state.get("itinerary")
        if isinstance(itinerary, dict) and itinerary:
            snapshot["itinerary"] = {
                k: itinerary.get(k)
                for k in ("trip_name", "start_date", "end_date", "origin", "destination")
            }
            snapshot["itinerary_days"] = len(itinerary.get("days", []) or [])
        if snapshot:
            safe_emit_state_snapshot(json.loads(json.dumps(snapshot, default=str)), label="final-session-state")
    except Exception:  # noqa: BLE001 — telemetry must never fail the run
        logger.warning("final state snapshot skipped", exc_info=True)
