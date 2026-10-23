"""The user's own prompt corpus, run as a backend job through the normal Ask path.

The prompts file is the user's private document (real names, addresses and
plans). It lives outside the repository, at ~/.moe/corpus-prompts-1000.md by
default, and is read only here. Each entry is one line, ``N. `[R|W|S]` text``,
under ``## N. Section (count)`` headers; a multi-turn entry joins its turns
with `` → ``. This is the same format the Notch evaluation harness parses.

Every case runs as one chat through /agent inside review_scope (see
corpus.agent_turn), so anything that would send, delete or change something
asks first and is declined, and is recorded as "would ask to confirm". Each
case is saved as a "Corpus · P-<n> · …" chat, which memory, behaviour and
search leave out.

The run is a job in the service, not tied to the notch staying open. Its
state is saved, so after a restart `/corpus prompts resume` continues it and
skips the cases already finished. It waits while the user's own turn runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from service import corpus
from service.paths import MOE_DIR

log = logging.getLogger(__name__)
router = APIRouter(prefix="/corpus/prompts")

PROMPTS_FILE = MOE_DIR / "corpus-prompts-1000.md"
# The sha256 recorded at import, so a changed file is noticed, not run.
PROMPTS_META = MOE_DIR / "corpus-prompts.json"
JOB_FILE = MOE_DIR / "corpus-prompts-run.json"
FILE_LIMIT = 4 * 1024 * 1024
BATCH = 100
# One turn may plan, swap models and run tools, as the user's own can.
TURN_TIMEOUT_S = 600
# The user must have been idle this long before the next case starts.
IDLE_SETTLE_S = 3.0
IDLE_POLL_S = 2.0

# The reviewed corpus: it is verified before it is parsed, and its ids must
# be exactly 1..EXPECTED_CASES, once each, in order.
EXPECTED_SHA256 = "62f21e90c6b03b3d0391b3e17de69b946afa0df2a0802b43977bb4ea7382fdfc"
EXPECTED_CASES = 1000
# Bounds on what one turn's record keeps of the stream.
TURN_EVENTS = 200
EVENT_RESULT = 2000
TOOL_RESULT = 8000

ENTRY = re.compile(r"^(\d+)\. `\[([RWS])\]` (.+)$")
SECTION = re.compile(r"^## (\d+)\. (.+?)(?: \(\d+\))?$")
TURN_SEPARATOR = " → "


class PromptsError(ValueError):
    """A prompts file that is missing, changed or not in the corpus format."""


@dataclass(frozen=True)
class PromptCase:
    number: int
    tag: str
    text: str
    segments: tuple[str, ...]
    section: str = ""
    section_number: int = 0

    @property
    def id(self) -> str:
        return f"P-{self.number}"


def parse(text: str, expected_cases: int | None = None) -> list[PromptCase]:
    """Every case, in file order. Refuses a file that is not the corpus format,
    or whose ids are not exactly 1..expected_cases (default EXPECTED_CASES)."""
    cases: list[PromptCase] = []
    section, section_number = "", 0
    for line in text.splitlines():
        if heading := SECTION.match(line):
            section_number, section = int(heading.group(1)), heading.group(2).strip()
            continue
        match = ENTRY.match(line)
        if not match:
            continue
        body = match.group(3).strip()
        segments = tuple(part.strip() for part in body.split(TURN_SEPARATOR))
        if any(not part for part in segments):
            raise PromptsError(f"Prompt {match.group(1)} has an empty turn.")
        cases.append(PromptCase(int(match.group(1)), match.group(2), body, segments, section, section_number))
    if not cases:
        raise PromptsError("No prompts found. Each line should read: 12. `[R]` What's on my calendar?")
    expected = EXPECTED_CASES if expected_cases is None else expected_cases
    if [c.number for c in cases] != list(range(1, expected + 1)):
        raise PromptsError(f"Expected prompts 1 to {expected} once each, in order; found {len(cases)}.")
    return cases


def sections(cases: list[PromptCase]) -> list[dict]:
    found: dict[int, dict] = {}
    for case in cases:
        entry = found.setdefault(case.section_number, {"number": case.section_number,
                                                       "title": case.section, "count": 0})
        entry["count"] += 1
    return list(found.values())


def _write_private(path: Path, data: bytes) -> None:
    """Whole-file replace, owner-only, never through a symlink."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_limited(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PromptsError(f"{path} is not a regular file.")
    if path.stat().st_size > FILE_LIMIT:
        raise PromptsError("The prompts file is larger than 4 MB.")
    return path.read_bytes()


def import_file(source: str) -> dict:
    """Copy the user's prompts file into ~/.moe (0600) and record its sha256."""
    path = Path(os.path.expanduser(source.strip().strip("'\""))).resolve()
    data = _read_limited(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise PromptsError("That file is not the reviewed prompts corpus (its sha256 does not match).")
    try:
        cases = parse(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PromptsError("The prompts file is not UTF-8 text.") from exc
    _write_private(PROMPTS_FILE, data)
    meta = {"sha256": digest, "cases": len(cases), "imported_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    _write_private(PROMPTS_META, json.dumps(meta).encode())
    return {**meta, "sections": sections(cases)}


def load() -> tuple[list[PromptCase], str]:
    """The imported prompts and their sha256. A file changed since import is refused."""
    if not PROMPTS_FILE.exists():
        raise PromptsError("No prompts file yet. Use /corpus prompts import <path to your prompts .md>.")
    data = _read_limited(PROMPTS_FILE)
    digest = hashlib.sha256(data).hexdigest()
    meta = _read_json(PROMPTS_META)
    if digest != EXPECTED_SHA256 or (meta and meta.get("sha256") != digest):
        raise PromptsError("The prompts file changed since it was imported. Import it again.")
    return parse(data.decode("utf-8", errors="strict")), digest


def select(cases: list[PromptCase], selector: str) -> list[PromptCase]:
    words = selector.strip().lower().split()
    if words == ["all"]:
        return cases
    if len(words) == 1 and (batch := re.fullmatch(r"batch-(\d+)", words[0])):
        number, batches = int(batch.group(1)), -(-len(cases) // BATCH)
        if not 1 <= number <= batches:
            raise PromptsError(f"Use batch-1 through batch-{batches}.")
        return cases[(number - 1) * BATCH:number * BATCH]
    if len(words) == 2 and words[0] == "section" and words[1].isdigit():
        chosen = [c for c in cases if c.section_number == int(words[1])]
        if not chosen:
            raise PromptsError(f"There is no section {words[1]}. Sections run 1 to {max(c.section_number for c in cases)}.")
        return chosen
    if len(words) == 1 and (number := re.fullmatch(r"(?:p-)?(\d+)", words[0])):
        chosen = [c for c in cases if c.number == int(number.group(1))]
        if not chosen:
            raise PromptsError(f"There is no prompt {number.group(1)}.")
        return chosen
    raise PromptsError("Use /corpus prompts all, batch-N, section N, or a prompt number such as 17.")


# A case's class is its worst turn's (the harness's order, worst first).
SEVERITY = ("INTERRUPTED", "TIMEOUT", "ERROR_TURN", "EMPTY_ANSWER", "CONFIRMATION_DECLINED", "OK")


class PromptRecord(corpus.CaseRecord):
    """One prompts case. Alongside the saved-line fields every corpus export
    reads, each turn keeps the Notch evaluation harness's record shape
    (scripts/notch_evaluation/driver.py), so its transcripts viewer can read it."""

    def __init__(self, case: PromptCase, run_id: str):
        section = f"{case.section_number}. {case.section}" if case.section_number else case.section
        super().__init__({"id": case.id, "category": section,
                          "conversation": [{"role": "user", "content": case.text}]}, run_id, "normal")
        self.line.update(corpus="prompts", number=case.number, tag=case.tag, risk_tag=case.tag,
                         section=case.section, section_number=case.section_number,
                         turn_count=len(case.segments), turns=[], failure_class=None)
        self.turn: dict | None = None
        self._turn_started = 0.0

    def begin_turn(self, prompt: str) -> None:
        self.end_turn()
        self.line["answer"] = ""
        self._turn_started = time.monotonic()
        self.turn = {"prompt": prompt, "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                     "final_text": "", "model": None, "route": {"role": None, "source": None, "reason": None},
                     "tools": [], "confirmation": None, "events": [], "latency_s": None, "ttft_s": None,
                     "failure_class": None, "error": None,
                     # The export's names for the same request and answer.
                     "answer": ""}
        self.line["turns"].append(self.turn)

    def observe(self, event):
        super().observe(event)
        turn, kind = self.turn, event.get("type")
        if turn is None or kind in ("heartbeat", "corpus_session"):
            return
        if kind == "routed":
            turn["model"] = event.get("model")
            turn["route"] = {"role": event.get("role"), "source": event.get("route_source"),
                             "reason": event.get("reason")}
        elif kind == "tool_call":
            turn["tools"].append({"id": str(event.get("id", "")), "name": event.get("name"),
                                  "args": event.get("args") if isinstance(event.get("args"), dict) else {},
                                  "decision": event.get("decision"), "result": ""})
        elif kind == "tool_result":
            for tool in reversed(turn["tools"]):
                if tool["id"] == str(event.get("id", "")):
                    tool["result"] = str(event.get("result", ""))[:TOOL_RESULT]
                    break
        elif kind in ("delta", "text"):
            if turn["ttft_s"] is None:
                turn["ttft_s"] = round(time.monotonic() - self._turn_started, 2)
        elif kind == "corpus_declined":
            turn["confirmation"] = turn["confirmation"] or {"tool": event.get("tool"), "reason": event.get("reason")}
            for tool in reversed(turn["tools"]):
                if tool["id"] == str(event.get("id", "")):
                    tool["decision"] = "declined"
                    break
        elif kind == "error":
            turn["error"] = str(event.get("message", ""))
        # What the notch would have shown, in order; streamed text is the answer itself.
        if kind not in ("delta", "text") and len(turn["events"]) < TURN_EVENTS:
            entry = {k: v for k, v in event.items() if k != "result"}
            if "result" in event:
                entry["result"] = str(event["result"])[:EVENT_RESULT]
            turn["events"].append(entry)

    def end_turn(self, failure: str | None = None) -> None:
        turn = self.turn
        if turn is None:
            return
        self.turn = None
        turn["final_text"] = turn["answer"] = self.line["answer"]
        turn["latency_s"] = round(time.monotonic() - self._turn_started, 2)
        turn["failure_class"] = failure or ("ERROR_TURN" if turn["error"] else
                                            "EMPTY_ANSWER" if not turn["final_text"].strip() else
                                            "CONFIRMATION_DECLINED" if turn["confirmation"] else "OK")

    def finish(self, stopped=False, timed_out=False):
        self.end_turn("INTERRUPTED" if stopped else "TIMEOUT" if timed_out else None)
        classes = [t["failure_class"] for t in self.line["turns"] if t.get("failure_class")]
        self.line["failure_class"] = min(classes, key=SEVERITY.index) if classes else (
            "INTERRUPTED" if stopped else "ERROR_TURN")
        return super().finish(stopped)


def harness_record(line: dict) -> dict:
    """A saved line in the Notch evaluation harness's results.jsonl shape."""
    latencies = [t["latency_s"] for t in line.get("turns") or [] if isinstance(t.get("latency_s"), (int, float))]
    return {"case_id": line.get("number"), "risk_tag": line.get("risk_tag") or line.get("tag"),
            "section": line.get("section", ""), "turn_count": line.get("turn_count"),
            "failure_class": line.get("failure_class"), "model": line.get("model") or None,
            "latency_s": round(sum(latencies), 2) if latencies else line.get("latency_s"),
            "recorded_at": line.get("started_at"), "label": line.get("run_id"),
            "session_id": line.get("session_id"),
            "turns": [{k: v for k, v in turn.items() if k != "answer"} for turn in line.get("turns") or []],
            "correctness": "UNVERIFIED"}


class _Job:
    """The one running job in this service, if any."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.current: dict | None = None
        self.waiting = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


_job = _Job()


def _save_state(state: dict) -> None:
    _write_private(JOB_FILE, json.dumps(state).encode())


# Parsed day files, keyed by path and (size, mtime): the notch polls status.
_day_cache: dict[Path, tuple[tuple[int, int], list[dict]]] = {}


def _day(path: Path, directory: Path) -> list[dict]:
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _day_cache.get(path)
    if cached is None or cached[0] != key:
        cached = (key, corpus.read_day(path.stem, directory))
        _day_cache[path] = cached
    return cached[1]


def run_lines(run_id: str, directory: Path | None = None) -> dict[str, dict]:
    """The latest saved line per case of one run, across the days it spans."""
    directory = directory or corpus.RUNS_DIR
    latest: dict[str, dict] = {}
    try:
        paths = sorted(p for p in directory.glob("*.jsonl") if corpus._DATE.fullmatch(p.stem))
    except OSError:
        return latest
    for path in paths:
        for line in _day(path, directory):
            if line.get("run_id") == run_id:
                latest[line["case_id"]] = line
    return latest


def _finished(line: dict) -> bool:
    # A stopped or cut-off case runs again on resume; a done or failed one does not.
    return line.get("status") in ("done", "error")


async def _wait_until_idle() -> None:
    """Never start a case under the user's own turn: it may swap models."""
    from service import idle
    quiet_since = None
    while True:
        if idle.foreground_busy():
            quiet_since = None
            _job.waiting = True
        else:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= IDLE_SETTLE_S:
                _job.waiting = False
                return
        await asyncio.sleep(IDLE_POLL_S if quiet_since is None else min(IDLE_POLL_S, IDLE_SETTLE_S))


async def run_case(case: PromptCase, run_id: str) -> dict:
    """One case, every turn in one chat, saved once whatever ends it."""
    record = PromptRecord(case, run_id)
    _job.current = {"case_id": case.id, "section": record.line["category"], "tag": case.tag,
                    "prompt": corpus._one_line(case.text, 300), "turns": len(case.segments), "turn": 0,
                    "started": time.time()}

    async def emit(event):
        try:
            record.observe(event)
        except Exception as exc:
            log.warning("Prompts event not recorded (%s)", type(exc).__name__)

    stopped = timed_out = False
    try:
        title = corpus.case_title({"case_id": case.id, "prompt": case.text})
        # Turn 1 starts a new chat; later turns are follow-ups in it.
        sid = await corpus.open_session(title, emit)
        for number, segment in enumerate(case.segments, 1):
            _job.current["turn"] = number
            record.begin_turn(segment)
            async with asyncio.timeout(TURN_TIMEOUT_S):
                sid = await corpus.agent_turn(segment, sid, emit, title)
            record.end_turn()
        await emit({"type": "done"})
    except asyncio.CancelledError:
        stopped = True
        raise
    except TimeoutError:
        timed_out = True
        await emit({"type": "error", "message": f"This prompt took longer than {TURN_TIMEOUT_S // 60} minutes."})
    except Exception as exc:
        # Provider errors can contain request bodies: the type only.
        await emit({"type": "error", "message": f"This prompt could not finish ({type(exc).__name__})."})
    finally:
        line = record.finish(stopped=stopped, timed_out=timed_out)
        corpus.save_session(line)
        corpus.persist(line)
        _job.current = None
    return line


async def _run(state: dict) -> None:
    try:
        cases, digest = load()
        if digest != state["corpus_sha256"]:
            raise PromptsError("The prompts file changed since this run started. Start a new run.")
        by_id = {c.id: c for c in cases}
        done = {cid for cid, line in run_lines(state["run_id"]).items() if _finished(line)}
        for case_id in state["case_ids"]:
            if case_id in done or case_id not in by_id:
                continue
            await _wait_until_idle()
            async with corpus._RUN_LOCK:
                await run_case(by_id[case_id], state["run_id"])
        state.update(status="finished", ended_at=time.time())
    except asyncio.CancelledError:
        state.update(status="stopped", ended_at=time.time())
        raise
    except Exception as exc:
        log.warning("Prompts run stopped (%s)", type(exc).__name__)
        state.update(status="failed", ended_at=time.time(),
                     error=str(exc) if isinstance(exc, PromptsError) else type(exc).__name__)
    finally:
        _job.waiting = False
        _save_state(state)


def _launch(state: dict) -> None:
    state.update(status="running", error="")
    _save_state(state)
    _job.task = asyncio.get_running_loop().create_task(_run(state))


def status() -> dict:
    meta = _read_json(PROMPTS_META)
    imported = None
    if meta:
        try:
            cases, _ = load()
            imported = {**meta, "sections": sections(cases)}
        except (PromptsError, OSError, UnicodeDecodeError) as exc:
            imported = {**meta, "error": str(exc)}
    state = _read_json(JOB_FILE)
    if not state:
        return {"imported": imported, "run": None}
    wanted = set(state["case_ids"])
    lines = [line for cid, line in run_lines(state["run_id"]).items() if cid in wanted]
    finished = [line for line in lines if _finished(line)]
    ok = sum(line["status"] == "done" and not line.get("confirmations") for line in finished)
    declined = sum(line["status"] == "done" and bool(line.get("confirmations")) for line in finished)
    errors = sum(line["status"] == "error" for line in finished)
    total = len(state["case_ids"])
    remaining = total - len(finished)
    times = [line["latency_s"] for line in finished if isinstance(line.get("latency_s"), (int, float))]
    run_status = state.get("status", "stopped")
    if run_status == "running" and not _job.running:
        run_status = "interrupted"   # the service restarted mid-run
    current = dict(_job.current) if _job.current and _job.running else None
    if current:
        current["elapsed_s"] = round(time.time() - current.pop("started"), 1)
    return {"imported": imported, "run": {
        "run_id": state["run_id"], "selector": state.get("selector", ""), "status": run_status,
        "started_at": state.get("started_at", ""), "error": state.get("error", ""),
        "total": total, "completed": len(finished), "ok": ok, "declined": declined, "errors": errors,
        "remaining": remaining, "eta_s": round(sum(times) / len(times) * remaining) if times and remaining else None,
        "current": current, "waiting_for_idle": _job.waiting and _job.running}}


def _http(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/status")
def status_route():
    return status()


@router.post("/import")
def import_route(body: dict):
    if _job.running:
        raise HTTPException(409, "Stop the prompts run before importing a new file.")
    try:
        return {"imported": import_file(str(body.get("path") or ""))}
    except PermissionError as exc:
        # macOS privacy: Desktop and Documents need Wisp's own access grant.
        raise HTTPException(400, "Wisp isn't allowed to read that folder. Move the file to Downloads, "
                                 "or allow Wisp in System Settings › Privacy & Security › Files and Folders.") from exc
    except (PromptsError, OSError) as exc:
        raise _http(exc) from exc


@router.post("/start")
async def start_route(body: dict):
    if _job.running:
        raise HTTPException(409, "A prompts run is already going. Use /corpus prompts stop first.")
    try:
        cases, digest = load()
        chosen = select(cases, str(body.get("selector") or "all"))
    except (PromptsError, OSError, UnicodeDecodeError) as exc:
        raise _http(exc) from exc
    _launch({"run_id": f"prompts-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
             "selector": " ".join(str(body.get("selector") or "all").lower().split()),
             "corpus_sha256": digest, "case_ids": [c.id for c in chosen],
             "started_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    return status()


@router.post("/resume")
async def resume_route():
    if _job.running:
        return status()
    state = _read_json(JOB_FILE)
    if not state:
        raise HTTPException(400, "There is no prompts run to resume. Start one with /corpus prompts batch-1.")
    _launch(state)
    return status()


@router.post("/stop")
async def stop_route():
    task = _job.task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return status()


def _results(run_id: str | None, limit: int, offset: int = 0) -> list[dict]:
    state = _read_json(JOB_FILE) or {}
    run_id = run_id or state.get("run_id")
    if not run_id or not corpus._RUN_ID.fullmatch(run_id):
        return []
    lines = run_lines(run_id)
    order = state.get("case_ids") if state.get("run_id") == run_id else None
    ordered = [lines[cid] for cid in order if cid in lines] if order else list(lines.values())
    return ordered[offset:offset + limit] if limit else ordered[offset:]


@router.get("/results")
def results_route(run_id: str | None = None, limit: int = 50, newest_first: bool = True):
    lines = _results(run_id, 0)
    if newest_first:
        lines = sorted(lines, key=lambda line: line.get("started_at", ""), reverse=True)
    return {"results": lines[:max(1, min(200, limit))]}


def _export_lines(run_id):
    lines = _results(run_id, 0)
    if not lines:
        raise HTTPException(404, "No prompts results yet.")
    return lines


@router.get("/export.md")
def export_md(run_id: str | None = None):
    return PlainTextResponse(corpus.export_markdown(_export_lines(run_id)), media_type="text/markdown; charset=utf-8")


@router.get("/export.jsonl")
def export_jsonl(run_id: str | None = None):
    """The run in the harness's results.jsonl shape (its transcripts viewer reads it)."""
    body = "".join(json.dumps(harness_record(line), ensure_ascii=False) + "\n" for line in _export_lines(run_id))
    return PlainTextResponse(body, media_type="application/x-ndjson; charset=utf-8")


@router.get("/export.csv")
def export_csv(run_id: str | None = None):
    return PlainTextResponse(corpus.export_csv(_export_lines(run_id)), media_type="text/csv; charset=utf-8")
