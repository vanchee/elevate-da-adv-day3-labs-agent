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
from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.bigtable_tool import (
    get_oidc_bearer_token,
    read_cashier_realtime_alerts,
    read_pos_transactions_enriched,
)
from app.tools.rag_tool import (
    OUT_OF_SCOPE_DECLINE_STRING,
    _format_gcs_link,
    pos_troubleshooting_rag_tool,
)


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

    @patch("google.cloud.bigquery.Client")
    def test_out_of_scope_returns_exact_decline_string(self, mock_bq_client):
        """Verifies that an out-of-scope inquiry returns the mandatory certified decline string."""
        mock_instance = MagicMock()
        mock_bq_client.return_value = mock_instance
        # Both vector query and fallback text query return empty
        mock_instance.query.return_value.result.return_value = []

        result = pos_troubleshooting_rag_tool("How do I replace the engine oil on a Ford F-150 truck?")
        assert result == OUT_OF_SCOPE_DECLINE_STRING
        assert result == "I cannot find certified warranty or repair rules for this specific error in our technical repository."

    @patch("google.cloud.bigquery.Client")
    def test_in_scope_hardware_query_with_boosting(self, mock_bq_client):
        """Verifies that in-scope error code query retrieves certified runbook with boosted score."""
        mock_instance = MagicMock()
        mock_bq_client.return_value = mock_instance

        mock_row = MagicMock()
        mock_row.document_filename = "Toshiba_TCx_810_Guide"
        mock_row.document_title = "Toshiba TCx 810 POS Hardware Guide"
        mock_row.equipment_covered = "Toshiba TCx 810"
        mock_row.source_pdf_uri = "gs://cymbal-bucket/toshiba_tcx810.pdf"
        mock_row.similarity_score = 0.8800
        mock_row.stitched_content = "Step 1: Check EMV reader power. Step 2: Clear cache."

        mock_instance.query.return_value.result.return_value = [mock_row]

        result = pos_troubleshooting_rag_tool(
            "What is the immediate field recovery protocol when a cashier encounters an ERR-PAY-4001 freeze?"
        )
        assert "Toshiba TCx 810" in result
        assert "0.8800" in result
        assert "https://storage.cloud.google.com/cymbal-bucket/toshiba_tcx810.pdf" in result
        assert "Step 1: Check EMV reader power" in result

        # Verify error_code was extracted and passed to query parameters
        call_kwargs = mock_instance.query.call_args[1]
        job_config = call_kwargs["job_config"]
        param_dict = {p.name: p.value for p in job_config.query_parameters}
        assert param_dict.get("error_code") == "ERR-PAY-4001"


class TestAnalyticsTool:
    """Unit tests for Cymbal Conversational Analytics tool."""

    @patch("app.tools.analytics_tool.ask_data_agent")
    def test_analytics_success_response(self, mock_ask):
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

        result = cymbal_analytics_tool("What is the total on-hand inventory for stockout risks < 20 hours?")
        assert "4,120 units" in result

    @patch("app.tools.analytics_tool.ask_data_agent")
    def test_analytics_persistent_failure_contract(self, mock_ask):
        """Verifies that persistent failure returns the specified JSON payload contract."""
        mock_ask.side_effect = Exception("Service unavailable")

        result = cymbal_analytics_tool("What is the Net Transaction Revenue?")
        parsed = json.loads(result)
        assert parsed.get("status") == "ERROR"
        assert "Store analytics data is currently unreachable" in parsed.get("error", "")
        assert parsed.get("data") is None


class TestBigtableMcpTool:
    """Unit tests for Bigtable MCP toolset and realtime alerts query."""

    @patch("httpx.Client")
    @patch("app.tools.bigtable_tool.get_oidc_bearer_token", return_value="mock-token-123")
    def test_read_cashier_realtime_alerts_success(self, mock_token, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # Construct simulated base64 Bigtable row from MCP service
        b64_key = base64.b64encode(b"STORE_048#CASH_1190#9221583").decode()
        b64_audit_col = base64.b64encode(b"audit_status").decode()
        b64_audit_val = base64.b64encode(b"clear").decode()
        b64_txn_col = base64.b64encode(b"cashier_1h_txn_count").decode()
        b64_txn_val = base64.b64encode(struct.pack(">q", 25)).decode()
        b64_override_col = base64.b64encode(b"cashier_1h_manual_override_count").decode()
        b64_override_val = base64.b64encode(struct.pack(">q", 5)).decode()
        b64_override_rate_col = base64.b64encode(b"cashier_1h_override_rate").decode()
        b64_override_rate_val = base64.b64encode(struct.pack(">d", 0.20)).decode()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "_key": b64_key,
                            "flags": {b64_audit_col: b64_audit_val},
                            "stats": {
                                b64_txn_col: b64_txn_val,
                                b64_override_col: b64_override_val,
                                b64_override_rate_col: b64_override_rate_val,
                            },
                        }),
                    }
                ]
            }
        }
        mock_client.post.return_value = mock_resp

        result = read_cashier_realtime_alerts("48", "1190")
        assert "CASH_1190 (STORE_048)" in result
        assert "CLEAR" in result
        assert "20.00%" in result
        assert "5 overrides across 25 transactions" in result

    @patch("httpx.Client")
    @patch("app.tools.bigtable_tool.get_oidc_bearer_token", return_value="mock-token-123")
    def test_read_cashier_realtime_alerts_not_found(self, mock_token, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"content": []}}
        mock_client.post.return_value = mock_resp

        result = read_cashier_realtime_alerts("99", "9999")
        assert "No real-time alert records found for Cashier CASH_9999 at STORE_099" in result

    @patch("httpx.Client")
    @patch("app.tools.bigtable_tool.get_oidc_bearer_token", return_value="mock-token-123")
    def test_read_pos_transactions_enriched_remote_mcp(self, mock_token, mock_client_cls):
        """Verifies remote declarative MCP lookup and base64 parsing for enriched transactions."""
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        b64_key = base64.b64encode(b"STORE_001#TXN-20260910-0000727").decode("utf-8")
        b64_total_col = base64.b64encode(b"total").decode("utf-8")
        b64_total_val = base64.b64encode(b"383.93").decode("utf-8")
        b64_cashier_col = base64.b64encode(b"cashier_id").decode("utf-8")
        b64_cashier_val = base64.b64encode(b"CASH_1002").decode("utf-8")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "_key": b64_key,
                            "tx": {
                                b64_total_col: b64_total_val,
                                b64_cashier_col: b64_cashier_val,
                            },
                            "alerts": {},
                        }),
                    }
                ]
            }
        }
        mock_client.post.return_value = mock_resp

        result = read_pos_transactions_enriched("1", transaction_id="TXN-20260910-0000727")
        assert "Cloud Bigtable Enriched Transactions" in result
        assert "STORE_001" in result
        assert "CASH_1002" in result
        assert "$383.93" in result

    @patch("httpx.Client")
    @patch("app.tools.bigtable_tool.get_oidc_bearer_token", return_value="mock-token-123")
    @patch("google.cloud.bigtable.Client")
    def test_read_pos_transactions_enriched_local_fallback(
        self, mock_bt_client_cls, mock_token, mock_http_client_cls
    ):
        """Verifies local native Bigtable fallback when remote MCP fails."""
        # Force remote MCP failure
        mock_http_client = MagicMock()
        mock_http_client_cls.return_value.__enter__.return_value = mock_http_client
        mock_http_client.post.side_effect = Exception("Connection refused")

        # Mock Bigtable native client
        mock_bt_client = MagicMock()
        mock_bt_client_cls.return_value = mock_bt_client
        mock_table = MagicMock()
        mock_bt_client.instance.return_value.table.return_value = mock_table

        mock_row = MagicMock()
        mock_row.row_key = b"STORE_048#TXN-20260906-0220917"
        mock_cell_cashier = MagicMock()
        mock_cell_cashier.value = b"CASH_1190"
        mock_cell_total = MagicMock()
        mock_cell_total.value = b"59.38"

        mock_row.cells = {
            "tx": {
                b"cashier_id": [mock_cell_cashier],
                b"total": [mock_cell_total],
            },
            "alerts": {},
        }
        mock_table.read_row.return_value = mock_row

        result = read_pos_transactions_enriched("48", transaction_id="TXN-20260906-0220917")
        assert "Cloud Bigtable Enriched Transactions" in result
        assert "STORE_048" in result
        assert "CASH_1190" in result
        assert "$59.38" in result
