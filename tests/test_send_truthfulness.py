"""A send's result says only what the evidence shows (invented names and numbers)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from service import main
from service.agent import loop
from service.tools import action_tools, imessage_tools
from service.tools.registry import _OutboundUnknownResult, classify_tool_outcome

ROOT = Path(__file__).resolve().parents[1]
PHONE = "+15550104321"


@pytest.fixture
def contacts(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"dana fixture": [PHONE]})
    monkeypatch.setattr(imessage_tools, "_contacts", {"5550104321": "Dana Fixture"})
    monkeypatch.setattr(imessage_tools, "_lines", "")
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: ["me.fixture@example.com"])


def _bridge(monkeypatch, **extra):
    calls = []

    async def request(op, payload, **kwargs):
        calls.append((op, payload))
        return {"ok": True, "status": "succeeded", **extra}
    monkeypatch.setattr(action_tools, "app_request", request)
    return calls


async def test_unconfirmed_delivery_is_not_called_sent(contacts, monkeypatch):
    for extra in ({"delivery": "unknown", "delivery_service": ""}, {"delivery": "pending"}, {}):
        _bridge(monkeypatch, **extra)
        result = await action_tools.send_message(PHONE, "Running late")
        assert result.startswith("Handed to Messages for ")
        assert not result.lower().startswith("message sent")
        assert "isn't confirmed" in result and "not" not in result.lower().split("delivery")[0]
        outcome = classify_tool_outcome("send_message", result)
        assert outcome.status == "succeeded"  # queued: nothing is retried and nothing failed
        assert "NOT sent" not in result and "not delivered" not in result.lower()


async def test_evidence_still_decides_sent_and_delivered(contacts, monkeypatch):
    _bridge(monkeypatch, delivery="delivered", delivery_service="iMessage")
    assert (await action_tools.send_message(PHONE, "Hi")).endswith("Messages shows it as delivered.")
    _bridge(monkeypatch, delivery="sent", delivery_service="SMS")
    assert (await action_tools.send_message(PHONE, "Hi")).startswith("Message sent to ")


async def test_a_messages_error_with_no_way_to_read_the_store_is_unknown_and_not_resent(contacts, monkeypatch):
    calls = _bridge(monkeypatch, delivery="uncertain")
    result = await action_tools.send_message(PHONE, "Hi")
    assert type(result) is _OutboundUnknownResult and result.operation == "send_message"
    assert "can't tell" in result and not result.startswith("Message sent")
    assert len(calls) == 1
    assert classify_tool_outcome("send_message", result).status == "unknown"


def test_not_delivered_wording_is_untouched():
    assert action_tools.QUIET_NOT_DELIVERED == "Passed to Messages."
    assert action_tools.NOT_DELIVERED_TEXT == "Messages shows it as Not Delivered."


async def test_the_draft_card_does_not_call_a_handed_over_text_failed(contacts, monkeypatch):
    _bridge(monkeypatch, delivery="unknown")
    answer = await main.assistant_send_message_draft({"to": PHONE, "text": "Hi"})
    assert answer["ok"] is True and answer["result"].startswith("Handed to Messages for ")
    app = (ROOT / "app/Sources/WispApp/OverlayModel.swift").read_text(encoding="utf-8")
    chat = (ROOT / "app/Sources/WispApp/Chat/ChatStore.swift").read_text(encoding="utf-8")
    assert 'hasPrefix("Handed to Messages")' in app and 'hasPrefix("Handed to Messages")' in chat


@pytest.mark.parametrize("result, reason", [
    ("The user denied this action.", "you declined the approval"),
    ("BLOCKED by safety policy: (error: NOT sent — `me@example.com` is the USER'S OWN address, not a recipient.)",
     "that address is yours"),
    ("BLOCKED by safety policy: a phone number is not an email address; ask for the recipient's email",
     "a phone number, not an email address"),
    ("BLOCKED by safety policy: view-only mode", "view-only"),
    ("BLOCKED by safety policy: something internal", "wasn't allowed to go out"),
])
def test_a_declined_or_blocked_send_gives_one_plain_reason(result, reason):
    text = loop._denied_outbound_text(result)
    assert text.startswith("Okay — I didn’t send it: ") and reason in text
    for internal in ("safety policy", "BLOCKED", "send_message", "send_email", "confirmed_self_send", "lookup_contact"):
        assert internal not in text


def test_a_denial_with_no_known_reason_keeps_the_plain_line():
    assert loop._denied_outbound_text("") == "Okay — I didn’t send it."


def _function(source, name):
    start = source.index(f"static func {name}(")
    return source[start:source.index("\n    static func ", start + 1) if "\n    static func " in source[start + 1:] else None]


def test_the_app_resends_by_the_other_route_only_when_the_store_shows_nothing_queued():
    sender = (ROOT / "app/Sources/WispApp/OutboundSender.swift").read_text(encoding="utf-8")
    script = _function(sender, "messageScript")
    assert "try" not in script and "on error" not in script, "no in-script second route"
    body = sender[sender.index("static func sendMessage("):sender.index("private static func run(")]
    check = body.index("MessageDeliveryCheck.confirmQueued(")
    fallback = body.index("fallback: true")
    assert check < fallback
    assert re.search(r"case \.absent:\s*let backup = messageScript\(", body)
    assert re.search(r'case \.unreadable:[^}]*"delivery": "uncertain"[^}]*return', body, re.S)
    assert re.search(r"case \.found:[^}]*result = \(out: nil, error: nil\)", body, re.S)
    assert "code != -1743" in body


@pytest.mark.parametrize("said, expected", [
    ("7 a.m.", "7:00 AM"), ("7:30 p.m.", "7:30 PM"), ("at 6pm", "6:00 PM"),
    ("noon", "12:00 PM"), ("midnight", "12:00 AM"), ("7.30am", "7:30 AM"), ("18:45", "6:45 PM"),
])
def test_an_alarm_time_said_the_way_people_say_it_is_read(said, expected, monkeypatch):
    from service.tools import timers_alarms
    monkeypatch.setattr(timers_alarms, "_TIMERS", {})
    monkeypatch.setattr(timers_alarms, "_save", lambda: None)
    monkeypatch.setattr(timers_alarms, "_arm", lambda tid: None)
    result = timers_alarms._set_alarm(said)
    assert result.startswith(f"Alarm set for {expected} on "), result


@pytest.mark.parametrize("tool, text", [
    ("toggle_setting", "(I couldn't confirm that Bluetooth changed. Check Control Center in the menu bar.)"),
    ("toggle_setting", "(Low Power Mode needs an admin password, so it can't be set from here.)"),
    ("clipboard_write", "(could not write clipboard: broken pipe)"),
    ("screen_capture", "(Wisp needs Screen Recording access to capture the screen.)"),
    ("screen_capture", "Screenshot cancelled — nothing was saved."),
])
def test_a_refused_system_action_is_not_a_success(tool, text):
    assert classify_tool_outcome(tool, text).status == "failed"


@pytest.mark.parametrize("tool, text", [
    ("toggle_setting", "Bluetooth turned on."),
    ("clipboard_write", "copied 5 chars to the clipboard"),
    ("screen_capture", "Saved screenshot-1.png to /tmp/x (12KB)."),
])
def test_a_confirmed_system_action_still_succeeds(tool, text):
    assert classify_tool_outcome(tool, text).status == "succeeded"
