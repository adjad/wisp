"""An email is added without a tap only with proof (autoadd.default_mail_proof): fetched by Wisp's own
Google sign-in, a strictly read sender, a correspondent known from the user's own side, the dated
sentence in that very message, and Gmail's own authentication result. Everything runs through the
production step with a real dated yes. Evidence is injected at its sources: the user's Sent rows,
their Contacts addresses, and gmail_source's store of fetched Authentication-Results. No mail,
Contacts, network or calendar is touched."""
from __future__ import annotations

from pathlib import Path

import pytest

import tests.test_proactive_auto_add as T
from service import gmail_source
from service.proactive import autoadd, mail_known, sources
from tests.test_proactive_auto_add import saved_contacts, store, yes  # noqa: F401

NOW, TZ, ME = T.NOW, T.TZ, T.ME
GMAIL = "me@gmail.com"                              # a Google account linked in Wisp
PRIYA = "priya@friend.com"
INVITE = "Can you come to my recital Sat Oct 31 at 6pm?"
PASS = "mx.google.com; dkim=pass header.i=@friend.com header.s=s1; spf=pass smtp.mailfrom=friend.com; " \
       "dmarc=pass (p=REJECT) header.from=friend.com"
FAIL = "mx.google.com; dkim=fail header.i=@friend.com; spf=softfail smtp.mailfrom=other.net; " \
       "dmarc=fail (p=NONE) header.from=friend.com"


@pytest.fixture(autouse=True)
def mail_switch_on(monkeypatch):
    """MAIL_ACTS ships True since 2026-11-13 (the owner's decision): an email is added without a
    tap only with the proof tested here. Set here too, so these tests say what they rely on; the
    shipped value has its own test at the end of this file."""
    monkeypatch.setattr(autoadd, "MAIL_ACTS", True)


def gmail_row(sender="Priya Shah", address=PRIYA, subject="Recital", body=INVITE, mid="m-1@friend.com",
              ago_h=1.0, account=GMAIL):
    # A Google-fetched row carries the linked address as its account AND its account id.
    return dict(ts=NOW - ago_h * 3600, account=account, account_id=account if "@" in account else "acct-1",
                sender=sender, sender_address=address,
                to=GMAIL, subject=subject, message_id=mid, body=body, unread=True)


def item_for(*rows, linked=frozenset({GMAIL})):
    items = sources.mail_items(list(rows), mine={ME, GMAIL}, now=NOW, tz=TZ, linked=set(linked), own=ME)
    assert len(items) == 1, items
    return items[0]


@pytest.fixture
def world(monkeypatch, yes):
    """A yes two hours ago, and evidence that starts EMPTY: no Sent mail, no contacts, nothing fetched."""
    yes(NOW - 2 * 3600)
    state = {"sent": ([], True), "contacts": set(), "auth": {}, "linked": {GMAIL}}

    async def read_sent(now=None):
        return state["sent"]
    monkeypatch.setattr(mail_known, "read_sent", read_sent)
    monkeypatch.setattr(mail_known, "read_contact_addresses", lambda: state["contacts"])
    monkeypatch.setattr(gmail_source, "_auth", state["auth"])
    monkeypatch.setattr(sources, "gmail_accounts", lambda: set(state["linked"]))
    return state


def fetched(world, headers, *, mid="m-1@friend.com", address=PRIYA, account=GMAIL):
    world["auth"][(account, gmail_source._mid_key(mid))] = {"sender_address": address, "auth_results": list(headers)}


def run(store, item):
    writers = T.Writers()
    out, notes = T.production_step(store, T.survivors(item), writers)
    return [k for k, _ in writers.calls], out, notes


def stays_a_card(store, item, reason):
    wrote, out, notes = run(store, item)
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]
    assert notes == [f"mail: tap to add ({reason})"], notes


def test_the_row_really_is_a_google_fetched_one(world):
    item = item_for(gmail_row())
    assert item["google_fetched"] is True and item["raw_account"] == GMAIL and item["ref"]["to"] == PRIYA
    # A Mail.app copy of the same message under the same address is NOT Google-fetched.
    copy = item_for({**gmail_row(), "account_id": "5A1C-MAIL-UUID", "native_id": "mail:123"})
    assert copy["google_fetched"] is False
    assert item["odd_sender"] is False and item["person"] is True


def test_a_saved_contact_whose_mail_gmail_verified_is_added(store, world):
    world["contacts"] = {PRIYA}
    fetched(world, [PASS])
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == ["calendar"] and len(out["added"]) == 1 and out["tap"] == []
    assert [r["sender_proof"] for r in store.auto_added().values()] == ["gmail"]


def test_someone_the_user_wrote_to_before_whose_mail_gmail_verified_is_added(store, world):
    world["sent"] = ([{"ts": NOW - 5 * 86400, "to": f"Priya Shah <{PRIYA}>", "subject": "hello"}], True)
    fetched(world, [PASS])
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == ["calendar"] and len(out["added"]) == 1


def test_a_contact_with_no_authentication_result_stays_a_card(store, world):
    world["contacts"] = {PRIYA}
    stays_a_card(store, item_for(gmail_row()), "not verified by your mail provider")       # never fetched
    fetched(world, [])
    stays_a_card(store, item_for(gmail_row()), "no header")                                # fetched, no result on it


def test_a_contact_whose_mail_failed_dmarc_stays_a_card(store, world):
    world["contacts"] = {PRIYA}
    fetched(world, [FAIL])
    wrote, out, notes = run(store, item_for(gmail_row()))
    assert wrote == [] and out["added"] == [] and len(out["tap"]) == 1 and notes[0].startswith("mail: tap to add (")


def test_a_stranger_whose_mail_passes_stays_a_card(store, world):
    fetched(world, [PASS])                                             # authentic mail, from nobody the user knows
    stays_a_card(store, item_for(gmail_row()), "not someone you have written to or saved")
    # Having only been written to AFTER the message arrived proves nothing about the sender.
    world["sent"] = ([{"ts": NOW - 60, "to": PRIYA, "subject": "Re: Recital"}], True)
    stays_a_card(store, item_for(gmail_row()), "not someone you have written to or saved")


def test_sent_mail_that_could_not_be_read_fresh_is_not_proof(store, world):
    fetched(world, [PASS])
    world["sent"] = ([{"ts": NOW - 5 * 86400, "to": PRIYA, "subject": "hello"}], False)       # stale
    stays_a_card(store, item_for(gmail_row()), "sent mail could not be checked")
    world["sent"] = (None, False)
    world["contacts"] = None                                                                  # Contacts unreadable too
    stays_a_card(store, item_for(gmail_row()), "sent mail could not be checked")


def test_a_result_the_sender_injected_is_ignored(store, world):
    world["contacts"] = {PRIYA}
    forged = "mail.evil.com; dmarc=pass header.from=friend.com; dkim=pass header.d=friend.com"
    fetched(world, [forged])                                           # only a foreign host's claim
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == [] and len(out["tap"]) == 1
    fetched(world, [FAIL, forged])                                     # Gmail says fail; the sender's line says pass
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == [] and len(out["tap"]) == 1
    fetched(world, [PASS, PASS.replace("friend.com", "friend.com")])   # two results both claiming to be Gmail's
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == [] and len(out["tap"]) == 1
    fetched(world, [forged, PASS])                                     # one real Gmail result, wherever it sits
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == ["calendar"]


def test_a_pass_for_some_other_domain_is_not_a_pass_for_this_sender(store, world):
    world["contacts"] = {PRIYA}
    bulk = "mx.google.com; dkim=pass header.i=@sendgrid.net; spf=pass smtp.mailfrom=bounces.sendgrid.net; " \
           "dmarc=none header.from=friend.com"
    fetched(world, [bulk])
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == [] and len(out["tap"]) == 1


def test_evidence_fetched_for_another_message_or_another_sender_does_not_carry_over(store, world):
    world["contacts"] = {PRIYA}
    fetched(world, [PASS], mid="some-other@friend.com")
    stays_a_card(store, item_for(gmail_row()), "not verified by your mail provider")
    fetched(world, [PASS], address="someone.else@friend.com")           # same Message-ID, a different real From
    stays_a_card(store, item_for(gmail_row()), "sender differs from the fetched message")


def test_mail_that_only_apple_mail_saw_follows_the_apple_mail_rule(store, world):
    """Since 2026-11-13 (the owner's decision) Apple Mail email acts on the known-sender rule
    (service/proactive/mail_sender_proof.py; its own tests are test_autoadd_apple_mail_proof.py).
    A stranger's still never does, and no Gmail proof is borrowed for it."""
    world["linked"] = set()
    fetched(world, [PASS], account="icloud")
    for account in ("iCloud", "me", "Work Exchange"):
        item = item_for(gmail_row(account=account), linked=frozenset())
        assert item["google_fetched"] is False
        stays_a_card(store, item, "not someone you have written to or saved")
    world["contacts"] = {PRIYA}
    wrote, out, _ = run(store, item_for(gmail_row(account="iCloud"), linked=frozenset()))
    assert wrote == ["calendar"] and [r["sender_proof"] for r in store.auto_added().values()] == ["unverified"]


def test_a_date_from_an_older_message_of_the_thread_has_no_proof(store, world):
    world["contacts"] = {PRIYA}
    fetched(world, [PASS], mid="m-2@friend.com")
    first = gmail_row(subject="Recital Sat Oct 31 at 6pm", body="Details soon.", mid="m-1@friend.com", ago_h=1.5)
    reply = gmail_row(subject="Re: Recital Sat Oct 31 at 6pm", body="Bring a friend!", mid="m-2@friend.com", ago_h=1.0)
    item = item_for(first, reply)
    wrote, out, notes = run(store, item)
    assert wrote == [] and out["added"] == []
    assert notes == ["mail: tap to add (the date is not from this message)"], notes


@pytest.mark.parametrize("sender, address", [
    ('"Priya <priya@friend.com>" <x@evil.com>', ""),                    # Apple Mail's raw form
    ("Priya <priya@friend.com> <x@evil.com>", ""),                      # Gmail's quotes-dropped form
    ('Priya <priya@friend.com> <"x y"@evil.com>', ""),                  # a quoted local part
    ('"Priya <priya@friend.com>" <"x>"@evil.com>', ""),
    ("Priya <priya@friend.com>", PRIYA),                                # split fields: an address in the name
    ("Priya <priya@friend.com>", "x@evil.com"),
    ("Priya ＜priya@friend.com＞", "x@evil.com"),                        # full-width brackets
    (f"Me <{GMAIL}> <x@evil.com>", ""),                                 # posing as the user
    ("Priya <priya@friend.com> and friends", ""),                       # junk after the address
])
def test_every_spoofed_sender_shape_adds_nothing_even_with_all_other_proof(store, world, sender, address):
    world["contacts"] = {PRIYA, "x@evil.com"}
    world["sent"] = ([{"ts": NOW - 5 * 86400, "to": f"{PRIYA}, x@evil.com", "subject": "hello"}], True)
    for who in (PRIYA, "x@evil.com", ""):
        fetched(world, [PASS], address=who)
    items = sources.mail_items([gmail_row(sender=sender, address=address)], mine={ME, GMAIL}, now=NOW, tz=TZ,
                               linked={GMAIL}, own=ME)
    for item in items:                                                  # a shape the mail filter drops adds nothing either
        assert item["odd_sender"] is True, sender
        wrote, out, _ = run(store, item)
        assert wrote == [] and out["added"] == [] and out["tap"] == [], sender
    assert store.auto_added() == {}


def test_the_switch_off_beats_every_proof(store, world, monkeypatch):
    world["contacts"] = {PRIYA}
    fetched(world, [PASS])
    monkeypatch.setattr(autoadd, "MAIL_ACTS", False)
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    # With mail not acting, a sender the user has not written to in the thread gets no add-card
    # at all; one they have written to gets the card, and still no write.
    mine = gmail_row(sender="Me", address=GMAIL, subject="Re: Recital", body="ok", mid="m-0@me", ago_h=1.5)
    theirs = gmail_row(subject="Re: Recital", ago_h=1.0)
    item = item_for(mine, theirs)
    assert item["user_wrote"] is True
    wrote, out, notes = run(store, item)
    assert wrote == [] and out["added"] == [] and notes == ["mail: tap to add (mail does not act)"], notes


def test_proof_that_cannot_be_had_is_no_proof(store, world, monkeypatch):
    world["contacts"] = {PRIYA}
    fetched(world, [PASS])

    async def broken(now=None):
        raise RuntimeError("mail bridge down")
    monkeypatch.setattr(mail_known, "read_sent", broken)
    wrote, out, notes = run(store, item_for(gmail_row()))
    assert wrote == [] and len(out["tap"]) == 1 and notes == ["mail: tap to add (proof unavailable: RuntimeError)"]


def test_an_organisation_or_a_roll_up_is_not_a_person(store, world):
    world["contacts"] = {"no-reply@friend.com"}
    fetched(world, [PASS], address="no-reply@friend.com")
    items = sources.mail_items([gmail_row(sender="Friend Co", address="no-reply@friend.com")], mine={ME, GMAIL},
                               now=NOW, tz=TZ, linked={GMAIL}, own=ME)
    for item in items:
        wrote, out, _ = run(store, item)
        assert wrote == [] and out["added"] == []


def test_as_shipped_mail_acts_and_only_with_proof(store, world, monkeypatch):
    import importlib
    source = Path(importlib.import_module("service.proactive.autoadd").__file__).read_text()
    assert autoadd.MAIL_ACTS is True and "\nMAIL_ACTS = True\n" in source      # the real, shipped value
    # Nothing known about the sender, nothing fetched: a card, never a write.
    wrote, out, notes = run(store, item_for(gmail_row()))
    assert wrote == [] and out["added"] == [] and len(out["tap"]) == 1
    # Known AND verified by Gmail: added.
    world["contacts"] = {PRIYA}
    fetched(world, [PASS])
    wrote, out, _ = run(store, item_for(gmail_row()))
    assert wrote == ["calendar"] and len(out["added"]) == 1


def test_a_yes_from_before_email_was_named_does_not_cover_it(store, world, tmp_path):
    import json
    from service.proactive import acting
    world["contacts"] = {PRIYA}
    fetched(world, [PASS])
    when = NOW - 2 * 3600
    (tmp_path / acting.FILE).write_text(json.dumps(
        {"enabled": True, "enabled_at": when, "asked": True, "covers": {"add": when, "hold": when, "follow_up": when}}))
    assert acting.pending() == ["add_more", "add_apple_mail"]
    wrote, out, notes = run(store, item_for(gmail_row()))                # fully proven, and still not added
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    # Not agreed to, and the user has not written in this thread: no card either, exactly as before email could act.
    assert out["tap"] == [] and notes == ["unknown sender: tap to add"]


def test_with_the_switch_off_a_strangers_dated_email_still_gets_no_card(store, world):
    from service.proactive import acting
    acting.set_enabled(False)
    wrote, out, notes = run(store, item_for(gmail_row()))
    assert wrote == [] and out["tap"] == [] and notes == ["unknown sender: tap to add"]


def test_this_file_is_in_the_simulation_manifest():
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_autoadd_mail_proof.py"' in manifest
