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

"""Centralised runtime configuration for the Cymbal Operations Coordinator Agent.

All tool modules resolve their GCP settings through this module so that project and
service identifiers are discovered once, consistently, and without any personal
project ID baked into the source tree.
"""

import functools
import logging
import os

import google.auth

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing."""


@functools.lru_cache(maxsize=1)
def get_project_id() -> str:
    """Resolves the active GCP project from the environment or ADC.

    Resolution order is PROJECT_ID, GOOGLE_CLOUD_PROJECT, then the project associated
    with Application Default Credentials. Raises rather than falling back to a
    hardcoded project so misconfiguration surfaces immediately instead of silently
    querying someone else's data.
    """
    for env_var in ("PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
        value = os.environ.get(env_var)
        if value:
            return value

    try:
        _, project = google.auth.default()
        if project:
            return project
    except Exception as e:  # pragma: no cover - depends on ambient credentials
        logger.debug("Could not resolve project from ADC: %s", e)

    raise ConfigError(
        "Unable to resolve the GCP project. Set PROJECT_ID (or GOOGLE_CLOUD_PROJECT) "
        "in your .env, or run `gcloud auth application-default login`."
    )


def get_region() -> str:
    """Returns the GCP region used for regional services (Cloud Run, BigQuery jobs)."""
    return os.environ.get("REGION") or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")


def get_data_agent_resource() -> str:
    """Returns the fully-qualified BigQuery Conversational Data Agent resource name.

    The agent is created in the `global` location on purpose: regional endpoints route
    through a different host and trigger mTLS/certificate failures from ADK.
    """
    explicit = os.environ.get("DATA_AGENT_RESOURCE")
    if explicit:
        return explicit
    agent_id = os.environ.get("DATA_AGENT_ID")
    if not agent_id:
        raise ConfigError(
            "Set DATA_AGENT_RESOURCE or DATA_AGENT_ID in your .env to target your "
            "published BigQuery Conversational Data Agent."
        )
    return f"projects/{get_project_id()}/locations/global/dataAgents/{agent_id}"


def get_bigtable_mcp_service_url() -> str:
    """Returns the Cloud Run URL of the deployed MCP Toolbox microservice."""
    url = os.environ.get("BIGTABLE_MCP_SERVICE_URL")
    if not url:
        raise ConfigError(
            "Set BIGTABLE_MCP_SERVICE_URL in your .env to the Cloud Run URL of your "
            "deployed `mcp-toolbox-bigtable` service."
        )
    return url.rstrip("/")


# Dataset / table identifiers.
GOLD_DATASET_ID = os.environ.get("GOLD_DATASET_ID", "cymbal_gold")
POS_CHUNK_TABLE_NAME = os.environ.get("POS_CHUNK_TABLE_NAME", "pos_manual_chunk_embeddings")

BIGTABLE_INSTANCE_ID = os.environ.get("BIGTABLE_INSTANCE_ID", "operations-db")
BIGTABLE_TABLE_ID = os.environ.get("BIGTABLE_TABLE_ID", "cashier_realtime_alerts")
BIGTABLE_ENRICHED_TABLE_ID = os.environ.get(
    "BIGTABLE_ENRICHED_TABLE_ID", "pos_transactions_enriched"
)

# Retrieval tuning.
SIMILARITY_THRESHOLD = float(os.environ.get("RAG_SIMILARITY_THRESHOLD", "0.70"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "3"))

# Cost guardrail: cap the bytes any single tool-issued query may bill.
MAX_BYTES_BILLED = int(os.environ.get("MAX_BYTES_BILLED", str(1_000_000_000)))

# Transient fault tolerance shared by all tools.
MAX_RETRIES = int(os.environ.get("TOOL_MAX_RETRIES", "3"))
RETRY_BASE_DELAY_SECONDS = float(os.environ.get("TOOL_RETRY_BASE_DELAY", "1.0"))


def get_pos_chunk_table_id() -> str:
    """Returns the fully-qualified POS manual chunk embeddings table."""
    return f"{get_project_id()}.{GOLD_DATASET_ID}.{POS_CHUNK_TABLE_NAME}"
