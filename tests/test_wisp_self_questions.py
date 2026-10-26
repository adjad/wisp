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
                "Do you send my data to the cloud?", "is wisp private", "privacy?",
                "does anything leave my Mac?", "is this app safe?"],
    "howto": ["How do I use Wisp?", "how do I use you", "How do I connect Google?",
              "how does wisp work", "help", "How do I get started?", "how do I quit wisp",
              "how do I turn off wisp", "how do I open the brief tab",
              "how do I change your settings", "How do I change the tone in Wisp?",
              "how do I connect my google account in wisp", "how do I turn off notifications in Wisp?",
              "how do I erase my profile in wisp",
              # Help, feedback and signing in are about Wisp (re-review f778a282).
              "how can I get help with wisp", "how do I give feedback on wisp",
              "how do I log in to wisp", "how do I sign in to Wisp?", "how can I update wisp",
              "how do I send feedback in wisp"],
}

# Requests with work in them, or about something other than Wisp, stay with
# their own routes.
NOT_SELF_QUESTIONS = [
    "who are you texting", "what can you do about my overdue reminders",
    "how do I make pancakes", "how do I use Excel pivot tables",
    "is my data plan running out", "help me write an email to Sam",
    "can you send text messages and create reminders?", "what do you know about me",
    "what's on my calendar today?", "privacy policy for my website",
    # Follow-ups and requests that only address Wisp by name (audit 32649065).
    "is it private?", "is this private", "is this safe?", "is it secure", "how private is this",
    "how do I reply to Sam, Wisp?", "Wisp, how do I reply to Sam?", "how do I free up memory",
    "how do I change my iphone settings", "how do I send this to Sam with wisp",
    # A task phrased "in Wisp" is still a task (re-review b7623e76).
    "how do I write a cover letter in wisp", "how do I plan my week in Wisp?",
    "how can I summarize this pdf in wisp", "how do I email Sam in wisp",
]


def _patch_cloud(monkeypatch, mode="", host="", *, pending=False, super_model=None, roles=(), known=True,
                 cloud_host=""):
    """Stand in for wisp_about.cloud_detail(): one mode for every answer."""
    from service import wisp_about as about
    if super_model is None:
        super_model = mode == "super" and not roles
    monkeypatch.setattr(about, "cloud_detail", lambda: {
        "mode": mode, "host": host, "pending": pending, "super_model": super_model,
        "roles": list(roles), "known": known, "cloud_host": cloud_host or host})


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
    _patch_cloud(monkeypatch, "", "")
    text = wisp_about.privacy()
    assert text.startswith("**Your data stays on this Mac.**")
    assert "No cloud AI is used unless you turn one on" in text
    assert "web searches" in text  # what does go online is said, not hidden
    assert "Before anything is sent, I show it to you and wait for your OK" in text


def test_privacy_never_claims_local_when_a_cloud_model_runs_everything(monkeypatch):
    _patch_cloud(monkeypatch, "everything", "api.example.com")
    text = wisp_about.privacy()
    assert "stays on this Mac" not in text.split("\n")[0]
    assert "api.example.com" in text and "turn the cloud model off" in text


def test_privacy_under_an_optional_cloud_model_says_exactly_what_goes(monkeypatch):
    # Super Model: only the question itself (service/main.py's super_model_cloud path).
    _patch_cloud(monkeypatch, "super", "openrouter.ai", super_model=True)
    text = wisp_about.privacy()
    assert not text.startswith("**Your data stays on this Mac.**")
    assert "kept on this Mac, not on a server" not in text
    assert "stored on this Mac" in text and "openrouter.ai" in text
    assert "only the question itself" in text
    assert "Getting to know you is never sent" in text
    # A role bound to the cloud: the direct-chat branch sends the conversation,
    # related memory and routine patterns, never About you (profile.may_send_to).
    _patch_cloud(monkeypatch, "super", "openrouter.ai", roles=("reasoning", "coding"))
    text = wisp_about.privacy()
    assert "hard reasoning questions and code questions" in text
    assert "recent conversation" in text and "remember" in text and "routines" in text
    assert "instructions of any skill in use" in text  # main.py's selected and always-on skill blocks
    assert "only the question itself" not in text
    assert "Getting to know you is never sent" in text


def test_pending_everything_matches_the_models_report(monkeypatch):
    _patch_cloud(monkeypatch, "everything", "gpu.example.net", pending=True)
    for text in (wisp_about.privacy(), wisp_about.identity(), wisp_about.overview()):
        assert "after Wisp restarts" in text and "this Mac answers" in text, text[:160]
        assert "gpu.example.net" in text
        assert "Right now a cloud model is on" not in text


def test_an_unreadable_cloud_setting_never_reads_as_local(monkeypatch):
    import service.config as config
    import service.inference.remote_policy as remote_policy

    def broken():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(config, "cloud_everything_enabled", lambda: False)
    monkeypatch.setattr(config, "cloud_provider_settings", broken)
    monkeypatch.setattr(config, "cloud_super_model_enabled", lambda: True)
    # The provider settings can't be read, but a flag says cloud: cloud, generic host.
    assert wisp_about.cloud_state() == ("super", "your cloud provider")
    monkeypatch.setattr(config, "cloud_super_model_enabled", broken)
    detail = wisp_about.cloud_detail()
    assert detail["mode"] == "super" and detail["known"] is False
    monkeypatch.setattr(config, "cloud_everything_enabled", broken)
    assert wisp_about.cloud_state() == ("everything", "your cloud provider")
    assert "runs right here on this Mac" not in wisp_about.overview()
    assert not wisp_about.privacy().startswith("**Your data stays on this Mac.**")
    for text in (wisp_about.overview(), wisp_about.identity(), wisp_about.privacy()):
        assert "couldn't read the cloud settings" in text
        assert "Right now a cloud model is on" not in text, "unknown isn't claimed either way"
    monkeypatch.setattr(remote_policy, "remote_only_profile", broken)
    monkeypatch.setattr(config, "cloud_everything_enabled", lambda: False)
    assert wisp_about.cloud_state()[0] == "everything", "an unreadable remote-only check isn't local"


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


# Audit 32649065: "runs on this Mac" and "never changes your calendar" are said
# only when they are true.
LOCAL_CLAIMS = ("runs right here on this Mac", "without sending them to a server")


@pytest.mark.parametrize("host", ["api.example.com", "your cloud provider"])
def test_overview_and_identity_never_claim_local_when_everything_runs_in_the_cloud(monkeypatch, host):
    _patch_cloud(monkeypatch, "everything", host)
    for text in (wisp_about.overview(), wisp_about.identity()):
        for claim in LOCAL_CLAIMS:
            assert claim not in text, (claim, text[:200])
        assert "cloud model" in text and host in text


def test_overview_and_identity_say_local_when_it_is(monkeypatch):
    _patch_cloud(monkeypatch, "", "")
    assert wisp_about.overview().startswith("**Hi, I'm Wisp.** My AI runs right here on this Mac.")
    assert "without sending them to a server" in wisp_about.identity()


def test_overview_and_identity_name_an_optional_cloud_model(monkeypatch):
    _patch_cloud(monkeypatch, "super", "api.example.com")
    for text in (wisp_about.overview(), wisp_about.identity()):
        assert "You've also turned on a cloud model" in text and "api.example.com" in text
        assert "without sending them to a server" not in text


def test_remote_only_build_reads_as_everything(monkeypatch):
    from types import SimpleNamespace
    import service.inference.remote_policy as remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile",
                        lambda: SimpleNamespace(base_url="https://gpu.example.net/v1"))
    assert wisp_about.cloud_state() == ("everything", "gpu.example.net")
    assert "runs right here on this Mac" not in wisp_about.overview()


def _access(monkeypatch, *, view_only=False, full=False, learning=True, held=None, attention=None):
    """Set View Only / Full access, behavior learning, the routine hold
    (held=None: no runtime to ask) and Caught for you's settings file
    (attention=None: no file, the shadow default; a str: the raw file)."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    import service.behavior_switch as behavior_switch
    import service.paths as paths
    from service.assistant import behavior_runtime
    from service.attention.live import settings_path
    from service.safety import policy
    monkeypatch.setattr(policy, "_READ_ONLY", view_only)
    monkeypatch.setattr(policy, "_FULL_ACCESS", full)
    monkeypatch.setattr(behavior_switch, "enabled", lambda: learning)
    # A fresh Wisp home of its own for the settings file: never the real one.
    home = Path(tempfile.mkdtemp(prefix="wisp-attention-"))
    monkeypatch.setattr(paths, "MOE_DIR", home)
    if attention is not None:
        path = settings_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(attention, encoding="utf-8")
    runtime = None if held is None else SimpleNamespace(auto_actions_held=lambda store, now: held)
    monkeypatch.setattr(behavior_runtime, "_runtime", runtime)


ROUTINE_ON_MY_OWN = "Tentative hold"
CALENDAR_PROMISE = "or your calendar changes, I show it to you"


def test_view_only_promises_nothing_is_added(monkeypatch):
    _access(monkeypatch, view_only=True, held=False)
    for auto_add in (False, True):
        monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: auto_add)
        for text in (wisp_about.privacy(), wisp_about.overview()):
            assert "In View Only, nothing is added to your calendar or reminders without your OK" in text
            assert ROUTINE_ON_MY_OWN not in text and "auto-add is on" not in text


def test_full_access_during_the_routine_hold(monkeypatch):
    _access(monkeypatch, full=True, held=True)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    for text in (wisp_about.privacy(), wisp_about.overview()):
        assert ("For your first few days I only suggest reminders from your routine. After that I may "
                "add a reminder or a Tentative hold on my own and tell you, with Undo.") in text
        assert CALENDAR_PROMISE not in text and "change your calendar" not in text


def test_full_access_after_the_routine_hold(monkeypatch):
    _access(monkeypatch, full=True, held=False)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    for text in (wisp_about.privacy(), wisp_about.overview()):
        assert ("I may add a reminder or a Tentative hold from your routine on my own, and tell you, "
                "with Undo.") in text
        assert CALENDAR_PROMISE not in text


def test_auto_add_combines_with_routine_actions(monkeypatch):
    _access(monkeypatch, full=True, held=False)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: True)
    text = wisp_about.privacy()
    assert ("On my own I may add a time-sensitive event or deadline to your Calendar or Reminders "
            "(auto-add is on); and a reminder or a Tentative hold from your routine, and tell you "
            "each time.") in text
    assert CALENDAR_PROMISE not in text


def test_cant_tell_says_the_cautious_version(monkeypatch):
    # No runtime to ask about the hold.
    _access(monkeypatch, full=True, held=None)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    text = wisp_about.privacy()
    assert "Depending on your settings, I may add a reminder or a Tentative hold" in text
    assert CALENDAR_PROMISE not in text and "without your OK" not in text
    # The policy itself can't be read.
    import service.safety as safety

    def broken(*args, **kwargs):
        raise RuntimeError("policy unavailable")
    monkeypatch.setattr(safety, "decide", broken)
    _access(monkeypatch, full=True, held=False)
    text = wisp_about.privacy()
    assert "Depending on your settings" in text and CALENDAR_PROMISE not in text
    _access(monkeypatch, full=True, learning=False)
    text = wisp_about.privacy()
    assert CALENDAR_PROMISE not in text and "In View Only" not in text


def test_default_access_acts_like_full_access_for_routine_adds(monkeypatch):
    # Neither View Only nor Full access: assistant_write ("Wisp's commitment
    # store") is still ALLOW, so routine actions add on their own after the hold.
    from service.safety import Tier, decide
    _access(monkeypatch, held=False)
    assert decide("assistant_write", {"title": "", "kind": "reminder"}, tool="add_reminder").tier is Tier.ALLOW
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    assert "from your routine on my own" in wisp_about.privacy()


def test_routine_actions_the_policy_refuses_are_only_suggested(monkeypatch):
    # When decide() refuses assistant_write (anything short of ALLOW),
    # behavior_runtime.policy_block refuses every automatic add.
    import service.safety as safety
    from service.safety import Decision, Tier
    _access(monkeypatch, held=False)
    monkeypatch.setattr(safety, "decide", lambda *a, **k: Decision(Tier.DENY, "remote profile"))
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    text = wisp_about.privacy()
    assert "Right now I only suggest reminders from your routine" in text
    assert ROUTINE_ON_MY_OWN not in text


def test_with_learning_off_and_no_auto_add_calendar_changes_wait(monkeypatch):
    _access(monkeypatch, full=True, learning=False)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    assert CALENDAR_PROMISE in wisp_about.overview()
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: True)
    for text in (wisp_about.privacy(), wisp_about.overview()):
        assert "change your calendar" not in text and "calendar changes" not in text
        assert "auto-add is on" in text and "Undo" in text


# The models report (wisp_status) and the self-answers read one cloud_state(),
# so "runs on this Mac" can't be claimed in one place and denied in another.
def _cloud_config(monkeypatch, *, everything=False, super_model=False, roles=(), remote_url=""):
    import service.config as config
    import service.inference.remote_policy as remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(config, "cloud_everything_enabled", lambda: everything)
    monkeypatch.setattr(config, "cloud_super_model_enabled", lambda: super_model)
    monkeypatch.setattr(config, "cloud_provider_settings",
                        lambda: {"enabled": True, "base_url": "https://openrouter.ai", "roles": list(roles)})
    real = config.models_config
    monkeypatch.setattr(config, "models_config", lambda: {
        **real(), "inference": {**(real().get("inference") or {}),
                                "remote_only": {"base_url": remote_url} if remote_url else {}}})


def _models_report(monkeypatch) -> str:
    from service.inference.omlx_client import OMLXClient
    from service.tools.registry import REGISTRY

    async def loaded(self):  # never contact the inference server
        return {"local-model"}
    monkeypatch.setattr(OMLXClient, "loaded_models", loaded)
    return asyncio.run(REGISTRY["wisp_status"].func("models"))


def test_cloud_state_reads_each_mode(monkeypatch):
    _cloud_config(monkeypatch)
    assert wisp_about.cloud_state() == ("", "")
    _cloud_config(monkeypatch, super_model=True)
    assert wisp_about.cloud_state() == ("super", "openrouter.ai")
    _cloud_config(monkeypatch, roles=("reasoning",))
    assert wisp_about.cloud_state() == ("super", "openrouter.ai"), "a role bound to the cloud counts"
    _cloud_config(monkeypatch, everything=True, remote_url="https://gpu.example.net/v1")
    assert wisp_about.cloud_state() == ("everything", "gpu.example.net")


def test_models_report_says_local_only_when_it_is(monkeypatch):
    _cloud_config(monkeypatch)
    text = _models_report(monkeypatch)
    assert "Inference runs on this Mac: no question or data is sent to a cloud model." in text
    assert "no request leaves the machine" not in text


def test_models_report_names_an_optional_cloud_model(monkeypatch):
    _cloud_config(monkeypatch, super_model=True)
    text = _models_report(monkeypatch)
    assert "You've also turned on a cloud model" in text and "openrouter.ai" in text
    assert "no question or data is sent" not in text and "no request leaves" not in text


def test_models_report_under_cloud_everything(monkeypatch):
    _cloud_config(monkeypatch, everything=True, remote_url="https://gpu.example.net/v1")
    text = _models_report(monkeypatch)
    assert 'Cloud "Everything" is turned on' in text and "gpu.example.net" in text
    assert "no question or data is sent" not in text and "no request leaves" not in text


def test_models_report_on_a_remote_only_build(monkeypatch):
    from types import SimpleNamespace
    import service.inference.remote_policy as remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: SimpleNamespace(
        model_id="example-model", base_url="https://gpu.example.net/v1"))
    text = _models_report(monkeypatch)
    assert "gpu.example.net" in text and "not on this Mac" in text
    assert "runs on this Mac" not in text and "no request leaves" not in text
    assert wisp_about.cloud_state() == ("everything", "gpu.example.net")


def test_pending_everything_names_roles_still_bound_to_the_cloud(monkeypatch):
    # Turning Everything on doesn't unbind a role: until the restart, hard
    # reasoning questions still go to OpenRouter, and the answers say so.
    import service.config as config
    import service.inference.remote_policy as remote_policy
    from service.memory import profile
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(config, "cloud_everything_enabled", lambda: True)
    monkeypatch.setattr(config, "cloud_super_model_enabled", lambda: False)
    real = config.models_config()
    monkeypatch.setattr(config, "models_config", lambda: {**real, "inference": {
        "remote_only": {"base_url": "https://gpu.example.net"},
        "endpoints": {"cloud": {"base_url": "https://openrouter.ai"}},
        "bindings": {"reasoning": {"endpoint": "cloud", "model_id": "big"}}}})
    detail = wisp_about.cloud_detail()
    assert detail["pending"] and detail["roles"] == ["reasoning"] and detail["cloud_host"] == "openrouter.ai"
    privacy = wisp_about.privacy()
    for text in (privacy, wisp_about.identity(), wisp_about.overview(), _models_report(monkeypatch)):
        assert "except hard reasoning questions, which still go to openrouter.ai" in text, text[:200]
    assert "stays on this Mac" in privacy and "gpu.example.net" in privacy
    # And About you doesn't go to that role in the meantime.
    assert profile.may_send_to(managed=False) is False and profile.sent_to() == ""


def test_signing_in_and_feedback_have_their_own_steps():
    signin = wisp_about.howto("how do I log in to wisp")
    assert "no account of its own" in signin and "Settings → Google" in signin
    assert "/report" in wisp_about.howto("how do I give feedback on wisp")


@pytest.mark.parametrize("prompt", ["how do I sign out of wisp", "how do I log out of Wisp?"])
def test_signing_out_has_its_own_step(prompt):
    assert routed(prompt).direct_calls[0][1].get("topic") == "howto"
    text = wisp_about.howto(prompt)
    assert "no account to sign out of" in text and "Disconnect & erase" in text
    assert "Getting started" not in text


@pytest.mark.parametrize("prompt", ["how do I update wisp", "how can I update Wisp?"])
def test_updating_has_its_own_step(prompt):
    assert routed(prompt).direct_calls[0][1].get("topic") == "howto"
    text = wisp_about.howto(prompt)
    assert "doesn't update itself" in text and "Applications" in text


def test_updating_a_feature_is_not_updating_wisp():
    # No specific step matches, so it gets the whole guide, not the update step alone.
    assert not wisp_about.howto("how do I update the model in wisp").startswith("- **Updating Wisp**")


# Caught for you (attention_runner): live adds reminders, capture_events adds
# Calendar events too, both through the same assistant_write gate.
LIVE = '{"mode": "live"}'
LIVE_EVENTS = '{"mode": "live", "capture_events": true}'


def test_caught_for_you_live_adds_reminders(monkeypatch):
    _access(monkeypatch, full=True, learning=False, attention=LIVE)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    for text in (wisp_about.privacy(), wisp_about.overview()):
        assert ("From what Caught for you finds in your mail and messages, I may add reminders on my "
                "own and tell you.") in text
        assert CALENDAR_PROMISE not in text and "Calendar events" not in text


def test_caught_for_you_live_with_events_adds_calendar_events(monkeypatch):
    _access(monkeypatch, full=True, learning=False, attention=LIVE_EVENTS)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    assert "I may add reminders and Calendar events on my own" in wisp_about.privacy()
    # Combined with the routine into one sentence.
    _access(monkeypatch, full=True, held=False, attention=LIVE_EVENTS)
    text = wisp_about.privacy()
    assert ("On my own I may add a reminder or a Tentative hold from your routine; and reminders and "
            "Calendar events from what Caught for you finds in your mail and messages, and tell you "
            "each time.") in text


def test_caught_for_you_settings_unreadable_says_the_cautious_version(monkeypatch):
    _access(monkeypatch, full=True, learning=False, attention="{not json")
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    text = wisp_about.privacy()
    assert "Depending on your Caught for you settings, I may also add reminders or Calendar events" in text
    assert CALENDAR_PROMISE not in text
    _access(monkeypatch, full=True, learning=False, attention='{"mode": "sometimes"}')
    assert "Depending on your Caught for you settings" in wisp_about.privacy()


def test_caught_for_you_live_under_view_only_adds_nothing(monkeypatch):
    _access(monkeypatch, view_only=True, held=False, attention=LIVE_EVENTS)
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    text = wisp_about.privacy()
    assert "In View Only, nothing is added to your calendar or reminders without your OK" in text
    assert "Caught for you" not in text


def test_caught_for_you_live_but_the_gate_refuses_adds_nothing(monkeypatch):
    import service.safety as safety
    from service.safety import Decision, Tier
    _access(monkeypatch, learning=False, attention=LIVE_EVENTS)
    monkeypatch.setattr(safety, "decide", lambda *a, **k: Decision(Tier.DENY, "remote profile"))
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    text = wisp_about.privacy()
    assert "Caught for you" not in text and CALENDAR_PROMISE in text


def test_everything_off_keeps_the_calendar_promise(monkeypatch):
    # Preview (shadow) Caught for you, learning off, auto-add off, Full access.
    _access(monkeypatch, full=True, learning=False, attention='{"mode": "shadow", "capture_events": true}')
    monkeypatch.setattr(wisp_about, "_auto_add_on", lambda: False)
    for text in (wisp_about.privacy(), wisp_about.overview()):
        assert CALENDAR_PROMISE in text
        assert "on my own" not in text
