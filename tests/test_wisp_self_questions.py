"""A new user's first questions about Wisp itself.

"What can you do?", "Who are you?", "Is my data private?" and "How do I use
Wisp?" are routed router-direct to wisp_capabilities(topic=...), whose answer is
written in service/wisp_about.py: no model runs (the tool is presynthesized), so
the answer is right even while the model is still loading on a first launch.

    .venv/bin/python -m pytest tests/test_wisp_self_questions.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.tools  # noqa: E402,F401 - registers every tool
from service import wisp_about  # noqa: E402
from service.router.router import route  # noqa: E402
from service.tools.misc_t1 import wisp_capabilities  # noqa: E402

SELF_QUESTIONS = {
    "overview": ["What can you do?", "what can you do", "hey, what can Wisp do?",
                 "What else can you help me with?", "what are you good at"],
    "identity": ["Who are you?", "who are you", "What is Wisp?", "tell me about yourself",
                 "What's your name?", "are you an AI?"],
    "privacy": ["Is my data private?", "is my data safe", "Where does my data go?",
                "Do you send my data to the cloud?", "is this private", "privacy?",
                "does anything leave my Mac?"],
    "howto": ["How do I use Wisp?", "how do I use you", "How do I connect Google?",
              "how does wisp work", "help", "How do I get started?", "how do I quit wisp"],
}

# Requests with work in them, or about something other than Wisp, stay with
# their own routes.
NOT_SELF_QUESTIONS = [
    "who are you texting", "what can you do about my overdue reminders",
    "how do I make pancakes", "how do I use Excel pivot tables",
    "is my data plan running out", "help me write an email to Sam",
    "can you send text messages and create reminders?", "what do you know about me",
    "what's on my calendar today?", "privacy policy for my website",
]


def routed(prompt: str):
    return asyncio.run(route(prompt))


@pytest.mark.parametrize("topic,prompt", [(t, p) for t, ps in SELF_QUESTIONS.items() for p in ps])
def test_self_question_goes_router_direct_with_its_topic(topic, prompt):
    d = routed(prompt)
    assert d.direct_calls == [("wisp_capabilities", {"query": prompt.strip(), "topic": topic})] \
        or (d.direct_calls and d.direct_calls[0][0] == "wisp_capabilities"
            and d.direct_calls[0][1].get("topic") == topic), (prompt, d.direct_calls, d.reason)
    assert d.tool_subset == ["wisp_capabilities"]
    assert not {"send_message", "send_email", "add_reminder", "run_shell"} & set(d.tool_subset)


@pytest.mark.parametrize("prompt", NOT_SELF_QUESTIONS)
def test_requests_are_not_claimed_as_self_questions(prompt):
    d = routed(prompt)
    topics = [args.get("topic") for name, args in (d.direct_calls or []) if name == "wisp_capabilities"]
    assert not any(topics), (prompt, d.direct_calls, d.reason)


def test_specific_capability_question_keeps_the_yes_no_report():
    d = routed("Can you send text messages, create reminders, and read browser history?")
    assert d.direct_calls[0][0] == "wisp_capabilities"
    assert "topic" not in d.direct_calls[0][1]
    report = wisp_capabilities("email scheduling")
    assert "Yes — Schedule a new email or text for later" in report


STALE = ("hackathon", "judge", "demo mode", "founder", "aria", "tool count", "usable tools",
         "registry", "compatibility registrations", "No —")


@pytest.mark.parametrize("topic", wisp_about.TOPICS)
def test_answers_are_current_and_plain(topic):
    text = wisp_capabilities("", topic)
    lowered = text.lower()
    for word in STALE:
        assert word.lower() not in lowered, (topic, word)
    assert "_" not in text  # no internal tool names


def test_overview_names_the_real_surfaces():
    text = wisp_capabilities("what can you do", "overview")
    for needle in ("Ask", "Brief and Day", "⌥ Space", "/about", "Settings → Google",
                   "wait for your OK", "**Right now**"):
        assert needle in text, needle
    # Three try-it prompts, fitted to what is connected.
    assert text.count('\n- "') == 3


def test_general_inventory_without_topic_is_the_overview():
    assert wisp_capabilities("What can you do?") == wisp_about.overview()


def test_overview_only_lists_usable_features(monkeypatch):
    monkeypatch.setattr(wisp_about, "_usable_tools", lambda: {"get_upcoming"})
    text = wisp_about.overview()
    assert "calendar" in text and "send emails" not in text and "weather" not in text


def test_overview_reports_live_source_state(monkeypatch):
    rows = [("calendar", "Calendar", "ready", ""), ("reminders", "Reminders", "ready", ""),
            ("email", "Email", "unavailable", "Open Mail once so Wisp can read it."),
            ("messages", "Messages", "syncing", ""), ("notes", "Notes", "disabled", "")]
    monkeypatch.setattr(wisp_about, "_sources",
                        lambda: [{"id": i, "label": l, "state": st, "reason": r} for i, l, st, r in rows])
    text = wisp_about.overview()
    assert "Connected: Calendar, Reminders." in text
    assert "Still reading for the first time: Messages." in text
    assert "Not connected: Email. Open Mail once so Wisp can read it." in text
    assert "Not connected: Notes." in text
    # Try-its follow what is connected: no mail prompt while Mail can't be read.
    assert "What's on my calendar tomorrow?" in text and "Any emails I need to reply to?" not in text


def test_overview_says_when_google_is_connected(monkeypatch):
    monkeypatch.setattr(wisp_about, "_google_connected", lambda: True)
    assert "your account is connected" in wisp_about.overview()


def test_privacy_is_local_by_default(monkeypatch):
    monkeypatch.setattr(wisp_about, "_cloud_state", lambda: ("", ""))
    text = wisp_about.privacy()
    assert text.startswith("**Your data stays on this Mac.**")
    assert "No cloud AI is used unless you turn one on" in text
    assert "web searches" in text  # what does go online is said, not hidden
    assert "without showing it to you first" in text


def test_privacy_never_claims_local_when_a_cloud_model_runs_everything(monkeypatch):
    monkeypatch.setattr(wisp_about, "_cloud_state", lambda: ("everything", "api.example.com"))
    text = wisp_about.privacy()
    assert "stays on this Mac" not in text.split("\n")[0]
    assert "api.example.com" in text and "turn the cloud model off" in text


def test_privacy_mentions_an_optional_cloud_model(monkeypatch):
    monkeypatch.setattr(wisp_about, "_cloud_state", lambda: ("super", "your cloud provider"))
    assert "You've turned on a cloud model" in wisp_about.privacy()


def test_identity_uses_the_name_the_user_gave(monkeypatch):
    monkeypatch.setattr(wisp_about, "_user_name", lambda: "Sam")
    assert "you're Sam" in wisp_about.identity()
    monkeypatch.setattr(wisp_about, "_user_name", lambda: "")
    assert "I don't know your name yet" in wisp_about.identity()


def test_specific_howto_answers_only_that_step():
    text = wisp_about.howto("How do I connect Google?")
    assert "Settings → Google" in text and "Research" not in text
    full = wisp_about.howto("How do I use Wisp?")
    assert full.startswith("**Getting started with Wisp**") and "⌥Space" in full
