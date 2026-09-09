"""Dystopic code-mode entrypoint for the ADK travel concierge.

The platform imports this file and calls ``run(task_input, *, proxy_url,
run_token)`` inside an E2B sandbox whose kernel thread already has a running
asyncio loop — so the ADK ``Runner`` must execute on a fresh thread
(``run_async_in_thread``), with the Dystopic envelope re-bound *inside* that
thread (ContextVars are thread-local).

ADK has no Dystopic adapter (unlike OpenAI Agents / LangChain), so this port
hand-wires the whole contract:

* World tools are plain functions in ``travel_concierge.world`` that call the
  Odyssey proxy through per-actor ``ScopedProxy`` handles.
* Sub-agent transfers become native ``handoff`` trace edges (graded lane) plus
  ``handoff_traversal`` telemetry spans, via ``before_agent_callback``s.
* Multi-turn replay: the platform's wire-format transcript is converted into
  ADK session events — text turns AND tool-call/tool-result records — so the
  model re-enters the conversation with the ids and prices it already looked
  up. There is no SDK helper for this conversion (``replay_to_input_items`` is
  OpenAI-Responses-shaped), so it is done here by hand.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dystopic.odyssey import Envelope
from dystopic.odyssey.context import set_current
from dystopic.odyssey.envelope import harness_variant_from_input

MAX_LLM_CALLS = 50
APP_NAME = "travel-concierge"
USER_ID = "traveler"

FALLBACK_ERROR_TEXT = (
    "The concierge hit an internal system error before it could finish this request. "
    "No booking was made and no payment was charged in this turn. Please try again."
)

# Which sub-agent owns each world tool — used to attribute REPLAYED tool events
# to their true author. Attributing them all to root_agent teaches the resumed
# model that the root calls specialist tools directly (it cannot: its only
# action is transfer_to_agent), which some models then imitate.
TOOL_OWNER = {
    "get_traveler_profile": "root_agent",
    "get_itinerary": "root_agent",
    "list_destinations": "inspiration_agent",
    "search_pois": "inspiration_agent",
    "find_place": "inspiration_agent",
    "search_flights": "planning_agent",
    "get_seat_availability": "planning_agent",
    "search_hotels": "planning_agent",
    "book_flight": "booking_agent",
    "book_hotel": "booking_agent",
    "book_activity": "booking_agent",
    "process_payment": "booking_agent",
    "get_reservations": "booking_agent",
    "trip_expense_report": "booking_agent",
    "lookup_destination_info": "pre_trip_agent",
    "flight_status_check": "trip_monitor_agent",
    "event_booking_check": "trip_monitor_agent",
    "weather_impact_check": "trip_monitor_agent",
}


def _configure_from_variant(task_input: dict) -> dict | None:
    """Apply harness-variant knobs BEFORE travel_concierge is imported.

    Knobs:
      * ``concierge_model`` — LiteLLM model string (e.g. ``openai/gpt-4.1``).
      * ``max_payment_usd`` — per-transaction payment-cap guardrail threshold.
    """
    variant = harness_variant_from_input(task_input)
    if variant is None:
        return None
    knobs = variant.get("knob_values") or {}
    if knobs.get("concierge_model"):
        os.environ["CONCIERGE_MODEL"] = str(knobs["concierge_model"])
    if knobs.get("max_payment_usd") is not None:
        os.environ["CONCIERGE_MAX_PAYMENT_USD"] = str(knobs["max_payment_usd"])
    return dict(variant)


def run_async_in_thread(coro_factory, envelope: Envelope):
    """Await ``coro_factory()`` on a fresh thread with its own event loop."""
    out: Dict[str, Any] = {}

    def _target() -> None:
        try:
            with set_current(envelope):
                out["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — surfaced to caller
            out["error"] = exc

    worker = threading.Thread(target=_target, name="agent-runner")
    worker.start()
    worker.join()
    if "error" in out:
        raise out["error"]
    return out["result"]


# ---------------------------------------------------------------------------
# Wire-format transcript  ->  ADK session events (replay memory mode).
# ---------------------------------------------------------------------------

def _replay_events(messages: list) -> tuple[list, Any]:
    """Convert prior wire-format messages into ADK Events, splitting off the
    final user message (the new turn's prompt).

    Returns ``(history_events, new_user_text)``.
    """
    from google.adk.events import Event
    from google.genai import types as genai_types

    history: list = []
    call_names: dict[str, str] = {}

    def _text_event(author: str, role: str, text: str) -> Any:
        return Event(
            invocation_id=f"replay-{uuid.uuid4().hex[:8]}",
            author=author,
            content=genai_types.Content(role=role, parts=[genai_types.Part(text=text)]),
        )

    entries = [m for m in messages if isinstance(m, dict)]
    new_user_text = None
    if entries and entries[-1].get("role") == "user" and isinstance(entries[-1].get("content"), str):
        new_user_text = entries[-1]["content"]
        entries = entries[:-1]

    for msg in entries:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str) and content:
            history.append(_text_event("user", "user", content))
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                parts = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    args = call.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (TypeError, ValueError):
                            args = {"raw": args}
                    call_id = call.get("id") or f"call-{uuid.uuid4().hex[:8]}"
                    name = call.get("name") or "unknown_tool"
                    call_names[call_id] = name
                    parts.append(
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(
                                id=call_id, name=name, args=args if isinstance(args, dict) else {}
                            )
                        )
                    )
                if parts:
                    author = TOOL_OWNER.get(call_names.get(parts[0].function_call.id, ""), "root_agent")
                    history.append(
                        Event(
                            invocation_id=f"replay-{uuid.uuid4().hex[:8]}",
                            author=author,
                            content=genai_types.Content(role="model", parts=parts),
                        )
                    )
            if isinstance(content, str) and content:
                history.append(_text_event("root_agent", "model", content))
        elif role == "tool":
            call_id = msg.get("tool_call_id") or ""
            name = call_names.get(call_id, "unknown_tool")
            author = TOOL_OWNER.get(name, "root_agent")
            payload = content
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {"result": payload}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            history.append(
                Event(
                    invocation_id=f"replay-{uuid.uuid4().hex[:8]}",
                    author=author,
                    content=genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part(
                                function_response=genai_types.FunctionResponse(
                                    id=call_id, name=name, response=payload
                                )
                            )
                        ],
                    ),
                )
            )
    return history, new_user_text


# ---------------------------------------------------------------------------
# ADK events  ->  wire-format transcript (this turn only).
# ---------------------------------------------------------------------------

def _events_to_wire(events: list) -> list:
    out: list = []
    for event in events:
        content = getattr(event, "content", None)
        if content is None or not getattr(content, "parts", None):
            continue
        author = getattr(event, "author", "") or ""
        for part in content.parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                role = "user" if author == "user" else "assistant"
                out.append({"role": role, "content": text})
                continue
            fc = getattr(part, "function_call", None)
            if fc is not None:
                args = fc.args if isinstance(fc.args, dict) else {}
                out.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": fc.id or "", "name": fc.name or "", "arguments": args}
                        ],
                    }
                )
                continue
            fr = getattr(part, "function_response", None)
            if fr is not None:
                response = fr.response if fr.response is not None else {}
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": fr.id or "",
                        "content": json.dumps(response, default=str),
                    }
                )
    return out


def run(task_input: dict, *, proxy_url: str, run_token: str) -> dict:
    """Platform entrypoint. Returns ``{"final_response", "messages", "metadata"}``."""
    os.environ.setdefault("DYSTOPIC_ODYSSEY_PROXY_URL", proxy_url)
    os.environ.setdefault("DYSTOPIC_RUN_TOKEN", run_token)
    envelope = Envelope.from_env()
    task_input = task_input if isinstance(task_input, dict) else {}

    variant = _configure_from_variant(task_input)

    # Import AFTER knob env vars are set — MODEL is resolved at import time.
    from travel_concierge import MODEL_NAME  # noqa: F401  (triggers agent build)
    from travel_concierge import world
    from travel_concierge.agent import root_agent

    # Per-scenario context the platform world does not model (e.g. the
    # "current" datetime of an in-trip scenario) rides on the task's
    # current_state, which code mode surfaces under task_input["input"].
    current_state = task_input.get("input")
    if not isinstance(current_state, dict):
        current_state = {}
    world.reset_run_state(
        {
            "itinerary_datetime": task_input.get("itinerary_datetime")
            or current_state.get("itinerary_datetime"),
            "max_payment_usd": os.environ.get("CONCIERGE_MAX_PAYMENT_USD"),
        }
    )

    messages = task_input.get("messages")
    history_events: list = []
    user_text = task_input.get("user_instruction") or ""
    if isinstance(messages, list) and messages:
        history_events, replay_user = _replay_events(messages)
        if replay_user:
            user_text = replay_user
    if not user_text:
        user_text = json.dumps(task_input)[:2000]

    collected: Dict[str, Any] = {"events": [], "final_text": None}

    async def _run():
        from google.adk.agents.run_config import RunConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as genai_types

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id="s1", state={}
        )
        for event in history_events:
            await session_service.append_event(session, event)

        runner = Runner(
            agent=root_agent, app_name=APP_NAME, session_service=session_service
        )
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id="s1",
            new_message=genai_types.Content(
                role="user", parts=[genai_types.Part(text=user_text)]
            ),
            run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS),
        ):
            collected["events"].append(event)
            content = getattr(event, "content", None)
            if content is not None and getattr(content, "parts", None):
                texts = [
                    p.text
                    for p in content.parts
                    if isinstance(getattr(p, "text", None), str) and p.text.strip()
                ]
                if texts and getattr(event, "author", "") != "user":
                    collected["final_text"] = "\n".join(texts)

        final_session = await session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id="s1"
        )
        if final_session is not None:
            world.snapshot_session_state(dict(final_session.state))

    metadata: Dict[str, Any] = {"model": os.environ.get("CONCIERGE_MODEL", "openai/gpt-4.1")}
    if variant is not None:
        metadata["harness_variant"] = {
            "source": "harness_variant",
            "name": variant.get("name"),
            "snapshot_id": variant.get("snapshot_id"),
            "fingerprint": variant.get("fingerprint"),
        }

    try:
        run_async_in_thread(_run, envelope)
    except Exception as exc:  # noqa: BLE001 — never return an empty final_response
        metadata.update({"outcome": "error", "detail": str(exc)[:400]})
        partial = collected.get("final_text")
        return {
            "final_response": partial or FALLBACK_ERROR_TEXT,
            "messages": _events_to_wire(collected["events"]) or None,
            "metadata": metadata,
        }

    final_text = collected.get("final_text") or (
        "The concierge completed without a textual response."
    )
    metadata.update({"outcome": "completed", "last_agent": world.current_actor()})
    return {
        "final_response": final_text,
        "messages": _events_to_wire(collected["events"]) or None,
        "metadata": metadata,
    }
