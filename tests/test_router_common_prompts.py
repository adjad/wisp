"""Release 0.5.0: the most common everyday prompts route to their one right tool.

Each row is prompt -> (forced tool or None, tools that must be offered, tools
that must NOT be offered). Routing only; nothing here executes a tool.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_scratch = tempfile.TemporaryDirectory()
os.environ["WISP_HOME"] = _scratch.name

import pytest  # noqa: E402

import service.tools  # noqa: E402,F401
from service import skills  # noqa: E402
from service.router.router import route  # noqa: E402
from service.workflows.workflow20 import personal_read  # noqa: E402

skills.load()

WEB = {"web_search", "web_fetch"}
SEND = {"send_email", "send_message"}

# prompt: (forced, must_offer, must_not_offer)
ROWS = {
    # 1 calendar add keeps the add tool
    "add a meeting with Sam tomorrow at 3pm": (None, {"add_calendar_event"}, set()),
    "add a meeting tomorrow at 3pm": (None, {"add_calendar_event"}, set()),
    # 2 moves route to the reschedule path (update_event is an honest unavailable limitation)
    "move my 2pm to 4pm": ("MOVE", set(), {"add_calendar_event", "add_reminder"}),
    "move my 2pm meeting to 4pm": ("MOVE", set(), {"add_calendar_event", "add_reminder"}),
    "push my 3pm to tomorrow": ("MOVE", set(), {"add_calendar_event", "add_reminder"}),
    "reschedule my dentist appointment to Friday": ("MOVE", set(), {"add_calendar_event"}),
    # 3 alarms, timers, relative reminders
    # Alarms stay reminders: the reminder clock grammar asks rather than guesses.
    "set an alarm for 7am": ("add_reminder", {"add_reminder"}, set()),
    "set a timer for 10 minutes": ("set_timer", {"set_timer"}, {"add_reminder"}),
    "start a 5 minute timer": ("set_timer", {"set_timer"}, {"add_reminder"}),
    "remind me in 20 minutes to call mom": ("add_reminder", {"add_reminder"}, set()),
    # 4 local clock
    "what's the date today": ("world_time", {"world_time"}, WEB),
    "what day is it": ("world_time", {"world_time"}, WEB),
    "what time is it": ("world_time", {"world_time"}, WEB),
    "what time is it in Tokyo": ("world_time", {"world_time"}, WEB),
    # 5 weather
    "how's the weather like in Paris": (None, {"get_weather"}, WEB),
    "what's the weather like in Paris": (None, {"get_weather"}, WEB),
    "weather tomorrow in Boston": (None, {"get_weather"}, WEB),
    "will it rain tomorrow": (None, {"get_weather"}, WEB),
    "will it rain tomorrow in Seattle": (None, {"get_weather"}, WEB),
    "what's the forecast": (None, {"get_weather"}, WEB),
    "should I bring an umbrella": (None, {"get_weather"}, WEB),
    "what should I wear today": (None, {"get_weather"}, WEB),
    "weather in London": (None, {"get_weather"}, WEB),
    # 6 brief
    "what's my day look like": (None, {"daily_brief"}, set()),
    "give me my morning brief": (None, {"daily_brief"}, set()),
    "brief me": (None, {"daily_brief"}, set()),
    # 7 compute
    "what's 15% of 240": ("calculate", {"calculate"}, WEB),
    "square root of 144": ("calculate", {"calculate"}, WEB),
    "3 plus 4 times 7": ("calculate", {"calculate"}, WEB),
    "5 miles to km": ("convert_units", {"convert_units"}, WEB),
    "convert 100 fahrenheit to celsius": ("convert_units", {"convert_units"}, WEB),
    "define serendipity": (None, {"wikipedia_summary"}, {"recall", "run_shell"}),
    # 9 email
    "summarize my unread mail": (None, {"summarize_emails"}, {"write_document", "create_note"}),
    "who emailed me about the invoice": ("view_emails", {"view_emails"}, {"write_document", "create_note"}),
    # 10 device
    "turn it down": (None, {"mac_get_volume", "mac_set_volume"}, set()),
    "turn it up": (None, {"mac_get_volume", "mac_set_volume"}, set()),
    "next song": (None, {"mac_media"}, set()),
    "previous song": (None, {"mac_media"}, set()),
    "skip": (None, {"mac_media"}, set()),
    "set volume to 30": ("mac_set_volume", {"mac_set_volume"}, set()),
}


def _offered(d):
    names = set(d.tool_subset or ())
    names |= {n for n, _ in d.direct_calls}
    for group in d.required_tool_groups:
        names |= set(group)
    return names


@pytest.mark.parametrize("prompt", list(ROWS))
def test_common_prompt_route(prompt):
    forced, offered, banned = ROWS[prompt]
    d = asyncio.run(route(prompt))
    got = _offered(d)
    if forced == "MOVE":
        assert "update_event" in got, d
    else:
        if forced:
            assert d.force_first_tool == forced, d
        assert offered <= got, (offered - got, d)
    assert not (banned & got), (banned & got, d)



@pytest.mark.parametrize("prompt", ["tell me a joke", "translate hello to Spanish"])
def test_no_tool_asks_offer_no_send_tools(prompt):
    d = asyncio.run(route(prompt))
    assert not d.tool_subset and not d.force_first_tool, d
    assert SEND <= set(d.forbidden_tools), d


@pytest.mark.parametrize("prompt", [
    "translate this email to Spanish", "tell me a joke and text it to Mom",
    "set a timer on my calendar tomorrow", "move the file to my desktop",
    "move my notes to the archive", "what's 2026-10-17", "define the variable in my code",
    "email Sam that I'm late", "who emailed me about the invoice and forward it to Dana",
    "set the house alarm",
])
def test_lookalikes_are_not_claimed(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool not in {"world_time", "calculate", "convert_units", "set_timer",
                                      "view_emails", "mac_set_volume", "get_weather"}, d


def test_weather_city_is_a_direct_read():
    read = personal_read("how's the weather like in Paris")
    assert read is not None and read.direct_calls == (("get_weather", {"location": "Paris", "period": ""}),)
    read = personal_read("will it rain tomorrow in Seattle")
    assert read.direct_calls[0][1] == {"location": "Seattle", "period": "tomorrow"}
    assert personal_read("will it rain tomorrow") is None


def test_brief_is_a_direct_read():
    for prompt in ("what's my day look like", "give me my morning brief", "brief me"):
        read = personal_read(prompt)
        assert read is not None and read.direct_calls[0][0] == "daily_brief", prompt
        # The verbatim read path must never return the brief's evidence text.
        from service.workflows.reads import compile_read
        assert compile_read(prompt) is None, prompt


def test_summarize_unread_mail_uses_the_summarizer():
    read = personal_read("summarize my unread mail")
    assert read.direct_calls[0][0] == "summarize_emails"
    assert personal_read("show my unread mail").direct_calls[0][0] == "view_emails"


@pytest.mark.parametrize("prompt", ["should I skip class today", "I skipped lunch", "skip the 3pm meeting"])
def test_skip_inside_a_sentence_is_not_a_music_command(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool not in {"mac_media", "mac_set_volume"}, d
    assert "matched skill tools offered" not in d.reason, d


@pytest.mark.parametrize("prompt", ["what's the date today", "what time is it", "what day is it"])
def test_local_clock_is_bound_to_this_macs_zone(prompt):
    # Live fresh-install run: the model passed place="UTC" and answered Sunday
    # at 7 PM on a Saturday in California.
    d = asyncio.run(route(prompt))
    assert d.force_first_tool == "world_time", d
    assert d.tool_argument_bindings.get("world_time") == {"place": "", "compare_to": ""}, d


# ---- review findings: a claim must never drop a clause or misread a lookalike
CLAIMED = {"world_time", "calculate", "convert_units", "set_timer", "view_emails",
           "mac_set_volume", "get_weather"}


@pytest.mark.parametrize("prompt", [
    "find emails from Ana, delete them", "show emails from boss, forward them to Bob",
    "who emailed me about the invoice, forward it to Bob",
    "did Ana email me about the lease, reply that I agree",
    "find emails about the trip, archive them", "what time is it in Tokyo, call Sam",
    "will it rain tomorrow in Seattle, remind me to bring an umbrella",
    "what should I wear in Paris, Ana asked",
])
def test_comma_joined_second_clause_falls_through(prompt):
    from service.router import common_claims
    assert common_claims.claim(prompt) is None, prompt
    assert common_claims.parse_weather(prompt) is None, prompt
    assert not common_claims.is_no_tool_ask(prompt), prompt
    d = asyncio.run(route(prompt))
    assert d.force_first_tool not in CLAIMED, d
    assert d.reason.split(" ·")[0] not in {
        "email lookup -> view_emails(query)", "time in a named place -> world_time"}, d


@pytest.mark.parametrize("first", [
    "set a timer for 10 minutes", "what's the weather in Paris",
    "find emails from Ana", "remind me in 20 minutes to call Mom",
])
@pytest.mark.parametrize("join", ["\n", "\r\n", "\u2028", ". ", ".", "? ", "! "])
def test_second_clause_is_rejected_before_whitespace_cleanup(first, join):
    from service.router import common_claims
    prompt = first + join + "email Bob that I am late"
    assert common_claims.clean(prompt) == ""
    assert common_claims.claim(prompt) is None
    assert common_claims.parse_weather(prompt) is None
    assert not common_claims.is_relative_reminder(prompt)
    decision = asyncio.run(route(prompt))
    assert decision.reason.split(" ·")[0] not in {
        "countdown timer -> set_timer", "weather -> get_weather",
        "email lookup -> view_emails(query)"}
    assert decision.force_first_tool not in {"set_timer", "view_emails", "get_weather"}


@pytest.mark.parametrize("prompt", [
    "set a timer for 10 minutes email Bob", "set a timer for 10 minutes call Mom",
    "set a timer for 10 minutes. Email Bob",
    "start a 5 minute timer email Bob", "set a 1 hr timer call Mom",
    "create a 5 minute timer\nemail Bob", "start a 1 hr timer. Email Bob",
    "start a 5 minute timer and email Bob", "create a 5 minute timer; call Mom",
])
def test_timer_consumes_its_entire_duration(prompt):
    from service.router import common_claims
    assert common_claims.claim(prompt) is None
    assert asyncio.run(route(prompt)).force_first_tool != "set_timer"


def test_quoted_multiline_punctuation_remains_literal_and_ordinary_claims_work():
    from service.router import common_claims
    prompt = 'explain the phrase "set a timer for 10 minutes\nemail Bob. Delete it"'
    decision = asyncio.run(route(prompt))
    assert not decision.needs_tools and not decision.force_first_tool
    assert common_claims.is_relative_reminder('remind me in 20 minutes to say "hello\nagain. Thanks"')
    assert asyncio.run(route("set a timer for 10 minutes")).force_first_tool == "set_timer"
    assert common_claims.parse_weather("weather in Paris") == ("Paris", "")


@pytest.mark.parametrize("prompt, place", [
    ("what time is it in Paris, France", "Paris, France"),
    ("what time is it in Paris right now", "Paris"),
    ("what time is it in London today", "London"),
    ("what time is it in Tokyo now", "Tokyo"),
])
def test_world_time_place_keeps_region_and_drops_now(prompt, place):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool == "world_time", d
    assert d.tool_argument_bindings["world_time"]["place"] == place, d


def test_weather_region_qualifier_still_reads_weather():
    from service.router import common_claims
    assert common_claims.parse_weather("will it rain tomorrow in Portland, OR") == ("Portland, OR", "tomorrow")
    d = asyncio.run(route("weather in Portland, OR"))
    assert "get_weather" in _offered(d), d


def test_someones_timezone_is_not_a_place():
    d = asyncio.run(route("what time is it in Sam's timezone"))
    assert d.force_first_tool != "world_time", d
    assert "world_time" not in d.tool_argument_bindings, d


@pytest.mark.parametrize("prompt", [
    "what is my schedule in spanish", "what is my next meeting in French",
    "what's the weather in Spanish",
])
def test_personal_reads_in_a_language_are_not_translations(prompt):
    d = asyncio.run(route(prompt))
    assert "answer without tools" not in d.reason, d
    assert d.needs_tools or d.direct_calls or d.tool_subset, d


@pytest.mark.parametrize("prompt", [
    "what's hello in Spanish", "how do you say thank you in French",
    "translate good morning to German", "tell me a joke about cats",
])
def test_translations_and_jokes_stay_no_tool(prompt):
    d = asyncio.run(route(prompt))
    assert not d.tool_subset and not d.force_first_tool, d
    assert SEND <= set(d.forbidden_tools), d


@pytest.mark.parametrize("prompt", [
    "set a timer to remind me to email Bob", "set the timer to go off when Sam emails",
    "start the timer for my standup meeting", "make a meeting timer for the board call",
])
def test_timer_without_a_length_is_not_forced(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool != "set_timer", d


@pytest.mark.parametrize("prompt", [
    "set a timer for 10 minutes", "10 minute timer", "timer for 5 min",
    "set a timer for an hour", "set a timer for half an hour", "set a timer for 90 seconds",
    "set a timer for 30s", "start a 1 hr timer",
    "set a 5 minute timer", "create a 5 minute timer", "please start a 5 minute timer",
    "start an hour timer", "start a half an hour timer",
])
def test_timer_with_a_length_is_forced(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool == "set_timer", d


def test_skill_trigger_matches_whole_words_only():
    d = asyncio.run(route("how long is my commute the day after tomorrow"))
    assert not {n for n in _offered(d) if n.startswith("mac_")}, d
    assert "matched skill tools offered" not in d.reason, d
    from service.skills import trigger_matches
    assert trigger_matches("mute the", "please mute the sound")
    assert not trigger_matches("mute the", "my commute the day after")


def test_skill_tools_never_add_outbound_or_calendar_writes(monkeypatch):
    from service.router import router as router_mod

    class _Skill:
        error, enabled = "", True
        tools = [{"name": n} for n in ("send_email", "draft_email", "delete_note", "cancel_event",
                                       "add_calendar_event", "forward_email", "reply_to_email",
                                       "mac_mute")]

        def matches(self, text):
            return True

    monkeypatch.setattr(skills, "all_skills", lambda: {"evil": _Skill()})
    d = router_mod._mk("general", reason="test")
    d.needs_tools, d.tool_subset = True, ["get_weather"]
    router_mod._add_matched_skill_tools(d, "anything")
    assert d.tool_subset == ["get_weather", "mac_mute"], d.tool_subset


@pytest.mark.parametrize("prompt", ["what's 9/11", "what's 24/7"])
def test_dates_and_phrases_are_not_division(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool != "calculate", d


def test_real_division_still_calculates():
    d = asyncio.run(route("what's 144/12"))
    assert d.force_first_tool == "calculate", d


@pytest.mark.parametrize("prompt", ["what does Sam mean", "what's the meaning of life"])
def test_not_a_dictionary_definition(prompt):
    d = asyncio.run(route(prompt))
    assert "word definition" not in d.reason, d


@pytest.mark.parametrize("prompt", ["what does LOL mean", "what's the meaning of serendipity"])
def test_terms_and_acronyms_still_define(prompt):
    d = asyncio.run(route(prompt))
    assert "word definition" in d.reason, d


def test_joke_about_my_data_is_not_a_no_tool_ask():
    d = asyncio.run(route("tell me a joke about my meeting tomorrow"))
    assert "answer without tools" not in d.reason, d


def test_relative_reminder_without_a_body_is_not_forced():
    from service.router import common_claims
    assert not common_claims.is_relative_reminder("remind me in 2 hours")
    d = asyncio.run(route("remind me in 2 hours"))
    assert d.force_first_tool != "add_reminder", d
    assert asyncio.run(route("remind me in 20 minutes to call mom")).force_first_tool == "add_reminder"


@pytest.mark.parametrize("prompt", ["push the review to tomorrow", "delay my flight to tomorrow"])
def test_ambiguous_nouns_are_not_calendar_moves(prompt):
    from service.router import common_claims
    assert not common_claims.is_calendar_move(prompt)


def test_weather_in_a_language_is_not_a_city():
    from service.router.common_claims import parse_weather
    assert parse_weather("what's the weather in Spanish") is None
    assert parse_weather("what's the weather in Paris") == ("Paris", "")


def test_direct_weather_read_rejects_languages_and_second_clauses():
    from service.workflows.workflow20 import weather_read
    assert weather_read("what's the weather in Spanish") is None
    assert weather_read("what's the weather in Seattle, remind me to bring an umbrella") is None
    assert weather_read("what's the weather in Portland, OR").direct_calls[0][1]["location"] == "Portland, OR"
    assert weather_read("weather in Paris tomorrow").direct_calls[0][1] == {"location": "Paris", "period": "tomorrow"}


def test_own_name_and_goals_are_answered_from_what_the_user_said(tmp_path, monkeypatch):
    # Offered 18 retrieved tools, the model looked in the calendar for the
    # goals the user had just given in Getting to know you, and said it could
    # not see them (clean install, build 2016).
    import json
    from service.memory import profile
    from service.router.common_claims import is_no_tool_ask
    path = tmp_path / "profile.json"
    monkeypatch.setattr(profile, "PROFILE_PATH", path, raising=False)
    monkeypatch.setattr(profile, "load", lambda: json.loads(path.read_text()) if path.exists() else profile.empty())
    for ask in ("what's my name?", "who am I", "what should you call me", "do you know my name"):
        assert is_no_tool_ask(ask), ask
    assert not is_no_tool_ask("what is my name and email Bob")
    assert not is_no_tool_ask("what are my goals?")  # nothing told yet: let the router look
    path.write_text(json.dumps({**profile.empty(), "goals": ["Finish my thesis"]}))
    assert is_no_tool_ask("what are my goals?")
    assert is_no_tool_ask("tell me my goals")


def test_identity_block_addresses_the_user_by_the_name_they_chose(monkeypatch):
    from service.memory import identity
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")
    monkeypatch.setattr(identity, "user_emails", lambda: [])
    monkeypatch.setattr(identity, "_preferred_name", lambda: "Tri")
    block = identity.identity_block(with_date=False)
    assert "Name: Adi Jain" in block and "asked to be called Tri" in block
    monkeypatch.setattr(identity, "_preferred_name", lambda: "adi jain")
    assert "asked to be called" not in identity.identity_block(with_date=False)


def test_preferred_name_stays_off_the_external_local_provider(monkeypatch):
    # profile.json is kept from an external local app (Ollama, LM Studio); the
    # chosen name comes from it, so the identity block leaves it out there.
    from service.memory import identity
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")
    monkeypatch.setattr(identity, "user_emails", lambda: [])
    monkeypatch.setattr(identity, "_preferred_name", lambda: "Tri")
    assert "called Tri" in identity.identity_prompt_block(with_date=False, messages=False)
    assert "called Tri" not in identity.identity_prompt_block(
        with_date=False, messages=False, preferred_name=False)
