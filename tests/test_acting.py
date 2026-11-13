"""Let Wisp act for me (service/proactive/acting.py): the one gate, the record of what Wisp
did, and the "Wisp did this" card with Undo. Also the worked example for a new kind of action."""
from __future__ import annotations

import asyncio

import pytest

from service import config
from service.proactive import acting, autoadd, executor
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0


@pytest.fixture
def setting(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    return saved


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def test_off_until_a_yes_and_the_yes_is_dated(setting):
    assert acting.state() == {"enabled": False, "enabled_at": None, "asked": False, "per_day": acting.MAX_PER_DAY,
                              "does": acting.DOES, "never": acting.NEVER}
    assert acting.DOES and "never sends" in acting.NEVER
    assert not acting.allows(NOW) and acting.still_ok() == "Acting for you was turned off"
    acting.set_enabled(True, now=NOW)
    assert setting["act_for_me"] == {"enabled": True, "enabled_at": NOW, "asked": True}
    assert acting.enabled() and acting.enabled_at() == NOW and acting.asked() and acting.still_ok() is None


def test_only_what_arrives_after_the_yes(setting):
    acting.set_enabled(True, now=NOW)
    assert acting.allows(NOW) and acting.allows(NOW + 60)
    assert not acting.allows(NOW - 1) and not acting.allows(None) and not acting.allows("later")
    # Off and on again moves the line forward: mail from the gap is not reached back for.
    acting.set_enabled(False, now=NOW + 100)
    assert not acting.allows(NOW + 200) and acting.enabled_at() is None and acting.asked()
    acting.set_enabled(True, now=NOW + 300)
    assert not acting.allows(NOW + 200) and acting.allows(NOW + 301)


def test_off_stops_a_write_that_was_about_to_happen_and_so_does_a_turn(setting, monkeypatch):
    acting.set_enabled(True, now=NOW)
    assert acting.still_ok() is None and autoadd._still_ok() is None
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert "turn" in acting.still_ok()
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    acting.set_enabled(False)
    assert acting.still_ok() == "Acting for you was turned off" == autoadd._still_ok()


def test_a_settings_file_that_cannot_be_read_means_off(setting, monkeypatch):
    acting.set_enabled(True, now=NOW)
    monkeypatch.setattr(config, "user_overlay_valid", lambda: False)
    assert not acting.enabled() and not acting.allows(NOW + 1) and acting.still_ok() is not None
    for bad in (None, "yes", {"enabled": True}, {"enabled": 1, "enabled_at": NOW}, {"enabled": True, "enabled_at": "x"}):
        monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
        monkeypatch.setattr(config, "models_config", lambda bad=bad: {"act_for_me": bad})
        assert not acting.enabled()
    with pytest.raises(ValueError):
        acting.set_enabled("yes")


def test_put_aside_is_not_asked_again_and_never_turns_it_on(setting):
    acting.mark_asked()
    assert acting.asked() and not acting.enabled() and setting["act_for_me"]["enabled"] is False


# --- a new kind of action, start to finish (the interface a second module uses) --------------

def _follow_up(store, now=NOW + 10, fp="fp-follow-1"):
    return acting.record(store, fp, kind="follow_up", status="added", now=now,
                         title="Answer Maya", quote="are you coming on Friday?", who="Maya Chen",
                         source="messages", item_id="text:maya", received_ts=NOW + 5, timezone="UTC",
                         card={"meta": "REMINDER", "title": "Set a reminder to answer Maya",
                               "did": "I'll remind you tomorrow at 9 AM."},
                         show_until=now + 86400, undo={"reminder_id": "R-77"})


def test_an_action_is_recorded_shown_on_one_card_and_undone_by_its_own_undoer(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW)
    assert acting.room(store, NOW + 10) == acting.MAX_PER_DAY
    _follow_up(store)
    assert acting.room(store, NOW + 20) == acting.MAX_PER_DAY - 1
    [card] = autoadd.done_cards(store, NOW + 20)
    assert card["action"]["method"] == "undo_add" and card["action"]["record"] == "fp-follow-1"
    assert card["title"] == "Set a reminder to answer Maya" and card["meta"] == "REMINDER"
    assert card["body"] == "“are you coming on Friday?” I'll remind you tomorrow at 9 AM."
    assert "R-77" not in str(card)                        # what Undo needs never leaves the store
    undone = []

    async def undo(record):
        undone.append(record["undo"]["reminder_id"])
        return {"ok": True, "status": "succeeded", "message": "Reminder removed"}
    monkeypatch.setattr(acting, "_undoers", {})
    assert asyncio.run(autoadd.undo(card, store, now=NOW + 30))["status"] == "blocked"   # no undoer yet
    acting.register_undoer("follow_up", undo)
    result = asyncio.run(autoadd.undo(card, store, now=NOW + 30))
    assert result == {"ok": True, "status": "succeeded", "message": "Reminder removed"} and undone == ["R-77"]
    assert store.auto_added()["fp-follow-1"]["status"] == "undone"
    assert autoadd.done_cards(store, NOW + 40) == []
    assert asyncio.run(autoadd.undo(card, store, now=NOW + 50))["status"] == "blocked"   # never twice
    assert acting.room(store, NOW + 60) == acting.MAX_PER_DAY - 1                         # an undone action still counts


def test_a_card_comes_only_from_the_record_and_leaves_when_its_time_passes(setting, store):
    _follow_up(store)
    assert autoadd.done_cards(store, NOW + 10 + 86400 + 1) == []           # past show_until
    forged = {"action": {"method": "undo_add", "record": "no-such-record"}}
    assert asyncio.run(autoadd.undo(forged, store))["status"] == "blocked"
    with pytest.raises(ValueError):
        acting.record(store, "fp-x", kind="send_email", status="added", now=NOW, title="x")
    with pytest.raises(ValueError):
        acting.register_undoer("send_email", None)


def test_the_daily_cap_is_shared_by_every_kind(setting, store):
    for i in range(acting.MAX_PER_DAY):
        _follow_up(store, now=NOW + i, fp=f"fp-{i}")
    assert acting.room(store, NOW + 100) == 0
    assert store.auto_added_since(NOW - 1) == acting.MAX_PER_DAY          # auto-add's own cap sees them too
    assert acting.room(store, NOW + acting.DAY_S + 100) == acting.MAX_PER_DAY


def test_recent_lists_words_only(setting, store):
    _follow_up(store)
    [row] = acting.recent(store, NOW + 20)
    assert row == {"kind": "follow_up", "title": "Set a reminder to answer Maya", "who": "Maya Chen",
                   "at": NOW + 10, "undone": False}


# --- the switch's routes ------------------------------------------------------------------------

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.proactive.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_switch_is_the_apps_own_and_takes_only_a_real_boolean(setting):
    client, url = _client(), "/assistant/proactive/act"
    assert client.get(url).json()["enabled"] is False
    assert client.put(url, json={"enabled": True}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put(url, json={"enabled": True}, headers={"Origin": "null"}).status_code == 403
    assert client.put(url, content='{"enabled": true}', headers={"Content-Type": "text/plain"}).status_code == 415
    for body in ({"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "enabled_at": 0}, {"enabled": None}):
        assert client.put(url, json=body).status_code == 422
    assert setting == {} and not acting.enabled()
    on = client.put(url, json={"enabled": True}).json()
    assert on["enabled"] is True and on["enabled_at"] > 1_700_000_000 and on["per_day"] == 10 and on["per_cycle"] == 3
    off = client.put(url, json={"enabled": False}).json()
    assert off["enabled"] is False and off["enabled_at"] is None and off["asked"] is True
    assert autoadd._still_ok() == "Acting for you was turned off"


def test_the_question_can_be_put_aside_from_the_app_only(setting):
    client, url = _client(), "/assistant/proactive/act/asked"
    assert client.post(url, json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    reply = client.post(url, json={}).json()
    assert reply["asked"] is True and reply["enabled"] is False


def test_a_settings_file_that_cannot_be_saved_keeps_everything_off_and_says_so(setting, monkeypatch):
    def refuse(_patch):
        raise OSError("unreadable")
    monkeypatch.setattr(config, "_save_overlay", refuse)
    reply = _client().put("/assistant/proactive/act", json={"enabled": True})
    assert reply.status_code == 409 and "does nothing on its own" in reply.json()["detail"]
    assert not acting.enabled()


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_acting.py"' in manifest
