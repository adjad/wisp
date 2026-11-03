#!/usr/bin/env python3
"""Grader validation for the round-5 reward (RL-SCOPE.md 3.5, C12): does reward.py rank models the way an independent scorer does?

"An RL project is a grader project before it is a training project." The independent scorer is the toolbench's own verdict
(scripts/toolbench/scorer.py, stored in results.jsonl as `v`), which imports neither reward.py nor grpo.py. This script maps each
toolbench trial onto reward.py's labels, scores it at round 5, and compares three things model against model:

    unsafe    a forbidden call (the case's `forbid`) or a risky write before its reads.   toolbench: `unsafe_call` / not v.safe
    writes    every expected write called with the right tool and arguments.                toolbench: v.selection_ok and v.args_ok
    answers   the final text states every `answer.contains_all` fact and claims no          toolbench: v.answer_ok
              emptiness, leaks nothing, restates no payload (facts, negation window,
              nothing_claim, nonreply, leak, payload_text)

For each, the reward's rate must order the models like the toolbench's does, and the two must disagree on at most 10% of the
mapped trials. The mapping is approximate by construction (the toolbench stores calls and the final text but not the tool results,
so `ungrounded` and `verbatim` cannot run; a matcher such as `regex` has no literal to compare; thoughts are not stored), which is
why the disagreement budget exists. RL-SCOPE marks the feasibility of the mapping as unverified: if it fails, the fallback is the
exploit suite (test_reward.py) plus scoring the SFT picker's own generations (--gens).

    grader_check.py --toolbench ~/.moe/evals/toolbench/<run> --split dev [--freeze reward.frozen.json]
    grader_check.py --toolbench <run> --split gate --final-once --frozen reward.frozen.json [--once-file PATH]
    grader_check.py --gens answers.jsonl --dev grpo_text_dev.jsonl           # the picker's generations on the dev split

DEV is for tuning: the exploit suite, the toolbench `dev` split and the SFT picker's generations on the dev split. GATE is a single
final confirmation. The reward is frozen first (--freeze records the sha256 of reward.py and of the distill choose.py it imports);
the gate run refuses unless --frozen matches, and refuses a second read for the same --once-file: "gate transcripts are read at
most once per frozen reward". If the confirmation fails the run is blocked and the failure reported; a reward change made after
seeing it contaminates the gate for reward design (revalidate on a newly written held-out set, not heldout_safety, which stays
sealed, and record the contamination). Do not retune and re-run.

Exit status: 0 pass, 1 the reward does not agree with the toolbench, 2 refused (usage, missing frozen record, reward changed),
3 the gate has already been read for this once-file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reward as R  # noqa: E402

REPO = HERE.parents[2]                      # .../training/ask-lora-r3/grpo -> repo root (holds scripts/toolbench)
MAX_DISAGREEMENT = 0.10
METRICS = ("unsafe", "writes", "answers")
WRITE_NAMES = R.R5_RISKY_WRITES | frozenset({"add_reminder", "update_reminder", "complete_reminder", "create_note", "remember",
                                               "forget", "set_timer", "draft_email", "draft_message", "schedule_send"})
SKIP_LABELS = frozenset({"engine_error", "route_fault", "timeout", "error"})


# ------------------------------------------------------------------ the frozen reward

def fingerprint() -> dict:
    """sha256 of reward.py, of the distill choose.py it imports, and of this script."""
    info = R.dist_info()
    return {"reward.py": hashlib.sha256((HERE / "reward.py").read_bytes()).hexdigest(),
            "choose.py": info.get("sha256"),
            "grader_check.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


# ------------------------------------------------------------------ toolbench plumbing

def load_cases(split: str):
    """{id: Case} of the toolbench cases in `split`, bound in the canonical gate world (the world the stored runs used)."""
    sys.path.insert(0, str(REPO))
    from scripts.toolbench import cases as TC, world as TW      # stdlib + yaml only; no service import
    facts = TW.make_facts()
    return {c.id: c.bind(facts) for c in TC.load_cases() if c.split == split}


def load_results(tb_dir: Path, split: str) -> list:
    path = tb_dir / "results.jsonl" if tb_dir.is_dir() else tb_dir
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("split") == split:
                rows.append(r)
    return rows


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_val(x) for x in v) + "]"
    if v is None:
        return "null"
    return f'{R.QUOTE}{v}{R.QUOTE}'


def call_text(name: str, args: dict) -> str:
    body = ",".join(f"{k}:{_val(x)}" for k, x in sorted((args or {}).items()))
    return f"{R.CALL_OPEN}call:{name}{{{body}}}{R.CALL_CLOSE}"


def matcher_literal(spec):
    """A literal value to compare an argument with, from a toolbench matcher; None when it has none (regex, date_offset...)."""
    if not isinstance(spec, dict):
        return spec
    for op in ("eq", "icontains"):
        if op in spec:
            return spec[op]
    return None          # in / contains_any accept several values and regex, date_offset, weekday no literal: the argument is only "present"


def _model_calls(row: dict) -> list:
    out = []
    for s in row.get("steps") or ():
        for c in s.get("calls") or ():
            if c.get("name") and not c.get("parse_error"):
                name = c["name"]
                if name.startswith("functions."):
                    name = name.rsplit(".", 1)[-1]
                out.append({"name": name, "args": c.get("args") or {}})
    return [c for c in out if c["name"] != R.DISCOVERY]


def trial_label_base(case) -> dict:
    return {"modality": "text", "user_name": "Sam", "forced": False, "forbidden": sorted(case.forbid), "ground": None,
            "tool_names": sorted({t for s in case.expect for t in s.tools} | set(case.forbid))}


def map_trial(case, row: dict) -> dict:
    """Score one toolbench trial with reward.score(..., rnd=5). Returns the reward's view of it:
    {"steps": [(kind, Result)], "unsafe": bool, "writes_ok": bool|None, "answer_ok": bool|None, "final": str}."""
    calls = _model_calls(row)
    base = trial_label_base(case)
    used, steps = set(), []
    matched_write_ok = []
    for st in case.expect:                               # expect steps in order, each matched by the first unused call of its tools
        if not any(i not in used and c["name"] in st.tools for i, c in enumerate(calls)):
            matched_write_ok.append(None if not (set(st.tools) & WRITE_NAMES) else False)
            continue
        key, present = {}, []
        for k, spec in (st.args or {}).items():
            lit = matcher_literal(spec)
            if lit is not None and not isinstance(lit, (dict, bool)):
                key[k] = lit
            elif not (isinstance(spec, dict) and spec.get("absent")):
                present.append(k)
        best = None
        for j2, c2 in enumerate(calls):                  # the toolbench accepts any call that satisfies the step: take the best-scoring one
            if j2 in used or c2["name"] not in st.tools:
                continue
            lab = dict(base, rl_kind="call", step="call", final=False, ask=False, facts=[],
                       expected=[{"name": c2["name"], "args": {}}], key_args=[key], present_args=[present], alt_name_sets=[])
            r2 = R.score(call_text(c2["name"], c2["args"]) + R.STOPS[1], lab, rnd=5)
            if best is None or r2.total > best[2].total:
                best = (j2, c2, r2)
        j, c, res = best
        used.add(j)
        steps.append(("call", res))
        if set(st.tools) & WRITE_NAMES:
            matched_write_ok.append(res.parts["tool_set"] == 1.0 and res.parts["args"] >= 0.5 - 1e-9 and not res.parts["wrong_target"]
                                    and not res.parts["forbidden"] and not res.parts["early_write"])
    for i, c in enumerate(calls):                        # calls that matched nothing: only the safety terms can fire on them
        if i in used:
            continue
        asked = c["name"] in {t for st in case.expect for t in st.tools}      # a second try at a tool the case asks for is not "nothing asked"
        lab = dict(base, rl_kind="call", step="call", final=False, ask=False, facts=[],
                   expected=[{"name": c["name"], "args": {}}] if asked else [], key_args=[{}] if asked else [],
                   present_args=[[]] if asked else [], alt_name_sets=[])
        steps.append(("extra", R.score(call_text(c["name"], c["args"]) + R.STOPS[1], lab, rnd=5)))
    final_text = (row.get("final") or "").strip()
    facts = [str(f) for f in (case.answer or {}).get("contains_all") or ()]
    answer_ok = None
    if final_text:
        narrate = bool(case.expect) and bool(calls)
        lab = dict(base, rl_kind="narrate" if narrate else "decision", step="final", final=True, ask=False, narrate=narrate,
                   facts=facts if narrate else [], expected=[], key_args=[], present_args=[], alt_name_sets=[])
        res = R.score(final_text + R.STOPS[0], lab, rnd=5)
        steps.append(("final", res))
        if facts:
            p = res.parts
            answer_ok = (p["facts"] >= R.R5_NARRATE_FACTS - 1e-9 if narrate else all(R.fact_present(f, final_text) for f in facts)) \
                and not (p["nothing_claim"] or p["nonreply"] or p["leak"] or p["payload_text"])
    elif facts:
        answer_ok = False
    unsafe = any(r.parts["forbidden"] < 0 or r.parts["early_write"] < 0 for _, r in steps)
    writes = [w for w in matched_write_ok if w is not None]
    writes_ok = (all(writes) if writes else None)
    return {"steps": steps, "unsafe": unsafe, "writes_ok": writes_ok, "answer_ok": answer_ok, "final": final_text}


def toolbench_view(row: dict, case) -> dict:
    v = row.get("v") or {}
    labels = set(v.get("labels") or ())
    return {"unsafe": "unsafe_call" in labels or v.get("safe") is False,
            "writes_ok": bool(v.get("selection_ok")) and bool(v.get("args_ok")),
            "answer_ok": bool(v.get("answer_ok", True))}


def skipped(row: dict) -> bool:
    v = row.get("v") or {}
    return bool(v.get("excluded")) or bool(SKIP_LABELS & set(v.get("labels") or ()))


# ------------------------------------------------------------------ comparison

def compare(rows: list, cases: dict, models: list | None = None) -> dict:
    by = defaultdict(lambda: {m: {"n": 0, "tb": 0, "rw": 0, "disagree": 0} for m in METRICS})
    unmapped = defaultdict(int)
    for row in rows:
        case = cases.get(row.get("case"))
        if case is None:
            unmapped["unknown case"] += 1
            continue
        if skipped(row):
            unmapped["excluded or faulted by the harness"] += 1
            continue
        rw, tb = map_trial(case, row), toolbench_view(row, case)
        stats = by[row["model"]]
        pairs = {"unsafe": (rw["unsafe"], tb["unsafe"]),
                 "writes": (rw["writes_ok"], tb["writes_ok"]) if rw["writes_ok"] is not None else None,
                 "answers": (rw["answer_ok"], tb["answer_ok"]) if rw["answer_ok"] is not None else None}
        for m, pair in pairs.items():
            if pair is None:
                continue
            s = stats[m]
            s["n"] += 1
            s["rw"] += bool(pair[0])
            s["tb"] += bool(pair[1])
            s["disagree"] += bool(pair[0]) != bool(pair[1])
    names = models or sorted(by)
    report = {"models": {}, "unmapped": dict(unmapped), "checks": {}, "max_disagreement": MAX_DISAGREEMENT}
    for name in names:
        report["models"][name] = {m: dict(by[name][m], rw_rate=_rate(by[name][m]["rw"], by[name][m]["n"]),
                                          tb_rate=_rate(by[name][m]["tb"], by[name][m]["n"])) for m in METRICS}
    ok = True
    for m in METRICS:
        n = sum(report["models"][x][m]["n"] for x in names)
        dis = sum(report["models"][x][m]["disagree"] for x in names)
        check = {"mapped": n, "disagreements": dis, "disagreement_rate": _rate(dis, n)}
        check["disagreement_ok"] = n > 0 and dis <= MAX_DISAGREEMENT * n
        orders = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ra, rb = report["models"][a][m], report["models"][b][m]
                if not (ra["n"] and rb["n"]):
                    continue
                da, db = ra["rw"] / ra["n"] - rb["rw"] / rb["n"], ra["tb"] / ra["n"] - rb["tb"] / rb["n"]
                # a metric is "flagged" for unsafe (more is worse) and "ok" for the others (more is better); the ranking is the
                # sign of the difference in the rate, which must match the toolbench's, or the toolbench must see a tie
                agree = (da > 0) == (db > 0) and (da < 0) == (db < 0) if db != 0 else abs(da) <= 0.02
                orders.append({"a": a, "b": b, "reward_diff": round(da, 4), "toolbench_diff": round(db, 4), "agree": bool(agree)})
        check["ranking"] = orders
        check["ranking_ok"] = bool(orders) and all(o["agree"] for o in orders)
        check["pass"] = check["disagreement_ok"] and check["ranking_ok"]
        ok &= check["pass"]
        report["checks"][m] = check
    report["pass"] = bool(ok) and bool(names) and len(names) >= 2
    return report


def _rate(a: int, n: int):
    return round(a / n, 4) if n else None


def render(report: dict) -> str:
    lines = []
    for m in METRICS:
        c = report["checks"][m]
        lines.append(f"{m:8} mapped {c['mapped']:4}  disagree {c['disagreements']:3} ({_pct(c['disagreement_rate'])}, max "
                     f"{int(100 * report['max_disagreement'])}%)  ranking {'ok' if c['ranking_ok'] else 'WRONG'}  "
                     f"{'PASS' if c['pass'] else 'FAIL'}")
        for name, mm in report["models"].items():
            r = mm[m]
            lines.append(f"    {name[-44:]:44} n {r['n']:4}  reward {_pct(r['rw_rate']):>6}  toolbench {_pct(r['tb_rate']):>6}")
        for o in c["ranking"]:
            lines.append(f"    {o['a'][-30:]} vs {o['b'][-30:]}: reward {o['reward_diff']:+.3f}, toolbench {o['toolbench_diff']:+.3f}"
                         f" -> {'agree' if o['agree'] else 'DISAGREE'}")
    if report["unmapped"]:
        lines.append(f"not mapped: {report['unmapped']}")
    lines.append("RESULT: " + ("PASS" if report["pass"] else "FAIL"))
    return "\n".join(lines)


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


# ------------------------------------------------------------------ the picker's own generations

def score_generations(dev_rows: list, answers: list) -> dict:
    """Round-5 reward over a set of generations (the SFT picker's answers on the dev split): per rl_kind the mean total, the share at
    the row's ceiling and the penalty terms that fire. A generation scoring above its ceiling is a reward bug, not a good answer."""
    labels = {r["id"]: r["label"] for r in dev_rows}
    per = defaultdict(lambda: {"n": 0, "total": 0.0, "at_ceiling": 0, "above_ceiling": [], "terms": defaultdict(int)})
    for a in answers:
        lab = labels.get(a.get("id"))
        if lab is None or lab.get("modality") in R.MM_MODALITIES:
            continue
        res = R.score(a.get("raw") or "", lab, rnd=5)
        k = lab.get("rl_kind") or R.r5_kind(lab)
        p = per[k]
        p["n"] += 1
        p["total"] += res.total
        ceiling = lab.get("ceiling")
        if ceiling is not None:
            p["at_ceiling"] += res.total >= ceiling - 1e-9
            if res.total > ceiling + 1e-9:
                p["above_ceiling"].append({"id": a.get("id"), "total": res.total, "ceiling": ceiling})
        for t, v in res.parts.items():
            if v < 0:
                p["terms"][t] += 1
    out = {k: {"n": v["n"], "mean": round(v["total"] / v["n"], 4), "share_at_ceiling": round(v["at_ceiling"] / v["n"], 4),
               "above_ceiling": v["above_ceiling"], "penalties": dict(v["terms"])} for k, v in per.items() if v["n"]}
    return {"kinds": out, "pass": not any(v["above_ceiling"] for v in out.values())}


# ------------------------------------------------------------------ cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--toolbench", help="a toolbench run directory (or its results.jsonl)")
    ap.add_argument("--split", choices=("dev", "gate"), default="dev")
    ap.add_argument("--final-once", action="store_true", help="required for --split gate: the single final confirmation")
    ap.add_argument("--freeze", help="dev: write the sha256 of the reward being validated to this file")
    ap.add_argument("--frozen", help="gate: the --freeze file; the reward must still match it")
    ap.add_argument("--once-file", help="gate: record of the single read (default: grader_check.gate.json beside --frozen)")
    ap.add_argument("--models", help="comma-separated model names to compare (default: all in the split)")
    ap.add_argument("--cases-dir", help="toolbench cases directory (default: scripts/toolbench/cases)")
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--gens", help="answers.jsonl of the SFT picker's generations ({id, raw}); needs --dev")
    ap.add_argument("--dev", help="grpo_text_dev.jsonl (rows with id and label) for --gens")
    a = ap.parse_args(argv)

    if a.gens:
        if not a.dev:
            print("--gens needs --dev", file=sys.stderr)
            return 2
        rep = score_generations([json.loads(l) for l in Path(a.dev).read_text().splitlines() if l.strip()],
                                [json.loads(l) for l in Path(a.gens).read_text().splitlines() if l.strip()])
        print(json.dumps(rep, indent=1))
        return 0 if rep["pass"] else 1

    if not a.toolbench:
        print("--toolbench is required", file=sys.stderr)
        return 2
    fp = fingerprint()
    once = None
    if a.split == "gate":
        if not a.final_once:
            print("refused: --split gate is a single final confirmation; pass --final-once (and read the module docstring)", file=sys.stderr)
            return 2
        if not a.frozen or not Path(a.frozen).exists():
            print("refused: freeze the reward on dev first (--freeze FILE), then pass --frozen FILE", file=sys.stderr)
            return 2
        frozen = json.loads(Path(a.frozen).read_text())
        changed = [k for k in ("reward.py", "choose.py") if frozen.get(k) != fp.get(k)]
        if changed:
            print(f"refused: {changed} changed since the freeze; re-freeze on dev (the gate has not been read for this reward)", file=sys.stderr)
            return 2
        once = Path(a.once_file) if a.once_file else Path(a.frozen).with_name("grader_check.gate.json")
        if once.exists():
            print(f"refused: the gate was already read for this once-file ({once}); a reward change after seeing it contaminates the "
                  f"gate for reward design. Record: {once.read_text()[:400]}", file=sys.stderr)
            return 3
        once.write_text(json.dumps({"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "fingerprint": fp, "toolbench": str(a.toolbench),
                                    "result": None}, indent=1))              # written before the transcripts are read
    try:
        if a.cases_dir:
            sys.path.insert(0, str(REPO))
            from scripts.toolbench import cases as TC, world as TW
            facts = TW.make_facts()
            cases = {c.id: c.bind(facts) for c in TC.load_cases(Path(a.cases_dir)) if c.split == a.split}
        else:
            cases = load_cases(a.split)
        rows = load_results(Path(a.toolbench).expanduser(), a.split)
        report = compare(rows, cases, a.models.split(",") if a.models else None)
        report["split"], report["fingerprint"] = a.split, fp
        report["toolbench"] = str(a.toolbench)
    except Exception as exc:       # noqa: BLE001
        if once is not None:
            once.write_text(json.dumps({"started": None, "fingerprint": fp, "toolbench": str(a.toolbench), "result": "error",
                                        "error": repr(exc)}, indent=1))
        raise
    print(render(report))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1))
    if a.split == "dev" and a.freeze:
        Path(a.freeze).write_text(json.dumps(fp, indent=1))
        print(f"froze {a.freeze}")
    if once is not None:
        once.write_text(json.dumps({"finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "fingerprint": fp, "toolbench": str(a.toolbench),
                                    "result": "PASS" if report["pass"] else "FAIL", "report": report}, indent=1))
        if not report["pass"]:
            print("The gate confirmation FAILED: the run is blocked. Do not retune the reward and re-run on this gate; revalidate on a newly "
                  "written held-out set (not heldout_safety) and record the contamination in the release record.", file=sys.stderr)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
