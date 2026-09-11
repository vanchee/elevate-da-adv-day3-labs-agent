# Telemetry & Operations Report — Module 3 / Part 4

Generated against live telemetry in `pvelevate-project.agent_telemetry`.
Every number below came from a query executed at the time of writing; none
are estimates.

**Observation window:** 320 events · 24 agent turns · 24 sessions
(one turn per session — these are eval-driven single-shot invocations, not
long conversations).

---

## 1. How the telemetry gets there

`BigQueryAgentAnalyticsPlugin` is attached to the ADK `App` in
[`app/agent.py`](../app/agent.py). It streams events over the BigQuery
**Storage Write API (gRPC)** on a background batcher, so agent latency is
not coupled to BigQuery write latency.

Two configuration details are load-bearing and easy to get wrong:

| Setting | Default | Ours | Consequence of the default |
| :--- | :--- | :--- | :--- |
| `location` | `"US"` | `us-central1` | Write-stream lookup resolves to a different BigQuery instance; rows silently land nowhere useful |
| `table_id` | `agent_events` | `events` | The 23 auto-generated views are named after the table; the lab's dashboard and recipes expect `events` |

The plugin is wired **fail-open**: if telemetry cannot initialise, the agent
still serves traffic. Observability is not worth an outage.

One turn emits ~10 events (`USER_MESSAGE_RECEIVED` → `INVOCATION_STARTING`
→ `AGENT_STARTING` → `LLM_REQUEST`/`LLM_RESPONSE` ×2 → `TOOL_STARTING` →
`TOOL_COMPLETED` → `AGENT_RESPONSE` → `AGENT_COMPLETED` →
`INVOCATION_COMPLETED`). **Count turns with `COUNT(DISTINCT invocation_id)`,
never `COUNT(*)`.**

---

## 2. Query recipes — run through the BigQuery Conversational Agent

Data Agent: `projects/pvelevate-project/locations/global/dataAgents/cymbal-agent-telemetry-data-agent`

All four recipes returned `status=SUCCESS` (0/4 failures) in 16.8–25.3s each.
Reference SQL for cross-checking is in
[`sql/06_telemetry_query_recipes.sql`](../sql/06_telemetry_query_recipes.sql).

### Recipe 1 — Cost & Token Analysis

BQ CA generated SQL against `v_llm_response` (joined to `events`) and returned:

| model | requests | input tokens | output tokens |
| :--- | ---: | ---: | ---: |
| gemini-3.6-flash | 55 | 171,680 | 12,101 |

> [!WARNING]
> **This answer understates output cost by 2.26×.** Re-running the same
> aggregation with thinking tokens included:
>
> | | tokens |
> | :--- | ---: |
> | `usage_completion_tokens` (what BQ CA reported as "output") | 12,101 |
> | `usage_thinking_tokens` (**also billed as output**) | 15,218 |
> | **True billable output** | **27,319** |
> | Total | 198,999 |
>
> Thinking tokens are 56% of billable output on this workload. The view
> exposes them in a separate column, and the phrase "output tokens" maps
> naturally onto `usage_completion_tokens`, so the agent picked the column
> whose name matched the question rather than the one that matches the bill.
> This is not a BQ CA defect — it is a reminder that **a correct query and a
> correct answer are different things**, and that the human reviewing the
> generated SQL is still the control.

Also worth noting: the generated SQL used
`COALESCE(JSON_VALUE(e.attributes, '$.model'), r.model_version, 'Unknown')`
with a `LEFT JOIN` back to `events`. `$.model` does not exist in
`attributes` (the key is `model_version`), so the join contributed nothing
and the `COALESCE` fallback carried the result. The answer was right; the
query was doing unnecessary work against a path that will always be NULL.

### Recipe 2 — Tool Performance & Latency

Generated SQL correctly targeted `v_tool_completed`. It volunteered a
7-day window and *said so* in the response — good behaviour, since an
unqualified latency question is genuinely ambiguous.

| tool | calls | avg ms | p50 ms | p95 ms | max ms |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `cymbal_analytics_tool` | 19 | 18,588.6 | 16,698 | 33,113 | 33,113 |
| `read_cashier_realtime_alerts` | 4 | 7,279.7 | 3,423 | 15,114 | 15,114 |
| `pos_troubleshooting_rag_tool` | 8 | 3,216.0 | 2,999 | 6,241 | 6,241 |
| `resolve_store_identifier` | 2 | 1,161.0 | 1,032 | 1,290 | 1,290 |

**Interpretation.** `cymbal_analytics_tool` dominates the latency budget at
~18.6s average. That is expected and not a bug: it is itself an LLM call
(the Data Agent generates SQL, then BigQuery executes it), so a single
invocation pays a text-to-SQL round trip *plus* a query. It is the obvious
target for caching or for a deterministic fast path on the handful of
questions that get asked repeatedly.

`read_cashier_realtime_alerts` has a p50 of 3.4s but a max of 15.1s — a
4.4× spread over only 4 calls. That is a cold-start signature (OIDC token
mint + Bigtable connection), not steady-state behaviour. With n=4 it is
suggestive, not conclusive.

> [!NOTE]
> With sample sizes this small (n=2 to n=19), p95 collapses onto max.
> These figures characterise the *shape* of the system, not its SLO. Treat
> the ordering as meaningful and the absolute tail values as provisional.

### Recipe 3 — Reliability & Error Analysis

**Zero errors.** This is the recipe whose result is most easily faked, so
the process matters more than the number. BQ CA did not simply return an
empty table — it ran four progressively broader queries before concluding:

1. `v_tool_error` → 0 rows
2. `events WHERE status='ERROR' OR error_message IS NOT NULL` → 0 rows
3. `events GROUP BY event_type` → 10 rows (confirming data exists)
4. `SELECT DISTINCT status FROM events` → 1 row (`OK`)

Only then did it answer "no failed tool calls". Steps 3 and 4 are the
important ones: they distinguish *"nothing failed"* from *"the query was
wrong"*. That is exactly the discipline an on-call engineer should apply
to a green dashboard, and it is worth pointing out that the agent applied
it unprompted.

### Recipe 4 — Tool Invocations Distribution

| tool | calls | % of all tool calls |
| :--- | ---: | ---: |
| `cymbal_analytics_tool` | 19 | 57.6% |
| `pos_troubleshooting_rag_tool` | 8 | 24.2% |
| `read_cashier_realtime_alerts` | 4 | 12.1% |

The three sum to 93.9%; the residual 6.1% is `resolve_store_identifier`.

Cross-referencing recipes 2 and 4 gives the actionable result: the most
frequently called tool is also the slowest. `cymbal_analytics_tool`
accounts for 57.6% of invocations at 18.6s each, so it owns the large
majority of total tool-time. Any latency work should start and probably
end there.

---

## 3. The JSON-path trap

`latency_ms`, `attributes` and `content` are JSON columns on `events`.
The obvious path is wrong:

```sql
JSON_VALUE(latency_ms, '$.total')     -- returns NULL, no error
JSON_VALUE(latency_ms, '$.total_ms')  -- correct
```

BigQuery does not error on a JSON path that matches nothing — it returns
NULL. A dashboard built on `$.total` renders successfully, shows zeroes,
and looks like a healthy system with no latency. **Prefer the
auto-generated views** (`v_llm_response`, `v_tool_completed`), which
already flatten these into typed columns and remove the opportunity for
this mistake entirely.

---

## 4. End-user latency (TTFT)

| metric | value |
| :--- | ---: |
| p50 time-to-first-token | 3,306 ms |
| p95 time-to-first-token | 9,129 ms |

TTFT is the number the user actually feels. It is decoupled from total
tool latency because the agent streams its first tokens before the slow
analytics call completes on multi-step turns.

---

## 5. What this buys operationally

Before the plugin, answering "why is the agent slow?" meant reading
application logs. Now it is a `GROUP BY`. Concretely, this dataset can
answer, with SQL and no instrumentation work:

- **Cost attribution** per model, per session, per user — including the
  thinking-token component that is invisible if you trust the column name.
- **Latency attribution** per tool, so optimisation targets the 57.6%/18.6s
  tool rather than whatever was most recently complained about.
- **Reliability**, with the denominator attached, so "5 errors" becomes
  "5 errors in 33 calls".
- **Regression detection** across deploys, because `timestamp` and
  `agent` are on every row.

The cost of all this is one plugin registration and a BigQuery dataset.

---

## 6. Known gaps

| Gap | Impact | Why it is open |
| :--- | :--- | :--- |
| n=24 turns | p95 ≈ max; tail figures are provisional | Needs sustained traffic, not a fix |
| No cost in currency | Token counts only | Requires a price table joined on `model_version`; deliberately omitted rather than hard-coding a rate that will drift |
| Cold-start not separated | `read_cashier_realtime_alerts` p50/max spread is unexplained | Would need a first-call-in-process flag on the event |
