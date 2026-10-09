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
                 "dinner at 7, here's the map https://maps.apple.com/?q=Quad",
                 "zoom at 3 https://ucsc.zoom.us/j/123456789",
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


# ------------------------------------------------ times taken from earlier in the thread

def lunch_thread(reply, reply_after=180, proposal="Yo getting lunch at 11:30 lmk if ur coming",
                 proposal_dir="outgoing", proposal_age=0):
    p = msg("p", "Sam", proposal, T - proposal_age, proposal_dir)
    r_ = msg("r", "Sam", reply, T + reply_after)
    h = msg("h", "Sam", "hey", T - 86400 - proposal_age, "outgoing")
    return detector([h, p, r_]), r_


def test_an_acceptance_inherits_the_time_the_user_proposed():
    det, reply = lunch_thread("yea sure i'll meet u guys there")
    d = det.decide(reply)
    assert d.alert and d.resolved.start == at(21, 11, 30)
    assert "time:from_thread" in d.resolved.inferred and not d.resolved.tentative


@pytest.mark.parametrize("kwargs", [
    dict(reply="sorry can't make it"),                                       # a decline
    dict(reply="what time again"),                                           # not an acceptance
    dict(reply="yea sure", proposal_dir="incoming"),                         # their own message echoed
    dict(reply="yea sure", proposal_age=7 * 3600),                           # stale proposal
    dict(reply="yea sure", reply_after=3 * 3600),                            # 11:30 already past
])
def test_inheritance_does_not_fire_when_it_would_be_a_guess(kwargs):
    det, reply = lunch_thread(**kwargs)
    d = det.decide(reply)
    assert not d.alert and d.blocked_by in ("no_time_stated", "no_usable_time")


def test_an_inherited_time_that_is_already_on_the_calendar_stays_silent():
    p = msg("p", "Sam", "lunch at 11:30 lmk if ur coming", T, "outgoing")
    reply = msg("r", "Sam", "yea sure", T + 120)
    d = detector([msg("h", "Sam", "hey", T - 86400, "outgoing"), p, reply],
                 [commit("Lunch with Sam", at(21, 11, 30))]).decide(reply)
    assert d.blocked_by == "already_on_file"


def test_a_bare_clock_twelve_hours_out_is_a_guess():
    evening = datetime(2026, 9, 21, 21, 0, tzinfo=ZONE)
    assert resolve.resolve("meet at 10am", evening, TZ).tentative            # 13h away
    assert not resolve.resolve("meet at 10am", ARRIVAL, TZ).tentative        # 1h away


@pytest.mark.parametrize("text", [
    "Call 800-555-0100 re your Chase card at 3pm or pay at chase-help.com/x",
    "Be at the bank at 3pm, ask for sara@bank-help.example",
    "pick up at 5, details at tinyurl.com/abc123",
    "Meet me at 6, text me at (650) 555-0123",
])
def test_a_lure_with_a_number_link_or_address_never_reaches_the_effect(text):
    m = msg("m", "Group", text, T)
    d = detector([msg("h", "Group", "hey", T - 3600, "outgoing"), m]).decide(m)
    assert not d.alert and d.blocked_by in ("contact_vector", "scam")


def test_dates_and_times_are_not_mistaken_for_phone_numbers_old():
    for text in ("Dinner 2026-09-25 at 7pm", "game 10/12 at 4pm", "meet at 6:30 on 9/21"):
        assert exclusions.screen(msg("m", "Mom", text, T)) is None


@pytest.mark.parametrize("text", [
    "meet at 6 https://maps.apple.com@evil.com/x",                 # userinfo trick
    "meet at 6 https://maps.apple.com.evil.com/x",                 # lookalike suffix
    "zoom at 6 https://ucsc.zoom.us.evil.io/j/1",
    "meet at 6 https://google.com@evil.com/maps",
    "meet at 6 https://evil.com/?u=maps.apple.com",
    "ring me at 6: 8005550100",
    "ring me at 6: (800)555-0100",
    "ring me at 6: 800-5550100",
    "ring me at 6: +44 20 7946 0958",
    "ring me at 6: +1 (650) 555 0123",
    "ring me at 6: 650.555.0123",
    "meet at 6 and check chase-help.com/login",
    "meet at 6 and check bit.ly/3xYz",
    "meet at 6 and read goo.gl/abc",
])
def test_the_contact_screen_cannot_be_bypassed_by_lookalike_hosts_or_number_formats(text):
    assert exclusions.contact_vector(text), text


@pytest.mark.parametrize("text", [
    "meet at 6 https://maps.apple.com/?q=Quad",
    "meet at 6 https://www.google.com/maps/place/Quad",
    "zoom at 3 https://ucsc.zoom.us/j/123456789?pwd=abc",
    "call at 3 meet.google.com/abc-defg-hij",
    "call at 3 https://teams.microsoft.com/l/meetup-join/xyz",
    "Dinner 2026-09-25 at 7pm", "game 10/12 at 4pm", "class 7-9pm", "meet 3.30pm", "room 2-180 at 4",
    "1200 Main St at 6", "10:40-11:45 lab", "countdown 3-2-1 at 8", "gate code 1234 at 7",
])
def test_ordinary_plans_are_not_mistaken_for_lures(text):
    assert not exclusions.contact_vector(text), text


# ---------------------------------------------- the lure-screen notes from the independent review

@pytest.mark.parametrize("text", [
    # bare domains on endings outside any short list
    "meet at 6 and pay at chase-help.ru/login",
    "meet at 6, details at chase.de",
    "meet at 6 shop.biz/deal",
    "meet at 6 x.shop",
    # a URL a browser reads differently from the parser
    "meet at 6 https://evil.com\\.zoom.us/j/1",
    "meet at 6 https://evil.com%2f.zoom.us/j/1",
    "meet at 6 https://mаps.apple.com/x",                  # Cyrillic a
    "meet at 6 https://www.google.com/maps/../url?q=https://evil.com",
    "meet at 6 https://www.google.com/maps%2e%2e/x",
    # phone numbers with other separators
    "ring me at 6: 800–555–0100",                      # en dashes
    "ring me at 6: 800—555—0100",                      # em dashes
    "ring me at 6: 800_555_0100",
    "ring me at 6: 800,555,0100",
    "ring me at 6: 800−555−0100",                      # minus sign
    # addresses and defanged dots
    "meet at 6 1.2.3.4/pay",
    "meet at 6 evil[.]com",
    "meet at 6 evil(.)com/login",
    "meet at 6 evil[dot]com",
])
def test_the_remaining_lure_forms_from_the_review_are_refused(text):
    assert exclusions.contact_vector(text), text


@pytest.mark.parametrize("text", [
    "ucsc.edu portal due 11:59pm",
    "see https://canvas.ucsc.edu/courses/95486 at 5",
    "meet at 6 https://maps.apple.com/?q=Quad",
    "meet at 6 https://www.google.com/maps/place/Quad",
    "Dr. Smith at 3", "St. Mary at 6", "7 p.m. at the gym", "e.g. at 6", "U.S. history at 4",
    "Dinner 2026-09-25 at 7pm", "game 10/12 at 4pm", "meet 3.30pm", "room 2-180 at 4",
])
def test_the_university_and_ordinary_abbreviations_are_not_refused(text):
    assert not exclusions.contact_vector(text), text


@pytest.mark.parametrize("text", [
    "Order #1234567890 pickup at 5pm",              # ten-digit order number
    "2026 09 25 19 30 dinner",                      # space-separated timestamp
    "send notes.pdf at 5",                          # file name
    "dr.smith at 3",                                # no space after the period
    "ok.thanks see you at 6",                       # sentence run together
])
def test_documented_false_positives_are_refused_on_purpose(text):
    """Conservative by design: each costs a reminder and nothing else. Pinned so a change to
    the screen has to decide about them out loud."""
    assert exclusions.contact_vector(text), text


def test_a_refused_lure_never_reaches_the_effect_end_to_end():
    for text in ("Meet me at 6 and pay at chase-help.ru/login", "Meet me at 6 call 800–555–0100"):
        m = msg("m", "Group", text, T)
        d = detector([msg("h", "Group", "hey", T - 3600, "outgoing"), m]).decide(m)
        assert not d.alert and d.blocked_by == "contact_vector", text
