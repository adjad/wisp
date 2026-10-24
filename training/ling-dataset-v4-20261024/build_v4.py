"""Build Ling dataset V4 from V3.3-thinking: the energetic-voice prompt, energetic final answers, personal-context rows.

    python3 build_v4.py --queue    # write rewrite_queue.jsonl: every final answer that needs a voice rewrite
    python3 build_v4.py            # write train.jsonl / dev.jsonl / stats.json (needs voice_rewrites.jsonl to cover the queue)

Three changes, applied in this order to V3.3's rows (which must match V3.3's pinned sha256):

1. New conversations from ext_v4_personal.py (read the user's day first; warm small talk), rendered with V3.2's
   Builder and given reasoning traces by V3.3's add_thinking.Row, the same as V3.3's own authored rows. They go after
   the model-led rows and before the V2 replay block, which stays last.
2. The system prompt. Every model-led row's first system message drops " Keep answers concise." and gains the voice
   rules that Wisp's agent prompt now ends with (assets/voice_tail.txt, copied from service/agent/loop.py SYSTEM on
   branch claude/energetic-voice, PR #357), right where they sit at runtime: after the last rule block, before the
   identity block. V2 replay rows use V2's own prompt and are left alone.
3. Final answers. Every trained final answer (an assistant message with no tool calls, outside the V2 replay block)
   is replaced by its reviewed rewrite from voice_rewrites.jsonl, matched by row id and the sha256 of the original
   text. The same text is replaced wherever it reappears as history later in that conversation, so a model never sees
   its old terse self in context. Reasoning traces are NOT recomputed: each stays the trace of the original decision
   (a closing offer in the new answer must not turn an "answer and stop" trace into a clarifying-question trace).

Standard library only; deterministic.
"""
import argparse
import hashlib
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
V32 = HERE.parent / "ling-dataset-v3.2-20261014"
V33 = HERE.parent / "ling-dataset-v3.3-thinking-20261023"
SEED = 20261024
CONCISE = " Keep answers concise.\n"
IDENTITY = "\n\nTHE USER —"
CATEGORY_PREFIX = "v4:"


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def load_v33():
    pins = json.loads((V33 / "validation.json").read_text())["sha256"]
    rows = {}
    for split in ("train", "dev"):
        path = V33 / f"{split}.jsonl"
        if not path.exists():
            sys.exit(f"missing {path}: rebuild V3.2 then V3.3 (see V3.3's README, Reproduce)")
        if sha(path.read_bytes()) != pins[split]:
            sys.exit(f"{path} does not match V3.3's pinned sha256; rebuild it first")
        rows[split] = [json.loads(line) for line in path.read_text().splitlines()]
    return rows, pins


def chat_trace(rng, user):
    said = user if len(user) <= 80 else user[:77] + "..."
    return " ".join([
        rng.choice([f"The user said “{said}”.", f"The message is “{said}”."]),
        rng.choice(["That's small talk, with no request and no personal data involved, so no tool is needed.",
                    "Nothing to look up or do here; it's a friendly message, so no tool."]),
        rng.choice(["Reply warmly.", "Answer with some warmth."]),
    ])


def authored():
    """V4's own conversations -> ({split: rows}, {cid: provenance})."""
    sys.path.insert(0, str(V32))
    sys.path.insert(0, str(V33))
    sys.path.insert(0, str(HERE))
    import generate as G
    import add_thinking as T
    import ext_v4_personal as X
    held = json.loads((V32 / "assets" / "heldout_shingles.json").read_text())["items"]
    rows, prov, counters = {"train": [], "dev": []}, {}, {"train": 0, "dev": 0}
    for m in X.build(G, held):
        split = m["split"]
        counters[split] += 1
        cid = f"v4-personal-{split}-{counters[split]:04d}"
        style = "terse" if counters[split] % 2 else "full"
        prefix = [({"role": role, "content": t, "training": False} if role == "assistant" else {"role": role, "content": t})
                  for role, t in m["prefix"]] + [{"role": "user", "content": m["user_text"]}]
        nrows, _, info = m["builder"].finish(prefix, m["now"], m["user_text"], style)
        p = {"id": cid, "split": split, "category": CATEGORY_PREFIX + X.CATEGORY, "family_id": m["fid"], "hint": m["hint"],
             "slots": m["meta"], "schema_style": style, "catalog_variant": m["variant"], "reference_now": m["now"],
             "user_prompt": m["user_text"], "max_overlap": round(m["overlap"], 3), "gold_calls": info["gold_calls"],
             "protocol_variant": info["protocol_variant"], "n_steps": info["n_steps"]}
        prov[cid] = p
        for r in nrows:
            row = {"id": f"{cid}-s{r['k']}", "conversation_id": cid, "tools": r["tools"], "messages": r["messages"]}
            if m["fid"] == "chat_warm":
                text = chat_trace(random.Random(f"{SEED}:{row['id']}"), m["user_text"])
            else:
                _, text = T.Row(row, p).reasoning()
            row["messages"][-1]["reasoning_content"] = " ".join(text.split())
            rows[split].append(row)
    return rows, prov


def is_final(row):
    last = row["messages"][-1]
    return not row["id"].startswith("v2r-") and last["role"] == "assistant" and not last.get("tool_calls")


def voice_prompt(text, tail):
    assert text.count(CONCISE) == 1 and text.count(IDENTITY) == 1, "unexpected system prompt shape"
    text = text.replace(CONCISE, "\n", 1)
    return text.replace(IDENTITY, "\n" + tail + IDENTITY, 1)


def assemble():
    source, pins = load_v33()
    new_rows, new_prov = authored()
    tail = (HERE / "assets" / "voice_tail.txt").read_text().rstrip("\n")
    out = {}
    for split in ("train", "dev"):
        rows = source[split]
        cut = next((i for i, r in enumerate(rows) if r["id"].startswith("v2r-")), len(rows))
        merged = rows[:cut] + new_rows[split] + rows[cut:]
        for row in merged:
            if not row["id"].startswith("v2r-"):
                row["messages"][0]["content"] = voice_prompt(row["messages"][0]["content"], tail)
        out[split] = merged
    return out, new_prov, pins


def provenance():
    prov = {}
    for f in (V32 / "train.provenance.jsonl", V32 / "dev.provenance.jsonl", V33 / "v33.provenance.jsonl"):
        for line in f.read_text().splitlines():
            p = json.loads(line)
            prov[p["id"]] = p
    return prov


def queue(rows, new_prov):
    prov = provenance()
    prov.update(new_prov)
    items = []
    for split in ("train", "dev"):
        for row in rows[split]:
            if not is_final(row):
                continue
            msgs = row["messages"]
            lu = max(i for i, m in enumerate(msgs) if m["role"] == "user")
            names = {tc["id"]: tc["function"]["name"] for m in msgs for tc in m.get("tool_calls") or []}
            results = [{"tool": names.get(m.get("tool_call_id"), "?"), "result": m["content"][:2500]}
                       for m in msgs[lu + 1:-1] if m["role"] == "tool"]
            history = [{"role": m["role"], "content": (m.get("content") or "")[:600]}
                       for m in msgs[3:lu] if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
            p = prov.get(row["conversation_id"], {})
            items.append({"id": row["id"], "split": split, "category": p.get("category"), "family": p.get("family_id"),
                          "now": msgs[1]["content"].split("\n")[1] if "\n" in msgs[1]["content"] else "",
                          "history": history[-4:], "user": msgs[lu]["content"], "tool_results": results,
                          "trace": msgs[-1].get("reasoning_content", ""), "original": msgs[-1]["content"],
                          "orig_sha": sha(msgs[-1]["content"])})
    path = HERE / "rewrite_queue.jsonl"
    path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
    print(f"{len(items)} final answers -> {path.name}")


def apply_rewrites(rows):
    rewrites = {}
    for line in (HERE / "voice_rewrites.jsonl").read_text().splitlines():
        r = json.loads(line)
        rewrites[r["id"]] = r
    missing, stale, replaced = [], [], 0
    for split in ("train", "dev"):
        by_conv = {}
        for row in rows[split]:
            by_conv.setdefault(row["conversation_id"], []).append(row)
        for conv in by_conv.values():
            swap = {}
            for row in conv:
                if not is_final(row):
                    continue
                r = rewrites.get(row["id"])
                orig = row["messages"][-1]["content"]
                if r is None:
                    missing.append(row["id"])
                elif r["orig_sha"] != sha(orig):
                    stale.append(row["id"])
                else:
                    swap[orig] = r["text"]
            for row in conv:
                for m in row["messages"]:
                    if m["role"] == "assistant" and not m.get("tool_calls") and m.get("content") in swap:
                        m["content"] = swap[m["content"]]
                        replaced += 1
    if missing or stale:
        sys.exit(f"voice_rewrites.jsonl: {len(missing)} missing (e.g. {missing[:3]}), {len(stale)} stale (e.g. {stale[:3]})")
    return replaced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", action="store_true")
    args = ap.parse_args()
    rows, new_prov, pins = assemble()
    if args.queue:
        queue(rows, new_prov)
        return
    replaced = apply_rewrites(rows)
    (HERE / "v4.provenance.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in new_prov.values()))
    stats = {"source_sha256": pins, "seed": SEED, "messages_rewritten": replaced, "splits": {}}
    for split in ("train", "dev"):
        lines = [json.dumps(r, ensure_ascii=False) for r in rows[split]]
        (HERE / f"{split}.jsonl").write_text("\n".join(lines) + "\n")
        finals = [r["messages"][-1]["content"] for r in rows[split] if is_final(r)]
        words = sorted(len(f.split()) for f in finals)
        stats["splits"][split] = {
            "rows": len(rows[split]), "v4_rows": sum(r["id"].startswith("v4-") for r in rows[split]),
            "v4_conversations": sum(p["split"] == split for p in new_prov.values()),
            "final_answers": len(finals), "final_words": {"p50": int(statistics.median(words)), "p90": words[int(0.9 * (len(words) - 1))],
                                                          "max": words[-1]},
            "final_with_bold_pct": round(100 * sum("**" in f for f in finals) / len(finals), 1),
            "sha256": sha((HERE / f"{split}.jsonl").read_bytes())}
    (HERE / "stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
