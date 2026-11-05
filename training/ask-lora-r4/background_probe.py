"""Do a model's background calls finish? Run for base and each tuned model at the gate.

Toolbench can't answer this. Its email digest is a deterministic stand-in and the
model-led loop never reads `no_thinking_capable`, so a tuned model missing from that
list scores the same there while its digests, message topics and Brief fail live.

This sends the production prompts, budgets and thinking settings to a running engine.
Inputs are synthetic. Bars for a tuned model, as rostered (no flag):
  - every call ends with finish_reason=stop and non-empty text
  - message topics and memory capture return valid JSON
  - digest recall is within 5 points of base, and no digest fails

    .venv/bin/python training/ask-lora-r4/background_probe.py --model M --api-key K --out f.json
    --unlisted reproduces a model missing from the list, which is how a Settings pick runs.

Set WISP_HOME to an empty directory first. Quit Wisp first at a gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TOPICS = {"0": ["dinner on Saturday", "rent split", "flight times", "ok"],
          "1": ["lab report", "study group", "parking"]}
MESSAGES = ("[Mon 9:12] Sam: can you bring the charger to dinner on Saturday?\n[Mon 9:30] You: sure\n"
            "[Tue 14:02] Priya: lab report moved to Thursday noon\n"
            "[Tue 18:44] Mom: call me about the flight on the 20th")
CAPTURE = "I'm allergic to peanuts. Also I just moved to Portland last month."
DOC = ("Source 1: The city library opens at 9am on weekdays and 10am on Saturdays. It is closed Sundays.\n\n"
       "Question: when does the library open on Saturday?")


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


async def run(args) -> dict:
    from scripts.long_email_digest_recall import EMAILS, HTTPClient, recall
    from service import config
    from service.assistant.brief import _MSG_SYS, _USER_FACING_SUMMARY_MAX_TOKENS
    from service.memory.capture import EXTRACTION_SYSTEM
    from service.search.synth import _SHORT_DOC_MAX_TOKENS, _SYSTEM as SYNTH
    from service.tools import email_digest as digest
    from service.tools.imessage_tools import _TOPIC_SYS

    model = args.model
    listed = [m for m in config.no_thinking_capable() if m != model]
    config.no_thinking_capable = lambda: listed + ([] if args.unlisted else [model])
    client = HTTPClient(args.base_url, args.api_key)
    summary = config.user_facing_summary_kwargs(model)
    off = config.no_thinking_kwargs(model)
    calls = [
        ("message_topics", _TOPIC_SYS, json.dumps(TOPICS),
         config.user_facing_summary_budget(model, 600), summary, True),
        ("brief_messages", _MSG_SYS, MESSAGES,
         config.user_facing_summary_budget(model, _USER_FACING_SUMMARY_MAX_TOKENS), summary, False),
        ("memory_capture", EXTRACTION_SYSTEM, CAPTURE, 650, off, True),
        ("search_synth", SYNTH, DOC, _SHORT_DOC_MAX_TOKENS, off, False),
    ]
    rows = []
    for name, system, user, budget, extra, wants_json in calls:
        started = time.monotonic()
        reply = await client.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  max_tokens=budget, temperature=0, **extra)
        choice = reply["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        usage = reply.get("usage") or {}
        rows.append({"case": name, "finish": choice.get("finish_reason"), "budget": budget,
                     "tokens": usage.get("completion_tokens"),
                     "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                     "chars": len(text), "seconds": round(time.monotonic() - started, 1),
                     "ok": choice.get("finish_reason") == "stop" and bool(text) and (not wants_json or _is_json(text))})
        print(json.dumps(rows[-1]), flush=True)
    got = total = failed = 0
    for rep in range(args.reps):
        for email in EMAILS:
            digest._cache.clear()
            started = time.monotonic()
            text, outcome = await digest._digest_outcome(
                {"sender": email["sender"], "subject": email["subject"], "body": email["body"]},
                client=client, model=model)
            hits, _ = recall(text, email["facts"])
            failed += outcome == "failed"
            if outcome == "digest":  # a failed digest returns the raw email, which recalls everything
                got, total = got + hits, total + len(email["facts"])
            print(json.dumps({"case": "email_digest", "email": email["id"], "rep": rep, "outcome": outcome,
                              "recall": f"{hits}/{len(email['facts'])}",
                              "seconds": round(time.monotonic() - started, 1)}), flush=True)
    return {"model": model, "listed": not args.unlisted, "calls": rows, "digests_failed": failed,
            "digest_recall": round(100 * got / total, 1) if total else None,
            "all_calls_ok": all(r["ok"] for r in rows) and not failed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=os.environ.get("OMLX_API_KEY"))
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--unlisted", action="store_true", help="run as a model missing from no_thinking_capable")
    parser.add_argument("--out")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({k: v for k, v in result.items() if k != "calls"}))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main()
