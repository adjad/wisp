"""Reply selection, approval binding, and receipt checks with no live Mail sends."""
import asyncio
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.tasks.engine import prepare_task_turn, finish_task
from service.tasks.executor import execute_task
from service.tasks.models import TaskPlan
from service.tasks.references import SourceRef, resolve_reference, select_candidate
from service.tasks.reply_contract import make_receipt, verify_receipt
from service.tasks.reply_engine import prepare_task_turn_async
from service.tasks.source_readers import CalendarReader, MailReader
from service.tools import action_tools
from service.tools.registry import REGISTRY, run_tool

NOW = datetime(2026, 9, 8, 10)
ENVELOPE = {"message_id": "<one@fixture>", "account": "Work", "account_id": "acct-1",
            "from": "me@work.example", "to": ["reply@dan.example"],
            "cc": [], "bcc": [], "subject": "Re: Dinner", "content": "Thanks\rOriginal email"}


def row(account="Work", mid="<one@fixture>", **kw):
    return {"message_id": mid, "account": account, "sender": "Dan <dan@fixture.example>",
            "to": "me@work.example", "subject": "Dinner", "body": "Original email",
            "ts": NOW.timestamp(), **kw}


@pytest.fixture
def stores(tmp_path):
    return SessionStore(tmp_path / "sessions.db"), AssistantStore(tmp_path / "assistant.db")


def reader(*rows, **kwargs):
    return MailReader(list(rows), accounts=["Work", "Personal"], synced_at=NOW.timestamp(), **kwargs)


def resolve(r, **hints):
    return resolve_reference(SourceRef("email", hints=hints), r, now=NOW)


def turn(stores, prompt, r):
    s, a = stores
    return prepare_task_turn(s, "s", prompt, assistant_store=a, now=NOW, mail_reader=r)


def test_same_message_id_in_two_accounts_is_ambiguous():
    result = resolve(reader(row(), row("Personal")), sender="Dan")
    assert result.status == "ambiguous"
    assert len({c.id for c in result.candidates}) == 2
    assert resolve(reader(row(), row("Personal")), account="Work").status == "resolved"


def test_no_subject_based_thread_collapse_or_top_five_false_uniqueness():
    result = resolve(reader(*(row(mid=f"<{i}>") for i in range(7))), sender="Dan")
    assert result.total_matches == 7 and len(result.candidates) == 5
    assert result.status == "ambiguous" and "2 more" in result.question
    assert select_candidate([c.to_dict() for c in result.candidates], "6") is None


def test_selection_uses_the_displayed_order_and_rejects_ambiguous_labels():
    result = resolve(reader(row(), row("Personal")), sender="Dan")
    offered = [c.to_dict() for c in result.candidates]
    assert select_candidate(offered, "the second one").fields["account"] == "Personal"
    assert select_candidate(offered, "Dinner") is None
    assert select_candidate(offered, "check my email") is None


def test_partial_scan_cannot_assert_uniqueness_or_no_match():
    r = reader(row(), complete=False, failed_accounts=["Personal"])
    assert resolve(r, sender="Dan").status == "unavailable"
    assert resolve(r, sender="Nobody").status == "unavailable"
    assert resolve(r, sender="Dan", account="Work").status == "resolved"


def test_empty_success_and_cold_cache_have_distinct_results():
    assert resolve(reader()).status == "no_match"
    assert "recent synced inbox" in resolve(reader()).question
    assert resolve(reader(available=False)).status == "unavailable"


def test_deep_non_actionable_hit_is_visible():
    result = resolve(reader(deep_rows=[row(mid="")]), sender="Dan")
    assert result.status == "not_actionable"
    assert "can see" in result.question


def test_calendar_uses_same_cardinality_contract_without_reminder_migration():
    r = CalendarReader([{"id": "1", "title": "move-in date", "source": "calendar", "when_ts": 10},
                        {"id": "2", "title": "move-in date", "source": "calendar", "when_ts": 20}])
    result = resolve_reference(SourceRef("calendar", "my move-in date"), r, now=NOW)
    assert result.status == "ambiguous"
    assert select_candidate([c.to_dict() for c in result.candidates], "second").fields["id"] == "2"


def test_unqualified_that_email_does_not_select_the_only_cached_message(stores):
    opened = turn(stores, "reply to that email saying Thanks", reader(row()))
    assert opened.event == "source_reference_needed"
    assert not opened.executable


def test_multi_turn_account_selection_preserves_body_and_snapshot(stores):
    r = reader(row(), row("Personal"))
    opened = turn(stores, "reply to the email from Dan saying Thanks", r)
    assert opened.event == "source_ambiguous"
    picked = turn(stores, "the second one", r)
    assert picked.plan.subject.value == "Thanks"
    assert picked.plan.resolved_references["reply.target"]["fields"]["account"] == "Personal"
    assert TaskPlan.from_dict(picked.plan.to_dict()).to_dict() == picked.plan.to_dict()
    assert picked.event == "reply_prepare"


def test_unrelated_prompt_leaves_pending_source_intact(stores):
    r = reader(row(), row("Personal"))
    turn(stores, "reply to the email from Dan saying Thanks", r)
    assert turn(stores, "check my weather", r) is None
    assert stores[0].active_task("s")["intent"] == "email.reply"


def test_cold_source_is_pending_and_never_falls_through(stores):
    opened = turn(stores, "reply to Dan's email saying Thanks", reader(available=False))
    assert opened.event == "source_unavailable" and not opened.executable
    recovered = turn(stores, "try again", reader(row()))
    assert recovered.event == "reply_prepare"


async def prepared(args):
    return {**args, "expected_reply": deepcopy(ENVELOPE)}, ""


async def ready(stores, **kw):
    s, a = stores
    return await prepare_task_turn_async(s, "s", "reply to Dan's email saying Thanks",
                                        assistant_store=a, now=NOW, mail_reader=reader(row()),
                                        reply_preparer=prepared, **kw)


def test_dry_run_does_not_prepare_native_compose(stores):
    prepare = AsyncMock(side_effect=AssertionError("native preparation called"))
    s, a = stores
    result = asyncio.run(prepare_task_turn_async(
        s, "s", "reply to Dan's email saying Thanks", assistant_store=a, now=NOW,
        mail_reader=reader(row()), reply_preparer=prepare, allow_native=False))
    assert not result.executable and "Dry run" in result.response
    prepare.assert_not_called()


def test_unavailable_native_source_never_approves_or_substitutes(stores):
    s, a = stores
    prepare = AsyncMock(return_value=(None, "The selected message moved. Nothing sent."))
    result = asyncio.run(prepare_task_turn_async(
        s, "s", "reply to Dan's email saying Thanks", assistant_store=a, now=NOW,
        mail_reader=reader(row()), reply_preparer=prepare))
    assert result.event == "reply_unavailable" and not result.executable
    assert result.plan.steps == []


@pytest.mark.parametrize("field,value", [("message_id", "wrong"), ("account", "Other"),
                                         ("from", "other@fixture.example"), ("to", []),
                                         ("cc", ["extra@fixture.example"]),
                                         ("subject", "Other"), ("content", "Changed")])
def test_receipt_checks_all_approved_fields(field, value):
    changed = {**ENVELOPE, field: value}
    assert not verify_receipt(make_receipt(changed), ENVELOPE)


def test_missing_receipt_is_never_success(monkeypatch):
    bridge = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(action_tools, "app_request", bridge)
    result = asyncio.run(run_tool(REGISTRY["reply_to_email"], {
        "message_id": ENVELOPE["message_id"], "account": "Work", "body": "Thanks",
        "expected_reply": ENVELOPE}))
    assert "outcome unknown" in result and not result.startswith("Reply sent")


def test_preparer_replaces_model_supplied_envelope(monkeypatch):
    bridge = AsyncMock(return_value={"ok": True, "reply": ENVELOPE})
    monkeypatch.setattr(action_tools, "app_request", bridge)
    args, error = asyncio.run(action_tools.prepare_reply_args({
        "message_id": ENVELOPE["message_id"], "account": "Work", "body": "Thanks",
        "expected_reply": {"to": ["invented@example.com"]}}))
    assert not error and args["expected_reply"] == ENVELOPE
    assert bridge.call_args.args[0] == "prepare_email_reply"


def test_exact_native_reply_is_previewed_sent_and_verified(stores, monkeypatch):
    bridge = AsyncMock(return_value={"ok": True, "accepted": True, "reply": ENVELOPE})
    monkeypatch.setattr(action_tools, "app_request", bridge)
    p = asyncio.run(ready(stores)).plan
    approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
    result = asyncio.run(execute_task(p, AsyncMock(), approver))
    assert result.status == "completed"
    preview = approver.confirm.call_args.args[0]["preview"]
    assert "From: me@work.example" in preview and "To: reply@dan.example" in preview
    assert "Re: Dinner" in preview and ENVELOPE["content"] in preview
    assert bridge.await_count == 1
    assert bridge.call_args.args[1]["expected_reply"] == ENVELOPE


def test_denial_has_no_effect_and_cannot_be_revived_by_yes(stores, monkeypatch):
    bridge = AsyncMock(side_effect=AssertionError("denied send called bridge"))
    monkeypatch.setattr(action_tools, "app_request", bridge)
    p = asyncio.run(ready(stores)).plan
    result = asyncio.run(execute_task(p, AsyncMock(), type("Approver", (), {"confirm": AsyncMock(return_value=False)})()))
    assert result.status == "denied"
    finish_task(stores[0], "s", p, status="denied")
    follow = turn(stores, "yes", reader(row()))
    assert follow.event == "closed_send_followup" and not follow.executable


def test_reply_claim_blocks_two_loaded_copies(stores, monkeypatch):
    bridge = AsyncMock(return_value={"ok": True, "accepted": True, "reply": ENVELOPE})
    monkeypatch.setattr(action_tools, "app_request", bridge)
    p = asyncio.run(ready(stores)).plan
    other = TaskPlan.from_dict(p.to_dict())
    async def approve(_):
        await asyncio.sleep(0.01)
        return True
    approver = type("Approver", (), {"confirm": staticmethod(approve)})()
    async def run():
        return await asyncio.gather(*(execute_task(
            plan, AsyncMock(), approver,
            on_claim=lambda p, cid: stores[0].claim_effect_call(p.id, cid)) for plan in (p, other)))
    result = asyncio.run(run())
    assert sorted(r.status for r in result) == ["completed", "failed"]
    assert bridge.await_count == 1


def test_restored_cache_not_promoted_to_fresh_and_empty_sync_is_ready(monkeypatch):
    from service.tools import email_tools as mail
    monkeypatch.setattr(mail, "_raw_reference_scan", {})
    monkeypatch.setattr(mail, "_raw_emails", "stale")
    monkeypatch.setattr(mail, "_raw_emails_at", 123)
    monkeypatch.setattr(mail.cache_store, "save", lambda *a: None)
    assert not mail.raw_reference_metadata()["available"]
    mail.cache_raw_emails("", {"accounts": ["Work"], "failed_accounts": [], "complete": True})
    assert mail.raw_reference_metadata()["available"]
    assert mail.raw_reference_metadata()["complete"]


def test_bare_reply_does_not_invent_email_channel():
    assert compile_task("reply to Dan saying Thanks", now=NOW) is None


def test_short_sender_answer_and_narrowing_preserve_prior_hints(stores):
    r = reader(row(), row(mid="<two>", subject="Lunch"))
    turn(stores, "reply to that email saying Thanks", r)
    by_sender = turn(stores, "Dan", r)
    assert by_sender.event == "source_ambiguous"
    narrowed = turn(stores, "about Dinner", r)
    assert narrowed.event == "reply_prepare"
    assert narrowed.plan.resolved_references["reply.target"]["fields"]["message_id"] == "<one@fixture>"
    assert narrowed.plan.parameters["reference_hints"].value == {"sender": "Dan", "topic": "Dinner"}


def test_correction_invalidates_old_approval_and_late_completion(stores, monkeypatch):
    bridge = AsyncMock(side_effect=AssertionError("old revision sent"))
    monkeypatch.setattr(action_tools, "app_request", bridge)
    old = asyncio.run(ready(stores)).plan
    s, a = stores
    async def corrected(args):
        return {**args, "expected_reply": {**ENVELOPE, "content": args["body"] + "\rOriginal email"}}, ""
    async def approve(_):
        updated = await prepare_task_turn_async(s, "s", "actually say No thanks",
                    assistant_store=a, now=NOW, mail_reader=reader(row()), reply_preparer=corrected)
        assert updated.executable and updated.plan.revision > old.revision
        return True
    approver = type("Approver", (), {"confirm": staticmethod(approve)})()
    result = asyncio.run(execute_task(old, AsyncMock(), approver,
                         on_claim=lambda p, cid: s.claim_effect_call(p.id, cid, revision=p.revision)))
    assert result.status == "failed"
    finish_task(s, "s", old, status="failed", result=result.response)
    current = s.active_task("s")
    assert current["status"] == "running" and current["revision"] > old.revision
    assert current["subject"]["value"] == "No thanks"
    bridge.assert_not_called()


def test_cancellation_while_native_preparing_cannot_resurrect_task(stores):
    s, a = stores
    async def cancel_then_return(args):
        cancelled = turn(stores, "cancel", reader(row()))
        assert cancelled.plan.status == "cancelled"
        return await prepared(args)
    result = asyncio.run(prepare_task_turn_async(
        s, "s", "reply to Dan's email saying Thanks", assistant_store=a, now=NOW,
        mail_reader=reader(row()), reply_preparer=cancel_then_return))
    assert not result.executable and result.event == "stale_preparation"
    assert s.latest_task("s")["status"] == "cancelled"


def test_losing_execution_cannot_overwrite_winning_completion(stores):
    s, _ = stores
    winner = asyncio.run(ready(stores)).plan
    loser = TaskPlan.from_dict(winner.to_dict())
    cid = f"task_{winner.id}_{winner.steps[0].id}_{winner.revision}"
    winner.claimed_calls.append(cid)
    assert s.claim_effect_call(winner.id, cid, revision=winner.revision)
    s.save_workflow("s", winner.to_dict())
    finish_task(s, "s", loser, status="failed", result="duplicate")
    assert s.active_task("s")["status"] == "running"
    finish_task(s, "s", winner, status="completed", result="sent")
    assert s.latest_task("s")["status"] == "completed"


def test_failed_raw_scan_preserves_display_cache_but_blocks_reference_resolution(monkeypatch):
    from service.tools import email_tools as mail
    monkeypatch.setattr(mail, "_raw_reference_scan", {})
    monkeypatch.setattr(mail, "_raw_emails", "older display cache")
    mail.cache_raw_emails("", {"complete": False, "accounts": [], "failed_accounts": ["Work"]})
    assert mail._raw_emails == "older display cache"
    assert not mail.raw_reference_metadata()["complete"]


def test_reply_preview_keeps_literal_escapes_and_multiline_text(monkeypatch):
    text = "Path C:\\new\\test\nSecond line"
    expected = {**ENVELOPE, "content": text + "\rOriginal email"}
    bridge = AsyncMock(return_value={"ok": True, "reply": expected})
    monkeypatch.setattr(action_tools, "app_request", bridge)
    args, _ = asyncio.run(action_tools.prepare_reply_args({"message_id": ENVELOPE["message_id"],
                                                          "account": "Work", "body": text}))
    assert args["body"] == text
    bridge.return_value = {"ok": True, "accepted": True, "reply": expected}
    out = asyncio.run(run_tool(REGISTRY["reply_to_email"], args))
    assert out.startswith("Reply sent")
    assert bridge.call_args.args[1]["body"] == text
