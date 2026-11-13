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
def setting(monkeypatch, tmp_path):
    saved: dict = {}
    # The gate's own file (acting._path), in this test's folder: never ~/.moe.
    if hasattr(acting, "_path"):
        monkeypatch.setattr(acting, "_path", lambda: tmp_path / "home" / "act_for_me.json")
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    return saved


@pytest.fixture(autouse=True)
def native(monkeypatch):
    """The native id behind each card ref (never the real assistant store). Empty: unreadable."""
    ids: dict[str, str] = {}
    monkeypatch.setattr(autohold, "resolve_native", lambda ref: ids.get(ref))
    return ids


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
    return autohold.step(cards, now=now, tz="UTC", store=store, abort=abort, hold=holds, hours=lambda slot, tz: True)


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
    from service.proactive import followups
    acting._undoers.pop("follow_up", None)
    assert acting.undoer("hold") is autohold.undo and acting.undoer("follow_up") is followups.undo
    assert any("Day plan" in line and "never on your calendar" in line for line in acting.DOES)


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_autohold.py"' in manifest


def test_a_slot_in_quiet_hours_or_outside_working_hours_is_never_held(setting, store, monkeypatch):
    from datetime import datetime, timezone
    from service import user_hours
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    quiet, working = {"on": False}, {"on": True}
    monkeypatch.setattr(user_hours, "in_quiet_hours", lambda moment=None: quiet["on"])
    monkeypatch.setattr(user_hours, "in_working_hours", lambda moment=None: working["on"])
    go = lambda: autohold.step([card(1)], now=NOW, tz="UTC", store=store, abort=lambda: False, hold=holds)   # noqa: E731
    quiet["on"] = True
    assert go() == set() and holds.calls == [] and store.auto_added() == {}
    quiet["on"], working["on"] = False, False
    assert go() == set() and holds.calls == []
    # The END of the slot counts too: an hour that runs past the working day is not held.
    edge = card(1)["action"]["slot"]["end"] - 60
    monkeypatch.setattr(user_hours, "in_working_hours",
                        lambda moment=None: moment.timestamp() < edge)
    assert go() == set() and holds.calls == []

    def broken(moment=None):
        raise RuntimeError("hours unreadable")
    monkeypatch.setattr(user_hours, "in_working_hours", broken)
    assert go() == set() and holds.calls == []                       # unknown hours: no hold
    monkeypatch.setattr(user_hours, "in_working_hours", lambda moment=None: True)
    assert go() == {"card:deadline1"}
    assert autohold.inside_hours({"start": 5.0, "end": 5.0}, "UTC") is False and autohold.inside_hours({}, "UTC") is False
    assert isinstance(datetime.fromtimestamp(NOW, timezone.utc), datetime)


# --- review repairs -------------------------------------------------------------------------

def moved(c, ref, n=7):
    """The same deadline after a reschedule: a new commitment row id, a new time, a new card id."""
    start = NOW + 3600 * n
    return {**c, "id": c["id"] + "-moved", "sources": [{**c["sources"][0], "ref": ref}],
            "action": {**c["action"], "slot": {"start": start, "end": start + 3600},
                       "envelope": {**c["action"]["envelope"], "start": start}}}


def undo_through_the_store(store, c):
    fp = autohold.fingerprint(c)
    store.auto_add_put(fp, {**store.auto_added()[fp], "status": "undone"})


def test_a_rescheduled_deadline_is_never_held_again_after_undo(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({"cal:1": "evt-1", "cal:1-next": "evt-1", "cal:2": "evt-2", "cal:2-next": "evt-2"})
    c = card(1)
    assert run(store, [c], Holds()) == {"card:deadline1"}
    undo_through_the_store(store, c)
    later = moved(c, "cal:1-next")
    assert autohold.fingerprint(later) == autohold.fingerprint(c)          # the native id, not the row id
    holds = Holds()
    assert run(store, [later, card(2)], holds) == {"card:deadline2"} and holds.calls == ["card:deadline2"]
    assert autohold.without_undone([later, card(2)], store) == [card(2)]   # nor offered as a tap card
    # Held (not undone) and then rescheduled: still one hold per deadline.
    holds = Holds()
    assert run(store, [moved(card(2), "cal:2-next")], holds) == set() and holds.calls == []


def test_without_a_native_id_the_source_and_title_still_block_it(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    c = card(5)                                                             # no native id can be read
    run(store, [c], Holds())
    undo_through_the_store(store, c)
    later = moved(c, "cal:5-next")
    assert autohold.fingerprint(later) != autohold.fingerprint(c)
    holds = Holds()
    assert run(store, [later], holds) == set() and holds.calls == []
    assert autohold.without_undone([later], store) == []
    # Readable now, unreadable then: the title still matches. Two deadlines with their own
    # native ids never block each other by title.
    native["cal:5-third"] = "evt-5"
    assert autohold.without_undone([moved(c, "cal:5-third")], store) == []
    native.update({"cal:6": "evt-6", "cal:6b": "evt-6b"})
    twin = {**card(6), "sources": [{**card(6)["sources"][0], "quote": "Weekly reading"}]}
    run(store, [twin], Holds())
    undo_through_the_store(store, twin)
    other = {**moved(twin, "cal:6b"), "id": "card:other"}
    assert autohold.without_undone([other], store) == [other] and run(store, [other], Holds()) == {"card:other"}


class Seq:
    def __init__(self, *outcomes):
        self.calls, self.outcomes = [], list(outcomes)

    def __call__(self, c):
        self.calls.append(c["id"])
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return dict(out)


def test_a_save_whose_read_back_failed_is_claimed_on_the_retry(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    holds = Seq({"ok": False, "status": "failed", "error": "The Day task wasn't saved."},
                {"ok": True, "status": "succeeded", "task_id": "t-9", "already": True})
    assert run(store, [c], holds) == set()
    assert run(store, [c], holds) == {"card:deadline1"}
    record = store.auto_added()[autohold.fingerprint(c)]
    assert record["status"] == "added" and record["undo"]["task_id"] == "t-9" and record["attempts"] == 2
    assert len(autoadd.done_cards(store, NOW + 1)) == 1 and acting.room(store, NOW) == acting.MAX_PER_DAY - 1


def test_an_already_planned_block_with_no_attempt_of_wisps_is_left_alone(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    holds = Seq({"ok": True, "status": "succeeded", "task_id": "theirs", "already": True})
    assert run(store, [card(1)], holds) == set() and store.auto_added() == {}


def test_a_hold_that_raised_is_unknown_counted_and_never_retried(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    holds = Seq(RuntimeError("database is locked"))
    assert run(store, [c], holds) == set()
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "unknown"
    assert acting.room(store, NOW) == acting.MAX_PER_DAY - 1
    assert run(store, [c], holds) == set() and holds.calls == ["card:deadline1"]
    assert autoadd.done_cards(store, NOW + 1) == []


def test_the_write_is_checked_against_the_clock_at_that_moment(setting, store, monkeypatch):
    from service.proactive import deadlines
    acting.set_enabled(True, now=NOW - 60)
    seen = []

    def fake(c, *, now, store=None):
        seen.append(now)
        return {"ok": True, "status": "succeeded", "task_id": "t-1"}
    monkeypatch.setattr(deadlines, "hold", fake)
    held = autohold.step([card(1)], now=NOW, tz="UTC", store=store, abort=lambda: False,
                         hours=lambda slot, tz: True, clock=lambda: NOW + 900)
    assert held == {"card:deadline1"} and seen == [NOW + 900]
    assert store.auto_added()[autohold.fingerprint(card(1))]["at"] == NOW   # the cap still counts by the cycle


def test_undo_of_a_block_the_person_finished_says_so_and_leaves_it(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    run(store, [c], Holds())
    record = store.auto_added()[autohold.fingerprint(c)]
    finished = Day({"id": "task-card:deadline1", "status": "done", "pinned_start": c["action"]["slot"]["start"],
                    "title": "Worksheet 1 for STAT 5", "revision": 2})
    result = autohold._undo(record, finished)
    assert result == {"ok": True, "status": "succeeded", "message": "You already finished that one, so Wisp left it"}
    assert finished.saves == [] and finished.task["status"] == "done"
    gone = Day({"id": "task-card:deadline1", "status": "dismissed", "revision": 2})
    assert autohold._undo(record, gone)["message"] == "It was already off your Day plan" and gone.saves == []


class FakeDay:
    """Just enough of the assistant store for the real deadlines.hold: a Day plan in memory."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.read_back = True

    def today_snapshot(self, day, tz):
        from service.assistant.today import default_preferences
        return {"tasks": [dict(t) for t in self.tasks.values()], "commitments": [],
                "preferences": default_preferences(day, tz), "sources": {}}

    def today_save_task(self, values, *, task_id=None, revision=None):
        if task_id is None:
            task_id = f"t{len(self.tasks) + 1}"
            self.tasks[task_id] = {"id": task_id, "status": "active", "revision": 1, **values}
        else:
            assert revision == self.tasks[task_id]["revision"]
            self.tasks[task_id] = {**self.tasks[task_id], **values, "revision": revision + 1}
        return dict(self.tasks[task_id])

    def today_task(self, task_id):
        return dict(self.tasks[task_id]) if self.read_back and task_id in self.tasks else None


def test_the_real_hold_retry_and_already_paths_end_in_one_tracked_block_with_undo(setting, store):
    from service.proactive import deadlines
    acting.set_enabled(True, now=NOW - 60)
    day, c = FakeDay(), card(1)
    real = lambda x: deadlines.hold(x, now=NOW, store=day)                   # noqa: E731
    day.read_back = False                                                   # saved, but the read-back fails
    assert run(store, [c], real) == set() and store.auto_added()[autohold.fingerprint(c)]["status"] == "failed"
    day.read_back = True
    assert run(store, [c], real) == {"card:deadline1"} and len(day.tasks) == 1   # claimed, not saved twice
    record = store.auto_added()[autohold.fingerprint(c)]
    assert record["status"] == "added" and record["undo"]["task_id"] == "t1"
    assert autohold._undo(record, day)["message"] == "Removed the hold from your Day plan"
    assert day.tasks["t1"]["status"] == "dismissed"
    # A block the person pinned themselves at that time is "already" with no attempt of Wisp's: left alone.
    theirs = FakeDay()
    theirs.today_save_task({"title": "Worksheet 1 for STAT 5", "pinned_start": c["action"]["slot"]["start"],
                            "duration_minutes": 60, "day": "2027-01-15", "timezone": "UTC"})
    fresh = card(1)
    fresh["sources"] = [{**fresh["sources"][0], "ref": "cal:fresh", "quote": "Something else"}]
    assert run(store, [fresh], lambda x: deadlines.hold(x, now=NOW, store=theirs)) == set()
    assert autohold.fingerprint(fresh) not in store.auto_added()


# --- a repeating deadline, and a reminder Wisp itself added -------------------------------------

def _with_native(monkeypatch, mapping):
    monkeypatch.setattr(autohold, "resolve_native", lambda ref: mapping.get(ref))


def test_next_weeks_occurrence_of_a_repeating_deadline_is_held_but_a_snooze_is_not(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    _with_native(monkeypatch, {"cal:1": "EK-weekly", "cal:1b": "EK-weekly", "cal:1c": "EK-weekly"})
    holds = Holds()
    first = {**card(1), "due_ts": NOW + 86400}
    assert run(store, [first], holds) == {"card:deadline1"}
    # Snoozed by a day: a new row id, the same native id, a due date close by. Still the same deadline.
    snoozed = {**card(1), "id": "card:deadline1b", "due_ts": NOW + 2 * 86400,
               "sources": [{"kind": "calendar", "ref": "cal:1b", "who": "", "quote": "Worksheet 1"}]}
    assert run(store, [snoozed], holds) == set()
    # A week later: the next occurrence, held in its own right, once.
    later = {**card(1), "id": "card:deadline1c", "due_ts": NOW + 8 * 86400,
             "sources": [{"kind": "calendar", "ref": "cal:1c", "who": "", "quote": "Worksheet 1"}]}
    assert run(store, [later], holds, now=NOW + 7 * 86400) == {"card:deadline1c"}
    assert run(store, [later], holds, now=NOW + 7 * 86400) == set()
    assert holds.calls == ["card:deadline1", "card:deadline1c"]


def test_a_reminder_wisp_itself_auto_added_is_not_also_given_a_held_hour(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    acting.record(store, "autoadd:1", kind="reminder", status="added", now=NOW - 30, title="Submit the report",
                  source_id="R-9", reminders=[{"source_id": "R-9"}, {"source_id": "R-10"}])
    _with_native(monkeypatch, {"cal:1": "R-9", "cal:2": "R-10", "cal:3": "R-other"})
    holds = Holds()
    assert run(store, [card(1), card(2), card(3)], holds) == {"card:deadline3"}
    assert holds.calls == ["card:deadline3"]
