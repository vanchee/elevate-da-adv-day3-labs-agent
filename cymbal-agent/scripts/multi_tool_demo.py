"""Four-tool demo turn — a single question that cannot be answered by any
one backend, driven end to end so the telemetry is real.

Tool coverage forced by the prompt:
  1. resolve_store_identifier      — "our Paris flagship" is not an ID
  2. pos_troubleshooting_rag_tool  — ERR-PAY-4001 lives in a vendor PDF
  3. cymbal_analytics_tool         — transaction count lives in BigQuery
  4. read_cashier_realtime_alerts  — live alerts live in Bigtable

"that store" in clause 2 is load-bearing: it makes the analytics call
depend on the resolution result rather than running independently.
Substituting an explicit STORE_007 skips resolution and loses a tool.

Tagged with a distinctive user_id so the resulting rows are trivial to
isolate in the BigQuery Console. See sql/06_telemetry_query_recipes.sql
for the companion trace queries.

Usage:  ./.venv/bin/python scripts/multi_tool_demo.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# scripts/ -> cymbal-agent/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    os.environ.setdefault(key.strip(), value.strip())


from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from app.agent import app as adk_app  # noqa: E402

USER_ID = "console-demo"

PROMPT = (
    "Our Paris flagship just hit error ERR-PAY-4001 on a checkout lane. "
    "Give me the recovery procedure, tell me how many POS transactions that "
    "store has on record, and check whether cashier CASH_1190 has any live "
    "alerts right now."
)


async def main() -> int:
    print(f"plugins attached: {[p.name for p in adk_app.plugins]}")
    if not adk_app.plugins:
        print("!! no telemetry plugin attached")
        return 1

    runner = InMemoryRunner(app=adk_app)
    session = await runner.session_service.create_session(
        app_name=adk_app.name, user_id=USER_ID
    )

    print(f"\nsession_id : {session.id}")
    print(f"user_id    : {USER_ID}")
    print(f"\nPROMPT:\n{PROMPT}\n")
    print("=" * 72)

    start = time.monotonic()
    text_parts: list[str] = []
    # Order matters for the demo narrative, so keep first-seen order rather
    # than a set. SSE emits each function_call twice (partial + aggregated);
    # the tool still runs once, so dedupe on first sight.
    tool_order: list[str] = []
    invocation_ids: set[str] = set()

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=PROMPT)]),
    ):
        if getattr(event, "invocation_id", None):
            invocation_ids.add(event.invocation_id)
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            fc = getattr(part, "function_call", None)
            if fc and fc.name not in tool_order:
                tool_order.append(fc.name)
                print(f"  [{time.monotonic() - start:6.1f}s] tool -> {fc.name}")
            if part.text and event.author != "user":
                text_parts.append(part.text)

    elapsed = time.monotonic() - start
    print("=" * 72)
    print(f"\nlatency        : {elapsed:.1f}s")
    print(f"distinct tools : {len(tool_order)}")
    print(f"dispatch order : {' -> '.join(tool_order)}")
    print(f"invocation_id  : {sorted(invocation_ids)}")

    answer = "".join(text_parts).strip()
    print(f"\n--- ANSWER ({len(answer)} chars) ---\n{answer}\n")

    plugin = adk_app.plugins[0]
    print("flushing telemetry...")
    for attempt in range(3):
        try:
            await plugin.flush()
            break
        except Exception as exc:
            print(f"  flush attempt {attempt + 1} failed: {exc}")
            await asyncio.sleep(2)
    await asyncio.sleep(6)

    drops = {k: v for k, v in plugin.get_drop_stats().items() if v}
    print(f"dropped rows   : {drops or 'none'}")

    try:
        await plugin.shutdown()
    except Exception as exc:
        print(f"shutdown warning: {exc}")

    print("\ndone")
    return 0 if len(tool_order) >= 3 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
