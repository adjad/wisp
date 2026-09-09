#!/usr/bin/env python3
"""Run the reviewable user-style workflow suite against installed Wisp.

Live effects are always denied at Wisp's real confirmation endpoint. Drafts
run only under test_mode because drafts are auto-run and would otherwise open
Mail or Messages.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "test_fixtures" / "workflow_user_suite.json"
PROMPTS_PATH = ROOT / "docs" / "WISP_USER_PROMPT_SUITE.md"
BASE = "http://127.0.0.1:8765"
FAILURE_WORDS = (
    " not run", "not sent", "not scheduled", "couldn't", "could not",
    " unavailable", "blocked by", " error", " failed",
)


def load_cases() -> list[dict]:
    return json.loads(SUITE_PATH.read_text())


def render_prompts(cases: list[dict]) -> None:
    lines = [
        "# Wisp user-style workflow test prompts",
        "",
        "These prompts are frozen before execution. Live outbound cases use Mom or "
        "`johnstandark@gmail.com`; the runner denies every confirmation, so nothing "
        "is sent. Draft cases use Wisp test mode and do not open applications.",
        "",
        "Strict failure conditions: wrong or missing route, wrong/extra/reordered tool, "
        "wrong bound recipient/channel, missing confirmation, any source/tool error, "
        "grounding rejection, incomplete action, or runtime over the case threshold.",
        "",
    ]
    current = None
    for case in sorted(cases, key=lambda item: (item["category"], item["id"])):
        if case["category"] != current:
            current = case["category"]
            lines += [f"## {current}", ""]
        lines += [f"### {case['id']}", "", case["description"], ""]
        for index, step in enumerate(case["steps"], 1):
            lines.append(f"{index}. **{step['prompt']}**")
        expected = []
        for step in case["steps"]:
            calls = step.get("exact_calls") or step.get("required_calls")
            if calls:
                expected.append(" → ".join(calls))
            elif step.get("question"):
                expected.append(step["question"])
        lines += ["", "Expected: " + " | ".join(expected), ""]
    PROMPTS_PATH.write_text("\n".join(lines) + "\n")


def post_json(path: str, body: dict, timeout: float = 15) -> dict:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def run_step(step: dict, session_id: str, timeout: float) -> tuple[str, list[dict], float, str]:
    body = {"prompt": step["prompt"], "debug": False}
    if session_id:
        body["session_id"] = session_id
    if step["mode"] == "dry":
        body["test_mode"] = True
    request = urllib.request.Request(
        BASE + "/agent", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    events: list[dict] = []
    error = ""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=max(60, timeout + 15)) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if event.get("type") == "session":
                    session_id = event.get("id", session_id)
                if event.get("type") == "confirm":
                    # No live effect can cross this boundary in this suite.
                    post_json("/agent/approve", {
                        "session_id": session_id,
                        "action_id": event.get("id"),
                        "approved": False,
                        "scope": "once",
                    })
    except Exception as exc:  # noqa: BLE001 - the report needs the exact failure
        error = f"{type(exc).__name__}: {exc}"
    return session_id, events, time.monotonic() - started, error


def ordered_subset(required: list[str], actual: list[str]) -> bool:
    cursor = 0
    for name in actual:
        if cursor < len(required) and name == required[cursor]:
            cursor += 1
    return cursor == len(required)


def grade_step(step: dict, events: list[dict], elapsed: float,
               transport_error: str, max_seconds: float) -> list[str]:
    failures: list[str] = []
    calls = [event.get("name", "") for event in events if event.get("type") == "tool_call"]
    routes = [event for event in events if event.get("type") == "routed"]
    texts = [str(event.get("text", "")) for event in events
             if event.get("type") in {"text", "delta"}]
    results = [str(event.get("result", "")) for event in events
               if event.get("type") == "tool_result"]
    confirms = [event for event in events if event.get("type") == "confirm"]
    errors = [event for event in events if event.get("type") == "error"]

    if transport_error:
        failures.append(transport_error)
    if errors:
        failures.append("service error event: " + "; ".join(str(e.get("message")) for e in errors))
    if elapsed > max_seconds:
        failures.append(f"too slow: {elapsed:.2f}s > {max_seconds:.2f}s")

    if "exact_calls" in step and calls != step["exact_calls"]:
        failures.append(f"wrong tool sequence: expected {step['exact_calls']}, got {calls}")
    if required := step.get("required_calls"):
        if calls != required:
            failures.append(f"wrong tool sequence: expected {required}, got {calls}")
    if expected_route := step.get("route"):
        actual = routes[-1].get("route_source") if routes else ""
        if actual != expected_route:
            failures.append(f"wrong route: expected {expected_route}, got {actual or 'none'}")
    if forbidden_route := step.get("route_not"):
        actual = routes[-1].get("route_source") if routes else ""
        if actual == forbidden_route:
            failures.append(f"forbidden route used: {actual}")
    if question := step.get("question"):
        text_events = [str(e.get("text", "")) for e in events if e.get("type") == "text"]
        actual = text_events[-1] if text_events else ""
        if actual != question:
            failures.append(f"wrong clarification: expected {question!r}, got {actual!r}")

    effect = step.get("effect")
    if effect:
        effect_calls = [event for event in events
                        if event.get("type") == "tool_call" and event.get("name") == effect]
        if not effect_calls:
            failures.append(f"missing effect call: {effect}")
        else:
            args = effect_calls[-1].get("args") or {}
            if expected := step.get("recipient"):
                if str(args.get("to", "")).lower() != str(expected).lower():
                    failures.append(f"wrong bound recipient: {args.get('to')!r} != {expected!r}")
            if expected := step.get("channel_arg"):
                if args.get("channel") != expected:
                    failures.append(f"wrong scheduled channel: {args.get('channel')!r} != {expected!r}")

    if step["mode"] == "live_deny":
        if len(confirms) != 1:
            failures.append(f"expected exactly one confirmation, got {len(confirms)}")
        if not any("user denied" in result.lower() for result in results):
            failures.append("confirmation denial was not observed")
        final_texts = [str(e.get("text", "")) for e in events if e.get("type") == "text"]
        if confirms and final_texts and not any(word in final_texts[-1].lower()
                                                for word in ("not completed", "denied", "couldn't")):
            failures.append("denied action ended with an unverified success-style response")
    elif confirms:
        failures.append("confirmation appeared during a non-live step")

    seen_failed_results = set()
    for result in results:
        lower = " " + result.lower()
        if "user denied" in lower or "dry-run placeholder" in lower:
            continue
        if any(marker in lower for marker in FAILURE_WORDS):
            excerpt = result[:240]
            if excerpt not in seen_failed_results:
                failures.append("tool/source failure: " + excerpt)
                seen_failed_results.add(excerpt)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()
    cases = load_cases()
    render_prompts(cases)
    if args.list_only:
        print(PROMPTS_PATH)
        print(f"{len(cases)} scenarios, {sum(len(c['steps']) for c in cases)} prompts")
        return 0
    if args.ids:
        wanted = set(args.ids)
        cases = [case for case in cases if case["id"] in wanted]

    rows = []
    for case in cases:
        session_id = ""
        case_failures = []
        step_rows = []
        max_seconds = float(case.get("max_seconds", 30))
        for index, step in enumerate(case["steps"], 1):
            session_id, events, elapsed, error = run_step(
                step, session_id, max_seconds)
            failures = grade_step(step, events, elapsed, error, max_seconds)
            case_failures.extend(f"step {index}: {failure}" for failure in failures)
            step_rows.append({
                "prompt": step["prompt"], "mode": step["mode"],
                "elapsed_seconds": round(elapsed, 3),
                "calls": [e.get("name") for e in events if e.get("type") == "tool_call"],
                "route": next((e.get("route_source") for e in events
                               if e.get("type") == "routed"), ""),
                "failures": failures,
                "events": events,
            })
        if session_id:
            try:
                request = urllib.request.Request(
                    BASE + f"/sessions/{session_id}", method="DELETE")
                urllib.request.urlopen(request, timeout=10).read()
            except Exception:
                case_failures.append("could not delete isolated test session")
        rows.append({**case, "status": "FAIL" if case_failures else "PASS",
                     "failures": case_failures, "results": step_rows})
        print(f"{rows[-1]['status']:4} {case['id']}"
              + (" — " + "; ".join(case_failures) if case_failures else ""), flush=True)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "test_results" / f"workflow_user_suite_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(rows, indent=2))
    passed = sum(row["status"] == "PASS" for row in rows)
    report = [
        "# Wisp user-style workflow suite results", "",
        f"Strict result: **{passed}/{len(rows)} passed; {len(rows) - passed} failed.**", "",
        "| Result | Scenario | Time | Tools | Failure |",
        "|---|---|---:|---|---|",
    ]
    for row in rows:
        elapsed = sum(step["elapsed_seconds"] for step in row["results"])
        tools = " / ".join(" → ".join(step["calls"]) or "none" for step in row["results"])
        failure = "<br>".join(row["failures"]) or ""
        report.append(f"| {row['status']} | {row['id']} | {elapsed:.2f}s | {tools} | {failure} |")
    report += ["", f"Full event traces: `{out_dir / 'results.json'}`", ""]
    (out_dir / "report.md").write_text("\n".join(report))
    print(out_dir)
    return 1 if passed != len(rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
