-- Challenge 2.2 · Step 1: Baseline retrieval quality on the Module 1 embeddings.
--
-- Purpose: demonstrate WHY the Module 1 table is unsuitable for error-code lookups
-- before re-chunking. Run this first and record the scores.
--
-- Usage: bq query --use_legacy_sql=false --project_id=$PROJECT_ID < 00_baseline_similarity_check.sql

DECLARE test_query STRING DEFAULT
  'What is the immediate field recovery protocol when a cashier encounters an '
  'ERR-PAY-4001 EMV contactless payment freeze?';

-- Baseline: coarse, whole-section embeddings from Module 1.
WITH q AS (
  SELECT AI.EMBED(test_query, endpoint => 'text-embedding-005').result AS qvec
)
SELECT
  'module1_baseline' AS source_table,
  m.base.document_filename,
  ROUND(1 - m.distance, 4) AS cosine_similarity,
  LENGTH(m.base.content)   AS embedded_text_length,
  ROUND(1 - m.distance, 4) >= 0.70 AS clears_threshold
FROM VECTOR_SEARCH(
  TABLE `<PROJECT_ID>.module1_unstructureddata.pos_manual_embeddings`,
  'embedding',
  (SELECT qvec FROM q),
  top_k => 3,
  distance_type => 'COSINE'
) m
ORDER BY cosine_similarity DESC;

-- Expected observation: the specific error code is diluted across thousands of
-- characters of general text, so similarity lands near or below the 0.70 safety
-- threshold and the top hit is often an unrelated maintenance section.
--
-- Re-run the same query against cymbal_gold.pos_manual_chunk_embeddings after
-- steps 01 and 02 to compare.
