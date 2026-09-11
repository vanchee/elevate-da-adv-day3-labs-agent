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

"""Bigtable MCP Toolset integrating Cloud Run MCP Toolbox and Cloud Bigtable.

The remote MCP Toolbox returns Bigtable cells exactly as they are stored: base64
wrappers around raw bytes, where numeric columns are big-endian IEEE-754 doubles or
int64s. A language model cannot decode those reliably, so this module exposes typed
Python wrappers as the agent-facing tools. The wrappers still use ADK's `McpToolset`
as the transport - they simply own argument normalisation ("Store 48" -> STORE_048)
and cell decoding before the payload reaches the model.
"""

import asyncio
import base64
import functools
import json
import logging
import os
import re
import struct
import threading
import time
from typing import Any, Optional

import google.auth
import google.oauth2.id_token
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import McpToolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.tool_context import ToolContext
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request

from app import config

logger = logging.getLogger(__name__)

# ID tokens are valid for 1 hour; refresh a little early to avoid edge-of-expiry 401s.
_TOKEN_TTL_SECONDS = 55 * 60

# Columns whose packed bytes represent floating point rather than integer values.
_FLOAT_HINTS = ("rate", "pct", "usd", "score", "ratio")

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _resolve_target_service_account() -> Optional[str]:
    """Resolves the service account to impersonate when minting OIDC ID tokens.

    Prefers an explicit BIGTABLE_MCP_SERVICE_ACCOUNT. Otherwise derives the project's
    Compute Engine default service account, which is the identity Cloud Run runs the
    MCP Toolbox as. Cached because the Resource Manager lookup is a network round-trip
    and the answer never changes for a given process.
    """
    explicit = os.environ.get("BIGTABLE_MCP_SERVICE_ACCOUNT")
    if explicit:
        return explicit

    try:
        creds, project = google.auth.default()
        if not project:
            return None
        from google.cloud import resourcemanager_v3

        crm_client = resourcemanager_v3.ProjectsClient(credentials=creds)
        proj = crm_client.get_project(name=f"projects/{project}")
        project_number = proj.name.split("/")[-1]
        return f"{project_number}-compute@developer.gserviceaccount.com"
    except Exception as e:
        logger.debug("Could not derive the compute default service account: %s", e)
        return None


def _mint_oidc_token(audience: str) -> Optional[str]:
    """Mints a GCP OIDC ID token for the target Cloud Run service audience."""
    auth_req = Request()

    # Preferred path: impersonate the Cloud Run runtime service account. Required on
    # Cloudtop / local workstations, where ADC is a user credential and therefore
    # cannot mint an ID token for an arbitrary audience directly.
    target_sa = _resolve_target_service_account()
    if target_sa:
        try:
            creds, _ = google.auth.default()
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
            logger.debug("Impersonated ID token generation failed for %s: %s", target_sa, e)

    # Standard path: metadata server or service account key (Cloud Run, GCE, Agent Engine).
    try:
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        if token:
            return token
    except Exception as e:
        logger.warning("Could not fetch native OIDC identity token: %s", e)

    return None


def get_oidc_bearer_token(audience: str) -> Optional[str]:
    """Returns a cached OIDC ID token for the audience, minting a new one when stale.

    Minting involves an impersonation round-trip that costs several seconds, so the
    token is cached until shortly before it expires rather than fetched per call.
    """
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(audience)
        if cached and cached[1] > now:
            return cached[0]

    token = _mint_oidc_token(audience)

    if token:
        with _token_lock:
            _token_cache[audience] = (token, now + _TOKEN_TTL_SECONDS)
    return token


# --------------------------------------------------------------------------------------
# Bigtable cell decoding
# --------------------------------------------------------------------------------------


def _b64_text(value: Any) -> str:
    """Decodes a base64-wrapped Bigtable cell into text."""
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except Exception:
        return str(value)


def _b64_number(column: str, value: Any) -> Any:
    """Decodes a base64-wrapped Bigtable cell that may hold packed binary numerics.

    The MCP Toolbox returns raw cell bytes. Bigtable increment/aggregate columns store
    8-byte big-endian int64 or IEEE-754 doubles, so the width and the column name
    together determine how to unpack them.
    """
    try:
        raw = base64.b64decode(value)
    except Exception:
        return value

    if len(raw) == 8:
        try:
            if any(hint in column for hint in _FLOAT_HINTS):
                return round(struct.unpack(">d", raw)[0], 4)
            return struct.unpack(">q", raw)[0]
        except Exception:
            pass
    elif len(raw) == 4:
        try:
            return struct.unpack(">i", raw)[0]
        except Exception:
            pass

    return raw.decode("utf-8", errors="replace")


def _decode_family(family: dict[str, Any], numeric: bool) -> dict[str, Any]:
    """Decodes every cell in one Bigtable column family."""
    decoded: dict[str, Any] = {}
    for column, value in (family or {}).items():
        name = _b64_text(column)
        decoded[name] = _b64_number(name, value) if numeric else _b64_text(value)
    return decoded


def _normalise_store(store_id: str) -> str:
    """Normalises '48', '048' or 'STORE_048' into the STORE_048 row-key convention."""
    digits = re.sub(r"[^0-9]", "", str(store_id))
    if digits:
        return f"STORE_{int(digits):03d}"
    value = str(store_id).strip()
    return value if value.startswith("STORE_") else f"STORE_{value}"


def _normalise_cashier(cashier_id: str) -> str:
    """Normalises '1190' or 'CASH_1190' into the CASH_1190 row-key convention."""
    digits = re.sub(r"[^0-9]", "", str(cashier_id))
    if digits:
        return f"CASH_{int(digits):04d}"
    value = str(cashier_id).strip()
    return value if value.startswith("CASH_") else f"CASH_{value}"


# --------------------------------------------------------------------------------------
# MCP transport
# --------------------------------------------------------------------------------------


async def _call_mcp_tool(
    tool_name: str, arguments: dict[str, Any], tool_context: ToolContext
) -> list[dict[str, Any]]:
    """Invokes a tool on the remote MCP Toolbox and returns its raw JSON rows.

    Rows come back still base64-encoded; decoding is the caller's responsibility.
    """
    tool = await bigtable_mcp_toolset.get_remote_tool(tool_name)
    if tool is None:
        raise RuntimeError(f"Remote MCP tool '{tool_name}' is unavailable")

    raw = await tool.run_async(args=arguments, tool_context=tool_context)

    content = raw.get("content", []) if isinstance(raw, dict) else []
    rows: list[dict[str, Any]] = []
    for item in content:
        if item.get("type") != "text":
            continue
        text = item.get("text", "").strip()
        if not text or text == "[]":
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        rows.extend(parsed if isinstance(parsed, list) else [parsed])
    return rows


async def _call_with_retries(
    tool_name: str, arguments: dict[str, Any], tool_context: ToolContext
) -> list[dict[str, Any]]:
    """Calls an MCP tool with exponential backoff on transient failures."""
    last_error: Optional[Exception] = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return await _call_mcp_tool(tool_name, arguments, tool_context)
        except Exception as e:
            last_error = e
            logger.warning("MCP call '%s' failed on attempt %d: %s", tool_name, attempt, e)
            if attempt < config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    raise RuntimeError(f"MCP call '{tool_name}' failed after {config.MAX_RETRIES} attempts") from last_error


# --------------------------------------------------------------------------------------
# Agent-facing tools
# --------------------------------------------------------------------------------------


async def read_cashier_realtime_alerts(
    store_id: str, cashier_id: str, tool_context: ToolContext
) -> str:
    """Reads live 1-hour rolling metrics and audit status flags for a cashier from Cloud Bigtable.

    Invokes the remote declarative MCP 'bigtable-sql' microservice to query the real-time
    operational alerts table (cashier_realtime_alerts) using GoogleSQL with row key prefix
    STORE_xxx#CASH_yyyy. Retrieves live 1-hour manual override rates, transaction counts,
    promo usage, discount amounts, and current audit flags ('clear', 'review', 'investigate').

    Args:
        store_id: Store number or ID (e.g., '48', '048', or 'STORE_048').
        cashier_id: Cashier ID (e.g., '1190', 'CASH_1190').

    Returns:
        Formatted summary containing the latest live 1-hour metrics and audit status flags.
    """
    store = _normalise_store(store_id)
    cashier = _normalise_cashier(cashier_id)
    prefix = f"{store}#{cashier}%"

    logger.info("Querying Bigtable realtime alerts with prefix: %s", prefix)

    try:
        rows = await _call_with_retries(
            "read_cashier_realtime_alerts", {"prefix": prefix}, tool_context
        )
    except Exception as e:
        logger.error("Bigtable realtime alerts lookup failed for %s: %s", prefix, e)
        return (
            f"Unable to read live metrics for Cashier {cashier} at {store} due to a transient "
            f"database connectivity error. Please retry shortly."
        )

    if not rows:
        return (
            f"No real-time alert records found for Cashier {cashier} at {store} "
            f"(row prefix: `{prefix}`). The cashier may not have recorded transactions "
            f"in the current 1-hour window."
        )

    # Row keys embed a reversed timestamp, so the key-ascending order returned by the
    # MCP query puts the most recent bucket first.
    latest = rows[0]
    row_key = _b64_text(latest.get("_key", "")) or prefix
    flags = _decode_family(latest.get("flags", {}), numeric=False)
    stats = _decode_family(latest.get("stats", {}), numeric=True)

    txn_count = stats.get("cashier_1h_txn_count", 0) or 0
    override_count = stats.get("cashier_1h_manual_override_count", 0) or 0
    override_rate = stats.get("cashier_1h_override_rate")
    if override_rate is None:
        override_rate = round(override_count / txn_count, 4) if txn_count else 0.0

    summary = {
        "store": store,
        "cashier": cashier,
        "latest_row_key": row_key,
        "audit_status": flags.get("audit_status", "UNKNOWN"),
        "audit_trigger_reason": flags.get("audit_trigger_reason", "None"),
        "live_1h_manual_override_count": override_count,
        "live_1h_txn_count": txn_count,
        "live_1h_override_rate": override_rate,
        "live_1h_promo_rate": stats.get("cashier_1h_promo_rate", 0.0),
        "live_1h_avg_discount_pct": stats.get("cashier_1h_avg_discount_pct", 0.0),
        "live_1h_total_discount_usd": stats.get("cashier_1h_total_discount_usd", 0.0),
        "risk_score": stats.get("risk_score", 0.0),
        "all_flags": flags,
        "all_stats": stats,
    }

    return (
        f"### Real-Time Cashier Metrics: {cashier} ({store})\n"
        f"- **Bigtable Instance / Table:** `{config.BIGTABLE_INSTANCE_ID}` / `{config.BIGTABLE_TABLE_ID}`\n"
        f"- **Row Key:** `{row_key}`\n"
        f"- **Audit Status:** `{str(summary['audit_status']).upper()}`\n"
        f"- **Live 1-Hour Manual Override Rate:** `{float(override_rate) * 100:.2f}%` "
        f"({override_count} overrides across {txn_count} transactions)\n"
        f"- **Live 1-Hour Promo Rate:** `{float(summary['live_1h_promo_rate']) * 100:.2f}%`\n"
        f"- **Average Discount:** `{float(summary['live_1h_avg_discount_pct']):.2f}%` "
        f"(Total: `${float(summary['live_1h_total_discount_usd']):,.2f}`)\n"
        f"- **Anomaly Risk Score:** `{summary['risk_score']}`\n"
        f"\n```json\n{json.dumps(summary, indent=2)}\n```"
    )


def _decode_transaction(raw: dict[str, Any], store: str, fallback_key: str) -> dict[str, Any]:
    """Decodes one enriched POS transaction row from the MCP payload."""
    record: dict[str, Any] = {
        "row_key": _b64_text(raw.get("_key", "")) or fallback_key,
        "store_id": store,
    }

    for column, value in (raw.get("tx", {}) or {}).items():
        name = _b64_text(column)
        text = _b64_text(value)
        try:
            if name in ("total", "subtotal_amount", "discount", "tax_amount"):
                record[name] = float(text)
            elif name in ("item_count", "total_quantity"):
                record[name] = int(text)
            elif name in ("manual_discount_flag", "is_contactless"):
                record[name] = text.lower() == "true"
            elif name == "items":
                record[name] = json.loads(text)
            else:
                record[name] = text
        except (ValueError, json.JSONDecodeError):
            record[name] = text

    alerts: dict[str, Any] = {}
    for column, value in (raw.get("alerts", {}) or {}).items():
        name = _b64_text(column)
        text = _b64_text(value)
        if "risk_score" in name:
            try:
                alerts[name] = float(text)
                continue
            except ValueError:
                pass
        alerts[name] = text
    record["anomaly_alerts"] = alerts

    return record


def _format_enriched_response(store: str, records: list[dict[str, Any]]) -> str:
    """Renders decoded enriched transactions for the coordinator agent."""
    lines = [
        f"### Cloud Bigtable Enriched Transactions: `{config.BIGTABLE_ENRICHED_TABLE_ID}`",
        f"- **Target Store:** `{store}`",
        f"- **Matched Records:** {len(records)}",
    ]
    for idx, rec in enumerate(records, 1):
        lines.append(
            f"\n#### Transaction #{idx}: `{rec.get('transaction_id', rec['row_key'])}`\n"
            f"- **Cashier:** `{rec.get('cashier_id', 'N/A')}` | **POS Terminal:** `{rec.get('pos_terminal_id', 'N/A')}`\n"
            f"- **Timestamp:** `{rec.get('event_timestamp', 'N/A')}`\n"
            f"- **Total:** `${rec.get('total', 0.0):.2f}` (Subtotal: `${rec.get('subtotal_amount', 0.0):.2f}`, "
            f"Discount: `${rec.get('discount', 0.0):.2f}`)\n"
            f"- **Promo Code:** `{rec.get('promo_code_applied', 'NONE')}` | "
            f"**Manual Override:** `{rec.get('manual_discount_flag', False)}`\n"
            f"- **Anomaly Alerts:** `{json.dumps(rec.get('anomaly_alerts', {}))}`"
        )
    lines.append(f"\n```json\n{json.dumps(records, indent=2)}\n```")
    return "\n".join(lines)


async def read_pos_transactions_enriched(
    store_id: str,
    tool_context: ToolContext,
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
    store = _normalise_store(store_id)
    cashier = _normalise_cashier(cashier_id) if cashier_id else None
    prefix = f"{store}#{transaction_id}" if transaction_id else f"{store}#TXN-%"

    try:
        rows = await _call_with_retries(
            "read_pos_transactions_enriched", {"prefix": prefix}, tool_context
        )
    except Exception as e:
        logger.error("Bigtable enriched transaction lookup failed for %s: %s", prefix, e)
        return (
            f"Unable to read enriched transactions for {store} due to a transient "
            f"database connectivity error. Please retry shortly."
        )

    records = [_decode_transaction(row, store, prefix) for row in rows]
    if cashier:
        records = [r for r in records if r.get("cashier_id") == cashier]

    if not records:
        if transaction_id:
            target = f"transaction `{transaction_id}`"
        elif cashier:
            target = f"cashier `{cashier}`"
        else:
            target = f"store `{store}`"
        return (
            f"No enriched transaction records found in Cloud Bigtable "
            f"`{config.BIGTABLE_ENRICHED_TABLE_ID}` for {target}."
        )

    return _format_enriched_response(store, records[:5])


# --------------------------------------------------------------------------------------
# Toolset
# --------------------------------------------------------------------------------------


class BigtableMcpToolset(BaseToolset):
    """ADK Toolset exposing Cloud Bigtable operational data via the Cloud Run MCP Toolbox.

    ADK's `McpToolset` provides the transport and session management. The tools handed to
    the agent are typed Python wrappers rather than the raw remote tools, because the
    MCP Toolbox returns base64-wrapped, struct-packed Bigtable cells that a model cannot
    interpret. The wrappers also let the model pass natural identifiers like "Store 48"
    instead of hand-building `STORE_048#CASH_1190%` row-key prefixes.
    """

    def __init__(self, service_url: Optional[str] = None):
        super().__init__()
        self._service_url = service_url
        self._mcp_toolset: Optional[McpToolset] = None
        self._remote_tools: Optional[dict[str, BaseTool]] = None
        self._lock = asyncio.Lock()

    @property
    def service_url(self) -> str:
        return self._service_url or config.get_bigtable_mcp_service_url()

    def _build_mcp_toolset(self) -> McpToolset:
        """Creates the MCP toolset with a freshly minted (cached) OIDC bearer token."""
        token = get_oidc_bearer_token(self.service_url)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=f"{self.service_url}/mcp",
                headers=headers,
            )
        )

    async def get_remote_tool(self, name: str) -> Optional[BaseTool]:
        """Returns a remote MCP tool by name, establishing the session on first use.

        The bearer token is re-read from the cache each time the session is rebuilt, so a
        long-lived agent process picks up a refreshed token instead of reusing the one
        captured at startup.
        """
        async with self._lock:
            if self._remote_tools is None:
                self._mcp_toolset = self._build_mcp_toolset()
                tools = await self._mcp_toolset.get_tools()
                self._remote_tools = {t.name: t for t in tools}
                logger.info("Connected to MCP Toolbox; remote tools: %s", list(self._remote_tools))
            return self._remote_tools.get(name)

    async def get_tools(self, readonly_context: Optional[ReadonlyContext] = None) -> list[BaseTool]:
        """Returns the decoding wrappers that the coordinator agent binds to."""
        return [
            FunctionTool(func=read_cashier_realtime_alerts),
            FunctionTool(func=read_pos_transactions_enriched),
        ]

    async def close(self) -> None:
        async with self._lock:
            if self._mcp_toolset:
                await self._mcp_toolset.close()
                self._mcp_toolset = None
            self._remote_tools = None


# Primary toolset instance exported for ADK coordinator binding
bigtable_mcp_toolset = BigtableMcpToolset()

__all__ = [
    "bigtable_mcp_toolset",
    "BigtableMcpToolset",
    "read_cashier_realtime_alerts",
    "read_pos_transactions_enriched",
    "get_oidc_bearer_token",
]
