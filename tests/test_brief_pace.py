"""When background Brief work may start (service/brief_pace.py): not during a chat
or just after one, not right after another build, never lost, never starved, and
an explicit refresh skips all of it.

No live model, no native sources, no writes outside a scratch WISP_HOME.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-pace-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service import brief_pace as P
from service import idle
from service import memory_saver as M
from service.assistant import brief as B
from service.assistant import mail_gists as G
from service.assistant import scheduler
from service.assistant.hub import hub
from service.proactive import cycle
from service.proactive import return_refresh as R
from service.proactive.store import ProactiveStore

MIN = 60.0
READY = {"READY": True, "FULL": "Your day.", "TODAY": "Today.", "MESSAGES": "Messages."}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No turn, no chat, no build, no holds, and an app listening."""
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_turn", None)
    monkeypatch.setattr(M._state, "last_intent", None)
    monkeypatch.setattr(hub, "_subs", {asyncio.Queue()})
    P._reset_for_tests()
    R._reset_for_tests()
    yield
    P._reset_for_tests()
    R._reset_for_tests()


@pytest.fixture
def store(tmp_path, monkeypatch):
    built = ProactiveStore(tmp_path / "proactive")
    from service.proactive import store as store_mod
    monkeypatch.setattr(store_mod, "store", lambda: built)
    monkeypatch.setattr(cycle, "_task", None)
    monkeypatch.setattr(cycle, "_rerun", None)
    monkeypatch.setattr(cycle, "_next_tick_at", 0.0)
    monkeypatch.setattr(cycle, "_idle_retry_at", 0.0)
    monkeypatch.setattr(cycle, "_last_standdown", "")
    monkeypatch.setattr(cycle, "_remote_background_paused", lambda: False)
    monkeypatch.setattr(G, "_task", None)
    built.save_state(idle_refresh_at=time.time())       # the hourly refresh isn't due
    from service.assistant.store import assistant_store
    monkeypatch.setattr(assistant_store, "first_launch_at", lambda: time.time() - 30 * 86400)   # not a new install
    # Most tests here are about the chat and gap rules, on the path quiet hours use.
    # The daily plan has its own section, and its own fixture, below.
    monkeypatch.setattr(P, "automatic_plan", lambda now=None, state=None: "as_is")
    return built


def _chatted(seconds_ago: float) -> None:
    idle._last_turn = time.monotonic() - seconds_ago


async def _settle(task):
    if task is not None:
        await task
    return task


def _tick() -> list[str]:
    """One scheduler pass over the cycle with the build itself stubbed: what it ran."""
    ran: list[str] = []

    async def tick():
        ran.append("ordinary")

    async def light_tick():
        ran.append("light")

    async def build_tick():
        ran.append("build")

    async def return_tick():
        ran.append("catch-up")

    async def idle_tick():
        ran.append("hourly")

    async def go():
        with patch.object(cycle, "_tick", tick), patch.object(cycle, "_return_tick", return_tick), \
             patch.object(cycle, "_idle_tick", idle_tick), patch.object(cycle, "_light_tick", light_tick), \
             patch.object(cycle, "_build_tick", build_tick):
            await _settle(cycle.schedule_tick())

    asyncio.run(go())
    return ran


async def _one_item_cycle(store, item_hash: str, reply, **kwargs):
    """A real cycle over one synthetic mail item and an injected model."""
    now = kwargs.pop("now", time.time())
    item = dict(id="mail:synthetic", source="mail", account="me", raw_account="", who="Sample Person",
                title="Sample subject", text="Subject: Sample subject\nFrom Sample Person: Hello.",
                lines=["Hello."], received_ts=now - 60, replied=False,
                ref={"kind": "email", "message_id": "m1", "account": "a", "account_id": "a",
                     "to": "sample@example.com", "subject": "Sample subject", "has_body": True},
                reply_ref=None, unread=True, person=True, secret=False, user_wrote=False, hash=item_hash)
    return await cycle.run_cycle(now=now, tz="UTC", reply_fn=reply, store=store, collect=lambda n, z: [item],
                                 schedule_fn=lambda n, z: {"calendar": [], "reminders": [], "past_calendar": []},
                                 linked=set(), own="me", allowed=set(), abort=kwargs.pop("abort", lambda: False),
                                 deadline_fn=None, autoadd_fn=None, **kwargs)


# --- chat in progress --------------------------------------------------------------


def test_no_chat_starts_at_once():
    assert not P.chat_recent()
    assert P.may_start("cycle")


def test_a_turn_in_flight_holds_everything(store):
    idle.begin_foreground()
    try:
        assert not P.may_start("cycle")
        assert _tick() == []
    finally:
        idle.end_foreground()


def test_the_gap_between_two_turns_is_still_the_chat(store):
    idle.begin_foreground()
    idle.end_foreground()                   # a reply just finished; the next isn't typed yet
    assert not idle.foreground_busy()
    assert P.chat_recent()
    assert _tick() == []
    _chatted(P.CHAT_QUIET_S - 5)
    assert _tick() == []


def test_opening_ask_counts_as_chatting(store):
    M.note_intent()
    assert P.chat_recent()
    assert _tick() == []


def test_a_catch_up_owed_during_chat_waits_too(store):
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    assert R.arm("launch", store=store)
    _chatted(84)                            # what started the Brief live: 84 s after the last turn
    assert _tick() == []
    assert R.pending()


def test_the_brief_does_not_claim_to_update_during_chat(store):
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    assert R.arm("opened", store=store)
    assert R.updating()
    _chatted(20)
    assert not R.updating()
    _chatted(P.CHAT_QUIET_S + 1)
    assert R.updating()


def test_a_daily_summary_waits_for_the_chat(monkeypatch):
    slot = (datetime.now().date(), "evening")
    monkeypatch.setattr(scheduler, "_brief_task", None)
    monkeypatch.setattr(scheduler, "_pending_slot", lambda now: slot)
    monkeypatch.setattr(scheduler, "_awaiting_first_permission", lambda now: False)
    monkeypatch.setattr(scheduler, "_early_first_brief_due", lambda now: False)
    run = AsyncMock(return_value=None)
    monkeypatch.setattr(scheduler, "_run_daily_brief", run)

    async def go():
        return await _settle(scheduler._start_daily_brief())

    _chatted(11)                            # the evening run landed 11 s after a turn, live
    assert asyncio.run(go()) is None and run.await_count == 0
    assert scheduler._brief_attempts.get(slot, 0) == 0     # waiting costs no retry
    _chatted(P.CHAT_QUIET_S + 1)
    assert asyncio.run(go()) is not None and run.await_count == 1


def test_settings_says_how_often_the_brief_really_updates():
    """Quick settings said "Composed twice a day" long after the Brief moved to
    about four builds across the working hours. The row has to track the number."""
    text = (Path(__file__).resolve().parents[1] / "app/Sources/WispApp/QuickSettingsText.swift").read_text()
    assert f"About {P.BUILDS_PER_DAY} times a day" in text
    assert f"about {P.BUILDS_PER_DAY} times a day" in text
    assert "twice a day" not in text


def test_the_first_ever_summary_only_waits_for_the_turn(monkeypatch):
    monkeypatch.setattr(scheduler, "_early_first_brief_due", lambda now: True)
    _chatted(5)
    assert not scheduler._held_for_chat()


def test_a_day_one_rebuild_waits_for_the_chat(monkeypatch):
    monkeypatch.setattr(scheduler, "_brief_task", None)
    monkeypatch.setattr(scheduler, "_pending_recompose", lambda now: (datetime.now().date(), "morning", "rebuilt"))
    monkeypatch.setattr(scheduler, "_early_first_brief_due", lambda now: False)
    _chatted(5)

    async def go():
        return scheduler._start_day_one_recompose()

    assert asyncio.run(go()) is None


def test_a_gist_waits_for_the_chat(monkeypatch):
    monkeypatch.setattr(P, "between_builds", lambda now=None: False)
    monkeypatch.setattr(G, "_task", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(G, "_remote_background_paused", lambda: False)
    monkeypatch.setattr(G, "refresh", AsyncMock(return_value=False))
    _chatted(5)

    async def go():
        return await _settle(G.schedule_tick())

    assert asyncio.run(go()) is None
    _chatted(P.CHAT_QUIET_S + 1)
    assert asyncio.run(go()) is not None


def test_a_turn_stops_a_running_build_and_keeps_its_place(store):
    """What happens to a build already running: the next model call doesn't start."""
    turn = {"on": False}
    calls = []

    async def reply(system, material, abort, tokens):
        calls.append(system)
        turn["on"] = True                   # the user starts a turn during this call
        return None if abort() else "{}"

    result = asyncio.run(_one_item_cycle(store, "h1", reply, abort=lambda: turn["on"]))
    assert result["status"] == "yielded"
    assert len(calls) == 1
    assert "build_at" not in store.state()  # an unfinished build starts no gap


# --- a delayed build still runs -----------------------------------------------------


def test_a_deferred_build_runs_once_the_chat_goes_quiet(store):
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    R.arm("launch", store=store)
    _chatted(10)
    assert _tick() == []
    _chatted(P.CHAT_QUIET_S + 1)
    assert _tick() == ["catch-up"]


def test_steady_chat_cannot_starve_it():
    start = 1000.0
    idle._last_turn = start
    assert not P.may_start("cycle", start)
    # A turn every minute for 29 minutes: held throughout.
    for minute in range(1, 30):
        idle._last_turn = start + minute * MIN - 40
        assert not P.may_start("cycle", start + minute * MIN), minute
    # Past the longest hold, it takes the next 30-second gap.
    idle._last_turn = start + 31 * MIN - 40
    assert P.may_start("cycle", start + 31 * MIN)


def _hold_for_the_longest_time(start: float) -> float:
    """A scheduler asking every few minutes through a steady chat."""
    at = start
    while at < start + P.MAX_HOLD_S:
        idle._last_turn = at - 5
        assert not P.may_start("cycle", at)
        at += 5 * MIN
    return at


def test_an_overdue_build_never_interrupts_a_turn_or_a_fresh_reply():
    later = _hold_for_the_longest_time(1000.0)
    idle._last_turn = later - 5
    assert not P.may_start("cycle", later)   # 5 s after a reply: under the settle
    idle._last_turn = later - 40
    idle._foreground_turns = 1
    assert not P.may_start("cycle", later)   # in a turn
    idle._foreground_turns = 0
    assert P.may_start("cycle", later)


def test_an_overdue_build_retries_at_a_pace_not_every_tick():
    at = _hold_for_the_longest_time(1000.0)
    idle._last_turn = at - 40
    assert P.may_start("cycle", at)
    idle._last_turn = at + 30 - 40
    assert not P.may_start("cycle", at + 30)  # it just tried; a turn stopped it
    at += P.OVERDUE_RETRY_S
    idle._last_turn = at - 40
    assert P.may_start("cycle", at)


def test_an_old_hold_does_not_count_against_a_new_chat():
    start = 1000.0
    idle._last_turn = start
    assert not P.may_start("daily", start)
    # Hours later, in a new chat, nobody has asked since: a new wait begins.
    later = start + 6 * 3600
    idle._last_turn = later - 40
    assert not P.may_start("daily", later)


# --- coalescing and the minimum gap --------------------------------------------------


def test_several_triggers_become_one_build(store):
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600,
                     idle_refresh_at=time.time() - 2 * 3600)
    R.arm("launch", store=store)
    R.arm("opened", store=store)
    R.arm("unparked", store=store)
    assert _tick() == ["catch-up"]
    store.save_state(build_at=time.time())  # that build finished
    assert _tick() == []
    assert _tick() == []


def test_no_second_build_right_after_a_build(store):
    """The live double run: a catch-up ended and the ordinary tick was already due."""
    store.save_state(build_at=time.time() - 15)
    cycle._next_tick_at = 0.0
    assert _tick() == []


def test_new_mail_shows_up_after_the_gap(store):
    calls = []

    async def reply(system, material, abort, tokens):
        calls.append(material)
        return "{}"

    assert asyncio.run(_one_item_cycle(store, "h1", reply))["status"] == "done"
    first = len(calls)
    assert first > 0 and store.state().get("build_at")
    assert _tick() == []                    # new mail right away waits
    store.save_state(build_at=time.time() - P.MIN_BUILD_GAP_S - 1)
    assert _tick() == ["ordinary"]          # past the gap, the tick runs
    assert asyncio.run(_one_item_cycle(store, "h2", reply))["status"] == "done"
    assert len(calls) > first               # and it reads what arrived


def test_a_cycle_that_used_no_model_starts_no_gap(store):
    async def reply(system, material, abort, tokens):
        return None

    asyncio.run(_one_item_cycle(store, "h1", reply))
    assert "build_at" not in store.state()


def test_the_gap_ignores_a_stamp_that_is_not_a_past_time():
    now = 2_000_000.0
    assert P.build_gap_passed(None, now)
    assert P.build_gap_passed("soon", now)
    assert P.build_gap_passed(True, now)
    assert P.build_gap_passed(now + 3600, now)
    assert P.build_gap_passed(now - P.MIN_BUILD_GAP_S, now)
    with patch.object(P, "_in_setup", lambda now: False):
        assert not P.build_gap_passed(now - P.MIN_BUILD_GAP_S + 1, now)


def test_a_new_install_has_no_gap(monkeypatch):
    from service.assistant.store import assistant_store
    now = 2_000_000.0
    monkeypatch.setattr(assistant_store, "first_launch_at", lambda: now - 10 * MIN)
    assert P.build_gap_passed(now - 30, now)
    monkeypatch.setattr(assistant_store, "first_launch_at", lambda: now - 2 * 3600)
    assert not P.build_gap_passed(now - 30, now)


# --- an explicit refresh --------------------------------------------------------------


def test_analyze_now_bypasses_the_gap_and_the_chat(store, monkeypatch):
    store.save_state(build_at=time.time() - 5)
    _chatted(5)
    assert _tick() == []                    # automatic: held by both
    seen = []

    async def fake(**kwargs):
        seen.append(kwargs)
        return {"status": "done"}

    monkeypatch.setattr(cycle, "run_cycle", fake)
    monkeypatch.setattr(cycle, "running", lambda: False)

    async def go():
        outcome = cycle.analyze()
        await cycle._task
        return outcome

    assert asyncio.run(go())["queued"] is False
    assert seen and seen[0]["force"] is True and seen[0]["patient"] is True


def test_analyze_queued_from_outside_a_loop_still_bypasses(store, monkeypatch):
    store.save_state(build_at=time.time() - 5)
    _chatted(5)
    cycle._rerun = {"account": "sample@example.com"}
    seen = []

    async def fake(**kwargs):
        seen.append(kwargs)
        return {"status": "done"}

    monkeypatch.setattr(cycle, "run_cycle", fake)

    async def go():
        task = cycle.schedule_tick()
        assert task is not None
        await task

    asyncio.run(go())
    assert seen and seen[0]["force"] is True and seen[0]["account"] == "sample@example.com"


# --- the loop: mail text must not change while the mail doesn't --------------------


class _Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def _record(*fields) -> str:
    from service.tools import email_tools as E
    return E._RAW_FS.join(fields)


@pytest.fixture
def two_copies(monkeypatch):
    """One linked Google account whose inbox Mail.app also holds, with synthetic mail."""
    from service.tools import email_tools as E
    address = "sample@school.example"
    clock = _Clock(1_800_000_000.0)
    monkeypatch.setattr(E.time, "time", clock)
    monkeypatch.setattr(E, "_external_accounts", {address: {"mail_account_id": "NATIVE-ID", "headers": None,
                                                            "history": None, "raw": None}})
    monkeypatch.setattr(E, "_native_source", "mail_app")
    monkeypatch.setattr(E, "_persist_composed_sources", lambda: None)
    monkeypatch.setattr(E, "_swift_headers", "")
    monkeypatch.setattr(E, "_swift_history", "")
    monkeypatch.setattr(E, "_swift_raw", _record("1", "U", "Google", "NATIVE-ID", "S", "me", "Subject", "<m@x>",
                                                 "NATIVE-COPY"))
    monkeypatch.setattr(E, "_native_raw_at", clock.at - 100)
    monkeypatch.setattr(E, "_refresh_native_bindings", lambda: None)
    google = _record("1", "U", address, address, "S", "me", "Subject", "<m@x>", "GOOGLE-COPY")
    E.cache_external_emails(account=address, raw=google)
    return E, clock, address, google


def _copy_shown(E) -> str:
    assert ("GOOGLE-COPY" in E._raw_emails) != ("NATIVE-COPY" in E._raw_emails)
    return "google" if "GOOGLE-COPY" in E._raw_emails else "native"


def test_the_copy_of_a_mail_stays_put_when_no_mail_arrives(two_copies):
    """What drove the loop: past 5 minutes, any recompose handed every thread to the
    Mail.app copy, and the next 15-minute fetch handed it back."""
    E, clock, address, google = two_copies
    assert _copy_shown(E) == "google"
    clock.at += 60
    E._native_raw_at = clock.at              # Mail.app syncs its bodies after Google did
    seen = set()
    for _ in range(14):                      # headers and sent syncs recompose every few minutes
        clock.at += 60
        E._recompose_all()
        seen.add(_copy_shown(E))
    assert seen == {"google"}
    clock.at += 60
    E.cache_external_emails(account=address, raw=google)
    assert _copy_shown(E) == "google"


def test_only_an_expired_sign_in_falls_back_at_once(two_copies):
    E, clock, address, _ = two_copies
    clock.at += 60
    E._native_raw_at = clock.at
    clock.at += 1
    E.external_email_error(address, "raw", "Google could not be reached")
    assert _copy_shown(E) == "google"
    E._external_accounts[address]["auth_expired"] = True
    E._recompose_all()
    assert _copy_shown(E) == "native"


def test_a_long_silent_google_gives_way_to_a_newer_mail_read(two_copies):
    E, clock, _, _ = two_copies
    clock.at += E._EXTERNAL_PART_MAX_AGE["raw"] + 1
    E._native_raw_at = clock.at - 1
    E._recompose_all()
    assert _copy_shown(E) == "native"


def test_a_snapshot_outlasts_its_own_fetch_interval():
    from service import gmail_source as S
    from service.tools import email_tools as E
    assert E._EXTERNAL_PART_MAX_AGE["headers"] >= 2 * S.HEADERS_EVERY
    assert E._EXTERNAL_PART_MAX_AGE["raw"] >= 2 * S.RAW_EVERY
    assert E._EXTERNAL_PART_MAX_AGE["history"] >= 2 * S.HISTORY_EVERY


# --- about four builds a day --------------------------------------------------------


def _at(hour: int, minute: int = 0, day: int = 15) -> float:
    return datetime(2027, 1, day, hour, minute).timestamp()


def test_the_days_times_spread_across_the_working_day():
    assert P.brief_times() == [9 * 60, 11 * 60 + 40, 14 * 60 + 20, 17 * 60]
    assert P.brief_times(8 * 60, 18 * 60) == [8 * 60, 11 * 60 + 20, 14 * 60 + 40, 18 * 60]


def test_the_days_times_handle_odd_hours():
    assert P.brief_times(22 * 60, 6 * 60) == [0 + 40, 3 * 60 + 20, 6 * 60, 22 * 60]    # a night shift
    assert P.brief_times(9 * 60, 9 * 60) == P.brief_times()                            # no real window
    assert len(P.brief_times(9 * 60, 9 * 60 + 2)) <= 4
    assert P.brief_times(0, 1440) == P.brief_times()                                   # a whole day: no span


def test_the_days_times_come_from_the_working_hours(monkeypatch):
    from service import user_hours
    monkeypatch.setattr(user_hours, "working_hours", lambda: user_hours.Hours(10 * 60, 19 * 60, (0, 1, 2)))
    assert P.todays_brief_times() == P.brief_times(10 * 60, 19 * 60)
    monkeypatch.setattr(user_hours, "working_hours", lambda: 1 / 0)
    assert P.todays_brief_times() == P.brief_times()


def test_the_latest_slot_reaches_back_to_yesterday():
    times = P.brief_times()
    assert P.latest_slot(_at(12, 0), times) == _at(11, 40)
    assert P.latest_slot(_at(9, 0), times) == _at(9, 0)
    assert P.latest_slot(_at(7, 0), times) == _at(17, 0, day=14)


def test_a_slot_is_due_until_something_covers_it():
    times = P.brief_times()
    assert P.slot_due(None, _at(12), times)
    assert P.slot_due(_at(9, 5), _at(12), times)
    assert not P.slot_due(_at(11, 45), _at(12), times)
    assert not P.slot_due(_at(11, 20), _at(12), times)  # just before it (Analyze now): covered
    assert P.slot_due(_at(13), _at(12), times)          # a stamp from the future doesn't count


@pytest.fixture
def daytime(monkeypatch):
    """An established install at an ordinary hour, with default hours."""
    monkeypatch.setattr(P, "_in_quiet_hours", lambda now: False)
    monkeypatch.setattr(P, "_in_setup", lambda now: False)
    monkeypatch.setattr(P, "todays_brief_times", P.brief_times)


def test_the_plan_by_day(daytime):
    state = {"model_as_of": _at(11, 45), "build_at": _at(11, 45)}
    assert P.automatic_plan(_at(12), state) == "light"
    assert P.automatic_plan(_at(14, 25), state) == "build"
    assert P.automatic_plan(_at(14, 25), {**state, "built_for": _at(14, 21)}) == "light"


def test_a_slot_missed_asleep_is_caught_up_once(daytime):
    state = {"model_as_of": _at(11, 45), "build_at": _at(11, 45)}
    assert P.automatic_plan(_at(17, 30), state) == "build"     # slept through 14:20 and 17:00
    state["built_for"] = _at(17, 30)
    assert P.automatic_plan(_at(17, 35), state) == "light"
    assert P.automatic_plan(_at(21, 0), state) == "light"      # the evening isn't quiet: still no build


def test_a_day_has_at_most_four_builds_however_much_mail_comes(daytime):
    state = {"model_as_of": _at(8, 0, day=15), "build_at": _at(8, 0, day=15)}
    builds = []
    at = _at(8, 5, day=15)
    while at < _at(21, 55, day=15):         # a tick every five minutes, all day, with mail always changing
        if P.automatic_plan(at, state) == "build":
            builds.append(at)
            state.update(model_as_of=at, build_at=at, built_for=at)
        at += 5 * MIN
    assert len(builds) == 4


def test_a_new_install_or_a_long_absence_goes_on_as_before(monkeypatch, daytime):
    assert P.automatic_plan(_at(12), {}) == "as_is"                                     # never built
    assert P.automatic_plan(_at(12), {"model_as_of": _at(12) - 2 * 86400}) == "as_is"   # days away
    monkeypatch.setattr(P, "_in_setup", lambda now: True)
    assert P.automatic_plan(_at(12), {"model_as_of": _at(11)}) == "as_is"


def test_quiet_hours_are_left_as_they_are(monkeypatch, daytime):
    monkeypatch.setattr(P, "_in_quiet_hours", lambda now: True)
    assert P.automatic_plan(_at(3), {"model_as_of": _at(2), "built_for": _at(2)}) == "as_is"
    assert not P.between_builds(_at(3))


def test_real_quiet_hours_are_read_from_the_setting(monkeypatch):
    from service import user_hours
    monkeypatch.setattr(user_hours, "in_quiet_hours", lambda when: when.hour < 8)
    assert P._in_quiet_hours(_at(3))
    assert not P._in_quiet_hours(_at(12))
    monkeypatch.setattr(user_hours, "in_quiet_hours", lambda when: 1 / 0)
    assert not P._in_quiet_hours(_at(3))


@pytest.fixture
def planned(store, monkeypatch):
    """schedule_tick with a chosen plan."""
    def choose(plan):
        monkeypatch.setattr(P, "automatic_plan", lambda now=None, state=None: plan)
    return choose


def test_between_builds_the_tick_is_light_and_ignores_the_chat(store, planned):
    planned("light")
    _chatted(5)                             # no model call, so a chat doesn't hold it
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    R.arm("opened", store=store)            # owed, but it doesn't start a forced rebuild by day
    assert _tick() == ["light"]
    assert R.pending()


def test_a_light_tick_keeps_its_pace_and_never_runs_in_a_turn(store, planned, monkeypatch):
    planned("light")
    monkeypatch.setattr(cycle, "_next_tick_at", time.time() + 60)
    assert _tick() == []
    monkeypatch.setattr(cycle, "_next_tick_at", 0.0)
    idle.begin_foreground()
    try:
        assert _tick() == []
    finally:
        idle.end_foreground()


def test_a_scheduled_build_waits_for_the_chat_then_runs(store, planned):
    planned("build")
    _chatted(30)
    assert _tick() == []
    _chatted(P.CHAT_QUIET_S + 1)
    assert _tick() == ["build"]


def test_a_scheduled_build_replaces_the_forced_catch_up(store, planned):
    planned("build")
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600,
                     idle_refresh_at=time.time() - 2 * 3600)
    R.arm("launch", store=store)
    assert _tick() == ["build"]


def test_the_brief_says_updating_only_when_a_build_may_run(store, monkeypatch):
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    assert R.arm("opened", store=store)
    assert R.updating()
    monkeypatch.setattr(P, "automatic_plan", lambda now=None, state=None: "light")
    assert not R.updating()
    monkeypatch.setattr(P, "automatic_plan", lambda now=None, state=None: "build")
    assert R.updating()


def _new_item(now: float, item_hash: str) -> dict:
    return dict(id="mail:new", source="mail", account="me", raw_account="", who="New Person", title="New subject",
                text="Subject: New subject\nFrom New Person: Could you confirm by Friday?",
                lines=["Could you confirm by Friday?"], received_ts=now - 60, replied=False,
                ref={"kind": "email", "message_id": "m9", "account": "a", "account_id": "a",
                     "to": "new@example.com", "subject": "New subject", "has_body": True},
                reply_ref=None, unread=True, person=True, secret=False, user_wrote=False, hash=item_hash)


def test_a_light_cycle_calls_no_model_and_keeps_what_the_model_wrote(store):
    now = time.time()
    written = dict(cards=[{"id": "card:1", "account": "me", "todo_id": "t1", "sources": [{"ref": "mail:old"}],
                           "action": {"method": "email_reply"}}],
                   todos=[{"id": "t1", "account": "me", "title": "Old to-do"}],
                   summaries={"me": "What the model wrote."},
                   judged={"me": {"key": "k", "rkey": "r", "at": now - 900, "model": True, "reply": "{}"}},
                   model=True, model_as_of=now - 900, as_of=now - 900, build_at=now - 900, accounts=["me"])
    store.save_state(**written)
    calls = []

    async def reply(system, material, abort, tokens):
        calls.append(system)
        return "{}"

    async def must_not_act(*args, **kwargs):
        raise AssertionError("auto-add acts only in a build")

    result = asyncio.run(cycle.run_cycle(
        now=now, tz="UTC", reply_fn=reply, store=store, collect=lambda n, z: [_new_item(n, "h1")],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": [], "past_calendar": []}, linked=set(), own="me",
        allowed=set(), abort=lambda: False, deadline_fn=None, autoadd_fn=must_not_act, light=True))
    state = store.state()
    assert result["status"] == "done" and calls == []
    assert state["summaries"] == written["summaries"] and state["judged"] == written["judged"]
    assert [c["id"] for c in state["cards"]] == ["card:1"] and [t["id"] for t in state["todos"]] == ["t1"]
    assert state["model"] is True
    assert state["model_as_of"] == written["model_as_of"] and state["build_at"] == written["build_at"]
    assert state["as_of"] == now
    assert store.triage_get("mail:new", cycle._tkey(_new_item(now, "h1"))) is None    # the build still reads it


def _build_ready(monkeypatch, cycle_result, gists=None):
    calls = {"cycle": [], "gists": 0, "announced": 0, "light": 0}

    async def fake_cycle(**kwargs):
        calls["cycle"].append(kwargs)
        return cycle_result

    async def fake_gists():
        calls["gists"] += 1

    async def fake_announce():
        calls["announced"] += 1

    async def ready():
        return True

    async def light():
        calls["light"] += 1

    monkeypatch.setattr(cycle, "run_cycle", fake_cycle)
    monkeypatch.setattr(G, "refresh_changed", fake_gists)
    monkeypatch.setattr(R, "model_ready", ready)
    monkeypatch.setattr(R, "_announce", fake_announce)
    monkeypatch.setattr(cycle, "_light_tick", light)
    return calls


def test_a_build_reads_incrementally_settles_whats_owed_and_covers_the_slot(store, monkeypatch):
    calls = _build_ready(monkeypatch, {"status": "done", "model": True})
    store.save_state(as_of=time.time() - 3 * 3600, model_as_of=time.time() - 3 * 3600)
    R.arm("opened", store=store)
    asyncio.run(cycle._build_tick())
    assert calls["cycle"] == [{}]            # not forced: nothing changed means no model call
    assert calls["gists"] == 1 and calls["announced"] == 1
    assert not R.pending() and not R.updating()
    assert store.state()["built_for"] >= time.time() - 5


def test_a_build_with_no_model_keeps_the_brief_current_and_stays_due(store, monkeypatch):
    calls = _build_ready(monkeypatch, {"status": "done", "model": True})

    async def not_ready():
        return False

    monkeypatch.setattr(R, "model_ready", not_ready)
    asyncio.run(cycle._build_tick())
    assert calls["cycle"] == [] and calls["light"] == 1
    assert "built_for" not in store.state()


@pytest.mark.parametrize("result", [{"status": "yielded"}, {"status": "done", "model": False}, {"status": "error"}])
def test_a_build_a_turn_stopped_stays_due(store, monkeypatch, result):
    calls = _build_ready(monkeypatch, result)
    asyncio.run(cycle._build_tick())
    assert calls["gists"] == 0
    assert "built_for" not in store.state()
    assert cycle._next_tick_at > time.time()


def test_a_build_says_updating_while_it_runs(store, monkeypatch):
    seen = []

    async def fake_cycle(**kwargs):
        seen.append(R.updating())
        return {"status": "yielded"}

    async def ready():
        return True

    monkeypatch.setattr(cycle, "run_cycle", fake_cycle)
    monkeypatch.setattr(R, "model_ready", ready)
    asyncio.run(cycle._build_tick())
    assert seen == [True] and not R.updating()


def test_by_day_a_changed_overview_waits_for_a_build(monkeypatch):
    monkeypatch.setattr(G, "_task", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(G, "_remote_background_paused", lambda: False)
    monkeypatch.setattr(G, "refresh", AsyncMock(return_value=False))
    monkeypatch.setattr(G, "_load", lambda: {"summary": {"gist": "Old.", "at": 1.0, "key": "k"}})

    async def go():
        return await _settle(G.schedule_tick())

    monkeypatch.setattr(P, "between_builds", lambda now=None: True)
    assert asyncio.run(go()) is None
    monkeypatch.setattr(G, "_load", dict)    # never written: no reason to wait
    assert asyncio.run(go()) is not None
    monkeypatch.setattr(G, "_load", lambda: {"summary": {"gist": "Old.", "at": 1.0, "key": "k"}})
    monkeypatch.setattr(P, "between_builds", lambda now=None: False)
    assert asyncio.run(go()) is not None     # overnight, as before


def test_a_build_rewrites_only_the_overviews_that_changed(monkeypatch):
    seen = []

    async def fake_refresh(now=None, *, synth=None, abort=None, force=False, kind=None):
        seen.append((kind, force))
        return True

    monkeypatch.setattr(G, "refresh", fake_refresh)
    monkeypatch.setattr(G, "_task", None)
    monkeypatch.setattr(G, "_remote_background_paused", lambda: False)
    assert asyncio.run(G.refresh_changed(abort=lambda: False)) == len(G.KINDS)
    assert seen == [(kind, False) for kind in G.KINDS]


def test_new_mail_reads_in_plain_sentences_once_the_overview_is_out_of_date():
    now = time.time()
    with patch.object(G, "_load", lambda: {"summary": {"gist": "What the model wrote.", "at": now - 40 * MIN,
                                                        "key": "before"}}):
        shown = G.overview("summary", "A plain sentence with the new mail.", now, key="after")
    assert shown == {"text": "A plain sentence with the new mail.", "source": "summary"}


def test_mail_memory_waits_for_the_chat(monkeypatch):
    from service.mail_memory import runner
    monkeypatch.setattr(runner, "_task", None)
    monkeypatch.setattr(runner, "_next_at", 0.0)
    monkeypatch.setattr(runner, "mode", lambda: "on")
    monkeypatch.setattr(runner, "_other_background_work", lambda: False)
    skips = []
    monkeypatch.setattr(runner, "note_skip", skips.append)
    _chatted(5)
    assert runner.schedule_tick() is None
    assert skips == ["foreground"] and runner._task is None
