"""Prove validate.py catches each problem it claims to: corrupt a real example
one way at a time and check the matching problem is reported.

Usage: python selftest.py   (needs train.jsonl; exit 1 if any corruption slips through)
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

import leak
import validate as V

OUT = V.OUT


def first(pred):
    for line in open(os.path.join(OUT, "train.jsonl")):
        ex = json.loads(line)
        if pred(ex):
            return ex
    raise SystemExit("no example matches the selftest's precondition")


def target_call(ex):
    tc = ex["messages"][-1].get("tool_calls")
    return tc[0] if tc else None


def user(ex):
    return next(m for m in ex["messages"] if m["role"] == "user")


def corrupt():
    call = first(lambda e: target_call(e) and target_call(e)["function"]["name"] == "get_upcoming"
                 and any(t["function"]["name"] == "get_upcoming" and "description" in t["function"] for t in e["tools"]))
    final = first(lambda e: not target_call(e) and any(m["role"] == "tool" for m in e["messages"]))
    inj = first(lambda e: not target_call(e) and any(V.INJECTION_MARKERS.search(s) and V._PAYLOAD.search(s)
                                                     for m in e["messages"] if m["role"] == "tool"
                                                     for s in str(m.get("content")).split("\n")))
    cases = {}

    e = copy.deepcopy(call)
    target_call(e)["function"]["name"] = "send_email"
    cases["call_not_offered"] = e

    e = copy.deepcopy(call)
    e["tools"].append(V.REGISTRY["send_email"].schema())
    target_call(e)["function"].update(name="send_email", arguments=json.dumps({"to": "a@example.com", "subject": "x", "body": "y"}))
    cases["write_call"] = e

    e = copy.deepcopy(call)
    target_call(e)["function"]["arguments"] = json.dumps({"bogus": 1})
    cases["args_schema(target)"] = e

    e = copy.deepcopy(call)
    for t in e["tools"]:
        if t["function"]["name"] == "get_upcoming":
            t["function"]["description"] += " (stale)"
    cases["live_schema"] = e

    e = copy.deepcopy(call)
    user(e)["content"] = leak.load_eval()[0]["prompt"]
    cases["leak:eval_question"] = e

    e = copy.deepcopy(inj)
    tool_text = next(m["content"] for m in e["messages"] if m["role"] == "tool" and V.INJECTION_MARKERS.search(str(m["content"])))
    payload = next(p for s in tool_text.split("\n") if V.INJECTION_MARKERS.search(s) for p in V._PAYLOAD.findall(s))
    e["messages"][-1]["content"] += f" Done — {payload.strip('`')}."
    cases["injection:payload_in_reply"] = e

    e = copy.deepcopy(final)
    e["messages"][-1]["content"] += " Write to someone@realmail-provider.com."
    cases["privacy:email_domain"] = e

    e = copy.deepcopy(final)
    e["messages"][-1]["content"] += " Saved to /Users/jdoe/Desktop."
    cases["privacy:home_path"] = e

    e = copy.deepcopy(final)
    e["messages"][-1]["content"] += " <|tool_call>call:get_upcoming{}<tool_call|>"
    cases["markup_in_content"] = e

    e = copy.deepcopy(final)
    e["messages"] = [m for m in e["messages"] if m["role"] != "tool"]
    cases["call_without_tool_result"] = e
    return cases


def main():
    leaks, ident = leak.LeakIndex(), V.identity_tokens()
    missed = []
    for want, ex in corrupt().items():
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        _, problems, _ = V.validate_file(f.name, leaks, ident, None, V.PUBLIC_WORDS)
        os.unlink(f.name)
        ok = want in problems
        print(f"{'caught' if ok else 'MISSED'}  {want:28s} reported: {dict(problems)}")
        if not ok:
            missed.append(want)
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
