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

"""RAG tool for POS hardware troubleshooting using BigQuery vector search."""

import asyncio
import logging
import re
from typing import Any, Optional

from google.cloud import bigquery

from app import config

logger = logging.getLogger(__name__)

# Sliding-window chunks are 500 chars with 100 chars of overlap, so adjacent chunks
# repeat their first 100 characters. Trim that when stitching N-1..N+1 together.
CHUNK_OVERLAP_CHARS = 100

OUT_OF_SCOPE_DECLINE_STRING = (
    "I cannot find certified warranty or repair rules for this specific error in our technical repository."
)

_ERROR_CODE_PATTERN = re.compile(r"[A-Z]{3,}-[A-Z0-9\-]+")

_client: Optional[bigquery.Client] = None


def _get_client() -> bigquery.Client:
    """Returns a lazily-created, module-level BigQuery client.

    Reusing one client avoids re-doing ADC discovery and the TLS handshake on every
    tool call.
    """
    global _client
    if _client is None:
        _client = bigquery.Client(project=config.get_project_id())
    return _client


def _format_gcs_link(uri: Optional[str]) -> str:
    """Converts gs:// URIs to clickable https://storage.cloud.google.com/ links."""
    if not uri:
        return ""
    if uri.startswith("gs://"):
        return uri.replace("gs://", "https://storage.cloud.google.com/", 1)
    return uri


def _extract_error_code(query: str) -> str:
    """Extracts the first hardware error code (e.g. ERR-PAY-4001) from a query."""
    matches = _ERROR_CODE_PATTERN.findall(query)
    return matches[0] if matches else ""


def _job_config(**params: Any) -> bigquery.QueryJobConfig:
    """Builds a parameterised job config with the shared cost guardrail applied."""
    return bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in params.items()
        ],
        maximum_bytes_billed=config.MAX_BYTES_BILLED,
    )


async def _run_query(sql: str, job_config: bigquery.QueryJobConfig) -> list:
    """Runs a BigQuery query off the event loop so concurrent tool calls stay parallel."""
    def _blocking() -> list:
        return list(_get_client().query(sql, job_config=job_config).result())

    return await asyncio.to_thread(_blocking)


def _stitch(chunks: list[tuple[int, str]]) -> str:
    """Concatenates adjacent chunks, removing the repeated sliding-window overlap.

    Chunks arrive ordered by chunk_index. Every chunk after the first repeats the
    trailing CHUNK_OVERLAP_CHARS of its predecessor, so that prefix is dropped to
    avoid emitting duplicated sentences to the model.
    """
    parts: list[str] = []
    for position, (_, content) in enumerate(chunks):
        text = content or ""
        if position > 0 and len(text) > CHUNK_OVERLAP_CHARS:
            text = text[CHUNK_OVERLAP_CHARS:]
        parts.append(text)
    return "".join(parts).strip()


def _format_hit(row: Any, stitched: str, exact_match: bool) -> str:
    """Renders a single retrieval hit with its true cosine score and provenance."""
    doc_title = row.document_title or "POS Manual"
    equipment = row.equipment_covered or "POS Terminal"
    doc_link = _format_gcs_link(row.source_pdf_uri)
    filename = row.document_filename or "Runbook"
    similarity = float(row.similarity_score) if row.similarity_score is not None else 0.0

    if exact_match:
        basis = "exact error-code match in chunk text"
    else:
        basis = f"cosine ≥ {config.SIMILARITY_THRESHOLD:.2f} threshold"

    return (
        f"### {doc_title} ({equipment})\n"
        f"**Certified Reference Document:** [{filename}]({doc_link})\n"
        f"**Similarity Score:** {similarity:.4f} (cosine)  ·  **Matched on:** {basis}\n\n"
        f"#### Troubleshooting Procedure:\n{stitched}"
    )


async def pos_troubleshooting_rag_tool(query: str) -> str:
    """Performs semantic vector search over POS terminal runbooks and manuals in BigQuery.

    Use this tool to resolve POS terminal hardware error codes (such as ERR-PAY-4001,
    ERR-DN-PRNT-24V), device freezes, barcode scanner errors, receipt printer jams, and
    cash drawer lock issues. Retrieves certified runbook instructions and adjacent steps.

    Args:
        query: Specific hardware error code, fault description, or troubleshooting procedure.

    Returns:
        Troubleshooting procedure with source documentation link, or safety warning if out-of-scope.
    """
    error_code = _extract_error_code(query)
    chunk_table = config.get_pos_chunk_table_id()

    # `similarity_score` is the true cosine similarity and is the ONLY value gated
    # against the safety threshold or shown to the user. `rank_score` adds a lexical
    # bonus for an exact error-code hit and is used solely for ordering, because pure
    # cosine ranking can favour a different vendor's manual that discusses the same
    # symptom in more general language.
    vector_sql = f"""
    WITH query_embed AS (
      SELECT AI.EMBED(@query_text, endpoint => 'text-embedding-005').result AS qvec
    ),
    raw_matches AS (
      SELECT
        m.base.document_filename,
        m.base.document_title,
        m.base.equipment_covered,
        m.base.source_pdf_uri,
        m.base.chunk_index,
        ROUND(1 - m.distance, 4) AS similarity_score,
        (@error_code != '' AND m.base.chunk_content LIKE CONCAT('%', @error_code, '%')) AS exact_error_match,
        ROUND(1 - m.distance, 4)
          + IF(@error_code != '' AND m.base.chunk_content LIKE CONCAT('%', @error_code, '%'), 0.20, 0.0)
          AS rank_score
      FROM VECTOR_SEARCH(
        TABLE `{chunk_table}`,
        'embedding',
        (SELECT qvec FROM query_embed),
        top_k => {config.RAG_TOP_K},
        distance_type => 'COSINE'
      ) m
    )
    SELECT
      rm.document_filename,
      rm.document_title,
      rm.equipment_covered,
      rm.source_pdf_uri,
      rm.chunk_index AS center_chunk_index,
      rm.similarity_score,
      rm.exact_error_match,
      rm.rank_score,
      ARRAY_AGG(STRUCT(c.chunk_index AS idx, c.chunk_content AS content) ORDER BY c.chunk_index ASC) AS context_chunks
    FROM raw_matches rm
    JOIN `{chunk_table}` c
      ON rm.document_filename = c.document_filename
     AND c.chunk_index BETWEEN (rm.chunk_index - 1) AND (rm.chunk_index + 1)
    GROUP BY
      rm.document_filename, rm.document_title, rm.equipment_covered, rm.source_pdf_uri,
      rm.chunk_index, rm.similarity_score, rm.exact_error_match, rm.rank_score
    ORDER BY rm.rank_score DESC
    """

    last_error: Optional[Exception] = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            logger.info(
                "Executing VECTOR_SEARCH on POS chunks (attempt %d): %s (error_code: %s)",
                attempt, query, error_code or "n/a",
            )
            rows = await _run_query(
                vector_sql, _job_config(query_text=query, error_code=error_code)
            )

            # A chunk is certified if it clears the cosine threshold OR literally
            # contains the queried error code. The lexical signal is deliberately an
            # independent admission criterion rather than a bonus folded into the
            # score: an exact code hit is stronger evidence than fuzzy similarity, and
            # keeping them separate means the reported score stays a true cosine and
            # the threshold still rejects genuinely out-of-scope questions.
            certified = [
                r for r in rows
                if bool(r.exact_error_match)
                or (
                    r.similarity_score is not None
                    and float(r.similarity_score) >= config.SIMILARITY_THRESHOLD
                )
            ]

            if certified:
                sections = [
                    _format_hit(
                        r,
                        _stitch([(c["idx"], c["content"]) for c in r.context_chunks]),
                        bool(r.exact_error_match),
                    )
                    for r in certified
                ]
                return "\n\n---\n\n".join(sections)

            logger.info(
                "No chunk met the %.2f cosine threshold; falling back to full-text SEARCH()",
                config.SIMILARITY_THRESHOLD,
            )
            return await _full_text_fallback(query, error_code, chunk_table)

        except Exception as e:
            last_error = e
            logger.warning("Error querying BigQuery RAG table on attempt %d: %s", attempt, e)
            if attempt < config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))

    logger.error("POS RAG retrieval failed after %d attempts: %s", config.MAX_RETRIES, last_error)
    return (
        "POS troubleshooting documentation is temporarily unavailable due to a database connectivity issue. "
        "Please retry your inquiry shortly."
    )


async def _full_text_fallback(query: str, error_code: str, chunk_table: str) -> str:
    """Keyword fallback used when no chunk clears the cosine similarity threshold."""
    if error_code:
        search_term = f"`{error_code}`"
    else:
        clean_tokens = re.findall(r"[a-zA-Z0-9]+", query)
        search_term = " ".join(clean_tokens) if clean_tokens else query

    fallback_sql = f"""
    SELECT
      document_filename,
      document_title,
      equipment_covered,
      source_pdf_uri,
      chunk_index,
      chunk_content
    FROM `{chunk_table}`
    WHERE SEARCH(chunk_content, @search_term)
    ORDER BY
      IF(@error_code != '' AND chunk_content LIKE CONCAT('%', @error_code, '%'), 0, 1),
      chunk_index
    LIMIT 1
    """

    rows = await _run_query(
        fallback_sql, _job_config(search_term=search_term, error_code=error_code)
    )
    if not rows:
        return OUT_OF_SCOPE_DECLINE_STRING

    row = rows[0]
    doc_title = row.document_title or "POS Manual"
    equipment = row.equipment_covered or "POS Terminal"
    doc_link = _format_gcs_link(row.source_pdf_uri)
    filename = row.document_filename or "Runbook"

    # No similarity score is reported here: this result came from a keyword match, not
    # from vector search, so inventing a numeric relevance value would be misleading.
    return (
        f"### {doc_title} ({equipment}) *(Retrieved via Full-Text Search Fallback)*\n"
        f"**Certified Reference Document:** [{filename}]({doc_link})\n"
        f"**Retrieval Method:** Keyword match — below the "
        f"{config.SIMILARITY_THRESHOLD:.2f} vector similarity threshold, treat as unverified.\n\n"
        f"#### Troubleshooting Procedure:\n{row.chunk_content or ''}"
    )
