"""Durable action ownership using disposable SQLite and synthetic arguments."""
import pytest

from service.memory.store import SessionStore
from service.router.model_led import needs_effect_owner
from tests.test_model_led_integration import synthetic, run, call, discover, Approval


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
