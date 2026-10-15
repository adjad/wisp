"""Scorecard, per-category table, failure labels, and run-vs-run comparison.

Input is the list of result rows a run writes to results.jsonl — one row per
(model, case, trial) with the scorer's verdict under "v". Pure functions;
`render()` returns text.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load_rows(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def _q(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def summarize(rows: list[dict]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    out = {}
    for model, rs in by_model.items():
        cases: dict[str, list[dict]] = defaultdict(list)
        for r in rs:
            cases[r["case"]].append(r)
        n = len(rs)
        no_tool = [r for r in rs if r.get("no_tool")]
        secs = [r["v"]["seconds"] for r in rs]
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


def _group(rows, key) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[r[key]].append(r)
    return dict(sorted(g.items()))


def _short(model: str) -> str:
    return model.replace("Ling-3.0-tiny-", "Ling-").replace("-merged", "")[:26]


def render(rows: list[dict], *, failures: int = 15) -> str:
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
    row("median / p90 s", [f"{s[m]['median_s']} / {s[m]['p90_s']}" for m in models])
    row("mean steps", [str(s[m]["mean_steps"]) for m in models])
    row("think chars p50/p90", [f"{s[m]['think_p50']} / {s[m]['think_p90']}" for m in models])
    row("think chars max", [str(s[m]["think_max"]) for m in models])
    row("mean prompt tok (est)", [str(s[m]["mean_prompt_tok"]) for m in models])
    if any(s[m]["escapes"] for m in models):
        row("guard refusals", [str(s[m]["escapes"]) for m in models])
    lines.append("* route faults are the router's, identical for every model; they still count as fails.")

    lines.append("")
    lines.append("BY CATEGORY (pass / trials)")
    cats = sorted({c for m in models for c in s[m]["by_category"]})
    row("", [_short(m) for m in models])
    for c in cats:
        row(c, [("{}/{}".format(*s[m]["by_category"][c]) if c in s[m]["by_category"] else "—")
                for m in models])

    lines.append("")
    lines.append("FAILURE LABELS (trials carrying each)")
    labels = sorted({l for m in models for l in s[m]["labels"]})
    row("", [_short(m) for m in models])
    for l in labels:
        row(l, [str(s[m]["labels"].get(l, 0)) for m in models])

    fails = [r for r in rows if not r["v"]["passed"]]
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
        for r in rows:
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
