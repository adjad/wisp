"""Scheduled-send review against fictional SQLite rows; native effects forbidden."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from service.assistant import outbound_queue as queues
from service.assistant.outbound_queue import OutboundQueue, REVIEW_STATUSES
from service.tools import action_tools as actions
from service.tools.registry import classify_tool_outcome


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = OutboundQueue(tmp_path / "fictional.sqlite")
    monkeypatch.setattr(queues, "outbound_queue", q)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No native effect may run in pending-action tests")

    monkeypatch.setattr(actions, "app_request", forbidden)
    from service.assistant import outbox
    monkeypatch.setattr(outbox, "request", forbidden)
    yield q
    q._db.close()


def add(queue, *, status="pending", body="Fictional full message", when=1791824400.0,
        created=1791800000.0, fired=None, channel="message", error=""):
    sid = queue.add(channel=channel, recipient="morgan@example.com", display="Morgan Lee",
                    body=body, subject="Fictional agenda" if channel == "email" else "", when_ts=when)
    queue._db.execute("UPDATE scheduled_sends SET status=?, created_at=?, fired_at=?, error=? WHERE id=?",
                      (status, created, fired, error, sid))
    queue._db.commit()
    return sid


def review(**args):
    return asyncio.run(actions.list_scheduled_sends(**args))


def cancel(sid):
    return asyncio.run(actions.cancel_scheduled_send(sid))


def snapshot(queue):
    return [tuple(row) for row in queue._db.execute("SELECT * FROM scheduled_sends ORDER BY id")]


def test_default_is_pending_only_and_read_only(queue):
    pending = add(queue)
    sent = add(queue, status="sent")
    before = snapshot(queue)
    text = review()
    assert pending in text and sent not in text
    assert "Recorded status: pending" in text
    assert "no send attempt is recorded" in text
    assert snapshot(queue) == before


@pytest.mark.parametrize("status,truth", [
    ("pending", "no send attempt is recorded"),
    ("sending", "final outcome is not recorded"),
    ("sent", "app accepted the send request"),
    ("missed", "retired without dispatch"),
    ("cancelled", "will not dispatch"),
    ("failed", "recorded as failed"),
    ("unknown", "may have been sent"),
])
def test_all_statuses_have_truthful_exact_id_full_body(queue, status, truth):
    body = "First line\n" + "source content " * 80 + "\nFINAL BODY SENTINEL"
    sid = add(queue, status=status, body=body, fired=1791810000.0, error="fictional failure" if status == "failed" else "")
    before = snapshot(queue)
    text = review(id=sid)
    assert f"Recorded status: {status}" in text
    assert truth in text and body in text
    assert "Full stored body:" in text and "truncated" not in text
    assert "Last recorded send attempt/outcome:" in text
    assert snapshot(queue) == before
    if status == "sent":
        assert "Recipient delivery and read receipts are not verified" in text
    if status == "unknown":
        assert "will not retry automatically" in text


def test_exact_id_overrides_status_and_count(queue):
    sid = add(queue, status="unknown")
    assert sid in review(id=sid, status="pending", count=1)
    # Overrides apply before list validation; no ambiguity about scope.
    assert sid in review(id=sid, status="invalid", count=0)


def test_id_prefix_does_not_select_or_modify_record(queue):
    sid = add(queue)
    before = snapshot(queue)
    text = review(id=sid[:5])
    assert "No scheduled-send record matches exact ID" in text
    assert "no delivery" not in text  # Must not imply a negative delivery outcome.
    assert "No send, cancellation or delivery outcome can be established" in text
    assert snapshot(queue) == before


def test_quoted_id_is_parameterized(queue):
    add(queue)
    text = review(id="' OR 1=1 --")
    assert "No scheduled-send record matches exact ID" in text
    assert "Full stored body:" not in text


@pytest.mark.parametrize("status", REVIEW_STATUSES)
def test_status_filter_is_exact(queue, status):
    ids = {s: add(queue, status=s) for s in REVIEW_STATUSES}
    text = review(status=status)
    assert ids[status] in text
    assert not any(sid in text for s, sid in ids.items() if s != status)


def test_all_and_history_cover_terminal_and_inflight_rows(queue):
    ids = {s: add(queue, status=s) for s in REVIEW_STATUSES}
    assert all(sid in review(status="all") for sid in ids.values())
    history = review(status="history")
    assert ids["pending"] not in history
    assert all(sid in history for s, sid in ids.items() if s != "pending")
    assert "Cancellation and missed-window times are not separately recorded" in history


def test_pending_order_and_list_limit_are_disclosed(queue):
    later = add(queue, when=3000)
    earlier = add(queue, when=1000)
    last = add(queue, when=5000)
    text = review(count=2)
    assert text.index(earlier) < text.index(later)
    assert last not in text
    assert "additional matching records are omitted by count=2" in text


def test_history_uses_attempt_or_creation_and_not_future_due_time(queue):
    older = add(queue, status="sent", created=1000, fired=2000, when=9000)
    newer = add(queue, status="failed", created=1000, fired=4000, when=6000)
    cancelled = add(queue, status="cancelled", created=3000, when=10000)
    text = review(status="history", count=2)
    assert text.index(newer) < text.index(cancelled)
    assert older not in text


def test_preview_explicitly_discloses_truncation_and_exact_id_inspection(queue):
    body = "x" * 180 + "FULL END SENTINEL"
    sid = add(queue, body=body)
    preview = review()
    assert "Body preview truncated" in preview and sid in preview
    assert "FULL END SENTINEL" not in preview
    assert body in review(id=sid)


def test_email_full_body_keeps_exact_subject_and_recipient(queue):
    sid = add(queue, channel="email", body="line one\nline two")
    text = review(id=sid)
    assert "Email to Morgan Lee" in text
    assert "Recipient: morgan@example.com" in text
    assert "Subject: Fictional agenda" in text
    assert "line one\nline two" in text


@pytest.mark.parametrize("scope", ["pending", "sent", "all", "history"])
def test_empty_view_does_not_infer_delivery(queue, scope):
    text = review(status=scope)
    assert "does not establish recipient delivery" in text


def test_unknown_stored_status_is_exposed_without_success_inference(queue):
    sid = add(queue, status="legacy-mystery")
    assert "Unrecognized stored status" in review(id=sid)
    assert sid in review(status="history")


def test_null_stored_status_is_not_silently_omitted_from_history(queue):
    sid = add(queue, status=None)
    assert "Recorded status: unrecognized" in review(id=sid)
    assert sid in review(status="history")


@pytest.mark.parametrize("count", [0, -1, 101, True, 1.5, "20", None])
def test_invalid_count_is_rejected_before_query(queue, count):
    assert "error:" in review(count=count)


@pytest.mark.parametrize("status", ["delivered", "read", "sent' OR 1=1 --", None])
def test_unsupported_scope_never_invents_delivery_state(queue, status):
    assert "error:" in review(status=status)


def test_full_review_does_not_sweep_overdue_pending_or_recover_sending(queue):
    pending = add(queue, when=1)
    sending = add(queue, status="sending", when=1)
    before = snapshot(queue)
    review(status="all")
    assert queue.get(pending)["status"] == "pending"
    assert queue.get(sending)["status"] == "sending"
    assert snapshot(queue) == before


def test_cancel_pending_prevents_claim(queue):
    sid = add(queue)
    text = cancel(sid)
    assert f"Cancelled [{sid}]" in text
    assert classify_tool_outcome("cancel_scheduled_send", text).status == "succeeded"
    assert queue.get(sid)["status"] == "cancelled"
    assert queue.claim(sid) is False


def test_claim_before_cancel_cannot_be_recalled(queue):
    sid = add(queue)
    assert queue.claim(sid)
    text = cancel(sid)
    assert "NOT cancelled" in text and "Recorded status: sending" in text
    assert "cannot recall" in text
    assert classify_tool_outcome("cancel_scheduled_send", text).status == "failed"
    assert queue.get(sid)["status"] == "sending"


@pytest.mark.parametrize("status", ["sent", "failed", "missed", "unknown"])
def test_cancelling_nonpending_only_reports_recorded_truth(queue, status):
    sid = add(queue, status=status)
    before = snapshot(queue)
    text = cancel(sid)
    assert "NOT cancelled" in text and f"Recorded status: {status}" in text
    assert classify_tool_outcome("cancel_scheduled_send", text).status == "failed"
    assert snapshot(queue) == before


def test_repeated_cancellation_does_not_create_a_new_effect(queue):
    sid = add(queue)
    cancel(sid)
    before = snapshot(queue)
    text = cancel(sid)
    assert "Already recorded as cancelled" in text
    assert classify_tool_outcome("cancel_scheduled_send", text).status == "no_match"
    assert snapshot(queue) == before


@pytest.mark.parametrize("sid", ["unknown-id", "' OR 1=1 --", "", None, 123])
def test_invalid_or_unknown_cancel_never_claims_success(queue, sid):
    real = add(queue)
    before = snapshot(queue)
    text = cancel(sid)
    assert "NOT cancelled" in text
    expected = "needs_input" if not isinstance(sid, str) or not sid.strip() else "no_match"
    assert classify_tool_outcome("cancel_scheduled_send", text).status == expected
    assert queue.get(real)["status"] == "pending"
    assert snapshot(queue) == before


def test_cancel_and_claim_arbitrate_across_connections(tmp_path):
    path = tmp_path / "competing.sqlite"
    first, second = OutboundQueue(path), OutboundQueue(path)
    try:
        for _ in range(30):
            sid = add(first)
            barrier = threading.Barrier(2)

            def claim():
                barrier.wait()
                return first.claim(sid)

            def cancellation():
                barrier.wait()
                return second.cancel(sid) is not None

            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = pool.submit(claim)
                cancelled = pool.submit(cancellation)
                outcomes = claimed.result(), cancelled.result()
            assert sum(outcomes) == 1
            assert first.get(sid)["status"] == ("sending" if outcomes[0] else "cancelled")
    finally:
        first._db.close()
        second._db.close()


def test_registry_keeps_read_and_cancel_categories_and_optional_defaults():
    from service.tools.registry import REGISTRY
    tool = REGISTRY["list_scheduled_sends"]
    assert tool.category == "assistant_read"
    assert not tool.parameters.get("required")
    assert set(tool.parameters["properties"]) == {"status", "count", "id"}
    assert REGISTRY["cancel_scheduled_send"].category == "assistant_write"


def test_invalid_timestamp_is_reported_without_fabricating_a_time(queue):
    sid = add(queue, when=1e100)
    assert "Scheduled: unavailable" in review(id=sid)
