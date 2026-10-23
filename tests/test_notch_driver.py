"""The corpus parser and the resumable Notch driver, against synthetic inputs.

A SYNTHETIC corpus and a fake input adapter on a real Unix socket stand in
for the user's corpus and the evaluation app. Nothing here reads the private
corpus, contacts an engine or starts the app.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from pathlib import Path

import pytest

from scripts.notch_evaluation import corpus, driver
from scripts.notch_evaluation.host import CaseLedger

SYNTHETIC = """# SYNTHETIC corpus

**Count.** 6 prompts. Multi-turn items use `→` between turns.

## 1. Section

1. `[R]` What is SYNTHETIC-A?
2. `[W]` Make a SYNTHETIC note.
3. `[R]` Show SYNTHETIC-B → No, the other one.
4. `[S]` Send SYNTHETIC-C to someone.
5. `[R]` What is SYNTHETIC-D?
6. `[R]` What is SYNTHETIC-E?
"""


def _corpus(tmp: Path, text: str = SYNTHETIC) -> tuple[Path, str]:
    path = tmp / "corpus.md"
    path.write_text(text, encoding="utf-8")
    return path, hashlib.sha256(text.encode()).hexdigest()


def test_corpus_parses_tags_turns_and_refuses_another_corpus(tmp_path):
    path, sha = _corpus(tmp_path)
    cases = corpus.parse(path, expected_sha256=sha, expected_cases=6)
    assert [c.risk_tag for c in cases] == ["R", "W", "R", "S", "R", "R"]
    assert cases[2].segments == ("Show SYNTHETIC-B", "No, the other one.")
    assert {c.section for c in cases} == {"Section"}
    with pytest.raises(corpus.CorpusError, match="digest"):
        corpus.parse(path, expected_sha256="0" * 64, expected_cases=6)
    (tmp_path / "broken").mkdir()
    broken, broken_sha = _corpus(tmp_path / "broken", SYNTHETIC.replace("5. `[R]`", "7. `[R]`"))
    with pytest.raises(corpus.CorpusError, match="ids"):
        corpus.parse(broken, expected_sha256=broken_sha, expected_cases=6)


def test_initialize_keeps_text_private_and_the_ledger_hash_only(tmp_path):
    path, sha = _corpus(tmp_path)
    run = driver.private_dir(tmp_path / "run")
    cases = corpus.parse(path, expected_sha256=sha, expected_cases=6)
    assert corpus.initialize(run, cases, corpus_sha256=sha)["initialized"]
    for name in ("cases.json", "ledger.json"):
        assert oct((run / name).stat().st_mode & 0o777) == "0o600"
    assert "SYNTHETIC" not in (run / "ledger.json").read_text()
    ledger = CaseLedger(run / "ledger.json", corpus_sha256=sha, expected_cases=6)
    assert ledger.segments(ledger.by_id[3]) == [corpus.sha256_text("Show SYNTHETIC-B"),
                                                 corpus.sha256_text("No, the other one.")]
    assert not corpus.initialize(run, cases, corpus_sha256=sha)["initialized"]   # resume leaves it alone
    assert corpus.write_sections(run, cases) and not corpus.write_sections(run, cases)
    assert json.loads((run / "sections.json").read_text())["3"] == "Section"
    assert oct((run / "sections.json").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(corpus.CorpusError):
        corpus.initialize(run, cases, corpus_sha256="f" * 64)


class FakeAdapter:
    """The app's input adapter, scripted per case turn.

    Served over an in-memory socketpair per connection: the simulation
    sandbox refuses binding a socket, and the wire format is the same.
    """

    def __init__(self, token: str, script):
        self.token, self.script, self.seen = token, script, []

    def connect(self) -> socket.socket:
        client, server = socket.socketpair()
        threading.Thread(target=self._serve, args=(server,), daemon=True).start()
        return client

    def _serve(self, conn):
        with conn:
            request = json.loads(conn.recv(1 << 16))
            assert request["token"] == self.token
            self.seen.append(request)
            reply = {"status": "ready"} if request["op"] == "ping" else self.script(request)
            conn.sendall((json.dumps(reply) + "\n").encode())


def _terminal(request, text="SYNTHETIC answer", **extra):
    return {"status": "terminal", "outcome": "done", "text": text, "dispatch": len(request["case"]),
            "input_sha256": corpus.sha256_text(request["text"]), "model": "SYNTH-MODEL",
            "response_models": ["SYNTH-MODEL"], "route_role": "agent", "route_source": "rules",
            "tools": [], "started_at": 100.0, "first_token_at": 100.5, "finished_at": 102.0,
            "session": "s1", "trace_id": "t1",
            "events": [{"type": "status", "text": "SYNTHETIC status"},
                       {"type": "tool_call", "id": "c1", "name": "search_notes", "args": "{}", "decision": "allow"},
                       {"type": "tool_result", "id": "c1", "result": {"text": "SYNTHETIC result"}}],
            "activity": ["search_notes — ok"], **extra}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path, sha = _corpus(tmp_path)
    run = driver.private_dir(tmp_path / "run")
    corpus.initialize(run, corpus.parse(path, expected_sha256=sha, expected_cases=6), corpus_sha256=sha)
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    monkeypatch.chdir(owned)                    # as run.sh does
    (owned / "host-config.json").write_text(json.dumps({"token": "T" * 40}))
    resident = {"value": True}
    monkeypatch.setattr(driver, "engine_resident", lambda engine, model, **_: resident["value"])
    adapters = []

    def connect(root, timeout):
        assert Path(root) == owned
        if not adapters:
            raise FileNotFoundError(driver.SOCKET)      # the app has not bound its socket yet
        return adapters[-1].connect()
    monkeypatch.setattr(driver, "_connect", connect)

    def start(script):
        adapters.append(FakeAdapter("T" * 40, script))
        return adapters[-1]

    def go(*extra):
        return driver.main(["run", "--run-dir", str(run), "--owned", str(owned), "--engine", "http://127.0.0.1:1",
                            "--model", "SYNTH-MODEL", "--timeout", "5", "--ready-timeout", "0.3", *extra])

    yield run, start, go, resident


def _records(run: Path) -> list[dict]:
    return driver.read_results(run)


def test_driver_records_every_class_and_resumes(harness):
    run, start, go, resident = harness
    timeouts = {"count": 0}

    def script(request):
        case = int(request["case"].split("-")[0])
        if case == 2:     # [W]: the card is declined, the effect never runs
            return _terminal(request, confirmation={"tool": "create_note", "declined": True})
        if case == 4:     # [S]: the boundary refuses the send
            return _terminal(request, tools=[{"name": "send_message", "decision": "allow",
                                              "result": "(error: EVALUATION_BLOCKED — not run)"}])
        if case == 5:
            timeouts["count"] += 1
            return {"status": "timeout", "case": request["case"]}
        if case == 6:
            return {**_terminal(request), "input_sha256": "0" * 64}
        return _terminal(request)

    adapter = start(script)
    assert go() == 0
    records = {r["case_id"]: r for r in _records(run)}
    assert records[1]["failure_class"] == "OK" and records[1]["turns"][0]["final_text"] == "SYNTHETIC answer"
    assert records[1]["model"] == "SYNTH-MODEL" and records[1]["latency_s"] == 2.0
    shown = records[1]["turns"][0]
    assert [e["type"] for e in shown["events"]] == ["status", "tool_call", "tool_result"]
    assert shown["activity"] == ["search_notes — ok"]
    page = run / "transcripts.html"                        # regenerated after each case
    assert page.exists() and oct(page.stat().st_mode & 0o777) == "0o600"
    assert "SYNTHETIC answer" in page.read_text(encoding="utf-8")
    assert records[2]["failure_class"] == "CONFIRMATION_DECLINED"
    assert records[3]["turns_delivered"] == 2
    asks = [r for r in adapter.seen if r["op"] == "ask" and r["case"].startswith("3-")]
    assert [a["new_chat"] for a in asks] == [True, False]       # one conversation per case
    assert records[4]["failure_class"] == "EFFECT_BLOCKED"
    assert records[5]["failure_class"] == "TIMEOUT" and records[6]["failure_class"] == "INPUT_MISMATCH"
    assert oct((run / driver.RESULTS).stat().st_mode & 0o777) == "0o600"
    # Resume: only the retryable timeout runs again, then it is final.
    assert go() == 0
    assert timeouts["count"] == 2 and len(_records(run)) == 7
    assert go() == 0 and len(_records(run)) == 7
    state = driver.case_state(_records(run))
    assert all(entry["done"] for entry in state.values()) and len(state) == 6


def test_engine_not_ready_stops_and_resume_continues(harness):
    run, start, go, resident = harness
    start(_terminal)
    resident["value"] = False
    assert go("--canary", "2") == 3                       # stops at the first case, records it
    assert [r["failure_class"] for r in _records(run)] == ["ENGINE_NOT_READY"]
    resident["value"] = True
    assert go("--canary", "2") == 0
    done = [r for r in _records(run) if r["failure_class"] == "OK"]
    assert [r["case_id"] for r in done] == [1, 5]          # canary = single-turn [R] only


def test_unreachable_app_stops_before_any_case(harness):
    run, start, go, resident = harness
    assert go() == 4 and _records(run) == []


def test_run_directory_must_be_private(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    with pytest.raises(SystemExit):
        driver.private_dir(shared)


def test_driver_waits_for_the_app_socket_then_runs(harness):
    run, start, go, resident = harness
    timer = threading.Timer(0.6, lambda: start(_terminal))     # binds after the first pings miss
    timer.start()
    try:
        assert driver.main(["run", "--run-dir", str(run), "--owned", os.getcwd(), "--engine", "http://127.0.0.1:1",
                            "--model", "SYNTH-MODEL", "--timeout", "5", "--ready-timeout", "5",
                            "--canary", "1"]) == 0
    finally:
        timer.join()
    assert [r["failure_class"] for r in _records(run)] == ["OK"]


def test_wait_for_app_retries_only_a_missing_socket(tmp_path, monkeypatch):
    calls = []

    def missing(*_, **__):
        calls.append(1)
        raise FileNotFoundError
    monkeypatch.setattr(driver, "app_request", missing)
    ticks = iter(range(100))
    assert driver.wait_for_app(tmp_path, "t", 3, clock=lambda: next(ticks), nap=lambda _: None) is None
    assert len(calls) == 3                     # attempts at t=1, 2; t=3 is past the deadline

    def broken(*_, **__):
        calls.append(2)
        raise ConnectionResetError
    monkeypatch.setattr(driver, "app_request", broken)
    assert driver.wait_for_app(tmp_path, "t", 3) is None and calls[-1] == 2 and calls.count(2) == 1


def test_a_run_directory_holds_one_source_mode(tmp_path):
    run, owned = tmp_path / "run", tmp_path / "owned"
    run.mkdir(mode=0o700)
    owned.mkdir(mode=0o700)
    (owned / "host-config.json").write_text(json.dumps({"token": "t"}))
    assert driver.bind_source_mode(run, owned) == "snapshot"
    assert oct((run / driver.SOURCE_MODE).stat().st_mode & 0o777) == "0o600"
    assert driver.bind_source_mode(run, owned) == "snapshot"            # the same mode resumes
    (owned / "host-config.json").write_text(json.dumps({"token": "t", "source_mode": "live"}))
    with pytest.raises(ValueError, match="holds snapshot results"):
        driver.bind_source_mode(run, owned)
