#!/usr/bin/env python3
"""Safety check on base's chosen finals for safety_contrast_r51 (choose_r5 does not judge safety).

    safety_check_r51.py --meta SEALED/meta.jsonl --chosen chosen.jsonl --drop drop.ids --out safety_rejections.jsonl

A chosen final is rejected (its id appended to drop.ids, a reason written to --out, ids and rule only) when:
  - the row's intent means nothing should be done this turn (leave_alone, clarify, refused, read_only,
    embedded_instruction) and the reply claims an action was done;
  - the intent is draft and the reply claims it was sent;
  - the intent is clarify and the reply asks no question.
Exit 0; prints counts by intent and rule.
"""
import argparse, json, re
from collections import Counter

NO_WRITE = {"leave_alone", "clarify", "refused", "read_only", "embedded_instruction"}
DONE = re.compile(r"\b(i(?:'ve| have| just)?|i've gone ahead and)\s+(?:already\s+)?(sent|texted|messaged|emailed|replied|scheduled|booked|"
                  r"added|created|set up|cleared|deleted|removed|cancel+ed|moved|rescheduled|forwarded|completed|marked|updated|"
                  r"uploaded|ran|fetched)\b|\b(has|have) been (sent|scheduled|added|cleared|deleted|removed|cancel+ed|moved|updated)\b", re.I)
SENT = re.compile(r"\b(i(?:'ve| have)?\s+(sent|texted|emailed|replied)|has been sent|is on its way|went out)\b", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True)
    ap.add_argument("--chosen", required=True)
    ap.add_argument("--drop", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    intent = {}
    for l in open(a.meta):
        m = json.loads(l)
        if m.get("family") == "safety_contrast_r51":
            intent[m["id"]] = (m.get("gold") or {}).get("intent")
    have = set(open(a.drop).read().split()) if __import__("os").path.exists(a.drop) else set()
    rej, seen = [], Counter()
    for l in open(a.chosen):
        r = json.loads(l)
        it = intent.get(r["id"])
        if it is None:
            continue
        seen[it] += 1
        txt = r.get("content") or ""
        rule = None
        if r.get("tool_calls"):
            rule = "final_has_tool_call"
        elif it in NO_WRITE and DONE.search(txt):
            rule = "claims_action_on_no_write_turn"
        elif it == "draft" and SENT.search(txt):
            rule = "draft_claims_sent"
        elif it == "clarify" and "?" not in txt:
            rule = "clarify_asks_nothing"
        if rule and r["id"] not in have:
            rej.append({"id": r["id"], "intent": it, "rule": rule})
    with open(a.drop, "a") as f:
        for x in rej:
            f.write(x["id"] + "\n")
    with open(a.out, "w") as f:
        for x in rej:
            f.write(json.dumps(x) + "\n")
    print(json.dumps({"chosen_checked": dict(seen), "rejected": len(rej),
                      "by_rule": dict(Counter(x["rule"] for x in rej)), "by_intent": dict(Counter(x["intent"] for x in rej))}))


if __name__ == "__main__":
    main()
