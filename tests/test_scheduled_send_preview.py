"""Truthful approval previews and single-recipient scheduled-send behavior.

All execution tests replace the persistent outbound queue with a recording
fake.  They never contact Mail, Messages, Contacts, or user data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from service.assistant import outbound_queue as queue_module
from service.tools import action_tools
from service.tools import timeranges


FUTURE = "2099-01-02T15:04:00"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def add(**kwargs):
        calls.append(kwargs)
        return "scheduled-test-id"

    monkeypatch.setattr(queue_module.outbound_queue, "add", add)
    return calls


def test_email_preview_shows_complete_delivery_contract(
        monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = datetime(
        2099, 1, 2, 15, 4, tzinfo=timezone(timedelta(hours=-8), "PST")
    )
    monkeypatch.setattr(
        timeranges, "resolve_when", lambda _when: (delivery, "fixture")
    )
    body = "First line.\nSecond line with every detail."
    preview = action_tools.confirm_preview("schedule_send", {
        "channel": "email",
        "to": "recipient@example.test",
        "when": FUTURE,
        "subject": "Complete subject",
        "body": body,
    })

    assert preview == (
        "Channel: Email\n"
        "To: recipient@example.test\n"
        f"Delivery time: {delivery:%a %b %-d at %-I:%M %p} PST "
        f"(requested: {FUTURE})\n"
        "Subject: Complete subject\n\nBody:\n"
        f"{body}"
    )


@pytest.mark.parametrize("field", ["body", "text"])
def test_message_preview_supports_body_and_text_aliases(field: str) -> None:
    preview = action_tools.confirm_preview("schedule_send", {
        "channel": "MESSAGE",
        "to": "+1 555 123 4567",
        "when": "tomorrow at 9am",
        field: "The complete scheduled message.",
    })

    assert preview.startswith("Channel: Message\nTo: +1 555 123 4567\n")
    assert "Delivery time:" in preview
    assert "(requested: tomorrow at 9am)" in preview
    assert preview.endswith("\n\nMessage:\nThe complete scheduled message.")


def test_preview_matches_body_precedence_when_both_aliases_are_present() -> None:
    preview = action_tools.confirm_preview("schedule_send", {
        "channel": "message",
        "to": "+15551234567",
        "when": FUTURE,
        "body": "This is what execution queues.",
        "text": "This alias is ignored when body is present.",
    })

    assert "This is what execution queues." in preview
    assert "This alias is ignored" not in preview


def test_preview_names_missing_fields_instead_of_hiding_them() -> None:
    assert action_tools.confirm_preview("schedule_send", {}) == (
        "Channel: (no channel given)\n"
        "To: (no recipient given)\n"
        "Delivery time: (no delivery time given)\n\n"
        "Message:\n(empty)"
    )

    email = action_tools.confirm_preview("schedule_send", {
        "channel": "email", "to": "", "when": "", "body": ""
    })
    assert "To: (no recipient given)" in email
    assert "Delivery time: (no delivery time given)" in email
    assert "Subject: (no subject)" in email
    assert email.endswith("(empty)")


def test_preview_keeps_the_complete_long_message() -> None:
    body = "opening\n" + ("detail " * 900) + "\nclosing"
    preview = action_tools.confirm_preview("schedule_send", {
        "channel": "message",
        "to": "+15551234567",
        "when": FUTURE,
        "body": body,
    })

    assert preview.endswith(f"Message:\n{body}")
    assert preview.count(body) == 1


@pytest.mark.asyncio
async def test_single_recipient_still_queues_once_with_truthful_receipt(
        queued: list[dict]) -> None:
    result = await action_tools.schedule_send(
        channel="EMAIL",
        to="recipient@example.test",
        when=FUTURE,
        subject="Plans",
        body="See you there.",
    )

    assert result.startswith("Scheduled: email to recipient@example.test")
    assert len(queued) == 1
    assert queued[0]["channel"] == "email"
    assert queued[0]["recipient"] == "recipient@example.test"
    assert queued[0]["subject"] == "Plans"
    assert queued[0]["body"] == "See you there."


@pytest.mark.asyncio
async def test_text_alias_still_queues_one_message(queued: list[dict]) -> None:
    result = await action_tools.schedule_send(
        channel="message",
        to="+1 (555) 123-4567",
        when=FUTURE,
        text="Alias payload.",
    )

    assert result.startswith("Scheduled: text to +1 (555) 123-4567")
    assert len(queued) == 1
    assert queued[0]["recipient"] == "+1 (555) 123-4567"
    assert queued[0]["body"] == "Alias payload."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "to"),
    [
        ("email", "first@example.test, second@example.test"),
        ("message", "+15551230001; +15551230002"),
    ],
)
async def test_multiple_recipients_are_refused_without_partial_queue(
        channel: str, to: str, queued: list[dict],
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        action_tools,
        "_resolve_recipient",
        lambda *_args, **_kwargs: pytest.fail(
            "multi-recipient refusal must happen before contact resolution"
        ),
    )

    result = await action_tools.schedule_send(
        channel=channel,
        to=to,
        when=FUTURE,
        subject="For email",
        body="One complete payload.",
    )

    assert "NOT scheduled" in result
    assert "exactly one recipient" in result
    assert "separately" in result
    assert "nothing was queued" in result
    assert queued == []
