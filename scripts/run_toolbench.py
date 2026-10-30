#!/usr/bin/env python3
"""Hermetic tool-calling scorecard: compare models on Wisp's real model-led Ask turn.

    .venv/bin/python scripts/run_toolbench.py --list                      # cases, no model
    .venv/bin/python scripts/run_toolbench.py --dry-run                   # load + bind cases, no model
    .venv/bin/python scripts/run_toolbench.py                             # the rostered agent model, 3 trials
    .venv/bin/python scripts/run_toolbench.py --models A,B --trials 3
    .venv/bin/python scripts/run_toolbench.py --only mail,safety --ids mail_chase_balance
    .venv/bin/python scripts/run_toolbench.py --models LFM2.5-24B-A2B-MLX-4bit --exclusive
    .venv/bin/python scripts/run_toolbench.py --split gate                    # the frozen fine-tune gate
    .venv/bin/python scripts/run_toolbench.py --world-seed 7                  # a variant world
    .venv/bin/python scripts/run_toolbench.py --models M --thinking default,narration-off,off
    .venv/bin/python scripts/run_toolbench.py --models G,N --profile-as G     # N treated exactly as G
    .venv/bin/python scripts/run_toolbench.py --omlx-port 8001                # a second engine
    .venv/bin/python scripts/run_toolbench.py --export-shingles out.json      # held-out prompts, hashed
    .venv/bin/python scripts/run_toolbench.py --report ~/.moe/evals/toolbench/<run>   # rescored offline
    .venv/bin/python scripts/run_toolbench.py --report <run> --stored        # the verdicts stored at run time
    .venv/bin/python scripts/run_toolbench.py --report <run> --compare <baseline_run>

Every turn runs the way production's model-led Ask does (runner.prepare_turn):
model_only on, model-led discovery from a small preload plus name-only stubs,
the Ask hints and guards, and the case's access mode. Every tool is a fake over
one synthetic world (scripts/toolbench/world.py): nothing real is read, written
or sent, and network/subprocess access is refused. The real Wisp on :8765 is
never contacted: a live run refuses to start while :8765 or :8791 listens
(--allow-wisp-running overrides). Models run one at a time; the harness loads a
model if needed and never evicts one unless --exclusive is passed (which unloads
the others for the run and reloads them afterwards).

A fair comparison: with more than one model, --exclusive is the default
(--shared opts out) and --profile-as defaults to the packaged agent model.
Before any trial the engine's per-model settings of each candidate and of the
profile model are compared on runner.PARITY_KEYS (sampling, thinking, window,
MTP); a mismatch aborts unless --allow-engine-mismatch. The clock is pinned to
the world's "now" for prompts and tools, and trial t sends seed --seed-base + t
to every model. An engine/stream failure is labelled engine_error and excluded
from every rate (the report counts them), not scored as the model's.

Results land in ~/.moe/evals/toolbench/<run_id>/ (results.jsonl + meta.json).
meta.json records the pinned time, the seeds, both sides' engine settings and
their mismatches, and per model the binding (context window, list membership,
--profile-as) and the effective thinking flags of each variant.

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
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.toolbench import report as rep  # noqa: E402  (no service imports)
from scripts.toolbench.cases import load_cases, select  # noqa: E402

# Empty = the packaged models.yaml agent model (what a fresh install runs).
DEFAULT_MODEL = ""
RESULTS_ROOT = Path.home() / ".moe" / "evals" / "toolbench"  # resolved BEFORE WISP_HOME moves
_TRANSIENT = re.compile(r"409|ModelLoadError|not loaded|ConnectError|RemoteProtocolError|choices|"
                        r"IncompleteStreamError|Invalid inference stream data|"
                        r"connection unavailable|couldn't verify the local AI engine", re.I)
# The live Wisp backend and its bundled engine. A run refuses to start while
# either listens: Wisp would contend for the one GPU and may evict or reload
# models mid-run.
WISP_PORTS = (8765, 8791)


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


def _listening(port: int) -> bool:
    """True when something on this Mac listens on TCP `port`. Asks lsof; never connects."""
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _row(run_id, model, case, trial, traj, verdict, variant="default", profile_as=None) -> dict:
    steps = [{k: s.get(k) for k in ("calls", "error", "tool_errors", "ttft", "seconds", "prompt_tokens_est",
                                     "output_chars", "reasoning_chars", "stubs", "offered", "guard_400")}
             | {"content": (s.get("content") or "")[:600]} for s in traj.get("steps", ())]
    label = model if variant == "default" else f"{model} +{variant}"
    return {"run": run_id, "model": label, "base_model": model, "variant": variant,
            "profile_as": profile_as, "access": traj.get("access"),
            "split": case.split, "case": case.id, "category": case.category,
            "trial": trial, "no_tool": case.no_tool, "path": traj.get("path"),
            "route": traj.get("route"), "steps": steps, "final": traj.get("final", "")[:2000],
            "effects": traj.get("effects"), "asked": traj.get("asked"), "escapes": traj.get("escapes"),
            "error": traj.get("error"), "timeout": traj.get("timeout"), "v": verdict.to_dict()}


def _dry_run(args, cases) -> int:
    """Offline: bind every case to the world and check its tool names and host
    preparation. Runs no model and tests no routing (production has no router
    menu any more; the model chooses). `host-forbids` flags an expected tool the
    host's own guards withhold this turn, which no model can then call."""
    from scripts.toolbench import runner
    from scripts.toolbench.world import build_world
    world = build_world(seed=args.world_seed)
    names = runner.install_fakes(world)
    runner.set_clock(world.now)
    try:
        return _dry_run_bound(args, cases, runner, world, names)
    finally:
        runner.set_clock(None)
        runner.uninstall_fakes()


def _dry_run_bound(args, cases, runner, world, names) -> int:
    model = runner.bind_model(args.models.split(",")[0].strip() or _agent_model(),
                              profile_as=args.profile_as or None)["model"]
    from service.config.endpoints import role_target
    client = runner.bench_client_class()(target=role_target("agent"))
    bad = 0
    for c in (c.bind(world.facts) for c in cases):
        problems = []
        for field, tools in (("expect", {t for s in c.expect for t in s.tools}), ("forbid", c.forbid),
                             ("confirm", c.confirm), ("no_confirm", c.no_confirm),
                             ("refused", c.refused)):
            if unknown := sorted(set(tools) - names):
                problems.append(f"{field} names unknown {unknown}")
        turn = runner.prepare_turn(client, c.prompt, c.history)
        withheld = set(turn.route["forbidden_tools"])
        if blocked := ["/".join(s.tools) for s in c.expect if not set(s.tools) - withheld]:
            problems.append(f"host-forbids {blocked}")
        bad += bool(problems)
        head = ",".join(turn.route["preload"][:3]) or "-"
        needs = ",".join(sorted(c.access_needed)) or "-"
        print(f"{c.id:<28} {c.access:<7} denied={needs:<9} direct={'y' if turn.route['ask_direct'] else '-'} "
              f"preload={head:<44} {'; '.join(problems) or 'ok'}")
    print(f"\n{len(cases)} cases bound for {model}, {bad} with problems (no model was run)")
    return 0


def rescore(run_dir: Path, rows: list[dict]) -> tuple[list[dict], dict]:
    """`rows` with every verdict recomputed by the CURRENT scorer, cases and fakes.

    A finished run keeps trajectories (calls, effects, asked, final) but not the
    judgement; scoring is a pure function of those, so a fixed scorer or case file
    applies to old runs without a model. Needs the isolated service import the dry
    run uses (no server, no model). The world is rebuilt from the run's meta.json
    (date, world seed) so `{fact}` placeholders bind to what the model saw. A row
    whose case no longer exists keeps its stored verdict. Returns (rows, summary).
    """
    from datetime import date

    from scripts.toolbench import runner
    from scripts.toolbench.scorer import score
    from scripts.toolbench.world import build_world
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    today = date.fromisoformat(meta["today"]) if meta.get("today") else None
    world = build_world(today, seed=meta.get("world_seed"))
    names = runner.install_fakes(world, seed=False)
    try:
        writes, refused = runner.write_tool_names(), runner.host_refused_tools()
    finally:
        runner.uninstall_fakes()
    cases = {c.id: c.bind(world.facts) for c in load_cases()}
    out, kept, changed = [], 0, 0
    for row in rows:
        case = cases.get(row["case"])
        if case is None:
            kept += 1
            out.append(row)
            continue
        v = score(case, rep.traj_from_row(row, meta), registry_names=names, today=world.today,
                  write_tools=writes, host_refused=refused).to_dict()
        changed += v["passed"] != row["v"]["passed"]
        out.append({**row, "v": v, "v_stored": row["v"]})
    return out, {"rows": len(rows), "rescored": len(rows) - kept, "kept_stored": kept,
                 "verdict_flipped": changed}


def _pass_counts(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """model -> (passed, scored trials), engine-excluded trials left out."""
    out: dict[str, list[int]] = {}
    for r in rows:
        if r["v"].get("excluded"):
            continue
        n = out.setdefault(r["model"], [0, 0])
        n[0] += bool(r["v"]["passed"])
        n[1] += 1
    return {m: (a, b) for m, (a, b) in out.items()}


def _report(run_dir: Path, *, stored: bool) -> list[dict]:
    """Rows for a finished run: rescored by default, the stored verdicts with --stored."""
    rows = rep.load_rows(run_dir)
    if stored:
        return rows
    from scripts.toolbench import runner
    if "service" not in sys.modules:  # --compare rescores a second run in the same process
        runner.isolate(Path(tempfile.mkdtemp(prefix="toolbench-")), omlx_port=runner.DEFAULT_OMLX_PORT)
    rows, summary = rescore(run_dir, rows)
    before = _pass_counts([{**r, "v": r.get("v_stored", r["v"])} for r in rows])
    after = _pass_counts(rows)
    print(f"rescored {summary['rescored']}/{summary['rows']} rows with the current scorer and cases "
          f"({summary['kept_stored']} kept their stored verdict, {summary['verdict_flipped']} changed):")
    for m in after:
        print(f"  {m}: pass {before.get(m, (0, 0))[0]}/{before.get(m, (0, 0))[1]} stored -> "
              f"{after[m][0]}/{after[m][1]} rescored")
    print()
    return rows


def _agent_model() -> str:
    from service.config import _packaged_config
    return str(_packaged_config()["roles"]["agent"])


async def _recover(client, model, *, wait: float = 240.0) -> None:
    """Wait for the engine to come back and the model to be resident again.

    oMLX can restart on its own mid-run (observed 2026-10-14: the server died
    during a prefill with nothing in its log and was back 5s later). Polls
    instead of failing on the first refused connection.
    """
    from service.inference.inference_errors import ModelNotDownloadedError
    deadline = time.monotonic() + wait
    last = None
    while time.monotonic() < deadline:
        try:
            await client.make_resident(model)
            return
        except ModelNotDownloadedError:
            # The engine answered: it has no such model. Waiting cannot fix that
            # (seen 2026-10-29: its model folder was renamed under a running oMLX).
            try:
                served = await client.models()
            except Exception:  # noqa: BLE001
                served = ["(inventory unavailable)"]
            raise RuntimeError(f"oMLX at {client.base_url} does not serve {model}; it serves {served}. "
                               "Rescan or restart oMLX if the files are on disk.") from None
        except Exception as e:  # noqa: BLE001 — engine down or still loading; keep waiting
            last = e
            await asyncio.sleep(3.0)
    raise RuntimeError(f"engine did not recover within {wait:.0f}s: {last}")


async def _one(runner, score, client, model, case, world, names, writes, args, run_id, trial,
               variant, host_refused=frozenset()) -> dict:
    """One trial, retried once after a transient engine error (409, restart, dropped stream)."""
    traj = await runner.run_case(client, model, case, world, timeout=args.timeout, max_steps=args.max_steps)
    if traj.get("error") and _TRANSIENT.search(traj["error"]):
        await _recover(client, model)
        traj = await runner.run_case(client, model, case, world, timeout=args.timeout,
                                     max_steps=args.max_steps)
    v = score(case, traj, registry_names=names, today=world.today, write_tools=writes,
              host_refused=host_refused)
    profile = args.profile_as if args.profile_as and args.profile_as != model else None
    return _row(run_id, model, case, trial, traj, v, variant, profile)


def _update_meta(out: Path, key: str, model: str, value) -> None:
    """meta.json[key][model] = value, kept across --resume."""
    path = out / "meta.json"
    meta = json.loads(path.read_text()) if path.exists() else {}
    meta.setdefault(key, {})[model] = value
    path.write_text(json.dumps(meta, indent=2, default=str))


async def _run(args, cases) -> int:
    from scripts.toolbench import runner
    from scripts.toolbench.world import build_world

    world = build_world(seed=args.world_seed)
    names = runner.install_fakes(world)
    # Every model sees the same pinned date and time, in its prompt and its tools.
    runner.set_clock(world.now)
    try:
        return await _trials(args, cases, runner, world, names)
    finally:
        runner.set_clock(None)
        runner.uninstall_fakes()


async def _parity(runner, client, models, profile_as, allow) -> tuple[dict, dict, bool]:
    """Engine settings of every model and of `profile_as`, and their mismatches
    on runner.PARITY_KEYS. A profiled candidate is only treated like the profile
    model fairly when oMLX samples and thinks for both the same way. The bool is
    False (stop the run) on a mismatch or unreadable settings, unless `allow`."""
    settings, mismatches, ok = {}, {}, True
    for m in dict.fromkeys([*models, *([profile_as] if profile_as else [])]):
        settings[m] = await runner.engine_settings(client, m)
    for m in models:
        if not profile_as or m == profile_as:
            continue
        unread = [x for x in (m, profile_as) if not settings[x]["source"]]
        if unread:
            mismatches[m] = {"unreadable": unread}
            print(f"engine parity: could not read oMLX settings for {unread}")
            ok = False
            continue
        diff = runner.engine_mismatches(settings[m], settings[profile_as])
        mismatches[m] = {k: list(v) for k, v in diff.items()}
        for k, (a, b) in diff.items():
            print(f"engine parity: {m} {k}={a!r} but {profile_as} {k}={b!r}")
        ok = ok and not diff
    if not ok:
        print(("--allow-engine-mismatch: continuing although " if allow else "ABORT: ")
              + "engine settings differ between a candidate and the profile model "
              "(align ~/.omlx/model_settings.json, or pass --allow-engine-mismatch)")
    return settings, mismatches, ok or allow


async def _trials(args, cases, runner, world, names) -> int:
    from scripts.toolbench.scorer import score
    from service.config.endpoints import role_target

    writes, refused = runner.write_tool_names(), runner.host_refused_tools()
    cases = [c.bind(world.facts) for c in cases]
    variants = [v.strip() for v in args.thinking.split(",") if v.strip()]
    if bad := [v for v in variants if v not in runner.THINKING_MODES]:
        print(f"unknown --thinking {bad}; choose from {runner.THINKING_MODES}")
        return 2
    Client = runner.bench_client_class()
    client = Client(timeout=args.timeout)       # inventory and residency; turns use a per-model client
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [_agent_model()]
    if len(models) > 1:
        # A comparison treats every candidate the same way: as the packaged agent
        # model (its window, list membership), each alone on the engine.
        args.profile_as = args.profile_as or _agent_model()
        if not args.shared:
            args.exclusive = True
    installed = await client.models()
    if args.profile_as and args.profile_as not in installed:
        print(f"note: --profile-as {args.profile_as} is not installed; its per-model treatment "
              "still applies, it just cannot run itself")
    missing = [m for m in models if m not in installed]
    if missing:
        print(f"not installed in oMLX: {missing}")
        close = [i for i in installed if any(m.lower()[:10] in i.lower() for m in missing)]
        if close:
            print(f"  close matches: {close}")
        return 2
    done: set[tuple] = set()
    if args.resume:
        out = Path(args.resume).expanduser()
        prior = rep.load_rows(out)
        run_id = prior[0]["run"] if prior else out.name
        done = {(r.get("base_model", r["model"]), r.get("variant", "default"), r["case"], r["trial"])
                for r in prior}
        print(f"resuming {out}: {len(done)} trials already recorded")
    else:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path(args.out).expanduser() if args.out else RESULTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    settings, parity, go = await _parity(runner, client, models, args.profile_as or None,
                                         args.allow_engine_mismatch)
    seeds = {t: args.seed_base + t for t in range(1, args.trials + 1)}
    meta = {"run": run_id, "git": _git_sha(), "models": models, "trials": args.trials,
            "cases": [c.id for c in cases], "started": time.time(), "exclusive": args.exclusive,
            "today": world.today.isoformat(), "max_steps": args.max_steps,
            "world_seed": args.world_seed, "thinking": variants, "split": args.split or "all",
            "path": "model_led", "profile_as": args.profile_as or None, "omlx_port": args.omlx_port,
            "cleared_env": sorted(runner.CLEARED_ENV),
            "access": dict(sorted(Counter(c.access for c in cases).items())),
            "now": world.now.isoformat(), "seed_base": args.seed_base, "seeds": seeds,
            "engine_settings": settings, "engine_parity": parity,
            "allow_engine_mismatch": args.allow_engine_mismatch}
    if not args.resume:
        (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    if not go:
        return 4
    rows: list[dict] = rep.load_rows(out) if args.resume else []
    started_loaded = await client.loaded_models()
    try:
        with (out / "results.jsonl").open("a") as fh:
            for model in models:
                if args.exclusive:
                    for other in await client.loaded_models():
                        if other != model:
                            print(f"--exclusive: unloading {other}")
                            await client.unload(other)
                binding = runner.bind_model(model, profile_as=args.profile_as or None)
                turn_client = Client(timeout=args.timeout, target=role_target("agent"))
                print(f"\n== {model}: loading…  window={binding['context_window']}"
                      + (f"  profiled as {binding['profile_as']}" if binding["profile_as"] else ""))
                await _recover(turn_client, model)
                _update_meta(out, "bindings", model, binding)
                for variant in variants:
                    flags = runner.set_thinking(turn_client, model, variant)
                    _update_meta(out, "thinking_flags", f"{model} +{variant}", flags)
                    print(f"-- thinking: {variant}  narration={flags['narration_after_tool_thinks']} "
                          f"direct={flags['direct_answer_thinks']}")
                    total, k = len(cases) * args.trials, 0
                    for trial in range(1, args.trials + 1):
                        for case in cases:
                            k += 1
                            if (model, variant, case.id, trial) in done:
                                continue
                            if not _swap_ok():
                                print("STOP: memory pressure (free swap < 500MB and pressure high)")
                                return 3
                            turn_client.seed = seeds[trial]   # same schedule for every model
                            row = await _one(runner, score, turn_client, model, case, world, names,
                                             writes, args, run_id, trial, variant, refused)
                            rows.append(row)
                            fh.write(json.dumps(row, default=str) + "\n")
                            fh.flush()
                            v = row["v"]
                            mark = ("EXCLUDED engine_error" if v.get("excluded") else
                                    "PASS" if v["passed"] else "FAIL " + ",".join(v["labels"]))
                            if v.get("guard_retries"):
                                mark += f"  guard400x{v['guard_retries']}"
                            calls = [c["name"] for s in row["steps"] for c in s.get("calls", ())]
                            print(f"[{k}/{total}] {case.id:<28} t{trial} {v['seconds']:>6.1f}s "
                                  f"(model {v['model_seconds']:.1f}s, {v['steps']} steps)  {mark}  {calls}")
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=DEFAULT_MODEL,
                    help="comma-separated oMLX model ids (default: the packaged agent model)")
    ap.add_argument("--profile-as", default="",
                    help="give every other --models entry exactly the per-model treatment production "
                         "gives this rostered model id (models.yaml lists, context window)")
    ap.add_argument("--omlx-port", type=int, default=0,
                    help="oMLX port on 127.0.0.1 (default 8000); the only port the network guard allows")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--only", default="", help="categories (case file stems), comma-separated")
    ap.add_argument("--ids", default="", help="case ids, comma-separated")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-turn seconds")
    ap.add_argument("--max-steps", type=int, default=8, help="agent loop step ceiling (production: 8)")
    ap.add_argument("--exclusive", action="store_true",
                    help="unload every other model while a model runs; restore them afterwards "
                         "(the default when more than one model runs)")
    ap.add_argument("--shared", action="store_true",
                    help="with several --models: do not unload the others (unfair: they contend)")
    ap.add_argument("--allow-engine-mismatch", action="store_true",
                    help="run even when a candidate's oMLX settings differ from --profile-as's on "
                         "sampling, thinking, window or MTP (recorded in meta.json either way)")
    ap.add_argument("--allow-wisp-running", action="store_true",
                    help=f"run even while something listens on {WISP_PORTS} (the live Wisp)")
    ap.add_argument("--seed-base", type=int, default=1000,
                    help="trial t sends seed seed_base+t, the same for every model")
    ap.add_argument("--out", default="", help="results directory (default ~/.moe/evals/toolbench/<run>)")
    ap.add_argument("--split", default="", choices=["", "gate", "dev", "voice"], help="only this split")
    ap.add_argument("--world-seed", type=int, default=None,
                    help="variant world (default: the canonical gate world)")
    ap.add_argument("--thinking", default="default",
                    help="comma-separated variants: default, narration-on, narration-off, off "
                         "(see runner.set_thinking)")
    ap.add_argument("--export-shingles", default="",
                    help="write hashed n-grams of every held-out (gate, voice) prompt (canonical + 50 "
                         "seeded worlds) and exit")
    ap.add_argument("--resume", default="", help="continue an interrupted run directory in place "
                    "(same --models/--thinking/--trials/--split); finished trials are skipped")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline: bind every case and check tool names and host preparation; no model, "
                         "no routing test")
    ap.add_argument("--report", default="", help="re-render a finished run directory")
    ap.add_argument("--compare", default="", help="with --report: baseline run directory")
    ap.add_argument("--stored", action="store_true",
                    help="with --report: show the verdicts stored when the run finished instead of "
                         "rescoring its trajectories with the current scorer and cases")
    args = ap.parse_args()

    if args.report:
        cur = _report(Path(args.report).expanduser(), stored=args.stored)
        print(rep.render(cur))
        if args.compare:
            print("\n" + rep.compare(_report(Path(args.compare).expanduser(), stored=args.stored), cur))
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

    if not args.dry_run and not args.allow_wisp_running:
        if busy := [p for p in WISP_PORTS if _listening(p)]:
            print(f"refusing to start: something listens on {busy} (the live Wisp or its engine). "
                  "Quit Wisp, or pass --allow-wisp-running.")
            return 5
    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp(prefix="toolbench-")),
                   omlx_port=args.omlx_port or runner.DEFAULT_OMLX_PORT)
    args.omlx_port = args.omlx_port or runner.DEFAULT_OMLX_PORT
    if args.dry_run:
        return _dry_run(args, cases)
    return asyncio.run(_run(args, cases))


if __name__ == "__main__":
    raise SystemExit(main())
