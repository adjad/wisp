"""Email read through Apple Mail acts too (service/proactive/mail_sender_proof.py, the owner's
decision of 2026-11-13): a known sender, never the user's own message, both yeses ("add_more" and
"add_apple_mail"), the receiving server's Authentication-Results when the reader has them (a
failing one refuses), and "unverified" recorded when there are none. Gmail's own path is
unchanged (test_autoadd_mail_proof.py). Everything runs through the production step with a real
dated yes; evidence is injected at its sources. No Mail, Contacts, network or calendar is touched.

The Apple Mail reader does not supply `auth_results` / `account_addresses` yet and sources does
not forward them: these tests put them on the item, as sources.mail_items will once it does."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.test_autoadd_mail_proof as G
import tests.test_proactive_auto_add as T
from service import gmail_source, mail_auth
from service.proactive import acting, autoadd, mail_known, mail_sender_proof as P, sources
from tests.test_autoadd_mail_proof import GMAIL, INVITE, PRIYA, gmail_row, mail_switch_on  # noqa: F401
from tests.test_proactive_auto_add import saved_contacts, store, yes  # noqa: F401

NOW, TZ, ME = T.NOW, T.TZ, T.ME
ICLOUD = "me@icloud.com"                              # the user's iCloud account in Apple Mail
# The server's own headers, top-most first. Only the top-most one of the server's is ever read.
IC_PASS = ["dmarc.icloud.com; dmarc=pass header.from=friend.com",
           "dkim-verifier.icloud.com; dkim=pass (2048-bit) header.d=friend.com header.i=@friend.com",
           "spf.icloud.com; spf=pass (spf.icloud.com: domain of friend.com designates 1.2.3.4) "
           "smtp.mailfrom=friend.com"]
BIMI = "bimi.icloud.com; bimi=skipped reason=\"insufficient dmarc\""
IC_FAIL = ["dmarc.icloud.com; dmarc=fail header.from=friend.com",
           "spf.icloud.com; spf=fail smtp.mailfrom=bulk-sender.net"]


@pytest.fixture
def world(monkeypatch, yes, tmp_path):
    """A yes two hours ago naming every action, and evidence that starts EMPTY: no Sent mail, no
    contacts, nothing Gmail fetched, no Google account linked; the user's own addresses fixed."""
    yes(NOW - 2 * 3600)
    state = {"sent": ([], True), "contacts": set(), "auth": {}, "linked": set(), "mine": [ME, ICLOUD, GMAIL]}

    async def read_sent(now=None):
        return state["sent"]
    monkeypatch.setattr(mail_known, "read_sent", read_sent)
    monkeypatch.setattr(mail_known, "read_contact_addresses", lambda: state["contacts"])
    monkeypatch.setattr(gmail_source, "_auth", state["auth"])
    monkeypatch.setattr(sources, "gmail_accounts", lambda: set(state["linked"]))
    monkeypatch.setattr(gmail_source, "CONFIG_PATH", tmp_path / "gmail.json")     # absent: nothing unreadable
    from service.memory import identity
    monkeypatch.setattr(identity, "user_emails", lambda: list(state["mine"]))
    return state


def apple_row(sender="Priya Shah", address=PRIYA, subject="Recital", body=INVITE, mid="m-1@friend.com",
              ago_h=1.0, account="iCloud", to=ICLOUD):
    # A Mail.app row: Mail's own account label and UUID, a "mail:" native id.
    return dict(ts=NOW - ago_h * 3600, account=account, account_id="5A1C-MAIL-UUID", native_id="mail:42",
                sender=sender, sender_address=address, to=to, subject=subject, message_id=mid, body=body,
                unread=True)


def apple_item(*rows, auth=None, addresses=(ICLOUD,), linked=frozenset()):
    items = sources.mail_items(list(rows), mine={ME, ICLOUD}, now=NOW, tz=TZ, linked=set(linked), own=ME)
    assert len(items) == 1, items
    item = dict(items[0])
    assert item["google_fetched"] is False
    if auth is not None:
        item["auth_results"] = list(auth)
    if addresses is not None:
        item["account_addresses"] = list(addresses)
    return item


def fetched(world, headers, *, ago_h=1.0, **kw):
    """Gmail fetched and stored a verdict for a message that arrived `ago_h` hours ago (its
    internalDate, gmail_source._remember_auth); None leaves the time out."""
    G.fetched(world, headers, **kw)
    key = (kw.get("account", GMAIL), gmail_source._mid_key(kw.get("mid", "m-1@friend.com")))
    world["auth"][key]["ts"] = None if ago_h is None else NOW - ago_h * 3600


def run(store, *items):
    writers = T.Writers()
    out, notes = T.production_step(store, T.survivors(*items), writers)
    return [k for k, _ in writers.calls], out, notes


def proofs(store):
    return sorted(r.get("sender_proof") or "" for r in store.auto_added().values())


def stays_a_card(store, item, reason):
    wrote, out, notes = run(store, item)
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]
    assert notes == [f"mail: tap to add ({reason})"], notes


# --- the rule, through the production step -----------------------------------------------------

def test_a_known_sender_with_no_server_result_is_added_and_marked_unverified(store, world):
    world["contacts"] = {PRIYA}
    wrote, out, notes = run(store, apple_item(apple_row()))
    assert wrote == ["calendar"] and len(out["added"]) == 1 and out["tap"] == [] and notes == ["added"]
    assert proofs(store) == ["unverified"]


def test_an_empty_header_list_is_also_unverified(store, world):
    # The reader looked and found none: the same as none supplied. (Known here by Sent mail.)
    world["sent"] = ([{"ts": NOW - 5 * 86400, "to": f"Priya Shah <{PRIYA}>", "subject": "hello"}], True)
    wrote, _out, _ = run(store, apple_item(apple_row(), auth=[]))
    assert wrote == ["calendar"] and proofs(store) == ["unverified"]


def test_a_passing_server_result_is_added_and_marked_verified(store, world):
    world["contacts"] = {PRIYA}
    wrote, out, _ = run(store, apple_item(apple_row(), auth=IC_PASS))
    assert wrote == ["calendar"] and len(out["added"]) == 1 and proofs(store) == ["verified"]


def test_a_failing_server_result_is_never_added(store, world):
    world["contacts"] = {PRIYA}
    stays_a_card(store, apple_item(apple_row(), auth=IC_FAIL), "your mail server's check failed: dmarc fail")
    # spf fail and an unaligned spf pass, no dmarc: no aligned pass.
    unaligned = ["spf.icloud.com; spf=pass smtp.mailfrom=bulk-sender.net", "dkim-verifier.icloud.com; dkim=fail "
                 "header.d=friend.com"]
    stays_a_card(store, apple_item(apple_row(), auth=unaligned), "your mail server's check failed: no aligned pass")


def test_an_unknown_sender_is_never_added_whatever_the_server_says(store, world):
    stays_a_card(store, apple_item(apple_row(), auth=IC_PASS), "not someone you have written to or saved")
    stays_a_card(store, apple_item(apple_row()), "not someone you have written to or saved")
    # Written to only AFTER the message arrived proves nothing.
    world["sent"] = ([{"ts": NOW - 60, "to": PRIYA, "subject": "Re: Recital"}], True)
    stays_a_card(store, apple_item(apple_row()), "not someone you have written to or saved")
    world["sent"] = (None, False)
    stays_a_card(store, apple_item(apple_row()), "sent mail could not be checked")


def test_the_users_own_message_is_never_added(store, world):
    # The user's own address as sources reads it: the thread is the user's own and is no item at all.
    assert sources.mail_items([apple_row(sender="Me", address=ICLOUD)], mine={ME, ICLOUD}, now=NOW, tz=TZ,
                              linked=set(), own=ME) == []
    # An address of the user's that sources was not told about (identity, or the Mail account's own),
    # even saved in Contacts and passing the server's check: never a correspondent.
    world["mine"] = [ME, ICLOUD, GMAIL, "me@work.example"]
    world["contacts"] = {"me@work.example", "alias@icloud.com"}
    item = apple_item(apple_row(sender="Me at work", address="me@work.example"), auth=IC_PASS)
    stays_a_card(store, item, "your own message")
    item = apple_item(apple_row(sender="Me", address="alias@icloud.com"), addresses=(ICLOUD, "alias@icloud.com"))
    stays_a_card(store, item, "your own message")


def _write_yes(tmp_path, covers):
    when = NOW - 2 * 3600
    (tmp_path / acting.FILE).write_text(json.dumps(
        {"enabled": True, "enabled_at": when, "asked": True, "covers": {k: when for k in covers}}))


def test_both_yeses_are_needed(store, world, tmp_path):
    world["contacts"] = {PRIYA}
    # A yes from before Apple Mail was named: email via Gmail, but not Apple Mail.
    _write_yes(tmp_path, ("add", "add_more", "hold", "follow_up"))
    assert acting.pending() == ["add_apple_mail"]
    wrote, out, notes = run(store, apple_item(apple_row()))
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    assert notes == ["arrived before the yes: tap to add"] and len(out["tap"]) == 1
    # A file naming Apple Mail but not "add_more" (no yes ever writes that; only a hand-made file):
    # still nothing, and with email not agreed a stranger-looking thread gets no card either.
    _write_yes(tmp_path, ("add", "add_apple_mail", "hold", "follow_up"))
    wrote, out, notes = run(store, apple_item(apple_row()))
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    # Off: nothing.
    acting.set_enabled(False)
    wrote, out, _ = run(store, apple_item(apple_row()))
    assert wrote == [] and store.auto_added() == {}


def test_the_switch_off_beats_everything(store, world, monkeypatch):
    world["contacts"] = {PRIYA}
    monkeypatch.setattr(P, "APPLE_MAIL_ACTS", False)
    stays_a_card(store, apple_item(apple_row(), auth=IC_PASS), "Apple Mail does not act")
    monkeypatch.setattr(P, "APPLE_MAIL_ACTS", True)
    monkeypatch.setattr(autoadd, "MAIL_ACTS", False)
    wrote, _out, _ = run(store, apple_item(apple_row(), auth=IC_PASS))
    assert wrote == [] and store.auto_added() == {}


def test_as_shipped_apple_mail_acts():
    source = Path(P.__file__).read_text()
    assert P.APPLE_MAIL_ACTS is True and "\nAPPLE_MAIL_ACTS = True\n" in source
    risk = ("Without server-side sender proof, a forged From line that imitates a known address could make\n"
            "Wisp add something. The owner chose this on 2026-11-13")
    assert risk in source
    line = acting.ACTIONS["add_apple_mail"]
    assert "Apple Mail" in line and "forged From line" in line and line.count(". ") == 0 and line.endswith(".")


@pytest.mark.parametrize("sender, address", [
    ('"Priya <priya@friend.com>" <x@evil.com>', ""),
    ("Priya <priya@friend.com>", "x@evil.com"),
    (f"Me <{ICLOUD}> <x@evil.com>", ""),
])
def test_every_spoofed_sender_shape_adds_nothing(store, world, sender, address):
    world["contacts"] = {PRIYA, "x@evil.com"}
    items = sources.mail_items([apple_row(sender=sender, address=address)], mine={ME, ICLOUD}, now=NOW, tz=TZ,
                               linked=set(), own=ME)
    for item in items:
        wrote, out, _ = run(store, {**item, "auth_results": IC_PASS})
        assert wrote == [] and out["added"] == []
    assert store.auto_added() == {}


def test_a_date_from_an_older_message_of_the_thread_is_not_added(store, world):
    world["contacts"] = {PRIYA}
    first = apple_row(subject="Recital Sat Oct 31 at 6pm", body="Details soon.", mid="m-1@friend.com", ago_h=1.5)
    reply = apple_row(subject="Re: Recital Sat Oct 31 at 6pm", body="Bring a friend!", mid="m-2@friend.com", ago_h=1.0)
    wrote, out, notes = run(store, apple_item(first, reply))
    assert wrote == [] and out["added"] == [] and notes == ["mail: tap to add (the date is not from this message)"]


# --- a Google account linked in Wisp ----------------------------------------------------------------

def test_an_apple_mail_copy_of_a_linked_google_accounts_mail_needs_gmails_proof(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    copy = apple_row(account="Google", to=GMAIL)
    stays_a_card(store, apple_item(copy, addresses=(GMAIL,), linked={GMAIL}),
                 "mail of a Google account you linked needs Gmail's proof")
    # Named only by its To header (the account label is not an address): still that account's mail.
    stays_a_card(store, apple_item(copy, addresses=None, linked={GMAIL}),
                 "mail of a Google account you linked needs Gmail's proof")
    # Its own headers cannot stand in for Gmail's verdict.
    stays_a_card(store, apple_item(copy, auth=[G.PASS], addresses=(GMAIL,), linked={GMAIL}),
                 "mail of a Google account you linked needs Gmail's proof")
    # Gmail fetched and verified that very message: the copy may act, on Gmail's proof.
    fetched(world, [G.PASS])
    wrote, _out, _ = run(store, apple_item(copy, addresses=(GMAIL,), linked={GMAIL}))
    assert wrote == ["calendar"] and proofs(store) == ["gmail"]


def test_other_accounts_mail_keeps_the_apple_mail_rule_while_google_is_linked(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    # POSITIVELY another account: its own addresses are named, none linked.
    wrote, _out, _ = run(store, apple_item(apple_row()))                   # iCloud mail, no headers
    assert wrote == ["calendar"] and proofs(store) == ["unverified"]


def test_with_google_linked_an_account_not_shown_to_be_another_one_needs_gmails_proof(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    need = "mail of a Google account you linked needs Gmail's proof"
    # Mail's usual label for a Google account, no account addresses, an empty To (a Bcc), no Gmail
    # verdict (a restart, outside Gmail's sync window): refused, even from a saved contact.
    stays_a_card(store, apple_item(apple_row(account="Google", to=""), addresses=None, linked={GMAIL}), need)
    # The same for any label that is not an address ("iCloud" alone proves nothing).
    stays_a_card(store, apple_item(apple_row(account="iCloud", to=""), addresses=None, linked={GMAIL}), need)
    # Its own passing headers do not stand in for Gmail's verdict.
    stays_a_card(store, apple_item(apple_row(account="Google", to=""), auth=IC_PASS, addresses=None,
                                   linked={GMAIL}), need)
    # No Message-ID: Gmail's verdict cannot be had at all.
    stays_a_card(store, apple_item(apple_row(account="Google", to="", mid=""), addresses=None, linked={GMAIL}), need)
    # A label that is a non-linked address IS another account: the Apple Mail rule.
    wrote, _out, _ = run(store, apple_item(apple_row(account=ICLOUD, to=""), addresses=None, linked={GMAIL}))
    assert wrote == ["calendar"] and proofs(store) == ["unverified"]


def test_with_google_linked_gmails_true_verdict_lets_an_unnamed_account_act(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    fetched(world, [G.PASS])
    wrote, _out, _ = run(store, apple_item(apple_row(account="Google", to=""), addresses=None, linked={GMAIL}))
    assert wrote == ["calendar"] and proofs(store) == ["gmail"]


def test_a_reused_message_id_borrows_no_gmail_verdict(store, world):
    """A forged mail repeating a verified message's From and Message-ID, with a new subject and
    date, into an account whose Mail label is not an address: Gmail's verdict was for another
    message (last week's), so there is none for this one."""
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    need = "mail of a Google account you linked needs Gmail's proof"
    fetched(world, [G.PASS], mid="real-old@friend.com", ago_h=7 * 24)
    forged = apple_row(account="Google", to="", mid="real-old@friend.com", subject="Dinner", ago_h=0.5)
    stays_a_card(store, apple_item(forged, addresses=None, linked={GMAIL}), need)
    stays_a_card(store, apple_item(forged, addresses=(GMAIL,), linked={GMAIL}), need)
    # Just outside the window, and with no time stored at all: no verdict either.
    fetched(world, [G.PASS], mid="real-old@friend.com", ago_h=0.5 + (P.SAME_MESSAGE_S + 60) / 3600)
    stays_a_card(store, apple_item(forged, addresses=None, linked={GMAIL}), need)
    fetched(world, [G.PASS], mid="real-old@friend.com", ago_h=None)
    stays_a_card(store, apple_item(forged, addresses=None, linked={GMAIL}), need)
    # The same message (Gmail's time within minutes of Mail's): it counts.
    fetched(world, [G.PASS], mid="real-old@friend.com", ago_h=0.5 + 120 / 3600)
    wrote, _out, _ = run(store, apple_item(forged, addresses=None, linked={GMAIL}))
    assert wrote == ["calendar"] and proofs(store) == ["gmail"]


def test_gmail_source_stores_gmails_own_time_with_the_evidence(monkeypatch):
    monkeypatch.setattr(gmail_source, "_auth", {})
    gmail_source._remember_auth(GMAIL, "g-1", {"message_id": "<m@x>", "sender_address": PRIYA,
                                                "auth_results": [G.PASS], "ts": 1234.5})
    assert gmail_source._auth[(GMAIL, "m@x")]["ts"] == 1234.5


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "\x00\xff"])
def test_an_unreadable_google_config_is_never_read_as_nothing_linked(store, world, content):
    world["contacts"] = {PRIYA}
    gmail_source.CONFIG_PATH.write_text(content, encoding="utf-8")
    wrote, out, notes = run(store, apple_item(apple_row()))
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    assert len(notes) == 1 and notes[0].startswith("mail: tap to add (proof unavailable: "), notes
    # A readable one is read as before.
    gmail_source.CONFIG_PATH.write_text("{}", encoding="utf-8")
    wrote, _out, _ = run(store, apple_item(apple_row()))
    assert wrote == ["calendar"]


def test_gmail_addresses_compare_in_their_canonical_spelling(store, world):
    adi = "adi@gmail.com"
    assert {P.canonical(a) for a in ("a.di@gmail.com", "adi+x@gmail.com", "adi@googlemail.com", "A.Di+y@GoogleMail.com",
                                     adi)} == {adi}
    assert P.canonical("a.di+x@icloud.com") == "a.di+x@icloud.com"
    world["linked"] = {adi}
    world["mine"] = [ME, ICLOUD, adi]
    world["contacts"] = {PRIYA}
    for spelled in ("a.di@gmail.com", "adi+x@gmail.com", "adi@googlemail.com"):
        # Mail names the account in another spelling of the linked address: still that account's mail.
        stays_a_card(store, apple_item(apple_row(account=spelled, to=""), addresses=None, linked={adi}),
                     "mail of a Google account you linked needs Gmail's proof")
        stays_a_card(store, apple_item(apple_row(to=""), addresses=(spelled,), linked={adi}),
                     "mail of a Google account you linked needs Gmail's proof")
    # The user's own mailbox in another spelling is still the user's own.
    world["contacts"] = {PRIYA, "a.di+x@googlemail.com"}
    stays_a_card(store, apple_item(apple_row(sender="Adi", address="a.di+x@googlemail.com")), "your own message")


def test_step_hands_the_writer_every_yes_the_plan_needs(store, world):
    world["contacts"] = {PRIYA}
    writers = T.Writers()
    T.production_step(store, T.survivors(apple_item(apple_row())), writers)
    assert [(k, p["acts"]) for k, p in writers.calls] == [("calendar", ["add_apple_mail", "add_more"])]
    writers = T.Writers()
    T.production_step(T.ProactiveStore(Path(gmail_source.CONFIG_PATH).parent / "p2"),
                      T.survivors(T.conversation("Maya Chen", (1.0, T.RECITAL))), writers)
    assert [p["acts"] for _k, p in writers.calls] == [["add"]]


@pytest.mark.parametrize("kind", ["calendar", "reminder"])
@pytest.mark.parametrize("revoked", ["add_apple_mail", "add_more"])
def test_a_yes_taken_back_between_the_plan_and_the_write_stops_it(world, tmp_path, monkeypatch, kind, revoked):
    import asyncio
    from service.assistant import attention_calendar, outbox
    from service.proactive import executor
    monkeypatch.setattr(autoadd, "_app_connected_now", lambda: True)
    monkeypatch.setattr(executor, "_reminder_problem", lambda: None)
    seen = []

    async def create_calendar(capture, *, approval, source_current):
        seen.append(source_current())
        return {"ok": seen[-1], "status": "succeeded" if seen[-1] else "blocked", "source_id": "E-1"}

    async def request(kind_, payload, *, before_dispatch=None, **_):
        seen.append(before_dispatch() is None)
        return {"ok": seen[-1], "status": "succeeded" if seen[-1] else "blocked", "source_id": "R-1",
                "title": payload["title"]}
    monkeypatch.setattr(attention_calendar, "create_calendar", create_calendar)
    monkeypatch.setattr(attention_calendar, "approve_capture", lambda capture: "ok")
    monkeypatch.setattr(outbox, "request", request)
    plan = {"kind": kind, "title": "Recital", "fingerprint": "fp", "day": "2026-10-31", "timezone": TZ,
            "start_ts": NOW + 86400, "duration_min": 60, "due_ts": NOW + 86400,
            "acts": ["add_apple_mail", "add_more"]}
    write = autoadd.create_event if kind == "calendar" else autoadd.create_reminder
    assert asyncio.run(write(plan))["ok"] is True and seen == [True]          # every yes still stands
    _write_yes(tmp_path, [k for k in acting.ACTIONS if k != revoked])          # one taken back meanwhile
    out = asyncio.run(write(plan))
    assert out["ok"] is False and out["status"] == "blocked"
    assert seen in ([True, False], [True])                                    # stopped at or before dispatch
    assert acting.enabled("add")                                              # "add" alone would have let it through


def test_add_apple_mail_without_add_more_is_refused_even_for_a_card_worthy_thread(store, world, tmp_path):
    world["contacts"] = {PRIYA}
    mine = apple_row(sender="Me", address=ICLOUD, subject="Recital", body="Are you performing soon?",
                     mid="m-0@icloud.com", ago_h=1.5)
    theirs = apple_row(subject="Re: Recital", mid="m-1@friend.com", ago_h=1.0)
    item = apple_item(mine, theirs)
    assert item["user_wrote"] is True and autoadd._card_worthy(item)      # passes the email-not-agreed gate
    # A hand-made file naming Apple Mail but not "add_more" (no yes ever writes this).
    _write_yes(tmp_path, ("add", "add_apple_mail", "hold", "follow_up"))
    assert acting.enabled("add_apple_mail") and not acting.enabled("add_more")
    wrote, out, notes = run(store, item)
    assert wrote == [] and out["added"] == [] and store.auto_added() == {}
    assert notes == ["arrived before the yes: tap to add"] and len(out["tap"]) == 1
    # With both yeses the same thread is added: the refusal above was the missing "add_more".
    _write_yes(tmp_path, ("add", "add_more", "add_apple_mail", "hold", "follow_up"))
    wrote, _out, _ = run(store, item)
    assert wrote == ["calendar"]


def test_gmails_failing_verdict_for_the_same_message_refuses_the_apple_mail_copy(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    G.fetched(world, [G.FAIL])                                               # Gmail saw it, and it failed
    stays_a_card(store, apple_item(apple_row(), auth=IC_PASS), "your mail provider's check failed")


def test_the_gmail_path_is_unchanged_when_google_is_linked(store, world):
    world["linked"] = {GMAIL}
    world["contacts"] = {PRIYA}
    item = G.item_for(gmail_row())
    assert item["google_fetched"] is True and autoadd._action_of(item, {"source": "mail"}) == "add_more"
    G.stays_a_card(store, item, "not verified by your mail provider")       # no Gmail result: never the Apple rule
    G.fetched(world, [G.PASS])
    wrote, _out, _ = run(store, G.item_for(gmail_row()))
    assert wrote == ["calendar"] and proofs(store) == ["gmail"]


# --- the cap of 10 a pass, across sources ---------------------------------------------------------

def test_ten_a_pass_across_gmail_apple_mail_and_texts(store, world):
    world["linked"] = {GMAIL}
    items, senders = [], set()
    for n in range(5):                                                       # Gmail, proven
        who = f"g{n}@friend.com"
        senders.add(who)
        mid = f"g{n}@friend.com.mid"
        G.fetched(world, [G.PASS.replace("friend.com", "friend.com")], mid=mid, address=who)
        items.append(G.item_for(gmail_row(sender="Gee " + "ABCDE"[n], address=who, subject="Recital " + "ABCDE"[n], mid=mid,
                                          body=f"Can you come to my recital on Nov {n + 2} at 6pm?",
                                          ago_h=1.0 - n * 0.01)))
    for n in range(5):                                                       # Apple Mail, no headers
        who = f"a{n}@friend.com"
        senders.add(who)
        items.append(apple_item(apple_row(sender="Ay " + "ABCDE"[n], address=who, subject="Concert " + "ABCDE"[n], mid=f"a{n}.mid",
                                          body=f"Can you come to my concert on Nov {n + 10} at 7pm?",
                                          ago_h=0.9 - n * 0.01)))
    items.append(T.conversation("Maya Chen", (0.5, T.RECITAL)))              # a text, newest of all
    items.append(apple_item(apple_row(sender="Late", address="late@friend.com", subject="Party", mid="late.mid",
                                      body="Can you come to my party on Nov 20 at 8pm?", ago_h=1.5)))
    senders.add("late@friend.com")
    world["contacts"] = senders
    wrote, out, notes = run(store, *items)
    assert len(wrote) == autoadd.MAX_PER_CYCLE == 10 and len(out["added"]) == 10
    assert notes.count("added") == 10 and notes.count("this pass is full: tap to add") == 2, notes
    assert len(out["tap"]) == 2
    # Newest first: the text, five Apple Mail emails, then four of the five Gmail ones.
    assert proofs(store) == [""] + ["gmail"] * 4 + ["unverified"] * 5


# --- the header rule on its own --------------------------------------------------------------------

FROM = "priya@friend.com"
DOM = P.receiving_domains([ICLOUD])


def verdict(headers, domains=DOM, sender=FROM):
    return P.header_verdict(headers, from_address=sender, domains=domains)


def test_receiving_domains_map_consumer_addresses_to_their_operator():
    assert P.receiving_domains(["x@me.com", "y@mac.com", "z@iCloud.com"]) == {"icloud.com"}
    assert P.receiving_domains(["a@gmail.com"]) == {"google.com"}
    assert P.receiving_domains(["a@ucsc.edu", "junk", ""]) == {"ucsc.edu"}


def test_absent_empty_and_checkless_results_are_absent():
    assert verdict(None)[0] == P.ABSENT and verdict([])[0] == P.ABSENT
    assert verdict(["bimi.icloud.com; bimi=skipped"]) == (P.ABSENT, "no sender check in your mail server's result")
    assert verdict(["mx.icloud.com; none"])[0] == P.ABSENT


def test_only_the_servers_top_most_header_is_read():
    assert verdict(IC_PASS) == (P.PASS, "dmarc pass")
    assert verdict(IC_FAIL) == (P.FAIL, "dmarc fail")
    assert verdict(["dkim-verifier.icloud.com; dkim=pass header.d=friend.com",
                    "dmarc.icloud.com; dmarc=fail header.from=friend.com"]) == (P.PASS, "dkim pass aligned")
    assert verdict(["spf.icloud.com; spf=pass smtp.mailfrom=bounce.friend.com"]) == (P.PASS, "spf pass aligned")
    # A check that says nothing about the sender on top (limit (d)): "unverified", never a pass.
    assert verdict([BIMI] + IC_PASS) == (P.ABSENT, "no sender check in your mail server's result")
    assert verdict([BIMI] + IC_FAIL)[0] == P.ABSENT


def test_a_header_injected_under_the_servers_own_cannot_turn_its_failure_into_a_pass():
    server = "spf.icloud.com; spf=fail smtp.mailfrom=other.net"
    for injected in ("zz.icloud.com; dkim=pass header.d=friend.com",
                     "dkim-verifier.icloud.com; dkim=pass header.d=friend.com",
                     "dmarc.icloud.com; dmarc=pass header.from=friend.com",
                     "mail.evil.com; dmarc=pass header.from=friend.com"):
        assert verdict([server, injected]) == (P.FAIL, "no aligned pass"), injected
        assert verdict([server, injected], domains=set()) == (P.FAIL, "no aligned pass"), injected


def test_folded_lines_mixed_case_and_the_header_name_are_tolerated():
    folded = ["Authentication-Results: DMARC.iCloud.COM;\r\n\tDMARC=PASS\r\n  header.from=Friend.COM"]
    assert verdict(folded) == (P.PASS, "dmarc pass")
    assert verdict(["dmarc.icloud.com;\n dmarc=fail\n header.from=friend.com"]) == (P.FAIL, "dmarc fail")


def test_a_forged_result_lower_in_the_chain_is_ignored():
    forged = "mail.evil.com; dmarc=pass header.from=friend.com; dkim=pass header.d=friend.com"
    # The server's own failure on top wins over the sender's pass below it.
    assert verdict(IC_FAIL + [forged]) == (P.FAIL, "dmarc fail")
    # The sender's forged failure below the server's pass does not refuse either.
    assert verdict(IC_PASS + ["mail.evil.com; dmarc=fail header.from=friend.com"]) == (P.PASS, "dmarc pass")
    # A hop above the server's result (a forwarder) is passed over when the account's domain is known.
    assert verdict(["relay.example.net; dmarc=fail header.from=friend.com"] + IC_PASS) == (P.PASS, "dmarc pass")
    # Junk lower in the chain is ignored too.
    assert verdict(IC_PASS + ["((unterminated"]) == (P.PASS, "dmarc pass")


def test_a_second_result_claiming_the_servers_own_id_refuses():
    forged = "dmarc.icloud.com; dmarc=pass header.from=friend.com"
    assert verdict(IC_FAIL + [forged])[0] == P.FAIL
    assert verdict(IC_PASS + [forged]) == (P.FAIL, "more than one result claims to be your mail server's")
    assert verdict(IC_PASS + [forged], domains=set()) == (P.FAIL, "more than one result claims to be your mail server's")


def test_without_a_known_domain_only_the_top_most_header_counts():
    forged = "mail.evil.com; dmarc=pass header.from=friend.com"
    assert verdict(IC_FAIL + [forged], domains=set()) == (P.FAIL, "dmarc fail")
    assert verdict(["mx.other.net; dmarc=fail header.from=friend.com", forged], domains=set())[0] == P.FAIL
    # The account's domain known but nothing matches it: the top-most, too.
    assert verdict(["mx.other.net; dmarc=pass header.from=friend.com"]) == (P.PASS, "dmarc pass")
    # Junk on top: no reading of it.
    assert verdict(["((unterminated", "mx.other.net; dmarc=pass header.from=friend.com"], domains=set()) \
        == (P.FAIL, "unparseable")


def test_an_id_less_microsoft_result_on_top_is_read():
    ms = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=friend.com; dkim=pass (signature was verified) "
          "header.d=friend.com;dmarc=pass action=none header.from=friend.com;compauth=pass reason=100")
    assert verdict([ms], domains=P.receiving_domains(["me@outlook.com"])) == (P.PASS, "dmarc pass")
    assert verdict([ms.replace("dmarc=pass", "dmarc=fail")], domains=set()) == (P.FAIL, "dmarc fail")


def test_explicit_failures_and_other_domains_refuse():
    assert verdict(["dmarc.icloud.com; dmarc=pass header.from=evil.com"]) == (P.FAIL, "dmarc for another domain")
    assert verdict(["dmarc.icloud.com; dmarc=bestguesspass header.from=friend.com"])[0] == P.FAIL
    assert verdict(["dkim-verifier.icloud.com; dkim=pass header.d=sendgrid.net",
                    "spf.icloud.com; spf=pass smtp.mailfrom=bounces.sendgrid.net"]) == (P.FAIL, "no aligned pass")
    assert verdict(["dmarc.icloud.com; dmarc=pass (comment dmarc=fail) header.from=friend.com"])[0] == P.PASS
    assert verdict(["dkim-verifier.icloud.com; dkim=fail (dmarc=pass header.from=friend.com)"]) \
        == (P.FAIL, "no aligned pass")


def test_unreadable_input_refuses():
    assert verdict("dmarc.icloud.com; dmarc=pass header.from=friend.com")[0] == P.FAIL     # not a list
    assert verdict([42])[0] == P.FAIL
    assert verdict(["x" * (mail_auth.MAX_HEADER_CHARS + 1)])[0] == P.FAIL
    assert verdict(IC_PASS * 20)[0] == P.FAIL                                               # over MAX_HEADERS
    assert verdict(IC_PASS, sender="")[0] == P.FAIL
    assert verdict(["dmarc.icloud.com; dmarc=pass header.from=friend.com; broken"]) == (P.FAIL, "unparseable")


@pytest.mark.parametrize("header", [
    G.PASS, G.FAIL,
    "mx.google.com; dkim=pass header.i=@sendgrid.net; spf=pass smtp.mailfrom=bounces.sendgrid.net; "
    "dmarc=none header.from=friend.com",
    "mx.google.com; dkim=pass header.d=mail.friend.com; spf=neutral smtp.mailfrom=friend.com",
    "mx.google.com; spf=pass smtp.mailfrom=friend.com",
    "mx.google.com; dmarc=pass header.from=other.com",
    "mx.google.com; dmarc=temperror header.from=friend.com; dkim=pass header.d=friend.com",
])
def test_the_same_strictness_as_the_gmail_rule(header):
    gmail_ok, _ = mail_auth.authenticated([header], from_address=FROM, provider="gmail")
    mine, _ = verdict([header], domains=P.receiving_domains([GMAIL]))
    assert (mine == P.PASS) is gmail_ok


def test_this_file_is_in_the_simulation_manifest():
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_autoadd_apple_mail_proof.py"' in manifest
