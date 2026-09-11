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

"""Semantic entity resolution from an informal store reference to a canonical store_id.

Operators speak in landmarks ("the Ginza store", "our Paris flagship"); the warehouse
keys on `store_id`. Without this step the model either guesses an ID -- silently
returning another store's numbers -- or emits a `LIKE '%ginza%'` filter whose behaviour
depends on punctuation and casing nobody controls.

Embedding the store directory turns that guess into a retrieval with a measurable score,
so a weak match can be refused instead of fabricated. See
`sql/03_store_directory_embeddings.sql` for how the directory is built.
"""

import asyncio
import logging
import re
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from app import bq, config
from app.auth import DelegationError, resolve_credentials

logger = logging.getLogger(__name__)

# Store IDs are `STORE_` followed by digits; a reference already in that form needs
# lookup, not retrieval.
_STORE_ID_PATTERN = re.compile(r"\bSTORE[_\-\s]?(\d{1,4})\b", re.IGNORECASE)

# Number of directory entries to retrieve. Small: the directory has 16 rows, and the
# runner-up scores are only needed to detect a near-tie.
STORE_TOP_K = 4

# If the runner-up is within this cosine margin of the best match, the two are treated as
# indistinguishable. Picking one and proceeding would present a coin flip as a fact.
AMBIGUITY_MARGIN = 0.02


def _normalise_store_id(reference: str) -> Optional[str]:
    """Returns the canonical `STORE_0NN` form if the reference already contains an ID."""
    match = _STORE_ID_PATTERN.search(reference or "")
    if not match:
        return None
    return f"STORE_{int(match.group(1)):03d}"


def _format_candidate(row: Any) -> str:
    """Renders one directory candidate as a table row."""
    return (
        f"| `{row.store_id}` | {row.store_name} | {row.city} | "
        f"{float(row.similarity_score):.4f} |"
    )


_CANDIDATE_TABLE_HEADER = (
    "| store_id | store_name | city | cosine similarity |\n"
    "| --- | --- | --- | --- |"
)


def _candidate_table(rows: list) -> str:
    return "\n".join([_CANDIDATE_TABLE_HEADER, *[_format_candidate(r) for r in rows]])


async def _lookup_by_id(store_id: str, resolved) -> list:
    """Fetches a single directory entry by exact ID."""
    sql = f"""
    SELECT store_id, store_name, city, 1.0 AS similarity_score
    FROM `{config.get_store_directory_table_id()}`
    WHERE store_id = @store_id
    """
    return await bq.run_query(sql, bq.string_job_config(store_id=store_id), resolved)


async def _search_by_name(store_reference: str, resolved) -> list:
    """Runs cosine VECTOR_SEARCH over the embedded store directory.

    The user's phrase is embedded with the same `text-embedding-005` endpoint that built
    the directory vectors; mixing encoders would make the distances meaningless.
    """
    sql = f"""
    WITH query_embed AS (
      SELECT AI.EMBED(@store_reference, endpoint => 'text-embedding-005').result AS qvec
    )
    SELECT
      m.base.store_id,
      m.base.store_name,
      m.base.city,
      ROUND(1 - m.distance, 4) AS similarity_score
    FROM VECTOR_SEARCH(
      TABLE `{config.get_store_directory_table_id()}`,
      'embedding',
      (SELECT qvec FROM query_embed),
      top_k => {STORE_TOP_K},
      distance_type => 'COSINE'
    ) m
    ORDER BY similarity_score DESC
    """
    return await bq.run_query(
        sql, bq.string_job_config(store_reference=store_reference), resolved
    )


async def resolve_store_identifier(store_reference: str, tool_context: ToolContext) -> str:
    """Resolves an informal, partial, or misspelled store reference to a canonical store_id.

    Call this FIRST whenever the user names a store in words (for example "the Ginza
    store", "our Paris flagship", "the Toronto location") and you need a store_id for an
    analytics query. Returns the matching store_id, or asks for clarification when the
    reference matches more than one store.

    Args:
        store_reference: The store as the user described it, e.g. "the Ginza store".

    Returns:
        The resolved store_id with its similarity score, a disambiguation prompt listing
        the tied candidates, or a statement that no store matched.
    """
    reference = (store_reference or "").strip()
    if not reference:
        return "No store reference was provided. Ask the user which store they mean."

    try:
        resolved = resolve_credentials(tool_context)
    except DelegationError as e:
        return str(e)

    last_error: Optional[Exception] = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            explicit_id = _normalise_store_id(reference)
            if explicit_id:
                logger.info("Store reference %r parsed as explicit ID %s", reference, explicit_id)
                rows = await _lookup_by_id(explicit_id, resolved)
                if rows:
                    row = rows[0]
                    return (
                        f"**Resolved store_id:** `{row.store_id}`\n"
                        f"**Store:** {row.store_name} ({row.city})\n"
                        f"**Matched on:** exact store_id in the user's request"
                    )
                return (
                    f"`{explicit_id}` has no entry in the inventory ledger store "
                    "directory, which covers only stores with reconciliation data. The "
                    "ID may still be valid for real-time alert lookups. Ask the user to "
                    "confirm the store before running an analytics query."
                )

            logger.info("Resolving store reference %r via VECTOR_SEARCH", reference)
            rows = await _search_by_name(reference, resolved)
            if not rows:
                return (
                    f"No store in the Cymbal directory matches \"{reference}\". "
                    "Ask the user to confirm the store name."
                )

            best = rows[0]
            best_score = float(best.similarity_score)

            if best_score < config.STORE_MATCH_THRESHOLD:
                # Reported rather than suppressed: a near-miss is useful signal for the
                # user, but it must not be passed downstream as if it were a match.
                return (
                    f"No store matched \"{reference}\" with enough confidence "
                    f"(best cosine {best_score:.4f} < {config.STORE_MATCH_THRESHOLD:.2f} "
                    "threshold). Closest directory entries:\n\n"
                    f"{_candidate_table(rows)}\n\n"
                    "Ask the user which store they mean before running any analytics query."
                )

            # Two distinct stores can be genuinely indistinguishable: this directory has
            # STORE_001 and STORE_013 both named "Cymbal Tokyo Ginza District Flagship".
            # Their vectors differ only by the ID embedded in the search text, so the
            # scores land within noise of each other. Choosing one would silently answer
            # about the wrong store.
            tied = [
                r for r in rows
                if float(r.similarity_score) >= best_score - AMBIGUITY_MARGIN
            ]
            if len(tied) > 1:
                same_name = all(
                    (r.store_name or "").casefold() == (best.store_name or "").casefold()
                    for r in tied
                )
                reason = (
                    "these stores share an identical name"
                    if same_name
                    else "their similarity scores are within noise of each other"
                )
                return (
                    f"\"{reference}\" is ambiguous - {reason}, so it cannot be resolved "
                    "to a single store_id:\n\n"
                    f"{_candidate_table(tied)}\n\n"
                    "Ask the user which store_id they mean. Do not guess, and do not run "
                    "an analytics query until they choose."
                )

            return (
                f"**Resolved store_id:** `{best.store_id}`\n"
                f"**Store:** {best.store_name} ({best.city})\n"
                f"**Similarity Score:** {best_score:.4f} (cosine)  ·  "
                f"**Matched on:** semantic search over the store directory\n\n"
                f"Use `{best.store_id}` in any follow-up analytics query."
            )

        except Exception as e:
            last_error = e
            logger.warning("Store resolution failed on attempt %d: %s", attempt, e)
            if attempt < config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))

    logger.error("Store resolution failed after %d attempts: %s", config.MAX_RETRIES, last_error)
    return (
        "The store directory is temporarily unavailable due to a database connectivity "
        "issue. Ask the user to supply the store_id directly, or retry shortly."
    )
