"""Durable action ownership using disposable SQLite and synthetic arguments."""
import pytest

from service.memory.store import SessionStore
from service.router.model_led import needs_effect_owner
from tests.test_model_led_integration import synthetic, run, call, discover, Approval
from service import main as _registered_main
from service.tools import registry
from service.router.model_led import BuiltinCompletion, builtin_completion_verified, family_for, registry_specs

BUILTIN_ARGS = {
    "run_shell": {"cmd": "touch fixture.txt"},
    "run_applescript": {"script": "return 1"},
    "manage_contacts": {"action": "create", "name": "Fixture"},
}
BUILTIN_PROMPTS = {
    "run_shell": "Run this exact shell command: touch fixture.txt",
    "run_applescript": "Run this exact AppleScript: return 1",
    "manage_contacts": "Create a contact named Fixture",
}
AUTHORITATIVE_BUILTINS = {name: registry.get_tool(name) for name in BUILTIN_ARGS}


def fake_builtin(name, effects, result, monkeypatch, *, sync=False):
    from dataclasses import replace
    authoritative = AUTHORITATIVE_BUILTINS[name]
    assert authoritative is not None and not authoritative.unavailable_reason
    def invoke(**args):
        effects.append((name, args))
        return result
    async def async_invoke(**args):
        return invoke(**args)
    func = invoke if sync else async_invoke
    func.__module__ = authoritative.func.__module__
    monkeypatch.setattr(__import__("sys").modules[func.__module__], name, func)
    tool = replace(authoritative, func=func)
    registry.REGISTRY[name] = tool
    family = family_for(registry_specs({name: tool})[0])
    return tool, family


@pytest.mark.parametrize("name", list(BUILTIN_ARGS))
@pytest.mark.parametrize("model_led", [True, False])
def test_working_builtin_mutation_parity_dispatches_once_with_explicit_receipt(synthetic, owned_store, monkeypatch, name, model_led):
    effects, _ = synthetic
    receipt = BuiltinCompletion("Unchanged synthetic completion text.", tool_name=name,
                                completion_code=0)
    _, family = fake_builtin(name, effects, receipt, monkeypatch)
    store, sid = owned_store
    script = [[discover(family, name)], [call(name, **BUILTIN_ARGS[name])], "Completed."] if model_led else [[call(name, **BUILTIN_ARGS[name])], "Completed."]
    run(script, prompt=BUILTIN_PROMPTS[name], approval=Approval(True),
        model_led_discovery=model_led, tools=[] if model_led else [name],
        claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a, outbound=True),
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert effects == [(name, BUILTIN_ARGS[name])]
    assert not store.pending_model_effects(sid)
    assert store._db.execute("SELECT count(*) FROM task_effect_claims").fetchone()[0] == int(model_led)


@pytest.mark.parametrize("name", list(BUILTIN_ARGS))
@pytest.mark.parametrize("kind", ["failed", "missing", "malformed", "wrong_phase"])
def test_unverified_builtin_attempt_is_consumed_and_cannot_retry(synthetic, owned_store, monkeypatch, name, kind):
    effects, _ = synthetic
    code = 17
    if kind == "malformed":
        code = "0"
    if kind == "wrong_phase":
        code = 0
    receipt = ("Arbitrary nonempty stdout: SUCCESS" if kind == "missing" else
               BuiltinCompletion("Arbitrary body: SUCCESS", tool_name=name, completion_code=code,
                                 phase="read" if kind == "wrong_phase" else "mutation"))
    _, family = fake_builtin(name, effects, receipt, monkeypatch)
    store, sid = owned_store
    run([[discover(family, name)], [call(name, **BUILTIN_ARGS[name])],
         [call(name, **BUILTIN_ARGS[name])], "Attempted."], prompt=BUILTIN_PROMPTS[name],
        approval=Approval(True),
        claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a, outbound=True),
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert effects == [(name, BUILTIN_ARGS[name])]
    assert store.pending_model_effects(sid)[0]["status"] == "uncertain"
    assert store._db.execute("SELECT count(*) FROM task_effect_claims").fetchone()[0] == 1


@pytest.mark.parametrize("name", list(BUILTIN_ARGS))
def test_denied_builtin_never_claims_or_dispatches(synthetic, owned_store, name, monkeypatch):
    from service.agent import loop as agent_loop
    from service.safety.policy import Decision, Tier
    effects, _ = synthetic
    _, family = fake_builtin(name, effects, "unused", monkeypatch)
    store, sid = owned_store
    monkeypatch.setattr(agent_loop, "decide",
                        lambda *a, **k: Decision(Tier.DENY, "Synthetic explicit denial"))
    run([[discover(family, name)], [call(name, **BUILTIN_ARGS[name])]],
        approval=Approval(False),
        claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a, outbound=True),
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert not effects and not store.pending_model_effects(sid)


@pytest.mark.parametrize("sync", [True, False])
def test_actual_registry_preserves_host_carrier_but_not_extension_attributes(synthetic, monkeypatch, sync):
    import asyncio
    effects, _ = synthetic
    receipt = BuiltinCompletion("Exact display text", tool_name="run_applescript", completion_code=0)
    tool, _ = fake_builtin("run_applescript", effects, receipt, monkeypatch, sync=sync)
    async def fake_worker(func, **args):
        return func(**args)  # Synthetic scheduling seam: no worker thread.
    monkeypatch.setattr(registry.asyncio, "to_thread", fake_worker)
    result = asyncio.run(registry.run_tool(tool, BUILTIN_ARGS["run_applescript"]))
    assert result is receipt and str(result) == "Exact display text" and builtin_completion_verified(tool, result)
    from dataclasses import replace
    async def extension(**args):
        return receipt
    extension.__module__ = "service.extension.untrusted"
    ordinary = asyncio.run(registry.run_tool(replace(tool, func=extension), BUILTIN_ARGS["run_applescript"]))
    assert type(ordinary) is str and not builtin_completion_verified(tool, ordinary)
    extension.__module__ = tool.func.__module__  # Attributes alone cannot impersonate the host function.
    forged = asyncio.run(registry.run_tool(replace(tool, func=extension), BUILTIN_ARGS["run_applescript"]))
    assert type(forged) is str and not builtin_completion_verified(tool, forged)
    with pytest.raises(AttributeError):
        receipt.completion_code = 1


@pytest.mark.parametrize("name", ["run_shell"])
def test_supported_builtin_read_phases_dispatch_without_action_claim(synthetic, owned_store, monkeypatch, name):
    effects, _ = synthetic
    args = {"cmd": "pwd"}
    _, family = fake_builtin(name, effects, "Synthetic read result.", monkeypatch)
    store, sid = owned_store
    run([[discover(family, name)], [call(name, **args)], "Read result."], approval=Approval(True),
        claim_effect=lambda *a: (_ for _ in ()).throw(AssertionError("Read acquired an action claim")))
    assert effects == [(name, args)] and not store.pending_model_effects(sid)


@pytest.mark.parametrize("name", list(BUILTIN_ARGS))
def test_actual_builtin_emitter_uses_host_status_not_stdout_or_body(monkeypatch, name):
    import sys
    from types import SimpleNamespace
    from service.safety.policy import Decision, Tier
    tool = AUTHORITATIVE_BUILTINS[name]
    module = sys.modules[tool.func.__module__]
    native = SimpleNamespace(returncode=0, stdout="Untrusted arbitrary output", stderr="")
    if name == "run_shell":
        monkeypatch.setattr("service.safety.policy.decide", lambda *a, **k: Decision(Tier.ALLOW, "synthetic"))
        monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: native)
    elif name == "run_applescript":
        monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: native)
    elif name == "manage_contacts":
        monkeypatch.setattr(module, "_osa", lambda *a, **k: native)
    result = tool.func(**BUILTIN_ARGS[name])
    assert builtin_completion_verified(tool, result)
    displayed = str(result)
    native.returncode = 17
    failed = tool.func(**BUILTIN_ARGS[name])
    assert not builtin_completion_verified(tool, failed)
    assert displayed  # Existing UI output remains a regular nonempty string.


@pytest.mark.parametrize("mode", [None, "1", "0"])
def test_consumed_cancelled_legacy_attempt_survives_reopen_and_mode_switch(owned_store, monkeypatch, mode):
    from pathlib import Path
    from service.workflows.engine import prepare_turn
    from service.workflows.models import WorkflowPlan
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    else:
        monkeypatch.setenv("WISP_MODEL_LED_ROUTING", mode)
    store, sid = owned_store
    plan = WorkflowPlan(status="running", sources=["messages"], recipient="Fixture", channel="messages", revision=1)
    store.save_workflow(sid, plan.to_dict())
    assert store.claim_workflow_effect(sid, plan.id, f"workflow_effect:{plan.id}", revision=1)
    closed = prepare_turn(store, sid, "cancel", owner_only=True)
    assert closed.plan.status == "cancelled"
    other = SessionStore(Path(store._db.execute("PRAGMA database_list").fetchone()[2]))
    try:
        assert other.unresolved_action(sid)
        for tool, args in [("send_message", {"to": "Fixture"}), ("send_message", {"to": "Changed"}),
                           ("send_email", {"to": "changed@example.invalid"})]:
            assert not other.claim_model_effect(sid, "new", tool, args, outbound=True)["admitted"]
        # A new typed plan is its own owner (1.2.5 semantics): the earlier
        # legacy attempt blocks only NEW model-proposed effects, never another
        # legacy plan's claim, in any routing mode.
        fresh = WorkflowPlan(status="running", channel="email", recipient="Changed", revision=1)
        other.save_workflow(sid, fresh.to_dict())
        assert other.claim_workflow_effect(sid, fresh.id, "fresh-outbound", revision=1)
        assert other.claim_effect_call(fresh.id, "fresh-task-call", revision=1)
        assert other._db.execute("SELECT count(*) FROM task_effect_claims").fetchone()[0] == 3
    finally:
        other._db.close()


@pytest.mark.parametrize("mode", [None, "0", "1"])
@pytest.mark.parametrize("final", ["failed", "cancelled", "superseded", "denied"])
def test_earlier_failed_plan_does_not_block_unrelated_typed_effect(owned_store, monkeypatch, mode, final):
    """Routing OFF default path: t1 consumed a claim then ended; unrelated t2 still runs."""
    from service.workflows.models import WorkflowPlan
    if mode is None:
        monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    else:
        monkeypatch.setenv("WISP_MODEL_LED_ROUTING", mode)
    store, sid = owned_store
    t1 = WorkflowPlan(status="running", channel="messages", recipient="One", revision=1)
    store.save_workflow(sid, t1.to_dict())
    assert store.claim_workflow_effect(sid, t1.id, f"workflow_effect:{t1.id}", revision=1)
    t1.status = final
    store.save_workflow(sid, t1.to_dict())
    t2 = WorkflowPlan(status="running", channel="email", recipient="Two", revision=1)
    store.save_workflow(sid, t2.to_dict())
    assert store.claim_effect_call(t2.id, "t2-call", revision=1)
    assert store.claim_workflow_effect(sid, t2.id, f"workflow_effect:{t2.id}", revision=1)


def test_unresolved_model_effect_still_blocks_legacy_claims_until_user_resolves(owned_store):
    from service.workflows.models import WorkflowPlan
    store, sid = owned_store
    first = store.claim_model_effect(sid, "one", "send_message", {"to": "A"}, outbound=True)
    assert first["admitted"] and store.finish_model_effect(sid, first, verified=False)
    plan = WorkflowPlan(status="running", channel="email", recipient="Two", revision=1)
    store.save_workflow(sid, plan.to_dict())
    assert not store.claim_effect_call(plan.id, "call", revision=1)
    assert not store.claim_workflow_effect(sid, plan.id, "wf", revision=1)
    assert not store.claim_model_effect(sid, "two", "send_email", {"to": "B"}, outbound=True)["admitted"]
    # Explicit user resolution lifts the barrier without re-running anything.
    assert store.resolve_model_effects(sid, "model_effect:unknown") == 0
    assert store.resolve_model_effects(sid) == 1
    assert not store.pending_model_effects(sid) and not store.unresolved_action(sid)
    assert store._db.execute("SELECT count(*) FROM task_effect_claims WHERE plan_id=?",
                             (first["effect_id"],)).fetchone()[0] == 1  # claim stays consumed
    assert store.resolve_model_effects(sid) == 0
    assert store.claim_effect_call(plan.id, "call", revision=1)


@pytest.mark.parametrize("status,claimed", [("cancelled", False), ("denied", False), ("completed", True)])
def test_no_attempt_cancellation_or_verified_completion_does_not_create_false_barrier(owned_store, status, claimed):
    from service.workflows.models import WorkflowPlan
    store, sid = owned_store
    plan = WorkflowPlan(status="running", channel="messages", recipient="Fixture", revision=1)
    store.save_workflow(sid, plan.to_dict())
    if claimed:
        assert store.claim_workflow_effect(sid, plan.id, f"workflow_effect:{plan.id}", revision=1)
    plan.status = status
    store.save_workflow(sid, plan.to_dict())
    assert not store.unresolved_action(sid)
    assert store.claim_model_effect(sid, "new", "send_message", {"to": "Changed"}, outbound=True)["admitted"]


@pytest.fixture
def owned_store(tmp_path):
    store = SessionStore(tmp_path / "effects.db")
    try:
        yield store, store.create_session()
    finally:
        store._db.close()
        # Each case owns this database; bound the expanded suite's temp state.
        for path in tmp_path.glob("effects.db*"):
            path.unlink(missing_ok=True)


def test_claim_is_atomic_across_connections_and_restart(owned_store):
    store, sid = owned_store
    path = store._db.execute("PRAGMA database_list").fetchone()[2]
    other = SessionStore(__import__("pathlib").Path(path))
    try:
        first = store.claim_model_effect(sid, "one", "send_message", {"to": "fixture"}, outbound=True)
        second = other.claim_model_effect(sid, "two", "send_message", {"to": "another"}, outbound=True)
        assert first["admitted"] and not second["admitted"]
        assert store.finish_model_effect(sid, first, verified=False)
        assert not other.claim_model_effect(sid, "three", "send_email", {"to": "changed"}, outbound=True)["admitted"]
        assert other.pending_model_effects(sid) == [{"tool": "send_message", "status": "uncertain", "outbound": True}]
    finally:
        other._db.close()


def test_uncertain_mutation_cannot_retry_by_changing_arguments(owned_store):
    store, sid = owned_store
    first = store.claim_model_effect(sid, "one", "add_reminder", {"title": "fixture"})
    store.finish_model_effect(sid, first, verified=False)
    assert not store.claim_model_effect(sid, "two", "add_reminder", {"title": "renamed"})["admitted"]


def test_only_exact_owner_can_settle_and_later_explicit_request_can_repeat(owned_store):
    store, sid = owned_store
    claim = store.claim_model_effect(sid, "one", "write_file", {"path": "fixture"})
    assert not store.finish_model_effect(sid, {**claim, "request_id": "impostor"}, verified=True)
    assert not store.finish_model_effect(sid, {**claim, "revision": 99}, verified=True)
    assert store.finish_model_effect(sid, claim, verified=True)
    assert not store.claim_model_effect(sid, "one", "write_file", {"path": "fixture"})["admitted"]
    repeated = store.claim_model_effect(sid, "two", "write_file", {"path": "fixture"})
    assert repeated["admitted"] and repeated["revision"] == claim["revision"] + 1


def test_fingerprint_does_not_copy_private_arguments(owned_store):
    store, sid = owned_store
    store.claim_model_effect(sid, "one", "write_file", {"content": "synthetic private fixture"})
    assert "synthetic private fixture" not in store._db.execute(
        "SELECT state_json FROM workflows WHERE session_id=?", (sid,)).fetchone()[0]


def test_missing_session_does_not_leave_claim(owned_store):
    store, _ = owned_store
    with pytest.raises(ValueError):
        store.claim_model_effect("missing", "one", "write_file", {})
    assert store._db.execute("SELECT count(*) FROM task_effect_claims").fetchone()[0] == 0


@pytest.mark.parametrize("category", ["compute", "assistant_read", "web_read", "mcp_read"])
def test_pure_reads_and_computation_need_no_action_owner(category):
    assert not needs_effect_owner(category)


@pytest.mark.parametrize("category", ["mcp_action", "skill_tool", "shell", "system_write", "unrecognized"])
def test_opaque_or_mutating_categories_require_owner(category):
    assert needs_effect_owner(category)


def test_action_cannot_run_without_durable_host_callbacks(synthetic):
    effects, _ = synthetic
    _, events, _ = run([[discover("files", "write_file")],
                       [call("write_file", path="fixture", content="fixture")]],
                      prompt="write fixture", approval=Approval(True))
    assert not effects
    assert any("durable action owner" in e.get("result", "") for e in events)


def test_denied_action_never_claims_or_runs(synthetic, owned_store):
    effects, _ = synthetic
    store, sid = owned_store
    def claim(tool, args):
        return store.claim_model_effect(sid, "one", tool.name, args)
    run([[discover("files", "write_file")],
         [call("write_file", path="fixture", content="fixture")]],
        approval=Approval(False), claim_effect=claim,
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert not effects and not store.pending_model_effects(sid)


def test_opaque_action_without_completion_contract_is_refused_before_effect(synthetic, owned_store):
    effects, register = synthetic
    register("opaque_action", "extension", "mcp_action")
    store, sid = owned_store
    run([[discover("connected", "opaque_action")], [call("opaque_action")]],
        approval=Approval(True),
        claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a, outbound=True),
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert not effects and not store.pending_model_effects(sid)


def test_existing_legacy_consumed_claim_blocks_model_executor(owned_store):
    import json, time
    store, sid = owned_store
    store._db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?)",
                      ("legacy", sid, "deliver_summary", "failed", json.dumps({"id": "legacy"}), time.time(), time.time()))
    store._db.execute("INSERT INTO task_effect_claims VALUES (?,?,?)", ("legacy-call", "legacy", time.time()))
    store._db.commit()
    assert store.unresolved_action(sid)
    assert not store.claim_model_effect(sid, "new", "send_message", {"to": "new"}, outbound=True)["admitted"]


def test_builtin_timer_receipt_is_not_mislabeled_as_uncertain(synthetic, owned_store):
    effects, register = synthetic
    tool = register("set_timer", "timers_alarms", "timer_write")
    async def timer(**args):
        effects.append(("set_timer", args))
        return "Timer set for 10 minutes."
    timer.__module__ = tool.func.__module__
    from dataclasses import replace
    from service.tools import registry
    registry.REGISTRY["set_timer"] = replace(tool, func=timer)
    store, sid = owned_store
    _, _, answer = run([[discover("automation", "set_timer")], [call("set_timer")], "Timer was set."],
        approval=Approval(True),
        claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a),
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert effects == [("set_timer", {})]
    assert not store.pending_model_effects(sid) and "not completed" not in answer


def test_exception_after_claim_cannot_release_retry_ownership(synthetic, owned_store, monkeypatch):
    import asyncio
    from service.tools import registry
    from service.agent import loop
    store, sid = owned_store
    tool = registry.get_tool("write_file")
    async def cancelled(**args):
        raise asyncio.CancelledError()
    cancelled.__module__ = "service.tools.files_tools"
    from dataclasses import replace
    registry.REGISTRY["write_file"] = replace(tool, func=cancelled)
    with pytest.raises(asyncio.CancelledError):
        run([[discover("files", "write_file")], [call("write_file", path="fixture", content="fixture")]],
            approval=Approval(True),
            claim_effect=lambda t, a: store.claim_model_effect(sid, "one", t.name, a),
            finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert store.pending_model_effects(sid)[0]["status"] == "uncertain"
    assert not store.claim_model_effect(sid, "two", "write_file", {"path": "changed"})["admitted"]


@pytest.mark.parametrize("legacy_kind", ["task", "workflow"])
def test_legacy_approval_cannot_claim_after_competing_model_action(owned_store, legacy_kind):
    import json, time
    store, sid = owned_store
    store._db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?)",
        ("legacy", sid, "task.email.send" if legacy_kind == "task" else "deliver_summary", "running",
         json.dumps({"id": "legacy", "revision": 1}), time.time(), time.time()))
    store._db.commit()
    claim = store.claim_model_effect(sid, "competing", "send_message", {"to": "fixture"}, outbound=True)
    assert claim["admitted"]
    store.finish_model_effect(sid, claim, verified=False)
    if legacy_kind == "task":
        assert not store.claim_effect_call("legacy", "late-approved-send", revision=1)
    else:
        assert not store.claim_workflow_effect(sid, "legacy", "late-approved-send", revision=1)


def test_preview_is_read_phase_and_failed_automation_is_not_receipt():
    from types import SimpleNamespace
    from service.router.model_led import effectful_call, trusted_effect_receipt
    tool = SimpleNamespace(name="organize_files", category="fs_write")
    assert not effectful_call(tool, {"confirm": False})
    assert effectful_call(tool, {"confirm": True})
    for name, text in [("music", "(music error: fixture)"),
                       ("open_app", "(could not open 'Fixture': fixture)")]:
        assert not trusted_effect_receipt(SimpleNamespace(name=name),
            SimpleNamespace(status="succeeded", effect="read", text=text))


def test_read_only_shell_executes_without_mutation_claim(synthetic, owned_store):
    effects, _ = synthetic
    store, sid = owned_store
    claims = []
    def claim(tool, args):
        claims.append(tool.name)
        return store.claim_model_effect(sid, "one", tool.name, args)
    run([[discover("automation", "run_shell")], [call("run_shell", cmd="pwd")], "Fixture directory."],
        approval=Approval(True), claim_effect=claim,
        finish_effect=lambda c, ok: store.finish_model_effect(sid, c, verified=ok))
    assert effects == [("run_shell", {"cmd": "pwd"})] and not claims
    assert not store.pending_model_effects(sid)


def test_known_memory_receipts_have_correct_phases():
    from types import SimpleNamespace
    from service.router.model_led import has_effect_contract, trusted_effect_receipt
    for name, text in (("remember", "Saved memory 1: fixture"),
                       ("forget", "Forgot 1 matching memory record(s), including correction history."),
                       ("forget", "No exact matching memories were forgotten. Use recall to identify the statement to forget.")):
        t = SimpleNamespace(name=name)
        result = SimpleNamespace(status="succeeded", effect="read", text=text)
        assert has_effect_contract(t, result) and trusted_effect_receipt(t, result)
    assert not trusted_effect_receipt(SimpleNamespace(name="remember"),
        SimpleNamespace(status="succeeded", effect="read", text="Nothing saved. Explicit saving requires current user request."))


@pytest.mark.parametrize("text,verified", [("Cancelled 2 timers.", True), ("Cancelled 1 timer.", True),
    ("Nothing to cancel.", True), ("That matches several timers — which one?", False),
    ("Cancelled all maybe.", False), ("Cancelled 2 timers. But failed.", False)])
def test_timer_cancel_all_matches_exact_numeric_or_noop_receipt(text, verified):
    from types import SimpleNamespace
    from service.router.model_led import trusted_effect_receipt
    assert trusted_effect_receipt(SimpleNamespace(name="manage_timers"),
        SimpleNamespace(status="succeeded", effect="read", text=text)) is verified


@pytest.mark.parametrize("text", ["Zoomed the front window.", "Entered full screen for the front window.",
                                  "Exited full screen for the front window."])
def test_window_receipts_match_actual_builtin_actions(text):
    from types import SimpleNamespace
    from service.router.model_led import trusted_effect_receipt
    assert trusted_effect_receipt(SimpleNamespace(name="window_control"),
        SimpleNamespace(status="succeeded", effect="read", text=text))


def test_resolve_endpoint_lifts_uncertain_barrier_only_on_explicit_request(owned_store, monkeypatch):
    from fastapi.testclient import TestClient
    from service import main
    store, sid = owned_store
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    json_type = {"Content-Type": "application/json"}
    claim = store.claim_model_effect(sid, "one", "place_call", {"to": "A"}, outbound=True)
    store.finish_model_effect(sid, claim, verified=False)
    assert store.pending_model_effects(sid)
    url = f"/sessions/{sid}/effects/resolve"
    assert client.post("/sessions/missing-session/effects/resolve", json={"all": True}).status_code == 404
    # A cross-site "simple" request cannot lift the barrier: no body, no JSON type.
    assert client.post(url).status_code == 415
    assert client.post(url, content='{"all": true}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(url, data={"all": "true"}).status_code == 415
    assert client.post(url, content="", headers=json_type).status_code == 422
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json=[]).status_code == 422
    assert client.post(url, json={"effect_id": 7}).status_code == 422
    assert client.post(url, json={"all": "yes"}).status_code == 422
    assert client.post(url, json={"all": True, "effect_id": claim["effect_id"]}).status_code == 422
    assert store.pending_model_effects(sid)  # nothing resolved by rejected requests
    ok = client.post(url, json={"effect_id": claim["effect_id"]})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "resolved": 1}
    assert not store.pending_model_effects(sid) and not store.unresolved_action(sid)


def test_resolve_endpoint_all_resolves_every_pending_effect(owned_store, monkeypatch):
    from fastapi.testclient import TestClient
    from service import main
    store, sid = owned_store
    monkeypatch.setattr(main, "store", store)
    claim = store.claim_model_effect(sid, "one", "place_call", {"to": "A"}, outbound=True)
    store.finish_model_effect(sid, claim, verified=False)
    reply = TestClient(main.app).post(f"/sessions/{sid}/effects/resolve", json={"all": True})
    assert reply.json() == {"ok": True, "resolved": 1}
    assert not store.pending_model_effects(sid)


def test_malformed_legacy_row_does_not_break_legacy_claims_or_the_model_barrier(owned_store):
    """Admission never parses unrelated legacy state_json (legacy paths skip it entirely)."""
    import time
    from service.workflows.models import WorkflowPlan
    store, sid = owned_store
    other = store.create_session()
    now = time.time()
    for session in (sid, other):
        store._db.execute(
            "INSERT INTO workflows (id,session_id,kind,status,state_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (f"legacy-bad-{session}", session, "delivery", "failed", "{not json", now, now))
    store._db.commit()
    # Legacy claim paths: only an unresolved model effect blocks them.
    plan = WorkflowPlan(status="running", channel="email", recipient="One", revision=1)
    store.save_workflow(other, plan.to_dict())
    assert store.claim_effect_call(plan.id, "call", revision=1)
    assert store.claim_workflow_effect(other, plan.id, "wf", revision=1)
    # Model barrier still applies in the session holding the malformed legacy row.
    first = store.claim_model_effect(sid, "one", "send_message", {"to": "A"}, outbound=True)
    assert first["admitted"] and store.finish_model_effect(sid, first, verified=False)
    plan2 = WorkflowPlan(status="running", channel="email", recipient="Two", revision=1)
    store.save_workflow(sid, plan2.to_dict())
    assert not store.claim_effect_call(plan2.id, "call2", revision=1)


@pytest.mark.parametrize("status", ["failed", "running"])
def test_user_resolution_lifts_a_legacy_typed_task_with_a_consumed_claim(owned_store, status):
    """0.5.0-rc3 audit M2: an rc2 typed task that consumed its effect claim and
    never reached a verified result blocked every model-proposed action in the
    chat, forever, and resolve only cleared model-led rows. The user's
    attestation now lifts it too, by status only: the legacy state is not
    rewritten and the claim stays consumed."""
    from service.workflows.models import WorkflowPlan
    store, sid = owned_store
    plan = WorkflowPlan(status="running", channel="email", recipient="Fixture", revision=1)
    store.save_workflow(sid, plan.to_dict())
    assert store.claim_effect_call(plan.id, "legacy-call", revision=1)
    store._db.execute("UPDATE workflows SET status=? WHERE id=?", (status, plan.id))
    store._db.commit()
    before = store._db.execute("SELECT state_json FROM workflows WHERE id=?", (plan.id,)).fetchone()[0]
    assert store.unresolved_action(sid) == {"id": plan.id, "status": status}
    assert not store.claim_model_effect(sid, "new", "send_message", {"to": "A"}, outbound=True)["admitted"]
    # Another session's legacy row is untouched by this session's resolution.
    other = store.create_session()
    other_plan = WorkflowPlan(status="running", channel="email", recipient="Fixture", revision=1)
    store.save_workflow(other, other_plan.to_dict())
    assert store.claim_effect_call(other_plan.id, "other-call", revision=1)
    assert store.resolve_model_effects(sid, "unknown-id") == 0
    assert store.resolve_model_effects(sid) == 1
    assert store.unresolved_action(sid) is None and store.unresolved_action(other)
    row = store._db.execute("SELECT status, state_json FROM workflows WHERE id=?", (plan.id,)).fetchone()
    assert row[0] == "completed" and row[1] == before
    assert store._db.execute("SELECT count(*) FROM task_effect_claims WHERE plan_id=?",
                             (plan.id,)).fetchone()[0] == 1
    # A legacy row with no consumed claim is not anyone's barrier and is left alone.
    idle = WorkflowPlan(status="running", channel="email", recipient="Fixture", revision=1)
    store.save_workflow(sid, idle.to_dict())
    assert store.resolve_model_effects(sid) == 0
    assert store._db.execute("SELECT status FROM workflows WHERE id=?", (idle.id,)).fetchone()[0] != "completed"
    assert store.claim_model_effect(sid, "new", "send_message", {"to": "A"}, outbound=True)["admitted"]
