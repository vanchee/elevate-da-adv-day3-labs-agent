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

import logging
import os
import re
import time
from typing import Optional

from google.cloud import bigquery

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "pvelevate-project")
DATASET_ID = "cymbal_gold"
CHUNK_TABLE_ID = f"{PROJECT_ID}.{DATASET_ID}.pos_manual_chunk_embeddings"
SIMILARITY_THRESHOLD = 0.70


def _format_gcs_link(uri: Optional[str]) -> str:
    """Converts gs:// URIs to clickable https://storage.cloud.google.com/ links."""
    if not uri:
        return ""
    if uri.startswith("gs://"):
        return uri.replace("gs://", "https://storage.cloud.google.com/", 1)
    return uri


def pos_troubleshooting_rag_tool(query: str) -> str:
    """Performs semantic vector search over POS terminal runbooks and manuals in BigQuery.

    Use this tool to resolve POS terminal hardware error codes (such as ERR-PAY-4001,
    ERR-DN-PRNT-24V), device freezes, barcode scanner errors, receipt printer jams, and
    cash drawer lock issues. Retrieves certified runbook instructions and adjacent steps.

    Args:
        query: Specific hardware error code, fault description, or troubleshooting procedure.

    Returns:
        Troubleshooting procedure with source documentation link, or safety warning if out-of-scope.
    """
    max_retries = 3
    base_delay = 1.0

    client = bigquery.Client(project=PROJECT_ID)

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
        ROUND(1 - m.distance, 4) AS similarity_score
      FROM VECTOR_SEARCH(
        TABLE `{CHUNK_TABLE_ID}`,
        'embedding',
        (SELECT qvec FROM query_embed),
        top_k => 3,
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
      STRING_AGG(c.chunk_content, '\\n\\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM raw_matches rm
    JOIN `{CHUNK_TABLE_ID}` c
      ON rm.document_filename = c.document_filename
     AND c.chunk_index BETWEEN (rm.chunk_index - 1) AND (rm.chunk_index + 1)
    GROUP BY rm.document_filename, rm.document_title, rm.equipment_covered, rm.source_pdf_uri, rm.chunk_index, rm.similarity_score
    ORDER BY rm.similarity_score DESC
    LIMIT 1
    """

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Executing VECTOR_SEARCH on POS chunks (attempt %d): %s", attempt, query)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("query_text", "STRING", query)
                ]
            )
            rows = list(client.query(vector_sql, job_config=job_config).result())

            if rows:
                row = rows[0]
                similarity = float(row.similarity_score) if row.similarity_score is not None else 0.0
                doc_title = row.document_title or "POS Manual"
                equipment = row.equipment_covered or "POS Terminal"
                doc_link = _format_gcs_link(row.source_pdf_uri)
                stitched_content = row.stitched_content or ""

                if similarity >= SIMILARITY_THRESHOLD:
                    return (
                        f"### {doc_title} ({equipment})\n"
                        f"**Certified Reference Document:** [{row.document_filename or 'Runbook'}]({doc_link})\n"
                        f"**Similarity Score:** {similarity:.4f}\n\n"
                        f"#### Troubleshooting Procedure:\n"
                        f"{stitched_content}"
                    )

            # Similarity threshold not met or no vector match; trigger full-text search fallback
            logger.info("Vector similarity below %.2f or empty; triggering full-text search fallback", SIMILARITY_THRESHOLD)

            # Extract hardware error codes or keywords (e.g. ERR-PAY-4001)
            error_codes = re.findall(r'[A-Z]{3,}-[A-Z0-9\-]+', query)
            search_term = error_codes[0] if error_codes else query

            fallback_sql = f"""
            SELECT
              document_filename,
              document_title,
              equipment_covered,
              source_pdf_uri,
              chunk_index,
              chunk_content
            FROM `{CHUNK_TABLE_ID}`
            WHERE chunk_content LIKE CONCAT('%', @search_term, '%')
            LIMIT 1
            """
            fallback_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("search_term", "STRING", search_term)
                ]
            )
            fallback_rows = list(client.query(fallback_sql, job_config=fallback_config).result())

            if fallback_rows:
                fb_row = fallback_rows[0]
                doc_title = fb_row.document_title or "POS Manual"
                equipment = fb_row.equipment_covered or "POS Terminal"
                doc_link = _format_gcs_link(fb_row.source_pdf_uri)
                content = fb_row.chunk_content or ""

                return (
                    f"### {doc_title} ({equipment}) *(Retrieved via Full-Text Search Fallback)*\n"
                    f"**Certified Reference Document:** [{fb_row.document_filename or 'Runbook'}]({doc_link})\n\n"
                    f"#### Troubleshooting Procedure:\n"
                    f"{content}"
                )

            # Truly out-of-scope query
            return (
                f"WARNING: Out-of-scope query. No certified POS hardware documentation found for '{query}' "
                f"(similarity score below certified threshold {SIMILARITY_THRESHOLD:.2f}). "
                f"This system only supports Cymbal POS terminal hardware, peripheral troubleshooting, "
                f"and certified Toshiba TCx 810 operational runbooks."
            )

        except Exception as e:
            logger.warning("Error querying BigQuery RAG table on attempt %d: %s", attempt, str(e))
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))

    return (
        "POS troubleshooting documentation is temporarily unavailable due to a database connectivity issue. "
        "Please retry your inquiry shortly."
    )
