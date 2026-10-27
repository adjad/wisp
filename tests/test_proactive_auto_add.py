"""Proactive auto-add: a missed, time-sensitive event or deadline from a text or
email goes on Calendar or Reminders, then a "Wisp did this" card with Undo.

Fake EventKit/Reminders writers only: no native code, no live model, no
writes outside a scratch WISP_HOME.
"""
import asyncio
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-autoadd-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import autoadd, cycle, executor, sources
from service.proactive.store import ProactiveStore

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=ZONE).timestamp()        # Sat Oct 24, 10 AM
ME = "me@ucsc.edu"
ROOT = Path(__file__).resolve().parents[1]


def at(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE).timestamp()


def text(who, line, ago_h=1.0, unread=True, mine=()):
    records = [(NOW - ago_h * 3600, who, f"{who}: {line}", "", unread)]
    records += [(NOW - ago_h * 3600 + 60 * (i + 1), who, f"Me: {m}", "", None) for i, m in enumerate(mine)]
    return sources.text_items(records, now=NOW, own=ME)[0]


def mail(sender, address, subject, body, ago_h=2.0, unread=True, **extra):
    row = dict(ts=NOW - ago_h * 3600, account="iCloud", account_id="acct-1", sender=f"{sender} <{address}>",
               sender_address=address, to=ME, subject=subject, message_id="m-" + subject[:8], body=body,
               unread=unread) | extra
    items = sources.mail_items([row], mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)
    assert items, "fixture mail was filtered out"
    return items[0]


OK = {"junk": False, "sensitive": False, "title": "", "summary": "", "asks_of_me": "x"}


def survivors(*items):
    return [{"item": i, "result": dict(OK)} for i in items]


class Writers:
    """Fake EventKit + Reminders: records every write and answers verified."""

    def __init__(self, outcome=None):
        self.calls: list[tuple[str, dict]] = []
        self.outcome = outcome
        self.n = 0

    async def calendar(self, plan):
        return self._answer("calendar", plan)

    async def reminder(self, plan):
        return self._answer("reminder", plan)

    def _answer(self, kind, plan):
        self.calls.append((kind, dict(plan)))
        self.n += 1
        if self.outcome is not None:
            return dict(self.outcome)
        out = {"ok": True, "status": "succeeded", "source_id": f"EK-{self.n}"}
        if kind == "reminder":
            out["title"] = plan["title"]
        return out

    def table(self):
        return {"calendar": self.calendar, "reminder": self.reminder}


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def step(store, items, writers, *, sched=None, existing=None, on=True, abort=lambda: False, now=NOW):
    return asyncio.run(autoadd.step(list(items), sched or {"calendar": [], "reminders": []}, now=now, tz=TZ,
                                    store=store, abort=abort, writers=writers.table(),
                                    existing=existing or (lambda n: []), is_enabled=lambda: on))


RECITAL = "can you come to my recital Sat Oct 31 at 6pm?"


# --- the two examples ----------------------------------------------------------------------


def test_an_unanswered_text_invitation_is_added_to_calendar_with_a_done_card(store):
    writers = Writers()
    result = step(store, survivors(text("Maya", RECITAL)), writers)
    assert [k for k, _ in writers.calls] == ["calendar"]
    plan = writers.calls[0][1]
    assert plan["start_ts"] == at(10, 31, 18) and plan["duration_min"] == 60
    assert plan["title"] == "Maya's recital" and plan["quote"] == RECITAL
    assert result["added"] and not result["tap"]
    [card] = autoadd.done_cards(store, NOW)
    assert card["meta"] == "ADDED · CALENDAR"
    assert card["title"] == "Added Maya's recital to your calendar"
    assert "From Maya's text today: “can you come to my recital Sat Oct 31 at 6pm?”" in card["body"]
    assert card["body"].endswith("I added it for Sat, Oct 31, 6–7 PM.")
    assert card["action"]["method"] == "undo_add" and card["action"]["button"] == "Undo?"
    # The identifier Undo deletes lives in the store's record, never on the card.
    assert "EK-1" not in str(card)
    assert store.auto_added()[card["action"]["record"]]["source_id"] == "EK-1"


def test_an_email_deadline_becomes_a_reminder_at_its_stated_time(store):
    writers = Writers()
    item = mail("Ed Park", "ed@ucsc.edu", "CSE 20 homework", "Hi all, HW4 due Friday Oct 30 11:59 PM. Good luck!")
    step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["reminder"]
    plan = writers.calls[0][1]
    assert plan["due_ts"] == at(10, 30, 23, 59)
    assert "HW4" in plan["title"]
    [card] = autoadd.done_cards(store, NOW)
    assert card["meta"] == "ADDED · REMINDER"
    assert card["title"].startswith("Added ") and card["title"].endswith(" to your reminders")
    assert "From Ed's email today" in card["body"] and "Fri, Oct 30 at 11:59 PM" in card["body"]


def test_a_date_only_deadline_is_due_at_9_am_and_an_exam_with_a_clock_goes_to_calendar(store):
    writers = Writers()
    deadline = mail("Ana Ruiz", "ana@ucsc.edu", "Lab report", "Please submit the lab report by Nov 3.")
    exam = mail("Ed Park", "ed@ucsc.edu", "CSE 20: Exam 1 logistics",
                "Exam 1 is Wednesday Oct 28 at 10 AM. Bring a pencil.")
    step(store, survivors(deadline, exam), writers)
    got = {k: p for k, p in writers.calls}
    assert got["reminder"]["due_ts"] == at(11, 3, 9)
    assert got["calendar"]["start_ts"] == at(10, 28, 10) and got["calendar"]["title"].startswith("CSE 20")
    cards = {c["meta"]: c for c in autoadd.done_cards(store, NOW)}
    assert cards["ADDED · CALENDAR"]["body"].endswith("I added it for Wed, Oct 28, 10–11 AM.")
    assert "Tue, Nov 3 at 9 AM" in cards["ADDED · REMINDER"]["body"]


# --- what is never added ---------------------------------------------------------------------


@pytest.mark.parametrize("case", ["past", "promo", "duplicate", "duplicate_far", "withheld", "dateless",
                                  "group", "bare_clock", "proposal", "read_recently", "declined", "sensitive"])
def test_ineligible_items_add_nothing(store, case):
    item = text("Maya", RECITAL)
    sched, existing, result = None, None, dict(OK)
    if case == "past":
        item = mail("Ed Park", "ed@ucsc.edu", "Interview", "Your interview is Oct 22 at 3 PM.", ago_h=24 * 4)
    elif case == "promo":
        # Sources already drop bulk mail; auto-add refuses a promo on its own too.
        item = {**mail("Ed Park", "ed@ucsc.edu", "Fall party", "Our party is Oct 30 at 6pm."), "title": "Fall sale",
                "text": "Subject: Fall sale\nFrom Shop · Sat:\n50% off! Sale party is Oct 30 at 6pm. Unsubscribe."}
    elif case == "duplicate":
        sched = {"calendar": [{"id": "c1", "title": "Maya recital", "when_ts": at(10, 31, 18), "all_day": False,
                               "source": "calendar", "account": ME}], "reminders": []}
    elif case == "duplicate_far":
        existing = lambda n: [{"id": "c2", "title": "Recital!", "when_ts": at(10, 31, 17, 30), "all_day": False,
                               "source": "calendar"}]
    elif case == "withheld":
        item = {**item, "secret": True}
    elif case == "dateless":
        item = text("Maya", "can you come to my recital soon?")
    elif case == "group":
        item = {**item, "group": True}
    elif case == "bare_clock":
        item = text("Maya", "can you come to my recital at 6?")
    elif case == "proposal":
        item = text("Maya", "maybe we could do dinner Sat Oct 31 at 6pm?")
    elif case == "read_recently":
        item = text("Maya", RECITAL, unread=False)
    elif case == "declined":
        item = text("Maya", RECITAL, mine=["sorry, can't make it"])
    elif case == "sensitive":
        result = {**OK, "sensitive": True}
    writers = Writers()
    out = step(store, [{"item": item, "result": result}], writers, sched=sched, existing=existing)
    assert writers.calls == [] and not out["added"]
    assert autoadd.done_cards(store, NOW) == []


def test_an_answered_yes_still_qualifies_only_without_a_matching_event(store):
    item = text("Maya", RECITAL, mine=["yes! see you there"])
    writers = Writers()
    step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["calendar"]
    other = ProactiveStore(store.base.parent / "other")
    writers = Writers()
    sched = {"calendar": [{"id": "c1", "title": "Recital", "when_ts": at(10, 31, 18), "all_day": False,
                           "source": "calendar"}], "reminders": []}
    step(other, survivors(item), writers, sched=sched)
    assert writers.calls == []


def test_dates_resolve_against_the_message_arrival_not_today(store):
    # "Friday" in an email that arrived Mon Oct 19 meant Fri Oct 23: past now, never "next Friday".
    item = mail("Ed Park", "ed@ucsc.edu", "Essay", "The essay is due Friday at 5 PM.", ago_h=24 * 5)
    writers = Writers()
    step(store, survivors(item), writers)
    assert writers.calls == []


def test_a_turn_starting_stops_every_write(store):
    writers = Writers()
    step(store, survivors(text("Maya", RECITAL)), writers, abort=lambda: True)
    assert writers.calls == []


# --- limits ----------------------------------------------------------------------------------


def _many(n):
    out = []
    for i in range(n):
        out.append(text(f"Friend{chr(65 + i)}", f"can you come to my recital Sat Oct 31 at {i + 1}pm?",
                        ago_h=1 + i / 10))
    return out


def test_at_most_three_writes_a_cycle_and_the_rest_become_tap_cards(store):
    writers = Writers()
    out = step(store, survivors(*_many(5)), writers)
    assert len(writers.calls) == 3 and len(out["added"]) == 3
    assert len(out["tap"]) == 2 and {c["action"]["method"] for c in out["tap"]} == {"calendar_event"}


def test_at_most_ten_a_day(store):
    for i in range(9):
        store.auto_add_put(f"autoadd:old{i}", {"status": "added", "at": NOW - 3600, "kind": "calendar"})
    writers = Writers()
    out = step(store, survivors(*_many(3)), writers)
    assert len(writers.calls) == 1 and len(out["tap"]) == 2


def test_done_cards_lead_and_count_toward_the_five_card_cap(store):
    step(store, survivors(*_many(2)), Writers())
    done = autoadd.done_cards(store, NOW)
    alert = {"id": "card:alert", "critical": True, "sources": [{"ref": "mail:x"}], "action": {"method": "read"}}
    others = [{"id": f"card:{i}", "sources": [{"ref": f"messages:{i}"}], "action": {"method": "text_reply"}}
              for i in range(5)]
    merged = autoadd.merge_cards(done, [], [alert] + others, set(), 5)
    assert [c["id"] for c in merged[:3]] == [done[0]["id"], done[1]["id"], "card:alert"]
    assert len(merged) == 5


# --- undo --------------------------------------------------------------------------------------


def test_undo_deletes_only_the_created_identifier_and_blocks_a_re_add(store):
    writers = Writers()
    item = text("Maya", RECITAL)
    step(store, survivors(item), writers)
    [card] = autoadd.done_cards(store, NOW)
    store.save_state(cards=[card])
    deleted = []

    async def undo_calendar(record):
        deleted.append(record["source_id"])
        assert record["title"] == "Maya's recital" and record["start_ts"] == at(10, 31, 18)
        return {"ok": True, "status": "succeeded", "error": "", "is_absent": True}

    async def never(record):
        raise AssertionError("a reminder was touched")

    result = asyncio.run(executor.fire(card["id"], store=store, now=NOW,
                                       methods={"undo_calendar": undo_calendar, "undo_reminder": never}))
    assert result["ok"] is True and result["removed"] is True
    assert deleted == ["EK-1"]
    assert store.card(card["id"]) is None
    assert store.auto_added()[card["action"]["record"]]["status"] == "undone"
    again = Writers()
    step(store, survivors(item), again)
    assert again.calls == [] and autoadd.done_cards(store, NOW) == []


def test_undo_refuses_without_a_record_and_a_failed_undo_keeps_the_card(store):
    step(store, survivors(text("Maya", RECITAL)), Writers())
    [card] = autoadd.done_cards(store, NOW)
    store.save_state(cards=[card])

    async def refused(record):
        return {"ok": False, "status": "failed", "error": "That event changed since Wisp last read Calendar"}

    result = asyncio.run(executor.fire(card["id"], store=store, now=NOW, methods={"undo_calendar": refused}))
    assert result["ok"] is False and store.card(card["id"]) is not None
    assert store.auto_added()[card["action"]["record"]]["status"] == "added"
    forged = {**card, "id": "card:forged", "action": {**card["action"], "record": "autoadd:nothing"}}
    store.save_state(cards=[forged])
    result = asyncio.run(executor.fire("card:forged", store=store, now=NOW, methods={"undo_calendar": refused}))
    assert result["ok"] is False and result["status"] == "blocked"


def test_dismiss_keeps_the_event_and_the_card_stays_gone(store):
    writers = Writers()
    item = text("Maya", RECITAL)
    step(store, survivors(item), writers)
    [card] = autoadd.done_cards(store, NOW)
    store.save_state(cards=[card])
    store.dismiss_card(card, NOW)
    assert autoadd.done_cards(store, NOW) == []
    assert store.auto_added()[card["action"]["record"]]["status"] == "added"
    step(store, survivors(item), writers)
    assert len(writers.calls) == 1


# --- setting, failures ------------------------------------------------------------------------


def test_with_the_flag_off_nothing_is_written_and_tap_cards_stand_in(store):
    writers = Writers()
    out = step(store, survivors(text("Maya", RECITAL),
                                mail("Ed Park", "ed@ucsc.edu", "CSE 20 homework", "HW4 due Friday Oct 30 11:59 PM.")),
               writers, on=False)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}
    methods = sorted(c["action"]["method"] for c in out["tap"])
    assert methods == ["calendar_event", "reminder"]
    event = next(c for c in out["tap"] if c["action"]["method"] == "calendar_event")["action"]["event"]
    assert event["start_ts"] == at(10, 31, 18) and event["duration_min"] == 60


def test_the_config_flag_defaults_off_and_only_true_turns_it_on(monkeypatch):
    import service.config as config
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: {})
    assert config.proactive_auto_add_enabled() is False
    for value in (None, "true", 1, "yes"):
        monkeypatch.setattr(config, "models_config", lambda value=value: {"proactive_auto_add": value})
        assert config.proactive_auto_add_enabled() is False
    monkeypatch.setattr(config, "models_config", lambda: {"proactive_auto_add": False})
    assert config.proactive_auto_add_enabled() is False
    monkeypatch.setattr(config, "models_config", lambda: {"proactive_auto_add": True})
    assert config.proactive_auto_add_enabled() is True
    monkeypatch.setattr(config, "user_overlay_valid", lambda: False)
    assert config.proactive_auto_add_enabled() is False


def test_a_fresh_install_gets_tap_cards_and_writes_nothing(store, monkeypatch):
    """No `proactive_auto_add` in config: the production setting (not an
    injected one) keeps every item a tap-to-add card."""
    import service.config as config
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: {})
    writers = Writers()
    out = asyncio.run(autoadd.step(survivors(text("Maya", RECITAL)), {"calendar": [], "reminders": []},
                                   now=NOW, tz=TZ, store=store, abort=lambda: False,
                                   writers=writers.table(), existing=lambda n: []))
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]


@pytest.mark.parametrize("outcome", [
    {"ok": False, "status": "failed", "error": "Calendar access denied"},
    {"ok": True, "status": "succeeded"},                      # no identifier read back: not verified
    {"ok": False, "status": "unknown", "error": "Native result was not confirmed"},
])
def test_a_failed_or_unverified_write_makes_no_done_card(store, outcome):
    writers = Writers(outcome)
    step(store, survivors(text("Maya", RECITAL)), writers)
    assert len(writers.calls) == 1 and autoadd.done_cards(store, NOW) == []
    if outcome.get("status") == "unknown":
        # It may have landed: never written a second time.
        step(store, survivors(text("Maya", RECITAL)), writers)
        assert len(writers.calls) == 1


def test_a_blocked_write_is_retried_later_but_a_failed_one_only_once_more(store):
    writers = Writers({"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"})
    item = text("Maya", RECITAL)
    step(store, survivors(item), writers)
    step(store, survivors(item), writers)
    assert len(writers.calls) == 2
    writers.outcome = {"ok": False, "status": "failed", "error": "denied"}
    for _ in range(4):
        step(store, survivors(item), writers)
    assert len(writers.calls) == 2 + autoadd.MAX_ATTEMPTS


# --- the cycle ------------------------------------------------------------------------------------


async def _quiet_model(system, material, abort, tokens):
    return ""                                # every check fails closed; rules stand in


def test_the_background_cycle_adds_and_leads_with_the_done_card(store):
    writers = Writers()

    async def auto(survivors_, sched, *, now, tz, store, abort):
        return await autoadd.step(survivors_, sched, now=now, tz=tz, store=store, abort=abort,
                                  writers=writers.table(), existing=lambda n: [], is_enabled=lambda: True)

    item = text("Maya", RECITAL)
    result = asyncio.run(cycle.run_cycle(
        now=NOW, tz=TZ, force=True, reply_fn=_quiet_model, abort=lambda: False, collect=lambda n, z: [item],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked=set(), own=ME, allowed=set(), freeze=lambda ref: None, autoadd_fn=auto))
    assert result["status"] == "done"
    assert [k for k, _ in writers.calls] == ["calendar"]
    cards = store.state()["cards"]
    assert cards[0]["meta"] == "ADDED · CALENDAR" and cards[0]["action"]["method"] == "undo_add"
    assert not any(c["action"]["method"] in ("calendar_event", "reminder") for c in cards[1:])
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: {"calendar": [], "reminders": []},
                          own=ME, linked=set())
    assert view["cards"][0]["title"] == "Added Maya's recital to your calendar"
    # A second cycle neither writes again nor loses the card.
    asyncio.run(cycle.run_cycle(
        now=NOW + 600, tz=TZ, force=True, reply_fn=_quiet_model, abort=lambda: False, collect=lambda n, z: [item],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked=set(), own=ME, allowed=set(), freeze=lambda ref: None, autoadd_fn=auto))
    assert len(writers.calls) == 1 and store.state()["cards"][0]["meta"] == "ADDED · CALENDAR"


def test_a_cycle_that_yields_to_a_turn_writes_nothing(store):
    writers = Writers()
    busy = {"on": False}

    async def model(system, material, abort, tokens):
        busy["on"] = True
        return ""

    async def auto(survivors_, sched, *, now, tz, store, abort):
        return await autoadd.step(survivors_, sched, now=now, tz=tz, store=store, abort=abort,
                                  writers=writers.table(), existing=lambda n: [], is_enabled=lambda: True)

    asyncio.run(cycle.run_cycle(
        now=NOW, tz=TZ, force=True, reply_fn=model, abort=lambda: busy["on"], collect=lambda n, z: [text("Maya", RECITAL)],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked=set(), own=ME, allowed=set(), freeze=lambda ref: None, autoadd_fn=auto))
    assert writers.calls == []


# --- where writes can happen ---------------------------------------------------------------------------


_WRITE = re.compile(r"outbox|create_calendar|\"create_reminder\"|\"cancel_calendar_event\"|\"delete_reminder\"|"
                    r"send_message|send_reviewed_reply|create_calendar_event")


def test_prepare_still_has_no_send_or_write_capability():
    source = (ROOT / "service/proactive/prepare.py").read_text()
    assert not _WRITE.search(source)
    writers = sorted(p.name for p in (ROOT / "service/proactive").glob("*.py") if _WRITE.search(p.read_text()))
    assert writers == ["autoadd.py", "executor.py"]


def test_the_new_test_file_is_in_the_simulation_manifest():
    manifest = (ROOT / "scripts/run_simulation_qa.py").read_text()
    assert '"tests/test_proactive_auto_add.py"' in manifest


# --- release audit follow-ups ---------------------------------------------------------------


def test_a_stranger_never_writes_to_the_calendar_but_still_gets_a_tap_card(store):
    cold = mail("Recruiter", "talent@gmail.com", "Interview", "Interview Fri Oct 30 at 3pm, see you then.")
    unsaved = text("+14085550199", "Interview Fri Oct 30 at 3pm, see you then.")
    writers = Writers()
    step(store, survivors(cold, unsaved), writers)
    assert writers.calls == []


def test_an_undone_thread_is_never_added_again_even_at_a_nudged_time(store):
    writers = Writers()
    step(store, survivors(text("Maya", RECITAL)), writers)
    [record_id] = list(store.auto_added())
    store.auto_add_put(record_id, {**store.auto_added()[record_id], "status": "undone"})
    again = Writers()
    step(store, survivors(text("Maya", "can you come to my recital Sat Oct 31 at 6:15pm?")), again)
    assert again.calls == []


def test_unconfirmed_writes_count_toward_the_daily_cap(store, monkeypatch):
    monkeypatch.setattr(store, "auto_added_since", lambda since: autoadd.MAX_PER_DAY - 1)
    writers = Writers(outcome={"ok": False, "status": "unknown"})
    step(store, survivors(text("Maya", RECITAL), text("Sam", "dinner Sun Nov 1 at 7pm?"),
                          text("Ava", "game Mon Nov 2 at 5pm?")), writers)
    assert len(writers.calls) == 1


def test_the_card_fire_route_is_denied_from_the_shell():
    from service.safety import policy
    assert policy.local_send_endpoint_hit(
        "curl -X POST http://127.0.0.1:8765/assistant/proactive/cards/card:abc/fire -d '{}'")


def test_a_clocked_deadline_is_reminded_the_morning_of_and_three_hours_before(store, monkeypatch):
    item = mail("Ed Park", "ed@ucsc.edu", "CSE 20 homework", "Hi all, HW4 due Friday Oct 30 11:59 PM. Good luck!")
    plan, _ = autoadd.plan_for(item, dict(OK), now=NOW, tz=TZ)
    assert plan["alerts"] == [at(10, 30, 9), at(10, 30, 20, 59)]
    sent, deleted = [], []

    async def fake_request(kind, payload, **_):
        if kind == "create_reminder":
            sent.append(payload["due_ts"])
            return {"ok": True, "status": "succeeded", "source_id": f"R-{len(sent)}", "title": payload["title"]}
        deleted.append((payload["source_id"], payload["expected_due_ts"]))
        return {"ok": True, "status": "succeeded"}

    import service.assistant.outbox as outbox
    monkeypatch.setattr(outbox, "request", fake_request)
    monkeypatch.setattr(autoadd, "_app_connected_now", lambda: True)
    monkeypatch.setattr(autoadd, "_reminder_problem", lambda: None, raising=False)
    from service.proactive import executor
    monkeypatch.setattr(executor, "_reminder_problem", lambda: None)
    result = asyncio.run(autoadd.create_reminder(plan))
    assert result["ok"] and sent == plan["alerts"] and len(result["reminders"]) == 2
    record = {**plan, "source_id": result["source_id"], "reminders": result["reminders"], "status": "added",
              "received_ts": item["received_ts"], "timezone": TZ}
    card = autoadd.done_card(record, NOW)
    assert "I'll remind you Fri, Oct 30 at 9 AM and 8:59 PM." in card["body"]
    assert asyncio.run(autoadd.delete_reminder(record))["ok"]
    assert deleted == [("R-1", at(10, 30, 9)), ("R-2", at(10, 30, 20, 59))]
