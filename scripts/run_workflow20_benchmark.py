#!/usr/bin/env python3
"""Validate corpus, diagnose actual offline routing, or grade supplied observations.

No installed application, model inference, personal readers or effect tools run.
Routing is a tool-availability diagnostic; it is never answer accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import datetime as calendar_clock
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.workflow20.contract import aggregate, grade, load_corpus, subset


def forbidden(*args, **kwargs):
    raise AssertionError("Workflow20 offline boundary: network/process/credential/effect forbidden")


@contextmanager
def scenario_context(scenario):
    """Freeze classification inputs; source readers and actions remain forbidden."""
    instant = calendar_clock.datetime.fromisoformat(scenario["clock"])
    stamp = instant.timestamp()
    zone = ZoneInfo(scenario["timezone"])
    real_datetime, real_date = calendar_clock.datetime, calendar_clock.date
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(stamp, tz) if tz else cls.fromtimestamp(stamp, zone).replace(tzinfo=None)
        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(stamp, calendar_clock.timezone.utc).replace(tzinfo=None)
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return FrozenDateTime.now().date()
    previous_tz = os.environ.get("TZ")
    contacts = scenario["sources"].get("contacts", {})
    names = [row["name"] for row in contacts.get("records", [])] if contacts.get("state") == "ready" else []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"TZ": scenario["timezone"]}))
        time.tzset()
        stack.enter_context(patch("time.time", lambda: stamp))
        # Cover existing aliases and late function-local imports equally.
        for name, module in list(sys.modules.items()):
            if module is not None and name.startswith("service."):
                for attr, value in list(vars(module).items()):
                    if value is real_datetime:
                        stack.enter_context(patch.object(module, attr, FrozenDateTime))
                    elif value is real_date:
                        stack.enter_context(patch.object(module, attr, FrozenDate))
        stack.enter_context(patch.object(calendar_clock, "datetime", FrozenDateTime))
        stack.enter_context(patch.object(calendar_clock, "date", FrozenDate))
        stack.enter_context(patch("service.tools.imessage_tools.contact_names", lambda: names))
        try:
            yield
        finally:
            # tzset must run after patch.dict restores the environment.
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()


async def diagnose_routes(corpus):
    if "service.paths" in sys.modules:
        raise RuntimeError("Run in a fresh process; Wisp state imported before isolation")
    rows = []
    with tempfile.TemporaryDirectory(prefix="wisp-workflow20-") as scratch, ExitStack() as stack:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WISP_", "WISPAIR_"))}
        env.update(WISP_HOME=scratch, WISPAIR_HOME=str(Path(scratch) / "air"), PYTHONDONTWRITEBYTECODE="1")
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("socket.socket.connect", forbidden))
        stack.enter_context(patch("socket.socket.connect_ex", forbidden))
        stack.enter_context(patch("socket.socket.sendto", forbidden))
        stack.enter_context(patch("subprocess.Popen", forbidden))
        from service import config
        from service.config import credentials, endpoints
        stack.enter_context(patch.object(config, "OMLX_SETTINGS", Path(scratch) / "absent-settings.json"))
        stack.enter_context(patch.object(config, "OMLX_MODEL_SETTINGS", Path(scratch) / "absent-models.json"))
        stack.enter_context(patch.object(config, "omlx_api_key", forbidden))
        stack.enter_context(patch.object(config, "_credential", forbidden))
        stack.enter_context(patch.object(credentials, "resolve", forbidden))
        stack.enter_context(patch.object(endpoints.Endpoint, "api_key", forbidden))
        import service.tools
        from service.tools.registry import REGISTRY, routable_tool_names
        stack.enter_context(patch.dict(REGISTRY, {name: replace(tool, func=forbidden) for name, tool in REGISTRY.items()}))
        from service.router import router
        from service.workflows.reads import compile_read
        if (config.models_config().get("tool_retrieval") or {}).get("provider") != "lexical":
            raise RuntimeError("Offline route diagnostic requires packaged lexical retrieval")
        for case in corpus["cases"]:
            history = case["turns"][:-1]
            users = [t["text"] for t in history if t["role"] == "user"]
            assistants = [t["text"] for t in history if t["role"] == "assistant"]
            started = time.perf_counter()
            try:
                scenario = corpus["scenarios"][case["scenario"]]
                with scenario_context(scenario):
                    compiled = compile_read(case["turns"][-1]["text"], last_user=users[-1] if users else "")
                    decision = None if compiled is not None else await router.route(
                        case["turns"][-1]["text"], last_user=users[-1] if users else None,
                        recent_users=users, last_assistant=assistants[-1] if assistants else None)
                elapsed = (time.perf_counter() - started) * 1000
                offered = (set(REGISTRY) if decision.tool_subset is None else set(decision.tool_subset)) if decision else set()
                offered &= routable_tool_names()
                if decision:
                    offered -= set(decision.forbidden_tools)
                direct = [{"name": name, "args": args} for name, args in
                          (compiled[0] if compiled is not None else decision.direct_calls)]
                names = offered | {call["name"] for call in direct}
                expect = case["expect"]
                missing = [group for group in expect["required_tool_groups"] if not names & set(group)]
                bound = [{"name": name, "args": args} for name, args in decision.tool_argument_bindings.items()] if decision else []
                missing_args = [name for name, args in expect["arguments"].items()
                                if not any(c["name"] == name and subset(args, c["args"]) for c in direct + bound)]
                forbidden_direct = sorted({c["name"] for c in direct} & set(expect["forbidden_tools"]))
                rows.append({"case_id": case["id"], "family": case["family"], "route_ms": elapsed,
                             "diagnostic_pass": (not missing and not forbidden_direct) if expect["required_tool_groups"] else None,
                             "positive_evidence_evaluated": bool(expect["required_tool_groups"]),
                             "tool_groups_prescribed": bool(expect["required_tool_groups"]),
                             "entrypoint": "compile_read" if compiled is not None else "router.route",
                             "context": {"clock": scenario["clock"], "timezone": scenario["timezone"],
                                         "contacts": "synthetic", "prior_tool_history": "NOT_EXECUTED"},
                             "offered_tools": sorted(offered), "direct_calls": direct,
                             "missing_groups": missing, "unbound_expected_arguments": missing_args,
                             "argument_completion": "NOT_RUN: model parameters are not measured",
                             "forbidden_offered_tools": sorted(offered & set(expect["forbidden_tools"])),
                             "forbidden_direct_calls": forbidden_direct, "decision": decision.as_dict() if decision else None,
                             "semantic_outcome": "NOT_RUN"})
            except Exception as exc:
                rows.append({"case_id": case["id"], "family": case["family"], "diagnostic_pass": False,
                             "error": f"{type(exc).__name__}: {exc}", "semantic_outcome": "NOT_RUN"})
    return rows


def index_observations(observations, known):
    """Validate trace identities before handing payloads to the fail-closed grader."""
    if not isinstance(observations, list):
        raise ValueError("observations must be a JSON list")
    indexed = {}
    for observation in observations:
        if not isinstance(observation, dict) or not isinstance(observation.get("case_id"), str):
            raise ValueError("observation requires a string case_id")
        cid = observation["case_id"]
        if cid in indexed or cid not in known:
            raise ValueError(f"duplicate/unknown observation: {cid}")
        indexed[cid] = observation
    return indexed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("validate", "routes", "grade"), default="validate")
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/workflow20/cases")
    parser.add_argument("--observations", type=Path, help="JSON list of adapter observations; grade mode only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus(args.cases)
    sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True)
    diff = subprocess.check_output(["git", "-C", str(ROOT), "diff", "HEAD", "--binary"])
    untracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard", "-z"])
    untracked_digests = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                         for name in untracked.decode().split("\0") if name and (ROOT / name).is_file()}
    report = {"schema_version": 1, "mode": args.mode, "source_sha": sha, "source_dirty": bool(dirty),
              "fixture_sha256": corpus["sha256"], "coverage": corpus["families"],
              "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
              "untracked_file_sha256": untracked_digests,
              "model_inference": "NOT_RUN", "model_latency": None, "platform": "Mac offline Python"}
    exit_code = 0
    if args.mode == "routes":
        report["rows"] = asyncio.run(diagnose_routes(corpus))
        report["routing_diagnostic_only"] = True
        report["limitations"] = ["No reader, action, model or final answer executed.",
            "Earlier turns supply text only; prior tool results/approvals are not replayed.",
            "Tool availability is separate from parameter completion and answer correctness.",
            "Cases with no prescribed tool groups provide no positive routing evidence.",
            "Dirty source evidence records tracked diff and untracked file digests; frozen commit qualification remains separate."]
        report["semantic_outcome"] = "NOT_RUN"
        report["routing_coverage"] = {
            "prescribed_cases": sum(row.get("positive_evidence_evaluated", False) for row in report["rows"]),
            "unprescribed_cases": sum(not row.get("positive_evidence_evaluated", False) for row in report["rows"]),
            "errors": sum("error" in row for row in report["rows"]),
        }
        exit_code = int(any(row.get("error") or row["diagnostic_pass"] is False
                            or row.get("forbidden_direct_calls") for row in report["rows"]))
    elif args.mode == "grade":
        if not args.observations:
            parser.error("grade mode requires --observations")
        raw = args.observations.read_bytes()
        observations = json.loads(raw)
        try:
            indexed = index_observations(observations, {case["id"] for case in corpus["cases"]})
        except ValueError as exc:
            parser.error(str(exc))
        rows = [grade(c, corpus["scenarios"][c["scenario"]], indexed[c["id"]])
                for c in corpus["cases"] if c["id"] in indexed]
        report.update(rows=rows, families=aggregate(rows), observations_sha256=hashlib.sha256(raw).hexdigest(),
                      unrun=[c["id"] for c in corpus["cases"] if c["id"] not in indexed],
                      observation_execution="External supplied observations; provenance must be independently reviewed")
        exit_code = int(bool(report["unrun"]) or any(not row["outcome_pass"] for row in rows))
    else:
        report["semantic_outcome"] = "NOT_RUN"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mode": args.mode, "cases": len(corpus["cases"]), "output": str(args.output), "exit_code": exit_code}))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
