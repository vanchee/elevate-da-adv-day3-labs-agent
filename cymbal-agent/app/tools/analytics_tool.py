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

import logging
import os
import time
from unittest.mock import MagicMock

import google.auth
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "pvelevate-project")
DATA_AGENT_ID = os.environ.get("DATA_AGENT_ID", "cymbal-retail-analytics-data-agent")
DATA_AGENT_RESOURCE = os.environ.get(
    "DATA_AGENT_RESOURCE",
    f"projects/{PROJECT_ID}/locations/global/dataAgents/{DATA_AGENT_ID}",
)


def cymbal_analytics_tool(query: str) -> str:
    """Queries the Cymbal retail enterprise analytics data layer using natural language.

    Supports questions about store transactions, inventory reconciliation, stockout risks,
    estimated cover hours, historical cashiers baselines, warranty terms, and sales metrics.
    Pass inquiries referencing standardized enterprise terms (Net Transaction Revenue,
    Total On-Hand Inventory, Estimated Cover Hours, Cashier Manual Override Rate) verbatim.

    Args:
        query: Verbatim natural language business question or SQL analytics request.

    Returns:
        The analytical response including data insights, numbers, and generated metrics.
    """
    max_retries = 3
    base_delay = 1.0

    creds, _ = google.auth.default()
    settings = DataAgentToolConfig(location="global")

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Calling BigQuery Conversational Data Agent (attempt %d): %s", attempt, query)
            result = ask_data_agent(
                data_agent_name=DATA_AGENT_RESOURCE,
                query=query,
                credentials=creds,
                settings=settings,
                tool_context=MagicMock(),
            )

            if result.get("status") == "SUCCESS":
                response_steps = result.get("response", [])
                final_texts = []
                sql_executed = None
                data_table = None

                for step in response_steps:
                    if "text" in step:
                        text_obj = step["text"]
                        if text_obj.get("textType") == "FINAL_RESPONSE":
                            final_texts.extend(text_obj.get("parts", []))
                    if "data" in step and "generatedSql" in step["data"]:
                        sql_executed = step["data"]["generatedSql"]
                    if "Data Retrieved" in step:
                        data_table = step["Data Retrieved"]

                if final_texts or data_table:
                    response_parts = []
                    if final_texts:
                        response_parts.append("\n".join(final_texts))

                    if data_table and "rows" in data_table:
                        headers = data_table.get("headers", [])
                        rows = data_table.get("rows", [])
                        summary = data_table.get("summary", "")
                        table_md = "| " + " | ".join(headers) + " |\n"
                        table_md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
                        for row in rows[:20]:
                            table_md += "| " + " | ".join(str(cell) for cell in row) + " |\n"
                        if summary:
                            table_md += f"\n*{summary}*"
                        response_parts.append(table_md)

                    if sql_executed:
                        response_parts.append(f"*(Generated SQL: `{sql_executed.strip()}`)*")

                    return "\n\n".join(response_parts)

                return "Query executed successfully, but no response text was returned."

            error_msg = result.get("error_details", "Unknown error from Data Agent")
            logger.warning("Data Agent returned error on attempt %d: %s", attempt, error_msg)

        except Exception as e:
            logger.warning("Exception calling Data Agent on attempt %d: %s", attempt, str(e))

        if attempt < max_retries:
            time.sleep(base_delay * (2 ** (attempt - 1)))

    return (
        "Store analytics data is currently unreachable. The BigQuery Conversational Data Agent "
        "could not be reached after multiple retry attempts. Please verify connectivity or retry shortly."
    )
