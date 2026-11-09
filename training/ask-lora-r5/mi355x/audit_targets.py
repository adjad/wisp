#!/usr/bin/env python3
"""Audit of the Ask LoRA r5 training targets and their effective loss. Analysis only: it changes nothing about training.

    python audit_targets.py --build training/ask-lora-r5/data/r5-final --out OUT --tokenizer ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp

Reads the build's train split (train.jsonl + train.meta.jsonl; the other files only for the stats.json cross-check) and writes
OUT/audit.json, OUT/AUDIT.md, OUT/examples/*.txt (exact rendered text, supervised span in ⟦ ⟧) and OUT/date_candidates.jsonl.
Loads the TOKENIZER only (transformers' tokenizers; no torch, no weights, no GPU).

What is IMPORTED from the trainer (torch-free, checked by test_audit_targets.py):
  ask-lora-r3/brev/render.py : render() (row -> prompt/completion text), FORCED, EMPTY_THOUGHT, row_shape(), STOPS
  ask-lora-r3/brev/train.py  : tokenize() (prompt/completion -> ids, max_len skip) and sequence() (ids + labels, -100 over the prompt)
  ask-lora-r5/mi355x/r5_stats.py : origin_of(), trace_origin(), is_forced()
What is MIRRORED (cites in MIRRORED below): the per-row loss reduction, which lives inside torch code (row_losses / the train loop).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
R5 = HERE.parent
BREV = R5.parent / "ask-lora-r3" / "brev"
SFT_SH = R5.parent / "ask-lora-r4" / "a6000" / "run_sft.sh"
RUN_R5 = HERE / "run_r5.sh"
MARK_L, MARK_R = "⟦", "⟧"

MIRRORED = {
    "labels": "train.py sequence(): labels = [-100]*len(prompt_ids) + completion_ids (imported, not mirrored)",
    "ce": "train.py row_losses(): F.cross_entropy(logits, target) = MEAN over the completion tokens of the row (mirrored as per-row mean)",
    "row_weight": "train.py train loop: (loss / len(gidx)).backward() with gidx = the global batch of 8 rows (mirrored: every row weighs 1/8)",
    "anchor": "train.py step_loss(): anchored only if a.anchor_kl > 0 and (mm is not None or is_anchor_text(...)) (mirrored)",
}

STEP_TYPES = ["conversation_final", "final_after_tool", "load", "call_single", "call_parallel", "chain_call_single",
              "chain_call_parallel"]
STEP_LABEL = {
    "conversation_final": "conversation / final without tools",
    "final_after_tool": "final after tool results",
    "load": "load (get_tool_schemas)",
    "call_single": "single call (first step)",
    "call_parallel": "parallel calls (first step)",
    "chain_call_single": "dependent-chain later step, single call",
    "chain_call_parallel": "dependent-chain later step, parallel calls",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def trainer_modules():
    render = _load("ask_lora_render", BREV / "render.py")
    train = _load("ask_lora_train", BREV / "train.py")
    stats = _load("ask_lora_r5_stats", HERE / "r5_stats.py")
    return render, train, stats


# ---------------------------------------------------------------------------------------------------------------- classification

def step_type(ex: dict, meta: dict) -> str:
    """One of STEP_TYPES for the row's TARGET (the last message).
    A call whose previous message is a tool result is a later step of a dependent chain; a first-step call is single or parallel."""
    msgs = ex["messages"]
    last = msgs[-1]
    after_tool = len(msgs) >= 2 and msgs[-2].get("role") == "tool"
    calls = last.get("tool_calls") or []
    if not calls:
        return "final_after_tool" if after_tool else "conversation_final"
    if meta.get("target_kind") == "load" or any(c["function"]["name"] == "get_tool_schemas" for c in calls):
        return "load"
    kind = "parallel" if len(calls) > 1 else "single"
    return f"chain_call_{kind}" if after_tool else f"call_{kind}"


def thought_class(trace_origin: str) -> str:
    return {"base": "base_written", "scripted": "scripted", "none": "absent"}[trace_origin]


# ---------------------------------------------------------------------------------------------------------------- post-tool closure

CLOSE = "<channel|>"


def closes_immediately(prompt: str, completion: str, forced: str = "<|channel>thought\n") -> bool:
    """True if the supervised completion starts with the thought channel closing at once: the prompt already ends inside the
    channel (forced) and the completion starts with `<channel|>` (after at most newlines), or the completion opens the channel
    itself and closes it with nothing between."""
    if prompt.endswith(forced):
        return completion.lstrip("\n").startswith(CLOSE)
    if completion.startswith(forced):
        return completion[len(forced):].lstrip("\n").startswith(CLOSE)
    return False


def follows_tool_result(ex: dict) -> bool:
    msgs = ex["messages"]
    return len(msgs) >= 2 and msgs[-2].get("role") == "tool"


# ---------------------------------------------------------------------------------------------------------------- date scan

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PERIODS = {
    "day": {"today": "today", "tonight": "today", "tomorrow": "tomorrow", "yesterday": "yesterday"},
}
_REL = re.compile(r"\b(this|next|last|previous|past|current|coming)[\s_-]+(week|month)\b")
_DAY = re.compile(r"\b(today|tonight|tomorrow|yesterday)\b")
_WD = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b")
_REL_NORM = {"this": "this", "current": "this", "next": "next", "coming": "next", "last": "last", "previous": "last", "past": "last"}


def relative_terms(text: str) -> dict[str, set[str]]:
    """{'month': {'next'}, 'week': {...}, 'day': {'today'}, 'weekday': {'friday'}} from a text; arguments such as
    period=this_month count the same as prose."""
    t = (text or "").lower()
    out: dict[str, set[str]] = defaultdict(set)
    for m in _REL.finditer(t):
        out[m.group(2)].add(_REL_NORM[m.group(1)])
    for m in _DAY.finditer(t):
        out["day"].add(_PERIODS["day"][m.group(1)])
    for m in _WD.finditer(t):
        out["weekday"].add(m.group(1))
    return dict(out)


def _args_text(calls: list[dict]) -> str:
    return " ".join(t for _, t in _call_texts(calls))


def _call_texts(calls: list[dict]) -> list[tuple[str, str]]:
    out = []
    for c in calls:
        a = c["function"].get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except ValueError:
                pass
        out.append((c["function"]["name"], a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)))
    return out


def current_turn_calls(msgs: list[dict]) -> list[dict]:
    """Tool calls of every assistant message after the last user message (the calls whose results this target follows)."""
    out = []
    for m in reversed(msgs[:-1]):
        if m.get("role") == "user":
            break
        out = (m.get("tool_calls") or []) + out
    return out


def date_conflicts(thought: str, calls: list[tuple[str, str]], answer: str) -> list[dict]:
    """Conflicts between the relative-date words in a thought and (a) EACH call's arguments, (b) the answer. A group (month,
    week, day, weekday) conflicts with a source when both name that group and their word sets are DISJOINT (a thought that weighs
    'today' against 'tomorrow' beside an argument that says 'tomorrow' is not flagged). Calls are checked one by one, so a thought
    that says 'next month' over a summarize_emails call for 'this month' is caught even when another call in the turn says
    'next month'. For human review, not auto-fixing."""
    t = relative_terms(thought)
    found = []
    sources = [(f"call_args:{name}", text) for name, text in calls] + [("answer", answer)]
    for source, text in sources:
        e = relative_terms(text)
        for group, words in t.items():
            ev = e.get(group)
            if ev and not (words & ev):
                found.append({"group": group, "source": source, "thought_words": sorted(words), "evidence_words": sorted(ev)})
    return found


# ---------------------------------------------------------------------------------------------------------------- rendering

def mark_span(prompt_text: str, completion_text: str) -> str:
    return prompt_text + MARK_L + completion_text + MARK_R


def elide_tools(text: str) -> tuple[str, int]:
    """Replace the tool declarations (`<|tool>declaration:...<tool|>` blocks) with a one-line marker; returns (text, chars cut)."""
    cut = 0

    def sub(m):
        nonlocal cut
        cut += len(m.group(0))
        return "<|tool>[…tool declaration elided…]<tool|>"

    return re.sub(r"<\|tool>.*?<tool\|>", sub, text, flags=re.S), cut


def read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


# ---------------------------------------------------------------------------------------------------------------- trainer settings

def cite(path: Path, pattern: str) -> str | None:
    rx = re.compile(pattern)
    try:
        for i, line in enumerate(open(path), 1):
            if rx.search(line):
                return f"{path.relative_to(R5.parent.parent) if path.is_relative_to(R5.parent.parent) else path}:{i}: {line.strip()[:200]}"
    except OSError:
        pass
    return None


def trainer_settings() -> dict:
    t = BREV / "train.py"
    cites = {
        "lr_default": cite(t, r'"--lr"'), "epochs_default": cite(t, r'"--epochs"'), "rank_default": cite(t, r'"--rank"'),
        "alpha_default": cite(t, r'"--alpha"'), "global_batch_default": cite(t, r'"--global-batch"'),
        "lora_config": cite(t, r"LoraConfig\(r="), "optimizer": cite(t, r"AdamW\(params"),
        "scheduler": cite(t, r"get_cosine_schedule_with_warmup\(opt"), "grad_clip": cite(t, r"clip_grad_norm_"),
        "row_mean": cite(t, r"F\.cross_entropy\(logits, target\)"), "row_weight": cite(t, r"loss / len\(gidx\)"),
        "anchor_rule": cite(t, r"anchored = a\.anchor_kl > 0"), "anchor_default": cite(t, r"^ANCHOR_KL ="),
        "anchor_text_frac": cite(t, r'"--anchor-text-frac"'), "vision_weight": cite(t, r"^VISION_WEIGHT ="),
        "audio_weight": cite(t, r"^AUDIO_WEIGHT ="), "labels": cite(t, r"labels = \[-100\] \* len\(p\)"),
        "run_sft_lr": cite(SFT_SH, r"LR=2e-4"), "run_sft_args": cite(SFT_SH, r"--anchor-kl"),
        "run_r5_overrides": cite(RUN_R5, r"anchor|--lr|EXTRA_TRAIN_ARGS=") or "none: run_r5.sh sets no loss, weight, KL or lr flag",
    }
    return {
        "learning_rate": 2e-4, "lr_schedule": "cosine, 3% warmup", "epochs": 2, "lora_rank": 16, "lora_alpha": 16,
        "lora_dropout": 0.05, "global_batch_rows": 8, "optimizer": "AdamW, weight_decay 0", "grad_clip_norm": 0.3,
        "max_len": 10240,
        "loss": "cross-entropy, MEAN over the completion tokens of each row; each row then weighs 1/8 of the step",
        "label_mask": "everything in the prompt is -100; EVERY completion token is a label, including the thought channel "
                      "(<|channel>thought ... <channel|>), the tool call or answer, and the stop token",
        "sample_or_family_weights": "none: rows are shuffled once per epoch and weigh the same; no family or origin weight",
        "kl_anchor": {"on": True, "coefficient": 0.5, "applies_to": "image and audio rows only (--vision-weight 0.12, "
                      "--audio-weight 0.06 pools, not in this build dir)", "text_rows": "NO anchor: --anchor-text-frac default 0, "
                      "and neither run_sft.sh nor run_r5.sh sets it"},
        "multimodal_pools": {"vision_weight": 0.12, "audio_weight": 0.06,
                             "note": "added per epoch on top of the text rows, so text rows are ~82% of an epoch's rows when the pools are present"},
        "cites": cites,
        "nothing_changed": "this audit reads the settings; it changes none of them",
    }


# ---------------------------------------------------------------------------------------------------------------- the audit

def thought_token_count(tok, completion: str) -> int:
    """Tokens of the completion that sit inside the thought channel, from its opening (or the forced prefix) to `<channel|>`.
    Special tokens split the text, so tokenizing the pieces apart is exact."""
    if CLOSE not in completion:
        return 0
    head = completion.split(CLOSE, 1)[0] + CLOSE
    return len(tok(head, add_special_tokens=False).input_ids)


def run(build: Path, out: Path, tokenizer_path: str) -> dict:
    from transformers import AutoTokenizer
    render, train, r5s = trainer_modules()
    tok = AutoTokenizer.from_pretrained(tokenizer_path)
    FORCED, EMPTY = render.FORCED, render.EMPTY_THOUGHT
    examples = read_jsonl(build / "train.jsonl")
    metas = read_jsonl(build / "train.meta.jsonl")
    if len(examples) != len(metas):
        raise SystemExit("train.jsonl and train.meta.jsonl differ in length")
    out.mkdir(parents=True, exist_ok=True)

    rendered, errors = [], []
    for ex, m in zip(examples, metas):
        try:
            rendered.append(render.render(tok, ex, m))
        except ValueError as e:
            errors.append({"id": m.get("id"), "error": str(e)})
            rendered.append(None)
    ok = [(ex, m, r) for ex, m, r in zip(examples, metas, rendered) if r is not None]
    toks, skipped = train.tokenize(tok, [r for _, _, r in ok], 10240)
    by_id = {t[0]: t for t in toks}

    rows = []
    for ex, m, r in ok:
        tid = by_id.get(m["id"])
        if tid is None:      # skipped by max_len exactly as the trainer would
            continue
        _, p, c, _ = tid
        ids, labels, _ = train.sequence(p, c)
        sup = sum(1 for l in labels if l != -100)
        origin, trace = r5s.origin_of(ex, m), r5s.trace_origin(m)
        c_text = r["completion"]
        rows.append({
            "id": m["id"], "family": m["family"], "target_kind": m["target_kind"], "origin": origin, "trace_origin": trace,
            "thought": thought_class(trace), "step": step_type(ex, m), "shape": render.row_shape(r), "forced": r["prompt"].endswith(FORCED),
            "after_tool": follows_tool_result(ex), "prompt_tokens": len(p), "sup_tokens": sup,
            "thought_tokens": thought_token_count(tok, c_text), "closed_now": closes_immediately(r["prompt"], c_text, FORCED),
            "_ex": ex, "_m": m, "_r": r, "_p": p, "_c": c,
        })
    n = len(rows)

    # 1. rows with no thought ------------------------------------------------------------------------------------------------
    thought_counts = Counter(x["thought"] for x in rows)
    no_thought = [x for x in rows if x["thought"] == "absent"]
    cube = Counter((x["family"], x["target_kind"], x["origin"]) for x in no_thought)
    full_cube = defaultdict(Counter)
    for x in rows:
        full_cube[(x["family"], x["target_kind"], x["origin"])][x["thought"]] += 1
    shapes_train = Counter(x["shape"] for x in rows)
    stats = json.loads((build / "stats.json").read_text()) if (build / "stats.json").is_file() else {}
    all_shapes = stats.get("rendered_shapes", {})
    valid_meta = build / "valid.meta.jsonl"
    valid_absent = valid_trace = None
    if valid_meta.is_file() and (build / "valid.jsonl").is_file():
        vex, vm = read_jsonl(build / "valid.jsonl"), read_jsonl(valid_meta)
        valid_trace = Counter(r5s.trace_origin(m) for m in vm)
        valid_absent = valid_trace.get("none", 0)
    sec1 = {
        "train_rows_counted": n, "train_rows_in_file": len(examples), "render_errors": errors, "skipped_over_max_len": len(toks) and len(ok) - len(toks),
        "thought_class_counts": dict(thought_counts),
        "no_thought_rows": len(no_thought),
        "no_thought_by_family_kind_origin": [{"family": f, "target_kind": k, "origin": o, "rows": v} for (f, k, o), v in sorted(cube.items(), key=lambda kv: -kv[1])],
        "no_thought_by_kind_origin": [{"target_kind": k, "origin": o, "rows": v} for (k, o), v in sorted(Counter((x["target_kind"], x["origin"]) for x in no_thought).items(), key=lambda kv: -kv[1])],
        "thought_by_kind_origin": [{"target_kind": k, "origin": o, "thought": th, "rows": v} for (k, o, th), v in sorted(
            Counter((x["target_kind"], x["origin"], x["thought"]) for x in rows).items())],
        "no_thought_by_step_type": dict(Counter(x["step"] for x in no_thought)),
        "no_thought_that_follow_a_tool_result": sum(1 for x in no_thought if x["after_tool"]),
        "family_thought_cube": [{"family": f, "target_kind": k, "origin": o, **dict(c)} for (f, k, o), c in sorted(full_cube.items())],
        "render_shapes_train": dict(shapes_train),
        "stats_json_rendered_shapes_all_splits": all_shapes,
        "valid_rows_without_thought": valid_absent,
        "confirm_1597": {
            "stats_json_first_no_think": all_shapes.get("first_no_think"),
            "train_first_answer_rows": shapes_train.get("first_answer", 0),
            "train_plus_valid_no_thought": len(no_thought) + (valid_absent or 0),
            "explanation": "stats.json counts ALL splits (train + valid) and names the shape first_no_think; render.row_shape calls the same "
                           "shape first_answer. The train-only count is the first number; adding valid's no-thought rows gives stats.json's.",
        },
    }
    sec1["confirm_1597"]["matches_stats_json"] = sec1["confirm_1597"]["train_plus_valid_no_thought"] == all_shapes.get("first_no_think")

    # 2. supervision weight --------------------------------------------------------------------------------------------------
    tot_sup = sum(x["sup_tokens"] for x in rows)
    tot_thought = sum(x["thought_tokens"] for x in rows)
    w_all = {}
    for st in STEP_TYPES:
        xs = [x for x in rows if x["step"] == st]
        sup = sum(x["sup_tokens"] for x in xs)
        w_all[st] = {
            "label": STEP_LABEL[st], "rows": len(xs), "row_share_pct": pct(len(xs), n), "supervised_tokens": sup,
            "token_share_pct": pct(sup, tot_sup),
            "effective_share_per_row_mean_pct": pct(len(xs), n),     # each row weighs 1/8 of its step whatever its length
            "mean_supervised_tokens_per_row": round(sup / len(xs), 1) if xs else 0,
            "thought_tokens": sum(x["thought_tokens"] for x in xs),
            "thought_token_share_of_class_pct": pct(sum(x["thought_tokens"] for x in xs), sup),
            "relative_weight_per_token": round((sum(1 / x["sup_tokens"] for x in xs) / len(xs)) / (n / tot_sup), 3) if xs else None,
        }
    groups = {
        "tool_call_rows (call + load)": [s for s in STEP_TYPES if s.startswith(("call", "chain", "load"))],
        "final_rows": ["conversation_final", "final_after_tool"],
    }
    group_rows = {}
    for g, ss in groups.items():
        xs = [x for x in rows if x["step"] in ss]
        group_rows[g] = {"rows": len(xs), "row_share_pct": pct(len(xs), n), "supervised_tokens": sum(x["sup_tokens"] for x in xs),
                         "token_share_pct": pct(sum(x["sup_tokens"] for x in xs), tot_sup)}
    sec2 = {
        "rows": n, "supervised_tokens": tot_sup, "thought_tokens_supervised": tot_thought,
        "thought_token_share_pct": pct(tot_thought, tot_sup),
        "by_step_type": w_all, "groups": group_rows,
        "earlier_figures_checked": {"claim": "tool-call rows ~47% of rows but ~20% of supervised tokens",
                                    "recomputed_row_share_pct": group_rows["tool_call_rows (call + load)"]["row_share_pct"],
                                    "recomputed_token_share_pct": group_rows["tool_call_rows (call + load)"]["token_share_pct"]},
        "loss_weighting": {
            "actual": "per-row mean: CE is the mean over a row's completion tokens and each row weighs 1/8 of its step, so a row's "
                      "weight does not depend on its length and the effective share of a step type equals its ROW share "
                      "(per-token weight is 1/(8 x row tokens): short call rows put far more weight on each token)",
            "token_sum_alternative": "a token-sum loss would give the TOKEN shares in token_share_pct",
            "pools": "image/audio pools (12% / 6% of an epoch's rows by default) are added on top and dilute every text share by ~18%",
            "adam_note": "Adam and clip 0.3 rescale updates, so shares describe gradient proportions, not step sizes",
        },
        "mean_sup_tokens_overall": round(tot_sup / n, 1),
    }

    # 3. post-tool closure ---------------------------------------------------------------------------------------------------
    post = [x for x in rows if x["after_tool"]]
    offenders = [x["id"] for x in post if x["closed_now"]]
    post_no_thought = [x for x in post if x["thought"] == "absent"]
    sec3 = {
        "rows_following_a_tool_result": len(post),
        "forced_rows (prompt ends inside the thought channel)": sum(1 for x in post if x["forced"]),
        "rows_following_a_tool_result_with_no_thought_and_thinking_off": sum(1 for x in post if not x["forced"] and x["thought"] == "absent"),
        "no_thought_rows_following_a_tool_result": len(post_no_thought),
        "offenders_immediately_closed_thought": offenders, "offender_count": len(offenders),
        "detector": "closes_immediately(prompt, completion): prompt ends with '<|channel>thought\\n' and the completion starts with '<channel|>'"
                    " (or the completion opens the channel itself and closes it at once)",
        "all_rows_immediately_closed_any_position": [x["id"] for x in rows if x["closed_now"]],
        "pass": not offenders,
    }

    # 4. exact examples ------------------------------------------------------------------------------------------------------
    def pick(pred):
        for x in rows:
            if pred(x):
                return x
        return None

    wanted = {
        "conversation": pick(lambda x: x["step"] == "conversation_final" and x["thought"] != "absent") or pick(lambda x: x["step"] == "conversation_final"),
        "single_call": pick(lambda x: x["step"] == "call_single" and x["thought"] != "absent") or pick(lambda x: x["step"] == "call_single"),
        "parallel_reads": pick(lambda x: x["step"] == "call_parallel" and x["thought"] != "absent") or pick(lambda x: x["step"] == "call_parallel"),
        "dependent_chain_later_step": pick(lambda x: x["step"].startswith("chain_call") and x["thought"] != "absent") or pick(lambda x: x["step"].startswith("chain_call")),
        "post_tool_answer": pick(lambda x: x["step"] == "final_after_tool" and x["thought"] != "absent") or pick(lambda x: x["step"] == "final_after_tool"),
    }
    exdir = out / "examples"
    exdir.mkdir(exist_ok=True)
    sec4 = {}
    for name, x in wanted.items():
        if x is None:
            sec4[name] = None
            continue
        r = x["_r"]
        full = mark_span(r["prompt"], r["completion"])
        (exdir / f"{name}.txt").write_text(full)
        short, cut = elide_tools(r["prompt"])
        sec4[name] = {"id": x["id"], "family": x["family"], "step_type": x["step"], "thought": x["thought"], "prompt_tokens": len(x["_p"]),
                      "supervised_tokens": x["sup_tokens"], "total_tokens": len(x["_p"]) + len(x["_c"]), "thought_tokens": x["thought_tokens"],
                      "tool_declaration_chars_elided_in_md": cut, "full_text_file": str((exdir / f"{name}.txt").name),
                      "rendered_md": mark_span(short, r["completion"])}

    # 5. thought / answer / call consistency ---------------------------------------------------------------------------------
    cands = []
    scanned = 0
    for x in rows:
        thought = x["_m"].get("thought")
        if x["thought"] == "absent" or not isinstance(thought, str):
            continue
        scanned += 1
        ex = x["_ex"]
        last = ex["messages"][-1]
        calls = last.get("tool_calls") or current_turn_calls(ex["messages"])
        answer = "" if last.get("tool_calls") else (last.get("content") or "")
        hits = date_conflicts(thought, _call_texts(calls), answer)
        if hits:
            cands.append({"id": x["id"], "family": x["family"], "origin": x["origin"], "trace_origin": x["trace_origin"],
                          "target_kind": x["target_kind"], "calls": [c["function"]["name"] for c in calls],
                          "conflicts": hits, "thought_excerpt": thought[:300], "args": _args_text(calls)[:300]})
    next_month = [c for c in cands if any(h["group"] == "month" and h["source"].startswith("call_args") and "next" in h["thought_words"] and "this" in h["evidence_words"] for h in c["conflicts"])]
    with open(out / "date_candidates.jsonl", "w") as f:
        for c in cands:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    sec5 = {
        "rows_with_a_thought_scanned": scanned, "candidate_count": len(cands),
        "by_source": dict(Counter(h["source"].split(":")[0] for c in cands for h in c["conflicts"])),
        "by_origin": dict(Counter(c["trace_origin"] for c in cands)),
        "by_group": dict(Counter(h["group"] for c in cands for h in c["conflicts"])),
        "next_month_vs_this_month": [{"id": c["id"], "family": c["family"], "origin": c["origin"], "calls": c["calls"], "args": c["args"]} for c in next_month],
        "candidates": [{k: c[k] for k in ("id", "family", "origin", "trace_origin", "target_kind", "calls", "conflicts")} for c in cands],
        "note": "heuristic for human review; a word set conflicts only when the thought and the source name the same group "
                "(month, week, day, weekday) with DISJOINT words. Not auto-fixed.",
    }

    audit = {
        "build": str(build), "tokenizer": tokenizer_path, "split": "train",
        "1_no_thought": sec1, "2_supervision_weight": sec2, "3_post_tool_closure": sec3, "4_examples": sec4,
        "5_date_consistency": sec5, "6_trainer_settings": trainer_settings(), "mirrored_from_trainer": MIRRORED,
    }
    (out / "audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False))
    (out / "AUDIT.md").write_text(markdown(audit))
    return audit


# ---------------------------------------------------------------------------------------------------------------- markdown

def table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def markdown(a: dict) -> str:
    s1, s2, s3, s4, s5, s6 = (a[k] for k in ("1_no_thought", "2_supervision_weight", "3_post_tool_closure", "4_examples",
                                              "5_date_consistency", "6_trainer_settings"))
    L = [f"# Ask LoRA r5 target audit (TRAIN split of `{a['build']}`)", "",
         "Analysis only. Nothing about training was changed. Rendering and label masking come from the trainer's own `render.render`, "
         "`train.tokenize` and `train.sequence`.", "",
         "## 1. Rows with no thought", "",
         f"Train rows audited: {s1['train_rows_counted']} (of {s1['train_rows_in_file']}; {len(s1['render_errors'])} render errors). "
         f"Thought written by: {s1['thought_class_counts']}.", "",
         f"**No-thought rows: {s1['no_thought_rows']}** ({s1['no_thought_rows'] * 100 // max(1, s1['train_rows_counted'])}% of train), "
         f"of which {s1['no_thought_that_follow_a_tool_result']} follow a tool result.", "",
         "By target kind x origin (all absent thoughts):", "",
         table(["target_kind", "origin", "rows"], [[r["target_kind"], r["origin"], r["rows"]] for r in s1["no_thought_by_kind_origin"]]), "",
         "Thought class by target kind x origin (all train rows):", "",
         table(["target_kind", "origin", "thought", "rows"], [[r["target_kind"], r["origin"], r["thought"], r["rows"]] for r in s1["thought_by_kind_origin"]]), "",
         "No-thought rows by family x target kind x origin (full cube, with base/scripted counts, in audit.json `family_thought_cube`):", "",
         table(["family", "target_kind", "origin", "rows"], [[r["family"], r["target_kind"], r["origin"], r["rows"]] for r in s1["no_thought_by_family_kind_origin"][:60]]),
         "" if len(s1["no_thought_by_family_kind_origin"]) <= 60 else f"\n(first 60 of {len(s1['no_thought_by_family_kind_origin'])} cells)\n", "",
         "### The 1,597 figure", "",
         f"- stats.json `rendered_shapes.first_no_think` = {s1['confirm_1597']['stats_json_first_no_think']} counts ALL splits.",
         f"- Train-only no-thought rows (`first_answer` in `render.row_shape`, same shape under another name) = {s1['confirm_1597']['train_first_answer_rows']}; "
         f"valid adds {s1['valid_rows_without_thought']}; sum = {s1['confirm_1597']['train_plus_valid_no_thought']}. "
         f"Matches stats.json: **{s1['confirm_1597']['matches_stats_json']}**.",
         f"- Train render shapes: {s1['render_shapes_train']}.", ""]
    L += ["## 2. Supervision weight", "",
          f"Supervised (label != -100) tokens: {s2['supervised_tokens']} over {s2['rows']} rows (mean {s2['mean_sup_tokens_overall']} per row). "
          f"Of those, {s2['thought_tokens_supervised']} ({s2['thought_token_share_pct']}%) are inside the thought channel; the trainer supervises them like any other token.", "",
          table(["step type", "rows", "row share %", "sup tokens", "token share %", "effective share, per-row mean %", "mean tok/row", "thought tok % of class", "rel. weight per token"],
                [[v["label"], v["rows"], v["row_share_pct"], v["supervised_tokens"], v["token_share_pct"], v["effective_share_per_row_mean_pct"],
                  v["mean_supervised_tokens_per_row"], v["thought_token_share_of_class_pct"], v["relative_weight_per_token"]] for v in s2["by_step_type"].values()]), "",
          table(["group", "rows", "row share %", "sup tokens", "token share %"],
                [[g, v["rows"], v["row_share_pct"], v["supervised_tokens"], v["token_share_pct"]] for g, v in s2["groups"].items()]), "",
          f"Earlier figure (~47% of rows, ~20% of tokens) recomputed on train: **{s2['earlier_figures_checked']['recomputed_row_share_pct']}% of rows, "
          f"{s2['earlier_figures_checked']['recomputed_token_share_pct']}% of supervised tokens**.", "",
          f"- Actual loss: {s2['loss_weighting']['actual']}.", f"- If it were a token sum: {s2['loss_weighting']['token_sum_alternative']}.",
          f"- {s2['loss_weighting']['pools']}.", f"- {s2['loss_weighting']['adam_note']}.", ""]
    L += ["## 3. Post-tool closure", "",
          f"Rows whose target follows a tool result: {s3['rows_following_a_tool_result']} ({s3['forced_rows (prompt ends inside the thought channel)']} forced, "
          f"{s3['no_thought_rows_following_a_tool_result']} with no thought at all). "
          f"**Immediately closed thoughts supervised: {s3['offender_count']}** -> {'PASS' if s3['pass'] else 'FAIL'}.", ""]
    if s3["offenders_immediately_closed_thought"]:
        L += ["Offenders:", ""] + [f"- `{i}`" for i in s3["offenders_immediately_closed_thought"]] + [""]
    L += [f"Detector: {s3['detector']}.", ""]
    L += ["## 4. Exact rendered examples", "",
          f"Supervised tokens are wrapped in {MARK_L} {MARK_R}. Tool declarations are elided here only (the full exact text of each is in `examples/<name>.txt`).", ""]
    for name, e in s4.items():
        if not e:
            L += [f"### {name}", "", "No such row in this build.", ""]
            continue
        L += [f"### {name}: `{e['id']}`", "", f"family {e['family']}, step type {e['step_type']}, thought {e['thought']}; prompt {e['prompt_tokens']} tokens, "
              f"supervised {e['supervised_tokens']} tokens ({e['thought_tokens']} in the thought), total {e['total_tokens']}.", "", "```", e["rendered_md"], "```", ""]
    L += ["## 5. Thought / answer / call consistency (heuristic)", "",
          f"Scanned {s5['rows_with_a_thought_scanned']} rows with a thought; **{s5['candidate_count']} candidates** (by source {s5['by_source']}, "
          f"by thought origin {s5['by_origin']}, by group {s5['by_group']}). Human review only; nothing is auto-fixed.", "",
          "Known case (thought says next month, call says this month): " + (", ".join(f"`{c['id']}` ({c['family']}, {c['origin']}, {c['calls']}, args {c['args']})" for c in s5["next_month_vs_this_month"]) or "NOT FOUND"), "",
          table(["id", "family", "origin", "thought by", "target", "group", "source", "thought words", "evidence words"],
                [[c["id"], c["family"], c["origin"], c["trace_origin"], c["target_kind"], h["group"], h["source"], "/".join(h["thought_words"]), "/".join(h["evidence_words"])]
                 for c in s5["candidates"] for h in c["conflicts"]]), ""]
    L += ["## 6. Trainer settings as they stand", ""]
    L += [f"- Learning rate {s6['learning_rate']} ({s6['lr_schedule']}), {s6['epochs']} epochs, LoRA rank {s6['lora_rank']} alpha {s6['lora_alpha']} dropout {s6['lora_dropout']}, "
          f"global batch {s6['global_batch_rows']} rows, {s6['optimizer']}, grad-norm clip {s6['grad_clip_norm']}, max_len {s6['max_len']}.",
          f"- Loss: {s6['loss']}.", f"- Labels: {s6['label_mask']}.", f"- Weights: {s6['sample_or_family_weights']}.",
          f"- KL anchor: **on**, coefficient {s6['kl_anchor']['coefficient']}, applied to {s6['kl_anchor']['applies_to']}. Text rows: {s6['kl_anchor']['text_rows']}.",
          f"- Multimodal pools: {s6['multimodal_pools']['note']}.", "", "Sources:", ""]
    L += [f"- {v}" for v in s6["cites"].values() if v]
    L += ["", "### Risks, stated plainly", "",
          "- The anchor never touches text rows, so nothing but the cross-entropy holds the tuned model near the base on chat and on tool-using text; "
          "drift on those rows is measured (dev eval) but not penalised.",
          "- Thoughts are fully supervised. A non-empty thought is not evidence of good reasoning, and scripted thoughts (rows above) are templated text the model "
          "will imitate; any conflict listed in section 5 is a wrong supervision signal if it is real.",
          "- Per-row mean loss gives short tool-call rows a far higher weight per token than long answers; the row share, not the token share, is what the gradient sees.",
          "- Rows without a thought teach 'answer or call with no thought'. Whether that is wanted per family is a data decision, not something this audit settles.",
          "- Crusoe's study used Gemma 31B, not E4B. Its findings transfer only as hypotheses here and were not verified on this model.",
          "- This is a static audit of the data and the trainer's code. No model was loaded; it says nothing yet about behaviour.", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tokenizer", required=True)
    a = ap.parse_args()
    audit = run(a.build.resolve(), a.out, str(Path(a.tokenizer).expanduser()))
    s1, s2, s3, s5 = audit["1_no_thought"], audit["2_supervision_weight"], audit["3_post_tool_closure"], audit["5_date_consistency"]
    print(f"no-thought {s1['no_thought_rows']}/{s1['train_rows_counted']}; tool-call rows {s2['groups']['tool_call_rows (call + load)']}; "
          f"closure offenders {s3['offender_count']}; date candidates {s5['candidate_count']}; wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
