-- Part 5 Bonus 4 · Teardown for row-level security
--
-- Removes every row access policy from pos_transactions_gold, restoring unrestricted
-- access for anyone holding table-level BigQuery permissions.
--
-- Run this if the agent starts reporting "no data" after 04_row_level_security.sql:
-- that symptom means the operator policy failed to match the querying principal, and
-- BigQuery is correctly returning zero rows.
--
-- Usage:
--   sed "s/<PROJECT_ID>/$PROJECT_ID/g" 04_row_level_security_teardown.sql \
--     | bq query --use_legacy_sql=false --label datacloud:antigravity --project_id=$PROJECT_ID

DROP ALL ROW ACCESS POLICIES ON `<PROJECT_ID>.cymbal_gold.pos_transactions_gold`;

-- Confirm full visibility is back: expect 65,210 rows across 50 stores.
SELECT
  COUNT(*)                 AS visible_rows,
  COUNT(DISTINCT store_id) AS visible_stores
FROM `<PROJECT_ID>.cymbal_gold.pos_transactions_gold`;

-- The grant table is intentionally left in place; it holds no transaction data and
-- dropping it would lose the tenant mapping. Remove it explicitly if you no longer need it:
--   DROP TABLE `<PROJECT_ID>.cymbal_governance.store_access_grants`;
