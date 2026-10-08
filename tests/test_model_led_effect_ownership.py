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
    "software_update": {"action": "install", "confirm": True},
    "uninstall_app": {"name": "FixtureApp"},
    "http_request": {"url": "https://fixture.example.invalid", "method": "POST", "body": "{}"},
    "manage_contacts": {"action": "create", "name": "Fixture"},
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
                                completion_code=204 if name == "http_request" else 0)
    _, family = fake_builtin(name, effects, receipt, monkeypatch)
    store, sid = owned_store
    script = [[discover(family, name)], [call(name, **BUILTIN_ARGS[name])], "Completed."] if model_led else [[call(name, **BUILTIN_ARGS[name])], "Completed."]
    run(script, prompt="Perform the explicit synthetic fixture action", approval=Approval(True),
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
    code = 503 if name == "http_request" else 17
    if kind == "malformed":
        code = "0"
    if kind == "wrong_phase":
        code = 204 if name == "http_request" else 0
    receipt = ("Arbitrary nonempty stdout: SUCCESS" if kind == "missing" else
               BuiltinCompletion("Arbitrary body: SUCCESS", tool_name=name, completion_code=code,
                                 phase="read" if kind == "wrong_phase" else "mutation"))
    _, family = fake_builtin(name, effects, receipt, monkeypatch)
    store, sid = owned_store
    run([[discover(family, name)], [call(name, **BUILTIN_ARGS[name])],
         [call(name, **BUILTIN_ARGS[name])], "Attempted."], prompt="Perform the synthetic fixture action",
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


@pytest.mark.parametrize("name", ["run_shell", "software_update"])
def test_supported_builtin_read_phases_dispatch_without_action_claim(synthetic, owned_store, monkeypatch, name):
    effects, _ = synthetic
    args = {"cmd": "pwd"} if name == "run_shell" else {"action": "check"}
    _, family = fake_builtin(name, effects, "Synthetic read result.", monkeypatch)
    store, sid = owned_store
    run([[discover(family, name)], [call(name, **args)], "Read result."], approval=Approval(True),
        claim_effect=lambda *a: (_ for _ in ()).throw(AssertionError("Read acquired an action claim")))
    assert effects == [(name, args)] and not store.pending_model_effects(sid)


@pytest.mark.parametrize("name", list(BUILTIN_ARGS))
def test_actual_builtin_emitter_uses_host_status_not_stdout_or_body(monkeypatch, name):
    import asyncio, sys
    from pathlib import Path
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
    elif name == "software_update":
        monkeypatch.setattr(module, "_run", lambda *a, **k: native)
    elif name == "uninstall_app":
        monkeypatch.setattr(Path, "exists", lambda p: str(p) == "/Applications/FixtureApp.app")
        monkeypatch.setattr(module, "_osa", lambda *a, **k: native)
    elif name == "manage_contacts":
        monkeypatch.setattr(module, "_osa", lambda *a, **k: native)
    else:
        class FakeHTTP:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def request(self, *args, **kwargs):
                return SimpleNamespace(status_code=native.returncode, text=native.stdout)
        native.returncode = 204
        monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    result = (asyncio.run(tool.func(**BUILTIN_ARGS[name])) if name == "http_request"
              else tool.func(**BUILTIN_ARGS[name]))
    assert builtin_completion_verified(tool, result)
    displayed = str(result)
    native.returncode = 503 if name == "http_request" else 17
    failed = (asyncio.run(tool.func(**BUILTIN_ARGS[name])) if name == "http_request"
              else tool.func(**BUILTIN_ARGS[name]))
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
        fresh = WorkflowPlan(status="running", channel="email", recipient="Changed", revision=1)
        other.save_workflow(sid, fresh.to_dict())
        assert not other.claim_workflow_effect(sid, fresh.id, "fresh-outbound", revision=1)
        assert not other.claim_effect_call(fresh.id, "fresh-task-call", revision=1)
        assert other._db.execute("SELECT count(*) FROM task_effect_claims").fetchone()[0] == 1
    finally:
        other._db.close()


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
    for name, text in [("run_shortcut", "(the Shortcut ran into a problem: fixture)"),
                       ("convert_file", "(no such file: fixture)")]:
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


def test_power_receipts_match_actual_builtin_verbs():
    from types import SimpleNamespace
    from service.router.model_led import trusted_effect_receipt
    for text in ("Restart initiated.", "Shut down initiated.", "Sleep initiated.", "Log out initiated."):
        assert trusted_effect_receipt(SimpleNamespace(name="power_control"),
            SimpleNamespace(status="succeeded", effect="read", text=text))


def test_known_memory_receipts_and_clear_preview_have_correct_phases():
    from types import SimpleNamespace
    from service.router.model_led import effectful_call, has_effect_contract, trusted_effect_receipt
    tool = SimpleNamespace(name="clear_memory", category="fs_delete")
    assert not effectful_call(tool, {"confirm": False})
    assert effectful_call(tool, {"confirm": True})
    for name, text in (("remember", "Saved memory 1: fixture"),
                       ("forget", "Forgot 1 matching memory record(s), including correction history."),
                       ("forget", "No exact matching memories were forgotten. Use recall to identify the statement to forget."),
                       ("clear_memory", "Forgot 0 memory record(s), including their correction history. Conversations were retained.")):
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
