"""Regression contract for the 100-tool consolidation.

Covers what the merge could silently break: the registry ceiling, retired names
staying retired, internal tools staying out of retrieval, merged tools keeping
the fixed arguments their old single-purpose routes implied, and the two
retrieval regressions found while retiring tools (the .txt typo fallback and
the uninstall gate, covered in test_lexical_tool_retrieval).
"""
from __future__ import annotations

import asyncio

import pytest

import service.tools  # noqa: F401  (registers every tool)
from service.router import router as R
from service.router.reranker import lexical_rank
from service.tools.registry import REGISTRY, is_tool_searchable, searchable_tool_names

RETIRED = {
    # merged into a broader tool
    "set_alarm", "set_sleep_timer", "stopwatch", "list_timers", "cancel_timer",
    "get_battery_status", "network_info", "list_bluetooth_devices",
    "set_wifi", "spotify", "play_podcast", "play_audiobook", "play_radio",
    "stop_radio", "play_ambient", "archive_email", "flag_email",
    "mark_email_read", "wisp_sync", "weather_alerts", "air_quality",
    "rain_radar", "switch_app",
    # retired outright
    "trash_file", "http_request", "country_info", "set_keyboard_backlight",
    "get_directions", "convert_currency", "uninstall_app", "unsubscribe",
}
MERGED = {"set_timer", "manage_timers", "system_status", "toggle_setting",
          "music", "play_audio", "email_triage", "wisp_status", "get_weather"}
INTERNAL = {"collect_task_evidence", "grocery_reminder_evidence",
            "grocery_note_evidence", "search_coverage"}


def _route(prompt):
    return asyncio.run(R.route(prompt))


def test_registry_is_capped_at_one_hundred():
    assert len(REGISTRY) == 100


def test_retired_names_stay_retired_and_merged_tools_exist():
    assert RETIRED.isdisjoint(REGISTRY)
    assert MERGED <= set(REGISTRY)


def test_internal_tools_are_registered_but_never_searchable():
    assert INTERNAL <= set(REGISTRY)
    searchable = set(searchable_tool_names())
    assert INTERNAL.isdisjoint(searchable)
    assert not any(is_tool_searchable(name) for name in INTERNAL)
    assert not is_tool_searchable("update_event")


@pytest.mark.parametrize("prompt, tool, args", [
    ("what's my battery level", "system_status", {"section": "battery"}),
])
def test_device_reads_dispatch_the_merged_tool_with_its_section(prompt, tool, args):
    assert _route(prompt).direct_calls == [(tool, args)]


@pytest.mark.parametrize("prompt", [
    "get that out of my inbox",
    "move the Walgreens email out of my inbox",
])
def test_inbox_removal_phrasing_offers_email_triage(prompt):
    assert "email_triage" in (_route(prompt).tool_subset or [])


@pytest.mark.parametrize("prompt", ["what is in my inbox", "show me emails out of my inbox today"])
def test_inbox_reads_do_not_gain_triage(prompt):
    assert "email_triage" not in (_route(prompt).tool_subset or [])


def test_txt_file_request_ranks_read_file_first_without_outbound_tools():
    # Retiring convert_file left "txt" out of the vocabulary, so the typo
    # fallback read it as "text" and ranked Messages above read_file.
    prompt = "open report.txt and read it to me"
    assert lexical_rank(prompt)[0] == "read_file"
    offered = set(_route(prompt).tool_subset or [])
    assert "read_file" in offered
    assert {"send_message", "send_email", "reply_to_email", "forward_email"}.isdisjoint(offered)
