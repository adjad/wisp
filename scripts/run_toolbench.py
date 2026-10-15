#!/usr/bin/env python3
"""Hermetic tool-calling scorecard: compare models on Wisp's real router + loop.

    .venv/bin/python scripts/run_toolbench.py --list                      # cases, no model
    .venv/bin/python scripts/run_toolbench.py --dry-run                   # route every case, no model
    .venv/bin/python scripts/run_toolbench.py                             # Ling V3, 3 trials
    .venv/bin/python scripts/run_toolbench.py --models A,B --trials 3
    .venv/bin/python scripts/run_toolbench.py --only mail,safety --ids mail_chase_balance
    .venv/bin/python scripts/run_toolbench.py --models LFM2.5-24B-A2B-MLX-4bit --exclusive
    .venv/bin/python scripts/run_toolbench.py --split gate                    # the frozen fine-tune gate
    .venv/bin/python scripts/run_toolbench.py --world-seed 7                  # a variant world
    .venv/bin/python scripts/run_toolbench.py --models M --thinking default,narration-off,off
    .venv/bin/python scripts/run_toolbench.py --export-shingles out.json      # gate prompts, hashed
    .venv/bin/python scripts/run_toolbench.py --report ~/.moe/evals/toolbench/<run>
    .venv/bin/python scripts/run_toolbench.py --report <run> --compare <baseline_run>

Every tool is a fake over one synthetic world (scripts/toolbench/world.py):
nothing real is read, written or sent, and network/subprocess access is
refused. The real Wisp on :8765 is never contacted. Models run one at a time;
the harness loads a model if needed and never evicts one unless --exclusive is
passed (which unloads the others for the run and reloads them afterwards).
Results land in ~/.moe/evals/toolbench/<run_id>/ (results.jsonl + meta.json).

`scripts/test_model.py` stays the live smoke test against real data; this is
the repeatable comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(line_buffering=True)

from scripts.toolbench import report as rep  # noqa: E402  (no service imports)
from scripts.toolbench.cases import load_cases, select  # noqa: E402

DEFAULT_MODEL = "Ling-3.0-tiny-Wisp-V3-merged-oQ4e"
RESULTS_ROOT = Path.home() / ".moe" / "evals" / "toolbench"  # resolved BEFORE WISP_HOME moves
_TRANSIENT = re.compile(r"409|ModelLoadError|not loaded|ConnectError|RemoteProtocolError|choices", re.I)


def _git_sha() -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def _swap_ok() -> bool:
    """False when free swap is low AND memory pressure is high (same rule as the Ling eval)."""
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    m = re.search(r"free = ([\d.]+)([MG])", out)
    free_mb = float(m.group(1)) * (1024 if m.group(2) == "G" else 1) if m else 1e9
    mp = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    pct = re.search(r"free percentage: (\d+)%", mp)
    return not (free_mb < 500 and (int(pct.group(1)) if pct else 0) < 25)


def _wisp_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "uvicorn service.main:app"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _row(run_id, model, case, trial, traj, verdict, variant="default") -> dict:
    steps = [{k: s.get(k) for k in ("calls", "error", "ttft", "seconds", "prompt_tokens_est",
                                     "output_chars", "reasoning_chars")}
             | {"content": (s.get("content") or "")[:600]} for s in traj.get("steps", ())]
    label = model if variant == "default" else f"{model} +{variant}"
    return {"run": run_id, "model": label, "base_model": model, "variant": variant,
            "split": case.split, "case": case.id, "category": case.category,
            "trial": trial, "no_tool": case.no_tool, "path": traj.get("path"),
            "route": traj.get("route"), "steps": steps, "final": traj.get("final", "")[:2000],
            "effects": traj.get("effects"), "escapes": traj.get("escapes"),
            "error": traj.get("error"), "timeout": traj.get("timeout"), "v": verdict.to_dict()}


async def _dry_run(args, cases) -> int:
    """Route every case (no model call) and flag expectations the router can't reach."""
    from scripts.toolbench import runner
    from scripts.toolbench.world import build_world
    from service.router.pinning import apply_session_pin
    from service.router.router import route
    world = build_world(seed=args.world_seed)
    names = runner.install_fakes(world)
    bad = 0
    for c in (c.bind(world.facts) for c in cases):
        _, ctx = runner._history_messages(c.history)
        d = apply_session_pin(await route(c.prompt, last_user=ctx["last_user"],
                                          recent_users=ctx["recent_users"] or None,
                                          last_assistant=ctx["last_assistant"],
                                          last_tools=ctx["last_tools"]), None, c.prompt)
        offered = set(d.tool_subset or names) | {n for n, _ in (d.direct_calls or ())}
        if d.force_first_tool:
            offered.add(d.force_first_tool)
        miss = [("/".join(s.tools)) for s in c.expect if not set(s.tools) & offered]
        if c.expect and not d.needs_tools:
            miss.append("(plain chat route)")
        flag = "ROUTE-FAULT " + ",".join(miss) if miss else "ok"
        bad += bool(miss)
        menu = len(d.tool_subset) if d.tool_subset else "ALL"
        direct = [n for n, _ in (d.direct_calls or ())]
        print(f"{c.id:<28} {d.role:<8} menu={menu!s:<4} force={d.force_first_tool or '-':<16} "
              f"direct={direct or '-'}  {flag}")
    print(f"\n{len(cases)} cases, {bad} route faults")
    return 0


async def _one(runner, score, client, model, case, world, names, args, run_id, trial, variant) -> dict:
    """One trial, retried once on a transient engine error (409, reload, dropped stream)."""
    traj = await runner.run_case(client, model, case, world, timeout=args.timeout, max_steps=args.max_steps)
    if traj.get("error") and _TRANSIENT.search(traj["error"]):
        await client.make_resident(model)
        traj = await runner.run_case(client, model, case, world, timeout=args.timeout,
                                     max_steps=args.max_steps)
    v = score(case, traj, registry_names=names, today=world.today)
    return _row(run_id, model, case, trial, traj, v, variant)


async def _run(args, cases) -> int:
    from scripts.toolbench import runner
    from scripts.toolbench.scorer import score
    from scripts.toolbench.world import build_world

    world = build_world(seed=args.world_seed)
    names = runner.install_fakes(world)
    cases = [c.bind(world.facts) for c in cases]
    variants = [v.strip() for v in args.thinking.split(",") if v.strip()]
    client = runner.bench_client_class()(timeout=args.timeout)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    installed = await client.models()
    missing = [m for m in models if m not in installed]
    if missing:
        print(f"not installed in oMLX: {missing}")
        close = [i for i in installed if any(m.lower()[:10] in i.lower() for m in missing)]
        if close:
            print(f"  close matches: {close}")
        return 2
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out).expanduser() if args.out else RESULTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    meta = {"run": run_id, "git": _git_sha(), "models": models, "trials": args.trials,
            "cases": [c.id for c in cases], "started": time.time(), "exclusive": args.exclusive,
            "today": world.today.isoformat(), "max_steps": args.max_steps,
            "world_seed": args.world_seed, "thinking": variants, "split": args.split or "all"}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    if _wisp_running():
        print("note: Wisp is running. It may unload a non-roster model mid-run; the harness "
              "reloads and retries once. Quit Wisp for the cleanest numbers.")
    rows: list[dict] = []
    started_loaded = await client.loaded_models()
    try:
        with (out / "results.jsonl").open("a") as fh:
            for model in models:
                if args.exclusive:
                    for other in await client.loaded_models():
                        if other != model:
                            print(f"--exclusive: unloading {other}")
                            await client.unload(other)
                print(f"\n== {model}: loading…")
                await client.make_resident(model)
                for variant in variants:
                    runner.set_thinking(client, model, variant)
                    print(f"-- thinking: {variant}")
                    total, k = len(cases) * args.trials, 0
                    for trial in range(1, args.trials + 1):
                        for case in cases:
                            k += 1
                            if not _swap_ok():
                                print("STOP: memory pressure (free swap < 500MB and pressure high)")
                                return 3
                            row = await _one(runner, score, client, model, case, world, names,
                                             args, run_id, trial, variant)
                            rows.append(row)
                            fh.write(json.dumps(row, default=str) + "\n")
                            fh.flush()
                            v = row["v"]
                            mark = "PASS" if v["passed"] else "FAIL " + ",".join(v["labels"])
                            calls = [c["name"] for s in row["steps"] for c in s.get("calls", ())]
                            print(f"[{k}/{total}] {case.id:<28} t{trial} {v['seconds']:>6.1f}s  {mark}"
                                  f"  {calls}")
    finally:
        if args.exclusive:
            for m in started_loaded:
                if m not in await client.loaded_models():
                    print(f"--exclusive: restoring {m}")
                    try:
                        await client.make_resident(m)
                    except Exception as e:  # noqa: BLE001
                        print(f"  could not restore {m}: {e}")
    print("\n" + rep.render(rows))
    print(f"\nresults: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=DEFAULT_MODEL, help="comma-separated oMLX model ids")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--only", default="", help="categories (case file stems), comma-separated")
    ap.add_argument("--ids", default="", help="case ids, comma-separated")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-turn seconds")
    ap.add_argument("--max-steps", type=int, default=8, help="agent loop step ceiling (production: 8)")
    ap.add_argument("--exclusive", action="store_true",
                    help="unload every other model while a model runs; restore them afterwards")
    ap.add_argument("--out", default="", help="results directory (default ~/.moe/evals/toolbench/<run>)")
    ap.add_argument("--split", default="", choices=["", "gate", "dev"], help="only this split")
    ap.add_argument("--world-seed", type=int, default=None,
                    help="variant world (default: the canonical gate world)")
    ap.add_argument("--thinking", default="default",
                    help="comma-separated variants: default, narration-off, off")
    ap.add_argument("--export-shingles", default="",
                    help="write hashed n-grams of every gate prompt (canonical + 50 seeded worlds) and exit")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--dry-run", action="store_true", help="route every case without a model")
    ap.add_argument("--report", default="", help="re-render a finished run directory")
    ap.add_argument("--compare", default="", help="with --report: baseline run directory")
    args = ap.parse_args()

    if args.report:
        cur = rep.load_rows(Path(args.report).expanduser())
        print(rep.render(cur))
        if args.compare:
            print("\n" + rep.compare(rep.load_rows(Path(args.compare).expanduser()), cur))
        return 0
    cases = select(load_cases(), only=args.only, ids=args.ids, split=args.split)
    if not cases:
        print("no cases selected")
        return 2
    if args.export_shingles:
        from scripts.toolbench.cases import heldout_shingles
        from scripts.toolbench.world import make_facts
        worlds = [make_facts(None)] + [make_facts(i) for i in range(50)]
        data = heldout_shingles(cases, worlds)
        Path(args.export_shingles).expanduser().write_text(json.dumps(data, separators=(",", ":")) + "\n")
        print(f"wrote {data['n_prompts']} hashed gate prompts to {args.export_shingles}")
        return 0
    if args.list:
        for c in cases:
            want = "no tool" if c.no_tool else " > ".join("/".join(s.tools) for s in c.expect) or "-"
            print(f"{c.split:<5} {c.category:<10} {c.id:<28} {want:<40} {c.prompt[:60]}")
        print(f"\n{len(cases)} cases")
        return 0
    if args.trials < 1:
        ap.error("--trials must be >= 1")

    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp(prefix="toolbench-")))
    if args.dry_run:
        return asyncio.run(_dry_run(args, cases))
    return asyncio.run(_run(args, cases))


if __name__ == "__main__":
    raise SystemExit(main())
