"""Offline self-check of the private eval: every case is passable, and every recorded failure fails.

No model, no network, no engine. For each case in $PRIVATE_EVAL_DIR/ideal.yaml the real agent loop runs against
the private world's fakes with a ScriptedClient (runner.scripted_client_class) that replays the plan:

    ideal   the trajectory a correct model would produce      -> the scorer must PASS it
    fail    recorded bad trajectories (what r3 actually did,  -> the scorer must NOT pass each one, and,
            a draft with no read, a no-call answer ...)          when `labels_any` is given, must label it so

so a case that no model can pass (a tool the host withholds, an answer check the world cannot satisfy) and a
case that the failure it was written for would pass are both caught before any model is loaded.

    PRIVATE_EVAL_DIR=~/.moe/evals/private-real-use .venv/bin/python -m scripts.toolbench.private_selfcheck

Exit status 0 when every case behaves; 1 otherwise (a host-blocked case counts as not behaving until the host
is fixed; `--assume-host-fix` shows it passing with the fix). The report names case ids and labels only, never the
user's text.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

GEMMA = "gemma-4-E4B-it-oQ4e-mtp"
CANDIDATE = "toolbench-selfcheck-E4B"


def _plan(entries) -> list[dict]:
    """ideal.yaml plan entries -> ScriptedClient plan: calls become (name, args) tuples."""
    out = []
    for e in entries or ():
        e = dict(e)
        if "calls" in e:
            e["calls"] = [(str(n), dict(a or {})) for n, a in e["calls"]]
        out.append(e)
    return out


# The host fix the repo's own xfail names (tests/test_toolbench.py, "responding to" cases). Two patterns in
# service/model_only.py miss the -ing form: _STORED_CONTENT (the reply/respond/answer-to read keeper) and
# _DESTINATION (whose `respond` matches \brespond\b only), so "draft a message responding to <name>" is both
# pure writing (reads withheld) and compose-only (draft_message withheld too).
_FIX_STORED = r"|\b(?:repl(?:y|ies|ying)|respon(?:d|ds|ding|se))\s+to\b"
_FIX_DESTINATION = r"|(?i:\b(?:repl(?:y|ies|ying)|respon(?:d|ds|ding|se))\b)"


def _assume_host_fix():
    """Extend model_only._STORED_CONTENT and _DESTINATION as the pending fix would; returns an undo callable."""
    import re

    import service.model_only as mo
    stored, dest = mo._STORED_CONTENT, mo._DESTINATION
    mo._STORED_CONTENT = re.compile(stored.pattern + _FIX_STORED, stored.flags)
    mo._DESTINATION = re.compile(dest.pattern + _FIX_DESTINATION, dest.flags)

    def undo():
        mo._STORED_CONTENT, mo._DESTINATION = stored, dest
    return undo


def run_selfcheck(directory: Path, *, only: set[str] | None = None, assume_host_fix: bool = False) -> list[dict]:
    """Results per case: {"id", "ideal": verdict dict, "fails": [{"name", "passed", "labels", "ok"}], "ok",
    "host_blocked"}.

    A case whose scripted ideal cannot run because the HOST withholds the tool it needs (a route_fault on a
    prompt service.model_only.pure_writing treats as pure writing) is `host_blocked`, not a defect of the case:
    it is reported apart and still counts as unmet. `assume_host_fix` applies the pending host fix
    for the run, proving such a case is passable once the host is fixed.

    Needs the isolated service import (runner.isolate) and engine windows for GEMMA and CANDIDATE
    (main() sets both; tests/test_toolbench_private.py reuses the toolbench fixtures).
    """
    from scripts.toolbench import private, runner
    from scripts.toolbench.scorer import score
    from service.config.endpoints import role_target

    world = private.private_world(directory)
    cases = {c.id: c.bind(world.facts) for c in private.load_private_cases(directory)}
    plans = private.load_plans(directory)
    if extra := sorted(set(plans) - set(cases)):
        raise private.PrivateEvalError(f"ideal.yaml names unknown cases {extra}")
    if missing := sorted(set(cases) - set(plans)):
        raise private.PrivateEvalError(f"cases without a scripted ideal: {missing}")

    names = runner.install_fakes(world)
    runner.set_clock(world.now)
    results = []
    undo = _assume_host_fix() if assume_host_fix else (lambda: None)
    try:
        writes, refused = runner.write_tool_names(), runner.host_refused_tools()
        runner.bind_model(CANDIDATE, profile_as=GEMMA)
        client = runner.scripted_client_class()(api_key="toolbench-selfcheck", target=role_target("agent"))

        def verdict(case, entries):
            client.plan = _plan(entries)
            client.failure = ""
            active = private.activate_case_world(runner, case.id, world, directory)
            traj = asyncio.run(runner.run_case(client, CANDIDATE, case, active, timeout=60))
            v = score(case, traj, registry_names=names, today=active.today, write_tools=writes,
                      host_refused=refused)
            return v, traj

        for cid, case in cases.items():
            if only and cid not in only:
                continue
            from service.model_only import pure_writing
            spec = plans[cid]
            v, traj = verdict(case, spec["ideal"])
            row = {"id": cid, "ideal": {"passed": v.passed, "labels": v.labels,
                                        "error": traj.get("error") or client.failure},
                   "fails": [], "host_blocked": False}
            ok = v.passed and not traj.get("error")
            if not ok and "route_fault" in v.labels and pure_writing(case.prompt):
                row["host_blocked"] = True      # winnable for no model until service/model_only.py is fixed
                row["ok"] = False
                results.append(row)
                continue
            for fail in spec.get("fail") or ():
                fv, ftraj = verdict(case, fail["plan"])
                want = set(fail.get("labels_any") or ())
                fok = (not fv.passed) and (not want or bool(want & set(fv.labels)))
                row["fails"].append({"name": str(fail.get("name", "?")), "passed": fv.passed, "labels": fv.labels,
                                     "ok": fok, "error": ftraj.get("error") or ""})
                ok = ok and fok
            row["ok"] = ok
            results.append(row)
    finally:
        undo()
        runner.bind_model(CANDIDATE)
        runner._restore_aliases()
        runner.set_clock(None)
        runner.uninstall_fakes()
    return results


def render(results: list[dict]) -> str:
    lines = []
    for r in results:
        mark = "ok  " if r["ok"] else ("HOST" if r.get("host_blocked") else "FAIL")
        lines.append(f"{mark} {r['id']:<32} ideal={'pass' if r['ideal']['passed'] else 'FAIL ' + ','.join(r['ideal']['labels'])}"
                     + (f" ({r['ideal']['error']})" if r["ideal"]["error"] else ""))
        for f in r["fails"]:
            lines.append(f"       failure {f['name']!r:<28} -> {'fails as it should' if f['ok'] else 'WRONG'}"
                         f" labels={','.join(f['labels']) or '-'}{' PASSED' if f['passed'] else ''}")
    good = sum(r["ok"] for r in results)
    n_fail = sum(len(r["fails"]) for r in results)
    blocked = [r["id"] for r in results if r.get("host_blocked")]
    lines.append(f"\n{good}/{len(results)} cases behave; {n_fail} recorded failures checked"
                 + (f"; {len(blocked)} blocked by the host (service/model_only.py pure_writing withholds "
                    f"their reads): {', '.join(blocked)}" if blocked else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import socket

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", default="", help="case ids, comma-separated")
    ap.add_argument("--assume-host-fix", action="store_true",
                    help="apply the pending service/model_only.py fix (_STORED_CONTENT and _DESTINATION learn reply/respond -ing) "
                         "for this run, to prove the host-blocked cases are passable once it lands")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from scripts.toolbench import private
    try:
        directory = private.private_dir()
        private.load_private_cases(directory)
    except private.PrivateEvalError as e:
        print(f"refusing to run: {e}")
        return 2

    from scripts.toolbench import runner
    runner.isolate(Path(tempfile.mkdtemp(prefix="toolbench-selfcheck-")))
    for fn in ("connect", "connect_ex"):    # nothing leaves this process: not even the engine's port
        def refuse(sock, address, _fn=fn):
            raise PermissionError(f"selfcheck: network to {address!r} is blocked")
        setattr(socket.socket, fn, refuse)
    from service import config
    settings = Path(tempfile.mkdtemp(prefix="toolbench-selfcheck-")) / "model_settings.json"
    settings.write_text(json.dumps({"models": {GEMMA: {"max_context_window": 16384},
                                               CANDIDATE: {"max_context_window": 9000}}}))
    config.OMLX_MODEL_SETTINGS = settings
    results = run_selfcheck(directory, only={i for i in args.ids.split(",") if i},
                            assume_host_fix=args.assume_host_fix)
    print(render(results))
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
