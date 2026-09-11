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

"""Analytics tool integrating with BigQuery Conversational Data Agent."""

import asyncio
import json
import logging
from typing import Any, Optional

import google.auth
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent
from google.adk.tools.tool_context import ToolContext

from app import config

logger = logging.getLogger(__name__)

MAX_TABLE_ROWS = 20

_credentials = None


def _get_credentials():
    """Returns cached Application Default Credentials."""
    global _credentials
    if _credentials is None:
        _credentials, _ = google.auth.default()
    return _credentials


def _render_response(result: dict[str, Any]) -> Optional[str]:
    """Renders a successful Data Agent payload into markdown for the coordinator."""
    response_steps = result.get("response", [])
    final_texts: list[str] = []
    sql_executed: Optional[str] = None
    data_table: Optional[dict[str, Any]] = None

    for step in response_steps:
        if "text" in step:
            text_obj = step["text"]
            if text_obj.get("textType") == "FINAL_RESPONSE":
                final_texts.extend(text_obj.get("parts", []))
        if "data" in step and "generatedSql" in step["data"]:
            sql_executed = step["data"]["generatedSql"]
        if "Data Retrieved" in step:
            data_table = step["Data Retrieved"]

    if not final_texts and not data_table:
        return None

    response_parts: list[str] = []
    if final_texts:
        response_parts.append("\n".join(final_texts))

    if data_table and "rows" in data_table:
        headers = data_table.get("headers", [])
        rows = data_table.get("rows", [])
        summary = data_table.get("summary", "")
        table_md = "| " + " | ".join(headers) + " |\n"
        table_md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in rows[:MAX_TABLE_ROWS]:
            table_md += "| " + " | ".join(str(cell) for cell in row) + " |\n"
        if len(rows) > MAX_TABLE_ROWS:
            table_md += f"\n*Showing {MAX_TABLE_ROWS} of {len(rows)} rows.*"
        if summary:
            table_md += f"\n*{summary}*"
        response_parts.append(table_md)

    if sql_executed:
        response_parts.append(f"*(Generated SQL: `{sql_executed.strip()}`)*")

    return "\n\n".join(response_parts)


async def cymbal_analytics_tool(query: str, tool_context: ToolContext) -> str:
    """Queries the Cymbal retail enterprise analytics data layer using natural language.

    Supports questions about store transactions, inventory reconciliation, stockout risks,
    estimated cover hours, historical cashiers baselines, warranty terms, and sales metrics.
    Pass inquiries referencing standardized enterprise terms (Net Transaction Revenue,
    Total On-Hand Inventory, Estimated Cover Hours, Cashier Manual Override Rate) verbatim.

    Args:
        query: Verbatim natural language business question or SQL analytics request.

    Returns:
        The analytical response including data insights, numbers, and generated metrics,
        or a formatted JSON error payload contract under persistent failure.
    """
    data_agent_resource = config.get_data_agent_resource()
    settings = DataAgentToolConfig()

    def _blocking_ask() -> dict[str, Any]:
        # The Data Agent SDK call is synchronous; run it off the event loop so a
        # concurrently dispatched Bigtable call is not blocked behind it.
        return ask_data_agent(
            data_agent_name=data_agent_resource,
            query=query,
            credentials=_get_credentials(),
            settings=settings,
            tool_context=tool_context,
        )

    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            logger.info(
                "Calling BigQuery Conversational Data Agent (attempt %d): %s", attempt, query
            )
            result = await asyncio.to_thread(_blocking_ask)

            if result.get("status") == "SUCCESS":
                rendered = _render_response(result)
                if rendered:
                    return rendered
                return "Query executed successfully, but no response text was returned."

            logger.warning(
                "Data Agent returned error on attempt %d: %s",
                attempt,
                result.get("error_details", "Unknown error from Data Agent"),
            )

        except Exception as e:
            logger.warning("Exception calling Data Agent on attempt %d: %s", attempt, e)

        if attempt < config.MAX_RETRIES:
            await asyncio.sleep(config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))

    return json.dumps({
        "status": "ERROR",
        "error": (
            "Store analytics data is currently unreachable. The BigQuery Conversational "
            "Data Agent could not be reached after multiple retry attempts. Please verify "
            "connectivity or retry shortly."
        ),
        "data": None,
    })
