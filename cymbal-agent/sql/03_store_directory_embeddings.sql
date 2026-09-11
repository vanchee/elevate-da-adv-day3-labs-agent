-- Part 5 Bonus 3 · Semantic entity resolution: store_name <-> store_id
--
-- Users say "the Ginza store" or "our Champs Elysees flagship"; the warehouse keys on
-- store_id. Embedding the store directory lets the agent resolve informal or misspelled
-- names to an exact ID before it issues an analytical query.
--
-- Note the deliberate ambiguity in this dataset: STORE_001 and STORE_013 share the name
-- "Cymbal Tokyo Ginza District Flagship". The resolution tool must surface both rather
-- than silently picking one -- see app/tools/store_resolution_tool.py.
--
-- Embeddings are produced with AI.EMBED against the `text-embedding-005` endpoint --
-- the identical call app/tools/store_resolution_tool.py makes for the user's phrase at
-- query time. Document and query vectors MUST come from the same model, otherwise
-- cosine distance between them is meaningless.
--
-- Usage:
--   sed "s/<PROJECT_ID>/$PROJECT_ID/g" 03_store_directory_embeddings.sql \
--     | bq query --use_legacy_sql=false --label datacloud:antigravity --project_id=$PROJECT_ID

-- 1. Build the distinct store directory from the gold inventory ledger and embed it in
--    one pass. A richer surface form gives the encoder more to match informal phrasing
--    against ("the Ginza store", "Tokyo flagship", "our Japan location").
CREATE OR REPLACE TABLE `<PROJECT_ID>.cymbal_gold.store_directory_embeddings` AS
WITH directory AS (
  SELECT
    store_id,
    ANY_VALUE(store_name) AS store_name,
    ANY_VALUE(city)       AS city,
    CONCAT(ANY_VALUE(store_name), ' - ', ANY_VALUE(city), ' (', store_id, ')') AS search_text
  FROM `<PROJECT_ID>.cymbal_gold.gold_inventory_reconciliation_ledger`
  GROUP BY store_id
)
SELECT
  store_id,
  store_name,
  city,
  search_text,
  AI.EMBED(search_text, endpoint => 'text-embedding-005').result AS embedding
FROM directory;

-- 2. Verify: every store embedded with a consistent vector width.
SELECT
  COUNT(*)                              AS n_stores,
  COUNTIF(ARRAY_LENGTH(embedding) > 0)  AS n_embedded,
  MIN(ARRAY_LENGTH(embedding))          AS min_dims,
  MAX(ARRAY_LENGTH(embedding))          AS max_dims
FROM `<PROJECT_ID>.cymbal_gold.store_directory_embeddings`;

-- 3. Report duplicate names the resolution tool must disambiguate at query time.
--    Expected: "Cymbal Tokyo Ginza District Flagship" -> STORE_001, STORE_013.
SELECT store_name, STRING_AGG(store_id ORDER BY store_id) AS ambiguous_ids
FROM `<PROJECT_ID>.cymbal_gold.store_directory_embeddings`
GROUP BY store_name
HAVING COUNT(*) > 1;
