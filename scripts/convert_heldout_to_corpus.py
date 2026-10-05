#!/usr/bin/env python3
"""Convert the frozen comparison held-out set into routing-quality corpus form.

Deterministic and reproducible: the held-out file must match its recorded
sha256, and the output is written with sorted keys. Rules:
  * intent chat/clarify -> expect chat/clarify, nothing may be forced;
  * otherwise expect action, need = [acceptable first tools] (any one reachable);
  * forbid committing sends unless the gold intent is a send, plus
    permanent/bulk deletes always.

    python scripts/convert_heldout_to_corpus.py            # writes the fixture
    python scripts/score_routing_quality.py \
        --corpus test_fixtures/routing/comparison_heldout_as_corpus.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "test_fixtures/routing/comparison_heldout.json"
OUT = ROOT / "test_fixtures/routing/comparison_heldout_as_corpus.json"
SENDS = ["send_email", "send_message", "reply_to_email", "forward_email", "schedule_send", "place_call"]
DELETES = ["delete_path", "trash_file", "clear_memory", "clear_reminders", "clear_past_reminders"]


def convert() -> dict:
    raw = SRC.read_bytes()
    expected = SRC.with_suffix(".sha256").read_text().split()[0]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("held-out set changed after it was frozen")
    cases = []
    for c in json.loads(raw)["cases"]:
        expect = {"chat": "chat", "clarify": "clarify"}.get(c["intent"], "action")
        send = c["intent"] in ("mail_send", "messages_send")
        cases.append(dict(
            id=c["id"], domain=c["category"], styles=[], prompt=c["prompt"],
            last_user=c["last_user"], last_assistant=c["last_assistant"], last_tools=c["last_tools"],
            expect=expect, need=[sorted(c["first"])] if c["first"] and expect == "action" else [],
            force=None if expect == "action" else [],
            forbid=sorted(DELETES if send else SENDS + DELETES), avoid=[]))
    return {"source_sha256": expected, "cases": cases}


def main() -> int:
    data = json.dumps(convert(), indent=1, sort_keys=True) + "\n"
    OUT.write_text(data)
    OUT.with_suffix(".sha256").write_text(f"{hashlib.sha256(data.encode()).hexdigest()}  {OUT.relative_to(ROOT)}\n")
    print(OUT.with_suffix(".sha256").read_text().strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
