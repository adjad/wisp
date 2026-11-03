#!/usr/bin/env python3
"""Pick one base answer per vision row from vision_gen.py's samples, rejecting only defects, and write the spliced
rows file.

The checks are the r4 GRPO reward's own (ask-lora-r3/grpo/reward.py ``score_mm``, ``score_picture_read``), with
labels built the way export_prompts.py builds them. ``facts`` come from the row's meta when it has them, else
``mm_facts`` of the scripted answer.

Answers (``image``, ``document``, ``text_document``) are rejected for:

- ``malformed``: a thought channel, a tool call, template markup, or no stop;
- ``planted``: any of the row's ``banned_in_target`` strings (planted text quoted or obeyed);
- ``proposal_spurious``: a proposal block where none is due;
- ``proposal_missing``: no block where one is due;
- ``proposal_invalid``: a block that fails ``vision.parse_proposal``'s validation;
- ``proposal_fields``: a proposal with any field not matching the scripted one;
- ``unreadable``: an unreadable image not flagged as unreadable, or with text asserted from it;
- ``facts_missing``: fewer than half of the row's facts (dates, times, names, amounts) in the answer.

Readings (``picture_read``) are rejected for:

- ``planted``: obeyed or restated planted text;
- ``format``: not exactly TEXT and DESCRIPTION;
- ``invented``: invented text lines;
- ``answered``: the description answers the request or offers an action;
- ``recall``: fewer than 80% of the true lines read.

Every row is also rejected for ``error``, ``empty``, ``truncated`` and ``stale_hash``. Length, closers and wording
are not checked: the base voice is the point.

    vision_choose.py --rows ../vision/vision_rows.jsonl --raw raw/x/vision.jsonl --out-chosen raw/x/vision.chosen.jsonl
        --out-stats raw/x/vision.stats.json [--out-rows _build/x-vision/vision_rows.jsonl]

With ``--out-rows`` it writes vision_rows.jsonl with each chosen row's ``target`` replaced by the base answer, and
``meta.distilled``, ``meta.distill_kind`` ("anchor_final"), ``meta.distill_k`` and ``meta.distill_sampling`` set.
Every other row stays byte-identical. It also adds an ``images`` link to the source images, so the relative image
paths still resolve.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "ask-lora-r3", "grpo")))
import export_prompts as EP  # noqa: E402
import reward as W  # noqa: E402
from vision_gen import image_bytes, vision_hash  # noqa: E402

FACTS_MIN = 0.5
READ_RECALL_MIN = 0.8
STOP = "<turn|>"


def label_for(row: dict) -> dict:
    """The reward label of one row, as export_prompts.vision_rows / picture_read_rows build it."""
    meta = row.get("meta") or {}
    if row["kind"] == "picture_read":
        return {"modality": "vision", "task": "picture_read", "truth_lines": [str(x) for x in row.get("truth_lines") or ()],
                "inj": row.get("inj"), "inj_text": row.get("inj_text"), "banned": []}
    visible, prop, _ = W.parse_proposal(row["target"])
    unreadable = bool(meta.get("unreadable"))
    banned = list(meta.get("banned_in_target") or [])
    if unreadable:
        facts = []
    elif isinstance(meta.get("facts"), list) and meta["facts"]:
        facts = [str(f) for f in meta["facts"] if not W._banned_hit(str(f), banned)]
    else:
        facts = [f for f in EP.mm_facts(visible) if not W._banned_hit(f, banned)]
    if not unreadable and not EP.usable_facts(facts):
        facts = []
    return {"modality": "vision", "facts": facts, "banned": banned, "unreadable": unreadable,
            "proposal": {k: prop[k] for k in W.PROPOSAL_FIELDS} if prop else None}


def reject_reasons(rec: dict, row: dict, cur_hash: str) -> list[str]:
    if rec.get("error"):
        return ["error"]
    out = []
    content = rec.get("content") or ""
    if rec.get("no_choices") or not content.strip():
        out.append("empty")
    if rec.get("finish_reason") == "length":
        out.append("truncated")
    if rec.get("tool_calls"):
        out.append("malformed")
    if rec.get("request_hash") != cur_hash:
        out.append("stale_hash")
    if out:
        return out
    label = label_for(row)
    res = W.score_mm(content + STOP, label, rnd=4)
    p = res.parts
    if p.get("malformed"):
        return ["malformed"]
    if row["kind"] == "picture_read":
        for key, why in (("p_obeyed", "planted"), ("p_format", "format"), ("p_invented", "invented"),
                         ("p_answered", "answered")):
            if p.get(key, 0) < 0:
                out.append(why)
        if p.get("p_recall", 0) < READ_RECALL_MIN:
            out.append("recall")
        return out
    if p.get("v_banned", 0) < 0:
        out.append("planted")
    if p.get("v_empty", 0) < 0:
        out.append("empty")
    want = label["proposal"]
    _, got, had_block = W.parse_proposal(content)
    if want is None and (got is not None or had_block):
        out.append("proposal_spurious")
    elif want is not None and got is None:
        out.append("proposal_invalid" if had_block else "proposal_missing")
    elif want is not None and not all(W._proposal_field_ok(k, want.get(k), got.get(k)) for k in W.PROPOSAL_FIELDS):
        out.append("proposal_fields")
    if label["unreadable"] and not p.get("v_facts"):
        out.append("unreadable")
    elif label["facts"] and p.get("v_facts", 0) < FACTS_MIN:
        out.append("facts_missing")
    return out


def read_jsonl(path: str):
    for line in open(path):
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def run(rows_path: str, raws: list[str]):
    base = os.path.dirname(os.path.abspath(rows_path))
    rows = list(read_jsonl(rows_path))
    by_id = {r["id"]: r for r in rows}
    cur = {r["id"]: vision_hash(r, image_bytes(base, r)) for r in rows}
    got = {}
    for rec in (x for p in raws for x in read_jsonl(p)):
        if rec.get("id") not in by_id:
            continue
        key = (rec["id"], rec["k"])
        if key not in got or (got[key].get("error") and not rec.get("error")):
            got[key] = rec
    samples = defaultdict(list)
    for (i, _k), rec in sorted(got.items()):
        samples[i].append({**rec, "_reasons": reject_reasons(rec, by_id[i], cur[i])})
    chosen = {}
    for i, ss in samples.items():
        ok = next((s for s in ss if not s["_reasons"]), None)
        if ok:
            chosen[i] = {k: v for k, v in ok.items() if k != "_reasons"}
            chosen[i]["content"] = chosen[i]["content"].strip()
            chosen[i]["distill_kind"] = "anchor_final"

    def group(ids):
        recs = [s for i in ids for s in samples.get(i, [])]
        firsts = [samples[i][0] for i in ids if samples.get(i)]
        no_pass = {i: [{"k": s["k"], "reasons": s["_reasons"]} for s in samples[i]] for i in ids
                   if i not in chosen and samples.get(i)}
        lens = sorted(len(chosen[i]["content"]) for i in ids if i in chosen)
        slens = sorted(len(by_id[i]["target"]) for i in ids if i in chosen)
        return {"rows": len(ids), "sampled": sum(1 for i in ids if samples.get(i)), "samples": len(recs),
                "chosen": sum(1 for i in ids if i in chosen),
                "first_sample_pass_rate": round(sum(not s["_reasons"] for s in firsts) / len(firsts), 3) if firsts else None,
                "rejects_by_reason": dict(Counter(x for s in recs for x in s["_reasons"]).most_common()),
                "scripted_kept": {"count": len(no_pass), "ids": sorted(no_pass)}, "no_pass": no_pass,
                "chosen_k": dict(sorted(Counter(chosen[i]["k"] for i in ids if i in chosen).items())),
                "chosen_chars_p50": lens[len(lens) // 2] if lens else None,
                "scripted_chars_p50": slens[len(slens) // 2] if slens else None}
    sampled = [r["id"] for r in rows if samples.get(r["id"])]
    kinds = sorted({by_id[i]["kind"] for i in sampled})
    stats = {"overall": group(sampled), "rows_in_file": len(rows),
             "kinds": {k: group([i for i in sampled if by_id[i]["kind"] == k]) for k in kinds}}
    return rows, chosen, stats


def write_rows(rows_path: str, rows: list[dict], chosen: dict, out: str) -> int:
    lines = [ln for ln in open(rows_path) if ln.strip()]
    if len(lines) != len(rows):
        sys.exit("rows file changed while reading")
    n, buf = 0, []
    for ln, r in zip(lines, rows):
        c = chosen.get(r["id"])
        if c is None:
            buf.append(ln if ln.endswith("\n") else ln + "\n")
            continue
        r = dict(r)
        r["target"] = c["content"]
        r["meta"] = {**(r.get("meta") or {}), "distilled": True, "distill_kind": "anchor_final", "distill_k": c["k"],
                     "distill_sampling": c.get("sampling")}
        buf.append(json.dumps(r, ensure_ascii=False) + "\n")
        n += 1
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(buf)
    os.replace(tmp, out)
    link = os.path.join(os.path.dirname(os.path.abspath(out)), "images")
    src = os.path.join(os.path.dirname(os.path.abspath(rows_path)), "images")
    if not os.path.lexists(link) and os.path.abspath(link) != os.path.abspath(src):
        os.symlink(src, link)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--raw", action="append", required=True)
    ap.add_argument("--out-chosen", required=True)
    ap.add_argument("--out-stats", required=True)
    ap.add_argument("--out-rows")
    a = ap.parse_args(argv)
    rows, chosen, stats = run(a.rows, a.raw)
    with open(a.out_chosen, "w") as f:
        for r in rows:
            if r["id"] in chosen:
                f.write(json.dumps(chosen[r["id"]], ensure_ascii=False) + "\n")
    if a.out_rows:
        stats["spliced_rows"] = write_rows(a.rows, rows, chosen, a.out_rows)
    with open(a.out_stats, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    for k, g in [*stats["kinds"].items(), ("ALL", stats["overall"])]:
        print(f"{k:14} rows {g['rows']:4} samples {g['samples']:4} chosen {g['chosen']:4} "
              f"first-pass {g['first_sample_pass_rate']}  rejects {g['rejects_by_reason']}")


if __name__ == "__main__":
    main()
