#!/usr/bin/env python3
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

"""Attach each trace's OWN tool output to it as grounding context.

Why this is necessary
---------------------
`GROUNDING` is reference-based: it needs a ``context`` field and asks, sentence
by sentence, "is this claim supported by that evidence?".

The obvious move is to reuse the ``context`` shipped inside
``basic-dataset.json``. That is wrong, and it fails in a way that looks like an
agent bug:

  * The shipped context is a snapshot of what the **reference** agent retrieved,
    against the **lab author's** project. Our answers correctly cite
    ``pvelevate-project...``; the judge sees a citation absent from the evidence
    and labels it ``unsupported``.
  * Half the benchmark asks about **live** operational tables. Our run returns
    today's numbers, the frozen context holds the numbers from whenever the
    benchmark was recorded, and the judge labels the difference
    ``contradictory``.

Measured on this repo, grading against the shipped context produced 58
``unsupported`` and 22 ``contradictory`` sentence labels and a flat 0.000 across
all ten cases -- a score that says almost nothing about our agent, because
``grounding_v1`` is strict: a single unsupported sentence zeroes the case.

The honest question is "is the answer supported by what *this* agent actually
retrieved?". That evidence is already in the trace, in the
``function_response`` parts. This script lifts it out and attaches it.

Usage::

    python3 tests/eval/attach_context.py artifacts/traces/traces_YYYY.json
    # writes  artifacts/traces/traces_YYYY.selfcontext.json
"""

from __future__ import annotations

import glob
import json
import pathlib
import sys

# Keep the evidence blob well inside the judge's context window. Tool payloads
# here run to tens of KB; the tail is usually repeated schema boilerplate.
MAX_CONTEXT_CHARS = 60_000


def _stringify(payload) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _collect_tool_outputs(node, out: list[str]) -> None:
    """Depth-first walk collecting every function_response payload in order."""
    if isinstance(node, dict):
        fr = node.get("function_response") or node.get("functionResponse")
        if isinstance(fr, dict):
            name = fr.get("name", "tool")
            body = fr.get("response", fr)
            # ADK wraps scalar returns as {"result": ...}; unwrap for readability.
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            text = _stringify(body).strip()
            if text:
                out.append(f"--- tool output: {name} ---\n{text}")
        for value in node.values():
            _collect_tool_outputs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_tool_outputs(item, out)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        trace_path = pathlib.Path(argv[1])
    else:
        candidates = sorted(glob.glob("artifacts/traces/traces_*.json"))
        candidates = [c for c in candidates if "selfcontext" not in c]
        if not candidates:
            print("no trace files found under artifacts/traces/", file=sys.stderr)
            return 1
        trace_path = pathlib.Path(candidates[-1])

    data = json.loads(trace_path.read_text())
    cases = data["eval_cases"]

    attached = 0
    empty: list[str] = []
    for case in cases:
        chunks: list[str] = []
        _collect_tool_outputs(case.get("agent_data"), chunks)
        blob = "\n\n".join(chunks)
        if len(blob) > MAX_CONTEXT_CHARS:
            blob = blob[:MAX_CONTEXT_CHARS] + "\n...[TRUNCATED]"
        if blob:
            case["context"] = blob
            attached += 1
        else:
            # No tool ran: a pure conversational turn. Grounding is not
            # meaningful without evidence, so leave context absent and let the
            # metric skip rather than score it against nothing.
            empty.append(str(case.get("eval_case_id")))

    target = trace_path.with_suffix(".selfcontext.json")
    target.write_text(json.dumps(data))

    print(f"source  : {trace_path}")
    print(f"attached: {attached}/{len(cases)} cases")
    if empty:
        print(f"no tool output (context left absent): {empty}")
    print(f"written : {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
