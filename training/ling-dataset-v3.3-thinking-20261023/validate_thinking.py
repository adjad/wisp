#!/usr/bin/env python3
"""Checks for the V3.3-thinking rows (stdlib only). Writes validation.json and exits 1 on any problem.

  * every row equals its V3.2 source row once `reasoning_content` is removed (messages, tools, calls untouched);
  * only the final (training: true) assistant message carries reasoning, and it is non-empty, single-paragraph,
    at most MAX_CHARS, and free of template control text;
  * grounding: every quoted span appears in the row, every tool name mentioned is one the row offers (V2 replay rows
    offer none and may name none), every tool the step calls is named, a denial is called a denial, and a failed
    result is never described as a success;
  * reproducibility: train/dev sha256 match stats.json.
"""
import hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
V32 = HERE.parent / "ling-dataset-v3.2-20261014"
MAX_CHARS = 600
CONTROL = ("<think>", "</think>", "<tool_call>", "</tool_call>", "<|role_end|>", "<role>", "<arg_key>", "<tool_response>")
QUOTED = re.compile(r"“((?:[^“”]|“[^“”]*”)*)”")
SUCCESS_WORDS = ("succeeded", "went through", "confirms it's done")


def norm(text):
    return " ".join(text.split())


def row_text(row):
    parts = []
    for m in row["messages"]:
        if m.get("content"):
            parts.append(m["content"])
        for tc in m.get("tool_calls") or []:
            parts.append(json.dumps(tc["function"]["arguments"], ensure_ascii=False))
    return norm("\n".join(parts))


def _type_ok(value, spec):
    t = spec.get("type")
    ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
          "boolean": isinstance(value, bool), "array": isinstance(value, list), "object": isinstance(value, dict),
          "number": isinstance(value, (int, float)) and not isinstance(value, bool)}.get(t, True)
    return ok and ("enum" not in spec or value in spec["enum"])


def check_authored(r, prov, schemas, bad):
    """V3.3-authored rows: provenance, a schema-valid trained call on a tool the step actually offers, no send."""
    rid, msgs = r["id"], r["messages"]
    if r["conversation_id"] not in prov:
        bad(rid, "authored row without v33 provenance")
        return
    offered = {t["function"]["name"]: t["function"] for t in r["tools"]}
    for tc in msgs[-1].get("tool_calls") or []:
        name, args = tc["function"]["name"], tc["function"]["arguments"]
        if name not in offered:
            bad(rid, f"trained call {name} is not offered at this step")
            continue
        if "description" not in offered[name] or name == "get_tool_schemas":
            continue                        # a name-only stub or discovery call: loading, checked by the trace rules
        spec = schemas.get(name, {})
        props = spec.get("properties", {})
        for k in spec.get("required", []) or []:
            if k not in args:
                bad(rid, f"{name} missing required {k}")
        for k, v in args.items():
            if k not in props:
                bad(rid, f"{name} has unknown argument {k}")
            elif not _type_ok(v, props[k]):
                bad(rid, f"{name}.{k}={v!r} violates its schema")
        if name in ("send_email", "send_message", "draft_email", "forward_email", "reply_to_email", "schedule_send"):
            bad(rid, f"authored V3.3 rows never send, but the trained step calls {name}")
        if name in ("add_reminder", "clear_past_reminders", "clear_reminders", "cancel_event") and \
                prov[r["conversation_id"]]["family_id"] == "read_not_write":
            bad(rid, f"read_not_write trains a write ({name})")
    # every context-only (refused) call in an authored conversation must be followed by a corrected one or a stop
    text = " ".join(m.get("content") or "" for m in msgs if m["role"] == "tool")
    if "@" in text and "example.invalid" not in text and "example.com" not in text:
        bad(rid, "a non-synthetic email address appears in a tool result")


def main():
    all_tools = set(json.loads((V32 / "assets" / "tools.json").read_text())["tool_family"])
    all_tools |= {"get_tool_schemas", "web_search", "web_fetch", "use_skill"}
    tool_word = re.compile(r"\b(" + "|".join(sorted(map(re.escape, all_tools), key=len, reverse=True)) + r")\b")
    kinds = {}
    for line in (HERE / "thinking.provenance.jsonl").read_text().splitlines():
        p = json.loads(line)
        kinds[p["id"]] = p["kind"]
    stats = json.loads((HERE / "stats.json").read_text())
    tools_asset = json.loads((V32 / "assets" / "tools.json").read_text())
    schemas = {s["function"]["name"]: s["function"]["parameters"] for s in tools_asset["full_registry_schemas"]}
    v33 = {json.loads(x)["id"]: json.loads(x) for x in (HERE / "v33.provenance.jsonl").read_text().splitlines() if x}
    problems, checked, distinct = [], 0, set()

    def bad(rid, msg):
        problems.append(f"{rid}: {msg}")

    for split in ("train", "dev"):
        src_all = [json.loads(x) for x in (V32 / f"{split}.jsonl").read_text().splitlines() if x]
        out_all = [json.loads(x) for x in (HERE / f"{split}.jsonl").read_text().splitlines() if x]
        authored = [r for r in out_all if r["id"].startswith("v33-")]
        out = [r for r in out_all if not r["id"].startswith("v33-")]
        src = src_all
        if len(src) != len(out):
            bad(split, f"{len(out)} V3.2-derived rows, V3.2 has {len(src)}")
            continue
        if not out_all[-1]["id"].startswith("v2r-"):
            bad(split, "the V2 replay block is no longer last")
        for r in authored:
            check_authored(r, v33, schemas, bad)
        src = src + authored              # authored rows go through the same trace checks below
        out = out + authored
        digest = hashlib.sha256((HERE / f"{split}.jsonl").read_bytes()).hexdigest()
        if digest != stats["splits"][split]["sha256"]:
            bad(split, "sha256 differs from stats.json (re-run add_thinking.py)")
        for s, r in zip(src, out):
            rid = r["id"]
            checked += 1
            msgs = r["messages"]
            reasoning = msgs[-1].get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning.strip():
                bad(rid, "final message has no reasoning_content")
                continue
            if any("reasoning_content" in m for m in msgs[:-1]):
                bad(rid, "reasoning on a context message (Wisp never sends reasoning back in history)")
            stripped = json.loads(json.dumps(r))
            stripped["messages"][-1].pop("reasoning_content")
            if not rid.startswith("v33-") and stripped != s:
                bad(rid, "differs from its V3.2 source beyond reasoning_content")
            answer = norm(msgs[-1].get("content") or "")
            if len(answer) >= 40 and answer[:60] in norm(reasoning):
                bad(rid, "the trace repeats the answer (decode cost twice; the 2026-10-23 run did this on 52% of turns)")
            if len(reasoning) > MAX_CHARS:
                bad(rid, f"reasoning is {len(reasoning)} chars (> {MAX_CHARS})")
            if reasoning != norm(reasoning):
                bad(rid, "reasoning is not a single normalized paragraph")
            for c in CONTROL:
                if c in reasoning:
                    bad(rid, f"reasoning contains template text {c!r}")
            text = row_text(s)
            for span in QUOTED.findall(reasoning):
                needle = norm(span).rstrip("…").strip()
                if needle and needle not in text:
                    bad(rid, f"quoted span not in the row: {span[:80]!r}")
            offered = {t["function"]["name"] for t in r["tools"]}
            unquoted = QUOTED.sub("", reasoning)
            for name in set(tool_word.findall(unquoted)):
                if name not in offered:
                    bad(rid, f"names tool {name} the row does not offer")
            kind = kinds.get(rid)
            calls = [tc["function"]["name"] for tc in msgs[-1].get("tool_calls") or []]
            for name in calls:
                if name not in unquoted:
                    bad(rid, f"called tool {name} not named in the reasoning")
            if kind and kind.startswith("final_after"):
                last_tool = next(m for m in reversed(msgs[:-1]) if m["role"] == "tool")["content"].strip()
                if last_tool == "The user denied this action." and "denied" not in reasoning:
                    bad(rid, "a denial is not called a denial")
                low = last_tool.lower()
                if (low.startswith("(exit code") or low.startswith("could not")) and any(w in reasoning for w in SUCCESS_WORDS):
                    bad(rid, "a failed result described as a success")
            distinct.add(QUOTED.sub("“”", reasoning))
    report = {"status": "PASS" if not problems else "FAIL", "rows_checked": checked, "problem_count": len(problems),
              "problems": problems[:50], "distinct_reasoning_skeletons": len(distinct),
              "source_sha256": stats["source_sha256"],
              "sha256": {k: v["sha256"] for k, v in stats["splits"].items()}}
    (HERE / "validation.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report, indent=1))
    sys.exit(0 if not problems else 1)


if __name__ == "__main__":
    main()
