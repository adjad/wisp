"""The user's profile (service/memory/profile.py) and everything it feeds.

Store round trip and bounds, the prompt block, the brief's Keeping in mind
section, Today's Needs you order, the offer logic and the HTTP surface.
Everything runs against the disposable WISP_HOME conftest.py sets.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.memory import profile as P
from service.memory import profile_api


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROFILE_PATH", tmp_path / "profile.json")
    yield


SAMPLE = {
    "name": "Adi",
    "about": "A student building a local assistant on the side.",
    "goals": ["Ship Wisp 1.5", "Run a half marathon"],
    "struggles": ["Overcommitting to too much", "Letting email pile up"],
    "people": [{"name": "Mom", "relation": "mother", "note": "calls on Sundays"},
               {"name": "Sam Lee", "relation": "co-founder", "note": ""}],
    "tone": "gentle",
    "detail": "short",
    "boundaries": "Don't bring up my weight.",
    "notes": "I think out loud.",
}


# --- store -----------------------------------------------------------------

def test_round_trip_and_defaults():
    assert P.load() == P.empty()
    saved = P.update(SAMPLE, now=1000.0)
    again = P.load()
    assert again == saved
    for key, value in SAMPLE.items():
        assert again[key] == value
    assert again["updated_at"] == 1000.0
    assert again["completed_at"] is None
    on_disk = json.loads(P.PROFILE_PATH.read_text())
    assert on_disk["name"] == "Adi" and on_disk["version"] == P.VERSION


def test_partial_patch_keeps_other_fields():
    P.update(SAMPLE, now=1.0)
    out = P.update({"name": "Adi J"}, now=2.0)
    assert out["name"] == "Adi J" and out["goals"] == SAMPLE["goals"]
    assert out["updated_at"] == 2.0


@pytest.mark.parametrize("patch", [
    {"name": "x" * 61},
    {"goals": [f"goal {i}" for i in range(6)]},
    {"goals": "not a list"},
    {"people": [{"name": f"P{i}"} for i in range(9)]},
    {"people": ["Mom"]},
    {"tone": "sarcastic"},
    {"detail": "medium"},
    {"nickname": "nope"},
    {"notes": 42},
])
def test_rejects_out_of_bounds(patch):
    with pytest.raises(ValueError):
        P.validate(patch)


def test_cleaning_dedupes_and_trims():
    out = P.validate({"goals": ["  Ship it ", "ship it", ""], "people": [
        {"name": " Mom ", "relation": None}, {"name": "mom"}, {"name": ""}]})
    assert out["goals"] == ["Ship it"]
    assert out["people"] == [{"name": "Mom", "relation": "", "note": ""}]
    assert P.validate({"tone": ""}) == {"tone": "warm"}


def test_corrupt_file_reads_as_empty_and_partial_bounds_keep_valid_fields():
    P.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    P.PROFILE_PATH.write_text("{not json")
    assert P.load() == P.empty()
    P.PROFILE_PATH.write_text(json.dumps({"name": "Adi", "about": "y" * 5000, "tone": "warm"}))
    loaded = P.load()
    assert loaded["name"] == "Adi" and loaded["about"] == ""


def test_unparseable_file_is_kept_aside_not_overwritten():
    P.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    P.PROFILE_PATH.write_text("{not json")
    assert P.load() == P.empty()
    assert not P.PROFILE_PATH.exists()
    kept = list(P.PROFILE_PATH.parent.glob("profile.json.bad-*"))
    assert len(kept) == 1 and kept[0].read_text() == "{not json"
    P.PROFILE_PATH.write_text("[1, 2]")
    assert P.load() == P.empty()
    assert len(list(P.PROFILE_PATH.parent.glob("profile.json.bad-*"))) == 2


def test_single_line_fields_collapse_whitespace_and_newlines():
    out = P.validate({"name": "Adi\n  Jain\t", "goals": ["Ship\nthe   app"],
                      "struggles": ["focus\r\nafter lunch"], "notes": "line one\nline two"})
    assert out["name"] == "Adi Jain"
    assert out["goals"] == ["Ship the app"]
    assert out["struggles"] == ["focus after lunch"]
    assert out["notes"] == "line one\nline two", "multi-line fields keep their breaks"


def test_no_offer_when_about_you_already_has_content():
    P.update({"name": "Adi", "goals": ["Ship the thesis"]})
    status = P.status(P.load())
    assert status["has_content"] is True and status["offer"] is False


def test_a_name_alone_still_offers_the_rest():
    # Step one answered, then the person left: goals and tone are still worth asking.
    P.update({"name": "Adi"})
    status = P.status(P.load())
    assert status["has_content"] is True and status["offer"] is True


def test_clear_erases_every_answer_and_keeps_the_decision():
    P.update(SAMPLE)
    erased = P.clear(now=5.0)
    for field in P.EDITABLE:
        assert erased[field] == P.empty()[field], field
    assert P.load() == erased and not P.has_content(P.load())
    on_disk = P.PROFILE_PATH.read_text()
    for value in ("Adi", "Mom", "weight", "Ship Wisp"):
        assert value not in on_disk, "no answer survives an erase"
    # Erasing on purpose is a decision: the questions are not offered again on
    # their own (the menu bar and /about still open them).
    assert P.status(P.load(), now=5.0 + 10 * P.REOFFER_AFTER_S)["offer"] is False


def test_profile_file_is_owner_only():
    P.update(SAMPLE)
    assert P.PROFILE_PATH.stat().st_mode & 0o777 == 0o600
    P.clear()
    assert P.PROFILE_PATH.stat().st_mode & 0o777 == 0o600


def test_version_one_short_reads_as_the_default_length():
    # The old flow saved its preselected "short" with every answer.
    P.PROFILE_PATH.write_text(json.dumps({"version": 1, "name": "Adi", "tone": "warm", "detail": "short"}))
    loaded = P.load()
    assert loaded["detail"] == "thorough"
    assert P.filled_fields(loaded) == ["name"]
    assert "Keep replies short" not in P.profile_block(loaded)
    # A current file keeps an explicit Short.
    P.update({"detail": "short"})
    assert P.load()["detail"] == "short"
    assert json.loads(P.PROFILE_PATH.read_text())["version"] == P.VERSION



@pytest.mark.parametrize("version", ["2", None, 2.5, True, [2], {"v": 2}])
def test_odd_version_reads_as_version_one_instead_of_failing(version):
    # Audit 32649065: a hand-edited "version" used to raise TypeError (a 500).
    P.PROFILE_PATH.write_text(json.dumps({"version": version, "name": "Adi", "detail": "short"}))
    loaded = P.load()
    assert loaded["name"] == "Adi" and loaded["detail"] == "thorough"


def test_a_world_readable_version_one_file_is_tightened_on_first_read():
    P.PROFILE_PATH.write_text(json.dumps({"version": 1, "name": "Adi"}))
    P.PROFILE_PATH.chmod(0o644)
    assert P.load()["name"] == "Adi"
    assert P.PROFILE_PATH.stat().st_mode & 0o777 == 0o600


def test_a_set_aside_unreadable_copy_is_owner_only():
    P.PROFILE_PATH.write_text('{"name": "Adi", broken')
    P.PROFILE_PATH.chmod(0o644)
    P.load()
    kept = list(P.PROFILE_PATH.parent.glob("profile.json.bad-*"))
    assert len(kept) == 1 and kept[0].stat().st_mode & 0o777 == 0o600


def test_erase_also_removes_set_aside_copies():
    # Erase means every copy: a .bad-* file holds the same personal text.
    P.PROFILE_PATH.write_text('{"name": "Adi", "people": [{"name": "Mom"}] broken')
    P.load()
    assert list(P.PROFILE_PATH.parent.glob("profile.json.bad-*"))
    P.update(SAMPLE)
    P.clear()
    assert not list(P.PROFILE_PATH.parent.glob("profile.json.bad-*"))
    leftovers = [f for f in P.PROFILE_PATH.parent.iterdir() if f.name.startswith("profile.json")]
    assert [f.name for f in leftovers] == ["profile.json"]
    assert "Adi" not in P.PROFILE_PATH.read_text()


def test_preview_does_not_promise_on_this_mac_only():
    # The app's lock line says where the profile lives (local or cloud); the
    # preview must not claim "on this Mac only" in a cloud mode.
    lines = P.preview({**P.empty(), "about": "A student."})
    assert any("shapes every answer" in line for line in lines)
    assert not any("this Mac" in line for line in lines)


# --- status / offer ----------------------------------------------------------

def test_offer_logic():
    now = 10_000_000.0
    assert P.status(P.empty(), now=now)["offer"] is True
    P.dismiss(now=now)
    assert P.status(P.load(), now=now + 60)["offer"] is False, "just said Later"
    assert P.status(P.load(), now=now + P.REOFFER_AFTER_S)["offer"] is True, "offered once more after three days"
    P.dismiss(now=now + P.REOFFER_AFTER_S)
    assert P.status(P.load(), now=now + 10 * P.REOFFER_AFTER_S)["offer"] is False, "two Laters settle it"
    P.update({"name": "Adi"}, now=now)
    P.mark_complete(now=now)
    status = P.status(P.load(), now=now)
    assert status["completed"] and not status["offer"] and status["filled"] == ["name"]
    assert status["stale"] is False
    assert P.status(P.load(), now=now + P.STALE_AFTER_S)["stale"] is True


def test_status_filled_ignores_defaults():
    assert P.status(P.empty())["has_content"] is False
    assert P.filled_fields(P.empty() | P.validate({"tone": "warm", "detail": "thorough"})) == []


@pytest.mark.parametrize("patch,field,rule", [
    ({"tone": "direct"}, "tone", "straight to the point"),
    ({"tone": "gentle"}, "tone", "Speak gently"),
    ({"detail": "short"}, "detail", "Keep replies short"),
])
def test_nondefault_style_alone_is_applied_and_can_be_erased(patch, field, rule, client):
    response = client.patch("/profile", json=patch)
    assert response.status_code == 200
    saved = response.json()
    assert saved["status"]["has_content"] is True
    assert saved["status"]["filled"] == [field]
    assert rule in P.profile_block(P.load()), "style applies even without a name or goal"
    assert not any("haven't told me anything" in line for line in saved["preview"])
    assert client.get("/profile").json()["status"]["has_content"] is True
    erased = client.delete("/profile").json()
    assert all(erased["profile"][f] == P.empty()[f] for f in P.EDITABLE)
    assert erased["status"]["has_content"] is False and erased["status"]["offer"] is False
    assert P.profile_block() == ""


# --- use 1: prompt block ---------------------------------------------------------

def test_prompt_block_is_empty_without_content_and_complete_with_it():
    assert P.profile_block(P.empty()) == ""
    block = P.profile_block(P.empty() | SAMPLE)
    assert block.startswith("\n\n")
    for needle in ("Call them Adi", "Ship Wisp 1.5", "Run a half marathon",
                   "Overcommitting to too much", "Mom (mother) — calls on Sundays",
                   "Sam Lee (co-founder)", "Speak gently", "Keep replies short",
                   "Don't bring up my weight", "I think out loud"):
        assert needle in block, needle
    # The rules that keep it kind and keep it from becoming a VIP list.
    assert "never as a reproach" in block
    assert "does not make Wisp alert about them" in block


def test_prompt_block_tone_and_detail_variants():
    direct = P.profile_block(P.empty() | {"name": "A", "tone": "direct", "detail": "short"})
    assert "straight to the point" in direct and "Keep replies short" in direct
    assert "Call them A" in direct


def test_a_name_alone_adds_no_voice_rule():
    # Wisp's own voice is the default. A person who only gave a name must not
    # silently get "keep replies short" or a second tone rule.
    block = P.profile_block(P.empty() | {"name": "Sam"})
    assert "Call them Sam" in block
    assert "How to talk to them" not in block
    assert not any("talk to you" in line or "replies" in line for line in P.preview(P.empty() | {"name": "Sam"}))


# --- use 2: keeping in mind ---------------------------------------------------------

def test_keeping_in_mind_is_silent_without_signals():
    profile = P.empty() | {"struggles": ["Overcommitting to too much"]}
    assert P.keeping_in_mind(profile, 0.0, events_today=2) == ""
    assert P.keeping_in_mind(P.empty(), 0.0, events_today=9) == ""


def test_keeping_in_mind_fires_on_evidence_only():
    profile = P.empty() | SAMPLE
    now = time.mktime((2026, 10, 17, 8, 0, 0, 0, 0, -1))
    text = P.keeping_in_mind(profile, now, events_today=6, overdue_reminders=2, mail_from_people=4,
                             people_in_events=["Sam Lee"], people_in_mail=["Mom"])
    assert text.startswith("**🧭 Keeping in mind**")
    assert "You're working toward: " in text
    assert "time with Sam Lee on your calendar and mail from Mom" in text
    assert "6 events today" in text and "Overcommitting to too much" in text
    assert "4 emails from people are waiting" in text and "Letting email pile up" in text
    # Procrastination was not among the struggles, so slipped reminders say nothing.
    assert "slipped" not in text
    # At most two struggle lines, one goal line, one people line.
    assert text.count("\n- ") == 4


def test_keeping_in_mind_clear_day_points_at_the_goal():
    profile = P.empty() | {"goals": ["Finish the thesis"], "struggles": ["Staying focused"]}
    text = P.keeping_in_mind(profile, 0.0, events_today=0)
    assert "block time for Finish the thesis" in text


def test_goal_rotates_by_day():
    profile = P.empty() | {"goals": ["A", "B", "C"]}
    seen = {P.goal_in_focus(profile, 86400.0 * day) for day in range(10)}
    assert seen == {"A", "B", "C"}


def test_people_mentioned_is_whole_word_first_name():
    profile = P.empty() | {"people": [{"name": "Sam Lee"}, {"name": "Mom"}, {"name": "Ann"}]}
    assert P.people_mentioned(profile, ["Lunch with Sam", "Samantha's recital"]) == ["Sam Lee"]
    assert P.people_mentioned(profile, ["Mom | Me: hi", "Planning"]) == ["Mom"]
    assert P.people_mentioned(profile, ["Annual review"]) == []
    assert P.people_mentioned(profile, []) == []


# --- use 3: Today order ----------------------------------------------------------

def test_prioritize_raises_items_naming_people_and_leaves_calendar_alone():
    profile = P.empty() | {"people": [{"name": "Mom", "relation": "mother", "note": ""}]}
    items = [
        {"source": "messages", "title": "Mom", "quote": "meet me at 6", "priority": 2, "reason": "Stated plan."},
        {"source": "calendar", "title": "Dinner with Mom", "quote": "", "priority": 2, "reason": "Scheduled."},
        {"source": "email", "title": "Invoice", "quote": "from accounts", "priority": 2, "reason": "x"},
    ]
    out = P.prioritize(items, profile)
    assert out[0]["priority"] == 1 and out[0]["reason"].startswith("From Mom, who you said matters to you. ")
    assert out[0]["profile_people"] == ["Mom"]
    assert out[1] == items[1], "calendar items are already on the plan"
    assert out[2] == items[2]
    assert items[0]["priority"] == 2, "inputs are not mutated"
    assert P.prioritize(items, P.empty()) is items


def test_workspace_assess_applies_the_profile():
    from service.assistant import daily_workspace as D
    day, zone, now = "2026-10-17", "UTC", 1_792_000_000.0
    lo, _ = D.day_bounds(day, zone)
    now = lo + 8 * 3600
    state = {"tasks": [], "commitments": [], "preferences": {},
             "sources": {"calendar": {"available": False}, "reminders": {"available": False}}}
    evidence = {"messages": {"ready": True, "records": [
        {"id": "m1", "title": "Mom", "quote": "Can you call me tonight?", "review_reason": "Direct ask."},
        {"id": "m2", "title": "Group chat", "quote": "anyone up for lunch", "review_reason": "Question."}]}}
    profile = P.empty() | {"people": [{"name": "Mom", "relation": "", "note": ""}]}
    plain = D.assess(day, zone, state, evidence, now=now)
    personal = D.assess(day, zone, state, evidence, now=now, profile=profile)
    assert [i["title"] for i in plain["attention"]][:2] == ["Mom", "Group chat"] or plain["attention"]
    first = personal["attention"][0]
    assert first["title"] == "Mom" and first["priority"] == 1
    assert first["reason"].startswith("From Mom, who you said matters to you.")


# --- preview ---------------------------------------------------------------------

def test_preview_names_real_behaviour():
    lines = P.preview(P.empty() | SAMPLE)
    joined = "\n".join(lines)
    assert "I'll call you Adi" in joined
    assert "When Mom and Sam Lee show up on your calendar or in your mail and texts, your morning brief points it out" in joined
    assert "Needs you" not in joined, "the notch's Needs you does not rank by the profile"
    assert "never changes what I alert you about" in joined
    assert "talk to you gently" in joined
    assert P.preview(P.empty())[-1].startswith("You haven't told me anything yet")


# --- API -----------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(profile_api.router)
    return TestClient(app)


def test_api_round_trip(client):
    empty = client.get("/profile").json()
    assert empty["status"]["offer"] is True and empty["profile"]["name"] == ""
    out = client.patch("/profile", json={"name": "Adi", "goals": ["Ship it"]}).json()
    assert out["profile"]["name"] == "Adi" and out["status"]["filled"] == ["name", "goals"]
    assert any("call you Adi" in line for line in out["preview"])
    assert client.patch("/profile", json={"tone": "rude"}).status_code == 422
    assert client.patch("/profile", json={"bogus": 1}).status_code == 422
    done = client.post("/profile/complete").json()
    assert done["status"]["completed"] is True and done["status"]["offer"] is False
    gone = client.delete("/profile").json()
    assert all(gone["profile"][f] == P.empty()[f] for f in P.EDITABLE)
    assert gone["status"]["has_content"] is False and gone["status"]["offer"] is False


def test_api_dismiss_counts(client):
    client.post("/profile/dismiss")
    out = client.get("/profile").json()
    assert out["profile"]["dismiss_count"] == 1 and out["status"]["offer"] is False


def test_api_says_whether_the_answers_stay_on_this_mac(client, monkeypatch):
    from service import wisp_about
    monkeypatch.setattr(wisp_about, "cloud_state", lambda: ("", ""))
    assert client.get("/profile").json()["status"]["local_only"] is True
    monkeypatch.setattr(wisp_about, "cloud_state", lambda: ("everything", "api.example.com"))
    assert client.get("/profile").json()["status"]["local_only"] is False
