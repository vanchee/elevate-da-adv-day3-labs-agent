# Evaluation Datasets

Datasets consumed by `agents-cli eval`. Three files, three jobs.

| File | Cases | Shape | Purpose |
| :--- | ---: | :--- | :--- |
| `basic-dataset.json` | 10 | completed trace | The benchmark as shipped by the lab. **Not runnable as-is** — see below. |
| `basic-dataset.runnable.json` | 10 | A (`prompt`) | Derived from the above. This is what the quality gate actually runs. |
| `eval-data.json` | 10 | A (`prompt`) | Our suite: 5 BRD capability cases + 5 guardrail probes. |
| `eval-data2.json` | 5 | B (`agent_data.turns`) | Our suite: multi-turn context retention, intent switching, safety under pressure. |

## Why `basic-dataset.json` needs a projection

The shipped file is not an input dataset — it is a **finished run**. Every case
carries all of:

- `prompt` — the user message
- `responses` — the reference agent's final answer
- `context` — the evidence its tools retrieved
- `agent_data.turns` — the recorded `function_call` / `function_response` events

`agents-cli eval generate` refuses that shape:

```text
Case has both top-level 'prompt' and agent_data.turns; ambiguous.
```

It is right to refuse. With both present there is no way to know whether you
meant *"run this prompt"* or *"continue this conversation"*.

You could grade the file untouched, but that scores the **reference**
implementation that produced the traces — its citations even point at the lab
author's GCS bucket rather than ours. That is a measurement of someone else's
agent. So we project the file down to prompts and re-run inference live:

```bash
python3 tests/eval/make_runnable.py
```

`context` is deliberately **kept** in the projection. `GROUNDING` is a
reference-based metric; without `context` the Vertex eval service rejects the
entire grade step with:

```text
400 INVALID_ARGUMENT - Error rendering metric prompt template:
Variable context is required but not provided.
```

That fails the whole run rather than skipping one metric, which is an easy way
to think you have a passing gate when half of it never executed.

`basic-dataset.json` itself is left byte-for-byte as delivered so the provenance
of the benchmark stays auditable.

## Running

The agent takes 5–45 s per turn and the ADK server needs longer than the CLI's
30 s boot timeout to come up (it builds an A2A agent card at startup). Start the
server yourself and point the eval at it, rather than letting `eval run` manage
the lifecycle:

```bash
# terminal 1
adk web --port 8000

# terminal 2 — the quality gate
agents-cli eval run \
  --dataset tests/eval/datasets/basic-dataset.runnable.json \
  --metrics tool_use_quality,grounding \
  --url http://127.0.0.1:8000 --app-name app \
  --project "$PROJECT_ID" --region us-central1 \
  --concurrency 5
```

Our own suite, using the full metric set in `../eval_config.yaml`:

```bash
agents-cli eval run \
  --dataset tests/eval/datasets/eval-data.json \
  --config tests/eval/eval_config.yaml \
  --url http://127.0.0.1:8000 --app-name app \
  --project "$PROJECT_ID" --region us-central1
```

## Re-grading without re-running the agent

Inference is the expensive half. When you only changed a metric or a threshold,
grade the traces you already have:

```bash
agents-cli eval grade \
  --traces artifacts/traces/<latest>.json \
  --metrics tool_use_quality,grounding \
  --project "$PROJECT_ID" --region us-central1
```

## Score scale

`agents-cli` reports built-in metrics on a **0–1** scale, not 1–5. The lab's
"≥ 4.0 / 5.0" quality gate therefore corresponds to **≥ 0.80** here. Do not
compare the two numbers directly.

## Dataset shapes

**Shape A — single-prompt case:**

```json
{
  "eval_case_id": "unique_case_id",
  "prompt": { "role": "user", "parts": [{ "text": "User message" }] }
}
```

**Shape B — continued conversation.** Prior turns go in `agent_data.turns` and
the last event must be a *user* message; `eval generate` appends the next agent
response. Seeding history rather than simulating a user keeps the graded
surface deterministic — the score reflects the agent's next action given a
fixed context, not the drift of a simulated counterpart.

```json
{
  "eval_case_id": "unique_case_id",
  "agent_data": {
    "turns": [
      {
        "turn_index": 0,
        "events": [
          { "author": "user",  "content": { "role": "user",  "parts": [{ "text": "First user message" }] } },
          { "author": "agent", "content": { "role": "model", "parts": [{ "text": "First agent reply" }] } },
          { "author": "user",  "content": { "role": "user",  "parts": [{ "text": "Follow-up" }] } }
        ]
      }
    ]
  }
}
```

## Discovering metrics

```bash
agents-cli eval metric list
```
