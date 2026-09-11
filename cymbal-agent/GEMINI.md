# Coding Agent Guide

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `agents-cli playground` for interactive testing. Iterate based on user feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run `agents-cli eval run`, iterate by making changes and rerunning it until satisfied. Expect 5-10+ iterations. Once you have a baseline, reach for `agents-cli eval compare` (regression diffs), `agents-cli eval analyze` (cluster failure modes), and `agents-cli eval optimize` (auto-tune prompts). See the **Evaluation Guide** for metrics, dataset schema, LLM-as-judge config, and common gotchas.

### Phase 4: Pre-Deployment Tests
Run `uv run pytest tests/unit tests/integration`. Fix issues until all tests pass.

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms. See the **Deployment Guide** for details.

### Phase 6: Production Deployment
Ask the user: Option A (simple single-project) or Option B (full CI/CD pipeline with `agents-cli infra cicd`).

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios for your agent |
| `agents-cli eval run` | Run the agent over the eval dataset and grade the traces |
| `agents-cli eval generate` / `agents-cli eval grade` | Decoupled form: produce traces, then grade them |
| `agents-cli eval compare` | Compare two grade-results files (regression check) |
| `agents-cli eval analyze` | Cluster failure modes from grade results |
| `agents-cli eval metric list` | List built-in metrics available in the SDK |
| `agents-cli eval optimize` | Auto-tune agent prompts using eval data |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up project infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD to project |
| `agents-cli scaffold upgrade` | Upgrade project to latest version |

---

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **Model 404 errors**: Fix `GOOGLE_CLOUD_LOCATION` (e.g., `global` instead of `us-east1`), not the model name.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): Use `terraform import` instead of retrying creation.

---

## Environment Gotchas

### `uv` fails with "No solution found" / index 401

On a corp machine `~/.config/uv/uv.toml` may set a private Artifact Registry
mirror as the **default** index. When its credential expires, `uv` cannot see
the real version set on PyPI and reports a dependency conflict rather than an
auth error — e.g. claiming `a2a-sdk[http-server]>=1.0,<2` is unsatisfiable,
with the 401 buried in a trailing `hint:`. Any `uv run` then fails, which
takes `agents-cli` down with it.

```bash
export UV_DEFAULT_INDEX=https://pypi.org/simple
```

`UV_INDEX_URL` and `--index-url` do **not** override a `uv.toml` default;
`UV_DEFAULT_INDEX` and `--default-index` do.

To bypass `uv` entirely for a one-off, call the venv directly:
`./.venv/bin/python script.py`.

### Verify the lockfile before deploying

Agent Runtime's build cannot authenticate to a private index. After any
relock:

```bash
grep -c "artifact-foundry" uv.lock   # must be 0
```

### `uv lock` keeps stale pins

`uv lock` preserves existing pins unless you pass `--upgrade-package <name>`.
Raising a floor in `pyproject.toml` is not enough. If a version below the new
floor is already pinned, uv may keep it and silently drop an extra with only
a warning — which surfaces later as an `ImportError` at runtime.

### `agents-cli eval run` times out booting the app

`eval run` starts its own server with `uv run uvicorn app.fast_api_app:app`
and gives it 30s. This app needs longer (the A2A agent card is built at
startup), and that internal `uv run` **re-syncs the venv from the lockfile**.
Pre-start the server and point the CLI at it:

```bash
adk web --port 8000                     # from cymbal-agent/
agents-cli eval run --url http://127.0.0.1:8000 --app-name app ...
```

### Eval scores are 0–1, not 1–5

A rubric target of "4.0 / 5.0" means **0.80**. Reading 0.909 as a failure
against a 4.0 bar is the most common misreading of these results.

