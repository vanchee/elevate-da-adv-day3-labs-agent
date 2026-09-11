-- Part 5 Bonus 4 · Multi-tenant data isolation with BigQuery row-level security
--
-- WHY THIS AND NOT A PROMPT
-- -------------------------
-- Telling the model "only show STORE_007 data to the STORE_007 manager" is not a control:
-- it is a request, evaluated by a probabilistic system, that any prompt injection or
-- rephrasing can route around. Row access policies are enforced by BigQuery itself, below
-- every tool, so a query issued as a store manager physically cannot return another
-- store's rows regardless of what the model was persuaded to ask for.
--
-- This only has teeth in combination with end-user credential delegation (app/auth.py).
-- If every query runs as the agent's own service identity, BigQuery sees one principal and
-- there is nothing to isolate.
--
-- WHY pos_transactions_gold AND NOT gold_inventory_reconciliation_ledger
-- ----------------------------------------------------------------------
-- The inventory ledger is a BigQuery table for Apache Iceberg, and BigQuery rejects row
-- access policies on Iceberg tables outright:
--
--   Invalid value: BigQuery tables for Apache Iceberg have not been enabled for
--   row-level security.
--
-- This is worth internalising as an architectural constraint, not a workaround: choosing
-- open table formats for interoperability costs you BigQuery-native governance features.
-- If a table must carry row-level security, it must live in BigQuery managed storage, or
-- the isolation has to move up into an authorized view over the Iceberg table.
-- `pos_transactions_gold` is native storage with a `store_id` column (65,210 rows across
-- 50 stores), so it carries the policy instead.
--
-- >> READ BEFORE RUNNING <<
-- Once ANY row access policy exists on a table, principals matched by NO policy see ZERO
-- rows. The operator policy in step 2 is therefore mandatory, not optional: without it the
-- agent's own identity is locked out and every analytics answer silently becomes "no data".
-- Policies are OR'd, so a principal matched by several sees the union of their filters.
--
-- Usage:
--   sed -e "s/<PROJECT_ID>/$PROJECT_ID/g" \
--       -e "s/<OPERATOR_USER>/you@example.com/g" \
--       -e "s/<AGENT_SERVICE_ACCOUNT>/NNN-compute@developer.gserviceaccount.com/g" \
--       -e "s/<TENANT_DOMAIN>/example.com/g" \
--       04_row_level_security.sql \
--     | bq query --use_legacy_sql=false --label datacloud:antigravity --project_id=$PROJECT_ID
--
-- Roll back with 04_row_level_security_teardown.sql.

-- ---------------------------------------------------------------------------
-- 1. The grant table: which principal may see which store.
--    Kept tiny and clustered on user_email. A policy subquery is re-evaluated per query
--    and does NOT participate in partition or cluster pruning on the target table, so the
--    lookup side must stay cheap.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `<PROJECT_ID>.cymbal_governance.store_access_grants` (
  user_email STRING  NOT NULL OPTIONS(description="Principal email, matched against SESSION_USER()"),
  store_id   STRING  NOT NULL OPTIONS(description="Store this principal may read"),
  granted_by STRING           OPTIONS(description="Who approved the grant"),
  granted_at TIMESTAMP        OPTIONS(description="When the grant was issued")
)
CLUSTER BY user_email
OPTIONS(description="Row-level security grant table for cymbal_gold.pos_transactions_gold.");

-- Demonstration grants. Replace with your real tenant mapping.
MERGE `<PROJECT_ID>.cymbal_governance.store_access_grants` AS t
USING (
  SELECT * FROM UNNEST([
    STRUCT('store-manager-tokyo@<TENANT_DOMAIN>' AS user_email, 'STORE_001' AS store_id),
    STRUCT('store-manager-paris@<TENANT_DOMAIN>' AS user_email, 'STORE_007' AS store_id)
  ])
) AS s
ON t.user_email = s.user_email AND t.store_id = s.store_id
WHEN NOT MATCHED THEN
  INSERT (user_email, store_id, granted_by, granted_at)
  VALUES (s.user_email, s.store_id, SESSION_USER(), CURRENT_TIMESTAMP());

-- ---------------------------------------------------------------------------
-- 2. Operator escape hatch. MUST be created FIRST and MUST NOT be dropped while the
--    per-tenant policy exists, or the agent loses all visibility.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ROW ACCESS POLICY cymbal_operator_full_access
ON `<PROJECT_ID>.cymbal_gold.pos_transactions_gold`
GRANT TO (
  'user:<OPERATOR_USER>',
  'serviceAccount:<AGENT_SERVICE_ACCOUNT>'
)
FILTER USING (TRUE);

-- ---------------------------------------------------------------------------
-- 3. Per-tenant isolation. Every other principal in the organisation sees only the
--    stores the grant table lists for them -- which for an unlisted principal is
--    nothing. SESSION_USER() resolves to the identity that issued the query, which is
--    exactly the end-user token app/auth.py forwards when delegation is active.
--
--    The grantee is a domain rather than 'allAuthenticatedUsers' because most
--    enterprise orgs enforce the `constraints/iam.allowedPolicyMemberDomains`
--    (domain restricted sharing) org policy. Under it, binding a public principal to a
--    row access policy fails with:
--      "One or more users named in the policy do not belong to a permitted customer."
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ROW ACCESS POLICY cymbal_store_tenant_isolation
ON `<PROJECT_ID>.cymbal_gold.pos_transactions_gold`
GRANT TO ('domain:<TENANT_DOMAIN>')
FILTER USING (
  store_id IN (
    SELECT store_id
    FROM `<PROJECT_ID>.cymbal_governance.store_access_grants`
    WHERE user_email = SESSION_USER()
  )
);

-- ---------------------------------------------------------------------------
-- 4. Verify. As the operator this MUST return the full 65,210 rows / 50 stores. If it
--    returns 0, the operator policy did not match your principal -- run the teardown
--    immediately and correct <OPERATOR_USER>.
-- ---------------------------------------------------------------------------
SELECT
  COUNT(*)                 AS visible_rows,
  COUNT(DISTINCT store_id) AS visible_stores,
  SESSION_USER()           AS acting_principal
FROM `<PROJECT_ID>.cymbal_gold.pos_transactions_gold`;

SELECT row_access_policy_name, table_name
FROM `<PROJECT_ID>.cymbal_gold.INFORMATION_SCHEMA.ROW_ACCESS_POLICIES`
WHERE table_name = 'pos_transactions_gold';
