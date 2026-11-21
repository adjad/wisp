"""Ideate's service bridge (service/ideate): projects, events, the gate, approvals, cancel,
one run at a time, the sandbox profile and the lean Pi profile.

No real Pi, Node or engine: the harness is a small Python stand-in (FAKE_PI below) that
speaks Pi's RPC JSONL on stdin/stdout, started through the real PiAdapter/PiSession code
with `contain` set to run it unwrapped (a sandbox cannot be applied inside another, e.g.
Simulation QA). The engine is a stand-in Residency. WISP_HOME and the project root are
under pytest's tmp_path. One test applies the real profile with sandbox-exec and is skipped
where a sandbox cannot be applied.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.ideate import api, config, engine, gate, harness, runner, sandbox, store

FAKE_PI = r'''
import json, os, sys, time
scenario = os.environ.get("FAKE_SCENARIO", "simple")
marker = os.path.join(os.environ["HOME"], "launches")
with open(marker, "a") as fh:
    fh.write("x")
launches = len(open(marker).read())
with open(os.path.join(os.environ["HOME"], "argv.json"), "w") as fh:
    json.dump({"argv": sys.argv, "pwd": os.environ.get("PWD"), "cwd": os.getcwd(),
               "key": os.environ.get("WISP_IDEATE_ENGINE_KEY")}, fh)

def out(rec):
    sys.stdout.write(json.dumps(rec) + "\n"); sys.stdout.flush()

def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)

n = [0]
def gate(tool, inp):
    n[0] += 1
    cid = "call%d" % n[0]
    out({"type": "tool_execution_start", "toolCallId": cid, "toolName": tool, "args": inp})
    out({"type": "extension_ui_request", "id": "g%d" % n[0], "method": "input", "title": "wisp-ideate-gate",
         "placeholder": json.dumps({"id": cid, "tool": tool, "input": inp})})
    while True:
        rec = read()
        if rec.get("type") == "abort":
            out({"type": "agent_settled", "aborted": True}); sys.exit(0)
        if rec.get("type") == "extension_ui_response":
            ans = json.loads(rec.get("value") or "{}")
            ok = ans.get("allow") is True
            if ok and tool == "write":
                with open(inp["path"], "w") as fh:
                    fh.write(inp.get("content", ""))
            out({"type": "tool_execution_end", "toolCallId": cid, "toolName": tool, "isError": not ok})
            return ok

def say(text, stop="stop", error=None):
    out({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}})
    out({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"}})
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [], "stopReason": stop}
    if error:
        msg["errorMessage"] = error
    out({"type": "message_end", "message": msg})

rec = read()
assert rec["type"] == "prompt", rec
out({"id": rec.get("id"), "type": "response", "command": "prompt", "success": True, "data": {"disposition": "started"}})
if scenario == "crash":
    sys.exit(3)
if scenario == "guard" and (launches == 1 or os.environ.get("FAKE_ALWAYS_GUARD")):
    say("", stop="error", error="Prefill memory estimate 7.9 GB exceeds the limit")
    out({"type": "agent_settled", "aborted": False}); sys.exit(0)
if scenario == "guard":
    say("Picked it up again."); out({"type": "agent_settled", "aborted": False}); sys.exit(0)
if scenario == "other_dialog":
    out({"type": "extension_ui_request", "id": "q1", "method": "confirm", "title": "Trust?", "message": "x"})
    rec = read()
    with open(os.path.join(os.environ["HOME"], "dialog.json"), "w") as fh:
        json.dump(rec, fh)
    say("ok"); out({"type": "agent_settled", "aborted": False}); sys.exit(0)
if scenario == "hang":
    gate("bash", {"command": "touch made-by-agent.txt"})
    say("after"); out({"type": "agent_settled", "aborted": False}); sys.exit(0)
say("Plan: one page.")
gate("write", {"path": "index.html", "content": "<h1>hi</h1>"})
gate("bash", {"command": "ls"})
gate("bash", {"command": "npm install left-pad"})
gate("write", {"path": "../outside.txt", "content": "no"})
gate("bash", {"command": "rm -rf /"})
say("Done: open index.html.")
out({"type": "agent_settled", "aborted": False})
for line in sys.stdin:
    pass
'''


class FakeResidency:
    def __init__(self, loaded=True, listed=True, reachable=True):
        self.model = engine.ModelState(reachable, listed, loaded)
        self.ensured = 0
        self.begun = 0
        self.ended = 0

    async def state(self):
        return self.model

    def state_sync(self):
        return self.model

    def ensure_sync(self):
        self.ensured += 1
        self.model = engine.ModelState(True, True, True)

    def wait_for_ask(self):
        self.waited = getattr(self, "waited", 0) + 1

    def begin(self):
        self.begun += 1

    def end(self):
        self.ended += 1


class FakeAdapter(harness.PiAdapter):
    def __init__(self, script: Path):
        self.script = script

    def discover(self):
        return harness.HarnessInstall("pi", Path(sys.executable), self.script, "0.0-test", "env",
                                      [self.script.parent]), ""

    def prepare(self, *args, **kwargs):
        spec = super().prepare(*args, **kwargs)
        # The real environment is built from scratch; only the stand-in's own switches pass.
        spec.env.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_")})
        return spec


@pytest.fixture
def world(tmp_path, monkeypatch):
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI)
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_S", 30.0)
    residency = FakeResidency()
    manager = runner.Manager(
        store.Store(tmp_path / "projects"), FakeAdapter(script), residency,
        sandbox_usable=lambda: True,
        engine_target=lambda: harness.EngineTarget("http://127.0.0.1:8000/v1", 8000),
        api_key=lambda: "",
        contain=lambda argv, profile: list(argv))
    monkeypatch.setattr(runner, "_manager", manager)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield manager, client, residency


def wait_for(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def start(client, monkeypatch, scenario="simple", prompt="A habit tracker as a single page"):
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    reply = client.post("/ideate/projects", json={"prompt": prompt, "kind": "ui"})
    assert reply.status_code == 200, reply.text
    return reply.json()


def pending(client, project_id):
    def check():
        body = client.get(f"/ideate/projects/{project_id}").json()
        return body if body["approvals"] else None
    return wait_for(check)


# ------------------------------------------------------------------- whole runs over HTTP ---

def test_run_events_files_and_approval(world, monkeypatch):
    manager, client, residency = world
    project = start(client, monkeypatch)
    for key in ("id", "name", "kind", "status", "path", "created_at", "updated_at", "events", "files", "approvals"):
        assert key in project
    assert project["kind"] == "ui" and project["name"] == "Habit tracker single page"
    assert project["path"].endswith("/work")
    waiting = pending(client, project["id"])
    assert waiting["status"] == "waiting"
    card = waiting["approvals"][0]
    assert set(card) >= {"id", "tool", "text", "path"}
    assert card["tool"] == "bash" and card["text"] == "npm install left-pad"
    assert "no internet" in card["note"]
    reply = client.post(f"/ideate/projects/{project['id']}/approvals/{card['id']}", json={"allow": False})
    assert reply.status_code == 200
    # The write outside the project is a card too (the sandbox would refuse it anyway).
    outside = pending(client, project["id"])["approvals"][0]
    assert outside["tool"] == "write" and outside["note"] == gate.OUTSIDE_NOTE
    client.post(f"/ideate/projects/{project['id']}/approvals/{outside['id']}", json={"allow": False})
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert body["status"] == "done" and body["approvals"] == []
    kinds = [e["kind"] for e in body["events"]]
    assert kinds[-1] == "done" and "thinking" in kinds and "approval" in kinds and "file" in kinds
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    tools = [(e["tool"], e["text"], e["path"], e["status"]) for e in body["events"] if e["kind"] == "tool"]
    assert ("write", "", "index.html", "ok") in tools
    assert ("bash", "ls", "", "ok") in tools                       # read-only: no card
    assert ("bash", "npm install left-pad", "", "denied") in tools
    assert ("bash", "rm -rf /", "", "denied") in tools             # Wisp's never-run list
    assert not any(t[0] == "bash" and t[1] == "npm install left-pad" and t[3] == "error" for t in tools)
    assert body["files"] == [{"path": "index.html", "size": 11}]
    assert not (Path(body["path"]).parent / "outside.txt").exists()
    # Events after a seq.
    later = client.get(f"/ideate/projects/{project['id']}", params={"after": seqs[-2]}).json()["events"]
    assert [e["seq"] for e in later] == [seqs[-1]]
    listed = client.get("/ideate/projects").json()["projects"]
    assert listed[0]["id"] == project["id"] and set(listed[0]) == {"id", "name", "kind", "status",
                                                                    "created_at", "updated_at"}
    assert residency.begun == residency.ended == 1


def test_lean_pi_profile_and_private_config(world, monkeypatch, tmp_path):
    manager, client, _ = world
    project = start(client, monkeypatch, scenario="other_dialog")
    wait_for(lambda: manager.active is None)
    p = manager.store.project(project["id"])
    seen = json.loads((p.agent / "home" / "argv.json").read_text())
    argv = seen["argv"]
    assert argv[argv.index("--mode") + 1] == "rpc"
    assert argv[argv.index("--tools") + 1] == "read,write,edit,bash"
    assert argv[argv.index("--thinking") + 1] == config.THINKING_LEVEL
    prompt_file = Path(argv[argv.index("--system-prompt") + 1])
    assert prompt_file.parent == p.private and len(prompt_file.read_text()) < 2400
    assert "index.html" in prompt_file.read_text()                 # the UI variant
    for flag in ("-ne", "--no-mcp", "--no-skills", "--no-context-files", "--offline", "--no-approve"):
        assert flag in argv
    assert Path(argv[argv.index("-e") + 1]).parent == p.private    # not writable by the harness
    assert seen["pwd"] == str(p.work) and os.path.realpath(seen["cwd"]) == os.path.realpath(p.work)
    assert seen["key"] is None
    models = json.loads((p.agent / "pi" / "models.json").read_text())
    provider = models["providers"][config.PROVIDER]
    assert provider["apiKey"] == "local" and provider["baseUrl"] == "http://127.0.0.1:8000/v1"
    assert provider["models"][0]["contextWindow"] - json.loads(
        (p.agent / "pi" / "settings.json").read_text())["compaction"]["reserveTokens"] == config.PROMPT_BUDGET_TOKENS
    # Any other dialog through the RPC channel is answered "cancelled".
    assert json.loads((p.agent / "home" / "dialog.json").read_text())["cancelled"] is True
    ext = (p.private / "ideate-gate.ts").read_text()
    assert "wisp-ideate-gate" in ext and "block: true" in ext and str(config.TOOL_RESULT_CHARS) in ext


def test_engine_key_goes_only_to_the_environment(world, monkeypatch):
    manager, client, _ = world
    monkeypatch.setattr(manager, "_api_key", lambda: "invented-test-key")
    project = start(client, monkeypatch, scenario="other_dialog")
    wait_for(lambda: manager.active is None)
    p = manager.store.project(project["id"])
    assert json.loads((p.agent / "home" / "argv.json").read_text())["key"] == "invented-test-key"
    assert json.loads((p.agent / "pi" / "models.json").read_text())["providers"][config.PROVIDER]["apiKey"] \
        == "$" + harness.ENGINE_KEY_ENV
    for path in p.root.rglob("*"):
        if path.is_file() and path.name != "argv.json":
            assert b"invented-test-key" not in path.read_bytes(), path


def test_approval_timeout_is_a_no(world, monkeypatch):
    manager, client, _ = world
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_S", 0.3)
    project = start(client, monkeypatch, scenario="hang")
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert ("bash", "touch made-by-agent.txt", "denied") in [(e["tool"], e["text"], e["status"])
                                                              for e in body["events"] if e["kind"] == "tool"]
    assert body["status"] == "done"
    assert not (Path(body["path"]) / "made-by-agent.txt").exists()


def test_cancel_and_one_run_at_a_time(world, monkeypatch):
    manager, client, _ = world
    project = start(client, monkeypatch, scenario="hang")
    pending(client, project["id"])
    second = client.post("/ideate/projects", json={"prompt": "Another one", "kind": "general"})
    assert second.status_code == 409 and "already working on" in second.json()["detail"]
    assert client.post(f"/ideate/projects/{project['id']}/message", json={"text": "more"}).status_code == 409
    assert len(client.get("/ideate/projects").json()["projects"]) == 1
    reply = client.post(f"/ideate/projects/{project['id']}/cancel", json={})
    assert reply.status_code == 200
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert body["status"] == "cancelled" and body["events"][-1]["kind"] == "error"
    assert body["approvals"] == []
    # Free again: a follow-up message resumes the same project.
    monkeypatch.setenv("FAKE_SCENARIO", "other_dialog")
    assert client.post(f"/ideate/projects/{project['id']}/message", json={"text": "go on"}).status_code == 200
    wait_for(lambda: manager.active is None)
    p = manager.store.project(project["id"])
    assert "--continue" in json.loads((p.agent / "home" / "argv.json").read_text())["argv"]
    assert client.get(f"/ideate/projects/{project['id']}").json()["status"] == "done"


def test_guard_refusal_restarts_fresh_then_fails_honestly(world, monkeypatch):
    manager, client, _ = world
    project = start(client, monkeypatch, scenario="guard")
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert body["status"] == "done"
    assert any("Starting fresh" in e["text"] for e in body["events"])
    p = manager.store.project(project["id"])
    assert "--continue" not in json.loads((p.agent / "home" / "argv.json").read_text())["argv"]
    monkeypatch.setenv("FAKE_ALWAYS_GUARD", "1")
    project = start(client, monkeypatch, scenario="guard")
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert body["status"] == "failed" and body["events"][-1]["text"] == config.GUARD_ERROR


def test_crash_fails_with_a_line(world, monkeypatch):
    manager, client, _ = world
    project = start(client, monkeypatch, scenario="crash")
    wait_for(lambda: manager.active is None)
    body = client.get(f"/ideate/projects/{project['id']}").json()
    assert body["status"] == "failed" and "stopped unexpectedly" in body["events"][-1]["text"]


def test_model_swap_is_announced_and_released(world, monkeypatch):
    manager, client, residency = world
    residency.model = engine.ModelState(True, True, False)
    assert client.get("/ideate/status").json()["model_loaded"] is False
    project = start(client, monkeypatch, scenario="other_dialog")
    wait_for(lambda: manager.active is None)
    events = client.get(f"/ideate/projects/{project['id']}").json()["events"]
    assert events[0]["kind"] == "text" and "Loading Inkling 2 Mini" in events[0]["text"]
    assert residency.ensured == 1 and residency.begun == residency.ended == 1
    assert residency.waited == 1


def test_a_loaded_model_is_still_made_the_only_one(world, monkeypatch):
    """Inkling already loaded: the run still calls the swap helper, which unloads the rest."""
    manager, client, residency = world
    residency.model = engine.ModelState(True, True, True)
    project = start(client, monkeypatch, scenario="other_dialog")
    wait_for(lambda: manager.active is None)
    events = client.get(f"/ideate/projects/{project['id']}").json()["events"]
    assert not any("Loading Inkling" in (e.get("text") or "") for e in events)
    assert residency.ensured == 1 and residency.begun == residency.ended == 1


def test_status_and_unavailable_reasons(world, monkeypatch, tmp_path):
    manager, client, residency = world
    body = client.get("/ideate/status").json()
    assert body["available"] is True and body["harness"] == "pi" and body["model"] == config.MODEL_LABEL
    assert set(body) >= {"available", "reason", "harness", "model", "model_loaded", "projects_root"}
    monkeypatch.setattr(manager, "_sandbox_usable", lambda: False)
    body = client.get("/ideate/status").json()
    assert body["available"] is False and "sandbox" in body["reason"]
    refused = client.post("/ideate/projects", json={"prompt": "x", "kind": "general"})
    assert refused.status_code == 409 and "sandbox" in refused.json()["detail"]
    assert client.get("/ideate/projects").json()["projects"] == []
    monkeypatch.setattr(manager, "_sandbox_usable", lambda: True)
    residency.model = engine.ModelState(True, False, False)
    monkeypatch.setattr(engine, "_home", lambda: tmp_path / "no-home")
    monkeypatch.setattr(engine, "models_dir", lambda: tmp_path / "no-models")
    body = client.get("/ideate/status").json()
    assert body["reason"] == "Inkling 2 Mini isn't on this Mac." and body["can_link"] is False


def test_routes_refuse_web_pages_bad_ids_and_kinds(world):
    _, client, _ = world
    assert client.post("/ideate/projects", json={"prompt": "x"}, headers={"Origin": "https://example.com"}
                       ).status_code == 403
    assert client.post("/ideate/projects", content=b'{"prompt":"x"}',
                       headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/ideate/projects", json={"prompt": "x", "kind": "nope"}).status_code == 400
    assert client.get("/ideate/projects/..%2F..%2Fetc").status_code == 404
    assert client.get("/ideate/projects/ABCDEFGH").status_code == 404
    assert client.post("/ideate/projects/abcdefgh/approvals/zz", json={"allow": True}).status_code == 404


def test_stale_active_project_is_recovered(world, tmp_path):
    manager, client, _ = world
    project = manager.store.create("An invented idea", "general")
    manager.store.update(project, status="running")
    body = client.get(f"/ideate/projects/{project.id}").json()
    assert body["status"] == "failed" and "Wisp stopped" in body["events"][-1]["text"]


# -------------------------------------------------------------------------- pure pieces ----

def test_names_and_ids_are_safe():
    for prompt in ("../../etc/passwd; rm -rf ~", "<script>alert(1)</script>", "\x00\x1b[31m", "", "   "):
        name = store.display_name(prompt)
        assert name and "/" not in name and "\x00" not in name and "<" not in name and len(name) <= 28
    assert store.display_name("Build me a tip splitter for dinners") == "Tip splitter dinners"
    assert store.slug("Tip splitter: dinners!") == "tip-splitter-dinners" and store.slug("///") == "idea"
    for _ in range(50):
        assert store.valid_id(store.new_id())
    for bad in ("../x", "abc", "ABCDEFGH", "abcdefgh/", "abcdefg.", ""):
        assert not store.valid_id(bad)


def test_path_confinement(tmp_path):
    work = tmp_path / "proj" / "work"
    (work / "src").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (work / "link").symlink_to(tmp_path / "elsewhere")
    (work / "filelink").symlink_to(tmp_path / "elsewhere" / "f.txt")
    assert store.inside(work, "src/new.py") is not None
    assert store.inside(work, str(work / "a.txt")) is not None
    assert store.inside(work, "deep/new/dir/a.txt") is not None
    for bad in ("../x.txt", "src/../../x", "~/x", "/etc/hosts", str(tmp_path / "elsewhere" / "x"),
                "link/x.txt", "filelink", "a\x00b", ""):
        assert store.inside(work, bad) is None, bad
    # The /private spelling of a temp folder is the same folder.
    real = Path(os.path.realpath(work))
    assert store.inside(work, str(real / "ok.txt")) is not None


def test_gate_judgements(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    assert gate.judge("read", {"path": "notes.txt"}, work).action == gate.ALLOW
    assert gate.judge("write", {"path": "a.txt"}, work).action == gate.ALLOW
    assert gate.judge("edit", {"path": "../a.txt"}, work).action == gate.CONFIRM
    assert gate.judge("bash", {"command": "ls -la"}, work).action == gate.ALLOW
    assert gate.judge("bash", {"command": "cat index.html"}, work).action == gate.ALLOW
    for command in ("python3 app.py", "touch x", "node check.js"):
        assert gate.judge("bash", {"command": command}, work).action == gate.CONFIRM, command
    for command in ("npm install", "pip install requests", "curl https://example.com", "git clone x"):
        verdict = gate.judge("bash", {"command": command}, work)
        assert verdict.action == gate.CONFIRM and verdict.note == gate.NO_NETWORK_NOTE, command
    assert gate.judge("bash", {"command": "rm -rf /"}, work).action == gate.DENY
    assert gate.judge("bash", {"command": "curl http://127.0.0.1:8765/agent"}, work).action == gate.DENY
    assert gate.judge("bash", {"command": "x" * 5000}, work).action == gate.DENY
    assert gate.judge("bash", {}, work).action == gate.DENY
    assert gate.judge("grep", {"pattern": "x"}, work).action == gate.DENY


def test_pi_translation():
    class Proc:
        stdin = None
    session = harness.PiSession(Proc())
    events = [e for rec in (
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}],
                                            "stopReason": "stop"}},
        {"type": "message_end", "message": {"role": "user", "content": "ignored"}},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "isError": True},
        {"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                            "errorMessage": "boom"}},
        {"type": "agent_settled", "aborted": True},
    ) for e in session.translate(rec)]
    assert [e.kind for e in events] == ["thinking", "text", "tool_start", "tool_end", "error", "settled"]
    assert events[3].ok is False and events[4].text == "boom" and events[5].aborted is True


# ---------------------------------------------------------------------- sandbox profile ----

def test_profile_writes_only_the_project(tmp_path):
    project = tmp_path / "proj"
    work, agent = project / "work", project / ".ideate" / "agent"
    work.mkdir(parents=True)
    agent.mkdir(parents=True)
    text = sandbox.build_profile(project, [work, agent], 8000)
    assert "(deny default)" in text and "(allow default)" not in text
    write_lines = [line for line in text.splitlines() if line.startswith("(allow file-write*")]
    assert len(write_lines) == 1
    subpaths = [part.split('"')[1] for part in write_lines[0].split("(subpath ")[1:]]
    assert subpaths and all(Path(s) in (work, agent) or Path(s) in (Path(os.path.realpath(work)),
                                                                   Path(os.path.realpath(agent)))
                            for s in subpaths)
    assert '(allow network-outbound (remote ip "localhost:8000"))' in text
    assert text.count("network-outbound") == 1 + sum(1 for l in text.splitlines() if "(deny network-outbound" in l)
    assert "/Library/Keychains" in text and "/.pi" in text and "/.config/opencode" in text
    with pytest.raises(sandbox.ProfileError):
        sandbox.build_profile(project, [tmp_path / "elsewhere"], 8000)
    with pytest.raises(sandbox.ProfileError):
        sandbox.build_profile(project, [work], 8765)


@pytest.mark.skipif(not sandbox.usable(), reason="a sandbox cannot be applied inside another sandbox")
def test_profile_really_confines_writes_and_network(tmp_path):
    project = Path(os.path.realpath(tmp_path)) / "proj"
    work, agent = project / "work", project / ".ideate" / "agent"
    work.mkdir(parents=True)
    agent.mkdir(parents=True)
    profile = project / ".ideate" / "p.sb"
    profile.write_text(sandbox.build_profile(project, [work, agent], 1))
    script = (f"echo in > in.txt; echo out > {tmp_path}/out.txt; echo meta > {project}/.ideate/meta.json; "
              "/usr/bin/curl -s -m 3 http://example.com >/dev/null && echo NET")
    result = subprocess.run(sandbox.contain(["/bin/sh", "-c", script], profile), cwd=work,
                            capture_output=True, text=True, timeout=30)
    assert (work / "in.txt").exists()
    assert not (tmp_path / "out.txt").exists() and not (project / ".ideate" / "meta.json").exists()
    assert "NET" not in result.stdout


def test_read_outside_the_project_needs_a_card(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    assert gate.judge("read", {"path": "src/a.txt"}, work).action == gate.ALLOW
    for path in ("/etc/hosts", "../x", "~/Documents/a.txt", "/Users/someone/Library/Mail/V10/x"):
        assert gate.judge("read", {"path": path}, work).action in (gate.CONFIRM, gate.DENY), path
    assert gate.judge("read", {"path": "/etc/hosts"}, work).action == gate.CONFIRM
    assert gate.judge("read", {}, work).action == gate.DENY


def test_engine_talk_and_barred_commands(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for command in ("python3 -c 'import urllib.request as u; u.urlopen(\"http://localhost:8000/v1/models\")'",
                    "nc 127.0.0.1 8000", "echo hi > /dev/tcp/0.0.0.0/22", "run-thing --port 8000"):
        verdict = gate.judge("bash", {"command": command}, work, 8000)
        assert verdict.action == gate.CONFIRM and verdict.note == gate.ENGINE_NOTE, command
    assert "no internet" not in gate.ENGINE_NOTE and "fail" not in gate.ENGINE_NOTE
    assert gate.judge("bash", {"command": "echo 18000"}, work, 8000).note != gate.ENGINE_NOTE
    for command in ("launchctl list", "security find-generic-password -a x", "screencapture x.png",
                    "open https://example.com", "osascript -e 'beep'", "shortcuts run x",
                    "defaults write com.x y 1", "crontab -l", "ls && /usr/bin/open .", "sudo security dump-keychain",
                    "echo a; osascript -e x"):
        assert gate.judge("bash", {"command": command}, work, 8000).action == gate.DENY, command
    assert gate.judge("bash", {"command": "python3 -c \"open('a.txt').read()\""}, work).action == gate.CONFIRM


def test_profile_denies_stores_mach_and_foreign_ttys(tmp_path):
    project = tmp_path / "proj"
    work, agent = project / "work", project / ".ideate" / "agent"
    work.mkdir(parents=True)
    agent.mkdir(parents=True)
    text = sandbox.build_profile(project, [work, agent], 8000)
    for name in ("Library/Mail", "Library/Messages", "Library/Safari", "Library/Group Containers",
                 "Library/Containers", "Library/Cookies", "Library/Preferences", "Library/Calendars",
                 "Library/Keychains", "Application Support/AddressBook"):
        assert name in text, name
    for name in ("com.apple.SecurityServer", "com.apple.securityd", "com.apple.windowserver",
                 "com.apple.screencapture", "com.apple.nsurlsessiond", "com.apple.tccd"):
        assert name in text, name
    assert '(literal "/dev/tty")' in text and 'regex #"^/dev/tty' not in text
    assert "process-info* (target self)" in text and "same-sandbox))" not in text.split("process-info")[1][:40]


def test_a_cancelled_run_gates_nothing(tmp_path):
    answers = []

    class Session:
        def answer_gate(self, gate_id, allow, reason=""):
            answers.append((gate_id, allow))

    class Run:
        cancel = True
        session = Session()
        engine = None
        pending = {}

    event = harness.HarnessEvent("gate", tool="write", args={"path": "a.txt"}, gate_id="g1", call_id="c1")
    runner.Manager._gate(object(), Run(), event)
    assert answers == [("g1", False)]


def test_short_pi_override_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_IDEATE_PI", "/cli.js")
    monkeypatch.setattr(harness, "_release_entry", lambda path: (Path("/a/b/cli.js"), "1"))
    monkeypatch.setattr(harness, "_first_node", lambda: Path("/usr/bin/true"))
    monkeypatch.setattr("service.safety.shell_sandbox.app_bundle", lambda: None)
    install, why = harness.PiAdapter().discover()
    assert install is None and why


@pytest.mark.skipif(not sandbox.usable(), reason="a sandbox cannot be applied inside another sandbox")
def test_profile_hides_stores_parent_environment_and_ttys(tmp_path):
    import shutil
    project = Path(os.path.realpath(tmp_path)) / "proj"
    work, agent = project / "work", project / ".ideate" / "agent"
    work.mkdir(parents=True)
    agent.mkdir(parents=True)
    profile = project / ".ideate" / "p.sb"
    profile.write_text(sandbox.build_profile(project, [work, agent], 1))
    # Node (when present) still starts under the profile.
    node = shutil.which("node")
    if node:
        ran = subprocess.run(sandbox.contain([node, "--version"], profile), cwd=work, capture_output=True,
                             text=True, timeout=30)
        assert ran.returncode == 0 and ran.stdout.startswith("v"), ran.stderr
    # A child may not read its parent's environment (the engine key lives there).
    script = "/bin/sh -c '/bin/ps -E -ww -p $PPID 2>&1 | grep -c INVENTED_KEY_VALUE'"
    env = dict(os.environ, WISP_IDEATE_ENGINE_KEY="INVENTED_KEY_VALUE")
    ran = subprocess.run(sandbox.contain(["/bin/sh", "-c", script], profile), cwd=work, env=env,
                         capture_output=True, text=True, timeout=30)
    assert ran.stdout.strip() in ("0", ""), ran.stdout
    # The person's stores can't be listed, even by a process that holds Full Disk Access.
    mail = Path.home() / "Library" / "Mail"
    if mail.exists():
        ran = subprocess.run(sandbox.contain(["/bin/ls", str(mail)], profile), cwd=work,
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode != 0
