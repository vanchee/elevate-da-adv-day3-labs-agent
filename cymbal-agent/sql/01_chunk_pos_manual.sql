-- Challenge 2.2 · Step 2: Re-chunk the POS manuals with a fine-grained sliding window.
--
-- Replaces the coarse Module 1 section embeddings with 500-character windows advanced
-- in 400-character steps, giving 100 characters of overlap between adjacent chunks.
-- The overlap keeps procedures that straddle a boundary intact; the RAG tool trims it
-- back out when it stitches chunks N-1..N+1 together at query time.
--
-- Usage: bq query --use_legacy_sql=false --project_id=$PROJECT_ID < 01_chunk_pos_manual.sql

CREATE OR REPLACE TABLE `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings` AS
SELECT
  s.document_filename,
  s.document_title,
  s.equipment_covered,
  s.source_pdf_uri,
  chunk_index,
  SUBSTR(s.extracted_full_content, offset_pos, 500) AS chunk_content
FROM `<PROJECT_ID>.module1_unstructureddata.pos_manual_generic_sections_extracted` AS s,
UNNEST(
  GENERATE_ARRAY(1, GREATEST(LENGTH(s.extracted_full_content), 1), 400)
) AS offset_pos WITH OFFSET AS chunk_index
-- Drop trailing whitespace-only or near-empty windows produced at the tail of a document.
WHERE LENGTH(TRIM(SUBSTR(s.extracted_full_content, offset_pos, 500))) > 30;

-- The embedding column is added separately so this step stays re-runnable on its own.
ALTER TABLE `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings`
  ADD COLUMN IF NOT EXISTS embedding ARRAY<FLOAT64>;

-- Sanity check: expect ~129 chunks across 5 manuals, max length 500.
SELECT
  COUNT(*)                          AS n_chunks,
  COUNT(DISTINCT document_filename) AS n_documents,
  MIN(LENGTH(chunk_content))        AS min_chunk_len,
  MAX(LENGTH(chunk_content))        AS max_chunk_len
FROM `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings`;
