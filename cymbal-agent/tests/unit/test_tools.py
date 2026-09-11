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

"""Unit tests for Cymbal Operations Agent tools."""

import base64
import json
import struct
from unittest.mock import MagicMock, patch

import pytest
from google.adk.tools.function_tool import FunctionTool

from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.bigtable_tool import (
    _decode_family,
    _normalise_cashier,
    _normalise_store,
    bigtable_mcp_toolset,
    read_cashier_realtime_alerts,
    read_pos_transactions_enriched,
)
from app.tools.rag_tool import (
    OUT_OF_SCOPE_DECLINE_STRING,
    _extract_error_code,
    _format_gcs_link,
    _stitch,
    pos_troubleshooting_rag_tool,
)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("utf-8")


def _vector_row(**overrides):
    """Builds a mock VECTOR_SEARCH result row."""
    row = MagicMock()
    row.document_filename = overrides.get("document_filename", "Toshiba_TCx_810_Guide")
    row.document_title = overrides.get("document_title", "Toshiba TCx 810 POS Hardware Guide")
    row.equipment_covered = overrides.get("equipment_covered", "Toshiba TCx 810")
    row.source_pdf_uri = overrides.get("source_pdf_uri", "gs://cymbal-bucket/toshiba_tcx810.pdf")
    row.center_chunk_index = overrides.get("center_chunk_index", 13)
    row.similarity_score = overrides.get("similarity_score", 0.8800)
    row.exact_error_match = overrides.get("exact_error_match", False)
    row.rank_score = overrides.get("rank_score", row.similarity_score)
    row.context_chunks = overrides.get(
        "context_chunks", [{"idx": 13, "content": "Step 1: Check EMV reader power."}]
    )
    return row


class TestRagTool:
    """Unit tests for POS troubleshooting RAG tool."""

    def test_format_gcs_link(self):
        assert _format_gcs_link(None) == ""
        assert _format_gcs_link("") == ""
        assert (
            _format_gcs_link("gs://cymbal-bucket/pos/guide.pdf")
            == "https://storage.cloud.google.com/cymbal-bucket/pos/guide.pdf"
        )
        assert (
            _format_gcs_link("https://storage.cloud.google.com/doc.pdf")
            == "https://storage.cloud.google.com/doc.pdf"
        )

    def test_extract_error_code(self):
        assert _extract_error_code("cashier hit ERR-PAY-4001 at lane 3") == "ERR-PAY-4001"
        assert _extract_error_code("ERR-DN-PRNT-24V cutter lock") == "ERR-DN-PRNT-24V"
        assert _extract_error_code("the printer is jammed") == ""

    def test_stitch_removes_sliding_window_overlap(self):
        """Adjacent chunks repeat 100 chars; the stitch must not emit them twice."""
        overlap = "X" * 100
        first = "A" * 400 + overlap
        second = overlap + "B" * 400

        stitched = _stitch([(4, first), (5, second)])

        assert stitched == ("A" * 400 + overlap + "B" * 400)
        assert stitched.count(overlap) == 1

    @pytest.mark.asyncio
    @patch("app.tools.rag_tool._run_query")
    async def test_out_of_scope_returns_exact_decline_string(self, mock_query):
        """An out-of-scope inquiry must return the mandatory certified decline string."""
        mock_query.return_value = []  # no vector hits, no full-text hits

        result = await pos_troubleshooting_rag_tool(
            "How do I replace the engine oil on a Ford F-150 truck?"
        )

        assert result == OUT_OF_SCOPE_DECLINE_STRING

    @pytest.mark.asyncio
    @patch("app.tools.rag_tool._run_query")
    async def test_in_scope_hardware_query_returns_certified_runbook(self, mock_query):
        mock_query.return_value = [
            _vector_row(
                similarity_score=0.8800,
                exact_error_match=True,
                context_chunks=[
                    {"idx": 12, "content": "Preceding step."},
                    {"idx": 13, "content": "Step 1: Check EMV reader power."},
                ],
            )
        ]

        result = await pos_troubleshooting_rag_tool(
            "Field recovery protocol for an ERR-PAY-4001 freeze?"
        )

        assert "Toshiba TCx 810" in result
        assert "0.8800" in result
        assert "https://storage.cloud.google.com/cymbal-bucket/toshiba_tcx810.pdf" in result
        assert "Step 1: Check EMV reader power" in result

        # The error code must reach BigQuery as a bound parameter.
        job_config = mock_query.call_args[0][1]
        params = {p.name: p.value for p in job_config.query_parameters}
        assert params["error_code"] == "ERR-PAY-4001"

    @pytest.mark.asyncio
    @patch("app.tools.rag_tool._run_query")
    async def test_reported_score_is_true_cosine_not_boosted(self, mock_query):
        """Regression: the lexical boost must never inflate the score shown to the user.

        A chunk containing the exact error code is admitted even below the 0.70 gate,
        but the number reported must remain the raw cosine similarity.
        """
        mock_query.return_value = [
            _vector_row(similarity_score=0.6920, exact_error_match=True, rank_score=0.8920)
        ]

        result = await pos_troubleshooting_rag_tool("ERR-PAY-4001 EMV freeze")

        assert "0.6920" in result, "must report the true cosine similarity"
        assert "0.8920" not in result, "must not report the lexically boosted rank score"
        assert "exact error-code match" in result

    @pytest.mark.asyncio
    @patch("app.tools.rag_tool._run_query")
    async def test_below_threshold_without_error_code_falls_back_to_full_text(self, mock_query):
        """A weak semantic match with no error code must go through SEARCH(), not pass the gate."""
        weak = _vector_row(similarity_score=0.4100, exact_error_match=False)
        fallback = MagicMock()
        fallback.document_filename = "Clover_Station_Solo_Guide"
        fallback.document_title = "Clover Station Solo Guide"
        fallback.equipment_covered = "Clover Station Solo"
        fallback.source_pdf_uri = "gs://cymbal-bucket/clover.pdf"
        fallback.chunk_content = "Reseat the receipt spindle."

        mock_query.side_effect = [[weak], [fallback]]

        result = await pos_troubleshooting_rag_tool("the paper feed feels stiff")

        assert mock_query.call_count == 2, "expected the full-text fallback query to run"
        assert "Full-Text Search Fallback" in result
        assert "Reseat the receipt spindle" in result
        # No fabricated numeric relevance score for a keyword match.
        assert "Relevance Score" not in result

    @pytest.mark.asyncio
    @patch("app.tools.rag_tool._run_query")
    async def test_cost_guardrail_applied_to_every_query(self, mock_query):
        mock_query.return_value = []

        await pos_troubleshooting_rag_tool("ERR-PAY-4001")

        for call in mock_query.call_args_list:
            assert call[0][1].maximum_bytes_billed > 0


class TestAnalyticsTool:
    """Unit tests for Cymbal Conversational Analytics tool."""

    @pytest.mark.asyncio
    @patch("app.tools.analytics_tool.ask_data_agent")
    async def test_analytics_success_response(self, mock_ask):
        mock_ask.return_value = {
            "status": "SUCCESS",
            "response": [
                {
                    "text": {
                        "textType": "FINAL_RESPONSE",
                        "parts": ["Total on-hand inventory across stores with < 20h cover is 4,120 units."],
                    }
                }
            ],
        }

        result = await cymbal_analytics_tool(
            "What is the total on-hand inventory for stockout risks < 20 hours?",
            MagicMock(),
        )
        assert "4,120 units" in result

    @pytest.mark.asyncio
    @patch("app.tools.analytics_tool.ask_data_agent")
    async def test_business_glossary_terms_passed_verbatim(self, mock_ask):
        """Standardised enterprise terms must reach the Data Agent unmodified."""
        mock_ask.return_value = {
            "status": "SUCCESS",
            "response": [{"text": {"textType": "FINAL_RESPONSE", "parts": ["ok"]}}],
        }
        query = (
            "Compare Net Transaction Revenue against Total On-Hand Inventory and the "
            "Cashier Manual Override Rate where Estimated Cover Hours < 20."
        )

        await cymbal_analytics_tool(query, MagicMock())

        assert mock_ask.call_args.kwargs["query"] == query

    @pytest.mark.asyncio
    @patch("app.tools.analytics_tool.ask_data_agent")
    async def test_analytics_persistent_failure_contract(self, mock_ask):
        """Persistent failure must return the specified JSON payload contract."""
        mock_ask.side_effect = Exception("Service unavailable")

        result = await cymbal_analytics_tool("What is the Net Transaction Revenue?", MagicMock())

        parsed = json.loads(result)
        assert parsed["status"] == "ERROR"
        assert "Store analytics data is currently unreachable" in parsed["error"]
        assert parsed["data"] is None


class TestBigtableMcpTool:
    """Unit tests for the Bigtable MCP toolset and its cell decoding."""

    def test_row_key_normalisation(self):
        assert _normalise_store("48") == "STORE_048"
        assert _normalise_store("048") == "STORE_048"
        assert _normalise_store("STORE_048") == "STORE_048"
        assert _normalise_cashier("1190") == "CASH_1190"
        assert _normalise_cashier("CASH_1190") == "CASH_1190"

    def test_decode_family_unpacks_packed_numerics(self):
        """Bigtable returns 8-byte big-endian doubles/int64s wrapped in base64."""
        family = {
            _b64(b"cashier_1h_txn_count"): _b64(struct.pack(">q", 28)),
            _b64(b"cashier_1h_total_discount_usd"): _b64(struct.pack(">d", 5293.57)),
        }

        decoded = _decode_family(family, numeric=True)

        assert decoded["cashier_1h_txn_count"] == 28
        assert decoded["cashier_1h_total_discount_usd"] == 5293.57

    @pytest.mark.asyncio
    async def test_agent_facing_tools_are_decoding_wrappers(self):
        """Regression: the agent must never be handed the raw MCP tools.

        The MCP Toolbox exposes tools with these same names, but they return
        base64-wrapped, struct-packed Bigtable cells that a model cannot interpret.
        The toolset must therefore surface the typed Python wrappers instead.
        """
        tools = await bigtable_mcp_toolset.get_tools()

        assert {t.name for t in tools} == {
            "read_cashier_realtime_alerts",
            "read_pos_transactions_enriched",
        }
        assert all(isinstance(t, FunctionTool) for t in tools), (
            "expected decoding FunctionTool wrappers, not raw MCPTool instances"
        )

    @pytest.mark.asyncio
    @patch("app.tools.bigtable_tool._call_mcp_tool")
    async def test_read_cashier_realtime_alerts_decodes_payload(self, mock_call):
        mock_call.return_value = [
            {
                "_key": _b64(b"STORE_048#CASH_1190#9221583"),
                "flags": {_b64(b"audit_status"): _b64(b"clear")},
                "stats": {
                    _b64(b"cashier_1h_txn_count"): _b64(struct.pack(">q", 25)),
                    _b64(b"cashier_1h_manual_override_count"): _b64(struct.pack(">q", 5)),
                    _b64(b"cashier_1h_override_rate"): _b64(struct.pack(">d", 0.20)),
                },
            }
        ]

        result = await read_cashier_realtime_alerts("48", "1190", MagicMock())

        assert "CASH_1190 (STORE_048)" in result
        assert "CLEAR" in result
        assert "20.00%" in result
        assert "5 overrides across 25 transactions" in result
        # The model must not be shown raw base64.
        assert "Y2FzaGll" not in result

        # Agent-friendly args must be normalised into a row-key prefix for the MCP tool.
        assert mock_call.call_args[0][1] == {"prefix": "STORE_048#CASH_1190%"}

    @pytest.mark.asyncio
    @patch("app.tools.bigtable_tool._call_mcp_tool")
    async def test_read_cashier_realtime_alerts_not_found(self, mock_call):
        mock_call.return_value = []

        result = await read_cashier_realtime_alerts("99", "9999", MagicMock())

        assert "No real-time alert records found for Cashier CASH_9999 at STORE_099" in result

    @pytest.mark.asyncio
    @patch("app.tools.bigtable_tool._call_mcp_tool")
    async def test_read_cashier_realtime_alerts_connectivity_fallback(self, mock_call):
        """Persistent transport failure must degrade to a user-friendly message."""
        mock_call.side_effect = Exception("Connection refused")

        result = await read_cashier_realtime_alerts("48", "1190", MagicMock())

        assert "transient" in result.lower()
        assert mock_call.call_count == 3, "expected 3 attempts with exponential backoff"

    @pytest.mark.asyncio
    @patch("app.tools.bigtable_tool._call_mcp_tool")
    async def test_read_pos_transactions_enriched_decodes_payload(self, mock_call):
        mock_call.return_value = [
            {
                "_key": _b64(b"STORE_001#TXN-20260910-0000727"),
                "tx": {
                    _b64(b"total"): _b64(b"383.93"),
                    _b64(b"cashier_id"): _b64(b"CASH_1002"),
                },
                "alerts": {},
            }
        ]

        result = await read_pos_transactions_enriched(
            "1", MagicMock(), transaction_id="TXN-20260910-0000727"
        )

        assert "Cloud Bigtable Enriched Transactions" in result
        assert "STORE_001" in result
        assert "CASH_1002" in result
        assert "$383.93" in result

    @pytest.mark.asyncio
    @patch("app.tools.bigtable_tool._call_mcp_tool")
    async def test_read_pos_transactions_enriched_filters_by_cashier(self, mock_call):
        mock_call.return_value = [
            {
                "_key": _b64(b"STORE_048#TXN-1"),
                "tx": {_b64(b"cashier_id"): _b64(b"CASH_1190"), _b64(b"total"): _b64(b"59.38")},
                "alerts": {},
            },
            {
                "_key": _b64(b"STORE_048#TXN-2"),
                "tx": {_b64(b"cashier_id"): _b64(b"CASH_2000"), _b64(b"total"): _b64(b"12.00")},
                "alerts": {},
            },
        ]

        result = await read_pos_transactions_enriched("48", MagicMock(), cashier_id="1190")

        assert "Matched Records:** 1" in result
        assert "CASH_1190" in result
        assert "CASH_2000" not in result
