"""The prompts corpus job, on a SYNTHETIC prompts file in tmp. The user's own
file is private and never read, copied or embedded here."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from service import corpus, corpus_prompts
from service.agent import loop
from service.agent.approver import InteractiveApprover
from service.safety import grants, policy
from service.tools import action_tools, assistant_tools  # noqa: F401  register before fakes
from service.tools.registry import REGISTRY

SYNTHETIC = """# Synthetic prompts

Count: 6 prompts. Tags: `[R]` read, `[W]` write, `[S]` send. Multi-turn entries join turns with " → ".

## 1. Reading (3)

1. `[R]` What is on the fixture calendar today?
2. `[R]` Summarise the fixture inbox – the "important" part.
3. `[R]` Who is Example Person? → And when did we last talk?

## 2. Changes "and sends" (3)

4. `[W]` Remind me to water the fixture plant.
5. `[S]` Email person@example.com that the fixture is ready.
6. `[W]` Add a fixture reminder → Actually also email person@example.com about it.
"""


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    """Every path the job writes is under tmp_path, never ~/.moe."""
    monkeypatch.setattr(corpus_prompts, "PROMPTS_FILE", tmp_path / "moe" / "corpus-prompts-1000.md")
    monkeypatch.setattr(corpus_prompts, "PROMPTS_META", tmp_path / "moe" / "corpus-prompts.json")
    monkeypatch.setattr(corpus_prompts, "JOB_FILE", tmp_path / "moe" / "corpus-prompts-run.json")
    monkeypatch.setattr(corpus_prompts, "IDLE_SETTLE_S", 0.0)
    monkeypatch.setattr(corpus_prompts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(corpus_prompts, "_day_cache", {})
    monkeypatch.setattr(corpus_prompts, "_job", corpus_prompts._Job())
    # The reviewed corpus is the user's private file; these tests verify a synthetic one.
    import hashlib
    monkeypatch.setattr(corpus_prompts, "EXPECTED_SHA256", hashlib.sha256(SYNTHETIC.encode()).hexdigest())
    monkeypatch.setattr(corpus_prompts, "EXPECTED_CASES", 6)
    monkeypatch.setattr(corpus, "RUNS_DIR", tmp_path / "corpus-runs")
    source = tmp_path / "Desktop" / "my prompts.md"
    source.parent.mkdir()
    source.write_text(SYNTHETIC, encoding="utf-8")
    return source


@pytest.fixture
def chats(tmp_path, monkeypatch):
    from service.memory.store import SessionStore
    store = SessionStore(tmp_path / "sessions.db")
    monkeypatch.setattr(corpus, "_session_store", lambda: store)
    return store


def test_parse_reads_sections_tags_and_turns():
    cases = corpus_prompts.parse(SYNTHETIC)
    assert [c.id for c in cases] == ["P-1", "P-2", "P-3", "P-4", "P-5", "P-6"]
    assert [c.tag for c in cases] == ["R", "R", "R", "W", "S", "W"]
    assert cases[2].segments == ("Who is Example Person?", "And when did we last talk?")
    assert cases[0].segments == ("What is on the fixture calendar today?",)
    # An en dash is text, not a separator; quotes survive.
    assert cases[1].segments == ('Summarise the fixture inbox – the "important" part.',)
    assert (cases[3].section_number, cases[3].section) == (2, 'Changes "and sends"')
    assert corpus_prompts.sections(cases) == [{"number": 1, "title": "Reading", "count": 3},
                                              {"number": 2, "title": 'Changes "and sends"', "count": 3}]
    for bad in ("1. `[R]` a\n1. `[R]` b", "1. `[R]` a →  → b", "no prompts here", "2. `[R]` b\n1. `[R]` a",
                "1. `[R]` a\n3. `[R]` c"):
        with pytest.raises(corpus_prompts.PromptsError):
            corpus_prompts.parse(bad, expected_cases=2)
    # Exactly 1..EXPECTED_CASES: a file one short is refused.
    with pytest.raises(corpus_prompts.PromptsError, match="1 to 6"):
        corpus_prompts.parse(SYNTHETIC.replace("6. `[W]`", "x. `[W]`"))


def test_import_copies_privately_and_a_changed_file_is_refused(private):
    imported = corpus_prompts.import_file(f'"{private}"')
    copy = corpus_prompts.PROMPTS_FILE
    assert copy.read_text(encoding="utf-8") == SYNTHETIC
    assert oct(copy.stat().st_mode & 0o777) == "0o600"
    assert oct(corpus_prompts.PROMPTS_META.stat().st_mode & 0o777) == "0o600"
    import hashlib
    assert imported["sha256"] == hashlib.sha256(SYNTHETIC.encode()).hexdigest() and imported["cases"] == 6
    assert json.loads(corpus_prompts.PROMPTS_META.read_text())["sha256"] == imported["sha256"]
    cases, digest = corpus_prompts.load()
    assert len(cases) == 6 and digest == imported["sha256"]
    link = private.with_name("link.md")
    os.symlink(private, link)
    corpus_prompts.import_file(str(link))  # resolved to the real file, then read
    copy.write_text(SYNTHETIC + "7. `[R]` One more.\n", encoding="utf-8")
    with pytest.raises(corpus_prompts.PromptsError, match="changed since it was imported"):
        corpus_prompts.load()
    # Only the reviewed corpus imports: its sha256 is checked before parsing.
    other = private.with_name("other.md")
    other.write_text(SYNTHETIC.replace("fixture plant", "fixture tree"), encoding="utf-8")
    with pytest.raises(corpus_prompts.PromptsError, match="not the reviewed prompts corpus"):
        corpus_prompts.import_file(str(other))
    with pytest.raises(corpus_prompts.PromptsError):
        corpus_prompts.import_file(str(private.with_name("missing.md")))


def test_selectors():
    many = "\n".join(f"{n}. `[R]` Prompt {n}" for n in range(1, 251))
    cases = corpus_prompts.parse("## 1. A (250)\n" + many, expected_cases=250)
    assert len(corpus_prompts.select(cases, "all")) == 250
    assert [c.number for c in corpus_prompts.select(cases, "batch-3")] == list(range(201, 251))
    assert [c.number for c in corpus_prompts.select(cases, "P-17")] == [17]
    assert [c.number for c in corpus_prompts.select(cases, "17")] == [17]
    synthetic = corpus_prompts.parse(SYNTHETIC)
    assert [c.id for c in corpus_prompts.select(synthetic, "section 2")] == ["P-4", "P-5", "P-6"]
    for bad in ("batch-4", "batch-0", "section 9", "999", "everything"):
        with pytest.raises(corpus_prompts.PromptsError):
            corpus_prompts.select(cases, bad)


class ScriptedModel:
    """Tries a change or a send for W and S prompts, then answers."""

    def __init__(self, prompt):
        self.prompt, self.steps = prompt, 0

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.steps += 1
        call = None
        if self.steps == 1 and "email" in self.prompt.lower():
            call = {"id": "s1", "function": {"name": "send_email", "arguments": json.dumps(
                {"to": "person@example.com", "subject": "Fixture", "body": "It is ready."})}}
        elif self.steps == 1 and ("remind" in self.prompt.lower() or "reminder" in self.prompt.lower()):
            call = {"id": "w1", "function": {"name": "add_reminder", "arguments": json.dumps(
                {"title": "Fixture plant", "when_iso": "2036-09-28T18:00:00"})}}
        content = "" if call else f"Answer to: {self.prompt}\n\nSecond paragraph."
        yield {"kind": "final", "message": {"role": "assistant", "content": content,
                                            "tool_calls": [call] if call else None}}


@pytest.fixture
def wisp(monkeypatch, chats, tmp_path):
    """A stand-in /agent over the REAL agent loop, policy and approver, with
    every effect tool replaced by a recorder that must never run."""
    executed, bodies = [], []
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(grants, "check", lambda tool, args: "allow")  # "always allow" everything
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)
    for name in ("send_email", "add_reminder"):
        def effect(*, _name=name, **args):
            executed.append(_name)
            return "done"
        monkeypatch.setitem(REGISTRY, name, replace(REGISTRY[name], func=effect))

    async def agent(body):
        bodies.append(deepcopy(body))
        sid = body["session_id"]

        async def frames():
            yield f"data: {json.dumps({'type': 'session', 'id': sid})}\n\n"
            yield 'data: {"type": "routed", "model": "fixture-model", "role": "agent", "route_source": "router"}\n\n'
            events = []

            async def emit(event):
                events.append(deepcopy(event))
            approver = InteractiveApprover(emit, timeout=1, request_id="r")
            await loop.run_agent(ScriptedModel(body["prompt"]), "fixture-model",
                                 [{"role": "user", "content": body["prompt"]}], emit, approver,
                                 tools=["send_email", "add_reminder", "read_file"],
                                 max_steps=3, include_memory_context=False, debug=False)
            for event in events:
                if event.get("type") != "done":
                    yield f"data: {json.dumps(event)}\n\n"
            yield 'data: {"type": "done"}\n\n'
            answer = "".join(e.get("text", "") for e in events if e.get("type") in ("delta", "text"))
            chats.add_turn(sid, "user", body["prompt"])
            chats.add_turn(sid, "assistant", answer or "(no answer)")
        return SimpleNamespace(body_iterator=frames())

    monkeypatch.setitem(sys.modules, "service.main", SimpleNamespace(agent=agent))
    return SimpleNamespace(executed=executed, bodies=bodies)


async def _until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_a_run_declines_every_effect_and_saves_each_case_as_an_excluded_chat(private, wisp, chats):
    corpus_prompts.import_file(str(private))

    async def run():
        await corpus_prompts.start_route({"selector": "all"})
        await corpus_prompts._job.task
    asyncio.run(run())
    assert wisp.executed == []  # no send or change ran, under full access and a grant
    status = corpus_prompts.status()["run"]
    assert status["status"] == "finished" and status["total"] == status["completed"] == 6
    assert (status["ok"], status["declined"], status["errors"]) == (3, 3, 0)
    lines = corpus_prompts.run_lines(status["run_id"])
    assert lines["P-5"]["confirmations"][0]["tool"] == "send_email"
    assert lines["P-4"]["confirmations"][0]["tool"] == "add_reminder"
    assert {c["tool"] for c in lines["P-6"]["confirmations"]} == {"add_reminder", "send_email"}
    assert lines["P-1"]["confirmations"] == [] and lines["P-1"]["mode"] == "normal"
    assert (lines["P-5"]["tag"], lines["P-5"]["category"]) == ("S", '2. Changes "and sends"')
    # Each turn keeps the harness's record shape.
    import hashlib
    turn = lines["P-5"]["turns"][0]
    assert turn["input_sha256"] == hashlib.sha256(turn["prompt"].encode()).hexdigest()
    assert turn["failure_class"] == "CONFIRMATION_DECLINED" and lines["P-5"]["failure_class"] == "CONFIRMATION_DECLINED"
    assert turn["confirmation"]["tool"] == "send_email" and turn["model"] == "fixture-model"
    assert turn["route"] == {"role": "agent", "source": "router", "reason": None}
    assert [(t["name"], t["decision"]) for t in turn["tools"]] == [("send_email", "declined")]
    assert isinstance(turn["latency_s"], float) and isinstance(turn["ttft_s"], float)
    assert any(e["type"] == "corpus_declined" for e in turn["events"])
    assert not any(e["type"] in ("delta", "text") for e in turn["events"])
    assert lines["P-1"]["failure_class"] == lines["P-1"]["turns"][0]["failure_class"] == "OK"
    # A multi-turn case is one chat, turn by turn, with every answer.
    multi = lines["P-3"]
    assert [t["prompt"] for t in multi["turns"]] == ["Who is Example Person?", "And when did we last talk?"]
    assert multi["turns"][1]["answer"].startswith("Answer to: And when did we last talk?")
    assert [b["prompt"] for b in wisp.bodies if b["session_id"] == multi["session_id"]] == [
        "Who is Example Person?", "And when did we last talk?"]
    listed = {c["id"]: c for c in chats.list_chats()}
    assert len(listed) == 6 and listed[multi["session_id"]]["turn_count"] == 4
    assert listed[multi["session_id"]]["title"].startswith("Corpus · P-3 · ")
    # Excluded from memory, behaviour and search, as every corpus chat is.
    from service.memory.queue import MemoryQueue
    with chats._lock:
        assert chats._db.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 0
    assert all(chats.is_corpus_session(sid) for sid in listed)
    assert MemoryQueue(chats).next() is None
    # Export: full answers, tools, route, model, latency, section and tag.
    markdown = corpus_prompts.export_md().body.decode()
    assert "## P-3 · 1. Reading · done" in markdown and "**Turn 2:** And when did we last talk?" in markdown
    assert "> Answer to: And when did we last talk?\n>\n> Second paragraph." in markdown
    assert "· **Tag:** S" in markdown and "Would ask to confirm `send_email`" in markdown
    import csv, io
    rows = list(csv.reader(io.StringIO(corpus_prompts.export_csv().body.decode())))
    column = {name: i for i, name in enumerate(rows[0])}
    by_case = {row[column["case_id"]]: row for row in rows[1:]}
    assert [row[column["case_id"]] for row in rows[1:]] == [f"P-{n}" for n in range(1, 7)]
    assert by_case["P-3"][column["turns"]] == "2" and "Turn 2: Answer to: And when" in by_case["P-3"][column["answer"]]
    assert by_case["P-5"][column["tag"]] == "S" and by_case["P-5"][column["would_confirm"]] == "send_email"
    assert by_case["P-1"][column["route_source"]] == "router" and by_case["P-1"][column["model"]] == "fixture-model"
    assert by_case["P-1"][column["latency_s"]] != ""
    harness = [json.loads(row) for row in corpus_prompts.export_jsonl().body.decode().splitlines()]
    assert [r["case_id"] for r in harness] == [1, 2, 3, 4, 5, 6]
    assert harness[2]["risk_tag"] == "R" and harness[2]["section"] == "Reading" and harness[2]["turn_count"] == 2
    assert [t["final_text"].split("\n")[0] for t in harness[2]["turns"]] == [
        "Answer to: Who is Example Person?", "Answer to: And when did we last talk?"]
    assert set(harness[4]["turns"][0]) >= {"prompt", "input_sha256", "final_text", "model", "route", "tools",
                                           "events", "latency_s", "ttft_s", "failure_class"}
    results = corpus_prompts.results_route(limit=2)["results"]
    assert len(results) == 2 and results[0]["answer"]


def test_resume_skips_finished_cases_and_stop_leaves_the_rest(private, wisp, monkeypatch):
    corpus_prompts.import_file(str(private))
    gate = asyncio.Event()
    real = corpus.agent_turn

    async def held(prompt, sid, emit, title):
        if "inbox" in prompt:
            await gate.wait()  # P-2 never finishes before stop
        return await real(prompt, sid, emit, title)
    monkeypatch.setattr(corpus, "agent_turn", held)

    async def first():
        await corpus_prompts.start_route({"selector": "section 1"})
        await _until(lambda: (corpus_prompts.status()["run"]["current"] or {}).get("case_id") == "P-2")
        stopped = await corpus_prompts.stop_route()
        return stopped["run"]
    stopped = asyncio.run(first())
    assert stopped["status"] == "stopped" and stopped["completed"] == 1 and stopped["total"] == 3
    lines = corpus_prompts.run_lines(stopped["run_id"])
    assert lines["P-1"]["status"] == "done" and lines["P-2"]["status"] == "stopped" and "P-3" not in lines
    # A restart: a saved "running" state without a task reads as interrupted.
    state = json.loads(corpus_prompts.JOB_FILE.read_text())
    state["status"] = "running"
    corpus_prompts.JOB_FILE.write_text(json.dumps(state))
    assert corpus_prompts.status()["run"]["status"] == "interrupted"
    gate.set()
    started = len(wisp.bodies)

    async def resume():
        await corpus_prompts.resume_route()
        await corpus_prompts._job.task
    asyncio.run(resume())
    run = corpus_prompts.status()["run"]
    assert run["status"] == "finished" and run["completed"] == 3 and run["run_id"] == stopped["run_id"]
    # P-1 was not run again; P-2 (stopped) and P-3 were.
    assert [b["prompt"] for b in wisp.bodies[started:]] == [
        'Summarise the fixture inbox – the "important" part.', "Who is Example Person?", "And when did we last talk?"]
    assert lines["P-2"]["failure_class"] == "INTERRUPTED"


def test_the_job_waits_for_the_users_turn(private, wisp, monkeypatch):
    from service import idle
    corpus_prompts.import_file(str(private))
    busy = {"on": True}
    monkeypatch.setattr(idle, "foreground_busy", lambda: busy["on"])

    async def run():
        await corpus_prompts.start_route({"selector": "P-1"})
        await _until(lambda: corpus_prompts.status()["run"]["waiting_for_idle"])
        await asyncio.sleep(0.1)
        assert wisp.bodies == []  # nothing started under the user's turn
        busy["on"] = False
        await corpus_prompts._job.task
    asyncio.run(run())
    assert [b["prompt"] for b in wisp.bodies] == ["What is on the fixture calendar today?"]


def test_start_and_import_refuse_while_running_and_without_a_file(private, wisp, monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match="No prompts file"):
        asyncio.run(corpus_prompts.start_route({"selector": "all"}))
    with pytest.raises(HTTPException, match="no prompts run to resume"):
        asyncio.run(corpus_prompts.resume_route())
    corpus_prompts.import_file(str(private))
    gate = asyncio.Event()

    async def held(*args):
        await gate.wait()
    monkeypatch.setattr(corpus, "agent_turn", held)

    async def run():
        await corpus_prompts.start_route({"selector": "all"})
        with pytest.raises(HTTPException, match="already going"):
            await corpus_prompts.start_route({"selector": "all"})
        with pytest.raises(HTTPException, match="Stop the prompts run"):
            corpus_prompts.import_route({"path": str(private)})
        await corpus_prompts.stop_route()
    asyncio.run(run())


def test_import_explains_a_folder_wisp_may_not_read(private, monkeypatch):
    from fastapi import HTTPException

    def denied(path):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(corpus_prompts, "_read_limited", denied)
    with pytest.raises(HTTPException, match="Files and Folders"):
        corpus_prompts.import_route({"path": str(private)})
    assert not corpus_prompts.PROMPTS_FILE.exists()
