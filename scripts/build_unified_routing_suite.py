#!/usr/bin/env python3
"""Build the unified, frozen routing evaluation suite (docs/ROUTING_EVAL_SUITE.md).

Merges, without modifying any source:
  * claude_corpus     test_fixtures/routing/quality_corpus.json (439, PR #159)
  * claude_heldout    test_fixtures/routing/comparison_heldout.json (104)
  * codex_*           test_fixtures/routing/unified/sources/codex/ — copied byte-identical
                      from origin/codex/independent-routing-eval-20261005
                      (eval/independent-routing-20261005/, Codex, 2026-10-05):
                      corpus.json (18 dev, 138 test, 12 challenge) and
                      followon_corpus.json (12 dev, 36 test)
  * historical        test_fixtures/routing_regressions/historical_prompts.json (22)
  * stress            test_fixtures/routing_stress/suite.json (1000)
  * invariant         test_fixtures/routing/unified/invariants.v1.json (PR131/150/153/159)

Optional: --recorded <dir> extracts Codex's recorded Ling intent outputs
(local, synthetic, git-ignored on its branch) into
test_fixtures/routing/unified/recorded/codex_ling_intents.v1.jsonl so CI can
replay them without a live model.

Every source is checked against a recorded sha256; the output is written with
sorted keys and its own sha256. Re-running with unchanged sources is a no-op.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
U = ROOT / "test_fixtures/routing/unified"
SUITE = U / "suite.v1.json"
CODEX_BRANCH_COMMIT = "c59d7f3b84c989d8940ccf598bc44f9d54edec57"
CODEX_CLOCK = {"now": "2026-10-05T00:55:00", "tz": "America/Los_Angeles"}

SOURCES = {
    "claude_corpus": "test_fixtures/routing/quality_corpus.json",
    "claude_heldout": "test_fixtures/routing/comparison_heldout.json",
    "codex_corpus": "test_fixtures/routing/unified/sources/codex/corpus.json",
    "codex_followon": "test_fixtures/routing/unified/sources/codex/followon_corpus.json",
    "codex_scoring_sensitivity": "test_fixtures/routing/unified/sources/codex/scoring_sensitivity.json",
    "historical": "test_fixtures/routing_regressions/historical_prompts.json",
    "stress": "test_fixtures/routing_stress/suite.json",
    "invariants": "test_fixtures/routing/unified/invariants.v1.json",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ctx(last_user=None, last_assistant=None, last_tools=None, history=None):
    return {"history": history or [], "last_user": last_user,
            "last_assistant": last_assistant, "last_tools": last_tools}


def _codex_ctx(c: dict) -> dict:
    hist = [{"role": m["role"], "content": m["content"]} for m in c.get("context", [])]
    users = [m["content"] for m in hist if m["role"] == "user"]
    assistants = [m["content"] for m in hist if m["role"] == "assistant"]
    tools = c.get("context_tools")
    return _ctx(users[-1] if users else None, assistants[-1] if assistants else None,
                ", ".join(tools) if isinstance(tools, list) else tools, hist)


def build() -> dict:
    cases: list[dict] = []

    corpus = json.loads((ROOT / SOURCES["claude_corpus"]).read_text())["cases"]
    for c in corpus:
        cases.append(dict(
            uid=f"claude_corpus:{c['id']}", origin="claude_corpus", split="dev", family=c["domain"],
            prompt=c["prompt"], context=_ctx(c["last_user"], c["last_assistant"], c["last_tools"]),
            clock=None, gold=dict(first_calls=None, tools_any=c["need"], forbidden_tools=c["forbid"],
                                  avoid_tools=c["avoid"], force=c["force"], expect=c["expect"],
                                  intent_hint=None, hard=None)))

    held = json.loads((ROOT / SOURCES["claude_heldout"]).read_text())
    for c in held["cases"]:
        expect = {"chat": "chat", "clarify": "clarify"}.get(c["intent"], "action")
        cases.append(dict(
            uid=f"claude_heldout:{c['id']}", origin="claude_heldout", split="heldout", family=c["category"],
            prompt=c["prompt"], context=_ctx(c["last_user"], c["last_assistant"], c["last_tools"]),
            clock=None, gold=dict(first_calls=None, tools_any=[c["first"]] if c["first"] and expect == "action" else [],
                                  forbidden_tools=[], avoid_tools=[], force=None, expect=expect,
                                  intent_hint={"label": c["intent"], "source": c["source"]}, hard=None)))

    for key, origin_map in (("codex_corpus", {"dev": "dev", "test": "heldout", "challenge": "challenge"}),
                            ("codex_followon", {"dev": "dev", "test": "heldout"})):
        for c in json.loads((ROOT / SOURCES[key]).read_text()):
            g = c["gold"]
            origin = ("codex_fresh36" if key == "codex_followon" and c["split"] == "test" else
                      "codex_heldout138" if key == "codex_corpus" and c["split"] == "test" else
                      f"{key}_{c['split']}")
            first = None if c["split"] == "challenge" else dict(tools=g.get("tools", []), args=g.get("args", {}),
                                                                 absent=g.get("absent", {}))
            cases.append(dict(
                uid=f"{key}:{c['id']}", origin=origin, split=origin_map[c["split"]], family=c["family"],
                prompt=c["prompt"], context=_codex_ctx(c), clock=CODEX_CLOCK,
                gold=dict(first_calls=first, tools_any=[[t] for t in g.get("tools", [])],
                          forbidden_tools=g.get("forbidden", []), avoid_tools=[], force=None,
                          expect="action" if g.get("tools") else "chat",
                          intent_hint={"domains": g.get("domains"), "note": g.get("note")}, hard=None)))

    hist = json.loads((ROOT / SOURCES["historical"]).read_text())["cases"]
    for c in hist:
        cases.append(dict(
            uid=f"historical:{c['id']}", origin="historical", split="regression", family="historical",
            prompt=c["prompt"], context=_ctx(c.get("last_user"), c.get("last_assistant"), c.get("last_tools")),
            clock=None, gold=dict(first_calls=None, tools_any=c.get("required_tool_groups", []),
                                  forbidden_tools=c.get("forbidden_tools", []), avoid_tools=[], force=None,
                                  expect="action", intent_hint=None,
                                  hard={"reminder_action": c["expected_reminder_action"]}
                                  if c.get("expected_reminder_action") else None)))

    stress = json.loads((ROOT / SOURCES["stress"]).read_text())["cases"]
    for c in stress:
        turns = c.get("context_turns") or []
        users = [t.get("user") or t.get("content") for t in turns if isinstance(t, dict)]
        cases.append(dict(
            uid=f"stress:{c['id']}", origin="stress", split="stress", family=c.get("category", "stress"),
            prompt=c["prompt"], context=_ctx(users[-1] if users else None),
            clock=None, gold=dict(first_calls=None, tools_any=[[t] for t in c.get("required_tools", [])],
                                  forbidden_tools=c.get("forbidden_tools", []), avoid_tools=[], force=None,
                                  expect="clarify" if c.get("clarification_expected") else "action",
                                  intent_hint=None, hard=None)))

    inv = json.loads((ROOT / SOURCES["invariants"]).read_text())["cases"]
    for c in inv:
        hard = {k: c[k] for k in ("never_reach", "never_commit", "must_reach", "needs_tools", "reminder_action")
                if k in c}
        cases.append(dict(
            uid=f"invariant:{c['id']}", origin="invariant", split="invariant", family=c["contract"].split()[0],
            prompt=c["prompt"], context=_ctx(c.get("last_user"), c.get("last_assistant"), c.get("last_tools")),
            clock=None, gold=dict(first_calls=None, tools_any=[], forbidden_tools=[], avoid_tools=[], force=None,
                                  expect="either", intent_hint=None, hard=hard)))

    uids = [c["uid"] for c in cases]
    assert len(uids) == len(set(uids)), "duplicate uid"
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["origin"]] = counts.get(c["origin"], 0) + 1
    return {
        "version": "1",
        "description": "Unified frozen routing evaluation suite. See docs/ROUTING_EVAL_SUITE.md.",
        "credits": {"codex": f"Codex independent routing evaluation, origin/codex/independent-routing-eval-20261005 @ {CODEX_BRANCH_COMMIT}, eval/independent-routing-20261005/ (copied unchanged)",
                    "claude": "PR #159 routing-quality corpus and held-out set"},
        "sources": {k: {"path": v, "sha256": sha(ROOT / v)} for k, v in SOURCES.items()},
        "counts": dict(sorted(counts.items())), "total": len(cases),
        "cases": cases,
    }


def extract_recorded(src: Path) -> list[dict]:
    """Recorded Ling intent outputs from Codex's local (synthetic) run files."""
    out = []
    for name, arm in (("followon_test.jsonl", "ling_intent_v2"), ("followon_dev_v2.jsonl", "ling_intent_v2"),
                      ("heldout.jsonl", "ling_intent")):
        path = src / name
        if not path.exists():
            continue
        for line in path.open():
            r = json.loads(line)
            if r.get("arm") != arm:
                continue
            trace = r.get("planner_trace") or ([r["planner"]] if r.get("planner") else [])
            steps = []
            for t in trace:
                resp = t.get("response") or {}
                choice = (resp.get("choices") or [{}])[0]
                usage = resp.get("usage") or {}
                steps.append(dict(content=(choice.get("message") or {}).get("content"),
                                  finish_reason=choice.get("finish_reason"),
                                  seconds=round(float(t.get("seconds", 0.0)), 4),
                                  prompt_tokens=usage.get("prompt_tokens"),
                                  completion_tokens=usage.get("completion_tokens"),
                                  cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                                  validation_error=t.get("validation_error")))
            system = (trace[0].get("request") or {}).get("messages", [{}])[0].get("content", "") if trace else ""
            origin_key = "codex_followon" if name.startswith("followon") else "codex_corpus"
            out.append(dict(uid=f"{origin_key}:{r['id']}", arm=arm, rep=r.get("rep"), source_file=name,
                            prompt_sha256=hashlib.sha256(r["prompt"].encode()).hexdigest(),
                            system_sha256=hashlib.sha256(system.encode()).hexdigest(),
                            steps=steps, intent=r.get("intent"), compiled=r.get("compiled"),
                            calls=r.get("calls"), codex_grade=r.get("grade"),
                            seconds=round(float(r.get("seconds", 0.0)), 4)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the committed suite differs")
    ap.add_argument("--recorded", type=Path, help="Codex eval dir holding the local *.jsonl run files")
    args = ap.parse_args()
    codex_sha = (ROOT / "test_fixtures/routing/unified/sources/codex/corpus.sha256").read_text().split()[0]
    if sha(ROOT / SOURCES["codex_corpus"]) != codex_sha:
        raise SystemExit("Codex corpus does not match its recorded sha256")
    data = json.dumps(build(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    digest = hashlib.sha256(data.encode()).hexdigest()
    if args.check:
        ok = SUITE.exists() and SUITE.read_text() == data
        print("suite up to date" if ok else "suite differs from sources")
        return 0 if ok else 1
    SUITE.write_text(data)
    SUITE.with_suffix(".sha256").write_text(f"{digest}  {SUITE.relative_to(ROOT)}\n")
    print(f"{digest}  {SUITE.relative_to(ROOT)}")
    if args.recorded:
        rec = extract_recorded(args.recorded)
        path = U / "recorded" / "codex_ling_intents.v1.jsonl"
        path.parent.mkdir(exist_ok=True)
        body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rec)
        path.write_text(body)
        path.with_suffix(".sha256").write_text(
            f"{hashlib.sha256(body.encode()).hexdigest()}  {path.relative_to(ROOT)}\n")
        print(f"recorded intents: {len(rec)} -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
