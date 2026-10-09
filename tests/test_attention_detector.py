"""The `uncaptured_commitment` detector and its parts, on synthetic data only.

Arrival is fixed at Monday 2026-09-21 09:00 America/Los_Angeles.
"""
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from service.attention import contacts, detectors, evaluate, exclusions, matching, resolve
from service.attention.corpus import Item, Snapshot

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
ARRIVAL = datetime(2026, 9, 21, 9, 0, tzinfo=ZONE)
T = ARRIVAL.timestamp()
HOUR = 3600.0


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def msg(guid, conv, text, ts, direction="incoming", sender=None):
    return Item(f"msg:{guid}", "messages", ts, direction,
                sender or ("me" if direction == "outgoing" else conv), conv, text)


def snap(items, commitments=()):
    return Snapshot(Path("/nonexistent"), {}, sorted(items, key=lambda i: i.ts), list(commitments))


def commit(title, when, status="active", cid="c1", location=None):
    return {"id": cid, "title": title, "when_ts": when.timestamp(), "status": status,
            "location": location, "source": "calendar", "kind": "event"}


# ------------------------------------------------------------------- resolve

def r(text):
    return resolve.resolve(text, ARRIVAL, TZ)


def test_bare_clock_means_the_next_occurrence():
    got = r("Meet me in the Quad at 6PM")
    assert got.start == at(21, 18) and got.has_clock and not got.tentative
    assert "date:next_occurrence" in got.inferred and got.blocked is None


def test_clock_that_already_passed_today_rolls_to_tomorrow():
    late = resolve.resolve("see you at 8am", datetime(2026, 9, 21, 9, 30, tzinfo=ZONE), TZ)
    assert late.start == at(22, 8)


def test_bare_hour_takes_the_nearest_upcoming_meridiem():
    got = r("see you at 6")                     # 9:00 now: 6am passed, so 6pm
    assert got.start == at(21, 18) and "meridiem" in got.inferred and not got.tentative


def test_colon_time_without_meridiem_is_not_read_as_morning():
    """The shared parser reads '7:30' as 07:30 with no flag; dinner is not at 7:30am."""
    got = r("Dinner tomorrow at 7:30?")
    assert got.start == at(22, 19, 30) and got.tentative and "meridiem" in got.inferred


def test_explicit_meridiem_and_24_hour_are_trusted():
    assert r("dinner tomorrow at 7:30pm").start == at(22, 19, 30)
    assert not r("dinner tomorrow at 7:30pm").tentative
    assert r("train at 19:30").start == at(21, 19, 30)
    assert r("lunch today at noon").start == at(21, 12)


def test_weekdays_resolve_to_the_nearest_upcoming_day():
    assert r("Let's meet Thursday at 2pm").start == at(24, 14)
    exam = r("Exam is Friday")
    assert exam.day == date(2026, 9, 25) and not exam.has_clock and exam.start is None
    assert resolve.resolve("lunch next monday at noon", ARRIVAL, TZ).start == at(28, 12)


def test_weekday_said_on_that_weekday_in_the_evening_means_next_week():
    evening = datetime(2026, 9, 21, 21, 0, tzinfo=ZONE)
    assert resolve.resolve("class monday at 10am", evening, TZ).start == at(28, 10)


def test_date_and_clock_in_separate_phrases_combine():
    assert r("pick me up at 5:45 pm tomorrow").start == at(22, 17, 45)
    got = r("game at 4 on oct 12")
    assert got.start == datetime(2026, 10, 12, 16, 0, tzinfo=ZONE) and "date:year" in got.inferred


@pytest.mark.parametrize("text,why", [
    ("I'll be there in 20 minutes", "relative_eta"),
    ("can't make it tonight at 7", "negated_or_cancelled"),
    ("I was at the library at 6", "past_reference"),
    ("lunch at noon and dinner at 7pm", "conflicting_times"),
    ("call me when you get out of class", "no_time_stated"),
])
def test_things_that_are_not_a_plan_resolve_to_nothing(text, why):
    got = r(text)
    assert got.blocked == why and got.start is None and got.day is None


# ---------------------------------------------------------------- exclusions

def test_screen_excludes_promo_scam_and_automated_senders():
    promo = msg("1", "SHOP", "50% OFF today only! Reply STOP to opt out", T)
    scam = msg("2", "x", "Your package is on hold. Verify your address at http://scam.example", T)
    toll = msg("3", "x", "Unpaid toll balance. Pay now at bit.ly/abc123 within 24 hours", T)
    short = msg("4", "x", "Your code is 123456", T, sender="48291")
    mail = Item("mail:1", "mail", T, "incoming", "Deals", "promo@shop.example", "Weekly digest")
    noreply = Item("mail:2", "mail", T, "incoming", "School", "no-reply@school.example", "Class Thursday 2pm")
    assert [exclusions.screen(i) for i in (promo, scam, toll, short, mail, noreply)] == \
        ["promo", "scam", "scam", "automated", "promo", "automated"]


def test_screen_leaves_ordinary_messages_alone():
    for text in ("Meet me in the Quad at 6PM",
                 "dinner at 7, here's the map https://maps.example/quad",
                 "lol did you see the game"):
        assert exclusions.screen(msg("1", "Mom", text, T)) is None


# ------------------------------------------------------------------ contacts

def test_two_way_contact_makes_a_sender_eligible_and_decays():
    items = [msg("o", "Mom", "love you", T - 2 * 86400, "outgoing"),
             msg("i", "Mom", "call me", T - 86400)]
    c = contacts.Contacts(items)
    near = c.importance(msg("n", "Mom", "x", T))
    assert near.eligible and near.reason == "recent_outgoing" and near.score > 0.8
    stale = contacts.Contacts([msg("o", "Mom", "hi", T - 90 * 86400, "outgoing")])
    assert not stale.importance(msg("n", "Mom", "x", T)).eligible


def test_strangers_with_no_history_are_not_eligible():
    c = contacts.Contacts([msg("i", "Rando", "hello", T - 3600)])
    got = c.importance(msg("n", "Rando", "meet at 6", T))
    assert not got.eligible and got.reason == "no_recent_two_way_contact"


def test_one_sided_traffic_is_not_contact_however_frequent():
    """A group blast with many messages and no reply from the user must not qualify."""
    items = [msg(str(n), "Team", "practice?", T - n * 3600) for n in range(1, 20)]
    got = contacts.Contacts(items).importance(msg("n", "Team", "x", T))
    assert not got.eligible and got.reason == "no_recent_two_way_contact"


def test_importance_never_uses_the_future():
    """A reply the user sends AFTER the message must not make it important."""
    items = [msg("later", "Mom", "ok", T + HOUR, "outgoing")]
    assert not contacts.Contacts(items).importance(msg("n", "Mom", "x", T)).eligible


def test_mail_counts_only_when_the_name_matches_someone_texted():
    c = contacts.Contacts([msg("o", "Mom", "hi", T - 3600, "outgoing")])
    known = Item("mail:1", "mail", T, "incoming", "Mom", "mom@x.example", "Lunch Sunday")
    unknown = Item("mail:2", "mail", T, "incoming", "Stranger", "s@x.example", "Lunch Sunday")
    assert c.importance(known).eligible and not c.importance(unknown).eligible


# ------------------------------------------------------------------ matching

def found(text, commitments, when=None):
    res = r(text)
    return matching.find_on_file(res, text, commitments, TZ)


def test_a_near_exact_time_is_known_even_with_no_words_in_common():
    assert found("Meet me in the Quad at 6PM", [commit("Zzz", at(21, 18, 10))]).basis == "time"


def test_a_nearby_time_needs_a_shared_word():
    c = commit("Dinner with Alex", at(21, 19))
    assert found("Meet me for dinner at 6PM", [c]).basis == "time+words"
    assert found("Meet me in the Quad at 6PM", [c]) is None


def test_far_times_dismissed_rows_and_other_days_do_not_match():
    assert found("Meet me in the Quad at 6PM", [commit("Quad meetup", at(22, 18))]) is None
    assert found("Meet me in the Quad at 6PM", [commit("Quad", at(21, 18), "dismissed")]) is None
    assert found("Meet me in the Quad at 6PM", [commit("Quad", at(21, 18), "done")]).basis == "time"


def test_date_only_commitments_match_on_day_and_a_shared_word():
    exam = commit("Chem exam", at(25, 9))
    assert found("Exam is Friday", [exam]).basis == "day+words"
    assert found("Exam is Friday", [commit("Dentist", at(25, 9))]) is None


# ------------------------------------------------------------------ detector

def detector(items, commitments=()):
    return detectors.UncapturedCommitment(snap(items, commitments), tz=TZ)


def history():
    return [msg("hist", "Mom", "ok see you", T - 86400, "outgoing")]


def test_the_motivating_example_alerts():
    mom = msg("m", "Mom", "Meet me in the Quad at 6PM", T)
    d = detector(history() + [mom]).decide(mom)
    assert d.alert and d.blocked_by is None
    assert d.resolved.start == at(21, 18)
    assert any("Quad" in w or "6PM" in w for w in d.why)


def test_the_same_message_is_silent_when_it_is_already_on_the_calendar():
    mom = msg("m", "Mom", "Meet me in the Quad at 6PM", T)
    d = detector(history() + [mom], [commit("Meet Mom", at(21, 18))]).decide(mom)
    assert not d.alert and d.blocked_by == "already_on_file"


@pytest.mark.parametrize("text,gate", [
    ("50% OFF dinner tonight at 7pm! Reply STOP to opt out", "promo"),
    ("Your package is on hold, verify at http://scam.example tonight at 7pm", "scam"),
    ("lol", "no_time_stated"),
    ("I'll be there in 20 minutes", "relative_eta"),
    ("dinner next Friday at 7pm", "not_soon"),
])
def test_blocked_by_names_the_gate(text, gate):
    m = msg("m", "Mom", text, T)
    d = detector(history() + [m]).decide(m)
    assert not d.alert and d.blocked_by == gate


def test_a_stranger_cannot_make_wisp_add_reminders():
    m = msg("m", "Rando", "Meet me in the Quad at 6PM", T)
    d = detector([m]).decide(m)
    assert not d.alert and d.blocked_by == "no_recent_two_way_contact"


def test_outgoing_messages_never_alert():
    m = msg("m", "Mom", "I'll meet you at 6PM", T, "outgoing")
    assert detector([m]).decide(m).blocked_by == "not_incoming"


def test_a_question_needs_the_users_own_yes():
    ask = msg("a", "Alex", "Dinner tomorrow at 7:30?", T)
    base = [msg("h", "Alex", "hey", T - HOUR, "outgoing"), ask]
    assert detector(base).decide(ask).blocked_by == "unconfirmed"
    yes = base + [msg("y", "Alex", "sounds good", T + 600, "outgoing")]
    assert detector(yes).decide(ask).alert
    no = base + [msg("n", "Alex", "sorry can't", T + 600, "outgoing")]
    assert detector(no).decide(ask).blocked_by == "unconfirmed"
    late = base + [msg("l", "Alex", "sounds good", T + 8 * HOUR, "outgoing")]
    assert detector(late).decide(ask).blocked_by == "unconfirmed"


def test_date_only_commitments_inside_the_window_alert():
    prof = msg("p", "Prof", "Exam is Wednesday", T)
    d = detector([msg("h", "Prof", "thanks", T - HOUR, "outgoing"), prof]).decide(prof)
    assert d.alert and not d.resolved.has_clock
    far = msg("f", "Prof", "Exam is Sunday", T)
    assert detector([msg("h", "Prof", "thanks", T - HOUR, "outgoing"), far]).decide(far).blocked_by == "not_soon"


def test_mail_from_a_known_name_can_alert():
    items = [msg("h", "Registrar", "thanks", T - HOUR, "outgoing", sender="Registrar"),
             msg("i", "Registrar", "ok", T - 2 * HOUR, sender="Registrar")]
    mail = Item("mail:1", "mail", T, "incoming", "Registrar", "reg@school.example",
                "Advising meeting tomorrow at 2pm")
    assert detector(items + [mail]).decide(mail).alert


# ----------------------------------------------------------------- reporting

def test_registry_and_missed_by_gate_attribution():
    assert "uncaptured_commitment" in evaluate.PREDICTORS
    items = history() + [msg("m", "Mom", "Meet me in the Quad at 6PM", T),
                         msg("p", "Mom", "50% OFF dinner tonight at 7pm! Reply STOP to opt out", T + 60)]
    s = snap(items)
    sample = [{"id": "msg:m", "stratum": "cue"}, {"id": "msg:p", "stratum": "cue"}]
    labels = {"msg:m": {"label": "missing"}, "msg:p": {"label": "missing"}}   # pretend promo was real
    out = evaluate.evaluate(s, sample, labels, evaluate.PREDICTORS["uncaptured_commitment"])
    assert (out["tp"], out["fn"]) == (1, 1)
    assert out["missed_by_gate"] == {"promo": 1}
    assert out["by_reason"]["uncaptured_commitment"] == {"alerts": 1, "correct": 1}


# ------------------------------------------- regressions from the real-data review
# Each case below has the structure of a real false alert or miss found when the
# detector was first scored against labelled data. The sentences are synthetic.

SUNDAY = datetime(2026, 10, 4, 9, 31, tzinfo=ZONE)


@pytest.mark.parametrize("text", [
    "Your order should arrive tomorrow",
    "Check your stocks today",
    "Is he going to the party on sunday",
    "Let me know what your plans are for Friday",
    "Your list is actually empty - nothing due today.",
])
def test_a_bare_day_without_an_obligation_does_not_alert(text):
    m = msg("m", "Mom", text, T)
    d = detector(history() + [m]).decide(m)
    assert not d.alert and d.blocked_by == "date_only_no_obligation"


@pytest.mark.parametrize("text", [
    "Quick Sunday heads-up. SlugsCARE is due Monday (10/5), if it's not already done. Not sure if you finished it, so ignore if so.",
    "Don't forget to pick up the package Monday",
])
def test_a_bare_day_with_an_obligation_alerts(text):
    m = Item("msg:s", "messages", SUNDAY.timestamp(), "incoming", "Asst", "Asst", text)
    h = Item("msg:h", "messages", SUNDAY.timestamp() - 3600, "outgoing", "me", "Asst", "ok")
    d = detectors.UncapturedCommitment(snap([h, m]), tz=TZ).decide(m)
    assert d.alert and d.resolved.day == date(2026, 10, 5) and not d.resolved.has_clock


def test_agreeing_day_mentions_are_one_day_and_the_arrival_day_is_context():
    got = resolve.resolve("Quick Sunday heads-up. It is due Monday (10/5).", SUNDAY, TZ)
    assert got.day == date(2026, 10, 5) and got.blocked is None


def test_genuinely_different_days_still_conflict():
    got = resolve.resolve("I will work from home tomorrow and drive to work on Friday", SUNDAY, TZ)
    assert got.blocked == "conflicting_times"


def test_if_and_not_alone_do_not_cancel_or_hedge_but_cancellation_words_do():
    ok = resolve.resolve("The form is due Monday, if it's not already done. Not sure if you finished", SUNDAY, TZ)
    assert ok.blocked is None and not ok.tentative
    assert resolve.resolve("can't make it tonight at 7", SUNDAY, TZ).blocked == "negated_or_cancelled"
    assert resolve.resolve("the 7pm game is cancelled", SUNDAY, TZ).blocked == "negated_or_cancelled"


def test_proposals_are_tentative_even_without_a_question_mark():
    assert r("Put us for 1PM lmk if that works").tentative
    assert r("maybe dinner tomorrow at 7pm").tentative
    assert not r("dinner tomorrow at 7pm").tentative


def test_got_is_not_past_tense():
    got = resolve.resolve("The deadline got extended to tonight. Room form due 11:59pm", ARRIVAL, TZ)
    assert got.blocked is None and got.start == at(21, 23, 59)
    assert resolve.resolve("I was at the library at 6", ARRIVAL, TZ).blocked == "past_reference"


def test_a_half_hour_drift_still_counts_as_on_file():
    assert found("meet at 1PM", [commit("Zzz", at(21, 13, 30))]).basis == "time"
    assert found("meet at 1PM", [commit("Zzz", at(21, 14, 0))]) is None


# ------------------------------------------------ an explicitly stated timezone (review ATT179-01)

def test_an_explicit_utc_time_is_converted_not_relabelled():
    """Monday 09:00 Pacific, "tomorrow at 15:00 UTC" is Tuesday 08:00 Pacific, not 15:00."""
    got = r("Meeting tomorrow at 15:00 UTC")
    assert got.start == at(22, 8) and got.start.utcoffset() == at(22, 8).utcoffset()
    assert got.start.astimezone(ZoneInfo("UTC")) == datetime(2026, 9, 22, 15, 0, tzinfo=ZoneInfo("UTC"))


def test_an_explicit_iana_zone_is_converted_across_local_midnight():
    got = r("dinner tomorrow at 9pm Asia/Kolkata")
    assert got.start.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None) == datetime(2026, 9, 22, 21, 0)
    assert got.start.astimezone(ZoneInfo(TZ)) == got.start and got.day == got.start.date()
    late = r("call tomorrow at 23:30 Europe/London")
    assert late.start == at(22, 15, 30)


@pytest.mark.parametrize("text", [
    "Meeting tomorrow at 3pm EST", "call tomorrow at 3pm PT", "tomorrow 3pm ET", "tomorrow at 3pm CET",
    "tomorrow at 3pm Eastern", "tomorrow at 3pm Pacific time", "at 15:00 +02:00 tomorrow",
])
def test_a_zone_that_cannot_be_read_safely_is_declined(text):
    got = r(text)
    assert got.start is None and got.blocked == "other_timezone", text


def test_a_named_zone_with_a_guessed_date_or_meridiem_is_declined():
    assert r("call at 15:00 UTC").blocked == "other_timezone"             # which UTC day?
    assert r("call tomorrow at 3 UTC").blocked == "other_timezone"        # am or pm?


def test_conflicting_zones_at_the_same_hour_are_not_merged():
    assert r("tomorrow 15:00 UTC or 15:00 Europe/London").blocked == "conflicting_times"


def test_dst_gap_and_fold_in_the_stated_zone_are_declined():
    arrival = datetime(2026, 3, 1, 9, 0, tzinfo=ZONE)
    gap = resolve.resolve("meet 2026-03-08 at 02:30 America/New_York", arrival, TZ)     # skipped hour
    fold = resolve.resolve("meet 2026-11-01 at 01:30 America/New_York", arrival, TZ)    # happens twice
    assert gap.blocked == fold.blocked == "other_timezone"


def test_ordinary_unzoned_times_and_place_names_still_resolve():
    assert r("meet tomorrow at 3pm").start == at(22, 15)
    assert r("meet tomorrow at 3pm Pacific Heights").start == at(22, 15)        # a place
    assert r("meet at 3pm Central Park tomorrow").start == at(22, 15)
    assert r("dinner tomorrow at 7pm America/Los_Angeles").start == at(22, 19)  # the user's own zone
