#!/usr/bin/env python3
"""r4 text checkpoint picker (PLAN.md section F): generate on the valid TEXT steps for each saved checkpoint, score the
generations with grpo/reward.py, and pick the checkpoint that

  1. made no forbidden call (the label's `forbidden` tools, and any reward part that is a forbidden-class penalty),
  2. passes the mmeval picker (vision / reading / audio within tolerance of the untuned base), and
  3. has the best FACTS score among the checkpoints whose SELECTION score is within 1 point of the best selection
     score of the checkpoints that pass 1 and 2.

Selection score = tool set + key arguments, as a percentage of what a perfect step earns (1.5 for a call or a load,
1.0 for an answer or a question). Facts score = share of the label's required facts found in the answer, over the
answer steps that have facts (reward.fact_present). Ties go to the higher selection score, then fewer malformed
completions, then the EARLIER step (less drift from the base).

    python pick_text.py candidates --ckpt-root OUT                      # comma list for --ckpts (steps >= 45% of the run)
    python pick_text.py generate   --base BASE --ckpt-root OUT --data text-dev.jsonl --out DIR [--ckpts a,b] [--limit 360]
    python pick_text.py score      --data text-dev.jsonl --out DIR      # re-score DIR/text_answers.jsonl (no GPU; also the Mac)
    python pick_text.py pick       --text-results DIR/text_results.json --mmeval-results MM/results.json --ckpt-root OUT

`generate` needs torch and the weights (the A6000 box); everything else is pure python and runs on the Mac
(test_pick_text.py runs it on fake generations). Generation is at production sampling (temperature 1.0, top_p 0.95,
top_k 64, thinking on, as the prompt is rendered) with the SAME random seed per (step, sample) for every checkpoint, so
checkpoints are compared on common random numbers; `--greedy` turns sampling off. Batch 1, resumable.

Where the other code comes from: reward.py (this directory, else --reward-dir, else the repo's grpo/) and the mmeval
modules pick_checkpoint / eval_mm / scoring (this directory after `unzip mmeval-data.zip` into it, else --mmeval-dir,
else the repo's ask-lora-r3/mmeval).
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import re
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_TRAINING = HERE.parent.parent            # .../training
STOP_IDS = (1, 50, 106)                        # <eos>, <|tool_response>, <turn|> (train.py's set)
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}      # Wisp's production sampling for Ask
WITHIN_POINTS = 1.0                            # PLAN.md F: "within 1 point of the best selection score"
FORBIDDEN_PARTS = ("forbidden", "planted", "requires_read", "early_write")   # reward parts that count as a forbidden call
STEP_MAX = {"call": 1.5, "load": 1.5, "final": 1.0, "ask": 1.0}


def die(msg: str):
    sys.exit(f"pick_text: {msg}")


# ------------------------------------------------------------------ locating the other code

def _find(name: str, dirs: list) -> Path | None:
    for d in dirs:
        if d and (Path(d) / name).is_file():
            return Path(d)
    return None


def load_reward(reward_dir: str | None = None):
    """reward.py as a module. Search: --reward-dir, this directory, the r4 grpo dir (if r4 adapts it), the r3 grpo dir."""
    d = _find("reward.py", [reward_dir, HERE, REPO_TRAINING / "ask-lora-r4" / "grpo", REPO_TRAINING / "ask-lora-r3" / "grpo"])
    if d is None:
        die("reward.py not found (looked in --reward-dir, next to pick_text.py, ask-lora-r4/grpo, ask-lora-r3/grpo)")
    spec = importlib.util.spec_from_file_location("wisp_reward", d / "reward.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wisp_reward"] = mod
    spec.loader.exec_module(mod)
    return mod


def mmeval_dir(arg: str | None = None) -> Path:
    d = _find("pick_checkpoint.py", [arg, HERE, REPO_TRAINING / "ask-lora-r3" / "mmeval"])
    if d is None:
        die("mmeval code not found (unzip mmeval-data.zip next to pick_text.py, or pass --mmeval-dir)")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    return d


# ------------------------------------------------------------------ rows

def load_text_dev(path) -> list[dict]:
    """Rows {"id", "family", "prompt", "label": {...}}; the label is reward.py's (made by pack_r4.py)."""
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if isinstance(r.get("label"), str):
            r["label"] = json.loads(r["label"])
        for k in ("id", "prompt", "label"):
            if k not in r:
                die(f"{path}:{n}: row has no {k!r}")
        rows.append(r)
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        die(f"{path}: duplicate ids")
    return rows


def subsample(rows: list[dict], limit: int) -> list[dict]:
    """At most `limit` rows, proportional to each family's size (largest remainder, at least one each while room),
    spread evenly inside a family, original file order kept. Deterministic: every checkpoint sees the same steps."""
    if limit <= 0 or limit >= len(rows):
        return list(rows)
    fams: dict = {}
    for i, r in enumerate(rows):
        fams.setdefault(r.get("family") or "?", []).append(i)
    quota = {f: limit * len(ix) / len(rows) for f, ix in fams.items()}
    take = {f: max(1, int(q)) for f, q in quota.items()}
    while sum(take.values()) > limit:                 # more families than rows to give: drop from the biggest shares
        f = max(take, key=lambda k: (take[k] - quota[k], k))
        take[f] -= 1
    while sum(take.values()) < limit:                 # largest remainder
        open_ = [f for f in fams if take[f] < len(fams[f])]
        if not open_:
            break
        f = max(open_, key=lambda k: (quota[k] - take[k], k))
        take[f] += 1
    keep = set()
    for f, ix in fams.items():
        k = min(take[f], len(ix))
        keep.update(ix[j * len(ix) // k] for j in range(k))
    return [r for i, r in enumerate(rows) if i in keep]


# ------------------------------------------------------------------ scoring

def _forbidden_keys(R) -> set:
    sel = getattr(R, "COMPONENTS", {}).get("selection", ())
    return set(FORBIDDEN_PARTS) | {k for k in sel if k not in ("tool_set", "args")}


def score_answer(R, raw: str, label: dict) -> dict:
    """One generation -> the numbers the picker uses. All of reward.py's judgment goes through R.score / R.parse."""
    res = R.score(raw, label)
    forced = bool(label.get("forced"))
    parsed = R.parse(raw, forced)
    parts = res.parts
    step = label.get("step") or ("final" if label.get("final") else "ask" if label.get("ask") else "call")
    got = parts.get("tool_set", 0.0) + parts.get("args", 0.0)
    out = {"malformed": bool(parsed.problems), "reward": res.total, "step": step,
           "selection": got / STEP_MAX.get(step, 1.0),
           "forbidden": any(parts.get(k, 0.0) < 0 for k in _forbidden_keys(R)),
           "needless_call": parts.get("needless_call", 0.0) < 0,
           "thought_words": len(parsed.thought.split()), "text_chars": len(parsed.text),
           "flags": [re.sub(r"\d+", "N", f)[:60] for f in res.flags], "facts": None}
    facts = label.get("facts") or []
    if label.get("final") and facts:
        out["facts"] = 0.0 if parsed.problems else sum(bool(R.fact_present(f, parsed.text)) for f in facts) / len(facts)
    out["narrate"] = bool(label.get("final")) and forced
    return out


def _pct(xs: list) -> float | None:
    return round(100 * sum(xs) / len(xs), 2) if xs else None


def _p50(xs: list) -> float | None:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def summarize(R, rows: list[dict], answers: list[dict]) -> dict:
    """{checkpoint: metrics} from answer records {"ckpt", "id", "sample", "raw"|"error"}. A missing or errored
    generation counts as a malformed one, so a checkpoint cannot look better by crashing; `n_missing` shows it."""
    by_id = {r["id"]: r for r in rows}
    per: dict = {}
    for a in answers:
        if a["id"] in by_id:
            per.setdefault(a["ckpt"], {}).setdefault(a["id"], []).append(a)
    out = {}
    for ck, recs in per.items():
        scored, missing, forb_ids = [], 0, []
        for rid, row in by_id.items():
            got = recs.get(rid)
            if not got:
                missing += 1
                continue
            for a in got:
                s = score_answer(R, a.get("raw", "") if "error" not in a else "", row["label"])
                scored.append((rid, s))
                if s["forbidden"]:
                    forb_ids.append(rid)
        ss = [s for _, s in scored]
        narr = [s for s in ss if s["narrate"]]
        flags = Counter(f for s in ss for f in s["flags"])
        out[ck] = {
            "n": len(ss), "n_missing": missing,
            "forbidden_n": len(forb_ids), "forbidden_ids": sorted(set(forb_ids)),
            "selection_pct": _pct([s["selection"] for s in ss]),
            "facts_pct": _pct([s["facts"] for s in ss if s["facts"] is not None]),
            "malformed_pct": _pct([s["malformed"] for s in ss]),
            "needless_call_pct": _pct([s["needless_call"] for s in ss if s["step"] in ("final", "ask")]),
            "mean_reward": round(sum(s["reward"] for s in ss) / len(ss), 4) if ss else None,
            "think_words_p50": _p50([s["thought_words"] for s in ss]),
            "narrate_think_pct": _pct([s["thought_words"] > 0 for s in narr]),
            "answer_chars_p50": _p50([s["text_chars"] for s in ss if s["step"] == "final"]),
            "top_flags": flags.most_common(6),
        }
    return out


# ------------------------------------------------------------------ the decision

def mmeval_status(results: dict, tol: dict | None = None, strict: bool = False, require_reading: bool = False,
                  mm_dir: str | None = None) -> dict:
    """{checkpoint: {"ok", "reason"}} from mmeval's own picker rules (pick_checkpoint.pick). pick() also refuses a
    checkpoint with no logged text dev loss, which is a ranking rule and not a regression, so a missing loss is
    filled with 0.0 before asking: this picker ranks by generation, not by loss."""
    mmeval_dir(mm_dir)
    import pick_checkpoint as P
    res = copy.deepcopy(results)
    for c in res["checkpoints"].values():
        if c.get("dev_loss_text") is None:
            c["dev_loss_text"] = 0.0
    _, table = P.pick(res, tol or None, strict, require_reading)
    return {t["name"]: {"ok": bool(t["ok"]), "reason": t["reason"]} for t in table}


def select(metrics: dict, mm: dict | None, steps: dict | None = None, within: float = WITHIN_POINTS) -> tuple:
    """(chosen name or None, table). `metrics` is summarize()'s output, `mm` mmeval_status()'s (None = not applied).
    Eligible = no forbidden call, no missing generations, and mmeval ok. Among the eligible, `near` = selection within
    `within` points of the best eligible selection; the winner has the best facts, then selection, fewer malformed,
    and the earlier step."""
    steps = steps or {}
    table = []
    for name, m in metrics.items():
        reasons = []
        if m["forbidden_n"]:
            reasons.append(f"{m['forbidden_n']} forbidden call(s): {', '.join(m['forbidden_ids'][:3])}")
        if m["n_missing"]:
            reasons.append(f"{m['n_missing']} dev steps not generated")
        if m["selection_pct"] is None:
            reasons.append("no scored generations")
        if mm is not None:
            st = mm.get(name)
            if st is None:
                reasons.append("not in the mmeval results (run eval_mm.py on the same --ckpts)")
            elif not st["ok"]:
                reasons.append("mmeval: " + (st["reason"] or "fails"))
        mm_col = "n/a" if mm is None else ("missing" if mm.get(name) is None else "ok" if mm[name]["ok"] else "FAIL")
        table.append({"name": name, "step": steps.get(name), "eligible": not reasons, "reasons": reasons, "mm": mm_col, **m})
    elig = [t for t in table if t["eligible"]]
    if not elig:
        return None, table
    best_sel = max(t["selection_pct"] for t in elig)
    for t in table:
        t["near_best_selection"] = t["eligible"] and t["selection_pct"] >= best_sel - within - 1e-9
    near = [t for t in elig if t["near_best_selection"]]
    key = lambda t: (t["facts_pct"] if t["facts_pct"] is not None else -1.0, t["selection_pct"],  # noqa: E731
                     -(t["malformed_pct"] or 0.0), -(t["step"] if t["step"] is not None else 1 << 30))
    chosen = max(near, key=key)
    return chosen["name"], table


def format_table(table: list, chosen: str | None) -> str:
    cols = ("ckpt", "step", "forb", "sel%", "facts%", "malf%", "think p50", "narr think%", "mmeval", "verdict")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    f = lambda v: "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))  # noqa: E731
    for t in sorted(table, key=lambda t: (t["step"] is None, t["step"] or 0, t["name"])):
        if t["name"] == chosen:
            verdict = "PICK"
        elif t["eligible"]:
            verdict = "near best, lower facts" if t.get("near_best_selection") else "ok, selection too far below"
        else:
            verdict = "; ".join(t["reasons"])
        lines.append("| " + " | ".join([t["name"], f(t["step"]), str(t["forbidden_n"]), f(t["selection_pct"]), f(t["facts_pct"]),
                                        f(t["malformed_pct"]), f(t["think_words_p50"]), f(t["narrate_think_pct"]),
                                        t["mm"], verdict]) + " |")
    return "\n".join(lines)


# ------------------------------------------------------------------ checkpoints

def step_of(name: str) -> int | None:
    m = re.fullmatch(r"step-(\d+)", name)
    return int(m.group(1)) if m else None


def candidate_names(names: list, total_steps: int, min_frac: float = 0.45, max_n: int = 6) -> list:
    """The checkpoints worth generating for: step-* at or past min_frac of the run (dev loss plateaus late; r2 stopped
    improving at 80%), the last one always, at most max_n spread evenly. `final` stands in for the last step-* when the
    final step has no dir of its own, and is dropped when step-<total> exists (same weights)."""
    steps = sorted((step_of(n), n) for n in names if step_of(n) is not None)
    last = [n for s, n in steps if s == total_steps]
    pool = [n for s, n in steps if s >= min_frac * total_steps]
    if "final" in names and not last:
        pool.append("final")
    if len(pool) <= max_n:
        return pool
    idx = sorted({round(i * (len(pool) - 1) / (max_n - 1)) for i in range(max_n)})
    return [pool[i] for i in idx]


def total_steps_of(log_path) -> int:
    for line in Path(log_path).read_text().splitlines():
        if line.startswith("{") and '"event": "start"' in line:
            return int(json.loads(line)["steps"])
    die(f"{log_path}: no start event with the step count")


# ------------------------------------------------------------------ GPU: generation

def row_seed(base_seed: int, rid: str, sample: int) -> int:
    return zlib.crc32(f"{base_seed}:{rid}:{sample}".encode()) & 0x7FFFFFFF


def _encode(tok, proc, prompt: str, device: int):
    """The prompt as eval_mm.py's text_document rows are encoded (processor, no image): proven on the r3 run."""
    import torch
    try:
        enc = proc(text=prompt, add_special_tokens=False, return_tensors="pt")
        enc = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in enc.items()}
    except Exception:  # noqa: BLE001 - the tokenizer path of train.py's `sample`
        ids = tok(prompt, add_special_tokens=False, return_tensors="pt").input_ids.to(device)
        enc = {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
    return enc


def cmd_generate(a) -> int:
    rows = subsample(load_text_dev(a.data), a.limit)
    R = load_reward(a.reward_dir)
    d = mmeval_dir(a.mmeval_dir)
    import torch
    import eval_mm as EM
    from pick_checkpoint import adapter_dirs, read_log, run_root  # noqa: F401
    ckpt_root = Path(a.ckpt_root)
    dirs = adapter_dirs(ckpt_root)
    if a.ckpts:
        names = [n.strip() for n in a.ckpts.split(",") if n.strip()]
        bad = [n for n in names if n not in dirs]
        if bad:
            die(f"no adapter in {ckpt_root}/{{{','.join(bad)}}} (found {sorted(dirs)})")
    else:
        log = run_root(ckpt_root) / "train.log.jsonl"
        names = candidate_names(sorted(dirs), total_steps_of(log))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.with_base:
        names = ["base"] + names
    print(f"{len(rows)} dev steps x {a.samples} sample(s) x {len(names)} checkpoints: {names}")
    EM.check_gpu(a.device)
    tok, proc, model = EM.load_base(Path(a.base), a.device)
    real = [n for n in names if n != "base"]
    peft = None
    if real:
        from peft import PeftModel
        peft = PeftModel.from_pretrained(model, str(dirs[real[0]]), adapter_name=real[0])
        for n in real[1:]:
            peft.load_adapter(str(dirs[n]), adapter_name=n)
        peft.eval()
    m = peft or model
    path = out / "text_answers.jsonl"
    done = set()
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["ckpt"], r["id"], r["sample"]))
    fh = path.open("a")
    (out / "text_run_info.json").write_text(json.dumps({
        "base": str(a.base), "ckpt_root": str(ckpt_root), "checkpoints": names, "steps": len(rows), "samples": a.samples,
        "decoding": "greedy" if a.greedy else SAMPLING, "max_new_tokens": a.max_new, "seed": a.seed,
        "reward_py": R.__file__, "when": time.strftime("%FT%T")}, indent=1))
    gen_kw = {"do_sample": False, "temperature": None, "top_p": None, "top_k": None} if a.greedy \
        else {"do_sample": True, **SAMPLING}

    def run(ck: str) -> None:
        for i, row in enumerate(rows):
            for s in range(a.samples):
                if (ck, row["id"], s) in done:
                    continue
                t0 = time.monotonic()
                rec = {"ckpt": ck, "id": row["id"], "sample": s}
                try:
                    enc = _encode(tok, proc, row["prompt"], a.device)
                    n = enc["input_ids"].shape[1]
                    torch.manual_seed(row_seed(a.seed, row["id"], s))
                    with torch.no_grad():
                        out_ids = m.generate(**enc, max_new_tokens=a.max_new, eos_token_id=list(STOP_IDS),
                                             pad_token_id=tok.pad_token_id, **gen_kw)
                    rec["raw"] = tok.decode(out_ids[0, n:], skip_special_tokens=False)
                    rec["tokens"] = int(out_ids.shape[1] - n)
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    rec["error"] = "OutOfMemoryError"
                except Exception as e:  # noqa: BLE001 - recorded, scored as malformed, never as an answer
                    rec["error"] = f"{type(e).__name__}: {e}"[:300]
                rec["secs"] = round(time.monotonic() - t0, 2)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                if i % 10 == 0 or "error" in rec:
                    shown = (rec.get("raw") or rec["error"]).replace("\n", " ")
                    print(f"{ck:>12} {i + 1:>4}/{len(rows)} {row['id'][:34]:<34} {rec['secs']:5.1f}s | {shown[:90]}", flush=True)

    for ck in names:
        if ck == "base":
            if peft is not None:
                with peft.disable_adapter():
                    run("base")
            else:
                run("base")
        else:
            peft.set_adapter(ck)
            run(ck)
    fh.close()
    return cmd_score(argparse.Namespace(data=a.data, out=str(out), limit=a.limit, reward_dir=a.reward_dir,
                                        ckpt_root=a.ckpt_root, mmeval_dir=a.mmeval_dir))


# ------------------------------------------------------------------ score / pick commands

def read_answers(path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def step_map(names, ckpt_root: str | None, mm_dir) -> dict:
    """{name: step} for the table; final / best resolve through the training log when it can be found."""
    out = {n: step_of(n) for n in names}
    if ckpt_root:
        mmeval_dir(mm_dir)
        from pick_checkpoint import checkpoint_losses, run_root
        log = run_root(Path(ckpt_root)) / "train.log.jsonl"
        if log.is_file():
            for n, v in checkpoint_losses(log, [x for x in names if x != "base"], ckpt_root).items():
                out[n] = v.get("step") if out.get(n) is None else out[n]
    return out


def cmd_score(a) -> int:
    R = load_reward(a.reward_dir)
    rows = subsample(load_text_dev(a.data), getattr(a, "limit", 0) or 0)
    out = Path(a.out)
    answers = read_answers(out / "text_answers.jsonl")
    metrics = summarize(R, rows, answers)
    steps = step_map(list(metrics), getattr(a, "ckpt_root", None), getattr(a, "mmeval_dir", None))
    res = {"steps": len(rows), "metrics": metrics, "ckpt_steps": steps}
    (out / "text_results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    _, table = select(metrics, None, steps)
    (out / "text_report.md").write_text(format_table(table, None) + "\n")
    print(format_table(table, None))
    return 0


def cmd_pick(a) -> int:
    res = json.loads(Path(a.text_results).read_text())
    metrics, steps = res["metrics"], res.get("ckpt_steps") or {}
    metrics = {k: v for k, v in metrics.items() if k != "base"}
    mm = None
    if a.mmeval_results:
        tol = {}
        for it in a.tol or []:
            k, _, v = it.partition("=")
            tol[k] = float(v)
        mm = mmeval_status(json.loads(Path(a.mmeval_results).read_text()), tol, a.strict, a.require_reading, a.mmeval_dir)
    elif not a.no_mmeval:
        die("--mmeval-results is required (the PLAN.md rule needs the mmeval picker); --no-mmeval skips it on purpose")
    else:
        print("WARNING: --no-mmeval: the multimodal regression check was NOT applied", file=sys.stderr)
    chosen, table = select(metrics, mm, steps, a.within)
    text = format_table(table, chosen)
    print(text, file=sys.stderr)
    if chosen is None:
        print("NO CHECKPOINT QUALIFIES: none has zero forbidden calls AND passes the mmeval picker. Do not merge; "
              "see the table (and mmeval's report.md).", file=sys.stderr)
        if a.json_out:
            Path(a.json_out).write_text(json.dumps({"chosen": None, "table": table}, indent=1, default=str))
        return 2
    path = None
    if a.ckpt_root:
        mmeval_dir(a.mmeval_dir)
        from pick_checkpoint import adapter_dirs
        path = str(adapter_dirs(Path(a.ckpt_root)).get(chosen, Path(a.ckpt_root) / chosen))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"chosen": chosen, "path": path, "table": table}, indent=1, default=str))
    print(path or chosen)
    return 0


def cmd_candidates(a) -> int:
    mmeval_dir(a.mmeval_dir)
    from pick_checkpoint import adapter_dirs, run_root
    root = Path(a.ckpt_root)
    names = candidate_names(sorted(adapter_dirs(root)), total_steps_of(run_root(root) / "train.log.jsonl"),
                            a.min_frac, a.max)
    print(",".join(names))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--reward-dir", help="dir with reward.py (default: next to this file, else the repo's grpo/)")
        p.add_argument("--mmeval-dir", help="dir with the mmeval modules (default: next to this file, else the repo's)")

    c = sub.add_parser("candidates", help="print the comma list of checkpoints worth evaluating")
    common(c)
    c.add_argument("--ckpt-root", required=True)
    c.add_argument("--min-frac", type=float, default=0.45)
    c.add_argument("--max", type=int, default=6)
    g = sub.add_parser("generate", help="GPU: generate on the dev steps for each checkpoint, then score")
    common(g)
    g.add_argument("--base", required=True, help="bf16 base dir (the untuned Gemma 4 E4B-it)")
    g.add_argument("--ckpt-root", required=True)
    g.add_argument("--data", required=True, help="text-dev.jsonl from pack_r4.py")
    g.add_argument("--out", required=True)
    g.add_argument("--ckpts", help="comma list (default: candidates)")
    g.add_argument("--limit", type=int, default=360, help="dev steps, proportional per family (0 = all)")
    g.add_argument("--samples", type=int, default=1)
    g.add_argument("--greedy", action="store_true", help="greedy instead of production sampling")
    g.add_argument("--max-new", type=int, default=384, help="max new tokens (grpo.py's max_completion)")
    g.add_argument("--with-base", action="store_true", help="also generate with the adapter off, for reference")
    g.add_argument("--seed", type=int, default=20261102)
    g.add_argument("--device", type=int, default=0)
    s = sub.add_parser("score", help="re-score DIR/text_answers.jsonl")
    common(s)
    s.add_argument("--data", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--limit", type=int, default=360)
    s.add_argument("--ckpt-root")
    k = sub.add_parser("pick", help="the decision: text results + mmeval results")
    common(k)
    k.add_argument("--text-results", required=True)
    k.add_argument("--mmeval-results")
    k.add_argument("--no-mmeval", action="store_true")
    k.add_argument("--ckpt-root", help="adds the chosen adapter's path to the output")
    k.add_argument("--within", type=float, default=WITHIN_POINTS)
    k.add_argument("--strict", action="store_true", help="mmeval --strict (a modality missing from the dev set disqualifies)")
    k.add_argument("--require-reading", action="store_true")
    k.add_argument("--tol", action="append", metavar="CHECK=VALUE")
    k.add_argument("--json-out")
    a = ap.parse_args()
    return {"candidates": cmd_candidates, "generate": cmd_generate, "score": cmd_score, "pick": cmd_pick}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
