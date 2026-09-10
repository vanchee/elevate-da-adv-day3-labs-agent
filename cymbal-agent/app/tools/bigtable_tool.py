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

"""Bigtable MCP Toolset integrating Cloud Run MCP Toolbox and Cloud Bigtable."""

import json
import logging
import os
import re
import struct
import subprocess
import time
from typing import Any, Dict, List, Optional

import google.auth
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import McpToolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.cloud import bigtable
from google.cloud.bigtable.row_set import RowSet

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "pvelevate-project")
BIGTABLE_INSTANCE_ID = os.environ.get("BIGTABLE_INSTANCE_ID", "operations-db")
BIGTABLE_TABLE_ID = os.environ.get("BIGTABLE_TABLE_ID", "cashier_realtime_alerts")
BIGTABLE_MCP_SERVICE_URL = os.environ.get(
    "BIGTABLE_MCP_SERVICE_URL",
    "https://mcp-toolbox-bigtable-797556643923.us-central1.run.app",
)


def get_oidc_bearer_token(audience: str) -> Optional[str]:
    """Generates a GCP OIDC ID Token for the target Cloud Run service audience."""
    try:
        cmd = [
            "gcloud",
            "auth",
            "print-identity-token",
            "--include-email",
            "--impersonate-service-account=797556643923-compute@developer.gserviceaccount.com",
            f"--audiences={audience}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        token = res.stdout.strip()
        if token:
            return token
    except Exception as e:
        logger.debug("Failed to obtain impersonated identity token: %s", e)

    try:
        # Fallback to standard gcloud identity token
        cmd = ["gcloud", "auth", "print-identity-token"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e:
        logger.warning("Could not fetch identity token: %s", e)
        return None


def read_cashier_realtime_alerts(store_id: str, cashier_id: str) -> str:
    """Reads live 1-hour rolling metrics and audit status flags for a cashier from Cloud Bigtable.

    Queries the real-time operational alerts table (cashier_realtime_alerts) using the
    standard row key prefix STORE_xxx#CASH_yyyy (e.g. STORE_048#CASH_1190).
    Retrieves live 1-hour manual override rates, transaction counts, promo usage, discount amounts,
    and current audit flags ('clear', 'review', 'investigate').

    Args:
        store_id: Store number or ID (e.g., '48', '048', or 'STORE_048').
        cashier_id: Cashier ID (e.g., '1190', 'CASH_1190').

    Returns:
        Formatted summary containing the latest live 1-hour metrics and audit status flags.
    """
    max_retries = 3
    base_delay = 1.0

    # Normalize store_id and cashier_id format
    store_clean = re.sub(r'[^0-9]', '', store_id)
    if store_clean:
        store_formatted = f"STORE_{int(store_clean):03d}"
    else:
        store_formatted = store_id if store_id.startswith("STORE_") else f"STORE_{store_id}"

    cashier_clean = re.sub(r'[^0-9]', '', cashier_id)
    if cashier_clean:
        cashier_formatted = f"CASH_{int(cashier_clean):04d}"
    else:
        cashier_formatted = cashier_id if cashier_id.startswith("CASH_") else f"CASH_{cashier_id}"

    prefix = f"{store_formatted}#{cashier_formatted}"
    logger.info("Querying Bigtable cashier_realtime_alerts with prefix: %s", prefix)

    for attempt in range(1, max_retries + 1):
        try:
            client = bigtable.Client(project=PROJECT_ID, admin=False)
            instance = client.instance(BIGTABLE_INSTANCE_ID)
            table = instance.table(BIGTABLE_TABLE_ID)

            row_set = RowSet()
            row_set.add_row_range_with_prefix(prefix)
            rows = list(table.read_rows(row_set=row_set, limit=5))

            if not rows:
                return (
                    f"No real-time alert records found for Cashier {cashier_formatted} at {store_formatted} "
                    f"(row prefix: `{prefix}`). The cashier may not have recorded transactions in the current 1-hour window."
                )

            # Take the latest record (reverse timestamp ordered)
            latest_row = rows[0]
            row_key = latest_row.row_key.decode("utf-8", errors="replace")

            flags: Dict[str, str] = {}
            stats: Dict[str, Any] = {}

            for cf, cols in latest_row.cells.items():
                for col_name_b, cell_list in cols.items():
                    col_name = col_name_b.decode("utf-8", errors="replace")
                    val_bytes = cell_list[0].value

                    if cf == "stats":
                        if len(val_bytes) == 8:
                            f_val = struct.unpack(">d", val_bytes)[0]
                            i_val = struct.unpack(">q", val_bytes)[0]
                            # Rates and USD amounts are stored as float64 (double)
                            if any(k in col_name for k in ["rate", "pct", "usd", "score", "ratio"]):
                                stats[col_name] = round(f_val, 4)
                            else:
                                stats[col_name] = i_val
                        elif len(val_bytes) == 4:
                            stats[col_name] = struct.unpack(">i", val_bytes)[0]
                        else:
                            stats[col_name] = val_bytes.decode("utf-8", errors="replace")
                    else:
                        flags[col_name] = val_bytes.decode("utf-8", errors="replace")

            # Calculate override rate if counts exist
            txn_count = stats.get("cashier_1h_txn_count", 0)
            override_count = stats.get("cashier_1h_manual_override_count", 0)
            calc_override_rate = round(override_count / txn_count, 4) if txn_count > 0 else 0.0

            result_summary = {
                "store": store_formatted,
                "cashier": cashier_formatted,
                "latest_row_key": row_key,
                "audit_status": flags.get("audit_status", "UNKNOWN"),
                "audit_trigger_reason": flags.get("audit_trigger_reason", "None"),
                "live_1h_manual_override_count": override_count,
                "live_1h_txn_count": txn_count,
                "live_1h_override_rate": stats.get("cashier_1h_override_rate", calc_override_rate),
                "live_1h_promo_rate": stats.get("cashier_1h_promo_rate", 0.0),
                "live_1h_avg_discount_pct": stats.get("cashier_1h_avg_discount_pct", 0.0),
                "live_1h_total_discount_usd": stats.get("cashier_1h_total_discount_usd", 0.0),
                "risk_score": stats.get("risk_score", 0.0),
                "all_flags": flags,
                "all_stats": stats,
            }

            formatted_text = (
                f"### Real-Time Cashier Metrics: {cashier_formatted} ({store_formatted})\n"
                f"- **Bigtable Instance / Table:** `{BIGTABLE_INSTANCE_ID}` / `{BIGTABLE_TABLE_ID}`\n"
                f"- **Row Key:** `{row_key}`\n"
                f"- **Audit Status:** `{result_summary['audit_status'].upper()}`\n"
                f"- **Live 1-Hour Manual Override Rate:** `{result_summary['live_1h_override_rate'] * 100:.2f}%` "
                f"({override_count} overrides across {txn_count} transactions)\n"
                f"- **Live 1-Hour Promo Rate:** `{result_summary['live_1h_promo_rate'] * 100:.2f}%`\n"
                f"- **Average Discount:** `{result_summary['live_1h_avg_discount_pct']:.2f}%` "
                f"(Total: `${result_summary['live_1h_total_discount_usd']:,.2f}`)\n"
                f"- **Anomaly Risk Score:** `{result_summary['risk_score']}`\n"
                f"\n```json\n{json.dumps(result_summary, indent=2)}\n```"
            )
            return formatted_text

        except Exception as e:
            logger.warning("Error querying Bigtable on attempt %d: %s", attempt, str(e))
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))

    return (
        f"Unable to read live metrics for Cashier {cashier_id} at Store {store_id} due to a transient "
        f"database connectivity error. Please retry shortly."
    )


class BigtableMcpToolset(BaseToolset):
    """ADK Toolset providing Bigtable real-time operational alerts and MCP Toolbox integration."""

    def __init__(self, service_url: Optional[str] = None):
        super().__init__()
        self.service_url = service_url or BIGTABLE_MCP_SERVICE_URL
        self._mcp_toolset: Optional[McpToolset] = None

    def _init_mcp(self) -> None:
        if self._mcp_toolset is None:
            token = get_oidc_bearer_token(self.service_url)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            connection_params = StreamableHTTPConnectionParams(
                url=f"{self.service_url}/mcp",
                headers=headers,
            )
            try:
                self._mcp_toolset = McpToolset(connection_params=connection_params)
            except Exception as e:
                logger.warning("Failed to initialize remote MCP connection: %s", e)

    async def get_tools(self, readonly_context: Optional[ReadonlyContext] = None) -> List[BaseTool]:
        """Returns tools for querying Bigtable real-time alerts."""
        tools: List[BaseTool] = [
            FunctionTool(func=read_cashier_realtime_alerts),
        ]
        try:
            self._init_mcp()
            if self._mcp_toolset:
                remote_tools = await self._mcp_toolset.get_tools(readonly_context)
                tools.extend(remote_tools)
        except Exception as e:
            logger.debug("Remote MCP tools retrieval skipped or unavailable: %s", e)

        return tools

    async def close(self) -> None:
        if self._mcp_toolset:
            await self._mcp_toolset.close()


# Primary toolset instance exported for ADK coordinator binding
bigtable_mcp_toolset = BigtableMcpToolset(service_url=BIGTABLE_MCP_SERVICE_URL)
