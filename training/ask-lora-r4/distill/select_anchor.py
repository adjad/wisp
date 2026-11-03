#!/usr/bin/env python3
"""The anchor rows: FINAL steps of the families r4 does not set out to change, so their target becomes the untuned
base's own reply (meta.distill_kind "anchor_final"). Tool-call steps stay scripted: the base has to reproduce the
same calls to get the same results.

- NARRATE: the final after the turn's reads (step > 0, nothing written in the turn) in the read families.
- SAFETY: every final of a turn that wrote nothing in the safety families: the step-0 declines and asks, and the
  read-then-decline finals. Finals after a write (receipts, a denied card) stay scripted.
- RECEIPT: the write-receipt and narrate finals (step > 0) of the unchanged write families, and the
  narrate-after-write finals of contacts_r3, device_r3 and lookup_answers_r4. Only the wording of the final is
  anchored; the write calls stay scripted.

    select_anchor.py BUILD/partial.jsonl > anchor.ids
"""
import json
import sys
from collections import Counter

NARRATE = ("right_source", "multi_source", "grounded_only", "mail_r3", "web_r3", "contacts_r3", "device_r3",
           "weather_and_web", "concise_gentle", "capability_honest", "recover_from_tool_error", "info_answers_r4",
           "lookup_answers_r4")
SAFETY = ("safe_effects", "safe_restraint", "ignore_embedded_instructions", "no_write_for_reads", "restraint_r4")
RECEIPT = ("reminders_r3", "finish_the_write", "right_write_tool", "act_dont_ask", "act_dont_promise", "exact_args")
NARRATE_AFTER_WRITE = ("contacts_r3", "device_r3", "lookup_answers_r4")


def anchor_set(meta: dict) -> str | None:
    """Which anchor set a row is in, or None."""
    if meta.get("target_kind") != "final":
        return None
    wrote = bool(meta.get("turn_writes") or meta.get("writes"))
    later = meta.get("step", 0) > 0
    if meta["family"] in RECEIPT or (meta["family"] in NARRATE_AFTER_WRITE and wrote):
        return "receipt" if later else None
    if wrote:
        return None
    if meta["family"] in NARRATE:
        return "narrate" if later else None
    return "safety" if meta["family"] in SAFETY else None


def is_anchor(meta: dict) -> bool:
    return anchor_set(meta) is not None


def main():
    n, by_fam, by_set, left = 0, Counter(), Counter(), Counter()
    for r in map(json.loads, open(sys.argv[1])):
        m = r["meta"]
        if is_anchor(m):
            print(m["id"])
            n += 1
            by_fam[m["family"]] += 1
            by_set[anchor_set(m)] += 1
        elif m["family"] in NARRATE + SAFETY + RECEIPT and m.get("target_kind") == "final":
            left[m["family"]] += 1
    print(f"{n} anchor ids {dict(by_set)} {dict(by_fam)}; finals left scripted {dict(left)}", file=sys.stderr)


if __name__ == "__main__":
    main()
