#!/usr/bin/env python3
"""The extra distill rows (audit chatstyle-1 / thinking-1), chosen by prompt so a rebuild that shifts ids keeps them:
knowledge_direct step-0 general-knowledge and small-talk finals, except the pure unit, date and rounding lookups,
which keep their scripted one-liners; and the six concise_gentle small-talk rows.

    select_extra.py BUILD/partial.jsonl > extra.ids
"""
import json
import sys

KEEP_SCRIPTED = {"weeks per year, how many", "ounces per pound?", "feet per mile?", "cups per quart?",
                 "minutes per day, how many", "3.14159 rounded to 2 decimals"}
SMALL_TALK = {"hi hi", "sup", "so tired", "okay", "haha", "forget it"}


def main():
    n = 0
    for r in map(json.loads, open(sys.argv[1])):
        m = r["meta"]
        p = (m.get("prompt") or "").strip().lower()
        first_final = m.get("step") == 0 and m.get("step_kind") == "answer_first" and m.get("target_kind") == "final"
        if (m["family"] == "knowledge_direct" and first_final and p not in KEEP_SCRIPTED) or \
                (m["family"] == "concise_gentle" and first_final and p in SMALL_TALK):
            print(m["id"])
            n += 1
    print(f"{n} extra ids", file=sys.stderr)


if __name__ == "__main__":
    main()
