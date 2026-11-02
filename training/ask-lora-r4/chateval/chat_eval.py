#!/usr/bin/env python3
"""Run models through Wisp's real Ask path on the r4 chat prompts and record every conversation in full.

    chat_eval.py --dry-run                                      # validate prompts_r4.yaml, print the plan, no model
    chat_eval.py --repo <wisp checkout> --models A,B --profile-as A --samples 3 --out <dir>

It is the chat stage gate.sh runs when CHATEVAL points at this file (gate.sh passes exactly these flags).

HOW A TURN RUNS
  The same way scripts/run_toolbench.py runs a case, because that is Wisp's model-led Ask turn
  (scripts/toolbench/runner.py documents the fidelity): scripts.toolbench.runner.isolate() first (throwaway
  WISP_HOME, outbound network and subprocesses refused except loopback to oMLX), every registered tool replaced by
  a fake over the toolbench's synthetic world (nothing real is read, written or sent), the clock pinned to the
  world's 08:30 morning, the model bound as the agent role (bind_model, with --profile-as giving the candidate the
  baseline's per-model treatment), then prepare_turn + run_agent for every user message. No temperature is sent, so
  oMLX's per-model profile applies as in production; trial t of every model gets seed --seed-base + t.

  A conversation is played turn by turn in one conversation: the history of turn k is the user messages AND THE
  MODEL'S OWN replies to turns 0..k-1 (runner.run_case seeds a real session from it), so each model is judged on
  the conversation it actually had. If a reply never comes (engine error, timeout, empty) the conversation stops
  there: the missing turns count as failures in the checks and show as "(no reply)" to the judge.
  Not kept in the history: the tool calls a past turn made (production stores a digest of them).

OUTPUT  <out>/replies.jsonl, one row per model turn, plus <out>/meta.json
  run, model, unit, kind, category, skill, sample, seed, variant, turn, n_turns, user, history_turns, reply,
  conversation (the FULL transcript up to and including this reply: [{role, text}, ...]), turn_seconds, error,
  timeout, escapes, metrics{tool_calls, n_steps, ttft_s, reasoning_chars, answer_chars}, steps[]
  The r2/r3 chat eval rows have the same keys (and none of skill/conversation).

NEEDS  Wisp quit, oMLX running, the heavy lock held (gate.sh does all three). Never run it by hand while Wisp is up:
  it refuses to start when :8765 or :8791 listens, unless --allow-wisp-running.
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chatlib  # noqa: E402

DEFAULT_REPO = HERE.parents[2]          # training/ask-lora-r4/chateval -> repository root
SEED_BASE = 1000
DEFAULT_TODAY = "2026-10-30"            # the clock the r2/r3 chat eval was pinned to (a Friday, 08:30)
WISP_PORTS = (8765, 8791)


# ---------------------------------------------------------------------------------------- rows
def _compact_steps(steps) -> list[dict]:
    return [{"seconds": s.get("seconds"), "ttft_s": s.get("ttft"), "reasoning_chars": s.get("reasoning_chars"),
             "answer_chars": len(s.get("content") or ""), "calls": [c.get("name") for c in s.get("calls") or ()],
             "error": s.get("error"), "guard_400": s.get("guard_400")} for s in steps or ()]


def make_row(*, run, model, unit, variant, sample, seed, turn, turns, history, traj) -> dict:
    """One replies.jsonl row from a finished turn (`traj` is runner.run_case's trajectory)."""
    reply = str(traj.get("final") or "").strip()
    steps = _compact_steps(traj.get("steps"))
    calls = [n for s in steps for n in s["calls"]]
    user = turns[turn]
    conversation = [m for h in history for m in ({"role": "user", "text": h["user"]},
                                                  {"role": "assistant", "text": h["assistant"]})]
    conversation += [{"role": "user", "text": user}, {"role": "assistant", "text": reply}]
    row = {"run": run, "model": model, "unit": unit["id"], "kind": unit["kind"], "category": unit["category"],
           "sample": sample, "seed": seed, "variant": variant, "turn": turn, "n_turns": len(turns),
           "user": user, "history_turns": len(history), "reply": reply, "conversation": conversation,
           "turn_seconds": traj.get("seconds"), "error": traj.get("error"), "timeout": bool(traj.get("timeout")),
           "escapes": list(traj.get("escapes") or ()),
           "metrics": {"tool_calls": calls, "n_steps": len(steps),
                       "ttft_s": next((s["ttft_s"] for s in steps if s["ttft_s"] is not None), None),
                       "reasoning_chars": sum(s["reasoning_chars"] or 0 for s in steps),
                       "answer_chars": len(reply)},
           "steps": steps}
    if unit.get("skill"):
        row["skill"] = unit["skill"]
    return row


def row_key(r: dict) -> tuple:
    return (r["model"], r["unit"], r["sample"], r["variant"], r["turn"])


async def play_unit(unit: dict, *, model: str, run_id: str, sample: int, seed: int, run_turn,
                    done: dict | None = None, emit=None) -> list[dict]:
    """Play every session of one unit for one model and sample; return its rows in order.

    `run_turn(case_id, prompt, history) -> traj` runs one turn (`history` = [{user, assistant}] of the session so
    far). Rows already in `done` (keyed by row_key, from --resume) are reused, not rerun. `emit(row)` is called
    for each NEW row. A session stops at its first turn with no reply."""
    rows: list[dict] = []
    for variant, turns in chatlib.unit_runs(unit):
        history: list[dict] = []
        for t, user in enumerate(turns):
            key = (model, unit["id"], sample, variant, t)
            if done and key in done:
                row = done[key]
            else:
                traj = await run_turn(f"{unit['id']}.s{sample}.v{variant}.t{t}", user, [dict(h) for h in history])
                row = make_row(run=run_id, model=model, unit=unit, variant=variant, sample=sample, seed=seed,
                               turn=t, turns=turns, history=history, traj=traj)
                if emit:
                    emit(row)
            rows.append(row)
            if chatlib.non_reply(row["reply"], row.get("error"), row.get("timeout")) in ("timeout", "error", "empty"):
                break
            history.append({"user": user, "assistant": row["reply"]})
    return rows


# ---------------------------------------------------------------------------------------- CLI
def select_units(units: list[dict], only: str) -> list[dict]:
    pats = [p.strip() for p in only.split(",") if p.strip()]
    if not pats:
        return units
    return [u for u in units if any(fnmatch.fnmatch(u["id"], p) for p in pats)]


def plan_text(units: list[dict], samples: int, models: list[str]) -> str:
    kinds = Counter(u["kind"] for u in units)
    skills = Counter(u.get("skill") for u in units if u["kind"] == "conversation" and u.get("skill"))
    per_sample = chatlib.model_turns(units)
    lines = [f"{len(units)} units: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())),
             "conversations by skill: " + (", ".join(f"{k} {n}" for k, n in sorted(skills.items())) or "none tagged"),
             f"{per_sample} model turns per sample; {samples} samples x {len(models) or 1} model(s) = "
             f"{per_sample * samples * max(1, len(models))} turns"]
    return "\n".join(lines)


def _git_sha(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def _listening(port: int) -> bool:
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="the Wisp checkout whose service/ is exercised")
    ap.add_argument("--prompts", default=str(chatlib.DEFAULT_PROMPTS))
    ap.add_argument("--models", default="", help="comma-separated oMLX model ids; the first is the baseline")
    ap.add_argument("--profile-as", default="", help="give every other model exactly this rostered model's per-model "
                    "treatment (models.yaml lists, context window); gate.sh passes the baseline")
    ap.add_argument("--samples", type=int, default=3, help="runs of every unit; sample s of every model uses seed "
                    "--seed-base + s")
    ap.add_argument("--seed-base", type=int, default=SEED_BASE)
    ap.add_argument("--out", default="", help="results directory (default: ~/.moe/evals/chateval/<timestamp>)")
    ap.add_argument("--only", default="", help="unit ids or shell wildcards, comma-separated (e.g. 'mt_*')")
    ap.add_argument("--today", default=DEFAULT_TODAY, help="the pinned date of the synthetic world")
    ap.add_argument("--world-seed", type=int, default=None)
    ap.add_argument("--omlx-port", type=int, default=0, help="default 8000")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-turn seconds")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--access", default="full", choices=["full", "default", "view"])
    ap.add_argument("--shared", action="store_true", help="do not unload the other models (unfair: they contend)")
    ap.add_argument("--allow-engine-mismatch", action="store_true")
    ap.add_argument("--allow-wisp-running", action="store_true")
    ap.add_argument("--resume", action="store_true", help="reuse the rows already in --out/replies.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="validate the prompts file, print the plan, run nothing")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    units = chatlib.load_prompts(args.prompts)
    errors = chatlib.validate_units(units)
    if errors:
        print(f"{args.prompts}: {len(errors)} problem(s)")
        for e in errors:
            print("  -", e)
        return 2
    units = select_units(units, args.only)
    if not units:
        print(f"--only {args.only!r} matches no unit")
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(plan_text(units, args.samples, models))
    if args.dry_run:
        print("dry run: prompts file valid; nothing was run")
        return 0
    if not models:
        print("--models is required")
        return 2
    if args.samples < 1:
        print("--samples must be >= 1")
        return 2
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / "service").is_dir() or not (repo / "scripts" / "toolbench" / "runner.py").is_file():
        print(f"--repo {repo} is not a Wisp checkout (needs service/ and scripts/toolbench/runner.py)")
        return 2
    if not args.allow_wisp_running and (busy := [p for p in WISP_PORTS if _listening(p)]):
        print(f"refusing to start: something listens on {busy} (the live Wisp or its engine). Quit Wisp first.")
        return 5
    out = Path(args.out).expanduser() if args.out else Path.home() / ".moe" / "evals" / "chateval" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    if (out / "replies.jsonl").exists() and (out / "replies.jsonl").stat().st_size and not args.resume:
        print(f"{out}/replies.jsonl exists: pass --resume to continue it, or choose another --out")
        return 2
    sys.path.insert(0, str(repo))
    from scripts import run_toolbench as RT          # no service import at module level
    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp(prefix="chateval-")), omlx_port=args.omlx_port or runner.DEFAULT_OMLX_PORT)
    args.omlx_port = args.omlx_port or runner.DEFAULT_OMLX_PORT
    return asyncio.run(_run(args, units, models, out, repo, runner, RT))


async def _run(args, units, models, out: Path, repo: Path, runner, RT) -> int:
    from scripts.toolbench.world import build_world
    world = build_world(today=date.fromisoformat(args.today), seed=args.world_seed)
    runner.install_fakes(world)
    runner.set_clock(world.now)            # every model sees the same date and 08:30, in its prompt and its tools
    try:
        return await _models(args, units, models, out, repo, runner, RT, world)
    finally:
        runner.set_clock(None)
        runner.uninstall_fakes()


async def _models(args, units, models, out: Path, repo: Path, runner, RT, world) -> int:
    from service.config.endpoints import role_target
    Client = runner.bench_client_class()
    client = Client(timeout=args.timeout)
    exclusive = len(models) > 1 and not args.shared
    profile_as = args.profile_as or (models[0] if len(models) > 1 else "")
    installed = await client.models()
    if missing := [m for m in models if m not in installed]:
        print(f"not installed in oMLX: {missing}")
        return 2
    settings, parity, go = await RT._parity(runner, client, models, profile_as or None, args.allow_engine_mismatch)
    prompts_sha = hashlib.sha1(Path(args.prompts).read_bytes()).hexdigest()[:12]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    done: dict = {}
    replies = out / "replies.jsonl"
    if args.resume and replies.exists():
        for line in replies.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[row_key(r)] = r
        run_id = next(iter(done.values()))["run"] if done else run_id
        print(f"resuming: {len(done)} turns already recorded")
    seeds = {s: args.seed_base + s for s in range(1, args.samples + 1)}
    meta = {"run": run_id, "kind": "chat_eval_r4", "git": _git_sha(repo), "models": models, "samples": args.samples,
            "units": [u["id"] for u in units], "prompts": str(args.prompts), "prompts_sha1": prompts_sha,
            "today": args.today, "now": world.now.isoformat(), "world_seed": args.world_seed, "seeds": seeds,
            "profile_as": profile_as or None, "access": args.access, "exclusive": exclusive,
            "turns_per_sample": chatlib.model_turns(units), "engine_settings": settings, "engine_parity": parity,
            "started": time.time()}
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    if not go:
        return 4
    started_loaded = await client.loaded_models()
    n_rows = n_bad = 0
    try:
        with replies.open("a") as fh:
            for model in models:
                if exclusive:
                    for other in await client.loaded_models():
                        if other != model:
                            print(f"--exclusive: unloading {other}")
                            await client.unload(other)
                binding = runner.bind_model(model, profile_as=profile_as or None)
                tc = Client(timeout=args.timeout, target=role_target("agent"))
                print(f"\n== {model}: loading...  window={binding['context_window']}"
                      + (f"  profiled as {binding['profile_as']}" if binding["profile_as"] else ""))
                await RT._recover(tc, model)

                async def run_turn(case_id, prompt, history, _tc=tc, _model=model):
                    case = SimpleNamespace(id=case_id, prompt=prompt, history=history, access=args.access,
                                           access_needed=())
                    traj = await runner.run_case(_tc, _model, case, world, timeout=args.timeout, max_steps=args.max_steps)
                    if traj.get("error") and RT._TRANSIENT.search(traj["error"]):
                        await RT._recover(_tc, _model)          # engine restart, dropped stream, 409: retry once
                        traj = await runner.run_case(_tc, _model, case, world, timeout=args.timeout, max_steps=args.max_steps)
                    return traj

                def emit(row, _fh=fh):
                    nonlocal n_rows, n_bad
                    _fh.write(json.dumps(row, default=str) + "\n")
                    _fh.flush()
                    n_rows += 1
                    why = chatlib.non_reply(row["reply"], row["error"], row["timeout"])
                    n_bad += bool(why)
                    print(f"  {row['unit']:<24} s{row['sample']} v{row['variant']} t{row['turn']} "
                          f"{row['turn_seconds'] or 0:>6.1f}s  {len(row['reply']):>5} chars"
                          + (f"  [{why}]" if why else "") + (f"  tools={row['metrics']['tool_calls']}" if row["metrics"]["tool_calls"] else ""))

                for sample in range(1, args.samples + 1):
                    tc.seed = seeds[sample]                      # same schedule for every model
                    for unit in units:
                        if not RT._swap_ok():
                            print("STOP: memory pressure (free swap < 500MB and pressure high)")
                            return 3
                        await play_unit(unit, model=model, run_id=run_id, sample=sample, seed=seeds[sample],
                                        run_turn=run_turn, done=done, emit=emit)
    finally:
        if exclusive:
            for m in started_loaded:
                if m not in await client.loaded_models():
                    print(f"--exclusive: restoring {m}")
                    try:
                        await client.make_resident(m)
                    except Exception as e:  # noqa: BLE001
                        print(f"  could not restore {m}: {e}")
    print(f"\n{n_rows} new turns, {n_bad} with no real reply.\nresults: {out}/replies.jsonl")
    print(f"next: {HERE}/judge_pairs.py build --replies {out}/replies.jsonl --out {out}/judge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
