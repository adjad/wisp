"""The bundled mac-controls skill: volume, mute, lock and playback.

These replaced the get_volume/set_volume/lock_screen/music built-ins removed in
PR #224. As skill tools they are always confirm-tier (category skill_tool), and
their script accepts only a closed set of words or a 0-100 integer, because the
values end up inside AppleScript source — a second interpreter that the skill
runner's one-argv-word rule does not protect.

No test here runs a valid control: every script call below is rejected before
osascript or pmset is reached, so nothing changes on the Mac.
"""
from __future__ import annotations

import asyncio
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


def test_runner_reports_a_rejected_level_without_acting(loaded) -> None:
    out = asyncio.run(REGISTRY["mac_set_volume"].func(level="50 & beep"))
    assert "0 to 100" in out


@pytest.mark.parametrize("prompt,tool", [
    ("turn the volume down", "mac_set_volume"), ("set volume to 30%", "mac_set_volume"),
    ("mute my volume", "mac_mute"), ("lock my screen", "mac_lock_screen"),
    ("pause the music", "mac_media"), ("next song on spotify", "mac_media"),
])
def test_requests_offer_the_matching_tool(loaded, prompt, tool) -> None:
    import service.router.router as R
    decision = asyncio.run(R.route(prompt))
    assert tool in (decision.tool_subset or ()), (prompt, decision.reason, decision.tool_subset)


@pytest.mark.parametrize("prompt", ["turn the volume down", "lock my screen", "pause the music"])
def test_triggers_inject_the_instructions(loaded, prompt) -> None:
    assert loaded["mac-controls"].matches(prompt)


def test_unrelated_words_do_not_inject_the_instructions(loaded) -> None:
    assert not loaded["mac-controls"].matches("how long is my commute")
