"""read_email_attachments: the tool, its routing words and its safety wiring.

service/mail_attachments.py is another slice's module, so a fake stands in for
it here. Nothing reads real mail, loads a model or touches the network.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys
import tempfile
import types

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-email-attachment-tool-"))

import pytest

from service.agent import loop
from service.agent.approver import InteractiveApprover as Approver
from service.model_only import ASK_CORE_STUBS, attachment_first
from service.safety import grants, policy
from service.tools import REGISTRY
from service.tools import email_tools, untrusted
from service.tools.short_descriptions import MAX_LEN, SHORT

TOOL = "read_email_attachments"


@pytest.fixture
def fake_attachments(monkeypatch):
    calls = []
    fake = types.ModuleType("service.mail_attachments")
    fake.ATTACHMENT_TOOL = TOOL

    async def run(**kwargs):
        calls.append(kwargs)
        return "read ok"
    fake.run = run
    fake.annotated = []

    async def annotate_rows(rows, timeout=1.5):
        fake.annotated.append(rows)
        return rows
    fake.annotate_rows = annotate_rows
    monkeypatch.setitem(sys.modules, "service.mail_attachments", fake)
    # `from service import x` reads the package attribute once the real module is loaded.
    import service
    monkeypatch.setattr(service, "mail_attachments", fake, raising=False)
    fake.calls = calls
    return fake


# ------------------------------------------------------------- registration

def test_the_tool_is_registered_as_an_email_read():
    tool = REGISTRY[TOOL]
    assert tool.category == "email_read"
    props = tool.parameters["properties"]
    assert set(props) == {"query", "name", "period", "day", "count", "account", "question", "list_only",
                          "pages", "sheet", "inside", "across"}
    for needed in ("pictures", "spreadsheets", "PDFs", "SEVERAL emails", "never run",
                   "Apple Mail", "Gmail", "question", "list_only", "view_emails"):
        assert needed in tool.description, needed


def test_the_tool_is_a_core_stub_and_has_a_short_line():
    assert TOOL in ASK_CORE_STUBS
    assert TOOL in SHORT and len(SHORT[TOOL]) <= MAX_LEN


def test_attachment_text_has_its_own_fence():
    assert untrusted.EMAIL_ATTACHMENT == "email attachment"
    fenced = untrusted.fence("hello", untrusted.EMAIL_ATTACHMENT)
    assert "email attachment" in fenced
    assert untrusted.strip_fenced(fenced, untrusted.EMAIL_ATTACHMENT).strip() == ""


# ------------------------------------------------------------- the call

def test_arguments_reach_the_reader_with_resolved_dates(fake_attachments):
    out = asyncio.run(REGISTRY[TOOL].func(
        query="john", name="invoice", day="2026-11-03", count=3, account="work",
        question="what is the total", list_only=True))
    assert out == "read ok"
    call = fake_attachments.calls[0]
    assert call["query"] == "john" and call["name"] == "invoice"
    assert call["count"] == 3 and call["account"] == "work"
    assert call["question"] == "what is the total" and call["list_only"] is True
    assert call["end"] - call["start"] == 86400
    assert call["label"]


def test_a_period_wins_over_a_day_and_bad_dates_are_plain(fake_attachments):
    asyncio.run(REGISTRY[TOOL].func(period="last week", day="2026-11-03"))
    call = fake_attachments.calls[0]
    assert call["end"] - call["start"] > 86400
    assert "couldn't understand" in asyncio.run(REGISTRY[TOOL].func(day="someday"))
    assert len(fake_attachments.calls) == 1


def test_a_corpus_run_is_rejected(fake_attachments):
    with _corpus("a@b.example"):
        out = asyncio.run(REGISTRY[TOOL].func(query="a@b.example"))
    assert "rejected" in out
    assert fake_attachments.calls == []


class _corpus:
    def __init__(self, ident):
        self.ident = ident

    def __enter__(self):
        self.token = email_tools._CORPUS_MAIL_IDENTIFIER.set(self.ident)

    def __exit__(self, *exc):
        email_tools._CORPUS_MAIL_IDENTIFIER.reset(self.token)


# ------------------------------------------------------------- view_emails

def _row(i):
    return {"ts": 1_700_000_000 + i, "sender": "a@b.example", "to": "me@b.example",
            "subject": f"s{i}", "body": "hello", "message_id": f"<m{i}>"}


def _prepare_view(monkeypatch):
    async def ready(**kwargs):
        return None
    monkeypatch.setattr(email_tools, "_ensure_email_cache", ready)
    monkeypatch.setattr(email_tools, "_purge_raw_if_expired", lambda: None)
    monkeypatch.setattr(email_tools, "_parse_raw", lambda: [_row(1), _row(2)])
    monkeypatch.setattr(email_tools, "_raw_emails", "x")
    monkeypatch.setattr(email_tools, "_email_available", True)


def test_view_emails_annotates_before_digesting(monkeypatch, fake_attachments):
    _prepare_view(monkeypatch)
    out = asyncio.run(email_tools.view_emails_impl(None, None, 5))
    assert "s1" in out and "s2" in out
    assert len(fake_attachments.annotated) == 1


def test_a_failure_in_annotating_never_breaks_view_emails(monkeypatch, fake_attachments):
    _prepare_view(monkeypatch)

    async def boom(rows, timeout=1.5):
        raise RuntimeError("mail bridge fell over")
    fake_attachments.annotate_rows = boom
    out = asyncio.run(email_tools.view_emails_impl(None, None, 5))
    assert "s1" in out


def test_a_missing_module_never_breaks_view_emails(monkeypatch):
    _prepare_view(monkeypatch)
    monkeypatch.setitem(sys.modules, "service.mail_attachments", None)
    assert "s1" in asyncio.run(email_tools.view_emails_impl(None, None, 5))


def test_view_emails_description_points_at_the_new_tool():
    desc = REGISTRY["view_emails"].description
    assert "[Attached:" in desc and TOOL in desc


# ------------------------------------------------------------- routing words

@pytest.mark.parametrize("prompt", [
    "what's in the attachment",
    "What's in the attachment from Maria?",
    "read the PDF John emailed me",
    "the spreadsheet attached to that email",
    "the screenshot in her email",
    "summarize the document in my email from the landlord",
    "open the attached invoice",
])
def test_attachment_questions_lead_with_the_tool(prompt):
    assert attachment_first(prompt)


@pytest.mark.parametrize("prompt", [
    "email Sam the file",
    "attach this to my note",
    "what's the weather",
    "summarize my emails",
    "send Dana the spreadsheet",
    "what is the capital of France",
])
def test_other_prompts_do_not(prompt):
    assert not attachment_first(prompt)


# ------------------------------------------------------------- the picture turn

@pytest.fixture
def full_access(monkeypatch):
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", True)
    monkeypatch.setattr(grants, "check", lambda *args: None)


def _in_fresh_context(fn):
    return contextvars.copy_context().run(fn)


def test_reading_marks_the_turn_and_listing_does_not(full_access):
    def reading():
        loop.mark_attachment_turn(TOOL, {"query": "x"})
        return policy.picture_turn()

    def listing():
        loop.mark_attachment_turn(TOOL, {"list_only": True})
        return policy.picture_turn()

    def other_tool():
        loop.mark_attachment_turn("view_emails", {})
        return policy.picture_turn()
    assert _in_fresh_context(reading) is True
    assert _in_fresh_context(listing) is False
    assert _in_fresh_context(other_tool) is False


def test_a_later_send_in_the_same_turn_needs_confirmation_even_with_full_access(full_access):
    # Under full access a note is written without asking; sends always ask.
    note = {"title": "x", "body": "y"}
    send = {"to": "a@b.example", "subject": "s", "body": "b"}
    assert policy.decide("notes_write", note, tool="create_note").tier is policy.Tier.ALLOW

    def turn():
        loop.mark_attachment_turn(TOOL, {"query": "invoice"})
        return (policy.decide("email_read", {}, tool=TOOL),
                policy.decide("notes_write", note, tool="create_note"),
                policy.decide("email_send", send, tool="send_email"))
    read, write, sent = _in_fresh_context(turn)
    assert read.tier is policy.Tier.ALLOW
    assert write.tier is policy.Tier.CONFIRM
    assert sent.tier is policy.Tier.CONFIRM
    # A task that never read an attachment is unaffected.
    assert policy.decide("notes_write", note, tool="create_note").tier is policy.Tier.ALLOW


def test_the_card_after_an_attachment_read_offers_no_always_allow(monkeypatch):
    stored = []
    monkeypatch.setattr("service.agent.approver._apply_scope",
                        lambda action, approved, scope: stored.append(scope))
    events = []

    async def flow():
        loop.mark_attachment_turn(TOOL, {"query": "invoice"})

        async def emit(event):
            events.append(event)
        approver = Approver(emit, timeout=5)
        waiter = asyncio.create_task(approver.confirm(
            {"id": "c1", "tool": "send_email", "args": {"to": "a@b.example"}, "reason": "r"}))
        await asyncio.sleep(0)
        approver.resolve("c1", True, "always")
        return await waiter
    assert asyncio.run(flow()) is True
    card = next(e for e in events if e["type"] == "confirm")
    assert card["grantable"] is False
    assert stored == ["once"]
