"""Drive the original corpus through the contained Notch evaluation app.

Stdlib only; runs outside the evaluation host. Each turn is typed into the
real Ask field by the app's evaluation-only input adapter (a private,
token-checked Unix socket in the owned root): the adapter sets the field,
calls the normal submit, declines any confirmation card through the card's
own path, and answers with the finalized turn exactly as displayed.

Results are appended to ``results.jsonl`` in the private run directory
(0600, outside Git), one record per case attempt, keyed by ``case_id``. A
resumed run skips every case with a terminal record. Nothing here grades
answers: correctness stays UNVERIFIED for the separate grading pass.

    driver.py init   --run-dir D --corpus PATH --corpus-sha256 SHA
    driver.py run    --run-dir D --owned ROOT --engine URL --model ID
                     [--canary N | --ids 1,2 | --limit N] [--label L]
    driver.py status --run-dir D
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.notch_evaluation import corpus  # noqa: E402

RESULTS = "results.jsonl"
SOCKET = "app-input.sock"
MAX_ATTEMPTS = 2
TURN_TIMEOUT_S = 240.0
BLOCKED_MARKER = "EVALUATION_BLOCKED"
# A case with one of these is tried again on resume (up to MAX_ATTEMPTS).
RETRYABLE = frozenset({"TIMEOUT", "NOT_SUBMITTED", "HOST_BUSY", "DROPPED", "ENGINE_NOT_READY",
                       "HOST_UNAVAILABLE", "INTERRUPTED"})
# These stop the run: continuing would record nothing meaningful.
STOPPING = frozenset({"ENGINE_NOT_READY", "HOST_UNAVAILABLE", "EVIDENCE_UNHEALTHY", "INTERRUPTED"})
# Worst first: a case's class is its worst turn's.
SEVERITY = ("EVIDENCE_UNHEALTHY", "HOST_UNAVAILABLE", "ENGINE_NOT_READY", "INTERRUPTED", "INPUT_MISMATCH",
            "NOT_SUBMITTED", "HOST_BUSY", "TIMEOUT", "DROPPED", "MODEL_MISMATCH", "ERROR_TURN",
            "EMPTY_ANSWER", "EFFECT_BLOCKED", "CONFIRMATION_DECLINED", "OK")


class Stop(Exception):
    """The run must stop; the reason is recorded."""


# --- run directory ---------------------------------------------------------

def private_dir(path: str | os.PathLike) -> Path:
    run = Path(path)
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(run)
    if run.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit(f"{run} must be a private directory owned by this user")
    return run


def read_results(run: Path) -> list[dict]:
    path = run / RESULTS
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"case_id": None, "failure_class": "UNREADABLE_RECORD"})
    return records


def append_result(run: Path, record: dict) -> None:
    fd = os.open(run / RESULTS, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def case_state(records: list[dict]) -> dict[int, dict]:
    """Per case: attempts so far and whether it is finished."""
    state: dict[int, dict] = {}
    for record in records:
        case_id = record.get("case_id")
        if not isinstance(case_id, int):
            continue
        entry = state.setdefault(case_id, {"attempts": 0, "done": False, "last": None})
        entry["attempts"] += 1
        entry["last"] = record.get("failure_class")
        if record.get("failure_class") not in RETRYABLE or entry["attempts"] >= MAX_ATTEMPTS:
            entry["done"] = True
    return state


def select(cases: list[corpus.Case], state: dict[int, dict], *, canary: int | None = None,
           ids: list[int] | None = None, limit: int | None = None) -> list[corpus.Case]:
    pending = [c for c in cases if not state.get(c.case_id, {}).get("done")]
    if ids:
        wanted = set(ids)
        pending = [c for c in pending if c.case_id in wanted]
    if canary:
        pending = [c for c in pending if c.risk_tag == "R" and len(c.segments) == 1][:canary]
    if limit:
        pending = pending[:limit]
    return pending


# --- engine and app --------------------------------------------------------

def engine_resident(engine: str, model: str, *, timeout: float = 5.0) -> bool:
    """True when the engine reports ``model`` loaded. A read; never loads anything."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{engine.rstrip('/')}/v1/models/status", timeout=timeout) as response:
            rows = json.loads(response.read()).get("models")
    except (OSError, ValueError):
        return False
    return isinstance(rows, list) and any(isinstance(r, dict) and r.get("id") == model
                                          and r.get("loaded") is True for r in rows)


def app_request(owned: Path, request: dict, *, timeout: float) -> dict:
    """One JSON line to the app's input adapter, one JSON line back."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        # Relative from inside the owned root: a socket path is limited to 104 bytes.
        same = Path.cwd().resolve() == owned.resolve()
        conn.connect(SOCKET if same else str(owned / SOCKET))
        conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    return json.loads(b"".join(chunks))


def run_token(owned: Path) -> str:
    return json.loads((owned / "host-config.json").read_text(encoding="utf-8"))["token"]


# --- classification --------------------------------------------------------

def classify_turn(reply: dict, *, segment: str, model: str) -> str:
    status = reply.get("status")
    if status == "unhealthy":
        return "EVIDENCE_UNHEALTHY"
    if status == "busy":
        return "HOST_BUSY"
    if status == "timeout":
        return "TIMEOUT"
    if status != "terminal":
        return "NOT_SUBMITTED"
    if reply.get("input_sha256") != corpus.sha256_text(segment):
        return "INPUT_MISMATCH"
    outcome = reply.get("outcome")
    if outcome == "dropped":
        return "DROPPED"
    served = [m for m in reply.get("response_models") or [] if m]
    if served and any(m != model for m in served):
        return "MODEL_MISMATCH"
    if outcome == "error":
        return "ERROR_TURN"
    if not (reply.get("text") or "").strip():
        return "EMPTY_ANSWER"
    if any(BLOCKED_MARKER in str(t.get("result", "")) for t in reply.get("tools") or []):
        return "EFFECT_BLOCKED"
    if reply.get("confirmation"):
        return "CONFIRMATION_DECLINED"
    return "OK"


def worst(classes: list[str]) -> str:
    return min(classes, key=SEVERITY.index) if classes else "NOT_SUBMITTED"


def _turn_record(segment: str, reply: dict, failure: str) -> dict:
    started, finished, first = reply.get("started_at"), reply.get("finished_at"), reply.get("first_token_at")
    latency = finished - started if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None
    ttft = first - started if isinstance(started, (int, float)) and isinstance(first, (int, float)) else None
    return {"prompt": segment, "failure_class": failure, "status": reply.get("status"),
            "outcome": reply.get("outcome"), "final_text": reply.get("text"),
            "model": reply.get("model"), "response_models": reply.get("response_models") or [],
            "route": {"role": reply.get("route_role"), "source": reply.get("route_source"),
                      "reason": reply.get("route_reason")},
            "tools": reply.get("tools") or [], "confirmation": reply.get("confirmation"),
            "latency_s": latency, "ttft_s": ttft, "session": reply.get("session"),
            "trace_id": reply.get("trace_id"),
            "dispatch": reply.get("dispatch"), "error": reply.get("error")}


def run_case(case: corpus.Case, *, owned: Path, token: str, engine: str, model: str,
             timeout: float, stopping) -> dict:
    turns, classes = [], []
    for index, segment in enumerate(case.segments):
        if stopping():
            classes.append("INTERRUPTED")
            break
        if not engine_resident(engine, model):
            classes.append("ENGINE_NOT_READY")
            turns.append({"prompt": segment, "failure_class": "ENGINE_NOT_READY"})
            break
        request = {"op": "ask", "token": token, "case": f"{case.case_id}-{index}", "text": segment,
                   "new_chat": index == 0, "timeout_s": timeout}
        try:
            reply = app_request(owned, request, timeout=timeout + 60)
        except KeyboardInterrupt:
            classes.append("INTERRUPTED")
            turns.append({"prompt": segment, "failure_class": "INTERRUPTED"})
            break
        except (OSError, ValueError) as exc:
            classes.append("HOST_UNAVAILABLE")
            turns.append({"prompt": segment, "failure_class": "HOST_UNAVAILABLE", "error": type(exc).__name__})
            break
        failure = classify_turn(reply, segment=segment, model=model)
        turns.append(_turn_record(segment, reply, failure))
        classes.append(failure)
        if failure not in ("OK", "EFFECT_BLOCKED", "CONFIRMATION_DECLINED", "EMPTY_ANSWER", "ERROR_TURN"):
            break       # later turns of this case would answer a different conversation
    latencies = [t.get("latency_s") for t in turns if isinstance(t.get("latency_s"), (int, float))]
    return {"case_id": case.case_id, "risk_tag": case.risk_tag, "turn_count": len(case.segments),
            "turns_delivered": sum(1 for t in turns if t.get("status") == "terminal"),
            "failure_class": worst(classes), "model": next((t.get("model") for t in turns if t.get("model")), None),
            "latency_s": sum(latencies) if latencies else None, "turns": turns,
            "correctness": "UNVERIFIED"}


# --- commands --------------------------------------------------------------

def cmd_init(args) -> int:
    run = private_dir(args.run_dir)
    cases = corpus.parse(args.corpus, expected_sha256=args.corpus_sha256)
    print(json.dumps(corpus.initialize(run, cases, corpus_sha256=args.corpus_sha256)))
    return 0


def cmd_status(args) -> int:
    run = private_dir(args.run_dir)
    records = read_results(run)
    state = case_state(records)
    classes: dict[str, int] = {}
    for entry in state.values():
        classes[entry["last"]] = classes.get(entry["last"], 0) + 1
    total = len(json.loads((run / "ledger.json").read_text(encoding="utf-8"))["cases"])
    print(json.dumps({"cases": total, "finished": sum(e["done"] for e in state.values()),
                      "attempts": len(records), "last_class": classes}, indent=1, sort_keys=True))
    return 0


def cmd_run(args) -> int:
    run = private_dir(args.run_dir)
    owned = Path(args.owned)
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    cases = corpus.load_cases(run, corpus_sha256=ledger["corpus_sha256"])
    state = case_state(read_results(run))
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    todo = select(cases, state, canary=args.canary, ids=ids, limit=args.limit)
    token = run_token(owned)
    signals = {"count": 0}

    def on_signal(signum, frame):
        signals["count"] += 1
        if signals["count"] > 1:
            raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    ping = None
    try:
        ping = app_request(owned, {"op": "ping", "token": token}, timeout=30)
    except (OSError, ValueError):
        pass
    if not ping or ping.get("status") != "ready":
        print(json.dumps({"stopped": "HOST_UNAVAILABLE", "ping": ping}))
        return 4
    print(json.dumps({"selected": len(todo), "label": args.label, "engine_model": args.model}), flush=True)
    for case in todo:
        if signals["count"]:
            print(json.dumps({"stopped": "INTERRUPTED"}))
            return 130
        record = run_case(case, owned=owned, token=token, engine=args.engine, model=args.model,
                          timeout=args.timeout, stopping=lambda: signals["count"] > 0)
        attempt = state.get(case.case_id, {}).get("attempts", 0) + 1
        record.update(attempt=attempt, label=args.label, candidate_sha=args.candidate_sha,
                      recorded_at=time.time())
        append_result(run, record)
        state.setdefault(case.case_id, {"attempts": 0, "done": False, "last": None})["attempts"] = attempt
        print(json.dumps({"case_id": case.case_id, "risk": case.risk_tag, "class": record["failure_class"],
                          "latency_s": record["latency_s"]}), flush=True)
        if record["failure_class"] in STOPPING:
            print(json.dumps({"stopped": record["failure_class"], "case_id": case.case_id}))
            return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--corpus", required=True)
    init.add_argument("--corpus-sha256", required=True)
    status = sub.add_parser("status")
    status.add_argument("--run-dir", required=True)
    go = sub.add_parser("run")
    go.add_argument("--run-dir", required=True)
    go.add_argument("--owned", required=True)
    go.add_argument("--engine", required=True)
    go.add_argument("--model", required=True)
    go.add_argument("--canary", type=int)
    go.add_argument("--ids")
    go.add_argument("--limit", type=int)
    go.add_argument("--label", default="run")
    go.add_argument("--candidate-sha", default="")
    go.add_argument("--timeout", type=float, default=TURN_TIMEOUT_S)
    args = parser.parse_args(argv)
    return {"init": cmd_init, "status": cmd_status, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
