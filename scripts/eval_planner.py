#!/usr/bin/env python
"""Evaluate the real planner loop with synthetic tool execution.

The default corpus is the same deterministic sample used by PR #211's
``real_eval.py``: 57 selected corpus cases plus two ``user_todo`` cases (59
total). ``--cases`` selects comma-separated IDs and ``--limit`` truncates the
selected list.

Tool calls are scored by matching each proposed ``(tool_name, args)`` call to
the first remaining execution with the same tool name, in proposal order.
Arguments are intentionally not compared because the agent loop may normalize
dates and bindings before dispatch. A matched execution is classified as
``ok`` or ``bad_args``; an unmatched proposal is ``refused``. ``extra`` counts
calls outside the union of ``need`` groups, excluding contact resolvers.

An ``action`` case passes when every ``need`` group has at least one successful
tool, no forbidden tool was proposed, and the run ended with nonempty text. A
``chat`` case passes when no tool was proposed. Other expectation types pass
when no forbidden tool was proposed. Repeat, extra-call, latency, request,
argument, and step-limit columns are diagnostics; they do not change pass
status. ``--compare`` prints the same aggregate and per-domain metrics for
existing JSONL result files.

The dry-run path builds and prints the same planner menus without constructing
an inference client, checking oMLX residency, or making model calls. The live
path redirects Wisp state to a temporary home, uses the real planner loop and
oMLX client, and replaces every tool dispatch with a registered synthetic
result from ``eval_planner_fakes.py``.

Examples:
    python scripts/eval_planner.py --dry-run --cases calendar-001,chat-001
    python scripts/eval_planner.py --model MODEL_ID --limit 3 --out results
    python scripts/eval_planner.py --compare --out results
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "test_fixtures" / "routing" / "quality_corpus.json"
DEFAULT_OUT = ROOT / "results"
EXTRA_CASES = [
    {"id": "user-todo-plan", "domain": "user_todo", "prompt": "help me plan my day",
     "expect": "action", "need": [["get_upcoming", "daily_brief", "search_reminders"]],
     "forbid": ["send_message", "send_email"]},
    {"id": "user-todo-forget", "domain": "user_todo",
     "prompt": "what am i forgetting to do this week", "expect": "action",
     "need": [["get_upcoming", "daily_brief", "search_reminders"]],
     "forbid": ["send_message", "send_email"]},
]
PICK = {
    "calendar": 3, "mail": 3, "messages": 2, "notes": 2, "files": 2,
    "reminders": 3, "reminders_read": 3, "calendar_write": 3, "compound": 5,
    "followup": 4, "mail_send": 2, "messages_send": 3,
    "outbound_paraphrase": 4, "web": 3, "device": 3, "chat": 3,
    "memory_write": 2, "delete_paraphrase": 2, "named_item": 2,
    "mail_draft": 1,
}
MUST = {"reminders_read-004", "calendar-011"}
RESOLVERS = {"lookup_contact", "list_contacts", "search_contacts"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="oMLX model ID (required for a live run)")
    parser.add_argument("--cases", default="",
                        help="comma-separated case IDs; default is the PR #211 sample")
    parser.add_argument("--limit", type=int, default=0,
                        help="run only the first N selected cases (0 means all)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="result directory (default: %(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--compare", action="store_true",
                      help="summarize existing JSONL results under --out")
    mode.add_argument("--dry-run", action="store_true",
                      help="print planner menus without model or oMLX calls")
    return parser


def load_corpus() -> list[dict[str, Any]]:
    with CORPUS_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)["cases"]


def sample_cases(corpus: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Select the PR #211 sample with its fixed seed and ordering."""
    corpus = load_corpus() if corpus is None else corpus
    rng = random.Random(20261013)
    selected = [case for case in corpus if case["id"] in MUST]
    for domain, count in PICK.items():
        pool = [case for case in corpus
                if case["domain"] == domain and case["id"] not in MUST]
        selected += rng.sample(pool, min(count, len(pool)))
    return selected + [dict(case) for case in EXTRA_CASES]


def select_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    corpus = load_corpus()
    if args.cases:
        requested = {item.strip() for item in args.cases.split(",") if item.strip()}
        available = {case["id"] for case in corpus + EXTRA_CASES}
        unknown = sorted(requested - available)
        if unknown:
            raise ValueError(f"unknown case ID(s): {', '.join(unknown)}")
        cases = [case for case in corpus + EXTRA_CASES if case["id"] in requested]
    else:
        cases = sample_cases(corpus)
    if args.limit:
        cases = cases[:args.limit]
    return cases


def _redirect_runtime_home(home: Path) -> None:
    """Isolate service state before the first service import."""
    home.mkdir(parents=True, exist_ok=True)
    wisp_home = home / ".moe"
    wisp_home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["WISP_HOME"] = str(wisp_home)
    os.environ.pop("WISP_MODEL_LED_ROUTING", None)
    os.environ.pop("WISP_PLANNER", None)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _load_runtime(original_home: Path) -> dict[str, Any]:
    """Import Wisp only after HOME and WISP_HOME have been redirected."""
    import service.config as config

    # Wisp state stays temporary. oMLX's endpoint and key still come from the
    # user's local oMLX settings for an explicitly requested live evaluation.
    config.OMLX_SETTINGS = original_home / ".omlx" / "settings.json"
    config.OMLX_MODEL_SETTINGS = original_home / ".omlx" / "model_settings.json"

    import service.tools  # noqa: F401  (registers the complete tool inventory)
    import service.memory.identity as identity
    from service.agent import loop, planner
    from service.router import router as router_module
    from service.router.menu import build_menu
    from service.router.router import route, routing_guard_contract
    from service.tools.registry import REGISTRY, _validate_args
    from service.inference.omlx_client import OMLXClient
    from scripts.eval_planner_fakes import (
        FAKE, RETIRED_FAKE_NAMES, fake_result, unregistered_fake_names,
    )

    identity.user_name = lambda: "Test User"
    identity.user_emails = lambda: ["test.user@example.com"]

    # A dry run must stay offline even if a local config changes the optional
    # retrieval provider. The lexical implementation uses no model endpoint.
    router_module.models_config = lambda: {"tool_retrieval": {"provider": "lexical"}}

    state: dict[str, Any] = {}

    async def fake_run_tool(tool, args, **_kwargs):
        if REGISTRY.get(tool.name) is not tool:
            state.setdefault("executed", []).append((tool.name, args, "unregistered"))
            return f"(error calling {tool.name}: unregistered synthetic tool.)"
        if (error := _validate_args(tool, args)) is not None:
            state.setdefault("executed", []).append((tool.name, args, "bad_args"))
            return error
        state.setdefault("executed", []).append((tool.name, args, "ok"))
        return fake_result(tool.name, args)

    loop.run_tool = fake_run_tool
    original_plan_message = planner.plan_message

    def recording_plan_message(plan):
        state["proposed"].extend(plan.calls)
        state["statuses"].append(plan.status)
        return original_plan_message(plan)

    planner.plan_message = recording_plan_message
    original_parse_plan = planner.parse_plan

    def recording_parse_plan(content, allowed):
        result = original_parse_plan(content, allowed)
        state.setdefault("thoughts", []).append(result.thought[:200])
        if result.ends_tool_use:
            state["statuses"].append(
                result.status + (f"({result.error})" if result.error else ""))
        return result

    planner.parse_plan = recording_parse_plan
    original_run_step = loop._run_step

    async def timed_step(*args, **kwargs):
        state["requests"] += 1
        return await original_run_step(*args, **kwargs)

    loop._run_step = timed_step

    class FakeApprover:
        async def confirm(self, action: dict) -> bool:
            # Every dispatch is replaced above; only registered tools can be
            # approved. The loop's calendar batch is a synthetic confirmation
            # for cancel_event/add_calendar_event calls, which are also faked.
            return action.get("tool") in REGISTRY or action.get("tool") == "calendar_changes"

    return {
        "config": config, "loop": loop, "planner": planner,
        "build_menu": build_menu, "route": route,
        "routing_guard_contract": routing_guard_contract,
        "REGISTRY": REGISTRY, "FAKE": FAKE,
        "RETIRED_FAKE_NAMES": RETIRED_FAKE_NAMES,
        "unregistered_fake_names": unregistered_fake_names,
        "OMLXClient": OMLXClient, "state": state, "FakeApprover": FakeApprover,
    }


def _report_fake_inventory(runtime: dict[str, Any]) -> None:
    registry = runtime["REGISTRY"]
    explicit = runtime["FAKE"]
    unregistered = runtime["unregistered_fake_names"]()
    if unregistered:
        print("[fakes] ERROR: explicit fakes without registered tools: "
              + ", ".join(unregistered), file=sys.stderr)
        raise RuntimeError("synthetic fake registry does not match Wisp's tool registry")

    retired = [name for name in runtime["RETIRED_FAKE_NAMES"] if name not in registry]
    print(f"[fakes] {len(explicit)} explicit mappings match registered tools.",
          file=sys.stderr)
    if retired:
        print("[fakes] removed PR #211 mappings for tools absent from the current "
              f"registry ({len(retired)}): " + ", ".join(retired), file=sys.stderr)


async def _case_menu(runtime: dict[str, Any], case: dict[str, Any]
                     ) -> tuple[list[str], Any, frozenset[str], dict[str, Any]]:
    decision = await runtime["route"](
        case["prompt"], last_user=case.get("last_user"),
        last_assistant=case.get("last_assistant"), last_tools=case.get("last_tools"))
    forbidden, bindings = runtime["routing_guard_contract"](case["prompt"])
    menu = runtime["build_menu"](
        case["prompt"], last_assistant=case.get("last_assistant"),
        forbidden=frozenset(forbidden))
    return menu, decision, frozenset(forbidden), bindings


async def print_dry_run(runtime: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    print("PLANNER DRY RUN — no inference client or model calls")
    for case in cases:
        menu, _decision, _forbidden, _bindings = await _case_menu(runtime, case)
        print(f"CASE {case['id']} [{case['domain']}/{case['expect']}]")
        print(f"PROMPT: {case['prompt']}")
        print(f"MENU ({len(menu)}): {', '.join(menu) if menu else '(empty)'}")


async def run_case(runtime: dict[str, Any], client: Any, model: str,
                   case: dict[str, Any]) -> dict[str, Any]:
    state = runtime["state"]
    state.update(executed=[], proposed=[], statuses=[], requests=0, thoughts=[])
    menu, decision, forbidden, guard_bindings = await _case_menu(runtime, case)
    messages = []
    if case.get("last_user"):
        messages.append({"role": "user", "content": case["last_user"]})
    if case.get("last_assistant"):
        messages.append({"role": "assistant", "content": case["last_assistant"]})
    messages.append({"role": "user", "content": case["prompt"]})
    async def emit(_event: dict[str, Any]) -> None:
        return None

    started = time.monotonic()
    try:
        final = await runtime["loop"].run_agent(
            client, model, messages, emit, runtime["FakeApprover"](),
            tools=menu, planner=True, multi_round=True,
            forbidden_tools=forbidden,
            tool_argument_bindings={**decision.tool_argument_bindings, **guard_bindings},
            debug=False)
        end = "text" if str(final).strip() else "empty"
    except Exception as error:  # noqa: BLE001 — retain failures as comparable rows
        final, end = f"{type(error).__name__}: {error}", "error"
    latency = time.monotonic() - started
    executed = list(state["executed"])
    proposed = list(state["proposed"])

    # Match proposals by tool name in order. The loop can normalize arguments
    # before dispatch, so argument equality would misclassify executed calls.
    pool = list(executed)
    calls = []
    for name, args in proposed:
        hit = next((item for item in pool if item[0] == name), None)
        if hit is None:
            kind = "refused"
        else:
            pool.remove(hit)
            kind = hit[2]
        calls.append((name, args, kind))

    need = [set(group) for group in case.get("need") or []]
    useful = set().union(*need) if need else set()
    ok_names = {name for name, _args, kind in calls if kind == "ok"}
    seen_exact: set[tuple[str, str]] = set()
    seen_tools: set[str] = set()
    repeat_exact = repeat_tool = 0
    for name, args, _kind in calls:
        key = (name, json.dumps(args, sort_keys=True))
        repeat_exact += key in seen_exact
        repeat_tool += name in seen_tools
        seen_exact.add(key)
        seen_tools.add(name)
    names = [name for name, _args, _kind in calls]
    complete = all(group & ok_names for group in need) if need else None
    forbidden_hits = sorted(set(case.get("forbid") or []) & set(names))
    if case["expect"] == "action":
        passed = bool(complete) and not forbidden_hits and end == "text"
    elif case["expect"] == "chat":
        passed = not names
    else:
        passed = not forbidden_hits

    return {
        "id": case["id"], "domain": case["domain"], "expect": case["expect"],
        "passed": passed, "complete": complete, "calls": len(names),
        "distinct": len(set(names)), "rep_exact": repeat_exact, "rep_tool": repeat_tool,
        "extra": sum(name not in useful and name not in RESOLVERS for name in names),
        "extra_names": [name for name in names if name not in useful and name not in RESOLVERS],
        "forbidden": forbidden_hits, "unoffered": 0,
        "bad_args": sum(kind == "bad_args" for _name, _args, kind in calls),
        "dup_guard": sum(kind == "refused" for _name, _args, kind in calls),
        "direct": 0, "end": end, "requests": state["requests"],
        "latency": round(latency, 2), "empty_retries": 0, "prompt_tokens": [0],
        "reasoning_chars": 0, "dropped_tools": [], "offered": len(menu),
        "route": "planner menu", "final": str(final)[:500],
        "statuses": state["statuses"], "thoughts": state["thoughts"],
        "trace": [(name, json.dumps(args)[:160], kind) for name, args, kind in calls],
        "executed": [(name, json.dumps(args)[:160], kind)
                     for name, args, kind in executed],
        "menu": menu,
    }


def _admin_json(base_url: str, api_key: str, path: str, *, method: str = "GET") -> dict:
    request = urllib.request.Request(
        base_url + path, method=method,
        data=b"" if method == "POST" else None,
        headers={**({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def ensure_only_resident(runtime: dict[str, Any], model: str) -> None:
    """Unload other resident oMLX models before an explicitly requested run."""
    config = runtime["config"]
    from service.config.endpoints import is_loopback

    base_url = config.omlx_base_url().rstrip("/")
    if not is_loopback(base_url):
        raise RuntimeError("planner evaluation residency checks require loopback oMLX")
    api_key = config.omlx_api_key()
    for _attempt in range(60):
        status = _admin_json(base_url, api_key, "/v1/models/status")
        resident = [row["id"] for row in status.get("models", [])
                    if row.get("loaded") or row.get("is_loading")]
        others = [name for name in resident if name != model]
        if not others:
            return
        for other in others:
            print(f"[residency] unloading {other} before using {model}", flush=True)
            escaped = urllib.parse.quote(other, safe="")
            try:
                _admin_json(base_url, api_key, f"/v1/models/{escaped}/unload", method="POST")
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                print(f"[residency] unload {other} failed: {error}", flush=True)
        time.sleep(2)
    raise RuntimeError(f"could not get {model} to be the only resident model")


async def run_live(runtime: dict[str, Any], args: argparse.Namespace,
                   cases: list[dict[str, Any]]) -> None:
    output = args.out / args.model / "PLANNER.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if output.exists():
        with output.open(encoding="utf-8") as stream:
            done = {json.loads(line)["id"] for line in stream if line.strip()}

    client = runtime["OMLXClient"]()
    try:
        for case in cases:
            if case["id"] in done:
                continue
            row = await run_case(runtime, client, args.model, case)
            with output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"PLANNER {case['id']:28s} pass={str(row['passed']):5s} "
                  f"calls={row['calls']} rep={row['rep_exact']}/{row['rep_tool']} "
                  f"extra={row['extra']} end={row['end']} {row['latency']:.1f}s "
                  f"{row['statuses']}", flush=True)
    finally:
        await client.aclose()


def _percent(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator} = {numerator / denominator:.0%}" if denominator else "-"


def _load_result_files(out_dir: Path) -> list[tuple[str, str, list[dict[str, Any]]]]:
    columns = []
    for path in sorted(out_dir.rglob("*.jsonl")):
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read result file {path}: {error}") from error
        if rows:
            relative = path.relative_to(out_dir)
            model = relative.parts[-2] if len(relative.parts) > 1 else "(default)"
            columns.append((model, path.stem, rows))
    return columns


def compare_results(out_dir: Path) -> str:
    columns = _load_result_files(out_dir)
    if not columns:
        return f"No JSONL result files found under {out_dir}."
    lines = ["| metric | " + " | ".join(f"{model} {arm}" for model, arm, _ in columns) + " |",
             "|---|" + "---|" * len(columns)]

    def row(label: str, render) -> None:
        lines.append("| " + label + " | " + " | ".join(render(rows) for _, _, rows in columns) + " |")

    row("cases", lambda rows: str(len(rows)))
    row("pass (all)", lambda rows: _percent(sum(bool(r.get("passed")) for r in rows), len(rows)))
    row("action pass (complete + answered)", lambda rows: _percent(
        sum(bool(r.get("passed")) for r in rows if r.get("expect") == "action"),
        sum(r.get("expect") == "action" for r in rows)))
    row("chat cases with no tool call", lambda rows: _percent(
        sum(bool(r.get("passed")) for r in rows if r.get("expect") == "chat"),
        sum(r.get("expect") == "chat" for r in rows)))
    row("model tool calls / case (mean)", lambda rows: f"{statistics.mean(r.get('calls', 0) for r in rows):.2f}")
    row("calls beyond need set / case", lambda rows: f"{statistics.mean(r.get('extra', 0) for r in rows):.2f}")
    row("exact repeat calls (total)", lambda rows: str(sum(r.get("rep_exact", 0) for r in rows)))
    row("same-tool repeat calls (total)", lambda rows: str(sum(r.get("rep_tool", 0) for r in rows)))
    row("cases with any exact repeat", lambda rows: str(sum(r.get("rep_exact", 0) > 0 for r in rows)))
    row("hit 8-step cap", lambda rows: str(sum(r.get("end") == "step_limit" for r in rows)))
    row("ended empty", lambda rows: str(sum(r.get("end") in ("empty", "bad_plan") for r in rows)))
    row("unoffered-tool calls", lambda rows: str(sum(r.get("unoffered", 0) for r in rows)))
    row("bad-argument calls", lambda rows: str(sum(r.get("bad_args", 0) for r in rows)))
    row("forbidden-tool cases", lambda rows: str(sum(bool(r.get("forbidden")) for r in rows)))
    row("model requests / case", lambda rows: f"{statistics.mean(r.get('requests', 0) for r in rows):.2f}")

    def latency(rows: list[dict[str, Any]]) -> str:
        values = sorted(float(r.get("latency", 0)) for r in rows)
        p90 = values[int(.9 * len(values))]
        return f"{statistics.median(values):.1f} / {p90:.1f}"

    row("latency median / p90 (s)", latency)

    def prompt_median(rows: list[dict[str, Any]]) -> str:
        values = [tokens[0] for r in rows
                  if (tokens := r.get("prompt_tokens")) and len(tokens)]
        return str(int(statistics.median(values))) if values else "-"

    row("round-0 prompt tokens (median)", prompt_median)
    row("cases where window trim dropped tools",
        lambda rows: str(sum(bool(r.get("dropped_tools")) for r in rows)))

    domains = sorted({r.get("domain", "unknown") for _, _, rows in columns for r in rows})
    lines.extend(["", "| domain | " + " | ".join(f"{model} {arm}" for model, arm, _ in columns) + " |",
                  "|---|" + "---|" * len(columns)])
    for domain in domains:
        cells = []
        for _, _, all_rows in columns:
            rows = [r for r in all_rows if r.get("domain", "unknown") == domain]
            if rows:
                passed = sum(bool(r.get("passed")) for r in rows)
                calls = statistics.mean(float(r.get("calls", 0)) for r in rows)
                cells.append(f"{passed}/{len(rows)} ({calls:.1f}c)")
            else:
                cells.append("-")
        lines.append(f"| {domain} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be zero or greater")
    if args.compare:
        try:
            print(compare_results(args.out))
        except ValueError as error:
            parser.error(str(error))
        return 0
    if not args.dry_run and not args.model:
        parser.error("--model is required unless --dry-run or --compare is used")
    try:
        cases = select_cases(args)
    except ValueError as error:
        parser.error(str(error))
    if not cases:
        print("No cases selected.")
        return 0

    caller_home = Path(os.environ.get("HOME") or Path.home()).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="wisp-planner-eval-") as temp:
        _redirect_runtime_home(Path(temp))
        runtime = _load_runtime(caller_home)
        _report_fake_inventory(runtime)
        if args.dry_run:
            asyncio.run(print_dry_run(runtime, cases))
            return 0
        ensure_only_resident(runtime, args.model)
        asyncio.run(run_live(runtime, args, cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
