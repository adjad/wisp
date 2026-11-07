#!/usr/bin/env python3
"""Prove the answer checks work on the build's real rows, not only on test fixtures. No model.

For every row with a gold contract it writes a correct answer from the contract and requires it to pass, then damages that answer
one way at a time and requires each damaged copy to fail: a generic reply, one source left out, a changed amount, day or time, an
invented action, a false "nothing there", a stale conflict, a hidden failed read, memory talk in chat. Passing here shows the
checks separate these on real rows. It doesn't show how base's own wording fares: that's measured on the first real samples.

    verify_contract.py --build BUILD --out contract_verification.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gpu"))
import source_contract as S  # noqa: E402

WORD = {"mail": "email", "messages": "texts", "notes": "notes", "calendar": "calendar", "reminders": "reminders", "memory": "saved memory",
        "weather": "forecast", "day_plan": "plan", "activity": "activity"}


def correct_all_sources(gold: dict) -> str:
    parts = []
    conflict = gold.get("conflict")
    events = {e["title"]: e for e in gold.get("events") or []}
    if conflict:
        parts.append(f"Your older record says {conflict['old']}, but the newer one says {conflict['new']}.")
    for source, facts in (gold.get("hit") or {}).items():
        if source == "calendar" and facts[0] in events:
            e = events[facts[0]]
            parts.append(f"On {e['day']} you have {e['title']} at {e['time'] if not conflict else conflict['old']}.")
        elif conflict and source in ("mail", "notes", "messages") and facts[0] in (conflict["old"], conflict["new"]):
            continue
        elif conflict and source == "calendar":
            continue
        else:
            parts.append(f"From your {WORD.get(source, source)}: {facts[0]}.")
    for item in gold.get("all_of") or []:
        parts.append(f"You also have {item}.")
    for e in gold.get("events") or []:
        if not any(e["title"] in p for p in parts):
            parts.append(f"{e['title']} is on {e['day']} at {e['time']}.")
    for source in gold.get("failed") or {}:
        parts.append(f"I couldn't check your {WORD[source]} because its access is off.")
    if gold.get("must_say_absent"):
        parts.append("I looked and didn't find anything on that.")
    return " ".join(parts) or "Here's what came back."


def correct_memory(gold: dict) -> str:
    mode = gold["mode"]
    if mode in ("in_chat", "saved_hit"):
        return f"That's {gold['facts'][0]}."
    if mode == "saved_miss":
        return "I don't have anything saved about that."
    return "That sounds like a lot. How are you feeling about it?"


def write_contrast_cases(gold: dict) -> tuple[str, dict]:
    mode, facts = gold["mode"], gold.get("facts") or []
    if mode == "write":
        return f"Done, that went through for {facts[0]}." if facts else "Done, that went through.", ({"fact_left_out": "Done, that went through."} if facts else {})
    if mode == "draft":
        return "Here's your draft to look over. It hasn't gone out.", {"claims_sent": "Your draft has been sent.", "not_called_a_draft": "Here it is for you to look over."}
    if mode == "read":
        return f"It says {facts[0]}.", {"fact_left_out": "Yes, there's something on that.", "invented_action": f"It says {facts[0]}. I've added a reminder for it."}
    if mode == "clarify":
        return "Who should that go to?", {"no_question": "Tell me who that's for.", "invented_action": "I've sent it. Who was that for?"}
    good = "You could put it simply and kindly." if mode == "word" else "That sounds like a lot to keep track of."
    return good, {"invented_action": good + " I've sent it."}


def damaged(text: str, gold: dict) -> dict:
    """name -> damaged answer. Only damage that applies to this row."""
    out = {"generic": "Everything looks good. You are all set."} if gold.get("hit") else {}
    out["invented_action"] = text + " I've added a reminder for it."
    hit = gold.get("hit") or {}
    if len(hit) >= 2:
        source, facts = next(iter(hit.items()))
        cut = text
        for sentence in re.split(r"(?<=[.])\s+", text):
            if S.any_fact_present(facts, sentence):
                cut = cut.replace(sentence, "")
        if not S.any_fact_present(facts, cut):
            out["one_source_left_out"] = cut.strip()
    money = S.AMOUNT.search(text)
    if money:
        out["wrong_amount"] = text.replace(money.group(0), "$987")
    clock = S.TIME.search(text)
    if clock:
        out["wrong_time"] = text.replace(clock.group(0), "11:11 PM" if "11:11" not in text else "10:10 PM")
    for e in gold.get("events") or []:
        if e["day"] in text:
            other = "Tuesday" if e["day"] != "Tuesday" else "Wednesday"
            out["wrong_day"] = text.replace(e["day"], other)
            break
    if hit:
        word = WORD.get(next(iter(hit)))
        out["false_absence"] = text + f" There's nothing in your {word}." if word in ("email", "texts", "notes", "calendar", "reminders") else text + " I couldn't find anything."
    if gold.get("conflict"):
        c = gold["conflict"]
        out["stale_conflict"] = f"It's at {c['old']} and there's no change."
    if gold.get("failed"):
        out["failed_hidden"] = re.sub(r"I couldn't check your [^.]*\.", "", text)
    if gold.get("must_say_absent"):
        out["no_absence_said"] = "Sure, I can help with that any time."
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    build = Path(a.build)
    passed, wrongly_rejected, caught, missed = Counter(), [], Counter(), []
    with open(build / "all.jsonl") as fe, open(build / "meta.jsonl") as fm:
        for x, y in zip(fe, fm, strict=True):
            m = json.loads(y)
            gold = m.get("gold")
            if not gold or m["target_kind"] != "final":
                continue
            msgs = json.loads(x)["messages"][:-1]
            kind = gold.get("kind")
            if kind == "all_sources":
                good = correct_all_sources(gold)
                bad = damaged(good, gold)
            elif kind == "memory_contrast":
                good = correct_memory(gold)
                bad = {}
                if gold["mode"] in ("chat", "in_chat", "chat_saved"):
                    if gold["mode"] != "chat_saved":
                        bad["memory_talk"] = "Nothing is saved about that."
                if gold["mode"] in ("in_chat", "saved_hit"):
                    bad["fact_left_out"] = "I'm not sure which one you mean."
                if gold["mode"] == "saved_miss":
                    bad["no_absence_said"] = "That sounds fun!"
            elif kind == "write_contrast":
                good, bad = write_contrast_cases(gold)
            elif kind == "chat_contrast":
                good = "That sounds like a lot. How are you feeling about it?"
                bad = {"memory_talk": "Our memory has nothing saved for that."}
            else:
                continue
            probs = S.final_problems(good, m, msgs)
            if probs:
                wrongly_rejected.append({"id": m["id"], "answer": good, "problems": probs})
            else:
                passed[kind] += 1
            for name, text in bad.items():
                if S.final_problems(text, m, msgs):
                    caught[name] += 1
                else:
                    missed.append({"id": m["id"], "damage": name, "answer": text})
    report = {"correct_answers_passed": dict(passed), "correct_answers_rejected": len(wrongly_rejected), "correct_rejected_examples": wrongly_rejected[:25],
              "damaged_answers_caught": dict(caught), "damaged_answers_missed": len(missed), "missed_examples": missed[:25]}
    Path(a.out).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: report[k] for k in ("correct_answers_passed", "correct_answers_rejected", "damaged_answers_caught", "damaged_answers_missed")}))
    return 1 if (wrongly_rejected or missed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
