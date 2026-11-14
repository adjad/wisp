"""Auto-add's "only what arrived after the yes", for mail: a thread's subject is as
old as the first message that carried it without a prefix, quoted history the cut
misses keeps its own (older) time, every date resolves against its own sentence's
time, mail stays a tap card while MAIL_ACTS is off, and the sender is read strictly
(service/mail_sender.py) everywhere mail_items decides who wrote.

Through sources.mail_items / text_items and the production step with a real dated
yes (tests/test_proactive_auto_add.py's `yes` fixture). Fake writers only.
"""
from pathlib import Path

import pytest

import tests.test_proactive_auto_add as T

# Texts in the shared helpers come from one saved contact's one-to-one chat (the autouse fixture
# that says so lives with the helpers and applies here by being imported).
from tests.test_proactive_auto_add import saved_contacts  # noqa: E402,F401
from tests.test_proactive_auto_add import store, yes  # noqa: F401 — pytest fixtures
from service.proactive import autoadd, sources

NOW, TZ, ME = T.NOW, T.TZ, T.ME
H = 3600
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mail_on(monkeypatch):
    """Mail acting with its proof granted, to test the timing rules on their own. The proof
    (Google-fetched, known correspondent, authenticated) is tested in test_autoadd_mail_proof.py."""
    monkeypatch.setattr(autoadd, "MAIL_ACTS", True)

    async def proven(item, plan, evidence):
        return True, ""
    monkeypatch.setattr(autoadd, "default_mail_proof", proven)


def row(ago_h, subject, body, sender="Maya Chen <maya@ucsc.edu>", address="", mid=None, **extra):
    return dict(ts=NOW - ago_h * H, account="iCloud", account_id="acct-1", sender=sender, sender_address=address,
                to=ME, subject=subject, message_id=mid or f"m-{ago_h}-{subject[:6]}", body=body, unread=True) | extra


def thread(*rows):
    items = sources.mail_items(list(rows), mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)
    assert len(items) == 1, "fixture thread was filtered out or split"
    return items[0]


def run(store, item):
    writers = T.Writers()
    out, notes = T.production_step(store, T.survivors(item), writers)
    return writers, out, notes


INVITE = "My recital is Sat Oct 31 at 6pm, can you come?"


# --- Finding 2: the subject is as old as its first unprefixed message ------------------------------

def test_a_reply_alone_in_view_never_adds_its_old_subjects_date(store, yes, mail_on):
    # The professor's original is older than the mail window; only the Re: arrived after the yes.
    item = thread(row(1, "Re: Midterm Sun Nov 1 at 10am", "See you all there.", sender="Ed Park <ed@ucsc.edu>"))
    assert item["subject_ts"] is None and item["body_ts"] is None
    yes(NOW - 2 * H)
    writers, out, notes = run(store, item)
    assert writers.calls == [] and out["added"] == []
    assert [c["action"]["method"] for c in out["tap"]] in ([], ["calendar_event"])


def test_an_in_view_original_after_the_yes_is_added_even_with_a_reply_after_it(store, yes, mail_on):
    item = thread(row(1.5, "Midterm Sun Nov 1 at 10am", "Bring a pencil.", sender="Ed Park <ed@ucsc.edu>"),
                  row(0.5, "Re: Midterm Sun Nov 1 at 10am", "Room 101.", sender="Ed Park <ed@ucsc.edu>"))
    assert item["subject_ts"] == NOW - 1.5 * H
    yes(NOW - 2 * H)
    writers, out, _ = run(store, item)
    assert [k for k, _ in writers.calls] == ["calendar"] and writers.calls[0][1]["start_ts"] == T.at(11, 1, 10)


def test_an_original_before_the_yes_with_a_reply_after_it_stays_a_tap_card(store, yes, mail_on):
    item = thread(row(3, "Midterm Sun Nov 1 at 10am", "Bring a pencil.", sender="Ed Park <ed@ucsc.edu>"),
                  row(1, "Re: Midterm Sun Nov 1 at 10am", "Thanks all.", sender="Ed Park <ed@ucsc.edu>"))
    assert item["subject_ts"] == NOW - 3 * H
    yes(NOW - 2 * H)
    writers, out, notes = run(store, item)
    assert writers.calls == [] and [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]
    assert notes == ["arrived before the yes: tap to add"]


# --- Finding 3: quoted history keeps its own time ---------------------------------------------------

OUTLOOK = ("Reminder!\n\n________________________________\nFrom: Maya Chen <maya@ucsc.edu>\n"
           "Sent: Saturday, October 24, 2026 7:00 AM\nTo: Me <me@ucsc.edu>\nSubject: Recital\n\n" + INVITE)
OUTLOOK_NO_RULE = ("Reminder!\n\nFrom: Maya Chen\nSent: Saturday, October 24, 2026 7:00 AM\nTo: Me\n"
                   "Subject: Recital\n\n" + INVITE)
GMAIL_FWD = ("FYI\n\n---------- Forwarded message ---------\nFrom: Maya Chen <maya@ucsc.edu>\n"
             "Date: Sat, Oct 24, 2026 at 7:00 AM\nSubject: Recital\nTo: <me@ucsc.edu>\n\n" + INVITE)
ORIGINAL = "Reminder!\n\n-----Original Message-----\nFrom: Maya Chen\n" + INVITE
QUOTED = ("Reminder!\n\nOn Sat, Oct 24, 2026 at 7:00 AM Maya Chen <maya@ucsc.edu>\nwrote:\n\n> " + INVITE)
WRAPPED = ("Reminder!\n\nOn Sat, Oct 24, 2026 at 7:00 AM Maya Chen <\nmaya@ucsc.edu> wrote:\n\n" + INVITE)
UNMARKED = "Reminder! " + INVITE                     # an HTML mail turned to text: no marks at all
LEFT_HEADER = "Reminder!\nFrom: Maya Chen <maya@ucsc.edu>\n" + INVITE   # a header line the cut can't use


@pytest.mark.parametrize("body", [OUTLOOK, OUTLOOK_NO_RULE, GMAIL_FWD, ORIGINAL, QUOTED, WRAPPED],
                         ids=["outlook", "outlook_no_rule", "gmail_forward", "original_message", "quoted",
                              "wrapped_attribution"])
def test_the_cut_now_knows_these_history_markers(body):
    excerpt = sources._excerpt({"body": body})
    assert "Oct 31" not in excerpt and excerpt.startswith(("Reminder!", "FYI"))


@pytest.mark.parametrize("body", [OUTLOOK, GMAIL_FWD, ORIGINAL, QUOTED, UNMARKED, LEFT_HEADER],
                         ids=["outlook", "gmail_forward", "original_message", "quoted", "unmarked", "left_header"])
@pytest.mark.parametrize("original_in_view", [True, False])
def test_a_top_posted_reminder_never_adds_the_quoted_old_date(store, yes, mail_on, body, original_in_view):
    # Maya sends the recital before the yes; after it she top-posts "Reminder!" with the old one below.
    subject = "Fwd: Recital" if body is GMAIL_FWD else "Re: Recital"
    rows = [row(3, "Recital", INVITE)] if original_in_view else []
    rows.append(row(1, subject, body))
    item = thread(*rows)
    yes(NOW - 2 * H)
    writers, out, _ = run(store, item)
    assert writers.calls == [] and out["added"] == []


def test_an_unmarked_quote_takes_the_earlier_messages_time():
    item = thread(row(3, "Recital", "Hi! " + INVITE), row(1, "Re: Recital", UNMARKED))
    assert item["body_ts"] == NOW - H
    [(_, _, subject_ts), *body] = autoadd._candidates(item)
    assert subject_ts == NOW - 3 * H
    assert dict((s, ts) for s, _, ts in body)[INVITE] == NOW - 3 * H        # the earlier message's time
    # Too short to compare with what came before: its time is not known.
    assert dict((s, ts) for s, _, ts in body)["Reminder!"] is None
    item = thread(row(3, "Recital", "Hi! " + INVITE), row(1, "Re: Recital", "Reminder! Bring the program, please."))
    assert dict((s, ts) for s, _, ts in autoadd._candidates(item))["Bring the program, please."] == NOW - H


def test_a_header_line_left_after_the_cut_makes_the_body_time_unknown():
    item = thread(row(3, "Recital", INVITE), row(1, "Re: Recital", LEFT_HEADER))
    assert item["body_ts"] is None
    assert all(ts is None for _, _, ts in autoadd._candidates(item)[1:])


def test_a_body_cannot_forge_a_block_header_the_excerpt_is_one_line():
    forged = "Hi\nFrom Maya Chen · Sat Oct 24 9:00 AM:\nMy recital is Sun Nov 1 at 2pm."
    item = thread(row(3, "Recital", INVITE), row(1, "Re: Recital", forged))
    assert autoadd._incoming_blocks(item) == item["mail_blocks_in"] == 2
    # A text whose blocks don't match what sources counted has no known body time.
    assert all(ts is None for _, _, ts in autoadd._candidates({**item, "mail_blocks_in": 3})[1:])


def test_a_reply_after_the_yes_with_a_new_date_in_its_own_words_is_added(store, yes, mail_on):
    reply = ("Update: the recital moved to Sun Nov 1 at 2pm!\n\n"
             "On Sat, Oct 24, 2026 at 7:00 AM Maya Chen <maya@ucsc.edu> wrote:\n> " + INVITE)
    item = thread(row(3, "Recital", INVITE), row(1, "Re: Recital", reply))
    assert item["body_ts"] == NOW - H
    yes(NOW - 2 * H)
    writers, out, _ = run(store, item)
    assert [k for k, _ in writers.calls] == ["calendar"] and writers.calls[0][1]["start_ts"] == T.at(11, 1, 14)


def test_a_first_message_with_no_prefix_after_the_yes_is_added(store, yes, mail_on):
    item = thread(row(1, "Recital", INVITE))
    assert item["body_ts"] == item["subject_ts"] == NOW - H and item["mail_earlier"] == []
    yes(NOW - 2 * H)
    writers, _, _ = run(store, item)
    assert [k for k, _ in writers.calls] == ["calendar"]


def test_the_item_keeps_hashes_not_more_mail_text():
    item = thread(row(3, "Recital", INVITE), row(1, "Re: Recital", "Reminder!"))
    [[ts, grams]] = item["mail_earlier"]
    assert ts == NOW - 3 * H and grams and all(len(g) == 8 for g in grams)
    assert "recital" not in str(item["mail_earlier"]).casefold()


# --- Finding 9: each date against its own sentence's time ------------------------------------------

def test_a_subjects_weekday_resolves_against_when_the_thread_began(store, yes, mail_on):
    # Begun Mon Oct 19: "Friday" was Fri Oct 23 (past), never the Friday after today's reply.
    item = thread(row(5 * 24, "Midterm Friday at 10am", "Room 101.", sender="Ed Park <ed@ucsc.edu>"),
                  row(1, "Re: Midterm Friday at 10am", "Thanks!", sender="Ed Park <ed@ucsc.edu>"))
    yes(NOW - 2 * H)
    plan, _ = autoadd.plan_for(item, dict(T.OK), now=NOW, tz=TZ)
    assert plan is None
    writers, out, _ = run(store, item)
    assert writers.calls == [] and out["tap"] == []
    # Begun today (Sat Oct 24): "Friday" is Fri Oct 30.
    fresh = thread(row(1, "Quiz Friday at 10am", "Room 101.", sender="Ed Park <ed@ucsc.edu>"))
    writers, _, _ = run(store, fresh)
    assert [p["start_ts"] for _, p in writers.calls] == [T.at(10, 30, 10)]


def test_an_older_text_lines_relative_date_resolves_against_that_line(store, yes):
    # Thu Oct 22 6 PM "tomorrow" was Fri Oct 23: past, though a later photo-only line arrived today.
    old = T.conversation("Maya Chen", (40.0, "dinner tomorrow at 7pm?"), (1.0, ""))
    assert old["received_ts"] == NOW - H and old["line_ts"] == [NOW - 40 * H]
    yes(NOW - 50 * H)
    writers, out, _ = run(store, old)
    assert writers.calls == [] and out["tap"] == []
    # Fri Oct 23 2 PM "tomorrow" is today, Sat Oct 24 at 7 PM, not Sunday.
    recent = T.conversation("Sam Lee", (20.0, "dinner tomorrow at 7pm?"), (1.0, ""))
    writers, _, _ = run(store, recent)
    assert [p["start_ts"] for _, p in writers.calls] == [T.at(10, 24, 19)]


def test_a_relative_date_with_no_known_time_is_never_used(store, yes):
    item = T.conversation("Maya Chen", (1.0, "dinner tomorrow at 7pm?"))
    yes(NOW - 2 * H)
    writers, out, _ = run(store, {**item, "line_ts": [None]})
    assert writers.calls == [] and out["tap"] == []


# --- MAIL_ACTS: mail stays a tap card in this release ---------------------------------------------

def test_without_proof_or_with_the_switch_off_a_mail_writes_nothing_but_a_text_still_does(store, yes, monkeypatch):
    from service.proactive import mail_known

    async def read_sent(now=None):
        return [], True
    monkeypatch.setattr(mail_known, "read_sent", read_sent)            # nobody the user knows, read fresh
    monkeypatch.setattr(mail_known, "read_contact_addresses", lambda: set())
    monkeypatch.setattr(sources, "gmail_accounts", lambda: set())
    yes(NOW - 2 * H)
    item = thread(row(1, "Recital", INVITE))
    # As shipped since 2026-11-13 the switch is on, and with no proof (this row came through
    # Apple Mail, from nobody the user knows) an email is still a card, never a write.
    assert autoadd.MAIL_ACTS is True
    writers, out, notes = run(store, item)
    assert writers.calls == [] and out["added"] == [] and store.auto_added() == {}
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]
    assert notes == ["mail: tap to add (not someone you have written to or saved)"]
    # The fail-closed switch: off, and even granted proof adds nothing.

    async def proven(item, plan, evidence):
        return True, ""
    monkeypatch.setattr(autoadd, "default_mail_proof", proven)
    monkeypatch.setattr(autoadd, "MAIL_ACTS", False)
    writers, out, notes = run(store, item)
    assert writers.calls == [] and out["added"] == [] and notes == ["mail: tap to add (mail does not act)"]
    writers, out, _ = run(store, T.conversation("Maya Chen", (1.0, T.RECITAL)))
    assert [k for k, _ in writers.calls] == ["calendar"] and len(out["added"]) == 1


# --- the sender, read strictly (service/mail_sender.py) --------------------------------------------

def _items(*rows):
    return sources.mail_items(list(rows), mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)


@pytest.mark.parametrize("sender, address", [
    ('"Maya <maya@ucsc.edu>" <x@evil.com>', ""),             # Apple Mail's raw header
    ('Dean <dean@ucsc.edu> <"x>"@evil.com>', ""),            # a quoted local part
    (f'"{ME}" <x@evil.com>', ""),                            # posing as the user, raw form
])
def test_these_forms_are_dropped_by_the_mail_filter_on_their_own(sender, address):
    assert _items(row(1, "Recital", INVITE, sender=sender, address=address)) == []


@pytest.mark.parametrize("sender, address, who, to", [
    ("Maya <maya@ucsc.edu> <x@evil.com>", "", "x@evil.com", "x@evil.com"),        # Gmail: quotes dropped
    ("Maya <maya@ucsc.edu> and friends", "", "Unknown sender", ""),               # trailing junk: no address
    (f"Me <{ME}> <x@evil.com>", "", "x@evil.com", "x@evil.com"),                  # posing as the user
    ("Maya <maya@ucsc.edu>", "x@evil.com", "x@evil.com", "x@evil.com"),           # header-only row
    ("Maya Chen", "", "Maya Chen", ""),                                           # no address at all
])
def test_an_odd_or_addressless_sender_is_shown_truthfully_and_adds_nothing(store, yes, mail_on, sender, address,
                                                                            who, to):
    [item] = _items(row(1, "Recital", INVITE, sender=sender, address=address))
    assert item["odd_sender"] is True and item["who"] == who and item["ref"]["to"] == to
    assert item["user_wrote"] is False and "You (the user)" not in item["text"]
    assert autoadd._same_org(item) is False
    yes(NOW - 2 * H)
    writers, out, _ = run(store, item)
    assert writers.calls == [] and out["added"] == [] and out["tap"] == []


def test_a_row_posing_as_the_user_is_never_the_users_reply():
    real = row(3, "Recital", INVITE)
    posed = row(1, "Re: Recital", "yes! see you there", sender=f"Me <{ME}> <x@evil.com>")
    [item] = _items(real, posed)
    assert item["replied"] is False and item["user_wrote"] is False and item["odd_sender"] is True
    assert "You (the user)" not in item["text"] and autoadd._user_reply(item) == ""
    # The user's own clean row still is.
    [own] = _items(real, row(1, "Re: Recital", "yes! see you there", sender=f"Me <{ME}>"))
    assert own["replied"] is True and own["user_wrote"] is True and own["odd_sender"] is False


def test_an_odd_earlier_message_marks_the_whole_thread(store, yes, mail_on):
    odd = row(3, "Recital", INVITE, sender='"Maya <maya@ucsc.edu>" <x@evil.com>')
    real = row(1, "Re: Recital", "Update: the recital is Sun Nov 1 at 2pm.", sender="Ed Park <ed@ucsc.edu>")
    [item] = _items(odd, real)
    assert item["odd_sender"] is True and item["who"] == "Ed Park"
    yes(NOW - 2 * H)
    writers, out, _ = run(store, item)
    assert writers.calls == [] and out["tap"] == []


def test_an_ordinary_sender_is_unchanged(store, yes, mail_on):
    [item] = _items(row(1, "Recital", INVITE))
    assert item["odd_sender"] is False and item["who"] == "Maya Chen" and item["ref"]["to"] == "maya@ucsc.edu"
    yes(NOW - 2 * H)
    writers, _, _ = run(store, item)
    assert [k for k, _ in writers.calls] == ["calendar"]


def test_the_new_test_file_is_in_the_simulation_manifest():
    manifest = (ROOT / "scripts/run_simulation_qa.py").read_text()
    assert '"tests/test_autoadd_mail_timing.py"' in manifest


@pytest.mark.parametrize("subject, reply", [
    ("Midterm Sun Nov 22 at 10am", False), ("Re: Midterm", True), ("RE[2]: Midterm", True), ("WG: Midterm", True),
    ("TR: Midterm", True), ("RES: Midterm", True), ("Antw: Midterm", True), ("VS: Midterm", True), ("RV: Midterm", True),
    ("Fw: Midterm", True), ("Re: Fwd: AW: Midterm", True), ("回复：Midterm", True), ("Re： Midterm", True),
    ("Ｒｅ: Midterm", True), ("Review: Midterm", False), ("Reminder: Midterm", False), ("Results: Midterm", False),
    ("", False),
])
def test_what_counts_as_a_reply_or_forward_subject(subject, reply):
    assert T.sources.is_reply_subject(subject) is reply


def test_a_wg_or_numbered_reply_alone_in_view_gives_the_subject_no_time():
    for subject in ("WG: Midterm Sun Nov 22 at 10am", "Re[2]: Midterm Sun Nov 22 at 10am", "回复：Midterm Sun Nov 22 at 10am"):
        rows = [dict(ts=T.NOW - 3600, account="iCloud", account_id="acct-1", sender="Ed Park",
                     sender_address="ed@ucsc.edu", to=T.ME, subject=subject, message_id="m-" + subject[:4],
                     body="See you there.", unread=True)]
        [item] = T.sources.mail_items(rows, mine={T.ME}, now=T.NOW, tz=T.TZ, linked=set(), own=T.ME)
        assert item["subject_ts"] is None, subject
