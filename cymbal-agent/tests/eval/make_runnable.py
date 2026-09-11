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

"""Project a completed eval trace back down to an inference-ready dataset.

Why this exists
---------------
The dataset shipped with the lab (`basic-dataset.json`) is not an input dataset --
it is a finished run. Each case carries all three of:

  * ``prompt``              the user message,
  * ``responses``           the reference agent's final answer,
  * ``agent_data.turns``    the recorded function_call / function_response events.

`agents-cli eval generate` refuses that shape outright::

    Case has both top-level 'prompt' and agent_data.turns; ambiguous.

and it is right to: with both present there is no way to tell whether you meant
"run this prompt" or "continue this conversation".

Grading the file untouched is possible, but it scores the *reference*
implementation that produced the traces -- the recorded citations point at the
lab author's GCS bucket, not ours. That measures someone else's agent. To make
the quality gate mean something for this repo, we keep the prompts, drop the
recorded outputs, and re-run inference against the live agent.

The pristine file is left byte-for-byte as delivered so the provenance of the
benchmark stays auditable.

Usage::

    python3 tests/eval/make_runnable.py
"""

from __future__ import annotations

import json
import pathlib
import sys

DATASETS = pathlib.Path(__file__).parent / "datasets"
SOURCE = DATASETS / "basic-dataset.json"
TARGET = DATASETS / "basic-dataset.runnable.json"

# Only these survive the projection. Anything else is recorded output.
#
# `context` stays: GROUNDING is a reference-based metric and needs the retrieved
# evidence to grade against. Without it the Vertex eval service rejects the whole
# grade step with:
#   400 INVALID_ARGUMENT - Error rendering metric prompt template:
#   Variable context is required but not provided.
# which fails the run outright rather than just skipping one metric.
#
# `responses` and `agent_data` do NOT stay -- those are the reference agent's
# answer and trajectory, and keeping either makes the case ambiguous for
# inference. Context is evidence; responses are answers. Only the answers have
# to go.
INFERENCE_KEYS = ("eval_case_id", "description", "prompt", "context")

HEADER = [
    "DERIVED FILE - do not edit by hand. Regenerate with:",
    "  python3 tests/eval/make_runnable.py",
    "",
    "The lab ships basic-dataset.json as a COMPLETED trace: every case carries a",
    "top-level `prompt` AND `responses` AND `agent_data.turns` holding the recorded",
    "function_call / function_response events from the reference implementation.",
    "agents-cli 1.5.0 rejects that shape for inference with:",
    "  'Case has both top-level prompt and agent_data.turns; ambiguous.'",
    "",
    "Grading the file as-is would score the REFERENCE agent that produced those",
    "traces, not ours - the recorded citations even point at the lab author's GCS",
    "bucket. To make the quality gate measure our agent, this projection keeps only",
    "the prompt and re-runs inference live.",
]


def main() -> int:
    if not SOURCE.exists():
        print(f"missing source dataset: {SOURCE}", file=sys.stderr)
        return 1

    payload = json.loads(SOURCE.read_text())
    cases = payload.get("eval_cases") or []
    if not cases:
        print(f"{SOURCE} contains no eval_cases", file=sys.stderr)
        return 1

    projected = []
    for case in cases:
        slim = {k: v for k, v in case.items() if k in INFERENCE_KEYS}
        if "prompt" not in slim:
            print(
                f"case {case.get('eval_case_id')!r} has no top-level prompt; "
                "it is a continued-conversation case and cannot be projected.",
                file=sys.stderr,
            )
            return 1
        projected.append(slim)

    TARGET.write_text(
        json.dumps({"_comment": HEADER, "eval_cases": projected}, indent=2) + "\n"
    )
    dropped = sorted({k for c in cases for k in c} - set(INFERENCE_KEYS))
    print(f"wrote {TARGET.relative_to(pathlib.Path.cwd())}  ({len(projected)} cases)")
    print(f"dropped recorded-output keys: {dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
