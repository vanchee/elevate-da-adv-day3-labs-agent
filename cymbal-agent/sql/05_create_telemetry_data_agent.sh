#!/usr/bin/env bash
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
#
# Creates the BigQuery Conversational Data Agent used to explore agent telemetry
# (Module 3 / Part 4.1).
#
# This is the observability counterpart to the retail analytics Data Agent: the
# same conversational interface, pointed at the agent's own operational logs
# rather than at store data. It answers "how is the agent behaving?" instead of
# "how are the stores performing?".
#
# Scoped to the raw `events` table plus the three auto-created views that carry
# the fields the query recipes need. Pointing it at the views as well as the raw
# table matters: the useful columns (tool_name, latency, token counts) live
# inside JSON columns on `events`, and the views are what flatten them out.
#
# Usage:
#   PROJECT_ID=my-project ./sql/05_create_telemetry_data_agent.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
DATASET="${BQ_TELEMETRY_DATASET:-agent_telemetry}"
AGENT_ID="${TELEMETRY_DATA_AGENT_ID:-cymbal-agent-telemetry-data-agent}"
LOCATION="global"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "PROJECT_ID is not set and gcloud has no default project." >&2
  exit 1
fi

echo "Creating Data Agent '${AGENT_ID}' over ${PROJECT_ID}.${DATASET}"

TOKEN="$(gcloud auth print-access-token)"

read -r -d '' SYSTEM_INSTRUCTION <<'INSTRUCTION' || true
### 1. Role & Scope
You are the Cymbal Agent Operations Telemetry Analyst. You answer questions about
how the Cymbal Operations Coordinator Agent is *behaving* -- its cost, latency,
reliability and tool usage -- by querying the BigQuery Agent Analytics telemetry
in the `agent_telemetry` dataset. You do not answer retail business questions.

### 2. Table Selection Matrix
- **`agent_telemetry.events`** is the raw append-only event stream. One row per
  lifecycle event. Prefer the views below; fall back to `events` only when you
  need a field the views do not expose.
- **`agent_telemetry.v_tool_completed`** for tool latency, tool call counts and
  tool-level success. This is the table for "which tool is slowest".
- **`agent_telemetry.v_llm_response`** for model name, token usage and cost
  analysis.
- **`agent_telemetry.v_tool_error`** and **`v_invocation_error`** for failures.

### 3. Schema Notes (important)
- `event_type` is the discriminator. Values include `USER_MESSAGE_RECEIVED`,
  `INVOCATION_STARTING`, `AGENT_STARTING`, `LLM_REQUEST`, `LLM_RESPONSE`,
  `TOOL_STARTING`, `TOOL_COMPLETED`, `AGENT_RESPONSE`, `AGENT_COMPLETED`,
  `INVOCATION_COMPLETED`, and the corresponding `*_ERROR` variants.
- `latency_ms`, `attributes` and `content` are **JSON columns**, not scalars.
  Extract with `JSON_VALUE(col, '$.key')` and cast explicitly, e.g.
  `SAFE_CAST(JSON_VALUE(latency_ms, '$.total') AS FLOAT64)`. Never compare a
  JSON column directly to a number.
- `status` is `OK` on success; error detail is in `error_message`.
- A conversation is identified by `session_id`; a single user turn by
  `invocation_id`. Count turns with `COUNT(DISTINCT invocation_id)`, not
  `COUNT(*)`, because each turn emits roughly ten rows.

### 4. Analysis Conventions
- Report latency as **P50 and P95**, not just the mean -- agent latency is
  heavily right-skewed by cold starts, and the mean hides that.
- When asked for a distribution, return both the absolute count and the
  percentage of total.
- Always state the time window you applied. Default to the last 7 days when the
  user does not specify one, and say so.
- Token and cost questions must be grouped by `model`; different models have
  different rates, so a blended total is misleading.
INSTRUCTION

python3 - "$PROJECT_ID" "$DATASET" "$SYSTEM_INSTRUCTION" > /tmp/telemetry_agent_payload.json <<'PY'
import json, sys
project, dataset, instruction = sys.argv[1], sys.argv[2], sys.argv[3]
tables = ["events", "v_tool_completed", "v_llm_response", "v_tool_error"]
payload = {
    "displayName": "Cymbal Agent Telemetry Data Agent",
    "description": (
        "Conversational analytics over BigQuery Agent Analytics telemetry for the "
        "Cymbal Operations Coordinator Agent: cost, latency, reliability, tool usage."
    ),
    "dataAnalyticsAgent": {
        "publishedContext": {
            "systemInstruction": instruction,
            "datasourceReferences": {
                "bq": {
                    "tableReferences": [
                        {"projectId": project, "datasetId": dataset, "tableId": t}
                        for t in tables
                    ]
                }
            },
        }
    },
}
json.dump(payload, sys.stdout)
PY

HTTP_CODE=$(curl -s -o /tmp/telemetry_agent_response.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://geminidataanalytics.googleapis.com/v1beta/projects/${PROJECT_ID}/locations/${LOCATION}/dataAgents?dataAgentId=${AGENT_ID}" \
  -d @/tmp/telemetry_agent_payload.json)

echo "HTTP ${HTTP_CODE}"
cat /tmp/telemetry_agent_response.json
echo

if [[ "${HTTP_CODE}" == "200" || "${HTTP_CODE}" == "409" ]]; then
  echo
  echo "Data Agent resource:"
  echo "  projects/${PROJECT_ID}/locations/${LOCATION}/dataAgents/${AGENT_ID}"
  if [[ "${HTTP_CODE}" == "409" ]]; then
    echo "  (already existed - not modified)"
  fi
  # Note: a trailing `[[ ... ]] && echo ...` here would make the script exit 1
  # whenever the test is false, reporting failure on a successful create.
  exit 0
else
  echo "Creation failed." >&2
  exit 1
fi
