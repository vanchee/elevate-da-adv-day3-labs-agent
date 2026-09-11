-- Challenge 2.2 · Step 3: Generate dense vector embeddings for each chunk.
--
-- Titles are prepended to the chunk text before embedding. Without this, chunks from
-- five different vendor manuals that describe the same symptom in similar language sit
-- very close together in vector space, and a query about a Toshiba fault can rank an
-- HP section higher. The title gives the encoder a document-identity signal.
--
-- Usage: bq query --use_legacy_sql=false --project_id=$PROJECT_ID < 02_generate_embeddings.sql

CREATE TEMP TABLE chunk_embeddings AS
SELECT
  document_filename,
  chunk_index,
  ml_generate_embedding_result AS embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL `<PROJECT_ID>.module1_unstructureddata.pos_text_embedding_model`,
  (
    SELECT
      document_filename,
      chunk_index,
      CONCAT('[', document_title, ']\n', chunk_content) AS content
    FROM `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings`
  ),
  STRUCT('RETRIEVAL_DOCUMENT' AS task_type)
);

UPDATE `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings` AS t
SET t.embedding = e.embedding
FROM chunk_embeddings AS e
WHERE t.document_filename = e.document_filename
  AND t.chunk_index = e.chunk_index;

-- Verify full coverage: n_chunks must equal n_embedded.
SELECT
  COUNT(*)                                  AS n_chunks,
  COUNTIF(ARRAY_LENGTH(embedding) > 0)      AS n_embedded,
  ANY_VALUE(ARRAY_LENGTH(embedding))        AS embedding_dimensions
FROM `<PROJECT_ID>.cymbal_gold.pos_manual_chunk_embeddings`;
