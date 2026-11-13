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


SAVED = "+15551230001"                     # the handle of a saved contact, in a one-to-one chat
CHAT = "iMessage;-;" + SAVED


@pytest.fixture(autouse=True)
def saved_contacts(monkeypatch):
    """Contacts holds exactly one handle, SAVED. Texts in these tests come from that one-to-one
    chat unless a test says otherwise; nothing reads the real Contacts."""
    monkeypatch.setattr(autoadd, "_contact_name", lambda handle: "Saved Contact" if handle == SAVED else "")


def text(who, line, ago_h=1.0, unread=True, mine=(), chat=CHAT, service="iMessage"):
    records = [(NOW - ago_h * 3600, who, f"{who}: {line}", chat, unread, service)]
    records += [(NOW - ago_h * 3600 + 60 * (i + 1), who, f"Me: {m}", chat, None, service) for i, m in enumerate(mine)]
    return sources.text_items(records, now=NOW, own=ME)[0]


def mail(sender, address, subject, body, ago_h=2.0, unread=True, **extra):
    row = dict(ts=NOW - ago_h * 3600, account="iCloud", account_id="acct-1", sender=sender,
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
                                    existing=existing or (lambda n: []), is_enabled=lambda: on,
                                    allows=lambda arrived: True))


RECITAL = "can you come to my recital Sat Oct 31 at 6pm?"


@pytest.fixture
def mail_acts(monkeypatch):
    """Mail is added without a tap only with proof (autoadd.default_mail_proof: a From line is not
    proof of who sent it). Tests of how a MAIL item is planned, timed and written grant the proof."""
    monkeypatch.setattr(autoadd, "MAIL_ACTS", True)

    async def proven(item, plan, evidence):
        return True, ""
    # The proof itself (Google-fetched, known correspondent, authenticated) has its own tests
    # in tests/test_autoadd_mail_proof.py; here it is granted.
    monkeypatch.setattr(autoadd, "default_mail_proof", proven)


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


@pytest.mark.usefixtures("mail_acts")
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


@pytest.mark.usefixtures("mail_acts")
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


# --- tap cards (auto-add off) -------------------------------------------------------------


def _tap(store, item, **kw):
    return step(store, survivors(item), Writers(), on=False, **kw)["tap"]


def test_a_dismissed_tap_card_stays_gone_when_the_thread_hash_changes(store):
    item = text("Maya", RECITAL)
    [card] = _tap(store, item)
    store.save_state(cards=[card])
    store.dismiss_card(card, NOW)
    assert _tap(store, item) == []
    changed = {**item, "hash": "hash-after-more-lines"}
    assert changed["hash"] != item["hash"]
    assert _tap(store, changed) == []


def test_a_fired_tap_card_stays_gone_when_the_thread_hash_changes(store):
    item = text("Maya", RECITAL)
    [card] = _tap(store, item)
    store.save_state(cards=[card])

    async def added(c):
        return {"ok": True, "status": "succeeded"}

    result = asyncio.run(executor.fire(card["id"], store=store, now=NOW, methods={"calendar_event": added}))
    assert result["ok"] is True
    assert _tap(store, {**item, "hash": "hash-after-more-lines"}) == []


def test_firing_a_tap_card_leaves_the_reply_card_and_its_to_do_alone(store):
    from service.proactive.prepare import todo_id
    item = text("Maya", RECITAL)
    [card] = _tap(store, item)
    assert card["todo_id"] is None
    store.save_state(cards=[card])

    async def added(c):
        return {"ok": True, "status": "succeeded"}

    asyncio.run(executor.fire(card["id"], store=store, now=NOW, methods={"calendar_event": added}))
    assert todo_id(item["id"]) not in store.decisions()["done"]
    # The reply card prepare builds for this message carries the item's own fingerprint.
    assert f"{item['id']}:{item['hash']}" not in store.suppressed_fingerprints()


def test_a_tap_card_steps_aside_for_a_reply_card_that_already_offers_the_add():
    def card(cid, method, ref, event=None):
        return {"id": cid, "sources": [{"ref": ref}], "action": {"method": method, "event": event}}

    reply_with = card("r1", "text_reply", "t1", event={"start_ts": 1})
    reply_without = card("r2", "text_reply", "t2")
    tap_1, tap_2 = card("a1", "calendar_event", "t1"), card("a2", "calendar_event", "t2")
    out = autoadd.merge_cards([], [tap_1, tap_2], [reply_with, reply_without], set(), 5)
    ids = [c["id"] for c in out]
    assert "a1" not in ids and "a2" in ids and {"r1", "r2"} <= set(ids)


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


def test_it_is_off_by_default_and_only_a_dated_yes_in_the_gates_own_file_turns_it_on(tmp_path, monkeypatch):
    import service.config as config
    from service.proactive import acting
    path = tmp_path / acting.FILE
    monkeypatch.setattr(acting, "_path", lambda: path)
    assert config.proactive_auto_add_enabled() is False and autoadd.enabled() is False
    # Nothing in config.yaml counts: the old key, or the gate's own shape written there.
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    for value in ({"proactive_auto_add": True}, {"act_for_me": {"enabled": True, "enabled_at": 5.0}}):
        monkeypatch.setattr(config, "models_config", lambda value=value: value)
        assert config.proactive_auto_add_enabled() is False and autoadd.enabled() is False
    for text in ('{"enabled": true}', '{"enabled": "true", "enabled_at": 5.0}', '{"enabled": false, "enabled_at": 5.0}'):
        path.write_text(text)
        assert config.proactive_auto_add_enabled() is False and autoadd.enabled() is False
    path.write_text('{"enabled": true, "enabled_at": 5.0}')              # a yes that named no action
    assert config.proactive_auto_add_enabled() is False and autoadd.enabled() is False
    path.write_text('{"enabled": true, "enabled_at": 5.0, "covers": {"hold": 5.0}}')
    assert config.proactive_auto_add_enabled() is False and autoadd.enabled() is False
    path.write_text('{"enabled": true, "enabled_at": 5.0, "covers": {"add": 5.0}}')
    assert config.proactive_auto_add_enabled() is True and autoadd.enabled() is True


def test_a_fresh_install_gets_tap_cards_and_writes_nothing(store, tmp_path, monkeypatch):
    """No yes on file: the production setting (not an injected one) keeps every
    item a tap-to-add card."""
    from service.proactive import acting
    monkeypatch.setattr(acting, "_path", lambda: tmp_path / "fresh-install" / acting.FILE)
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
                                  writers=writers.table(), existing=lambda n: [], is_enabled=lambda: True,
                                  allows=lambda arrived: True)

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
                                  writers=writers.table(), existing=lambda n: [], is_enabled=lambda: True,
                                  allows=lambda arrived: True)

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
    unsaved = text("+14085550199", "Interview Fri Oct 30 at 3pm, see you then.", chat="SMS;-;+14085550199")
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


# --- only what arrives after the yes --------------------------------------------------------

def test_a_message_from_before_the_yes_stays_a_tap_card(store):
    writers = Writers()
    old = text("Maya Chen", RECITAL, ago_h=3.0)
    new = text("Sam Lee", "can you come to my recital Sun Nov 1 at 2pm?", ago_h=0.5)
    yes_at = NOW - 3600                                  # the person said yes an hour ago
    out = asyncio.run(autoadd.step(survivors(old, new), {"calendar": [], "reminders": []}, now=NOW, tz=TZ,
                                   store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: [], is_enabled=lambda: True,
                                   allows=lambda received: received >= yes_at))
    assert [plan["who"] for _, plan in writers.calls] == ["Sam Lee"]          # only the one that arrived after
    assert len(out["tap"]) == 1 and out["tap"][0]["action"]["method"] == "calendar_event"
    assert len(out["added"]) == 1


# --- the yes covers a LINE that arrived after it, through the production step (no seams) ---------

@pytest.fixture
def yes(tmp_path, monkeypatch):
    """The real gate with its own file in tmp_path: yes(at) says yes at that time."""
    from service import idle
    from service.proactive import acting
    path = tmp_path / acting.FILE
    monkeypatch.setattr(acting, "_path", lambda: path)
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(acting, "_born", lambda: 0.0)        # a made-up clock; test_acting covers the real one
    return lambda at: acting.set_enabled(True, now=at)


def conversation(who, *lines):
    """(hours ago, text) lines from `who`, oldest first, as one conversation item."""
    records = [(NOW - ago * 3600, who, f"{who}: {line}", CHAT, True, "iMessage") for ago, line in lines]
    return sources.text_items(records, now=NOW, own=ME)[0]


def production_step(store, items, writers):
    notes = []
    out = asyncio.run(autoadd.step(list(items), {"calendar": [], "reminders": []}, now=NOW, tz=TZ, store=store,
                                   abort=lambda: False, writers=writers.table(), existing=lambda n: [], debug=notes))
    return out, [n["auto_add"] for n in notes]


def test_an_old_text_is_not_added_because_a_newer_line_arrived_after_the_yes(store, yes):
    # 3 h ago Maya sends the date; 2 h ago the person says yes; 1 h ago Maya sends a thumbs up.
    item = conversation("Maya Chen", (3.0, RECITAL), (1.0, "👍 see you"))
    assert item["received_ts"] == NOW - 3600 and item["line_ts"] == [NOW - 3 * 3600, NOW - 3600]
    yes(NOW - 2 * 3600)
    writers = Writers()
    out, notes = production_step(store, survivors(item), writers)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]     # still offered, for a tap
    assert notes == ["arrived before the yes: tap to add"]


def test_a_yes_that_did_not_name_adding_adds_nothing_and_the_card_waits(store, yes, tmp_path):
    import json
    from service.proactive import acting
    item = conversation("Maya Chen", (3.0, "hey are you around this weekend"), (1.0, RECITAL))
    at = NOW - 2 * 3600
    (tmp_path / acting.FILE).write_text(json.dumps(
        {"enabled": True, "enabled_at": at, "asked": True, "covers": {"hold": at, "follow_up": at}}))
    writers = Writers()
    out, _notes = production_step(store, survivors(item), writers)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}
    (tmp_path / acting.FILE).write_text(json.dumps(
        {"enabled": True, "enabled_at": at, "asked": True, "covers": {"add": at}}))
    out, _notes = production_step(store, survivors(item), writers)
    assert len(writers.calls) == 1 and len(out["added"]) == 1               # the same message, once adding is agreed


def test_a_dated_line_that_itself_arrived_after_the_yes_is_added(store, yes):
    item = conversation("Maya Chen", (3.0, "hey are you around this weekend"), (1.0, RECITAL))
    yes(NOW - 2 * 3600)
    writers = Writers()
    out, _ = production_step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["calendar"] and len(out["added"]) == 1


def test_a_line_whose_arrival_time_is_not_known_is_never_added_without_a_tap(store, yes):
    item = conversation("Maya Chen", (1.0, RECITAL))
    yes(NOW - 2 * 3600)
    for broken in ({**item, "line_ts": []}, {k: v for k, v in item.items() if k != "line_ts"},
                   {**item, "line_ts": [None]}, {**item, "line_ts": ["soon"]}):
        writers = Writers()
        out, _ = production_step(store, survivors(broken), writers)
        assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}


@pytest.mark.usefixtures("mail_acts")
def test_an_old_threads_subject_is_not_added_because_a_reply_arrived_after_the_yes(store, yes):
    rows = [dict(ts=NOW - 3 * 3600, account="iCloud", account_id="acct-1", sender="Ed Park",
                 sender_address="ed@ucsc.edu", to=ME, subject="Project report due Friday Oct 30 at 5 PM",
                 message_id="m-1", body="Details attached.", unread=True),
            dict(ts=NOW - 3600, account="iCloud", account_id="acct-1", sender="Ed Park",
                 sender_address="ed@ucsc.edu", to=ME, subject="Re: Project report due Friday Oct 30 at 5 PM",
                 message_id="m-2", body="Thanks everyone.", unread=True)]
    items = sources.mail_items(rows, mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)
    assert len(items) == 1 and items[0]["subject_ts"] == NOW - 3 * 3600 and items[0]["received_ts"] == NOW - 3600
    yes(NOW - 2 * 3600)
    writers = Writers()
    out, _ = production_step(store, survivors(items[0]), writers)
    assert writers.calls == [] and out["added"] == []
    # The same subject on a thread that began after the yes is added.
    fresh = sources.mail_items([{**rows[0], "ts": NOW - 1800, "message_id": "m-9",
                                 "subject": "Lab report due Friday Oct 30 at 5 PM"}],
                               mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)
    writers = Writers()
    out, _ = production_step(store, survivors(fresh[0]), writers)
    assert [k for k, _ in writers.calls] == ["reminder"]


def test_a_caller_that_replaces_the_setting_must_also_say_which_arrivals_it_covers(store):
    writers = Writers()
    out = asyncio.run(autoadd.step(survivors(text("Maya", RECITAL)), {"calendar": [], "reminders": []}, now=NOW,
                                   tz=TZ, store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: [], is_enabled=lambda: True))      # no `allows` given
    assert writers.calls == [] and out["added"] == [] and len(out["tap"]) == 1


# --- a sender name dressed up as someone else's address ------------------------------------------

def _row(sender, sender_address="", subject="Recital", body="Can you come to my recital Sat Oct 31 at 6pm?", **extra):
    return dict(ts=NOW - 3600, account="iCloud", account_id="acct-1", sender=sender, sender_address=sender_address,
                to=ME, subject=subject, message_id="m-" + subject, body=body, unread=True) | extra


@pytest.mark.parametrize("sender, parsed, real", [
    ('"Maya <maya@friend.com>" <x@evil.com>', "", "x@evil.com"),          # Apple Mail: the raw header
    ("Maya <maya@friend.com> <x@evil.com>", "", "x@evil.com"),             # Gmail: quotes already dropped
    (f'"{ME}" <x@evil.com>', "", "x@evil.com"),                            # posing as the user
    (f"Me <{ME}> <x@evil.com>", "", "x@evil.com"),
    ("Maya <maya@friend.com> <x@evil.com>", "x@evil.com", "x@evil.com"),   # the reader's own parsed field agrees
    ("Maya Chen <maya@friend.com>", "", "maya@friend.com"),                # an ordinary sender is unchanged
    ("maya@friend.com", "", "maya@friend.com"),
    ("Maya Chen", "maya@friend.com", "maya@friend.com"),
    ("Maya <maya@friend.com> and friends", "", ""),                        # junk after the address: none is guessed
    ("see maya@friend.com for details", "", ""),                           # not a mailbox: no address at all
])
def test_the_real_address_is_the_last_one_never_one_inside_the_name(sender, parsed, real):
    assert sources._address({"sender": sender, "sender_address": parsed}) == real


@pytest.mark.usefixtures("mail_acts")
def test_a_stranger_posing_as_a_contact_or_as_the_user_gets_nothing_added(store, yes):
    yes(NOW - 2 * 3600)
    reached = 0
    for sender in ('"Maya <maya@ucsc.edu>" <x@evil.com>', "Maya <maya@ucsc.edu> <x@evil.com>",
                   f'"{ME}" <x@evil.com>', f"Me <{ME}> <x@evil.com>"):
        assert sources.odd_sender(_row(sender)) is True
        # Some of these are dropped outright by the mail filter (fine); any that become an item
        # must be shown truthfully and must add nothing.
        items = sources.mail_items([_row(sender)], mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)
        if not items:
            continue
        reached += 1
        [item] = items
        assert item["odd_sender"] is True and item["who"] == "x@evil.com", sender       # shown by its real address
        assert item["user_wrote"] is False and item["ref"]["to"] == "x@evil.com"        # not "from you", and a reply
        assert "maya@ucsc.edu" not in item["text"] and "You (the user)" not in item["text"]   # would go to the real sender
        plan, why = autoadd.plan_for(item, dict(OK), now=NOW, tz=TZ)
        assert plan is None and why == "sender name carries an address"
        writers = Writers()
        out, _ = production_step(store, survivors(item), writers)
        assert writers.calls == [] and out["added"] == [] and out["tap"] == []
    assert reached >= 2                                   # the Gmail-shaped forms do become items
    # The same message from the organisation's real address is still added.
    [real] = sources.mail_items([_row("Ed Park <ed@ucsc.edu>", subject="Recital 2")], mine={ME}, now=NOW, tz=TZ,
                                linked=set(), own=ME)
    assert real["odd_sender"] is False and real["who"] == "Ed Park"
    writers = Writers()
    production_step(store, survivors(real), writers)
    assert [k for k, _ in writers.calls] == ["calendar"]


def test_an_address_in_the_name_can_no_longer_pass_for_the_users_own_organisation():
    [posed] = sources.mail_items([_row("Dean <dean@ucsc.edu> <x@gmail.com>")], mine={ME}, now=NOW, tz=TZ,
                                 linked=set(), own=ME)
    assert autoadd._same_org(posed) is False and posed["ref"]["to"] == "x@gmail.com"


def test_an_email_without_proof_is_never_added_without_a_tap(store):
    item = mail("Ed Park", "ed@ucsc.edu", "CSE 20 homework", "Hi all, HW4 due Friday Oct 30 11:59 PM. Good luck!")
    writers, notes = Writers(), []
    out = asyncio.run(autoadd.step(survivors(item, text("Maya", RECITAL)), {"calendar": [], "reminders": []},
                                   now=NOW, tz=TZ, store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: [], is_enabled=lambda: True, allows=lambda arrived: True,
                                   debug=notes))
    assert [k for k, _ in writers.calls] == ["calendar"]                 # the text is added; the email is not
    assert [c["action"]["method"] for c in out["tap"]] == ["reminder"]   # the email stays a card to tap
    assert autoadd.MAIL_ACTS is False                                    # as shipped
    assert "mail: tap to add (mail does not act)" in [n["auto_add"] for n in notes]


# --- a text counts only from a one-to-one chat whose HANDLE is saved in Contacts ----------------

def _texts(context, lines, chats, mine=(), services=("iMessage",)):
    """One conversation labelled `context`; `chats` and `services` give each line's chat guid and
    delivery service in turn."""
    records = [(NOW - 3600 + i, context, f"{context}: {line}", chats[i % len(chats)], True,
                services[i % len(services)]) for i, line in enumerate(lines)]
    records += [(NOW - 3000 + i, context, f"Me: {m}", chats[0], None, services[0]) for i, m in enumerate(mine)]
    return sources.text_items(records, now=NOW, own=ME)[0]


DATED = "Meeting Fri Oct 30 at 3pm, can you come?"


@pytest.mark.parametrize("label, chats, why", [
    # A stranger with an SMS chat and an iMessage chat: the label gets a suffix that used to read as a name.
    ("+15559990000 (conversation 2)", ["SMS;-;+15559990000"], "one chat, a stranger's number"),
    ("+15559990000 (conversation 2)", ["SMS;-;+15559990000", "iMessage;-;+15559990000"], "two chats: no single handle"),
    ("Maya Chen", ["iMessage;-;+15559990000"], "a name on the label, a handle Contacts does not hold"),
    ("AMAZON", ["SMS;-;AMAZON"], "an alphanumeric sender id"),
    ("Chase", ["SMS;-;Chase"], "an alphanumeric sender id"),
    ("Nike", ["iMessage;-;urn:biz:b15ed773-9eed-11e7-baa2-7b88b04daa8e"], "a business chat"),
    ("Maya Chen", [""], "no chat is known"),
    ("Maya Chen", ["iMessage;+;chat123456"], "a group chat's guid"),
])
def test_a_text_from_anything_but_a_saved_contacts_own_handle_is_never_added(store, yes, monkeypatch, label, chats, why):
    yes(NOW - 2 * 3600)
    # Even if Contacts "knew" these odd handles, a handle that is not a phone number or address is not a person.
    monkeypatch.setattr(autoadd, "_contact_name",
                        lambda handle: "Saved Contact" if handle in (SAVED, "AMAZON", "Chase", "36726") or
                        handle.startswith("urn:") else "")
    item = _texts(label, [DATED], chats)
    plan, reason = autoadd.plan_for(item, dict(OK), now=NOW, tz=TZ)
    assert plan is None and reason == "not a saved contact: tap to add", why
    writers = Writers()
    out, _ = production_step(store, survivors(item), writers)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}, why


def test_having_answered_a_stranger_does_not_make_them_a_contact(store, yes):
    yes(NOW - 2 * 3600)
    item = _texts("+15559990000", [DATED], ["SMS;-;+15559990000"], mine=["who is this?"])
    assert item["user_wrote"] is True and item["handle"] == "+15559990000"
    writers = Writers()
    out, notes = production_step(store, survivors(item), writers)
    assert writers.calls == [] and out["added"] == [] and "not a saved contact: tap to add" in notes


def test_a_saved_contacts_one_to_one_text_is_added(store, yes):
    yes(NOW - 2 * 3600)
    item = _texts("Maya Chen", [DATED], [CHAT])
    assert item["handle"] == SAVED and item["group"] is False
    writers = Writers()
    out, _ = production_step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["calendar"] and len(out["added"]) == 1
    # A saved-looking NAME on a chat whose handle Contacts does not hold is still a stranger.
    other = _texts("Sam Lee", [DATED], ["iMessage;-;sam@friend.com"])
    assert autoadd.plan_for(other, dict(OK), now=NOW, tz=TZ) == (None, "not a saved contact: tap to add")


def test_a_short_code_never_becomes_an_item_at_all():
    records = [(NOW - 3600, "36726 (conversation 2)", f"36726 (conversation 2): {DATED}", "SMS;-;36726", True)]
    assert sources.text_items(records, now=NOW, own=ME) == []


def test_contacts_that_cannot_be_asked_mean_nobody_is_known(store, yes, monkeypatch):
    yes(NOW - 2 * 3600)

    def broken(handle):
        raise RuntimeError("Contacts not synced")
    monkeypatch.setattr(autoadd, "_contact_name", broken)
    writers = Writers()
    out, _ = production_step(store, survivors(_texts("Maya Chen", [DATED], [CHAT])), writers)
    assert writers.calls == [] and out["added"] == []


@pytest.mark.parametrize("handle, person", [
    ("+15551230001", True), ("+1 (555) 123-0001", True), ("5551230001", True), ("maya@friend.com", True),
    ("36726", False), ("AMAZON", False), ("urn:biz:abc", False), ("", False), ("Maya <maya@friend.com>", False),
    ("+1555", False), ("maya@friend.com and others", False),
])
def test_what_counts_as_a_persons_handle(handle, person):
    assert autoadd._person_handle(handle) is person


# --- only an iMessage is added without a tap; an SMS, or a text of unknown delivery, is a card ----

@pytest.mark.parametrize("chat, services, why", [
    ("SMS;-;" + SAVED, ("SMS",), "an SMS: its sender number can be forged"),
    ("any;-;" + SAVED, ("SMS",), "an SMS in a chat the system names any;-;"),
    ("any;-;" + SAVED, ("",), "delivery not known"),
    ("iMessage;-;" + SAVED, ("",), "the chat says iMessage but the message itself does not"),
    ("any;-;" + SAVED, ("RCS",), "RCS"),
    ("any;-;" + SAVED, ("iMessage", "SMS"), "a conversation with both: not all verified"),
])
def test_a_saved_contacts_number_over_sms_or_unknown_delivery_stays_a_card(store, yes, chat, services, why):
    yes(NOW - 2 * 3600)
    item = _texts("Maya Chen", ["hey", DATED], [chat], services=services)
    assert item["handle"] == SAVED, why                              # it IS the saved contact's number
    writers = Writers()
    out, notes = production_step(store, survivors(item), writers)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}, why
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"], why
    assert notes == ["not an iMessage: tap to add"], why


def test_an_imessage_in_a_chat_the_system_names_any_is_added(store, yes):
    yes(NOW - 2 * 3600)
    item = _texts("Maya Chen", [DATED], ["any;-;" + SAVED], services=("iMessage",))
    assert item["service"] == "iMessage" and item["handle"] == SAVED
    writers = Writers()
    production_step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["calendar"]


def test_a_line_whose_chat_is_not_known_leaves_the_conversation_without_a_handle():
    records = [(NOW - 3600, "Maya Chen", "Maya Chen: hey", CHAT, True, "iMessage"),
               (NOW - 1800, "Maya Chen", f"Maya Chen: {DATED}", "", True, "")]      # joined to no native record
    [item] = sources.text_items(records, now=NOW, own=ME)
    assert sources.text_origin(item) == ("", "")                         # the lines the plan reads are not all placed
    assert item["handle"] == SAVED                                       # the reply card's handle, on its own rule
    assert autoadd.plan_for(item, dict(OK), now=NOW, tz=TZ) == (None, "not a saved contact: tap to add")


def test_older_five_field_records_read_as_unknown_delivery():
    [item] = sources.text_items([(NOW - 3600, "Maya Chen", f"Maya Chen: {DATED}", CHAT, True)], now=NOW, own=ME)
    assert item["service"] == "" and item["handle"] == SAVED


# --- the Contacts match itself, on the real lookup (not the fixture's stand-in) --------------------

@pytest.fixture
def contacts(monkeypatch):
    """Real Contacts maps, filled the way a sync fills them, with nothing persisted."""
    from service.tools import imessage_tools as im
    monkeypatch.setattr(im.cache_store, "save", lambda *a, **k: None)
    monkeypatch.setattr(im, "_contacts", {})
    monkeypatch.setattr(im, "_name_handles", {})
    monkeypatch.setattr(im, "_contacts_privacy_current", True)
    monkeypatch.setattr(im, "_contacts_not_allowed", False)
    monkeypatch.setattr(autoadd, "_region_code", lambda: "")
    return lambda mapping: im.cache_contacts(mapping)


def test_a_london_contact_is_not_a_maine_stranger(contacts, monkeypatch):
    contacts({"+44 20 7946 0958": "Priya Shah", "(555) 123-0001": "Maya Chen", "sam@friend.com": "Sam Lee",
              "020 7946 0111": "Local Shop", "15551239999": "Dana Wu", "(361) 234-5678": "Tex Ranger",
              "0036 1 555 0000": "Budapest Office"})
    from service.tools import imessage_tools as im
    assert im.contact_name_for_handle("+12079460958") == "Priya Shah"          # the loose match labels do use
    us = lambda handle: autoadd.strict_contact_name(handle, region="1")        # noqa: E731
    assert us("+12079460958") == ""                                            # ...is not who may add
    assert us("+442079460958") == "Priya Shah"                                 # saved with its country code
    assert us("+15551230001") == "Maya Chen"                                   # saved without one: the user's country
    assert us("+445551230001") == ""                                           # the same digits in another country
    assert us("+15551239999") == "Dana Wu"                                     # saved with the code but no "+"
    assert us("+13612345678") == "Tex Ranger"
    assert us("+3612345678") == ""                                             # Hungary's +36 1 234 5678 is not Texas
    assert us("+3615550000") == "Budapest Office"                              # "00" is a country code too
    assert us("+12079460111") == "" and us("+442079460111") == ""              # a UK local number, in the US: no match
    assert autoadd.strict_contact_name("+442079460111", region="44") == "Local Shop"   # ...and at home in the UK
    assert us("5551230001") == ""                                              # a handle with no country code
    assert not autoadd.phones_match("+15551230001", "(555) 123-0001 x22", "1")  # an extension is another line
    # Italy keeps the leading 0 of a national number; elsewhere it is a trunk prefix that goes.
    assert autoadd.phones_match("+390612345678", "06 1234 5678", "39")
    assert not autoadd.phones_match("+39612345678", "06 1234 5678", "39")
    assert autoadd.phones_match("+442079460111", "020 7946 0111", "44")
    assert autoadd.phones_match("+14165550123", "(416) 555-0123", "1")           # Canada shares +1
    assert autoadd.phones_match("+14155550123", "1 (415) 555-0123", "1")
    assert us("Sam@Friend.com") == "Sam Lee" and us("sam@friend.co") == "" and us("") == ""
    # Region unknown: only a number saved WITH its country code can match.
    unknown = lambda handle: autoadd.strict_contact_name(handle, region="")    # noqa: E731
    assert unknown("+442079460958") == "Priya Shah" and unknown("+3615550000") == "Budapest Office"
    assert unknown("+15551230001") == "" and unknown("+15551239999") == "" and unknown("+13612345678") == ""


@pytest.mark.parametrize("env, code", [
    ({"WISP_REGION": "GB"}, "44"), ({"WISP_REGION": "us"}, "1"), ({"WISP_REGION": "ZZ", "LANG": "de_DE.UTF-8"}, ""),
    ({"WISP_REGION": "FI", "LANG": "en_US.UTF-8"}, "358"), ({"WISP_REGION": "CA"}, "1"), ({"WISP_REGION": "IT"}, "39"),
    ({"LANG": "en_US.UTF-8"}, "1"), ({"LC_ALL": "fr_FR"}, "33"), ({"LANG": "C"}, ""), ({}, ""),
    ({"WISP_REGION": "US; rm -rf"}, ""),
])
def test_the_region_comes_from_the_app_or_the_locale(monkeypatch, env, code):
    for name in ("WISP_REGION", "LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert autoadd._region_code() == code


def test_contacts_not_synced_or_not_allowed_hold_nobody(contacts, monkeypatch):
    contacts({"+15551230001": "Maya Chen"})
    from service.tools import imessage_tools as im
    assert autoadd.strict_contact_name("+15551230001") == "Maya Chen"
    monkeypatch.setattr(im, "_contacts_privacy_current", False)
    assert autoadd.strict_contact_name("+15551230001") == ""
    monkeypatch.setattr(im, "_contacts_privacy_current", True)
    monkeypatch.setattr(im, "_contacts_not_allowed", True)
    assert autoadd.strict_contact_name("+15551230001") == ""


def test_the_real_lookup_through_the_production_step(store, yes, contacts, monkeypatch):
    yes(NOW - 2 * 3600)
    contacts({"+44 20 7946 0958": "Priya Shah"})
    monkeypatch.setattr(autoadd, "_contact_name", autoadd.strict_contact_name)     # undo the stand-in
    stranger = _texts("Priya Shah", [DATED], ["any;-;+12079460958"])                # Maine, posing by label
    writers = Writers()
    out, _ = production_step(store, survivors(stranger), writers)
    assert writers.calls == [] and out["added"] == []
    friend = _texts("Priya Shah", [DATED], ["any;-;+442079460958"])
    production_step(store, survivors(friend), writers)
    assert [k for k, _ in writers.calls] == ["calendar"]


def test_the_users_own_address_is_never_a_correspondent(monkeypatch):
    from service.memory import identity
    monkeypatch.setattr(identity, "user_emails", lambda: ["me@icloud.com"])
    monkeypatch.setattr(autoadd, "_contact_name", lambda handle: "Me")
    mine = _texts("Notes to self", [DATED], ["iMessage;-;me@icloud.com"])
    assert sources.text_origin(mine) == ("me@icloud.com", "iMessage") and autoadd._saved_contact(mine) is False
    assert autoadd._saved_contact(_texts("Maya Chen", [DATED], [CHAT])) is True


# --- a write is recorded before it is made ----------------------------------------------------------

def test_a_cycle_cut_off_during_the_write_never_writes_that_item_again(store):
    item = text("Maya", RECITAL)
    seen = []

    class Hanging(Writers):
        async def calendar(self, plan):
            seen.append(dict(store.auto_added()[plan["fingerprint"]]))       # what is on record while it writes
            raise asyncio.CancelledError()                                   # the cycle is cancelled mid-write

    with pytest.raises(asyncio.CancelledError):
        step(store, survivors(item), Hanging())
    assert [r["status"] for r in seen] == ["dispatching"] and seen[0]["attempts"] == 1
    [left] = store.auto_added().values()
    assert left["status"] == "dispatching" and autoadd.done_cards(store, NOW) == []     # no card for a maybe
    # The next cycle: it may have landed, so it is never written again, and it counts toward the day.
    writers = Writers()
    out = step(store, survivors(item), writers, now=NOW + 600)
    assert writers.calls == [] and out["added"] == [] and out["tap"] == []
    [after] = store.auto_added().values()
    assert after["status"] == "unknown" and store.auto_added_since(NOW - 1) == 1
    assert autoadd.done_cards(store, NOW + 600) == []


def test_a_normal_write_leaves_no_dispatching_record_behind(store):
    writers = Writers()
    step(store, survivors(text("Maya", RECITAL)), writers)
    assert [r["status"] for r in store.auto_added().values()] == ["added"]
    blocked = Writers(outcome={"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"})
    other = ProactiveStore(store.base.parent / "blocked")
    step(other, survivors(text("Maya", RECITAL)), blocked)
    [left] = other.auto_added().values()
    assert left["status"] == "blocked" and left["attempts"] == 0                      # may be tried again
    step(other, survivors(text("Maya", RECITAL)), Writers(), now=NOW + 600)
    assert [r["status"] for r in other.auto_added().values()] == ["added"]


def test_an_old_sms_or_unplaced_line_does_not_spoil_a_later_imessage(store, yes):
    yes(NOW - 2 * 3600)
    lines = ["an old green one", "another", "hey", "are you around", DATED]            # the plan reads the last three
    services = ("SMS", "", "iMessage", "iMessage", "iMessage")
    chats = ["SMS;-;" + SAVED, "", CHAT, CHAT, CHAT]
    records = [(NOW - 3600 + i, "Maya Chen", f"Maya Chen: {line}", chats[i], True, services[i])
               for i, line in enumerate(lines)]
    [item] = sources.text_items(records, now=NOW, own=ME)
    assert item["service"] == "" and autoadd._plan_handle(item) == SAVED and autoadd._text_service(item) == "iMessage"
    writers = Writers()
    out, _ = production_step(store, survivors(item), writers)
    assert [k for k, _ in writers.calls] == ["calendar"] and len(out["added"]) == 1
    # One of the last three over SMS: a card, not an add.
    records[3] = (records[3][0], "Maya Chen", "Maya Chen: are you around", "SMS;-;" + SAVED, True, "SMS")
    [mixed] = sources.text_items(records, now=NOW, own=ME)
    writers = Writers()
    out, notes = production_step(ProactiveStore(store.base.parent / "mixed"), survivors(mixed), writers)
    assert writers.calls == [] and out["added"] == []
