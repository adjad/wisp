from __future__ import annotations

import shutil
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


def test_wisp_ships_two_workflows_a_tool_skill_and_a_style_skill(loaded_bundled_skills) -> None:
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


def test_bundled_skill_toggle_keeps_the_bundled_skill(
        loaded_bundled_skills, tmp_path: Path) -> None:
    """The on/off is saved beside the user's skills; the skill itself stays the
    bundled one (it used to be copied out, which froze its text: #414 audit)."""
    assert skills.get("interview-me").bundled is True

    assert skills.set_enabled("interview-me", False) is True

    toggled = skills.get("interview-me")
    assert toggled is not None
    assert toggled.enabled is False
    assert toggled.bundled is True
    assert not (tmp_path / "user-skills" / "interview-me").exists()
    skills.load()
    assert skills.get("interview-me").enabled is False


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
    # Still the bundled skill: no copy that would freeze it at this release's text.
    assert on.enabled is True and on.bundled and on.path == skills.BUNDLED_SKILLS_DIR / "humanizer"
    assert not (tmp_path / "user-skills" / "humanizer").exists()
    assert "--- humanizer ---" in skills.always_skills_block()
    assert {s["name"]: s["enabled"] for s in (x.as_dict() for x in skills.all_skills().values())}["humanizer"] is True

    assert skills.set_enabled("humanizer", False) is True
    assert skills.get("humanizer").enabled is False
    assert "humanizer" not in skills.always_skills_block()


def test_a_bundled_skill_s_switch_survives_a_reload_and_follows_bundle_edits(
        loaded_bundled_skills, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#414 audit: turning a bundled skill on copied it to ~/.moe/skills, so the
    user kept that copy's text after later releases changed the bundled one."""
    bundle = tmp_path / "bundle"
    shutil.copytree(skills.BUNDLED_SKILLS_DIR / "humanizer", bundle / "humanizer")
    monkeypatch.setattr(skills, "BUNDLED_SKILLS_DIR", bundle)
    skills.load()
    assert skills.set_enabled("humanizer", True) is True
    md = bundle / "humanizer" / "SKILL.md"
    md.write_text(md.read_text().replace("Answer first.", "Answer first, always."))
    skills.load()
    humanizer = skills.get("humanizer")
    assert humanizer.enabled is True and "Answer first, always." in humanizer.body


def test_the_style_skill_stays_out_of_tool_turns(loaded_bundled_skills) -> None:
    """#414 audit: the humanizer applies to plain chat only, so the agent's catalog,
    an @humanizer mention and use_skill don't bring it into a tool-using turn."""
    import asyncio
    from service.skills.tools import use_skill
    assert skills.set_enabled("humanizer", True) is True
    block = skills.skills_context_block("@humanizer reword this")
    assert "humanizer" not in block
    assert "humanizer" not in skills.turn_skill_names("@humanizer reword this")
    assert "nothing to load" in asyncio.run(use_skill("humanizer"))
    assert "--- humanizer ---" in skills.always_skills_block()


@pytest.mark.parametrize(("role", "literal", "styled"), [
    ("general", False, True), ("coding", False, False), ("general", True, False),
])
def test_plain_chat_style_skips_coding_and_literal_envelopes(loaded_bundled_skills, role, literal, styled) -> None:
    """#414 audit: the dash and wording rules would bleed into code, and a literal
    envelope is quoted text to transform, so neither gets the style skill."""
    from service.main import _plain_chat_skill_blocks
    assert skills.set_enabled("humanizer", True) is True
    block = _plain_chat_skill_blocks("hi there", "", role=role, literal_envelope=literal)
    assert ("--- humanizer ---" in block) is styled


def test_every_bundled_body_is_a_shipped_one(loaded_bundled_skills) -> None:
    """A changed bundled body must be added to _SHIPPED_BODIES, or a toggle copy of
    it made by an older build would never be retired."""
    for name, skill in loaded_bundled_skills.items():
        if skill.bundled:
            assert skills._body_hash(skill.body) in skills._SHIPPED_BODIES[name], name


def _old_build_toggle_copy(tmp_path: Path, name: str, enabled: bool) -> Path:
    """What set_enabled did before #415: copy the bundled skill out and rewrite `enabled`."""
    copy = tmp_path / "user-skills" / name
    shutil.copytree(skills.BUNDLED_SKILLS_DIR / name, copy)
    md = copy / "SKILL.md"
    raw = md.read_text()
    marker = "enabled: false" if "enabled: false" in raw else "enabled: true"
    md.write_text(raw.replace(marker, f"enabled: {'true' if enabled else 'false'}", 1))
    return copy


def test_an_older_build_s_toggle_copy_is_retired_and_keeps_its_choice(loaded_bundled_skills, tmp_path: Path) -> None:
    """#415 audit: a copy an older build made to flip a bundled skill outranked the
    bundled skill for good, so later bundled edits never reached that user."""
    copy = _old_build_toggle_copy(tmp_path, "humanizer", True)
    skills.load()
    humanizer = skills.get("humanizer")
    assert humanizer.bundled and humanizer.enabled is True
    assert not copy.exists()
    assert any((tmp_path / "user-skills" / ".retired").iterdir())


def test_a_copy_the_user_edited_stays_theirs(loaded_bundled_skills, tmp_path: Path) -> None:
    copy = _old_build_toggle_copy(tmp_path, "humanizer", True)
    md = copy / "SKILL.md"
    md.write_text(md.read_text() + "\n- Always sign off with my initials.\n")
    skills.load()
    mine = skills.get("humanizer")
    assert not mine.bundled and "my initials" in mine.body and copy.exists()


def test_a_retired_copy_never_undoes_a_newer_toggle(loaded_bundled_skills, tmp_path: Path) -> None:
    assert skills.set_enabled("interview-me", False) is True
    _old_build_toggle_copy(tmp_path, "interview-me", True)
    skills.load()
    assert skills.get("interview-me").bundled and skills.get("interview-me").enabled is False


def test_simultaneous_toggles_keep_both_changes(loaded_bundled_skills, tmp_path: Path) -> None:
    """#415 audit: the state save was an unlocked read-modify-write with one temp name."""
    import threading
    names = ["humanizer", "interview-me", "idea-refine", "mac-controls"]
    barrier = threading.Barrier(len(names))

    def toggle(name: str) -> None:
        barrier.wait()
        assert skills._save_bundled_state(name, name != "mac-controls")

    threads = [threading.Thread(target=toggle, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert skills._bundled_state() == {"humanizer": True, "interview-me": True, "idea-refine": True,
                                      "mac-controls": False}
    assert not list((tmp_path / "user-skills").glob("*.tmp"))


def test_a_copy_with_only_its_frontmatter_edited_stays_theirs(loaded_bundled_skills, tmp_path: Path) -> None:
    """Auditor note: the body and other files matched, so a triggers edit was retired."""
    copy = _old_build_toggle_copy(tmp_path, "humanizer", True)
    md = copy / "SKILL.md"
    md.write_text(md.read_text().replace("---\n", "---\ntriggers: [my own phrase]\n", 1))
    skills.load()
    mine = skills.get("humanizer")
    assert not mine.bundled and "my own phrase" in mine.triggers and copy.exists()


def test_a_safe_dump_rewrite_of_the_frontmatter_is_still_a_toggle_copy(loaded_bundled_skills, tmp_path: Path) -> None:
    """The old set_enabled re-serialized the frontmatter with yaml.safe_dump."""
    import yaml
    copy = tmp_path / "user-skills" / "humanizer"
    shutil.copytree(skills.BUNDLED_SKILLS_DIR / "humanizer", copy)
    md = copy / "SKILL.md"
    m = skills._FRONTMATTER_RE.match(md.read_text())
    meta = yaml.safe_load(m.group(1))
    meta.setdefault("metadata", {}).setdefault("wisp", {})["enabled"] = True
    meta.pop("enabled", None)
    md.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + m.group(2).lstrip())
    skills.load()
    assert skills.get("humanizer").bundled and not copy.exists()


def test_a_copy_that_can_t_move_stays_and_is_retried(loaded_bundled_skills, tmp_path: Path, monkeypatch) -> None:
    copy = _old_build_toggle_copy(tmp_path, "humanizer", True)

    move = shutil.move

    def refuse(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(skills.shutil, "move", refuse)
    skills.load()
    assert copy.exists() and not skills.get("humanizer").bundled
    monkeypatch.setattr(skills.shutil, "move", move)
    skills.load()
    assert not copy.exists() and skills.get("humanizer").bundled


def test_a_failed_migration_still_loads_every_skill(loaded_bundled_skills, monkeypatch) -> None:
    def boom():
        raise RuntimeError("unexpected")
    monkeypatch.setattr(skills, "_retire_toggle_copies", boom)
    assert {"humanizer", "interview-me"} <= set(skills.load())
