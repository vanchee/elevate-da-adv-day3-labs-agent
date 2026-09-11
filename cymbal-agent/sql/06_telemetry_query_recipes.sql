-- =====================================================================
-- Module 3 / Part 4.1 — Telemetry Query Recipes
-- =====================================================================
-- Reference GoogleSQL for the four "core query recipes" the lab asks the
-- BigQuery Conversational Agent (BQ CA) to answer in natural language.
--
-- WHY THIS FILE EXISTS
-- --------------------
-- BQ CA generates SQL non-deterministically. These queries are the
-- ground truth we grade its output against: if the agent's generated SQL
-- disagrees with these numbers, the agent is wrong, not the data.
-- They also serve as a deterministic fallback for CI.
--
-- SCHEMA NOTES (discovered empirically — do not guess these)
-- ----------------------------------------------------------
-- `agent_telemetry.events` stores latency/tokens in JSON columns:
--     latency_ms  -> {"total_ms": <int>, "time_to_first_token_ms": <int>}
--     attributes  -> {"model_version": ..., "usage_metadata": {...}}
--
--   ** The path is `$.total_ms`, NOT `$.total`. ** `$.total` returns NULL
--   silently, which produces a query that "works" and reports nothing.
--
-- Prefer the auto-generated views: the plugin already flattens the JSON
-- into typed columns, so you avoid the JSON-path trap entirely.
--     v_llm_response  -> model_version, usage_prompt_tokens,
--                        usage_completion_tokens, usage_total_tokens,
--                        total_ms, ttft_ms
--     v_tool_completed-> tool_name, total_ms, status, error_message
--     v_tool_error    -> tool_name, tool_args, error_message
--
-- One agent turn emits ~10 rows. Count turns with
-- COUNT(DISTINCT invocation_id), never COUNT(*).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Recipe 1 — [Cost & Token Analysis]
-- "Aggregate total input tokens and output tokens and request count
--  grouped by model."
-- ---------------------------------------------------------------------
-- Note: `usage_completion_tokens` excludes thinking tokens, but thinking
-- tokens ARE billed as output. We report both so the cost figure is
-- honest — reporting only completion tokens understates spend, and on
-- this workload thinking is a large fraction of output.
SELECT
  model_version                                AS model,
  COUNT(*)                                     AS request_count,
  SUM(usage_prompt_tokens)                     AS total_input_tokens,
  SUM(usage_completion_tokens)                 AS total_output_tokens,
  SUM(usage_thinking_tokens)                   AS total_thinking_tokens,
  SUM(usage_completion_tokens)
    + SUM(COALESCE(usage_thinking_tokens, 0))  AS total_billable_output_tokens,
  SUM(usage_total_tokens)                      AS total_tokens,
  ROUND(AVG(usage_total_tokens), 1)            AS avg_tokens_per_request
FROM `pvelevate-project.agent_telemetry.v_llm_response`
GROUP BY model
ORDER BY total_tokens DESC;

-- ---------------------------------------------------------------------
-- Recipe 2 — [Tool Performance & Latency]
-- "Calculate the average and maximum execution latency per tool,
--  sorted by the slowest tools first."
-- ---------------------------------------------------------------------
-- P50/P95 are included alongside avg/max because avg hides the tail and
-- max is a single unlucky sample. P95 is the number you actually put in
-- an SLO.
SELECT
  tool_name,
  COUNT(*)                                                  AS call_count,
  ROUND(AVG(total_ms), 1)                                   AS avg_latency_ms,
  MAX(total_ms)                                             AS max_latency_ms,
  CAST(APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS INT64) AS p50_latency_ms,
  CAST(APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS INT64) AS p95_latency_ms
FROM `pvelevate-project.agent_telemetry.v_tool_completed`
WHERE total_ms IS NOT NULL
GROUP BY tool_name
ORDER BY avg_latency_ms DESC;

-- ---------------------------------------------------------------------
-- Recipe 3 — [Reliability & Error Analysis]
-- "Find all failed tool calls or sessions with errors, showing the
--  session ID, tool name, and error message."
-- ---------------------------------------------------------------------
-- Two distinct failure surfaces exist and a query against only one of
-- them will under-report:
--   (a) TOOL_ERROR events        -> the tool raised
--   (b) TOOL_COMPLETED w/ status -> the tool returned, but unsuccessfully
-- We union both so "zero rows" genuinely means "no failures".
WITH tool_errors AS (
  SELECT
    timestamp,
    session_id,
    invocation_id,
    tool_name,
    'TOOL_ERROR'   AS failure_surface,
    error_message
  FROM `pvelevate-project.agent_telemetry.v_tool_error`
),
completed_failures AS (
  SELECT
    timestamp,
    session_id,
    invocation_id,
    tool_name,
    'TOOL_COMPLETED_NON_OK' AS failure_surface,
    error_message
  FROM `pvelevate-project.agent_telemetry.v_tool_completed`
  WHERE status IS NOT NULL AND UPPER(status) NOT IN ('OK', 'SUCCESS')
)
SELECT * FROM tool_errors
UNION ALL
SELECT * FROM completed_failures
ORDER BY timestamp DESC;

-- Companion: error rate per tool. A raw error list has no denominator,
-- so it cannot tell you whether 5 failures is fine or catastrophic.
SELECT
  c.tool_name,
  COUNT(*)                                                       AS total_calls,
  COUNTIF(c.status IS NOT NULL AND UPPER(c.status) NOT IN ('OK','SUCCESS')) AS failed_calls,
  ROUND(
    SAFE_DIVIDE(
      COUNTIF(c.status IS NOT NULL AND UPPER(c.status) NOT IN ('OK','SUCCESS')),
      COUNT(*)
    ) * 100, 2)                                                  AS error_rate_pct
FROM `pvelevate-project.agent_telemetry.v_tool_completed` c
GROUP BY c.tool_name
ORDER BY error_rate_pct DESC, total_calls DESC;

-- ---------------------------------------------------------------------
-- Recipe 4 — [Tool Invocations Distribution]
-- "Show the top 3 most frequently invoked tools and their percentage
--  distribution."
-- ---------------------------------------------------------------------
-- The percentage is taken over ALL tool calls, not just the top 3, so
-- the three values will not sum to 100%. That is intentional: the
-- residual is the long tail, and hiding it would misrepresent
-- concentration.
WITH counts AS (
  SELECT tool_name, COUNT(*) AS call_count
  FROM `pvelevate-project.agent_telemetry.v_tool_completed`
  GROUP BY tool_name
)
SELECT
  tool_name,
  call_count,
  ROUND(call_count * 100.0 / SUM(call_count) OVER (), 2) AS pct_of_all_tool_calls,
  RANK() OVER (ORDER BY call_count DESC)                 AS rank
FROM counts
QUALIFY rank <= 3
ORDER BY rank;

-- ---------------------------------------------------------------------
-- Sanity / scale check — run this first when results look empty.
-- ---------------------------------------------------------------------
SELECT
  COUNT(*)                          AS total_events,
  COUNT(DISTINCT invocation_id)     AS agent_turns,
  COUNT(DISTINCT session_id)        AS sessions,
  MIN(timestamp)                    AS first_event,
  MAX(timestamp)                    AS last_event
FROM `pvelevate-project.agent_telemetry.events`;
