#!/usr/bin/env python3
"""Same-clock public toolbench comparison: base vs fine-tuned checkpoint from ONE toolbench run.

Usage:
  analyze.py --run RUN_DIR --base BASE_MODEL --cand CAND_MODEL --out FILE.json
             [--toolbench-src DIR] [--no-clock-check]

RUN_DIR holds results.jsonl, meta.json and clock.jsonl. Markdown goes to stdout, the same
numbers as JSON to --out. Exit status: 0 ok, 2 when the clock check failed (or was skipped
with the test-only flag), 1 on bad input.

Metric definitions are NOT re-implemented: scorecard, quantiles and the unchecked-absence
test are imported from scripts/toolbench/report.py of a toolbench checkout
(--toolbench-src, default: the r5-pilot checkout).

--no-clock-check is TEST ONLY: it lets the script run on an older two-model run that never
wrote clock.jsonl. Its output is marked INVALID FOR SAME-CLOCK CLAIMS and the exit code is 2.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_SRC = "/Users/adijain/Desktop/r5-pilot/wisp-main-7699a96b"


def load_report(src: Path):
    path = src / "scripts" / "toolbench" / "report.py"
    if not path.is_file():
        sys.exit(f"cannot find toolbench report.py at {path} (use --toolbench-src)")
    spec = importlib.util.spec_from_file_location("toolbench_report", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["toolbench_report"] = mod
    spec.loader.exec_module(mod)
    return mod


def sign_test_p(b: int, c: int) -> float:
    """Exact two-sided binomial (p=0.5) sign test on b and c discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


def med(xs):
    return round(statistics.median(xs), 3) if xs else None


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------ clock check
def clock_check(run: Path, meta: dict, rows: list[dict], base: str, cand: str, skip: bool) -> dict:
    res: dict = {"skipped_test_only": skip, "ok": False, "problems": [],
                 "meta": {k: meta.get(k) for k in
                          ("run", "git", "now", "today", "seeds", "seed_base", "case_sessions",
                           "engine_parity", "engine_settings", "models", "bindings")}}
    if skip:
        res["problems"].append("clock check SKIPPED by --no-clock-check (test-only)")
        return res
    path = run / "clock.jsonl"
    if not path.is_file():
        res["problems"].append(f"clock.jsonl is MISSING in {run}")
        return res
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    res["clock_lines"] = len(lines)
    per_model: dict[str, set] = defaultdict(set)
    today_set: dict[str, set] = defaultdict(set)
    world: dict[str, set] = defaultdict(set)
    cases_seen: dict[str, set] = defaultdict(set)
    for l in lines:
        per_model[l["model"]].add((l.get("now"), l.get("strftime")))
        today_set[l["model"]].add(l.get("today"))
        world[l["model"]].add(l.get("world_now"))
        cases_seen[l["model"]].add(l.get("case"))
    res["per_model"] = {m: {"clocks": sorted(map(list, per_model[m])), "today": sorted(map(str, today_set[m])),
                            "world_now": sorted(map(str, world[m])), "case_runs": sum(1 for l in lines if l["model"] == m)}
                        for m in per_model}
    for m in (base, cand):
        if m not in per_model:
            res["problems"].append(f"no clock lines for model {m}")
        elif len(per_model[m]) != 1:
            res["problems"].append(f"{m} saw {len(per_model[m])} distinct (now, strftime) values")
        elif len(today_set[m]) != 1 or len(world[m]) != 1:
            res["problems"].append(f"{m} saw several today/world_now values")
    if base in per_model and cand in per_model:
        if per_model[base] != per_model[cand]:
            res["problems"].append(f"ARMS DIFFER: base {sorted(map(list, per_model[base]))} vs cand {sorted(map(list, per_model[cand]))}")
        if today_set[base] != today_set[cand] or world[base] != world[cand]:
            res["problems"].append("ARMS DIFFER in today / world_now")
    # meta must agree with the observed clock
    allnow = {n for m in (base, cand) for n, _ in per_model.get(m, ())}
    alltoday = {t for m in (base, cand) for t in today_set.get(m, ())}
    if meta.get("now") is not None and allnow and allnow != {meta["now"]}:
        res["problems"].append(f"meta.now {meta.get('now')!r} != clock now {sorted(map(str, allnow))}")
    if meta.get("today") is not None and alltoday and alltoday != {meta["today"]}:
        res["problems"].append(f"meta.today {meta.get('today')!r} != clock today {sorted(map(str, alltoday))}")
    # every (model, case) that produced rows has a clock line
    run_cases = defaultdict(set)
    for r in rows:
        run_cases[r["model"]].add(r["case"])
    for m in (base, cand):
        gone = run_cases.get(m, set()) - cases_seen.get(m, set())
        if gone:
            res["problems"].append(f"{m}: {len(gone)} cases have rows but no clock line, e.g. {sorted(gone)[:3]}")
    if run_cases.get(base) != run_cases.get(cand):
        res["problems"].append("arms ran different case sets")
    res["ok"] = not res["problems"]
    return res


# ------------------------------------------------------------------ unsafe detail
_FORBID = re.compile(r"forbidden: (\[.*?\])")
_UNCONF = re.compile(r"(\S+) ran without a confirmation card")


def forbidden_tools(row: dict) -> list[str]:
    out: list[str] = []
    for n in (row["v"].get("notes") or ()):
        m = _FORBID.search(n)
        if m:
            try:
                out += list(ast.literal_eval(m.group(1)))
            except Exception:
                out.append(m.group(1))
    return sorted(set(out))


def unconfirmed_tools(row: dict) -> list[str]:
    return sorted({m.group(1) for n in (row["v"].get("notes") or ()) for m in [_UNCONF.search(n)] if m})


def arm_view(row: dict | None) -> dict | None:
    if row is None:
        return None
    effects = row.get("effects") or []
    forb = forbidden_tools(row)
    return {
        "unsafe": not row["v"]["safe"],
        "forbidden_tools_flagged": forb,
        "unconfirmed_effect_tools": unconfirmed_tools(row),
        "card_asked": bool(row.get("asked")),
        "asked": list(row.get("asked") or []),
        "forbidden_tools_called": sorted({c["name"] for s in row.get("steps") or () for c in s.get("calls") or ()
                                          if c["name"] in forb}),
        "effects": [{"tool": e.get("tool"), "args": e.get("args")} for e in effects],
        "forbidden_effects": [{"tool": e.get("tool"), "args": e.get("args")} for e in effects if e.get("tool") in forb],
        "labels": list(row["v"].get("labels") or ()),
        "passed": bool(row["v"]["passed"]),
    }


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--base", required=True)
    ap.add_argument("--cand", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--toolbench-src", type=Path, default=Path(DEFAULT_SRC))
    ap.add_argument("--no-clock-check", action="store_true",
                    help="TEST ONLY: skip the clock check (run has no clock.jsonl). Output is marked invalid.")
    a = ap.parse_args()

    rp = load_report(a.toolbench_src)
    try:
        rows_all = rp.load_rows(a.run)
        meta = json.loads((a.run / "meta.json").read_text())
    except Exception as e:  # noqa: BLE001
        print(f"cannot read run {a.run}: {e}", file=sys.stderr)
        return 1
    rows = [r for r in rows_all if r["model"] in (a.base, a.cand)]
    present = {r["model"] for r in rows}
    if present != {a.base, a.cand}:
        print(f"models in run: {sorted({r['model'] for r in rows_all})}; need {a.base!r} and {a.cand!r}", file=sys.stderr)
        return 1

    md: list[str] = []
    P = md.append
    result: dict = {"run": str(a.run), "base": a.base, "cand": a.cand}

    # 1) clock
    clk = clock_check(a.run, meta, rows, a.base, a.cand, a.no_clock_check)
    result["clock"] = clk
    P(f"# Same-clock toolbench comparison: {a.base} vs {a.cand}")
    P("")
    P("## 1. Clock check")
    if clk["ok"]:
        pm = clk["per_model"]
        P(f"**PASS** - both arms saw exactly one identical clock: `{pm[a.base]['clocks'][0]}` "
          f"(now, strftime), today `{pm[a.base]['today'][0]}`, world_now `{pm[a.base]['world_now'][0]}`; "
          f"{clk['clock_lines']} case runs logged.")
    else:
        P("**!!! CLOCK CHECK FAILED - THIS COMPARISON IS NOT A SAME-CLOCK RESULT !!!**")
        if a.no_clock_check:
            P("**TEST-ONLY RUN (--no-clock-check): output below is INVALID FOR SAME-CLOCK CLAIMS.**")
        for p in clk["problems"]:
            P(f"- FAIL: {p}")
    if "per_model" in clk:
        P("")
        P(md_table(["model", "(now, strftime) values", "today", "world_now", "case runs"],
                   [[m, "; ".join(map(str, v["clocks"])), ", ".join(v["today"]), ", ".join(v["world_now"]), v["case_runs"]]
                    for m, v in clk["per_model"].items()]))
    mt = clk["meta"]
    P("")
    P(md_table(["meta field", "value"], [
        ["run", mt["run"]], ["git", mt["git"]], ["now", mt["now"]], ["today", mt["today"]],
        ["seeds", json.dumps(mt["seeds"])], ["seed_base", mt["seed_base"]],
        ["case_sessions", json.dumps(mt["case_sessions"])[:300]],
        ["engine_parity", json.dumps(mt["engine_parity"], sort_keys=True)[:600]],
    ]))

    # 2) scorecard
    summ = rp.summarize(rows)
    result["scorecard"] = {}
    sc_rows = []
    labels = [("pass", lambda s: f"{pct(s['pass'], s['trials'])} ({s['pass']}/{s['trials']})"),
              ("consistent cases", lambda s: f"{s['consistent']}/{s['cases']}"),
              ("tool selection", lambda s: f"{pct(s['selection'], s['trials'])} ({s['selection']})"),
              ("selection + args", lambda s: f"{pct(s['args'], s['trials'])} ({s['args']})"),
              ("answer grounded", lambda s: f"{pct(s['answer'], s['trials'])} ({s['answer']})"),
              ("no-tool restraint", lambda s: f"{s['no_tool_ok']}/{s['no_tool_n']}"),
              ("UNSAFE trials", lambda s: str(s["unsafe"])),
              ("UNCHECKED absence", lambda s: f"{s['unchecked_absence']} of {s['absence_claims']}"),
              ("turn s p50 / p90", lambda s: f"{s['median_s']} / {s['p90_s']}"),
              ("model s p50 / p90", lambda s: f"{s['model_median_s']} / {s['model_p90_s']}"),
              ("excluded (engine)", lambda s: str(s["excluded"]))]
    for lab, fn in labels:
        sc_rows.append([lab, fn(summ[a.base]), fn(summ[a.cand])])
    for m in (a.base, a.cand):
        s = summ[m]
        result["scorecard"][m] = {k: s[k] for k in (
            "trials", "cases", "pass", "consistent", "selection", "args", "answer", "no_tool_ok", "no_tool_n",
            "unsafe", "absence_claims", "unchecked_absence", "median_s", "p90_s", "model_median_s", "model_p90_s",
            "excluded", "format_bad", "route_faults", "timeouts")}
        result["scorecard"][m]["labels"] = dict(s["labels"])
    P("")
    P("## 2. Scorecard (per trial, engine-excluded trials not counted)")
    P(md_table(["metric", "base", "candidate"], sc_rows))

    # 3) paired
    scored = {(r["model"], r["case"], r["trial"]): r for r in rows}
    keys = sorted({(r["case"], r["trial"]) for r in rows})
    pairs = []
    dropped = 0
    for case, trial in keys:
        b, c = scored.get((a.base, case, trial)), scored.get((a.cand, case, trial))
        if b is None or c is None or b["v"].get("excluded") or c["v"].get("excluded"):
            dropped += 1
            continue
        pairs.append((case, trial, b, c))
    tally = Counter()
    by_cat: dict[str, Counter] = defaultdict(Counter)
    for case, trial, b, c in pairs:
        bp, cp = b["v"]["passed"], c["v"]["passed"]
        k = "both" if bp and cp else "cand_only" if cp else "base_only" if bp else "neither"
        tally[k] += 1
        by_cat[str(b.get("category") or "")][k] += 1
    p = sign_test_p(tally["base_only"], tally["cand_only"])
    result["paired"] = {"pairs": len(pairs), "dropped_unpaired_or_excluded": dropped, **{k: tally[k] for k in
                        ("cand_only", "base_only", "both", "neither")}, "sign_test_p_two_sided": p,
                        "by_category": {c: {k: v[k] for k in ("cand_only", "base_only", "both", "neither")}
                                        | {"p": sign_test_p(v["base_only"], v["cand_only"])}
                                        for c, v in sorted(by_cat.items())}}
    P("")
    P("## 3. Paired comparison (same case and trial)")
    P(f"{len(pairs)} pairs ({dropped} dropped: unpaired or an engine-excluded side).")
    P("")
    P(md_table(["candidate only", "base only", "both", "neither", "sign test p (exact, two-sided)"],
               [[tally["cand_only"], tally["base_only"], tally["both"], tally["neither"], f"{p:.3g}"]]))
    P("")
    P(md_table(["category", "pairs", "cand only", "base only", "both", "neither", "p"],
               [[c, sum(v.values()), v["cand_only"], v["base_only"], v["both"], v["neither"],
                 f"{sign_test_p(v['base_only'], v['cand_only']):.3g}"] for c, v in sorted(by_cat.items())]))

    # 4) unsafe
    unsafe_rows = []
    cells = defaultdict(dict)
    for case, trial in keys:
        for m in (a.base, a.cand):
            r = scored.get((m, case, trial))
            if r is not None and not r["v"].get("excluded"):
                cells[(case, trial)][m] = r
    for (case, trial), d in sorted(cells.items()):
        if any(not r["v"]["safe"] for r in d.values()):
            unsafe_rows.append({"case": case, "trial": trial, "category": next(iter(d.values())).get("category"),
                                "base": arm_view(d.get(a.base)), "cand": arm_view(d.get(a.cand))})
    counts = {}
    for key, m in (("base", a.base), ("cand", a.cand)):
        scored_m = [r for r in rows if r["model"] == m and not r["v"].get("excluded")]
        counts[key] = {
            "unsafe_trials": sum(not r["v"]["safe"] for r in scored_m),
            "trials_with_forbidden_simulated_effect": sum(bool(arm_view(r)["forbidden_effects"]) for r in scored_m),
            "unsafe_trials_with_card_asked": sum((not r["v"]["safe"]) and bool(r.get("asked")) for r in scored_m),
        }
    result["unsafe"] = {"counts": counts, "cases": unsafe_rows}
    P("")
    P("## 4. Unsafe")
    P(md_table(["", "unsafe trials", "trials with a forbidden simulated effect", "unsafe trials where a card was asked"],
               [[k, v["unsafe_trials"], v["trials_with_forbidden_simulated_effect"], v["unsafe_trials_with_card_asked"]]
                for k, v in counts.items()]))
    P("")
    P(f"{len(unsafe_rows)} (case, trial) pairs where either model was unsafe. "
      "Forbidden = the tool list in the scorer's `forbidden:` note; `unconfirmed` = an effect that ran without its card.")

    def fmt(v):
        if v is None:
            return "-"
        if not v["unsafe"]:
            return "safe"
        eff = "; ".join(f"{e['tool']}({json.dumps(e['args'], sort_keys=True)[:80]})" for e in v["effects"]) or "no effect"
        bits = []
        if v["forbidden_tools_flagged"]:
            bits.append("forbidden " + ",".join(v["forbidden_tools_flagged"]))
        if v["unconfirmed_effect_tools"]:
            bits.append("unconfirmed " + ",".join(v["unconfirmed_effect_tools"]))
        bits.append("card asked: " + (",".join(v["asked"]) if v["card_asked"] else "no"))
        bits.append("effects: " + eff)
        return " / ".join(bits)
    P("")
    P(md_table(["case", "trial", "base", "candidate"], [[u["case"], u["trial"], fmt(u["base"]), fmt(u["cand"])] for u in unsafe_rows]))

    # 5) latency
    def lat(m):
        rs = [r for r in rows if r["model"] == m and not r["v"].get("excluded")]
        t = [r["v"]["seconds"] for r in rs]
        ms = [float(r["v"].get("model_seconds") or 0.0) for r in rs]
        return {"turn_p50": round(statistics.median(t), 2), "turn_p90": round(rp._q(t, 0.9), 2),
                "model_p50": round(statistics.median(ms), 2), "model_p90": round(rp._q(ms, 0.9), 2)}
    lb, lc = lat(a.base), lat(a.cand)
    dturn = [c["v"]["seconds"] - b["v"]["seconds"] for _, _, b, c in pairs]
    dmod = [float(c["v"].get("model_seconds") or 0) - float(b["v"].get("model_seconds") or 0) for _, _, b, c in pairs]
    result["latency"] = {"base": lb, "cand": lc, "paired_median_diff_turn_s": med(dturn),
                         "paired_median_diff_model_s": med(dmod), "pairs": len(pairs)}
    P("")
    P("## 5. Latency (seconds)")
    P(md_table(["", "turn p50", "turn p90", "model p50", "model p90"],
               [["base", *lb.values()], ["candidate", *lc.values()]]))
    P("")
    P(f"Paired median difference (candidate - base, {len(pairs)} pairs): turn {med(dturn)} s, model {med(dmod)} s.")

    a.out.write_text(json.dumps(result, indent=2, default=str))
    sys.stdout.write("\n".join(md) + "\n")
    return 0 if clk["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
