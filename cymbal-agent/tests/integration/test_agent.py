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

import os
import google.auth
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
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


def test_agent_stream_hardware_troubleshooting() -> None:
    """Integration test for agent streaming on POS hardware troubleshooting (ERR-PAY-4001)."""
    session_service = get_session_service()
    session = session_service.create_session_sync(user_id="store_manager_01", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="How do I fix error code ERR-PAY-4001 on the POS EMV terminal reader?")],
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="store_manager_01",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one streaming event"

    all_texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    all_texts.append(part.text)

    combined_text = " ".join(all_texts)
    assert len(combined_text) > 0, "Expected non-empty text in streaming response"
    # Agent should provide troubleshooting steps or reference runbook
    assert any(term in combined_text.lower() for term in ["err-pay-4001", "emv", "terminal", "reader", "pos"])


def test_agent_stream_cashier_live_audit() -> None:
    """Integration test for agent streaming on cashier real-time metrics and audit status."""
    session_service = get_session_service()
    session = session_service.create_session_sync(user_id="audit_lead_01", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="What are the live rolling 1-hour metrics and override rate for Cashier CASH_1190 at Store 48?")],
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="audit_lead_01",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one streaming event"

    all_texts = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    all_texts.append(part.text)

    combined_text = " ".join(all_texts)
    assert len(combined_text) > 0, "Expected non-empty text in streaming response"
    assert any(term in combined_text.lower() for term in ["1190", "store", "cashier", "override", "audit", "metrics"])
