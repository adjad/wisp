"""Validate Ling dataset V4 against its V3.3 source and the voice rules. Writes validation.json; exit 1 on any problem.

    python3 validate_v4.py

Checks:
- Structure: every V3.3 row is present, in order, with the V4 rows inserted after the model-led rows and before the V2
  replay block. V2 replay rows are byte-identical to V3.3.
- Prompt: no model-led row says "Keep answers concise"; each carries assets/voice_tail.txt exactly once, directly
  before the identity block, and is otherwise identical to its V3.3 first system message.
- Only voice changed: for each V3.3 row, every tool call, tool result, user message, system message 1-2, tool offer and
  reasoning trace is identical to the source. Only assistant prose (finals and their repeats in history) may differ.
- Voice: every trained final answer passes check_rewrite.problems() against its queue item (facts kept, no new facts,
  decision kept, no emoji on bad news), and no trace repeats its answer.
- V4 rows: provenance present, trained calls offered and schema-valid, no send or write, synthetic data only, held-out
  overlap under 0.4, reasoning on the trained turn only.
"""
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_v4 as B
import check_rewrite as C

V33 = B.V33
WRITES = {"send_email", "send_message", "draft_email", "forward_email", "reply_to_email", "schedule_send", "add_reminder",
          "add_calendar_event", "cancel_event", "clear_past_reminders", "clear_reminders", "update_reminder", "remember"}


def norm(text):
    return " ".join(text.split())


def type_ok(value, spec):
    t = spec.get("type")
    ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
          "boolean": isinstance(value, bool), "array": isinstance(value, list), "object": isinstance(value, dict)}.get(t, True)
    return ok and ("enum" not in spec or value in spec["enum"])


def skeleton(row):
    """The row with assistant prose removed: what must be identical to V3.3."""
    out = json.loads(json.dumps(row))
    for m in out["messages"]:
        if m["role"] == "assistant" and not m.get("tool_calls"):
            m["content"] = "<prose>"
    out["messages"][0]["content"] = "<system0>"
    return out


def main():
    tail = (HERE / "assets" / "voice_tail.txt").read_text().rstrip("\n")
    queue = C.load_queue()
    v4prov = {json.loads(line)["id"]: json.loads(line) for line in (HERE / "v4.provenance.jsonl").read_text().splitlines()}
    schemas = {s["function"]["name"]: s["function"]["parameters"]
               for s in json.loads((B.V32 / "assets" / "tools.json").read_text())["full_registry_schemas"]}
    problems = []

    def bad(rid, msg):
        problems.append({"id": rid, "problem": msg})

    metrics = {}
    for split in ("train", "dev"):
        src = [json.loads(x) for x in (V33 / f"{split}.jsonl").read_text().splitlines()]
        rows = [json.loads(x) for x in (HERE / f"{split}.jsonl").read_text().splitlines()]
        src_by_id = {r["id"]: r for r in src}
        old_ids = [r["id"] for r in rows if not r["id"].startswith("v4-")]
        if old_ids != [r["id"] for r in src]:
            bad(split, "V3.3 rows missing, added or reordered")
        first_v2 = next((i for i, r in enumerate(rows) if r["id"].startswith("v2r-")), len(rows))
        if any(r["id"].startswith("v4-") for r in rows[first_v2:]):
            bad(split, "a V4 row sits inside the V2 replay block")
        finals, bold, emoji, openers = [], 0, 0, []
        for r in rows:
            rid, msgs = r["id"], r["messages"]
            if rid.startswith("v2r-"):
                if r != src_by_id.get(rid):
                    bad(rid, "V2 replay row changed")
                continue
            s0 = msgs[0]["content"]
            if "Keep answers concise" in s0:
                bad(rid, "system prompt still says Keep answers concise")
            if s0.count(tail) != 1 or (tail + B.IDENTITY) not in s0:
                bad(rid, "voice tail missing, repeated or not directly before the identity block")
            source = src_by_id.get(rid)
            if source is not None:
                if B.voice_prompt(source["messages"][0]["content"], tail) != s0:
                    bad(rid, "system prompt differs from V3.3 beyond the voice change")
                if skeleton(r) != skeleton(source):
                    bad(rid, "differs from V3.3 beyond assistant prose and the system prompt")
            if any("reasoning_content" in m for m in msgs[:-1]):
                bad(rid, "reasoning on a context message")
            reasoning = msgs[-1].get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning.strip():
                bad(rid, "trained turn has no reasoning_content")
            if rid.startswith("v4-"):
                cid = r["conversation_id"]
                p = v4prov.get(cid)
                if p is None:
                    bad(rid, "V4 row without provenance")
                elif p["max_overlap"] >= 0.4:
                    bad(rid, "user prompt overlaps the held-out eval (>= 0.4)")
                offered = {t["function"]["name"]: t["function"] for t in r["tools"]}
                for tc in msgs[-1].get("tool_calls") or []:
                    name, args = tc["function"]["name"], tc["function"]["arguments"]
                    if name not in offered:
                        bad(rid, f"trained call {name} not offered")
                        continue
                    if name in WRITES:
                        bad(rid, f"V4 rows only read, but the trained step calls {name}")
                    if "description" in offered[name] and name != "get_tool_schemas":
                        props = schemas.get(name, {}).get("properties", {})
                        for k, v in args.items():
                            if k not in props or not type_ok(v, props[k]):
                                bad(rid, f"{name}.{k}={v!r} is not schema-valid")
                text = " ".join(m.get("content") or "" for m in msgs)
                for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
                    if not addr.endswith((".invalid", "example.com")):
                        bad(rid, f"non-synthetic address {addr}")
            if not B.is_final(r):
                continue
            answer = msgs[-1]["content"]
            item = queue.get(rid)
            if item is None:
                bad(rid, "final answer not in rewrite_queue.jsonl")
            else:
                for prob in C.problems(item, answer):
                    bad(rid, "voice: " + prob)
            if len(norm(answer)) >= 40 and norm(answer)[:60] in norm(reasoning or ""):
                bad(rid, "the trace repeats the answer")
            finals.append(answer)
            bold += "**" in answer
            emoji += bool(C.EMOJI.search(answer))
            openers.append(" ".join(re.findall(r"[a-z']+", answer.lower())[:3]))
        words = sorted(len(f.split()) for f in finals)
        metrics[split] = {"final_answers": len(finals), "words_p50": int(statistics.median(words)),
                          "words_p90": words[int(0.9 * (len(words) - 1))], "bold_pct": round(100 * bold / len(finals), 1),
                          "emoji_pct": round(100 * emoji / len(finals), 1),
                          "distinct_openers_pct": round(100 * len(set(openers)) / len(openers), 1)}
    report = {"status": "PASS" if not problems else "FAIL", "problem_count": len(problems), "problems": problems[:200],
              "voice_metrics": metrics,
              "sha256": {s: B.sha((HERE / f"{s}.jsonl").read_bytes()) for s in ("train", "dev")}}
    (HERE / "validation.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "problems"}, indent=1))
    for p in problems[:15]:
        print(p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
