#!/usr/bin/env python3
"""Turn base's answers into r5's final build, on the Mac. Then run the builder's validate.py on --out, and pack_r4.py --data <out>.

    finish_r5.py --build BUILD --chosen chosen.jsonl --drop drop.ids --out FINAL [--traces traces.chosen.jsonl]
                 --choose-stats stats.json [--rejections rejections.jsonl] [--allow-incomplete] --wisp-repo PIN

Steps, in order:
1. Copy the build and splice ``chosen.jsonl`` (base's answers) into it.
2. Splice ``--traces``: base's own thought at tool-call steps where base's call matched the script. The call stays scripted.
3. Mark every row's origin (see ``r5_stats``).
4. Remove rows, each with its reason in ``drop_manifest.jsonl``: answers still the placeholder, questions close to a test
   question (no exemption), an empty thought after a tool result with nothing to replace it (a tool step takes its whole turn with
   it), rows base answered with a tool call in a ``TOOL_DROP_FAMILIES`` family (the box's own list, plus scripted rows whose
   rejections called a tool in a family added after the box's chooser ran: chat_voice_r4, "Drop them too", 2026-11-08), any
   chosen answer that fails ``source_contract`` here even though it passed the chooser (which means the two have
   drifted), and any base-written row the builder's ``validate.py`` flags (the user's call, 2026-11-08: "Drop them"). The box's
   chooser doesn't run the builder's checks, so base can pass it while repeating a planted instruction, claiming it lacks a tool
   without looking, or quoting a toolbench fact. A flagged tool step takes its turn with it. A scripted row the validator flags
   stays, so validation still fails on it: that's a build bug, not base's answer.
5. With ``--review``, apply the human-style semantic review (``semantic_review.jsonl``, reasons per row). ``REPLACE`` swaps a chosen
   answer for another sample of the SAME prompt, only if that sample passes the chooser's own checks and hash (``--raw``); nothing
   is rewritten by hand. ``DROP`` removes the row, or its whole turn with ``scope: turn``. ``KEEP`` and ``NOTE`` record a looked-at flag.
6. Refuse a build that still holds a placeholder, and recount ``stats.json`` from the rows left.

**A build is ``complete`` only when every final was actually sampled.** Without ``--allow-incomplete``, finals that base never
sampled stop the run. With it, the output's status is ``incomplete_no_base_answers``, ``pack_r4.py`` refuses to pack it, and it must
never be called the dataset: it exists to check the pipeline.

The empty thought: after a tool result, the served prompt ends inside the thought channel, so a row with no thought supervises
``\\n<channel|>``, which trains the model to close it at once. Base nearly always thinks there (0.999 in r4's gate). So such a row
gets base's own thought or it leaves the build. First-step rows are different: Gemma's template renders nothing when there's no
thought, so they stay, and they're counted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TRAINING / "ask-lora-r5" / "gpu"))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import choose_r5  # noqa: E402
import r5_stats  # noqa: E402
import source_contract  # noqa: E402

SPLICE = TRAINING / "ask-lora-r4" / "distill" / "splice.py"
VALIDATE = TRAINING / "ask-lora-r2" / "builder" / "validate.py"
PLACEHOLDER = r5_stats.PLACEHOLDER    # scen_r5_debuglog.BASE_WRITES; test_finish_r5.py compares them
SPLITS = {"all": "meta.jsonl", "train": "train.meta.jsonl", "valid": "valid.meta.jsonl"}    # split -> its meta file
REUSE_MODEL = "r4-served-base"


def drop_rows(out: Path, drop: set) -> dict:
    """Remove the dropped ids from each split and its meta, keeping the two line-aligned. Returns rows removed per split."""
    removed = {}
    for split, meta_name in SPLITS.items():
        rows, metas = out / f"{split}.jsonl", out / meta_name
        keep_r, keep_m, n = [], [], 0
        with open(rows) as fr, open(metas) as fm:
            for line, meta in zip(fr, fm, strict=True):
                if json.loads(meta)["id"] in drop:
                    n += 1
                else:
                    keep_r.append(line)
                    keep_m.append(meta)
        for path, lines in ((rows, keep_r), (metas, keep_m)):
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(lines))
            os.replace(tmp, path)
        removed[split] = n
    return removed


def rewrite_metas(out: Path, change) -> None:
    """Apply ``change(meta)`` (in place) to every split's meta lines."""
    for meta_name in SPLITS.values():
        path = out / meta_name
        lines = []
        with open(path) as f:
            for line in f:
                m = json.loads(line)
                change(m)
                lines.append(json.dumps(m, ensure_ascii=False) + "\n")
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines))
        os.replace(tmp, path)


def leaking_turns(out: Path) -> set:
    """Row ids of every turn whose question is too close to a test question. seal_r5.py removes these before base writes; this
    is the last check, against this checkout's toolbench, and leak_ok is no exemption."""
    import leak
    import writes
    index = leak.LeakIndex()
    prefixes = set()
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        rows = [(json.loads(a), json.loads(b)) for a, b in zip(fr, fm, strict=True)]
    for ex, meta in rows:
        msgs, roles = ex["messages"], [m["role"] for m in ex["messages"]]
        i = roles.index("user")
        if meta.get("family") in writes.MULTITURN:
            i = max(j for j, r in enumerate(roles) if r == "user")
        hit, _score, _q = index.leaks(msgs[i].get("content") or "")
        if hit:
            prefixes.add(meta["id"].rsplit("-s", 1)[0])
    return {meta["id"] for _ex, meta in rows if meta["id"].rsplit("-s", 1)[0] in prefixes}


def placeholders_left(out: Path) -> list:
    left = []
    for split, meta_name in SPLITS.items():
        with open(out / f"{split}.jsonl") as fr, open(out / meta_name) as fm:
            for line, meta in zip(fr, fm, strict=True):
                if json.loads(line)["messages"][-1].get("content") == PLACEHOLDER:
                    left.append(json.loads(meta)["id"])
    return left


def never_sampled_ids(stats_path) -> set:
    """Finals base never wrote a sample for, placeholders included. An unreadable report is an error: without it a row that was
    never sampled would look like one that was sampled and failed, and the build would call itself complete."""
    rep = (json.load(open(stats_path)).get("r5") or {}).get("families")
    if rep is None:
        sys.exit(f"finish_r5: {stats_path} has no r5 family report; pass choose_r5.py's --out-stats")
    return {i for f in rep.values() for i in f.get("no_samples", [])}


def tool_drops(stats_path) -> dict:
    """choose_r5's chat and knowledge rows base answered with a tool call, which the user chose to drop:
    {id: (family, tools its samples called)}."""
    rep = json.load(open(stats_path)).get("r5") or {}
    return {i: (name, list(tools)) for name, f in (rep.get("families") or {}).items() for i, tools in (f.get("drop_tool") or {}).items()}


def tool_drops_after_choose(stats_path, rejections_path) -> dict:
    """Rows the box kept scripted after base called a tool, in a family added to ``TOOL_DROP_FAMILIES`` after its chooser ran
    (chat_voice_r4, the user's "Drop them too", 2026-11-08): the same rule, applied here from the rejection manifest. Same shape
    as ``tool_drops``. Without the manifest such a row can't be told from one that failed in text only, so that stops the build."""
    fams = (json.load(open(stats_path)).get("r5") or {}).get("families") or {}
    kept = {i: name for name, f in fams.items() if name in choose_r5.TOOL_DROP_FAMILIES for i in f.get("scripted_kept") or []}
    if not kept:
        return {}
    if not rejections_path:
        if any((fams[name].get("scripted_kept_after_tool") or {}) for name in set(kept.values())):
            sys.exit("finish_r5: scripted rows in a tool-drop family called tools on the box; pass --rejections to tell which")
        return {}
    called: dict[str, set] = {}
    for line in open(rejections_path):
        r = json.loads(line)
        if r.get("id") in kept:
            called.setdefault(r["id"], set()).update(r.get("tools") or [])
    return {i: (kept[i], sorted(t)) for i, t in sorted(called.items()) if t}


def tool_drop_report(dropped: dict, out: Path, families) -> dict:
    """Per family: finals dropped because base answered with a tool call, how many of them called each tool (a row whose
    samples called two tools counts under both), and the rows each family has left."""
    left = Counter(json.loads(line)["family"] for line in open(out / "meta.jsonl"))
    per, by_tool = Counter(), {}
    for fam, tools in dropped.values():
        per[fam] += 1
        by_tool.setdefault(fam, Counter()).update(tools)
    return {"dropped": sum(per.values()),
            "families": {f: {"dropped": per.get(f, 0), "by_tool": dict((by_tool.get(f) or Counter()).most_common()),
                             "rows_left": left.get(f, 0)} for f in sorted(families)}}


def load_chosen(path) -> dict:
    return {r["id"]: r for r in map(json.loads, open(path)) if r.get("id")}


def contract_failures(out: Path) -> dict:
    """id -> problem keys, for each base-written final that fails the contract here."""
    bad = {}
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            last = ex["messages"][-1]
            if m.get("distilled") and not last.get("tool_calls"):
                probs = source_contract.final_problems(last.get("content") or "", m, ex["messages"][:-1])
                if probs:
                    bad[m["id"]] = [k for k, _ in probs]
    return bad


def validator_failures(out: Path, wisp_repo: str | None) -> dict:
    """id -> the validator's problem keys, for each base-written row (``distilled``) it flags in ``all.jsonl``. It runs against
    the pinned service code the rows were built from, never this checkout's."""
    if not wisp_repo:
        sys.exit("finish_r5: pass --wisp-repo PIN_DIR (or set WISP_REPO): the validator must check against the build's pin")
    sink = out / "validate.problems.jsonl"
    env = {**os.environ, "ASK_LORA_ROUND": "4", "ASK_LORA_R5": "1", "ASK_LORA_OUT": str(out), "WISP_REPO": wisp_repo}
    r = subprocess.run([sys.executable, str(VALIDATE), str(out / "all.jsonl"), "--problems-out", str(sink)], env=env,
                       cwd=VALIDATE.parent, capture_output=True, text=True)
    if r.returncode not in (0, 1) or not sink.exists():
        sys.exit(f"finish_r5: validate.py crashed (exit {r.returncode}): {r.stderr[-600:]}")
    keys: dict[int, set] = {}
    for p in map(json.loads, open(sink)):
        if p.get("file") == "all.jsonl" and p.get("line"):
            keys.setdefault(p["line"], set()).add(p["key"])
    bad = {}
    for ln, line in enumerate(open(out / "meta.jsonl"), 1):
        if ln in keys:
            m = json.loads(line)
            if m.get("distilled"):
                bad[m["id"]] = sorted(keys[ln])
    return bad


def forced_empty_rows(out: Path, *, placeholders: bool = False) -> set:
    """Rows that would supervise an immediately closed thought: by default those with text, or with ``placeholders`` only
    the placeholder finals in that state (they have no text yet, so they're counted but never dropped for it)."""
    ids = set()
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            if r5_stats.is_forced(ex, m) and not m.get("think") and (ex["messages"][-1].get("content") == PLACEHOLDER) == placeholders:
                ids.add(m["id"])
    return ids


def forced_empty_counts(forced_empty: set, forced_placeholders: set, never_sampled: set, awaiting_traces: set, dropped: set) -> dict:
    """Every kept row that still ends in an empty thought after a tool result, by what it waits for. The two totals add up to
    stats' rendered_shapes forced_empty, and finish refuses a build where they don't."""
    scripted = (forced_empty & never_sampled) - dropped
    placeholder = forced_placeholders - dropped
    return {"forced_empty_awaiting_base": len(scripted | placeholder),
            "forced_empty_awaiting_base_by_text": {"scripted_final": len(scripted - placeholder), "placeholder_final": len(placeholder)},
            "forced_tool_steps_awaiting_traces": len(awaiting_traces - dropped)}


def turn_of(row_id: str) -> str:
    return row_id.rsplit("-s", 1)[0]


def target_kinds(out: Path) -> dict:
    with open(out / "meta.jsonl") as fm:
        return {m["id"]: m.get("target_kind") for m in map(json.loads, fm)}


def provenance_lines(out: Path, chosen: dict, traces: dict):
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            c = chosen.get(m["id"]) or {}
            t = traces.get(m["id"]) or {}
            yield {"id": m["id"], "family": m["family"], "target_kind": m["target_kind"],
                   "origin": r5_stats.origin_of(ex, m), "trace_origin": r5_stats.trace_origin(m),
                   "sample_k": c.get("k", t.get("k")), "model": c.get("model") or t.get("model"),
                   "request_hash": c.get("request_hash") or t.get("request_hash"), "attn": c.get("attn"), "batch": c.get("batch")}


REVIEW_DECISIONS = ("DROP", "KEEP", "REPLACE", "NOTE")


def load_review(path) -> dict:
    """semantic_review.jsonl -> {id: decision row}. A malformed file stops the build: a review that half applies is worse than none."""
    review = {}
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        i = r.get("id")
        if not i or i in review:
            sys.exit(f"finish_r5: review row without a unique id: {i!r}")
        d = r.get("decision")
        if d not in REVIEW_DECISIONS or not r.get("why"):
            sys.exit(f"finish_r5: review row {i} needs a decision in {REVIEW_DECISIONS} and a reason")
        if d == "DROP" and r.get("scope") not in ("row", "turn"):
            sys.exit(f"finish_r5: DROP {i} needs scope row or turn")
        if d == "REPLACE" and not isinstance(r.get("k"), int):
            sys.exit(f"finish_r5: REPLACE {i} needs the sample k")
        review[i] = r
    return review


def replace_chosen(review: dict, chosen: dict, raws: list, partial) -> list:
    """Swap each REPLACE row's chosen record for the named raw sample, in ``chosen``. The sample must exist for the prompt's current
    hash and pass the chooser's own checks, or the build stops: a review can pick among base's answers, never excuse one."""
    import choose
    wanted = {i: r["k"] for i, r in review.items() if r["decision"] == "REPLACE"}
    if not wanted:
        return []
    if not raws:
        sys.exit("finish_r5: the review replaces answers; pass --raw with base's saved samples")
    samples = {}
    for path in raws:
        for rec in choose.read_jsonl(path):
            if wanted.get(rec.get("id")) == rec.get("k") and not rec.get("error"):
                samples.setdefault(rec["id"], rec)
    finals = {r["meta"]["id"]: r for r in choose.read_jsonl(partial) if choose_r5.is_final(r) and r["meta"]["id"] in wanted}
    rows = list(finals.values())
    choose.apply_refs(rows, choose_r5.gold_refs(rows))
    reject = choose_r5.make_reject()
    done = []
    for i, k in sorted(wanted.items()):
        rec, row = samples.get(i), finals.get(i)
        if rec is None or row is None:
            sys.exit(f"finish_r5: REPLACE {i}: no saved sample k={k} or no such final")
        if rec.get("request_hash") != choose.request_hash(row["messages"][:-1], row.get("tools")):
            sys.exit(f"finish_r5: REPLACE {i}: sample k={k} was written for a different prompt")
        problems = reject(rec, row)
        if problems:
            sys.exit(f"finish_r5: REPLACE {i}: sample k={k} fails the chooser: {problems}")
        new = dict(rec)
        new["reasoning"] = (new.get("reasoning") or "").strip() or None
        done.append({"id": i, "old_k": (chosen.get(i) or {}).get("k"), "new_k": k})
        chosen[i] = new
    return done


def review_drops(review: dict, tool_steps: dict) -> dict:
    """{row id: reason} for the review's DROPs. A row the review names must exist in this build, so a typo can't pass as done."""
    unknown = sorted(i for i in review if i not in tool_steps)
    if unknown:
        sys.exit(f"finish_r5: {len(unknown)} review ids aren't in the build, such as {unknown[:3]}")
    out = {}
    for i, r in review.items():
        if r["decision"] != "DROP":
            continue
        out[i] = "semantic_review"
        if r["scope"] == "turn":
            for other in tool_steps:
                if turn_of(other) == turn_of(i) and other != i:
                    out.setdefault(other, "turn_lost_semantic_review")
    return out


def review_report(review: dict, drops: dict, replaced: list, out: Path) -> dict:
    fam = {m["id"]: m["family"] for m in map(json.loads, open(out / "meta.jsonl"))}
    reviewed = Counter(fam[i] for i in review)
    dropped = [i for i, r in review.items() if r["decision"] == "DROP" and drops.get(i) == "semantic_review"]
    return {"decisions": dict(Counter(r["decision"] for r in review.values())), "rows_dropped": len(dropped),
            "turn_rows_dropped": sum(1 for why in drops.values() if why == "turn_lost_semantic_review"),
            "dropped_by_category": dict(Counter(review[i].get("category") or "NONE" for i in dropped).most_common()),
            "dropped_by_family": dict(Counter(fam[i] for i in dropped).most_common()),
            "reviewed_by_family": dict(reviewed.most_common()), "replaced": replaced}


def write_jsonl(path: Path, rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--chosen", required=True)
    ap.add_argument("--drop", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--traces")
    ap.add_argument("--rejections")
    ap.add_argument("--choose-stats", required=True, help="choose_r5.py's --out-stats: tells never sampled from sampled and failed")
    ap.add_argument("--review", help="semantic_review.jsonl: rows to drop or replace after review, each with its reason")
    ap.add_argument("--raw", nargs="*", default=[], help="base's saved sample files, for the review's REPLACE rows")
    ap.add_argument("--allow-incomplete", action="store_true", help="for checking the pipeline only: the output can't be packed")
    ap.add_argument("--wisp-repo", default=os.environ.get("WISP_REPO"), help="the build's pinned service code, for validate.py")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        sys.exit(f"finish_r5: {out} already exists")
    late = tool_drops_after_choose(a.choose_stats, a.rejections)      # before writing anything: it can refuse the inputs
    review = load_review(a.review) if a.review else {}
    chosen = load_chosen(a.chosen)
    replaced = replace_chosen(review, chosen, a.raw, Path(a.build) / "partial.jsonl")
    chosen_path = a.chosen
    if replaced:                                                      # the splice reads a file: give it the reviewed answers
        chosen_tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        chosen_tmp.write("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chosen.values()))
        chosen_tmp.close()
        chosen_path = chosen_tmp.name
    r = subprocess.run([sys.executable, str(SPLICE), "--build", a.build, "--chosen", chosen_path, "--out", str(out)])
    if r.returncode:
        sys.exit(f"finish_r5: splice failed with exit {r.returncode}")
    traces = load_chosen(a.traces) if a.traces else {}

    def mark(m):
        c = chosen.get(m["id"])
        if c and m.get("distilled"):
            m["origin"] = "base_r4_reuse" if c.get("model") == REUSE_MODEL else "base_generated"
            m["trace_origin"] = "base"
        t = traces.get(m["id"])
        if t and m.get("target_kind") in ("call", "load"):
            m["think"], m["thought"], m["distilled"], m["trace_origin"] = True, t["reasoning"], True, "base"
            m["trace_k"] = t["k"]
    rewrite_metas(out, mark)

    # Never sampled is not the same as sampled and failed. choose_r5 lists both in drop.ids, so the stats tell them apart.
    never_sampled = never_sampled_ids(a.choose_stats)
    tooled = tool_drops(a.choose_stats)
    known_placeholders = set(placeholders_left(out))
    drops: dict[str, str] = {}
    for line in open(a.drop):
        i = line.strip()
        if i and i not in never_sampled:
            drops[i] = "base_answered_with_tool" if i in tooled else "no_base_answer_passed"
    missing = sorted(set(tooled) - set(drops))
    if missing:
        sys.exit(f"finish_r5: {len(missing)} tool-call drops in the choose stats are missing from {a.drop}, such as {missing[:3]}")
    for i in late:
        drops[i] = "base_answered_with_tool"
    tooled.update(late)
    for i in leaking_turns(out):
        drops.setdefault(i, "close_to_test_question")
    # A final base hasn't written yet has no thought *yet*: it only earns this drop once base has been asked and failed. A tool
    # step is the same in a pipeline check with no --traces. A dropped tool step takes its whole turn: a chain with a hole in it
    # isn't a chain, and the earlier steps' thoughts name dates only the missing call can vouch for.
    forced_empty = forced_empty_rows(out)
    forced_placeholders = forced_empty_rows(out, placeholders=True)
    tool_steps = target_kinds(out)
    awaiting_traces = {i for i in forced_empty if tool_steps.get(i) != "final"} if a.allow_incomplete and not a.traces else set()
    for i in forced_empty - never_sampled - awaiting_traces:
        drops.setdefault(i, "forced_empty_thought")
        if tool_steps.get(i) != "final":
            for other in tool_steps:
                if turn_of(other) == turn_of(i):
                    drops.setdefault(other, "turn_lost_a_tool_step")
    failures = contract_failures(out)
    for i in failures:
        drops.setdefault(i, "contract_failed_after_choose")
    for i, keys in validator_failures(out, a.wisp_repo).items():
        failures.setdefault(i, keys)
        drops.setdefault(i, "validator_failed_after_choose")
        if tool_steps.get(i) != "final":
            for other in tool_steps:
                if turn_of(other) == turn_of(i):
                    drops.setdefault(other, "turn_lost_a_tool_step")

    for i, why in review_drops(review, tool_steps).items():
        drops.setdefault(i, why)
    review_stats = review_report(review, drops, replaced, out) if review else None

    # Rows nothing has answered block a complete build. In a pipeline check they stay visible and are counted.
    unsampled = sorted((known_placeholders & never_sampled) - set(drops))
    unsampled_scripted = len((never_sampled - known_placeholders) - set(drops))
    incomplete = bool(unsampled or unsampled_scripted)
    if incomplete and not a.allow_incomplete:
        shutil.rmtree(out)
        sys.exit(f"finish_r5: {len(unsampled)} placeholder rows and {unsampled_scripted} scripted finals have no base sample; "
                 "generate them first (--allow-incomplete only checks the pipeline)")
    removed = drop_rows(out, set(drops))
    left = placeholders_left(out)
    if left and not a.allow_incomplete:
        sys.exit(f"finish_r5: {len(left)} rows still hold the placeholder, such as {left[:5]}")

    write_jsonl(out / "drop_manifest.jsonl", ({"id": i, "reason": why, "problems": failures.get(i)} for i, why in sorted(drops.items())))
    write_jsonl(out / "provenance.jsonl", provenance_lines(out, chosen, traces))
    for src, name in ((a.rejections, "rejections.jsonl"), (a.choose_stats, "choose_stats.json"), (chosen_path, "chosen.jsonl"), (a.drop, "drop.ids"),
                      (a.traces, "traces.chosen.jsonl"), (a.review, "semantic_review.jsonl")):
        if src and os.path.isfile(src):
            shutil.copy2(src, out / name)
    drop_reasons = dict(Counter(drops.values()).most_common())
    status = "incomplete_no_base_answers" if incomplete else "complete"
    stats = r5_stats.refresh(out, status=status, step="finish", extra={"drops": drop_reasons, "placeholders_left": len(set(left)),
                                                       "chosen_answers": len(chosen), "base_traces": len(traces),
                                                       "unsampled_scripted_finals": unsampled_scripted,
                                                       **({"semantic_review": review_stats} if review_stats else {}),
                                                       "tool_drop": {**tool_drop_report(tooled, out, {f for f, _ in tooled.values()} | choose_r5.TOOL_DROP_FAMILIES),
                                                                     "dropped_after_choose": len(late)},
                                                       **forced_empty_counts(forced_empty, forced_placeholders, never_sampled, awaiting_traces, set(drops))})
    shapes = stats["rendered_shapes"].get("forced_empty", 0)
    accounted = stats["forced_empty_awaiting_base"] + stats["forced_tool_steps_awaiting_traces"]
    if accounted != shapes:
        sys.exit(f"finish_r5: {shapes} rows end in an empty thought after a tool result, but only {accounted} are accounted for")
    print(json.dumps({"status": status, "drops": drop_reasons, "removed": removed, "rows": {k: stats[k] for k in ("examples", "train", "valid")},
                      "by_origin": stats["by_origin"], "by_trace_origin": stats["by_trace_origin"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
