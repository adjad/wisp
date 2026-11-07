#!/usr/bin/env python3
"""The numbers the audit asked for, counted from a build's rendered rows. Reads the tokenizer and template only; no weights.

    report_r5.py --build BUILD --model MODEL_DIR --out REPORT_DIR [--r4-zip r4/ask-lora-data.zip] [--seed 20261107]

Writes to REPORT_DIR:
- ``family_tokens.csv``     per family: rows, row share, supervised tokens, token share, thought, answer and call tokens, shapes, origins.
- ``token_stats.json``      totals, effective weighting, trace measures, final style, action and restraint balance.
- ``trajectories.json``     five types (chat, single call, parallel calls, dependent chain, final after tool results) for this build
                            and, with --r4-zip, for r4: ids, decoded tokens, the loss mask and the scored logit positions.
- ``sample40.jsonl``        40 random complete records.

The weighting matters because train.py averages CE within a row and then equally across rows. A token in a 30-token call row counts
20 times as much as a token in a 600-token answer, so row shares and token shares answer different questions.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TRAINING / "ask-lora-r3" / "brev"))
sys.path.insert(0, str(TRAINING / "ask-lora-r5" / "gpu"))
import r5_stats  # noqa: E402
import source_contract as S  # noqa: E402

CLOSE = "<channel|>"
OPEN = "<|channel>thought\n"
TOOL_OPEN = "<|tool_call>"
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
SOURCE_OF = {"view_emails": "mail", "summarize_emails": "mail", "view_messages": "messages", "summarize_messages": "messages", "search_notes": "notes",
             "get_upcoming": "calendar", "search_reminders": "reminders", "recall": "memory", "web_search": "web", "get_weather": "weather",
             "get_my_day_plan": "day_plan", "get_recent_activity": "activity", "daily_brief": "brief"}
RESTRAINT_NOTES = re.compile(r"contrast|negation|restrain|keep or clear|broad ask|unneeded lookup|passing mention|don't|no write|clarif", re.I)


def split_completion(comp: str, forced: bool) -> tuple[str, str]:
    """(thought text, what follows it)."""
    body = comp
    if not forced:
        if not body.startswith(OPEN):
            return "", body
        body = body[len(OPEN):]
    head, sep, tail = body.partition(CLOSE)
    return (head, tail) if sep else ("", body)


def trajectory(rid, family, prompt, completion, tok, kind) -> dict:
    p = tok(prompt, add_special_tokens=False)["input_ids"]
    c = tok(completion, add_special_tokens=False)["input_ids"]
    head = p[:12]
    return {"type": kind, "id": rid, "family": family, "prompt": prompt, "completion": completion, "prompt_tokens": len(p), "completion_tokens": len(c),
            "prompt_ids_head": head, "prompt_ids_tail": p[-24:], "completion_ids": c,
            "completion_decoded": [tok.decode([i]) for i in c],
            "loss_mask": {"prompt": f"0 for all {len(p)} prompt tokens", "completion": f"1 for all {len(c)} completion tokens"},
            "scored_logit_positions": [len(p) - 1, len(p) + len(c) - 2],
            "note": "train.py scores logits[len(prompt)-1 : -1] against the completion ids, then averages within this row"}


def row_state(ex: dict, m: dict, forced: bool) -> str:
    """What this row is now, and what the finished build does with it."""
    origin, trace = r5_stats.origin_of(ex, m), r5_stats.trace_origin(m)
    if origin == "scripted_call":
        if forced and trace == "none":
            return "awaiting base's trace: without one this row and its turn leave the build"
        if trace == "none":
            return "first step with no thought: nothing to mask; base's trace replaces this where its call matches"
        return f"scripted call with a {trace} thought; base's trace replaces it where its call matches"
    if origin == "scripted":
        return "scripted final awaiting base's answer; kept only as a counted fallback if base's samples fail and it has a thought"
    return f"{origin}, final target"


def row_type(ex: dict) -> str | None:
    msgs = ex["messages"]
    last = msgs[-1]
    roles = [m["role"] for m in msgs]
    lu = max(i for i, r in enumerate(roles) if r == "user")
    prior_calls = [m for m in msgs[lu + 1:-1] if m.get("tool_calls")]
    if last.get("tool_calls"):
        if prior_calls:
            return "dependent_tool_chain"
        return "independent_parallel_calls" if len(last["tool_calls"]) > 1 else "single_tool_call"
    return "final_after_tool_results" if prior_calls else "normal_conversation"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--r4-zip")
    ap.add_argument("--seed", type=int, default=20261107)
    a = ap.parse_args(argv)
    import render
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.model, local_files_only=True)
    build, out = Path(a.build), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    train_ids = {json.loads(l)["id"] for l in open(build / "train.meta.jsonl")}
    fam = defaultdict(Counter)
    lens = defaultdict(list)
    tot, shapes, kinds = Counter(), Counter(), defaultdict(Counter)
    trace_words = defaultdict(list)
    echoes = []
    style = Counter()
    final_chars = []
    turns = defaultdict(lambda: dict(family=None, tools=[], writes=False, parallel=False, steps=0, notes="", chain=False))
    per_token, examples, sample_pool = [], {}, []
    n_train = len(train_ids)
    records = []
    with open(build / "all.jsonl") as fe, open(build / "meta.jsonl") as fm:
        for x, y in zip(fe, fm, strict=True):
            ex, m = json.loads(x), json.loads(y)
            r = render.render(tok, ex, m)
            forced = r["prompt"].endswith(render.FORCED)
            comp_ids = tok(r["completion"], add_special_tokens=False)["input_ids"]
            n = len(comp_ids)
            thought, rest = split_completion(r["completion"], forced)
            nt = len(tok(thought, add_special_tokens=False)["input_ids"]) if thought else 0
            shape = render.row_shape(r)
            placeholder = ex["messages"][-1].get("content") == r5_stats.PLACEHOLDER
            target = m["target_kind"]
            f = fam[m["family"]]
            f["rows"] += 1
            f[f"rows_{target}"] += 1
            f["completion_tokens"] += n
            f["thought_tokens"] += nt
            f["call_tokens" if target != "final" else "answer_tokens"] += n - nt
            f[f"shape_{shape}"] += 1
            f[f"origin_{r5_stats.origin_of(ex, m)}"] += 1
            f[f"trace_{r5_stats.trace_origin(m)}"] += 1
            if m["id"] in train_ids:
                f["train_rows"] += 1
                f["train_completion_tokens"] += n
                per_token.append((target, shape, n))
            lens[m["family"]].append(n)
            tot["rows"] += 1
            tot["completion_tokens"] += n
            tot["thought_tokens"] += nt
            shapes[shape] += 1
            kinds[target]["rows"] += 1
            kinds[target]["tokens"] += n
            if m.get("think"):
                trace_words[f"{target}:{r5_stats.trace_origin(m)}"].append(len((m.get("thought") or "").split()))
            if target == "final" and not placeholder:
                text = ex["messages"][-1].get("content") or ""
                final_chars.append(len(text))
                style["finals"] += 1
                style["bold"] += "**" in text
                style["list"] += bool(re.search(r"^\s*(?:[-*]|\d+\.)\s", text, re.M))
                style["emoji"] += bool(EMOJI.search(text))
                style["table"] += bool(re.search(r"^\s*\|.*\|\s*$", text, re.M))
                if m.get("thought"):
                    echoes.append(S.echo_score(m["thought"], text))
            tid = m["id"].rsplit("-s", 1)[0]
            t = turns[tid]
            t["family"], t["notes"] = m["family"], m.get("notes") or ""
            t["steps"] += 1
            t["writes"] = t["writes"] or bool(m.get("turn_writes"))
            calls = ex["messages"][-1].get("tool_calls") or []
            t["tools"] += [c["function"]["name"] for c in calls]
            t["parallel"] = t["parallel"] or len(calls) > 1
            t["chain"] = t["chain"] or (bool(calls) and m["step"] > 0)
            ty = row_type(ex)
            if ty and ty not in examples and not placeholder and m["id"] in train_ids:
                examples[ty] = {**trajectory(m["id"], m["family"], r["prompt"], r["completion"], tok, ty), "state": row_state(ex, m, forced)}
            # the same step with a thought, since the row above may still be waiting for one
            if ty == "dependent_tool_chain" and m.get("thought") and "dependent_tool_chain_with_thought" not in examples and m["id"] in train_ids:
                examples["dependent_tool_chain_with_thought"] = {**trajectory(m["id"], m["family"], r["prompt"], r["completion"], tok, ty), "state": row_state(ex, m, forced)}
            records.append((m["id"], ex, m, r, placeholder, forced))

    # the source family's real targets don't exist until base writes them, so prefer it where a row exists
    for rid, ex, m, r, placeholder, forced in records:
        if m["family"] == "all_sources_r5" and not placeholder and m["id"] in train_ids:
            ty = row_type(ex)
            if ty in ("independent_parallel_calls", "dependent_tool_chain"):
                examples[ty] = {**trajectory(rid, m["family"], r["prompt"], r["completion"], tok, ty), "state": row_state(ex, m, forced)}

    with open(out / "family_tokens.csv", "w", newline="") as f:
        cols = ["family", "rows", "train_rows", "row_share", "completion_tokens", "train_completion_tokens", "token_share", "effective_weight_per_token_x1e6", "completion_median",
                "thought_tokens", "answer_tokens", "call_tokens", "rows_final", "rows_call", "rows_load", "shape_first_think", "shape_first_answer", "shape_forced_think",
                "shape_forced_empty", "origin_base_generated", "origin_base_r4_reuse", "origin_scripted", "origin_scripted_call", "origin_placeholder", "trace_base", "trace_scripted", "trace_none"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        train_tokens = sum(c["train_completion_tokens"] for c in fam.values())
        for name, c in sorted(fam.items()):
            row = {k: c.get(k, 0) for k in cols if k != "family"}
            row["family"] = name
            row["row_share"] = round(c["train_rows"] / max(1, n_train), 4)
            row["token_share"] = round(c["train_completion_tokens"] / max(1, train_tokens), 4)
            row["effective_weight_per_token_x1e6"] = round(1e6 * (c["train_rows"] / max(1, n_train)) / max(1, c["train_completion_tokens"]), 3)
            row["completion_median"] = statistics.median(lens[name])
            w.writerow(row)

    by_target = defaultdict(lambda: [0, 0])
    by_shape = defaultdict(lambda: [0, 0])
    for target, shape, n in per_token:
        by_target[target][0] += 1
        by_target[target][1] += n
        by_shape[shape][0] += 1
        by_shape[shape][1] += n
    total_rows, total_tokens = len(per_token), sum(n for *_, n in per_token)
    weight = lambda d: {k: {"rows": v[0], "row_share": round(v[0] / max(1, total_rows), 4), "tokens": v[1], "token_share": round(v[1] / max(1, total_tokens), 4),  # noqa: E731
                            "mean_tokens": round(v[1] / max(1, v[0]), 1)} for k, v in sorted(d.items())}

    multi = sum(1 for t in turns.values() if len({SOURCE_OF[n] for n in t["tools"] if n in SOURCE_OF}) >= 2)
    write_turns = sum(1 for t in turns.values() if t["writes"])
    restraint = sum(1 for t in turns.values() if not t["writes"] and (RESTRAINT_NOTES.search(t["notes"]) or t["family"] in ("restraint_r4", "safe_restraint", "no_write_for_reads")))
    no_tool = sum(1 for t in turns.values() if not t["tools"])
    tool_kind, contrast_modes = Counter(), Counter()
    for t in turns.values():
        if t["family"] == "write_contrast_r5":
            contrast_modes[t["notes"].split("(")[-1].split(")")[0]] += 1
        if t["family"] in ("tool_choice_r5", "restraint_r4", "safe_restraint"):
            low = t["notes"].lower()
            tag = ("draft" if "draft" in low else "clarify" if "clarif" in low or "ask" in low.split("]")[-1][:30] else "read" if t["tools"] and not t["writes"] else "no_action" if not t["writes"] else "write")
            tool_kind[tag] += 1

    stats = {"rows": tot["rows"], "train_rows": n_train, "completion_tokens": tot["completion_tokens"], "thought_tokens": tot["thought_tokens"],
             "rendered_shapes": dict(shapes), "placeholders_in_counts": sum(1 for rec in records if rec[4]),
             "effective_weighting": {"note": "train.py: F.cross_entropy(mean over the row), then each row / batch size. Train rows only.", "by_target": weight(by_target), "by_shape": weight(by_shape)},
             "trace_words": {k: {"n": len(v), "p10": S.percentile(v, 10), "p50": S.percentile(v, 50), "p90": S.percentile(v, 90)} for k, v in sorted(trace_words.items())},
             "trace_echo_vs_final": {"n": len(echoes), "p50": S.percentile(echoes, 50), "p90": S.percentile(echoes, 90), "share_ge_0_8": round(sum(e >= .8 for e in echoes) / max(1, len(echoes)), 4)},
             "finals_written": {"n": style["finals"], "chars_p10": S.percentile(final_chars, 10), "chars_p50": S.percentile(final_chars, 50), "chars_p90": S.percentile(final_chars, 90),
                                "bold_share": round(style["bold"] / max(1, style["finals"]), 3), "list_share": round(style["list"] / max(1, style["finals"]), 3),
                                "emoji_share": round(style["emoji"] / max(1, style["finals"]), 3), "table_count": style["table"]},
             "action_balance": {"turns": len(turns), "no_tool_turns": no_tool, "write_turns": write_turns, "restraint_turns": restraint, "multi_source_turns": multi,
                                "parallel_turns": sum(1 for t in turns.values() if t["parallel"]), "chain_turns": sum(1 for t in turns.values() if t["chain"]),
                                "tool_choice_and_restraint_families": dict(tool_kind), "write_contrast_modes": dict(contrast_modes)}}
    (out / "token_stats.json").write_text(json.dumps(stats, indent=1) + "\n")

    trajectories = {"r5": examples}
    if a.r4_zip and os.path.isfile(a.r4_zip):
        r4 = {}
        z = zipfile.ZipFile(a.r4_zip)
        for line in z.read("train.jsonl").decode().splitlines():
            row = json.loads(line)
            c = row["completion"]
            if row.get("kind") in ("final", "chat") or TOOL_OPEN not in c:
                ty = "final_after_tool_results" if row["prompt"].endswith(render.FORCED) else "normal_conversation"
            else:
                n_calls = c.count(TOOL_OPEN)
                ty = ("dependent_tool_chain" if row["prompt"].endswith(render.FORCED) else "independent_parallel_calls" if n_calls > 1 else "single_tool_call")
            if ty not in r4 and not row.get("image") and not row.get("audio"):
                r4[ty] = trajectory(row["id"], row.get("family"), row["prompt"], c, tok, ty)
            if len(r4) == 5:
                break
        trajectories["r4"] = r4
    (out / "trajectories.json").write_text(json.dumps(trajectories, indent=1, ensure_ascii=False) + "\n")

    rng = random.Random(a.seed)
    picked = rng.sample(records, min(40, len(records)))
    with open(out / "sample40.jsonl", "w") as f:
        for rid, ex, m, r, placeholder, forced in picked:
            f.write(json.dumps({"id": rid, "placeholder": placeholder, "meta": {k: m.get(k) for k in ("family", "notes", "target_kind", "step", "think", "thought", "origin", "trace_origin", "gold", "clock", "prompt", "turn_writes", "qkind")},
                                "messages": [x for x in ex["messages"] if x["role"] != "system"], "system_chars": sum(len(x.get("content") or "") for x in ex["messages"] if x["role"] == "system"),
                                "completion": r["completion"], "state": row_state(ex, m, forced), "seed": a.seed}, ensure_ascii=False) + "\n")
    print(json.dumps({k: stats[k] for k in ("rows", "completion_tokens", "rendered_shapes", "placeholders_in_counts", "action_balance")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
