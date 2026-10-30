from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from service import skills
from service.memory.store import SessionStore


@pytest.fixture()
def loaded_bundled_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = skills.all_skills()
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "user-skills")
    loaded = skills.load()
    try:
        yield loaded
    finally:
        skills._skills = original


def test_wisp_ships_only_the_two_requested_workflows(loaded_bundled_skills) -> None:
    bundled = {name for name, skill in loaded_bundled_skills.items()
               if skill.bundled}
    # mac-controls is a tool skill (fixed device commands), not a workflow; humanizer
    # is the style skill behind Settings › Human-sounding replies.
    assert bundled == {"interview-me", "idea-refine", "mac-controls", "humanizer"}
    assert all(skill.body for skill in loaded_bundled_skills.values())
    assert all(len(skill.body) <= skills.MAX_BODY_CHARS
               for skill in loaded_bundled_skills.values())
    workflows = {name for name, skill in loaded_bundled_skills.items()
                 if skill.conversation_workflow}
    assert workflows == {"interview-me", "idea-refine"}


def test_bundled_skill_toggle_creates_a_user_override(
        loaded_bundled_skills, tmp_path: Path) -> None:
    assert skills.get("interview-me").bundled is True

    assert skills.set_enabled("interview-me", False) is True

    overridden = skills.get("interview-me")
    assert overridden is not None
    assert overridden.enabled is False
    assert overridden.bundled is False
    assert overridden.path == tmp_path / "user-skills" / "interview-me"


def test_interview_activation_persists_and_can_stop(loaded_bundled_skills) -> None:
    active = skills.select_for_turn("Please interview me about this product idea")
    assert active == "interview-me"

    active = skills.select_for_turn("It is mainly for teachers", active)
    assert active == "interview-me"

    active = skills.select_for_turn("Stop interviewing me", active)
    assert active == ""


def test_explicit_invocation_and_completion_release(loaded_bundled_skills) -> None:
    active = skills.select_for_turn("@idea-refine a better morning routine")
    assert active == "idea-refine"

    active = skills.select_for_turn(
        "What should we do next?",
        active,
        "Looks good.\n<!-- wisp-skill-complete: idea-refine -->",
    )
    assert active == ""


def test_plain_chat_gets_only_the_selected_workflow(loaded_bundled_skills) -> None:
    assert skills.selected_skill_block("What is the weather?") == ""

    block = skills.selected_skill_block(
        "My audience is students", active_name="idea-refine")
    assert "--- Active skill: idea-refine ---" in block
    assert "Phase 1: Understand and expand" in block
    assert "interview-me" not in block
    assert "Installed skills" not in block


def test_ordinary_skill_does_not_become_a_persistent_workflow(
        loaded_bundled_skills, tmp_path: Path) -> None:
    skills._skills["one-shot"] = skills.Skill(
        name="one-shot",
        description="A normal trigger-matched instruction skill",
        body="Do one thing.",
        path=tmp_path / "one-shot",
        triggers=["one shot"],
    )

    assert skills.select_for_turn("Please do the one shot procedure") == ""
    assert skills.selected_skill_block("Please do the one shot procedure") == ""
    assert "one-shot" in skills.skills_context_block(
        "Please do the one shot procedure")


def test_existing_session_database_migrates_active_skill(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            created_at REAL,
            last_used REAL,
            summary TEXT DEFAULT '',
            summarized_idx INTEGER DEFAULT 0,
            pinned_role TEXT,
            pinned_model TEXT
        )
    """)
    db.commit()
    db.close()

    store = SessionStore(db_path)
    sid = store.create_session()
    store.set_active_skill(sid, "interview-me")

    session = store.get_session(sid)
    assert session is not None
    assert session["active_skill"] == "interview-me"


def test_the_human_sounding_replies_switch_has_a_skill_to_turn_on(loaded_bundled_skills, tmp_path: Path) -> None:
    """Settings › Human-sounding replies posts /skills/humanizer/enable. No humanizer
    skill shipped (it only ever existed as a user-installed one), so set_enabled
    returned False, the app re-read "off" and the switch snapped back (2026-10-29)."""
    humanizer = skills.get("humanizer")
    assert humanizer is not None and humanizer.bundled and humanizer.always
    assert humanizer.enabled is False                         # off until the user turns it on
    assert skills.always_skills_block() == "" or "humanizer" not in skills.always_skills_block()

    assert skills.set_enabled("humanizer", True) is True
    on = skills.get("humanizer")
    assert on.enabled is True and on.path == tmp_path / "user-skills" / "humanizer"
    assert "--- humanizer ---" in skills.always_skills_block()
    assert {s["name"]: s["enabled"] for s in (x.as_dict() for x in skills.all_skills().values())}["humanizer"] is True

    assert skills.set_enabled("humanizer", False) is True
    assert skills.get("humanizer").enabled is False
    assert "humanizer" not in skills.always_skills_block()
