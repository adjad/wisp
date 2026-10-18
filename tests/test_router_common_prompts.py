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


def test_summarize_unread_mail_uses_the_summarizer():
    read = personal_read("summarize my unread mail")
    assert read.direct_calls[0][0] == "summarize_emails"
    assert personal_read("show my unread mail").direct_calls[0][0] == "view_emails"


@pytest.mark.parametrize("prompt", ["should I skip class today", "I skipped lunch", "skip the 3pm meeting"])
def test_skip_inside_a_sentence_is_not_a_music_command(prompt):
    d = asyncio.run(route(prompt))
    assert d.force_first_tool not in {"mac_media", "mac_set_volume"}, d
    assert "matched skill tools offered" not in d.reason, d
