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
    "delete_path", "get_volume", "set_volume", "lock_screen", "music",
    "play_audio", "grocery_note_evidence", "grocery_reminder_evidence",
}
MERGED = {"set_timer", "manage_timers", "system_status", "toggle_setting",
          "email_triage", "wisp_status", "get_weather"}
INTERNAL = {"collect_task_evidence", "search_coverage"}


def _route(prompt):
    return asyncio.run(R.route(prompt))


def test_registry_stays_under_the_one_hundred_cap():
    # Built-in tools only. use_skill, skill tools and MCP tools register later,
    # once the user installs them, and test modules can leave them behind.
    builtin = [name for name, tool in REGISTRY.items()
               if tool.func.__module__.startswith("service.tools.")]
    assert len(builtin) == 94


def test_retired_names_stay_retired_and_merged_tools_exist():
    assert RETIRED.isdisjoint(REGISTRY)
    assert MERGED <= set(REGISTRY)


def test_internal_tools_are_registered_but_never_searchable():
    assert INTERNAL <= set(REGISTRY)
    searchable = set(searchable_tool_names())
    assert INTERNAL.isdisjoint(searchable)
    assert not any(is_tool_searchable(name) for name in INTERNAL)
    assert is_tool_searchable("update_event"), "a real verified move since 2026-10-18"


@pytest.mark.parametrize("prompt, tool, args", [
    ("what's my battery level", "system_status", {"section": "battery"}),
])
def test_device_reads_dispatch_the_merged_tool_with_its_section(prompt, tool, args):
    assert _route(prompt).direct_calls == [(tool, args)]


@pytest.mark.parametrize("prompt", [
    "get that out of my inbox",
    "move the Walgreens email out of my inbox",
    "take those newsletters out of the inbox",
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


@pytest.mark.parametrize("prompt", [
    "get the tracking number out of my inbox",
    "get the receipts out of my inbox and summarize them",
    "get my flight info out of my inbox",
    "get the info from that email out of my inbox",
    "take the text of that email out of my inbox and read it to me",
    "get the newest emails out of my inbox and summarize them",
    "get this week's mail out of my inbox and summarize it",
])
def test_extracting_information_from_the_inbox_stays_a_read(prompt):
    offered = set(_route(prompt).tool_subset or [])
    assert {"email_triage", "send_email", "reply_to_email", "forward_email"}.isdisjoint(offered)


# --- standing grants on merged tools ----------------------------------------

@pytest.fixture
def isolated_grants(monkeypatch, tmp_path):
    from service.safety import grants, policy
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", False, raising=False)
    return grants


def test_always_allow_on_one_setting_does_not_cover_another(isolated_grants):
    grants = isolated_grants
    assert grants.grant("toggle_setting", {"setting": "dnd", "on": True})["scope"] == "do_not_disturb"
    assert grants.check("toggle_setting", {"setting": "Do Not Disturb", "on": False}) == "allow"
    for setting in ("wifi", "Wi-Fi", "voiceover", "dark_mode", "bluetooth"):
        assert grants.check("toggle_setting", {"setting": setting, "on": False}) is None, setting


def test_always_allow_on_one_triage_action_does_not_cover_archive(isolated_grants):
    grants = isolated_grants
    grants.grant("email_triage", {"message_id": "<a@x>", "action": "mark_read"})
    assert grants.check("email_triage", {"message_id": "<b@x>", "action": "mark_read"}) == "allow"
    assert grants.check("email_triage", {"message_id": "<b@x>", "action": "archive"}) is None


def test_pre_merge_tool_wide_allow_keeps_its_old_coverage(isolated_grants):
    grants = isolated_grants
    # Written by an older build, where toggle_setting had five settings and
    # set_timer only counted down.
    grants._CACHE["toggle_setting"] = {"allow": [{"scope": ""}], "deny": []}
    grants._CACHE["set_timer"] = {"allow": [{"scope": ""}], "deny": []}
    assert grants.check("toggle_setting", {"setting": "bluetooth", "on": True}) == "allow"
    assert grants.check("toggle_setting", {"setting": "wifi", "on": False}) is None
    assert grants.check("set_timer", {"duration": "5 min"}) == "allow"
    assert grants.check("set_timer", {"kind": "alarm", "time": "7am"}) is None
    # Deliberately widening it again covers every behaviour.
    grants.grant("toggle_setting", {}, decision="allow", scoped=False)
    assert grants.check("toggle_setting", {"setting": "wifi", "on": False}) == "allow"


@pytest.mark.parametrize("retired, tool, args", [
    ("archive_email", "email_triage", {"message_id": "<a@x>", "action": "archive"}),
    ("set_wifi", "toggle_setting", {"setting": "wifi", "on": False}),
    ("set_alarm", "set_timer", {"kind": "alarm", "time": "7am"}),
])
def test_deny_on_a_retired_name_still_blocks_its_merged_behaviour(isolated_grants, monkeypatch,
                                                                   retired, tool, args):
    from service.safety import policy
    grants = isolated_grants
    grants._CACHE[retired] = {"allow": [], "deny": [{"scope": ""}]}
    monkeypatch.setattr(policy, "_FULL_ACCESS", True, raising=False)
    assert grants.check(tool, args) == "deny"
    assert policy.decide(REGISTRY[tool].category, args, tool=tool).tier is policy.Tier.DENY


def test_retired_deny_does_not_spill_onto_other_behaviours(isolated_grants):
    grants = isolated_grants
    grants._CACHE["archive_email"] = {"allow": [], "deny": [{"scope": ""}]}
    grants._CACHE["mark_email_read"] = {"allow": [], "deny": [{"scope": ""}]}
    assert grants.check("email_triage", {"message_id": "<a@x>", "action": "flag"}) is None
    # Allowing one behaviour lifts only that part of the old deny.
    grants.grant("email_triage", {"message_id": "<a@x>", "action": "mark_read"})
    assert grants.check("email_triage", {"message_id": "<a@x>", "action": "mark_read"}) == "allow"
    assert grants.check("email_triage", {"message_id": "<a@x>", "action": "mark_unread"}) == "deny"
    assert grants.check("email_triage", {"message_id": "<a@x>", "action": "archive"}) == "deny"


@pytest.mark.parametrize("tool, args", [
    ("toggle_setting", {"on": False}),
    ("email_triage", {"message_id": "<a@x>"}),
])
def test_always_allow_without_a_behaviour_is_refused(isolated_grants, tool, args):
    grants = isolated_grants
    assert grants.grant(tool, args, decision="allow")["ok"] is False
    assert grants._CACHE.get(tool, {}).get("allow", []) == []


def test_malformed_always_allow_cannot_erase_a_retired_deny(isolated_grants):
    from service.safety import policy
    grants = isolated_grants
    grants._CACHE["set_wifi"] = {"allow": [], "deny": [{"scope": ""}]}
    grants.grant("toggle_setting", {"on": False}, decision="allow")
    wifi_off = {"setting": "wifi", "on": False}
    assert grants.check("toggle_setting", wifi_off) == "deny"
    assert policy.decide(REGISTRY["toggle_setting"].category, wifi_off,
                         tool="toggle_setting").tier is policy.Tier.DENY


def test_tool_wide_allow_keeps_retired_denies_like_explicit_ones(isolated_grants):
    grants = isolated_grants
    grants._CACHE["set_wifi"] = {"allow": [], "deny": [{"scope": ""}]}
    grants.grant("toggle_setting", {"setting": "firewall"}, decision="deny")
    grants.grant("toggle_setting", {}, decision="allow", scoped=False)
    assert grants.check("toggle_setting", {"setting": "wifi", "on": False}) == "deny"
    assert grants.check("toggle_setting", {"setting": "firewall", "on": False}) == "deny"
    assert grants.check("toggle_setting", {"setting": "bluetooth", "on": False}) == "allow"


# ---- appearance (toggle_setting dark_mode / auto_appearance) -------------
def _fake_proc(returncode=0, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize("mode, flag", [("dark", "true"), ("light", "false")])
def test_dark_and_light_mode_script_is_one_clean_system_events_line(monkeypatch, mode, flag):
    from service.tools import system_extras2 as X
    scripts, runs = [], []
    monkeypatch.setattr(X, "_osa", lambda s: scripts.append(s) or _fake_proc())
    # `defaults delete` of an unset key exits 1; that must not matter.
    monkeypatch.setattr(X, "_run", lambda argv, *a, **k: runs.append(argv) or _fake_proc(1))
    assert X.appearance_mode(mode) == f"Appearance set to {mode}."
    assert scripts == [f'tell application "System Events" to tell appearance '
                       f'preferences to set dark mode to {flag}']
    assert "2>" not in scripts[0] and "do shell script" not in scripts[0]
    assert runs == [["defaults", "delete", "NSGlobalDomain",
                     "AppleInterfaceStyleSwitchesAutomatically"]]


def test_toggle_dark_mode_reaches_appearance_and_reports_osascript_failure(monkeypatch):
    from service.tools import system_extras2 as X
    monkeypatch.setattr(X, "_run", lambda argv, *a, **k: _fake_proc(1))
    monkeypatch.setattr(X, "_osa", lambda s: _fake_proc(1, stderr="not allowed"))
    out = REGISTRY["toggle_setting"].func(setting="dark_mode", on=True)
    assert out == "(could not change appearance: not allowed)"
    monkeypatch.setattr(X, "_osa", lambda s: _fake_proc(0))
    assert REGISTRY["toggle_setting"].func(setting="dark_mode", on=True) == "Appearance set to dark."


def test_delete_timeout_does_not_stop_the_appearance_change(monkeypatch):
    import subprocess
    from service.tools import system_extras2 as X

    def boom(argv, *a, **k):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(X, "_run", boom)
    monkeypatch.setattr(X, "_osa", lambda s: _fake_proc(0))
    assert X.appearance_mode("light") == "Appearance set to light."


# ---- bluetooth / airdrop must not claim a change they cannot confirm ------
def _bt_json(state):
    import json
    ctrl = {"controller_state": state} if state else {}
    return json.dumps({"SPBluetoothDataType": [{"controller_properties": ctrl}]})


def _fake_bluetooth(monkeypatch, states):
    """defaults write succeeds, killall fails, system_profiler walks `states`."""
    from service.tools import system_extras as X
    calls, seq = [], list(states)
    monkeypatch.setattr(X.time, "sleep", lambda s: None)

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[0] == "killall":
            return _fake_proc(1, stderr="Operation not permitted")
        if argv[0] == "system_profiler":
            return _fake_proc(0, stdout=_bt_json(seq.pop(0) if len(seq) > 1 else seq[0]))
        return _fake_proc(0)
    monkeypatch.setattr(X, "_run", fake_run)
    return calls


def test_bluetooth_off_that_changes_nothing_is_not_reported_as_done(monkeypatch):
    _fake_bluetooth(monkeypatch, ["attrib_on"])
    out = REGISTRY["toggle_setting"].func(setting="bluetooth", on=False)
    assert out.startswith("(") and "Bluetooth turned" not in out
    assert "Control Center" in out


def test_bluetooth_unreadable_state_is_not_reported_as_done(monkeypatch):
    _fake_bluetooth(monkeypatch, [""])
    out = REGISTRY["toggle_setting"].func(setting="bluetooth", on=False)
    assert "Bluetooth turned" not in out


@pytest.mark.parametrize("on, state", [(False, "attrib_off"), (True, "attrib_on")])
def test_bluetooth_change_that_reads_back_reports_success(monkeypatch, on, state):
    _fake_bluetooth(monkeypatch, [state])
    out = REGISTRY["toggle_setting"].func(setting="bluetooth", on=on)
    assert out == f"Bluetooth turned {'on' if on else 'off'}."


def test_bluetooth_that_lags_one_read_is_confirmed_on_the_second(monkeypatch):
    _fake_bluetooth(monkeypatch, ["attrib_on", "attrib_off"])
    assert REGISTRY["toggle_setting"].func(setting="bluetooth", on=False) == "Bluetooth turned off."


@pytest.mark.parametrize("on, stored, ok", [
    (False, "1", True), (False, "0", False), (True, "0", True), (True, "1", False)])
def test_airdrop_reports_only_what_the_preference_shows(monkeypatch, on, stored, ok):
    from service.tools import system_extras as X

    def fake_run(argv, **kw):
        if argv[:2] == ["defaults", "read"]:
            return _fake_proc(0, stdout=stored + "\n")
        return _fake_proc(0)
    monkeypatch.setattr(X, "_run", fake_run)
    out = REGISTRY["toggle_setting"].func(setting="airdrop", on=on)
    assert out.startswith("AirDrop ") == ok
    assert out.startswith("(") != ok
