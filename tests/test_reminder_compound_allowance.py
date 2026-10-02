"""Keep the reminder-compound guard, but let clear two-part requests through.

The guard (service/router/router.py, strict_reminder) fails closed on any request
whose parts it cannot each identify, so no unexplained text is ever turned into an
effect. It was too narrow in one direction: it recognized only reminder, device,
calendar-read and complete-send clauses, so an ordinary effect-free READ beside a
reminder ("summarize my inbox, and remind me to call Dan tomorrow") and the
reported "...and tell Mom about it too" follow-on were blocked too.

Clear shapes are served; everything else still asks first.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-compound-"))

import service.tools  # noqa: E402,F401
from service.router.router import route  # noqa: E402


def decide(text):
    return asyncio.run(route(text))


def blocked(decision):
    return "clarify before effects" in decision.reason


def offered(decision):
    return set(decision.tool_subset or ())


@pytest.mark.parametrize("text,reads", [
    ("summarize my inbox, and remind me to call Dan tomorrow", {"summarize_emails"}),
    ("who do I know named Sarah, and remind me to call her tonight", {"lookup_contact"}),
    ("remind me to call the dentist tonight, and check my email", {"summarize_emails"}),
])
def test_a_clear_read_beside_a_reminder_is_served(text, reads):
    decision = decide(text)
    assert not blocked(decision), decision.reason
    assert "add_reminder" in offered(decision)
    assert reads <= offered(decision)
    # An effect-free read arms no send or delete tool.
    assert not offered(decision) & {"send_email", "send_message", "reply_to_email", "trash_file",
                                    "delete_path", "archive_email"}
    assert len(decision.required_tool_groups) == 2   # both halves must run


def test_the_reported_notify_follow_on_is_served_and_asks_for_the_channel():
    # "tell" names no channel: the router asks which one rather than guessing, and
    # still offers the contact lookup and both send paths for after the answer.
    decision = decide("create a reminder to finish the report by tonight and tell mom about it too")
    assert not blocked(decision), decision.reason
    assert {"add_reminder"} in [set(g) for g in decision.required_tool_groups]
    assert {"add_reminder", "lookup_contact"} <= offered(decision)
    assert offered(decision) & {"send_message", "send_email"}
    assert decision.clarify_channel is True


def test_a_named_channel_is_not_asked_about():
    decision = decide("create a reminder to finish the report by tonight and text mom about it")
    assert not blocked(decision)
    assert {"send_message"} in [set(g) for g in decision.required_tool_groups]
    assert "send_email" not in offered(decision)
    assert decision.clarify_channel is False


@pytest.mark.parametrize("text", [
    "remind me to call mom and mute my volume",                    # an action the guard does not know
    "remind me to call mom, and do something with my files",       # unexplained text
    "remind me to call mom and send her my passwords",             # a send with no resolvable body
    "check my email and delete the old ones, and remind me to call Dan",   # a read that also writes
    "remind me to call Dan tomorrow and remind me to call Sam tomorrow",   # two obligations, one tool
    "what's on my calendar tomorrow, and remind me to bring my laptop",    # a reminder with no time
])
def test_everything_the_guard_cannot_identify_still_asks_first(text):
    decision = decide(text)
    assert blocked(decision), (text, decision.reason)
    assert decision.tool_subset == [] and decision.forbidden_tools, "no effect tool may be offered"


def test_a_reminder_that_mentions_replying_does_not_arm_a_reply():
    # "reply to Dan" is the reminder's text, not a request to send anything.
    decision = decide("summarize my inbox, and remind me to reply to Dan tomorrow")
    assert not blocked(decision)
    assert not offered(decision) & {"reply_to_email", "send_email", "draft_email"}
