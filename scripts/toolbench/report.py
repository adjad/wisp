"""Scorecard, per-category table, failure labels, and run-vs-run comparison.

Input is the list of result rows a run writes to results.jsonl — one row per
(model, case, trial) with the scorer's verdict under "v". Pure functions;
`render()` returns text.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load_rows(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_NOT_OFFERED = re.compile(r"(\S+) was not offered")


def traj_from_row(row: dict, meta: dict | None = None) -> dict:
    """The trajectory scorer.score() consumes, rebuilt from a stored result row.

    Rows keep everything the scorer reads except two things: rounded timings
    (taken from the stored verdict) and, in rows written before `offered` was
    stored, the per-step offered tool names. For those the stored verdict's own
    "X was not offered" notes stand in for them, so a rescore keeps an
    `unoffered_tool` the old scorer saw instead of silently dropping it.
    """
    v = row.get("v") or {}
    steps = [dict(s) for s in row.get("steps") or ()]
    if "unoffered_tool" in (v.get("labels") or ()) and not any(s.get("offered") for s in steps):
        gone = set(_NOT_OFFERED.findall("; ".join(v.get("notes") or ())))
        for s in steps:
            called = [c["name"] for c in s.get("calls") or ()]
            if gone & set(called):
                s["offered"] = [n for n in called if n not in gone] + ["(stored: not offered)"]
    return {"route": row.get("route"), "steps": steps, "final": row.get("final") or "",
            "effects": row.get("effects") or [], "asked": row.get("asked") or [],
            "escapes": row.get("escapes"), "error": row.get("error"), "timeout": row.get("timeout"),
            "access": row.get("access"), "path": row.get("path"),
            "seconds": v.get("seconds") or 0.0, "model_seconds": v.get("model_seconds") or 0.0,
            "guard_retries": v.get("guard_retries") or 0,
            "max_steps": (meta or {}).get("max_steps") or 8}


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def _q(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def _scored(rows: list[dict]) -> list[dict]:
    """Rows that count: an engine_error trial is the engine's, not the model's."""
    return [r for r in rows if not r["v"].get("excluded")]


def summarize(rows: list[dict]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    excluded: Counter = Counter()
    for r in rows:
        if r["v"].get("excluded"):
            excluded[r["model"]] += 1
            by_model.setdefault(r["model"], [])
        else:
            by_model[r["model"]].append(r)
    out = {}
    for model, rs in by_model.items():
        if not rs:
            out[model] = _empty(excluded[model])
            continue
        cases: dict[str, list[dict]] = defaultdict(list)
        for r in rs:
            cases[r["case"]].append(r)
        n = len(rs)
        no_tool = [r for r in rs if r.get("no_tool")]
        secs = [r["v"]["seconds"] for r in rs]
        model_secs = [float(r["v"].get("model_seconds") or 0.0) for r in rs]
        step_counts = [int(r["v"].get("steps") or 0) for r in rs]
        think = [int(r["v"].get("reasoning_chars") or 0) for r in rs]
        out[model] = {
            "trials": n,
            "cases": len(cases),
            "pass": sum(r["v"]["passed"] for r in rs),
            "consistent": sum(all(x["v"]["passed"] for x in xs) for xs in cases.values()),
            "selection": sum(r["v"]["selection_ok"] for r in rs),
            "args": sum(r["v"]["args_ok"] for r in rs),
            "unsafe": sum(not r["v"]["safe"] for r in rs),
            "format_bad": sum(not r["v"]["format_ok"] for r in rs),
            "answer": sum(r["v"]["answer_ok"] for r in rs),
            "no_tool_ok": sum("unneeded_call" not in r["v"]["labels"] for r in no_tool),
            "no_tool_n": len(no_tool),
            "route_faults": sum("route_fault" in r["v"]["labels"] for r in rs),
            "escapes": sum(bool(r.get("escapes")) for r in rs),
            "median_s": round(statistics.median(secs), 2) if secs else 0.0,
            "p90_s": round(_q(secs, 0.9), 2),
            "model_median_s": round(statistics.median(model_secs), 2) if model_secs else 0.0,
            "model_p90_s": round(_q(model_secs, 0.9), 2),
            "steps_p50": int(statistics.median(step_counts)) if step_counts else 0,
            "discovery": sum(int(r["v"].get("discovery_calls") or 0) for r in rs),
            "excluded": excluded[model],
            "timeouts": sum("timeout" in r["v"]["labels"] for r in rs),
            "guard_retries": sum(int(r["v"].get("guard_retries") or 0) for r in rs),
            "by_split": {
                sp: (sum(r["v"]["passed"] for r in xs), len(xs))
                for sp, xs in _group(rs, "split").items()},
            "think_p50": int(statistics.median(think)) if think else 0,
            "think_p90": int(_q(think, 0.9)),
            "think_max": max(think) if think else 0,
            "mean_steps": round(statistics.mean(r["v"]["steps"] for r in rs), 2) if rs else 0.0,
            "mean_prompt_tok": int(statistics.mean(r["v"]["prompt_tokens_est"] for r in rs)) if rs else 0,
            "labels": Counter(l for r in rs for l in r["v"]["labels"]),
            "by_category": {
                cat: (sum(r["v"]["passed"] for r in xs), len(xs))
                for cat, xs in _group(rs, "category").items()},
        }
    return out


def _empty(excluded: int) -> dict:
    """A model whose every trial was an engine error."""
    return {"trials": 0, "cases": 0, "pass": 0, "consistent": 0, "selection": 0, "args": 0, "unsafe": 0,
            "format_bad": 0, "answer": 0, "no_tool_ok": 0, "no_tool_n": 0, "route_faults": 0, "escapes": 0,
            "median_s": 0.0, "p90_s": 0.0, "model_median_s": 0.0, "model_p90_s": 0.0, "steps_p50": 0,
            "discovery": 0, "excluded": excluded, "timeouts": 0, "guard_retries": 0, "by_split": {},
            "think_p50": 0, "think_p90": 0, "think_max": 0, "mean_steps": 0.0, "mean_prompt_tok": 0,
            "labels": Counter(), "by_category": {}}


def _group(rows, key) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[str(r.get(key) or "")].append(r)
    return dict(sorted(g.items()))


def latency(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """case -> model -> median turn seconds, model seconds and steps over its trials."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for (model, case), rs in _group_pairs(_scored(rows)).items():
        out[case][model] = {
            "turn_s": round(statistics.median(r["v"]["seconds"] for r in rs), 2),
            "model_s": round(statistics.median(float(r["v"].get("model_seconds") or 0.0) for r in rs), 2),
            "steps": round(statistics.median(int(r["v"].get("steps") or 0) for r in rs), 1)}
    return dict(sorted(out.items()))


def guard_retries(rows: list[dict]) -> dict[str, dict[str, int]]:
    """case -> model -> HTTP-400 guard retries summed over trials (excluded trials too:
    the retries happened). Only cases with at least one retry."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for (model, case), rs in _group_pairs(rows).items():
        n = sum(int(r["v"].get("guard_retries") or 0) for r in rs)
        if n:
            out[case][model] = n
    return dict(sorted(out.items()))


def _group_pairs(rows) -> dict[tuple[str, str], list[dict]]:
    g: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        g[(r["model"], r["case"])].append(r)
    return g


def _short(model: str) -> str:
    base, _, variant = model.partition(" +")
    base = base.replace("Ling-3.0-tiny-", "Ling-").replace("-merged", "")
    if not variant:
        return base[:26]
    return f"{base[:25 - len(variant)]} +{variant}"


def render(rows: list[dict], *, failures: int = 15, per_case_latency: bool = True) -> str:
    s = summarize(rows)
    models = list(s)
    if not models:
        return "(no results)"
    lines = []
    w = max(18, *(len(_short(m)) for m in models)) + 2

    def row(label, cells):
        lines.append(f"{label:<22}" + "".join(f"{c:>{w}}" for c in cells))

    lines.append("SCORECARD (per trial; consistent = case passed every trial)")
    row("", [_short(m) for m in models])
    row("pass", [f"{_pct(s[m]['pass'], s[m]['trials'])} ({s[m]['pass']}/{s[m]['trials']})" for m in models])
    row("consistent cases", [f"{s[m]['consistent']}/{s[m]['cases']}" for m in models])
    row("tool selection", [_pct(s[m]["selection"], s[m]["trials"]) for m in models])
    row("selection + args", [_pct(s[m]["args"], s[m]["trials"]) for m in models])
    row("answer grounded", [_pct(s[m]["answer"], s[m]["trials"]) for m in models])
    row("no-tool restraint", [f"{s[m]['no_tool_ok']}/{s[m]['no_tool_n']}" for m in models])
    row("UNSAFE calls", [str(s[m]["unsafe"]) for m in models])
    row("format errors", [str(s[m]["format_bad"]) for m in models])
    row("route faults*", [str(s[m]["route_faults"]) for m in models])
    row("turn s p50 / p90", [f"{s[m]['median_s']} / {s[m]['p90_s']}" for m in models])
    row("model s p50 / p90", [f"{s[m]['model_median_s']} / {s[m]['model_p90_s']}" for m in models])
    row("steps p50 / mean", [f"{s[m]['steps_p50']} / {s[m]['mean_steps']}" for m in models])
    row("schema loads", [str(s[m]["discovery"]) for m in models])
    row("timeouts", [str(s[m]["timeouts"]) for m in models])
    row("guard 400 retries", [str(s[m]["guard_retries"]) for m in models])
    row("EXCLUDED (engine)", [str(s[m]["excluded"]) for m in models])
    row("think chars p50/p90", [f"{s[m]['think_p50']} / {s[m]['think_p90']}" for m in models])
    row("think chars max", [str(s[m]["think_max"]) for m in models])
    row("mean prompt tok (est)", [str(s[m]["mean_prompt_tok"]) for m in models])
    if any(s[m]["escapes"] for m in models):
        row("guard refusals", [str(s[m]["escapes"]) for m in models])
    lines.append("* route faults are the host's (a needed tool forbidden, never offered, or refused by the "
                 "loop for every model: no completion contract), identical for every model; they still "
                 "count as fails.")

    for title, key in (("BY CATEGORY (pass / trials)", "by_category"), ("BY SPLIT (pass / trials)", "by_split")):
        groups = sorted({c for m in models for c in s[m][key]})
        if key == "by_split" and len(groups) < 2:
            continue
        lines.append("")
        lines.append(title)
        row("", [_short(m) for m in models])
        for c in groups:
            row(c, [("{}/{}".format(*s[m][key][c]) if c in s[m][key] else "—") for m in models])

    if per_case_latency:
        lines.append("")
        lines.append("LATENCY BY CASE (median over trials: turn s / model s / steps)")
        row("", [_short(m) for m in models])
        for case, per in latency(rows).items():
            row(case[:21], [(f"{per[m]['turn_s']} / {per[m]['model_s']} / {per[m]['steps']:g}"
                             if m in per else "—") for m in models])

    retries = guard_retries(rows)
    if retries:
        lines.append("")
        lines.append("GUARD 400 RETRIES BY CASE (sum over trials)")
        row("", [_short(m) for m in models])
        for case, per in retries.items():
            row(case[:21], [str(per.get(m, 0)) for m in models])

    lines.append("")
    lines.append("FAILURE LABELS (trials carrying each)")
    labels = sorted({l for m in models for l in s[m]["labels"]})
    row("", [_short(m) for m in models])
    for l in labels:
        row(l, [str(s[m]["labels"].get(l, 0)) for m in models])

    fails = [r for r in _scored(rows) if not r["v"]["passed"]]
    engine = [r for r in rows if r["v"].get("excluded")]
    if engine:
        lines.append("")
        lines.append(f"EXCLUDED ENGINE ERRORS ({len(engine)}; not scored)")
        for r in engine[:failures or len(engine)]:
            lines.append(f"  {_short(r['model'])} · {r['case']} t{r['trial']}: "
                         f"{'; '.join(r['v']['notes'])[:160]}")
    if fails and failures:
        lines.append("")
        lines.append(f"FAILURES (first {failures})")
        for r in fails[:failures]:
            calls = [c["name"] for st in r.get("steps", ()) for c in st.get("calls", ())]
            lines.append(f"  {_short(r['model'])} · {r['case']} t{r['trial']}: "
                         f"{','.join(r['v']['labels'])} — {'; '.join(r['v']['notes'])[:160]}")
            lines.append(f"      calls={calls} final={r.get('final', '')[:100]!r}")
    return "\n".join(lines)


def compare(base: list[dict], cur: list[dict]) -> str:
    """Per (model, case) pass-rate changes from `base` to `cur`."""
    def rates(rows):
        g: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for r in _scored(rows):
            g[(r["model"], r["case"])].append(r["v"]["passed"])
        return {k: sum(v) / len(v) for k, v in g.items()}
    b, c = rates(base), rates(cur)
    lines = ["COMPARISON (pass rate, baseline -> current)"]
    worse = sorted(k for k in b.keys() & c.keys() if c[k] < b[k])
    better = sorted(k for k in b.keys() & c.keys() if c[k] > b[k])
    for title, keys in (("REGRESSED", worse), ("IMPROVED", better)):
        lines.append(f"{title}: {len(keys)}")
        for m, case in keys:
            lines.append(f"  {_short(m)} · {case}: {b[(m, case)]:.0%} -> {c[(m, case)]:.0%}")
    for m in sorted({k[0] for k in c}):
        bm = [v for k, v in b.items() if k[0] == m]
        cm = [v for k, v in c.items() if k[0] == m]
        if bm and cm:
            lines.append(f"{_short(m)} overall: {statistics.mean(bm):.0%} -> {statistics.mean(cm):.0%}")
    return "\n".join(lines)
