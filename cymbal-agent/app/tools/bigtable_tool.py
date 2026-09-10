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

import base64
import json
import logging
import os
import re
import struct
import time
from typing import Any, Dict, List, Optional

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request
import google.oauth2.id_token
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import McpToolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
import httpx

logger = logging.getLogger(__name__)


def _discover_project_id() -> str:
    """Discovers project ID from environment or ADC credentials dynamically."""
    if os.environ.get("PROJECT_ID"):
        return os.environ["PROJECT_ID"]
    try:
        _, project = google.auth.default()
        if project:
            return project
    except Exception:
        pass
    return "pvelevate-project"


PROJECT_ID = _discover_project_id()
BIGTABLE_INSTANCE_ID = os.environ.get("BIGTABLE_INSTANCE_ID", "operations-db")
BIGTABLE_TABLE_ID = os.environ.get("BIGTABLE_TABLE_ID", "cashier_realtime_alerts")
BIGTABLE_ENRICHED_TABLE_ID = os.environ.get(
    "BIGTABLE_ENRICHED_TABLE_ID", "pos_transactions_enriched"
)
BIGTABLE_MCP_SERVICE_URL = os.environ.get(
    "BIGTABLE_MCP_SERVICE_URL",
    "https://mcp-toolbox-bigtable-797556643923.us-central1.run.app",
)


def get_oidc_bearer_token(audience: str) -> Optional[str]:
    """Generates a GCP OIDC ID Token for the target Cloud Run service audience natively via google.auth."""
    auth_req = Request()

    # 1. Dynamic resolution of project compute service account if running with user ADC or on Cloudtop
    try:
        creds, project = google.auth.default()
        target_sa = os.environ.get("BIGTABLE_MCP_SERVICE_ACCOUNT")
        if not target_sa and project:
            try:
                from google.cloud import resourcemanager_v3
                crm_client = resourcemanager_v3.ProjectsClient(credentials=creds)
                proj = crm_client.get_project(name=f"projects/{project}")
                project_number = proj.name.split("/")[-1]
                target_sa = f"{project_number}-compute@developer.gserviceaccount.com"
            except Exception as ex:
                logger.debug("Could not resolve project number via resource manager: %s", ex)

        if target_sa and creds:
            source_creds = impersonated_credentials.Credentials(
                source_credentials=creds,
                target_principal=target_sa,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            id_creds = impersonated_credentials.IDTokenCredentials(
                target_credentials=source_creds,
                target_audience=audience,
                include_email=True,
            )
            id_creds.refresh(auth_req)
            if id_creds.token:
                return id_creds.token
    except Exception as e:
        logger.debug("Dynamic impersonation ID token generation skipped: %s", e)

    # 2. Standard native fetch_id_token (used in Cloud Run / standard compute environments)
    try:
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        if token:
            return token
    except Exception as e:
        logger.warning("Could not fetch native OIDC identity token via google.auth: %s", e)

    return None


def read_cashier_realtime_alerts(store_id: str, cashier_id: str) -> str:
    """Reads live 1-hour rolling metrics and audit status flags for a cashier from Cloud Bigtable.

    Invokes the remote declarative MCP 'bigtable-sql' microservice to query the real-time operational
    alerts table (cashier_realtime_alerts) using GoogleSQL with row key prefix STORE_xxx#CASH_yyyy.
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

    prefix = f"{store_formatted}#{cashier_formatted}%"
    logger.info("Querying Bigtable MCP service (%s) with prefix: %s", BIGTABLE_MCP_SERVICE_URL, prefix)

    for attempt in range(1, max_retries + 1):
        try:
            token = get_oidc_bearer_token(BIGTABLE_MCP_SERVICE_URL)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            payload = {
                "jsonrpc": "2.0",
                "id": attempt,
                "method": "tools/call",
                "params": {
                    "name": "read_cashier_realtime_alerts",
                    "arguments": {"prefix": prefix},
                },
            }

            with httpx.Client(headers=headers, timeout=15.0) as client:
                resp = client.post(f"{BIGTABLE_MCP_SERVICE_URL}/mcp", json=payload)
                resp.raise_for_status()
                data = resp.json()

            result = data.get("result", {})
            content_list = result.get("content", [])
            if not content_list:
                return (
                    f"No real-time alert records found for Cashier {cashier_formatted} at {store_formatted} "
                    f"(row prefix: `{prefix}`). The cashier may not have recorded transactions in the current 1-hour window."
                )

            rows = []
            for item in content_list:
                if item.get("type") == "text":
                    try:
                        rows.append(json.loads(item["text"]))
                    except Exception:
                        pass

            if not rows:
                return (
                    f"No real-time alert records found for Cashier {cashier_formatted} at {store_formatted} "
                    f"(row prefix: `{prefix}`). The cashier may not have recorded transactions in the current 1-hour window."
                )

            # Take the latest record
            latest_row = rows[0]
            raw_key = latest_row.get("_key", "")
            try:
                row_key = base64.b64decode(raw_key).decode("utf-8", errors="replace") if raw_key else prefix
            except Exception:
                row_key = raw_key or prefix

            flags: Dict[str, str] = {}
            for k, v in latest_row.get("flags", {}).items():
                try:
                    dec_k = base64.b64decode(k).decode("utf-8", errors="replace")
                    dec_v = base64.b64decode(v).decode("utf-8", errors="replace")
                    flags[dec_k] = dec_v
                except Exception:
                    flags[k] = str(v)

            stats: Dict[str, Any] = {}
            for k, v in latest_row.get("stats", {}).items():
                try:
                    dec_k = base64.b64decode(k).decode("utf-8", errors="replace")
                    raw_v = base64.b64decode(v)
                    if len(raw_v) == 8:
                        f_val = struct.unpack(">d", raw_v)[0]
                        i_val = struct.unpack(">q", raw_v)[0]
                        if any(term in dec_k for term in ["rate", "pct", "usd", "score", "ratio"]):
                            stats[dec_k] = round(f_val, 4)
                        else:
                            stats[dec_k] = i_val
                    elif len(raw_v) == 4:
                        stats[dec_k] = struct.unpack(">i", raw_v)[0]
                    else:
                        stats[dec_k] = raw_v.decode("utf-8", errors="replace")
                except Exception:
                    stats[k] = v

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
            logger.warning("Error querying Bigtable MCP service on attempt %d: %s", attempt, str(e))
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))

    return (
        f"Unable to read live metrics for Cashier {cashier_id} at Store {store_id} due to a transient "
        f"database connectivity error. Please retry shortly."
    )


def _format_enriched_response(store_formatted: str, parsed_records: List[Dict[str, Any]]) -> str:
    formatted_output = [
        f"### Cloud Bigtable Enriched Transactions: `{BIGTABLE_ENRICHED_TABLE_ID}`",
        f"- **Target Store:** `{store_formatted}`",
        f"- **Matched Records:** {len(parsed_records)}",
    ]
    for idx, rec in enumerate(parsed_records, 1):
        formatted_output.append(
            f"\n#### Transaction #{idx}: `{rec.get('transaction_id', rec['row_key'])}`\n"
            f"- **Cashier:** `{rec.get('cashier_id', 'N/A')}` | **POS Terminal:** `{rec.get('pos_terminal_id', 'N/A')}`\n"
            f"- **Timestamp:** `{rec.get('event_timestamp', 'N/A')}`\n"
            f"- **Total:** `${rec.get('total', 0.0):.2f}` (Subtotal: `${rec.get('subtotal_amount', 0.0):.2f}`, Discount: `${rec.get('discount', 0.0):.2f}`)\n"
            f"- **Promo Code:** `{rec.get('promo_code_applied', 'NONE')}` | **Manual Override:** `{rec.get('manual_discount_flag', False)}`\n"
            f"- **Anomaly Alerts:** `{json.dumps(rec.get('anomaly_alerts', {}))}`"
        )
    formatted_output.append(f"\n```json\n{json.dumps(parsed_records, indent=2)}\n```")
    return "\n".join(formatted_output)


def read_pos_transactions_enriched(
    store_id: str,
    cashier_id: Optional[str] = None,
    transaction_id: Optional[str] = None,
) -> str:
    """Queries Cloud Bigtable pos_transactions_enriched for frontline POS transactions and anomaly flags.

    Provides point-lookup or recent transaction history for a specific store,
    cashier, or transaction ID, returning enriched transaction details and fraud/anomaly risk scores.

    Args:
        store_id: Store identifier (e.g., 'STORE_048' or '48').
        cashier_id: Cashier identifier (e.g., 'CASH_1190' or '1190'), optional if transaction_id provided.
        transaction_id: Transaction identifier (e.g., 'TXN-20260906-0220917'), optional if cashier_id provided.

    Returns:
        Structured summary of enriched POS transactions including total, discount, promo code,
        manual override status, and any active anomaly alerts.
    """
    max_retries = 3
    base_delay = 1.0

    store_clean = str(store_id).strip()
    store_formatted = (
        f"STORE_{int(store_clean):03d}"
        if store_clean.isdigit()
        else (store_clean if store_clean.startswith("STORE_") else f"STORE_{store_clean}")
    )

    cashier_formatted: Optional[str] = None
    if cashier_id:
        c_clean = str(cashier_id).strip()
        cashier_formatted = (
            f"CASH_{int(c_clean):04d}"
            if c_clean.isdigit()
            else (c_clean if c_clean.startswith("CASH_") else f"CASH_{c_clean}")
        )

    # 1. Attempt remote declarative MCP Toolbox call
    token = get_oidc_bearer_token(BIGTABLE_MCP_SERVICE_URL)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers["Content-Type"] = "application/json"

    mcp_prefix = (
        f"{store_formatted}#{transaction_id}"
        if transaction_id
        else f"{store_formatted}#TXN-%"
    )

    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_pos_transactions_enriched",
                "arguments": {"prefix": mcp_prefix},
            },
        }
        with httpx.Client(headers=headers, timeout=10.0) as client:
            resp = client.post(f"{BIGTABLE_MCP_SERVICE_URL}/mcp", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                if "result" in data and not data.get("isError"):
                    content_list = data["result"].get("content", [])
                    texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
                    if texts and texts[0].strip() not in ("[]", ""):
                        mcp_records = []
                        for t in texts:
                            try:
                                r = json.loads(t)
                                raw_key = r.get("_key", "")
                                try:
                                    rk = base64.b64decode(raw_key).decode("utf-8", errors="replace") if raw_key else mcp_prefix
                                except Exception:
                                    rk = raw_key or mcp_prefix
                                rec: Dict[str, Any] = {"row_key": rk, "store_id": store_formatted}
                                for k, v in r.get("tx", {}).items():
                                    try:
                                        dk = base64.b64decode(k).decode("utf-8", errors="replace")
                                        dv = base64.b64decode(v).decode("utf-8", errors="replace")
                                        if dk in ["total", "subtotal_amount", "discount", "tax_amount"]:
                                            rec[dk] = float(dv)
                                        elif dk in ["item_count", "total_quantity"]:
                                            rec[dk] = int(dv)
                                        elif dk in ["manual_discount_flag", "is_contactless"]:
                                            rec[dk] = dv.lower() == "true"
                                        elif dk == "items":
                                            rec[dk] = json.loads(dv)
                                        else:
                                            rec[dk] = dv
                                    except Exception:
                                        rec[k] = v
                                alerts_data: Dict[str, Any] = {}
                                for k, v in r.get("alerts", {}).items():
                                    try:
                                        dk = base64.b64decode(k).decode("utf-8", errors="replace")
                                        dv = base64.b64decode(v).decode("utf-8", errors="replace")
                                        if "risk_score" in dk:
                                            alerts_data[dk] = float(dv)
                                        else:
                                            alerts_data[dk] = dv
                                    except Exception:
                                        alerts_data[k] = v
                                rec["anomaly_alerts"] = alerts_data
                                if cashier_formatted:
                                    if rec.get("cashier_id") == cashier_formatted:
                                        mcp_records.append(rec)
                                else:
                                    mcp_records.append(rec)
                            except Exception:
                                pass
                        if mcp_records:
                            return _format_enriched_response(store_formatted, mcp_records)
    except Exception as e:
        logger.debug("Remote MCP read_pos_transactions_enriched unavailable, using local fallback: %s", e)

    # 2. Local native Bigtable fallback logic
    for attempt in range(1, max_retries + 1):
        try:
            from google.cloud import bigtable
            from google.cloud.bigtable.row_set import RowSet
            from google.cloud.bigtable.row_filters import CellsColumnLimitFilter

            bt_client = bigtable.Client(project=PROJECT_ID, admin=False)
            instance = bt_client.instance(BIGTABLE_INSTANCE_ID)
            table = instance.table(BIGTABLE_ENRICHED_TABLE_ID)

            matching_rows = []
            if transaction_id:
                row_key_str = f"{store_formatted}#{transaction_id}"
                row = table.read_row(row_key_str.encode("utf-8"))
                if row:
                    matching_rows.append(row)
            else:
                prefix_str = f"{store_formatted}#"
                row_set = RowSet()
                row_set.add_row_range_with_prefix(prefix_str)
                filter_ = CellsColumnLimitFilter(1)
                scanned = table.read_rows(row_set=row_set, filter_=filter_, limit=50)
                for r in scanned:
                    if cashier_formatted:
                        c_cell = r.cells.get("tx", {}).get(b"cashier_id")
                        if c_cell and c_cell[0].value.decode("utf-8", errors="replace") != cashier_formatted:
                            continue
                    matching_rows.append(r)
                    if len(matching_rows) >= 5:
                        break

            if not matching_rows:
                target_desc = (
                    f"transaction `{transaction_id}`"
                    if transaction_id
                    else (f"cashier `{cashier_formatted}`" if cashier_formatted else f"store `{store_formatted}`")
                )
                return (
                    f"No enriched transaction records found in Cloud Bigtable `{BIGTABLE_ENRICHED_TABLE_ID}` "
                    f"for {target_desc}."
                )

            parsed_records = []
            for r in matching_rows:
                rec: Dict[str, Any] = {
                    "row_key": r.row_key.decode("utf-8", errors="replace"),
                    "store_id": store_formatted,
                }
                tx_cells = r.cells.get("tx", {})
                for col_bytes, val_list in tx_cells.items():
                    col_name = col_bytes.decode("utf-8", errors="replace")
                    raw_val = val_list[0].value.decode("utf-8", errors="replace")
                    try:
                        if col_name in ["total", "subtotal_amount", "discount", "tax_amount"]:
                            rec[col_name] = float(raw_val)
                        elif col_name in ["item_count", "total_quantity"]:
                            rec[col_name] = int(raw_val)
                        elif col_name in ["manual_discount_flag", "is_contactless"]:
                            rec[col_name] = raw_val.lower() == "true"
                        elif col_name == "items":
                            rec[col_name] = json.loads(raw_val)
                        else:
                            rec[col_name] = raw_val
                    except Exception:
                        rec[col_name] = raw_val

                alert_cells = r.cells.get("alerts", {})
                alerts_data: Dict[str, Any] = {}
                for col_bytes, val_list in alert_cells.items():
                    col_name = col_bytes.decode("utf-8", errors="replace")
                    raw_val = val_list[0].value.decode("utf-8", errors="replace")
                    try:
                        if "risk_score" in col_name:
                            alerts_data[col_name] = float(raw_val)
                        else:
                            alerts_data[col_name] = raw_val
                    except Exception:
                        alerts_data[col_name] = raw_val
                rec["anomaly_alerts"] = alerts_data
                parsed_records.append(rec)

            return _format_enriched_response(store_formatted, parsed_records)

        except Exception as e:
            logger.warning("Error querying Bigtable enriched transactions on attempt %d: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))

    return (
        f"Unable to read enriched transactions for Store {store_formatted} due to a transient "
        f"database connectivity error. Please retry shortly."
    )


class BigtableMcpToolset(BaseToolset):
    """ADK Toolset providing Bigtable real-time operational alerts and declarative MCP Toolbox integration."""

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
        """Returns declarative MCP tools for querying Bigtable real-time alerts and enriched transactions."""
        tools: List[BaseTool] = []
        try:
            self._init_mcp()
            if self._mcp_toolset:
                remote_tools = await self._mcp_toolset.get_tools(readonly_context)
                tools.extend(remote_tools)
        except Exception as e:
            logger.debug("Remote MCP tools retrieval skipped or unavailable: %s", e)

        # Add function tool fallback if not already supplied by the remote MCP server
        existing_names = {t.name for t in tools}
        if "read_cashier_realtime_alerts" not in existing_names:
            tools.append(FunctionTool(func=read_cashier_realtime_alerts))
        if "read_pos_transactions_enriched" not in existing_names:
            tools.append(FunctionTool(func=read_pos_transactions_enriched))

        return tools

    async def close(self) -> None:
        if self._mcp_toolset:
            await self._mcp_toolset.close()


# Primary toolset instance exported for ADK coordinator binding
bigtable_mcp_toolset = BigtableMcpToolset(service_url=BIGTABLE_MCP_SERVICE_URL)

__all__ = [
    "bigtable_mcp_toolset",
    "BigtableMcpToolset",
    "read_cashier_realtime_alerts",
    "read_pos_transactions_enriched",
    "get_oidc_bearer_token",
]
