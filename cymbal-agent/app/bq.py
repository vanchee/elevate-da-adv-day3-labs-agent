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

"""Identity-aware BigQuery access shared by every query-issuing tool.

Two invariants live here so that no individual tool can forget them:

1. **One client per acting principal.** A delegated end-user query must never reuse the
   agent's own client, or BigQuery would evaluate row-level security against the wrong
   identity and the delegation in `app.auth` would be cosmetic.
2. **Every query carries a byte cap.** `maximum_bytes_billed` makes BigQuery reject an
   over-budget job before it scans anything, which converts a runaway model-authored
   filter from a cost incident into an error message.
"""

import asyncio
import hashlib
import logging

from google.cloud import bigquery

from app import config
from app.auth import ResolvedCredentials

logger = logging.getLogger(__name__)

# Delegated tokens rotate, so the cache is bounded to stop it growing without limit in a
# long-lived multi-user process.
_MAX_CACHED_CLIENTS = 32

_clients: dict[str, bigquery.Client] = {}


def client_cache_key(resolved: ResolvedCredentials) -> str:
    """Derives a stable, non-sensitive cache key for a set of credentials."""
    token = getattr(resolved.credentials, "token", None)
    if resolved.is_end_user and isinstance(token, str):
        return "user:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    return "adc"


def get_client(resolved: ResolvedCredentials) -> bigquery.Client:
    """Returns a BigQuery client bound to the acting principal's credentials."""
    key = client_cache_key(resolved)
    client = _clients.get(key)
    if client is None:
        client = bigquery.Client(
            project=config.get_project_id(), credentials=resolved.credentials
        )
        if len(_clients) > _MAX_CACHED_CLIENTS:
            for stale in [k for k in _clients if k != "adc"][: _MAX_CACHED_CLIENTS // 2]:
                _clients.pop(stale, None)
        _clients[key] = client
    return client


def string_job_config(**params: str) -> bigquery.QueryJobConfig:
    """Builds a STRING-parameterised job config with the shared cost guardrail applied."""
    return bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in params.items()
        ],
        maximum_bytes_billed=config.MAX_BYTES_BILLED,
    )


async def run_query(
    sql: str, job_config: bigquery.QueryJobConfig, resolved: ResolvedCredentials
) -> list:
    """Runs a BigQuery query off the event loop so concurrent tool calls stay parallel."""

    def _blocking() -> list:
        return list(get_client(resolved).query(sql, job_config=job_config).result())

    return await asyncio.to_thread(_blocking)
