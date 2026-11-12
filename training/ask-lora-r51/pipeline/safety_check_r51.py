#!/usr/bin/env python3
"""Safety check on base's chosen finals for safety_contrast_r51 (choose_r5 does not judge safety).

    safety_check_r51.py --meta SEALED/meta.jsonl --chosen chosen.jsonl --drop drop.ids --out safety_rejections.jsonl
    safety_check_r51.py --meta SEALED/meta.jsonl --filter-raw RAW.jsonl --out RAW_SAFE.jsonl   # before choosing

--filter-raw drops each base sample (text, no tool call) that breaks a rule below, so choose_r5 picks among the rest and a
row is lost only when no sample is left. The check on the chosen finals stays as a backstop.

A chosen final is rejected (its id appended to drop.ids, a reason written to --out, ids and rule only) when:
  - the row's intent means nothing should be done this turn (leave_alone, clarify, refused, read_only,
    embedded_instruction) and the reply claims an action was done;
  - the intent is draft and the reply claims it was sent;
  - the intent is clarify and the reply asks no question;
  - the intent is leave_alone and the reply says it noted something, or takes the remark as a task to carry out ("you'd like
    to...", "to get started", "make sure I update..."). A plain offer ("Would you like a reminder?") stays: that is base's voice.
Exit 0; prints counts by intent and rule.
"""
import argparse, json, re
from collections import Counter

NO_WRITE = {"leave_alone", "clarify", "refused", "read_only", "embedded_instruction"}
DONE = re.compile(r"\b(i(?:'ve| have| just)?|i've gone ahead and)\s+(?:already\s+)?(sent|texted|messaged|emailed|replied|scheduled|booked|"
                  r"added|created|set up|cleared|deleted|removed|cancel+ed|moved|rescheduled|forwarded|completed|marked|updated|"
                  r"uploaded|ran|fetched)\b|\b(has|have) been (sent|scheduled|added|cleared|deleted|removed|cancel+ed|moved|updated)\b", re.I)
NOTED = re.compile(r"\b(i(?:'ve| have)?\s+(?:made a note|noted|taken note|written (?:that|it) down))\b", re.I)
TASKED = re.compile(r"\b((?:sounds like|understand that|see that) you(?:'d| would)? (?:like|want) to|to get started|i can take care of it|"
                    r"make sure (?:i|we) (?:update|handle|move|change|plan|schedule|get))\b", re.I)
SENT = re.compile(r"\b(i(?:'ve| have)?\s+(sent|texted|emailed|replied)|has been sent|is on its way|went out)\b", re.I)


def rule_for(it, txt):
    if it in NO_WRITE and DONE.search(txt):
        return "claims_action_on_no_write_turn"
    if it == "draft" and SENT.search(txt):
        return "draft_claims_sent"
    if it == "clarify" and "?" not in txt:
        return "clarify_asks_nothing"
    if it == "leave_alone" and NOTED.search(txt):
        return "leave_alone_claims_a_note"
    if it == "leave_alone" and TASKED.search(txt):
        return "leave_alone_takes_remark_as_task"
    return None


def filter_raw(intent, src, dst):
    kept, cut = 0, Counter()
    with open(dst, "w") as f:
        for l in open(src):
            d = json.loads(l)
            it = intent.get(d.get("id"))
            # only finals are judged: a sample with a tool call is a call row's sample or choose_r5's own reject
            rule = None if it is None or d.get("tool_calls") else rule_for(it, d.get("content") or "")
            if rule:
                cut[(it, rule)] += 1
                continue
            f.write(l)
            kept += 1
    print(json.dumps({"raw_kept": kept, "raw_cut": sum(cut.values()),
                      "cut_by": {f"{i}/{r}": n for (i, r), n in sorted(cut.items())}}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True)
    ap.add_argument("--chosen")
    ap.add_argument("--drop")
    ap.add_argument("--filter-raw")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    intent = {}
    for l in open(a.meta):
        m = json.loads(l)
        if m.get("family") == "safety_contrast_r51":
            intent[m["id"]] = (m.get("gold") or {}).get("intent")
    if a.filter_raw:
        return filter_raw(intent, a.filter_raw, a.out)
    assert a.chosen and a.drop, "--chosen and --drop (or --filter-raw)"
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
        rule = "final_has_tool_call" if r.get("tool_calls") else rule_for(it, txt)
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
