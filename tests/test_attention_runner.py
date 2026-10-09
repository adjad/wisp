"""Settings, the reminder planner, the ledger, and one pass of the live runner.

Every effect is a fake. Nothing here touches Reminders, the hub, the assistant store, or
the real ~/.moe: the ledger lives in a temp dir.
"""
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service.attention import ledger as ledger_mod, live, runner
from service.attention.corpus import Item
from service.attention.live import Settings
from service.attention.resolve import Resolved

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
MON_9AM = datetime(2026, 9, 21, 9, 0, tzinfo=ZONE).timestamp()
HOUR = 3600.0


def rec(guid, conv, text, ts, direction="incoming", sender=None):
    return {"guid": guid, "conversation": conv, "direction": direction, "timestamp": ts, "text": text,
            "sender": sender or ("me" if direction == "outgoing" else conv)}


def commit(title, ts, status="active"):
    return {"id": "c-" + title, "title": title, "when_ts": ts, "status": status, "location": None,
            "source": "calendar", "kind": "event"}


class Fakes:
    def __init__(self, result=None, raises=None, fresh=None):
        self.created, self.published = [], []
        self.result = result or {"ok": True, "status": "succeeded"}
        self.raises = raises
        self.fresh = fresh if fresh is not None else []

    async def create(self, plan):
        self.created.append(plan)
        if self.raises:
            raise self.raises
        return self.result

    async def publish(self, event):
        self.published.append(event)

    def fresh_commitments(self):
        return self.fresh


@pytest.fixture
def ledger(tmp_path):
    led = ledger_mod.Ledger(tmp_path / "attention" / "ledger.db")
    led.set_meta("enabled_at", repr(MON_9AM - 86400))        # enabled yesterday
    return led


async def run(records, now, ledger, fakes, commitments=(), **settings):
    s = Settings(mode=settings.pop("mode", "live"), **settings)
    return await runner.process(records=records, commitments=list(commitments), now=now, tz=TZ,
                                settings=s, ledger=ledger, create=fakes.create, publish=fakes.publish,
                                fresh_commitments=fakes.fresh_commitments)


def mom_thread(text="Meet me in the Quad at 6PM", at=MON_9AM):
    return [rec("h", "Mom", "ok love you", at - 3600, "outgoing"), rec("m", "Mom", text, at)]


# ------------------------------------------------------------------ settings

def test_settings_default_to_shadow_and_a_corrupt_file_means_off(tmp_path):
    path = live.settings_path(tmp_path)
    assert live.load_settings(path).mode == "shadow"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert live.load_settings(path).mode == "off"
    path.write_text('{"mode": "live", "daily_cap": 99999}')
    assert live.load_settings(path).mode == "off"             # out of range: unreadable choice
    path.write_text('{"mode": "banana"}')
    assert live.load_settings(path).mode == "off"


def test_settings_roundtrip_is_private_and_validated(tmp_path):
    path = live.settings_path(tmp_path)
    live.save_settings(path, Settings(mode="live", daily_cap=2, lead_minutes=15))
    assert live.load_settings(path) == Settings(mode="live", daily_cap=2, lead_minutes=15)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(ValueError):
        live.updated(Settings(), daily_cap=-1)
    with pytest.raises(ValueError):
        live.updated(Settings(), mode="loud")
    with pytest.raises(ValueError):
        live.updated(Settings(), lead_minutes=True)            # bool is not a whole number


def test_quiet_hours_wrap_midnight():
    s = Settings(quiet_start=22, quiet_end=8)
    at = lambda h: datetime(2026, 9, 21, h, 0, tzinfo=ZONE)
    assert [live.in_quiet_hours(at(h), s) for h in (7, 8, 12, 21, 22, 23, 0)] == \
        [True, False, False, False, True, True, True]
    assert not live.in_quiet_hours(at(3), Settings(quiet_start=5, quiet_end=5))


# ------------------------------------------------------------------- planner

def item(sender="Mom", text="Meet me in the Quad at 6PM"):
    return Item("msg:m", "messages", MON_9AM, "incoming", sender, sender, text)


def timed(hour, minute=0):
    start = datetime(2026, 9, 21, hour, minute, tzinfo=ZONE)
    return Resolved(start, start.date(), True, "q")


def test_a_timed_event_gets_a_reminder_ahead_of_it_that_names_the_time():
    plan = live.plan_reminder(item(), timed(18), MON_9AM, Settings(lead_minutes=30), TZ)
    assert plan.title == "Mom: Meet me in the Quad at 6PM (6:00 PM)"
    assert plan.due_ts == datetime(2026, 9, 21, 17, 30, tzinfo=ZONE).timestamp()
    assert plan.event_ts == datetime(2026, 9, 21, 18, 0, tzinfo=ZONE).timestamp()
    assert plan.when_label == "today at 6:00 PM"


def test_an_event_closer_than_the_lead_is_remembered_at_once_not_in_the_past():
    soon = timed(9, 20)
    plan = live.plan_reminder(item(), soon, MON_9AM, Settings(lead_minutes=30), TZ)
    assert plan.due_ts == MON_9AM + live.MIN_LEAD_S and plan.due_ts <= plan.event_ts


def test_an_event_that_is_already_over_has_no_plan():
    assert live.plan_reminder(item(), timed(8, 55), MON_9AM, Settings(), TZ) is None


def test_a_date_without_a_clock_is_reminded_in_the_morning():
    r = Resolved(None, datetime(2026, 9, 22, tzinfo=ZONE).date(), False, "q")
    plan = live.plan_reminder(item(text="Exam is tomorrow"), r, MON_9AM, Settings(default_hour=9), TZ)
    assert plan.due_ts == datetime(2026, 9, 22, 9, 0, tzinfo=ZONE).timestamp()
    assert plan.event_ts is None and plan.when_label == "tomorrow"
    today = Resolved(None, datetime(2026, 9, 21, tzinfo=ZONE).date(), False, "q")
    late = datetime(2026, 9, 21, 14, 0, tzinfo=ZONE).timestamp()
    assert live.plan_reminder(item(), today, late, Settings(), TZ).due_ts == late + live.MIN_LEAD_S


def test_phone_numbers_are_not_used_as_names_and_long_titles_are_clipped():
    assert live.plan_reminder(item(sender="+16505550123"), timed(18), MON_9AM, Settings(), TZ) \
        .title.startswith("Text: ")
    long = live.plan_reminder(item(text="x " * 200), timed(18), MON_9AM, Settings(), TZ)
    assert len(long.title) <= live.TITLE_MAX and "…" in long.title


# -------------------------------------------------------------------- ledger

def test_a_claim_succeeds_exactly_once(ledger):
    assert ledger.claim("msg:a", title="t", now=1.0)
    assert not ledger.claim("msg:a", title="again", now=2.0)
    assert ledger.get("msg:a")["title"] == "t"


def test_abandoned_claims_become_unknown_and_are_never_retried(ledger):
    ledger.claim("msg:a", now=100.0)
    assert ledger.recover(now=100.0 + 10) == 0                # too fresh to judge
    assert ledger.recover(now=100.0 + ledger_mod.STALE_CLAIM_S + 1) == 1
    row = ledger.get("msg:a")
    assert row["state"] == "unknown" and row["detail"]["recovered"] == 1
    assert not ledger.claim("msg:a")                          # still decided


def test_finish_rejects_unknown_states_and_known_handles_many_ids(ledger):
    ledger.claim("msg:a")
    with pytest.raises(ValueError):
        ledger.finish("msg:a", "banana")
    for i in range(700):
        ledger.claim(f"msg:{i}")
    ids = [f"msg:{i}" for i in range(700)] + ["msg:never"]
    assert len(ledger.known(ids)) == 700


def test_the_ledger_file_is_private(tmp_path):
    ledger_mod.Ledger(tmp_path / "x" / "ledger.db")
    assert stat.S_IMODE((tmp_path / "x" / "ledger.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "x").stat().st_mode) == 0o700


# -------------------------------------------------------------------- runner

async def test_off_does_nothing_at_all(ledger):
    f = Fakes()
    assert await run(mom_thread(), MON_9AM + 60, ledger, f, mode="off") == []
    assert f.created == [] and ledger.recent() == [] and ledger.meta("enabled_at") is not None


async def test_shadow_records_what_it_would_do_and_does_nothing(ledger):
    f = Fakes()
    out = await run(mom_thread(), MON_9AM + 60, ledger, f, mode="shadow")
    assert [o.state for o in out] == ["shadow"]
    assert f.created == [] and f.published == []
    assert ledger.get("msg:m")["state"] == "shadow"


async def test_live_creates_one_reminder_and_alerts_once(ledger):
    f = Fakes()
    out = await run(mom_thread(), MON_9AM + 60, ledger, f)
    assert [o.state for o in out] == ["created"]
    assert len(f.created) == 1 and f.created[0].title.startswith("Mom: Meet me in the Quad")
    assert f.created[0].due_ts == datetime(2026, 9, 21, 17, 30, tzinfo=ZONE).timestamp()
    assert [e["type"] for e in f.published] == ["attention_added"]
    assert f.published[0]["source_id"] == "msg:m" and "Quad" in f.published[0]["quote"]


async def test_the_same_message_never_acts_twice(ledger):
    f = Fakes()
    await run(mom_thread(), MON_9AM + 60, ledger, f)
    assert await run(mom_thread(), MON_9AM + 120, ledger, f) == []
    assert len(f.created) == 1 and len(f.published) == 1


async def test_enabling_never_acts_on_messages_from_before_it_was_enabled(tmp_path):
    led = ledger_mod.Ledger(tmp_path / "fresh.db")             # no baseline yet
    f = Fakes()
    out = await run(mom_thread(at=MON_9AM), MON_9AM + 600, led, f)      # message precedes the first pass
    assert out == [] and f.created == []
    later = mom_thread(at=MON_9AM + 900) + [rec("m2", "Mom", "Dinner tonight at 7pm", MON_9AM + 900)]
    out = await run(later, MON_9AM + 960, led, f)
    assert any(o.state == "created" for o in out)


async def test_the_daily_cap_stops_the_third_reminder(ledger):
    f = Fakes()
    recs = [rec("h", "Mom", "hi", MON_9AM - 3600, "outgoing"),
            rec("a", "Mom", "Lunch today at 12pm", MON_9AM),
            rec("b", "Mom", "Dinner today at 7pm", MON_9AM + 60),
            rec("c", "Mom", "Movie today at 9pm", MON_9AM + 120)]
    out = await run(recs, MON_9AM + 300, ledger, f, daily_cap=2)
    assert [o.state for o in out] == ["created", "created", "capped"]
    assert len(f.created) == 2
    assert await run(recs, MON_9AM + 400, ledger, f, daily_cap=2) == []     # capped stays capped


async def test_a_reminder_the_user_just_added_is_noticed_before_writing(ledger):
    onfile = commit("Meet Mom", datetime(2026, 9, 21, 18, 0, tzinfo=ZONE).timestamp())
    f = Fakes(fresh=[onfile])                   # appears after the tick began: not in `commitments`
    out = await run(mom_thread(), MON_9AM + 60, ledger, f)
    assert [o.state for o in out] == ["already_on_file"] and f.created == [] and f.published == []


@pytest.mark.parametrize("fakes,state", [
    (Fakes(result={"ok": False, "status": "unknown", "error": "no receipt"}), "unknown"),
    (Fakes(result={"ok": False, "status": "failed", "error": "no access"}), "failed"),
    (Fakes(raises=RuntimeError("boom")), "unknown"),
])
async def test_bad_outcomes_are_recorded_and_never_retried(ledger, fakes, state):
    out = await run(mom_thread(), MON_9AM + 60, ledger, fakes)
    assert [o.state for o in out] == [state] and fakes.published == []
    assert await run(mom_thread(), MON_9AM + 90, ledger, fakes) == []
    assert len(fakes.created) == 1


async def test_quiet_hours_still_create_the_reminder_but_send_no_alert(ledger):
    night = datetime(2026, 9, 21, 22, 30, tzinfo=ZONE).timestamp()
    led = ledger
    led.set_meta("enabled_at", repr(night - 86400))
    f = Fakes()
    out = await run(mom_thread("Meet me in the Quad at 11:45PM", at=night), night + 60, led, f)
    assert [o.state for o in out] == ["created"] and f.published == []


async def test_a_message_about_friday_is_reconsidered_once_friday_is_within_48_hours(ledger):
    recs = [rec("h", "Mom", "hi", MON_9AM - 3600, "outgoing"),
            rec("m", "Mom", "Dinner Friday at 7pm", MON_9AM)]
    f = Fakes()
    assert await run(recs, MON_9AM + 60, ledger, f) == []                    # four days out
    wednesday = datetime(2026, 9, 23, 9, 0, tzinfo=ZONE).timestamp()
    assert await run(recs, wednesday, ledger, f) == []                       # 58h: still not soon
    thursday = datetime(2026, 9, 24, 9, 0, tzinfo=ZONE).timestamp()
    out = await run(recs, thursday, ledger, f)                               # 34h: now it is
    assert [o.state for o in out] == ["created"]
    assert f.created[0].event_ts == datetime(2026, 9, 25, 19, 0, tzinfo=ZONE).timestamp()


async def test_promo_and_strangers_never_reach_the_effect(ledger):
    recs = [rec("p", "SHOP", "50% OFF dinner tonight at 7pm! Reply STOP to opt out", MON_9AM),
            rec("s", "Rando", "Meet me in the Quad at 6PM", MON_9AM + 30)]
    f = Fakes()
    assert await run(recs, MON_9AM + 60, ledger, f) == [] and f.created == []
    assert ledger.recent() == []


async def test_the_runner_package_still_avoids_the_live_assistant_store():
    import ast
    from pathlib import Path
    import service.attention as pkg
    for path in sorted(Path(pkg.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(n.startswith(("service.assistant", "service.tools")) for n in names), path.name


async def test_one_message_that_breaks_the_detector_does_not_stop_the_others(ledger, monkeypatch):
    from service.attention import detectors
    real = detectors.UncapturedCommitment.decide

    def flaky(self, item, as_of=None):
        if item.id == "msg:bad":
            raise RuntimeError("parser blew up")
        return real(self, item, as_of)
    monkeypatch.setattr(detectors.UncapturedCommitment, "decide", flaky)
    recs = [rec("h", "Mom", "hi", MON_9AM - 3600, "outgoing"),
            rec("bad", "Mom", "Lunch today at 12pm", MON_9AM),
            rec("ok", "Mom", "Dinner today at 7pm", MON_9AM + 60)]
    f = Fakes()
    out = await run(recs, MON_9AM + 120, ledger, f)
    assert [o.source_id for o in out] == ["msg:ok"] and ledger.get("msg:bad") is None


# ------------------------------------------------------------ what the title says

def test_a_long_message_is_cut_to_the_clause_that_states_the_plan():
    text = ("Two things due tonight: 1. CSE syllabus quiz - the deadline got extended to tonight "
            "and it's a real chunk of your grade 2. Room Condition Form - due 11:59pm, keeps you "
            "from being billed")
    at = text.index("11:59pm")
    clause = live._clause(text, at)
    assert clause.startswith("Room Condition Form - due 11:59pm") and "CSE" not in clause


def test_the_clause_holding_the_time_is_chosen_not_the_first_sentence():
    text = "Quick Sunday heads-up. SlugsCARE is due Monday (10/5), if it's not already done. Not sure if you finished."
    assert live._clause(text, text.index("Monday")) == "SlugsCARE is due Monday (10/5), if it's not already done."


def test_short_messages_and_unknown_offsets_use_the_whole_text():
    assert live._clause("Meet me in the Quad at 6PM", 20) == "Meet me in the Quad at 6PM"
    long = "word " * 40
    assert live._clause(long, None).endswith("…") and len(live._clause(long, None)) <= 90


async def test_an_accepted_plan_is_titled_from_the_users_own_proposal_with_the_friends_name(ledger):
    recs = [rec("p", "Sam", "3:30? Have to get lunch", MON_9AM, "outgoing"),
            rec("r", "Sam", "sure", MON_9AM + 120)]
    f = Fakes()
    out = await runner.process(records=recs, commitments=[], now=MON_9AM + 180, tz=TZ,
                               settings=Settings(mode="live"), ledger=ledger, create=f.create,
                               publish=f.publish, fresh_commitments=f.fresh_commitments,
                               name_for=lambda handle: "Sam Rivera" if handle == "Sam" else None)
    assert [o.state for o in out] == ["created"]
    assert f.created[0].title == "Sam Rivera: 3:30? Have to get lunch (3:30 PM)"


async def test_a_phone_number_without_a_contact_name_is_just_text(ledger):
    recs = [rec("h", "+16505550123", "hi", MON_9AM - 3600, "outgoing"),
            rec("m", "+16505550123", "Dinner today at 7pm", MON_9AM)]
    f = Fakes()
    await run(recs, MON_9AM + 60, ledger, f)
    assert f.created[0].title.startswith("Text: Dinner today at 7pm")
