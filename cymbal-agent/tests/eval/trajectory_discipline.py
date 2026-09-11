# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic trajectory discipline metric (see eval_config.yaml).

`TOOL_USE_QUALITY` asks a judge model whether the tool choice looked sensible.
That is the right question for open-ended cases, but it will not reliably catch
two specific architectural invariants this agent depends on, both of which are
cheap to assert exactly:

1. **Store resolution precedence.** When the user names a store in prose
   ("our Champs-Elysees flagship") rather than by ID, ``resolve_store_identifier``
   must run *before* any analytics call. Skipping it means the model invented a
   store ID, and an invented ID silently returns another store's numbers rather
   than an error.

2. **No redundant dispatch.** The same tool called twice with identical
   arguments in one turn is wasted latency and wasted spend. This shows up
   under retry logic and under parallel-dispatch prompts.

Both are graded from the recorded trace, so this metric adds no tokens and no
wall-clock time to an eval run.

Note on SSE traces: streaming emits each ``function_call`` twice (a partial and
an aggregated final event) while the tool executes only once. Duplicate
detection therefore requires *three or more* identical calls before flagging
redundancy -- two is the normal streaming artefact, not a real double dispatch.
"""

from __future__ import annotations

import json
import re

_STORE_ID = re.compile(r"\bSTORE[_-]?\d{3}\b", re.IGNORECASE)

# Prose store references that should force a resolution call first.
_NAMED_STORE_HINTS = (
    "champs-elysees",
    "champs elysees",
    "ginza",
    "flagship",
    "covent garden",
    "fifth ave",
    "michigan ave",
    "union square",
    "eaton",
    "harbour outlet",
    "paris",
    "tokyo",
    "london",
    "toronto",
)

_RESOLVER = "resolve_store_identifier"
_ANALYTICS = ("cymbal_analytics_tool", "ask_data_agent")

# Streaming duplicates every function_call once; only a third occurrence is real.
_STREAMING_DUPLICATE_ALLOWANCE = 2


def _extract_calls(agent_data) -> list[tuple[str, str]]:
    """Returns [(tool_name, canonical_args_json)] in trace order."""
    if not agent_data:
        return []
    if isinstance(agent_data, str):
        try:
            agent_data = json.loads(agent_data)
        except (ValueError, TypeError):
            return []

    calls: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            fc = node.get("function_call") or node.get("functionCall")
            if isinstance(fc, dict) and fc.get("name"):
                args = fc.get("args") or fc.get("arguments") or {}
                try:
                    canonical = json.dumps(args, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    canonical = str(args)
                calls.append((fc["name"], canonical))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(agent_data)
    return calls


def evaluate(instance):
    prompt = instance.get("prompt") or ""
    lowered = prompt.lower()
    calls = _extract_calls(instance.get("agent_data"))
    names = [name for name, _ in calls]

    if not calls:
        return {
            "score": 5,
            "explanation": "n/a - no tool calls in trace; nothing to assert.",
        }

    findings: list[str] = []
    score = 5

    # --- Invariant 1: resolution precedence -------------------------------
    names_a_store = any(hint in lowered for hint in _NAMED_STORE_HINTS)
    has_explicit_id = bool(_STORE_ID.search(prompt))
    analytics_used = [n for n in names if n in _ANALYTICS]

    if names_a_store and not has_explicit_id and analytics_used:
        first_analytics = min(names.index(n) for n in analytics_used)
        if _RESOLVER not in names:
            score = 1
            findings.append(
                "prose store reference went straight to analytics without "
                f"{_RESOLVER}; the store ID was guessed"
            )
        elif names.index(_RESOLVER) > first_analytics:
            score = 2
            findings.append(
                f"{_RESOLVER} ran AFTER analytics; resolution must precede the query"
            )

    # --- Invariant 2: redundant dispatch ----------------------------------
    seen: dict[tuple[str, str], int] = {}
    for call in calls:
        seen[call] = seen.get(call, 0) + 1
    redundant = {
        name: count
        for (name, _), count in seen.items()
        if count > _STREAMING_DUPLICATE_ALLOWANCE
    }
    if redundant:
        score = min(score, 3)
        findings.append(
            "identical call repeated beyond the streaming-duplicate allowance: "
            + ", ".join(f"{n}x{c}" for n, c in sorted(redundant.items()))
        )

    if findings:
        return {"score": score, "explanation": "; ".join(findings)}

    unique = sorted(set(names))
    return {
        "score": 5,
        "explanation": f"trajectory clean; tools used: {unique}",
    }
