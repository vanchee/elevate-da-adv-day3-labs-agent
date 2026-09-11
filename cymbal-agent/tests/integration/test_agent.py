# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Live integration tests for the Cymbal Operations Coordinator Agent.

These assert on the ADK trace - which tools were dispatched, in which turn - rather
than only on keywords in the final prose. A response can easily contain the word
"cashier" without any tool having run, so keyword-only assertions would pass even if
the agent silently stopped calling its tools.
"""

import os

import google.auth
import pytest
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.genai import types

# Ensure Vertex AI mode is enabled by default using ADC if no Gemini API key is set
if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        try:
            _, project = google.auth.default()
            if project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project
        except Exception:
            pass
    if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

from app.agent import root_agent
from app.app_utils.services import get_session_service


def _run(prompt: str, user_id: str) -> list:
    """Runs one turn against the coordinator agent and returns the raw event list."""
    session_service = get_session_service()
    session = session_service.create_session_sync(user_id=user_id, app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    return list(
        runner.run(
            new_message=message,
            user_id=user_id,
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )


def _tool_calls(events: list) -> list[str]:
    """Returns every tool name invoked across the run, in dispatch order."""
    names = []
    for event in events:
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.function_call:
                names.append(part.function_call.name)
    return names


def _parallel_call_groups(events: list) -> list[list[str]]:
    """Returns tool names grouped by event.

    ADK emits all function calls the model requested in a single turn as parts of one
    event, so a group with more than one entry is a genuine parallel dispatch.
    """
    groups = []
    for event in events:
        if not event.content or not event.content.parts:
            continue
        names = [p.function_call.name for p in event.content.parts if p.function_call]
        if names:
            groups.append(names)
    return groups


def _text(events: list) -> str:
    parts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    parts.append(part.text)
    return " ".join(parts)


def test_agent_stream_hardware_troubleshooting() -> None:
    """UC 1.1a: hardware fault must be routed to the RAG tool and cite the runbook."""
    events = _run(
        "How do I fix error code ERR-PAY-4001 on the POS EMV terminal reader?",
        "store_manager_01",
    )

    assert "pos_troubleshooting_rag_tool" in _tool_calls(events)

    combined = _text(events)
    assert len(combined) > 0, "Expected non-empty text in streaming response"
    assert any(term in combined.lower() for term in ["err-pay-4001", "emv", "reader"])


def test_agent_stream_cashier_live_audit() -> None:
    """UC 1.3: live cashier metrics must come from Bigtable, decoded (not base64)."""
    events = _run(
        "What are the live rolling 1-hour metrics and override rate for Cashier CASH_1190 at Store 48?",
        "audit_lead_01",
    )

    assert "read_cashier_realtime_alerts" in _tool_calls(events)

    combined = _text(events)
    assert len(combined) > 0
    assert "1190" in combined
    # Regression guard: the model must never be relaying raw base64 Bigtable cells.
    assert "Y2FzaGll" not in combined, "raw base64 leaked into the agent response"


def test_agent_out_of_scope_hardware_declines() -> None:
    """UC 1.1c: out-of-scope hardware must trigger the certified decline, not a guess."""
    events = _run(
        "How do I replace the engine oil on a Ford F-150 truck?",
        "store_manager_02",
    )

    combined = _text(events).lower()
    assert any(
        phrase in combined
        for phrase in ["cannot find", "not able to", "no certified", "outside", "unable to"]
    ), f"Expected a refusal for out-of-scope hardware, got: {combined[:300]}"


@pytest.mark.slow
def test_agent_parallel_dispatch_dual_cashier_baseline() -> None:
    """UC 2.2: live-vs-baseline comparison must dispatch both tools in a single turn."""
    events = _run(
        "What is Cashier CASH_1190's live 1-hour override rate right now, compared to "
        "their 7-day historical override baseline?",
        "audit_lead_02",
    )

    calls = _tool_calls(events)
    assert "read_cashier_realtime_alerts" in calls, f"Bigtable tool not called; saw {calls}"
    assert "cymbal_analytics_tool" in calls, f"Analytics tool not called; saw {calls}"

    groups = _parallel_call_groups(events)
    assert any(len(group) > 1 for group in groups), (
        f"Expected a parallel dispatch (2+ tool calls in one turn), got groups: {groups}"
    )


@pytest.mark.slow
def test_agent_sequential_dispatch_cross_cloud_audit() -> None:
    """UC 2.3: offender ranking then log retrieval must be two sequential turns."""
    events = _run(
        "Show cashiers with active cashier promo abuse alerts in the last 7 days and "
        "retrieve checkout logs for the top offender.",
        "auditor_01",
    )

    groups = _parallel_call_groups(events)
    assert len(groups) >= 2, f"Expected at least two sequential dispatch turns, got: {groups}"
    assert all("cymbal_analytics_tool" in g for g in groups[:2]), (
        f"Expected both audit turns to query the analytics layer, got: {groups}"
    )


def test_agent_resolves_store_name_before_querying() -> None:
    """Part 5 protocol D: an informal store name must be resolved to an ID first."""
    events = _run(
        "How much total on-hand inventory is at our Paris flagship store?",
        "store_manager_03",
    )

    calls = _tool_calls(events)
    assert "resolve_store_identifier" in calls, (
        f"Expected the store name to be resolved before querying; saw {calls}"
    )
    # Resolution must precede analytics, otherwise the analytics call guessed an ID.
    if "cymbal_analytics_tool" in calls:
        assert calls.index("resolve_store_identifier") < calls.index("cymbal_analytics_tool")

    assert "STORE_007" in _text(events)


@pytest.mark.slow
def test_agent_refuses_to_guess_between_identically_named_stores() -> None:
    """STORE_001 and STORE_013 share a name; the agent must ask, not pick.

    This is the failure mode that matters: guessing does not surface as an error, it
    surfaces as a confident answer about the wrong store.
    """
    events = _run(
        "What is the total on-hand inventory at the Ginza store?",
        "store_manager_04",
    )

    calls = _tool_calls(events)
    assert "resolve_store_identifier" in calls, f"Store resolution not attempted; saw {calls}"
    assert "cymbal_analytics_tool" not in calls, (
        f"Agent queried analytics despite an ambiguous store reference; saw {calls}"
    )

    combined = _text(events)
    assert "STORE_001" in combined and "STORE_013" in combined, (
        f"Expected both ambiguous store IDs to be surfaced, got: {combined[:400]}"
    )
