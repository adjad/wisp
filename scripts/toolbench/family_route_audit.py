"""Offline route audit for a fine-tune dataset's prompt families.

For every prompt a families module can generate, run the real route() +
apply_session_pin and check that the tool the family expects is OFFERED (or,
for a no-tool family, that nothing is forced). This is the check behind a
dataset's ROUTER_FINDINGS.md: a family that fails here cannot be answered by
any model, so it is a router fix, not a training target.

No model and no engine: the oMLX port is closed and retrieval uses the stub
embedder, so retrieval-dependent menus are approximate. Run:

    .venv/bin/python scripts/toolbench/family_route_audit.py \\
        --families training/mellum-dataset-r1-20261014/families.py [--only id,id] [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import itertools
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fill(template: str, mapping: dict) -> str:
    for _ in range(2):  # slot values may themselves name facts ("lunch with {coworker_first}")
        template = template.format_map(_Keep(mapping))
    return template


def _load_families(path: str):
    spec = importlib.util.spec_from_file_location("audit_families", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module.FAMILIES


def _verdict(d, names: set[str], expect: list[list[str]], no_tool: bool, forbid: list[str]) -> bool:
    offered = set(d.tool_subset or names) | {n for n, _ in (d.direct_calls or ())}
    if d.force_first_tool:
        offered.add(d.force_first_tool)
    if no_tool:  # offered tools are fine; a forced step or a clarify is not
        return not (d.force_first_tool or d.direct_calls or d.expect_tool_first
                    or d.required_tool_groups or "clarify" in (d.reason or ""))
    if not expect:  # safety-only family: nothing it forbids may be offered
        return not (d.needs_tools and offered & set(forbid))
    return bool(d.needs_tools) and all(set(any_of) & offered for any_of in expect)


async def audit(families, *, seed: int | None, only: set[str] | None) -> dict:
    from scripts.toolbench import runner
    from scripts.toolbench.world import build_world
    from service.router.pinning import apply_session_pin
    from service.router.router import route

    world = build_world(seed=seed)
    names = runner.install_fakes(world)
    out = {}
    for fam in families:
        if only and fam.id not in only:
            continue
        expect = [e.get("any_of") or [e["tool"]] for e in fam.expect]
        combos = list(itertools.product(*fam.slots.values())) or [()]
        passed, fails = 0, []
        for template in fam.prompts:
            for combo in combos:
                mapping = dict(world.facts)
                for part in combo:
                    mapping.update(part)
                prompt = _fill(template, mapping)
                history = tuple({k: _fill(v, mapping) for k, v in turn.items()} for turn in fam.history)
                _, ctx = runner._history_messages(history)
                d = apply_session_pin(await route(
                    prompt, last_user=ctx["last_user"], recent_users=ctx["recent_users"] or None,
                    last_assistant=ctx["last_assistant"], last_tools=ctx["last_tools"]), None, prompt)
                if _verdict(d, names, expect, fam.no_tool, fam.forbid):
                    passed += 1
                else:
                    fails.append({"prompt": prompt, "reason": d.reason})
        total = len(fam.prompts) * len(combos)
        out[fam.id] = {"passed": passed, "total": total, "fails": fails}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--families", required=True, help="path to a dataset families.py")
    ap.add_argument("--only", default="", help="comma-separated family ids")
    ap.add_argument("--seed", type=int, default=None, help="world seed (default: canonical world)")
    ap.add_argument("--json", default=None, help="write the per-family result here")
    args = ap.parse_args()

    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp()), omlx_port=1)  # no engine, not even embeddings
    sys.path.insert(0, str(ROOT / "tests"))
    import stub_embedder
    stub_embedder.install()

    result = asyncio.run(audit(_load_families(args.families), seed=args.seed,
                               only={s for s in args.only.split(",") if s} or None))
    for fid, row in result.items():
        print(f"{fid:<24} {row['passed']}/{row['total']}")
        for reason in dict.fromkeys(f["reason"] for f in row["fails"]):
            example = next(f["prompt"] for f in row["fails"] if f["reason"] == reason)
            print(f"    FAIL {example!r}: {reason}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
