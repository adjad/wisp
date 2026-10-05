#!/usr/bin/env python3
"""Score the real router against the held-out routing-quality corpus.

Router only: no model, embedding server, network, tool body or user data is
touched. `route()` runs exactly as main.py calls it (plus the session pin), with
the packaged lexical retrieval provider and the repository's stub embedder
installed in case a configuration selects the embedding provider. Sockets are
disabled for the whole run, so an accidental network dependency fails loudly.

Each case names what the USER needs (any-of tool groups), what may be forced
first, what must never be offered (hard leakage) and what should not be offered
(soft, wrong-direction). Failure classes:

  hard  zero_tool       an action request reached the model with no tools
        missing_tool    a needed tool group is not reachable
        wrong_domain    NO needed group is reachable
        leak_forbidden  an unsafe tool (send/delete...) is offered
        forced_wrong    a tool is forced/direct-called that the request did not ask for
        full_registry   needs tools but no subset (whole registry offered)
        acted_on_chat   a conversational/clarify prompt got a forced or direct call
  soft  not_forced      a single obvious tool exists but nothing is forced
        leak_avoid      a wrong-direction tool (write on a read...) is offered
        generic_fallback  the ~20-tool retrieved "ambiguous" fallback was used
        wide_menu       more than 12 tools offered

Usage:
  python scripts/score_routing_quality.py [--corpus PATH] [--json-out PATH]
         [--md-out PATH] [--baseline PATH] [--only DOMAIN,...] [--verbose]
         [--write-ratchet]

tests/test_routing_quality_corpus.py gates CI on this scorer: every id in
test_fixtures/routing/quality_ratchet.json must keep passing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEFAULT_CORPUS = ROOT / "test_fixtures/routing/quality_corpus.json"
RATCHET = ROOT / "test_fixtures/routing/quality_ratchet.json"

HARD = ("zero_tool", "missing_tool", "wrong_domain", "leak_forbidden", "forced_wrong",
        "full_registry", "acted_on_chat")
SOFT = ("not_forced", "leak_avoid", "generic_fallback", "wide_menu")
WIDE = 12
_OUTBOUND = frozenset({"send_email", "send_message", "reply_to_email", "draft_email", "draft_message",
                       "forward_email", "lookup_contact"})
_CHANNEL_WORD = re.compile(r"\b(?:e-?mail\w*|emial|texts?|txt|i?message|sms|reply)\b", re.I)


def _no_network(*_a, **_k):
    raise RuntimeError("routing-quality scorer must not open sockets")


def _isolate() -> None:
    socket.socket.connect = _no_network  # type: ignore[assignment]
    socket.create_connection = _no_network  # type: ignore[assignment]


def offered_tools(decision, registry) -> tuple[set[str], set[str]]:
    """(reachable tools, forced-or-direct tools) as the agent loop would see them."""
    if decision is None or not decision.needs_tools:
        reach: set[str] = set()
    elif decision.tool_subset is None:
        reach = set(registry)
    else:
        reach = set(decision.tool_subset)
    forced: set[str] = set()
    if decision is not None:
        direct = {n for n, _ in (decision.direct_calls or ())}
        groups = {n for g in (decision.required_tool_groups or ()) for n in g}
        reach |= direct | groups
        forced |= direct
        if decision.force_first_tool:
            reach.add(decision.force_first_tool)
            forced.add(decision.force_first_tool)
        # Single-tool required groups are an implicit force in the agent loop.
        forced |= {next(iter(g)) for g in (decision.required_tool_groups or ()) if len(g) == 1}
        reach -= set(decision.forbidden_tools or ())
        forced -= set(decision.forbidden_tools or ())
    return reach, forced


def classify(case: dict, decision, registry) -> dict:
    reach, forced = offered_tools(decision, registry)
    need = [set(g) for g in case["need"]]
    needed = set().union(*need) if need else set()
    classes: list[str] = []
    expect = case["expect"]
    is_action = expect == "action"
    if is_action and not reach:
        classes.append("zero_tool")
    covered = [bool(g & reach) for g in need]
    # "tell Sam I'm late" names no channel: withholding every committing
    # outbound tool until the user answers "text or email?" is the designed
    # safe behavior, not a missing tool. An explicitly named channel is not
    # excused.
    if (decision is not None and getattr(decision, "clarify_channel", False)
            and not _CHANNEL_WORD.search(case["prompt"])):
        covered = [ok or bool(g <= _OUTBOUND) for g, ok in zip(need, covered)]
    if need and not all(covered):
        classes.append("missing_tool")
        if not any(covered):
            classes.append("wrong_domain")
    if set(case["forbid"]) & reach:
        classes.append("leak_forbidden")
    if decision is not None and decision.needs_tools and decision.tool_subset is None:
        classes.append("full_registry")
    force = case["force"]
    if forced:
        if expect in ("chat", "clarify"):
            classes.append("acted_on_chat")
        elif force == [] or (force and not (forced & set(force))
                             and not (forced & needed)) or (force is None and needed
                                                            and not (forced & needed)):
            classes.append("forced_wrong")
    elif force and is_action:
        classes.append("not_forced")
    if set(case["avoid"]) & reach:
        classes.append("leak_avoid")
    reason = "" if decision is None else (decision.reason or "")
    if decision is not None and decision.source == "default":
        classes.append("generic_fallback")
    if len(reach) > WIDE:
        classes.append("wide_menu")
    recall = (sum(covered) / len(need)) if need else 1.0
    relevant = (len(reach & needed) / len(reach)) if (reach and needed) else None
    return dict(
        id=case["id"], domain=case["domain"], styles=case["styles"], prompt=case["prompt"],
        expect=expect, passed=not any(c in HARD for c in classes), classes=classes,
        reason=reason, source=None if decision is None else decision.source,
        role=None if decision is None else decision.role,
        menu=len(reach), forced=sorted(forced), recall=recall, relevance=relevant,
        tools=sorted(reach) if len(reach) <= 40 else ["<%d tools>" % len(reach)],
        missing=[sorted(g) for g, ok in zip(need, covered) if not ok],
        leaked=sorted(set(case["forbid"]) & reach),
    )


async def run(cases: list[dict]) -> list[dict]:
    import service.router.router as R
    import service.tools  # noqa: F401  register the roster
    from service.router.pinning import apply_session_pin
    from service.tools.registry import REGISTRY
    from tests.stub_embedder import install as install_stub_embedder

    install_stub_embedder()
    out = []
    for case in cases:
        t0 = time.perf_counter()
        decision = await R.route(
            case["prompt"], last_user=case.get("last_user"),
            recent_users=[case["last_user"]] if case.get("last_user") else None,
            last_assistant=case.get("last_assistant"), last_tools=case.get("last_tools"))
        decision = apply_session_pin(decision, None, case["prompt"])
        row = classify(case, decision, REGISTRY)
        row["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        out.append(row)
    return out


def summarize(rows: list[dict]) -> dict:
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_domain[r["domain"]].append(r)

    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        rel = [r["relevance"] for r in rs if r["relevance"] is not None]
        cls = Counter(c for r in rs for c in r["classes"])
        return dict(n=n, passed=sum(r["passed"] for r in rs),
                    pass_rate=round(sum(r["passed"] for r in rs) / n, 3) if n else 0.0,
                    recall=round(sum(r["recall"] for r in rs) / n, 3) if n else 0.0,
                    precision=round(sum(rel) / len(rel), 3) if rel else None,
                    mean_menu=round(sum(r["menu"] for r in rs) / n, 1) if n else 0.0,
                    classes={c: cls[c] for c in (*HARD, *SOFT) if cls[c]})
    ms = sorted(r["ms"] for r in rows)
    return dict(overall=agg(rows), domains={d: agg(rs) for d, rs in sorted(by_domain.items())},
                latency_ms=dict(p50=ms[len(ms) // 2], p95=ms[int(len(ms) * 0.95)], max=ms[-1]) if ms else {},
                fallback_reasons=Counter(r["reason"].split(" -> ")[0] for r in rows
                                         if "generic_fallback" in r["classes"]).most_common(10))


def markdown(summary: dict, rows: list[dict], baseline: dict | None) -> str:
    base = {r["id"]: r for r in (baseline or {}).get("rows", [])}
    bs = (baseline or {}).get("summary", {}).get("domains", {})
    lines = ["| domain | n | pass | recall | precision | mean menu | top classes |" + (" base pass |" if base else ""),
             "|---|---|---|---|---|---|---|" + ("---|" if base else "")]
    for d, a in [*summary["domains"].items(), ("**overall**", summary["overall"])]:
        top = ", ".join(f"{k}:{v}" for k, v in sorted(a["classes"].items(), key=lambda kv: -kv[1])[:4])
        prev = (bs.get(d) or (baseline or {}).get("summary", {}).get("overall") if d == "**overall**" else bs.get(d)) if base else None
        lines.append(f"| {d} | {a['n']} | {a['passed']}/{a['n']} ({a['pass_rate']:.0%}) | {a['recall']:.2f} | "
                     f"{'-' if a['precision'] is None else f'{a['precision']:.2f}'} | {a['mean_menu']} | {top} |"
                     + (f" {prev['passed']}/{prev['n']} |" if prev else (" - |" if base else "")))
    if base:
        fixed = [r["id"] for r in rows if r["passed"] and r["id"] in base and not base[r["id"]]["passed"]]
        broke = [r["id"] for r in rows if not r["passed"] and r["id"] in base and base[r["id"]]["passed"]]
        lines += ["", f"Fixed vs baseline ({len(fixed)}): {', '.join(fixed) or '-'}",
                  f"Regressed vs baseline ({len(broke)}): {', '.join(broke) or '-'}"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--md-out", type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--write-ratchet", action="store_true",
                    help="record the currently passing case ids (only after an intentional improvement)")
    args = ap.parse_args()
    _isolate()
    cases = json.loads(args.corpus.read_text())["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["domain"] in keep or c["id"] in keep]
    rows = asyncio.run(run(cases))
    summary = summarize(rows)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    md = markdown(summary, rows, baseline)
    if args.verbose:
        for r in rows:
            if not r["passed"] or r["classes"]:
                print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['id']:<18} {r['prompt']!r}\n"
                      f"     classes={r['classes']} menu={r['menu']} forced={r['forced']}\n"
                      f"     reason={r['reason'][:140]}\n     missing={r['missing']} leaked={r['leaked']}")
    print(md)
    o = summary["overall"]
    print(f"\nrouting quality: {o['passed']}/{o['n']} pass; latency p50={summary['latency_ms'].get('p50')}ms "
          f"p95={summary['latency_ms'].get('p95')}ms")
    if args.json_out:
        args.json_out.write_text(json.dumps(dict(summary=summary, rows=rows), indent=1))
    if args.md_out:
        args.md_out.write_text(md + "\n")
    if args.write_ratchet:
        ratchet = json.loads(RATCHET.read_text())
        ratchet["passing"] = sorted(r["id"] for r in rows if r["passed"])
        RATCHET.write_text(json.dumps(ratchet, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
