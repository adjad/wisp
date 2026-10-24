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


def main():
    all_tools = set(json.loads((V32 / "assets" / "tools.json").read_text())["tool_family"])
    all_tools |= {"get_tool_schemas", "web_search", "web_fetch", "use_skill"}
    tool_word = re.compile(r"\b(" + "|".join(sorted(map(re.escape, all_tools), key=len, reverse=True)) + r")\b")
    kinds = {}
    for line in (HERE / "thinking.provenance.jsonl").read_text().splitlines():
        p = json.loads(line)
        kinds[p["id"]] = p["kind"]
    stats = json.loads((HERE / "stats.json").read_text())
    problems, checked, distinct = [], 0, set()

    def bad(rid, msg):
        problems.append(f"{rid}: {msg}")

    for split in ("train", "dev"):
        src = [json.loads(x) for x in (V32 / f"{split}.jsonl").read_text().splitlines() if x]
        out = [json.loads(x) for x in (HERE / f"{split}.jsonl").read_text().splitlines() if x]
        if len(src) != len(out):
            bad(split, f"{len(out)} rows, V3.2 has {len(src)}")
            continue
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
            if stripped != s:
                bad(rid, "differs from its V3.2 source beyond reasoning_content")
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
