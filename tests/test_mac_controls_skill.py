"""The bundled mac-controls skill: volume, mute, lock and playback.

These replaced the get_volume/set_volume/lock_screen/music built-ins removed in
PR #224. As skill tools (category skill_tool) they are confirmed in the default
mode, allowed in full-access mode and denied in view-only mode, and their script accepts only a closed set of words or a 0-100 integer, because the
values end up inside AppleScript source — a second interpreter that the skill
runner's one-argv-word rule does not protect.

No test here runs a valid control: every script call below is rejected before
osascript or pmset is reached, so nothing changes on the Mac.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from service import skills
from service.skills.tools import register_skill_tools
from service.tools.registry import REGISTRY

SKILL_DIR = Path(skills.BUNDLED_SKILLS_DIR) / "mac-controls"
TOOLS = {"mac_get_volume", "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media"}
RETIRED = {"get_volume", "set_volume", "lock_screen", "music", "play_audio"}


@pytest.fixture()
def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = skills.all_skills()
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "user-skills")
    loaded = skills.load()
    register_skill_tools(loaded)
    try:
        yield loaded
    finally:
        skills._skills = original
        register_skill_tools(original)


def test_skill_is_bundled_enabled_and_registers_confirm_tier_tools(loaded) -> None:
    skill = loaded["mac-controls"]
    assert skill.bundled and skill.enabled and not skill.error
    assert {t["name"] for t in skill.tools} == TOOLS
    for name in TOOLS:
        assert REGISTRY[name].category == "skill_tool"
    # New names on purpose: a stale grant on a retired built-in must not apply.
    assert not TOOLS & RETIRED and not RETIRED & set(REGISTRY)


def test_typed_arguments_reach_the_model_schema(loaded) -> None:
    level = REGISTRY["mac_set_volume"].parameters["properties"]["level"]
    assert level["type"] == "integer" and (level["minimum"], level["maximum"]) == (0, 100)
    assert REGISTRY["mac_mute"].parameters["properties"]["state"]["enum"] == ["on", "off"]
    media = REGISTRY["mac_media"].parameters
    assert media["properties"]["action"]["enum"] == ["play", "pause", "next", "previous"]
    assert media["required"] == ["action"]


@pytest.mark.parametrize("argv", [
    ["set-volume", "101"], ["set-volume", "-5"], ["set-volume", "50&beep"],
    ["set-volume", '5 & do shell script "id"'], ["set-volume", "007x"], ["set-volume"],
    ["mute", "maybe"], ["mute"], ["media", "stop"], ["media", "play", "itunes"],
    ["media", 'play" & do shell script "id'], ["bogus"], [],
])
def test_script_rejects_anything_outside_its_closed_inputs(argv) -> None:
    proc = subprocess.run(["bash", str(SKILL_DIR / "controls.sh"), *argv],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 2, (argv, proc.stdout, proc.stderr)
    assert proc.stdout == ""


def test_runner_reports_a_rejected_level_without_acting(loaded, monkeypatch) -> None:
    # Sandboxing is irrelevant to validation, and sandbox-exec cannot nest
    # inside an already-sandboxed reviewer or CI process.
    from service.skills import sandbox
    monkeypatch.setattr(sandbox, "available", lambda: False)
    out = asyncio.run(REGISTRY["mac_set_volume"].func(level="50 & beep"))
    assert "0 to 100" in out


@pytest.mark.parametrize("read_only,full_access,tier", [
    (False, False, "CONFIRM"), (False, True, "ALLOW"), (True, False, "DENY")])
def test_policy_tier_follows_the_access_mode(loaded, monkeypatch, tmp_path,
                                             read_only, full_access, tier) -> None:
    # The skill's instructions must not promise a confirmation that full-access
    # mode does not ask for; this pins what each mode actually does.
    from service.safety import grants, policy
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access, raising=False)
    monkeypatch.setattr(policy, "_READ_ONLY", read_only, raising=False)
    for name in TOOLS:
        decision = policy.decide(REGISTRY[name].category, {}, tool=name)
        assert decision.tier.name == tier, (name, decision)
    assert "asks the user to confirm first, as every skill tool does" not in loaded["mac-controls"].body


def test_lock_claims_success_only_after_macos_confirms_it() -> None:
    # Display sleep locks only when "require password" is immediate; on this
    # Mac the delay was 300 s. The lock path must not use it, and its only
    # success line must sit behind the session's own locked flag.
    script = (SKILL_DIR / "controls.sh").read_text()
    lock = "\n".join(line for line in script[script.index("  lock)"):script.index("  media)")].splitlines()
                     if not line.lstrip().startswith("#"))
    assert "displaysleepnow" not in lock
    before_success = lock[:lock.index('echo "screen locked"')]
    assert "session_locked" in before_success


def _console(*sessions: dict) -> bytes:
    import plistlib
    return plistlib.dumps({"IOConsoleLocked": False, "IOConsoleUsers": list(sessions)})


def _session(uid: int, on_console: bool, locked: bool | None) -> dict:
    entry = {"kCGSSessionUserIDKey": uid, "kCGSSessionOnConsoleKey": on_console}
    if locked is not None:   # the key is absent while a session is unlocked
        entry["CGSSessionScreenIsLocked"] = locked
    return entry


_ME, _OTHER = os.getuid(), os.getuid() + 1


@pytest.mark.parametrize("sessions,expected", [
    # A locked background session must not vouch for the one on screen.
    ((_session(_ME, True, None), _session(_OTHER, False, True)), "unlocked"),
    ((_session(_OTHER, False, True), _session(_ME, True, None)), "unlocked"),
    ((_session(_OTHER, True, True), _session(_ME, False, None)), "unlocked"),
    ((_session(_ME, True, False),), "unlocked"),
    ((), "unlocked"),
    ((_session(_OTHER, False, None), _session(_ME, True, True)), "locked"),
])
def test_lock_state_is_read_from_this_users_console_session(sessions, expected) -> None:
    probe = subprocess.run(["bash", "-c", "plutil -help >/dev/null 2>&1"], capture_output=True, timeout=10)
    if probe.returncode == 126 or b"Operation not permitted" in probe.stderr:
        # Simulation QA's sandbox lets test children exec only sh/bash. The
        # probe then fails closed ("unlocked"); the unsandboxed regression
        # gate runs this test for real.
        pytest.skip("this environment does not allow executing plutil")
    proc = subprocess.run(["bash", str(SKILL_DIR / "controls.sh"), "session-locked"],
                          input=_console(*sessions), capture_output=True, timeout=10)
    assert proc.stdout.decode().strip() == expected, proc.stderr


@pytest.mark.parametrize("prompt,tool", [
    ("turn the volume down", "mac_set_volume"), ("set volume to 30%", "mac_set_volume"),
    ("mute my volume", "mac_mute"), ("lock my screen", "mac_lock_screen"),
    ("pause the music", "mac_media"), ("next song on spotify", "mac_media"),
])
def test_requests_offer_the_matching_tool(loaded, prompt, tool) -> None:
    import service.router.router as R
    decision = asyncio.run(R.route(prompt))
    assert tool in (decision.tool_subset or ()), (prompt, decision.reason, decision.tool_subset)


@pytest.mark.parametrize("prompt", ["turn the volume down", "set volume to 30%", "what's my volume",
                                    "mute my volume", "lock my screen", "pause the music",
                                    "next song on spotify"])
def test_triggers_inject_the_instructions(loaded, prompt) -> None:
    assert loaded["mac-controls"].matches(prompt)


@pytest.mark.parametrize("prompt", ["how long is my commute", "what is the volume of a sphere",
                                    "is Spotify cheaper than Apple Music"])
def test_unrelated_words_do_not_inject_the_instructions(loaded, prompt) -> None:
    assert not loaded["mac-controls"].matches(prompt)
