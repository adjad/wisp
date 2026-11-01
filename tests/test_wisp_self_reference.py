"""The Ask capability sheet for questions about Wisp itself
(model_only.wisp_self_hint): injected only for those questions, built from
the live registry and wisp_about, and checked here so it cannot drift.
Toolbench 2026-10-31: the `wisp` category passed 17%."""
from __future__ import annotations

import os
import re
import tempfile

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-self-ref-"))

import pytest  # noqa: E402

import service.tools  # noqa: E402,F401
from service import model_only as mo  # noqa: E402
from service import wisp_about  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

SELF = ["what tools are available to you", "what can u do", "so what exactly are you",
        "is my data private", "what model are you running", "when did my email last sync",
        "what skills do i have", "is my mcp server working", "can i talk to you",
        "how do i use wisp", "how do i connect my gmail", "from how far back can you check emails",
        "can you send texts?", "are you using the cloud?"]
NOT_SELF = ["what do u know about me", "what's on my calendar tomorrow", "text mom that I'm late",
            "Wisp, what's on my calendar?", "how far back is the dentist appointment",
            "what model is the dentist using", "remind me to call the vet"]


@pytest.mark.parametrize("prompt", SELF)
def test_questions_about_wisp_get_the_sheet(prompt):
    assert mo.wisp_self_question(prompt)


@pytest.mark.parametrize("prompt", NOT_SELF)
def test_other_questions_do_not(prompt):
    assert not mo.wisp_self_question(prompt)


def test_every_report_tool_the_sheet_names_is_registered_with_those_topics():
    hint = mo.wisp_self_hint()
    for name, _ in mo.WISP_SELF_TOOLS:
        assert name in REGISTRY
        assert name in hint
    topics = set(REGISTRY["wisp_capabilities"].parameters["properties"]["topic"]["enum"])
    assert set(wisp_about.TOPICS) == topics
    for topic in topics - {"overview"}:
        assert f"'{topic}'" in mo.WISP_SELF_TOOLS[0][1]
    status = set(REGISTRY["wisp_status"].parameters["properties"]["topic"]["enum"])
    for topic in status:
        assert f"'{topic}'" in mo.WISP_SELF_TOOLS[1][1]


def test_unregistered_report_tools_are_left_out(monkeypatch):
    import service.tools.registry as registry
    monkeypatch.setattr(registry, "routable_tool_names", lambda: {"wisp_capabilities"})
    hint = mo.wisp_self_hint()
    assert "wisp_capabilities:" in hint and "wisp_mcp:" not in hint and "wisp_status:" not in hint


def test_identity_line_is_wisp_abouts_own_live_line():
    intro = wisp_about.identity().split("\n\n", 1)[0].replace("**", "")
    assert "- " + intro in mo.wisp_self_hint()


def test_the_cloud_state_reaches_the_sheet(monkeypatch):
    monkeypatch.setattr(wisp_about, "cloud_detail", lambda: {
        "mode": "everything", "host": "api.example.com", "pending": False, "super_model": False,
        "roles": [], "known": True, "cloud_host": "api.example.com"})
    assert "sent to api.example.com" in mo.wisp_self_hint()


def test_shortcuts_and_google_steps_match_wisp_abouts_how_to():
    hint = mo.wisp_self_hint()
    howto = " ".join(step for _, step in wisp_about._HOWTO)
    for key in re.findall(r"⌥[⇧]?Space", hint):
        assert f"**{key}**" in howto
    assert "Disconnect & erase" in howto and "Disconnect & erase" in hint


def test_self_question_leads_with_wisps_own_reports(monkeypatch):
    from service import main
    monkeypatch.setattr(main, "_model_led_preload", lambda prompt: ["get_upcoming", "web_search"])
    preload, _ = main._ask_tool_rank("what model are you running")
    assert preload[:4] == [n for n, _ in mo.WISP_SELF_TOOLS]
    preload, _ = main._ask_tool_rank("what's on my calendar tomorrow?")
    assert "wisp_status" not in preload


def test_a_self_question_is_not_forced_tool_free():
    # It used to be ask_direct (no tools), so wisp_capabilities could not run.
    import inspect
    from service import main
    from scripts.toolbench import runner
    for module in (main, runner):
        source = inspect.getsource(module)
        assert "or capability_question(prompt))" not in source
