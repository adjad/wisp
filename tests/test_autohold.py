"""Hold time for a deadline (service/proactive/autohold.py): under "Let Wisp act for me" the free
slot is pinned on Day without a tap, Wisp-local, one per deadline, with Undo. Fake holds and a
fake Day store; nothing reads a calendar or writes anywhere real."""
from __future__ import annotations

import asyncio

import pytest

from service import config
from service.proactive import acting, autoadd, autohold
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


def card(n=1, method="block_time"):
    start = NOW + 3600 * n
    return {"id": f"card:deadline{n}", "title": f"Worksheet {n} for STAT 5", "why": f"Worksheet {n} for STAT 5 is due tomorrow.",
            "account": "", "sources": [{"kind": "calendar", "ref": f"cal:{n}", "who": "", "quote": f"Worksheet {n}"}],
            "action": {"method": method, "button": "Block 2–3 PM?", "slot": {"start": start, "end": start + 3600},
                       "envelope": {"kind": "block_time", "title": f"Worksheet {n} for STAT 5", "start": start,
                                    "duration_min": 60, "day": "2027-01-15", "timezone": "UTC"}}}


class Holds:
    def __init__(self, outcome=None):
        self.calls, self.outcome = [], outcome

    def __call__(self, c):
        self.calls.append(c["id"])
        return dict(self.outcome) if self.outcome else {"ok": True, "status": "succeeded", "task_id": "task-" + c["id"]}


def run(store, cards, holds, abort=lambda: False, now=NOW):
    return autohold.step(cards, now=now, tz="UTC", store=store, abort=abort, hold=holds)


def test_nothing_is_held_until_the_person_says_yes(setting, store):
    holds = Holds()
    assert run(store, [card(1)], holds) == set() and holds.calls == [] and store.auto_added() == {}
    acting.set_enabled(True, now=NOW - 60)
    assert run(store, [card(1)], holds) == {"card:deadline1"} and holds.calls == ["card:deadline1"]


def test_a_hold_becomes_one_wisp_did_this_card_that_names_the_day_plan(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    run(store, [card(1)], Holds())
    [done] = autoadd.done_cards(store, NOW + 1)
    assert done["meta"] == "DAY PLAN" and done["title"] == "Held 2–3 PM for Worksheet 1 for STAT 5"
    assert "Day plan in Wisp, not on your calendar" in done["body"] and "due tomorrow" in done["body"]
    assert done["action"]["method"] == "undo_add" and "task-" not in str(done)
    assert autoadd.done_cards(store, NOW + 2 * 3600 + 1) == []            # gone once the held hour has passed


def test_one_hold_per_deadline_two_a_cycle_and_the_shared_daily_cap(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    cards = [card(1), card(2), card(3), card(4, method="reminder")]
    assert run(store, cards, holds) == {"card:deadline1", "card:deadline2"}          # MAX_PER_CYCLE
    assert run(store, cards, holds) == {"card:deadline3"}                            # never the first two again
    assert holds.calls == ["card:deadline1", "card:deadline2", "card:deadline3"]     # and never a non-hold card
    for i in range(acting.MAX_PER_DAY):
        acting.record(store, f"other-{i}", kind="reminder", status="added", now=NOW, title="x")
    assert run(store, [card(9)], holds) == set() and "card:deadline9" not in holds.calls


def test_off_or_a_turn_stops_it_before_the_hold(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    assert run(store, [card(1)], holds, abort=lambda: True) == set() and holds.calls == []
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert run(store, [card(1)], holds) == set() and holds.calls == []
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    acting.set_enabled(False)
    assert run(store, [card(1)], holds) == set() and holds.calls == []


def test_a_slot_that_went_or_is_already_planned_records_nothing_and_a_failure_is_tried_once_more(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    for outcome in ({"ok": False, "status": "blocked", "error": "not free"},
                    {"ok": True, "status": "succeeded", "task_id": "t", "already": True}):
        assert run(store, [card(1)], Holds(outcome)) == set() and store.auto_added() == {}
    failing = Holds({"ok": False, "status": "failed", "error": "not saved"})
    for _ in range(4):
        assert run(store, [card(2)], failing) == set()
    assert len(failing.calls) == autohold.MAX_ATTEMPTS and autoadd.done_cards(store, NOW + 1) == []
    assert acting.room(store, NOW) == acting.MAX_PER_DAY                      # a failed hold used none of the cap


class Day:
    def __init__(self, task):
        self.task, self.saves = task, []

    def today_task(self, task_id):
        return dict(self.task) if self.task and self.task["id"] == task_id else None

    def today_save_task(self, values, *, task_id, revision):
        assert revision == self.task["revision"]
        self.saves.append((task_id, values))
        self.task = {**self.task, **values, "revision": revision + 1}
        return self.task


def test_undo_removes_only_wisps_own_block_and_the_deadline_is_never_held_or_offered_again(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    run(store, [c], Holds())
    record = store.auto_added()[autohold.fingerprint(c)]
    start = c["action"]["slot"]["start"]
    mine = {"id": "task-card:deadline1", "status": "active", "pinned_start": start,
            "title": "Worksheet 1 for STAT 5", "revision": 3}
    day = Day(dict(mine))
    assert autohold._undo(record, day) == {"ok": True, "status": "succeeded", "message": "Removed the hold from your Day plan"}
    assert day.saves == [("task-card:deadline1", {"status": "dismissed"})]
    # A block the person moved or renamed is theirs.
    for changed in ({**mine, "pinned_start": start + 1800}, {**mine, "title": "Worksheet 1 (with Sam)"}):
        moved = Day(changed)
        assert autohold._undo(record, moved)["status"] == "blocked" and moved.saves == []
    assert autohold._undo(record, Day(None))["ok"] is True                      # already gone
    assert autohold._undo({"undo": {}}, day)["status"] == "blocked"
    # Through the card: the registered undoer, by the stored record.
    [done] = autoadd.done_cards(store, NOW + 1)

    async def fake(rec):
        return autohold._undo(rec, Day(dict(mine)))
    acting.register_undoer("hold", fake)
    try:
        assert asyncio.run(autoadd.undo(done, store, now=NOW + 2))["ok"] is True
    finally:
        acting.register_undoer("hold", autohold.undo)
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "undone"
    holds = Holds()
    assert run(store, [c], holds) == set() and holds.calls == []
    assert autohold.without_undone([c, card(2)], store) == [card(2)]


def test_the_undoer_is_found_after_a_restart_and_the_words_say_what_is_true():
    acting._undoers.pop("hold", None)
    assert acting.undoer("hold") is autohold.undo and acting.undoer("follow_up") is None
    assert any("Day plan" in line and "never on your calendar" in line for line in acting.DOES)


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_autohold.py"' in manifest
