"""Mail memory (service/mail_memory): dates, extraction checks, the queue, the bridge,
idle levels, one full tick, and the endpoints. No model, no Mail, no subprocess:
the app, the model and the Mac's idle/power probes are all stubbed."""
from __future__ import annotations

import asyncio
import base64
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from service.mail_memory import api, bridge, extract, runner
from service.mail_memory import store as store_mod
from service.mail_memory.dates import resolve_due
from service.mail_memory.store import Store

ROOT = Path(__file__).resolve().parents[1]
TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 26, 18, tzinfo=timezone.utc).timestamp()


def _sent(d: str) -> datetime:
    return datetime.fromisoformat(d + "T17:00:00+00:00")


# --- dates ------------------------------------------------------------------------

@pytest.mark.parametrize("sent,phrase,want", [
    ("2026-08-05", "by Friday", "2026-08-07"),           # Gemma said 2026-08-11 in every bench run
    ("2026-04-12", "October 2026", "2026-10-31"),
    ("2026-09-01", "November 19", "2026-11-19"),
    ("2026-06-10", "next week", "2026-06-21"),
    ("2026-07-01", "in two weeks", "2026-07-15"),
    ("2026-08-20", "10/3 at 10am", "2026-10-03"),
    ("2026-12-15", "Jan 5", "2027-01-05"),
    ("2026-05-20", "no later than July 2, 2027", "2027-07-02"),
    ("2026-09-01", "in December", "2026-12-31"),
])
def test_resolve_due(sent, phrase, want):
    assert resolve_due(phrase, _sent(sent), TZ, month_first_order=True).day.isoformat() == want


@pytest.mark.parametrize("phrase", ["moved from November 12 to November 19", "November 12 to November 19", ""])
def test_two_dates_or_none_never_resolve(phrase):
    # The chat resolver picks the FIRST date, which in a correction is the superseded one.
    assert resolve_due(phrase, _sent("2026-09-01"), TZ, month_first_order=True).day is None


def test_day_first_locale():
    assert resolve_due("3/10", _sent("2026-08-20"), TZ, month_first_order=False).day == date(2026, 10, 3)


# --- extraction -------------------------------------------------------------------

RAW = (b"From: Harbor Property <leasing@harbor.example>\r\nTo: Sam <sam@example.com>\r\n"
       b"Subject: Your lease\r\nDate: Wed, 20 May 2026 09:00:00 -0700\r\nMessage-ID: <lease-1@harbor.example>\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Your lease ends on August 31, 2027.\r\nWritten notice must reach us no later than July 2, 2027.\r\n\r\n"
       b"On Tue, May 19, 2026 at 9:00 AM Sam wrote:\r\n> old quoted text about March 3\r\n")


def test_parse_raw_cuts_quoted_history():
    email = extract.parse_raw(RAW, 0)
    assert email.message_id == "<lease-1@harbor.example>"
    assert email.sent.date() == date(2026, 5, 20)
    assert "July 2, 2027" in email.body and "old quoted" not in email.body


def test_parse_raw_html_only():
    raw = (b"From: a@b.example\r\nSubject: x\r\nContent-Type: text/html\r\n\r\n"
           b"<html><style>p{}</style><p>Pay by <b>June&nbsp;1</b></p><script>x()</script></html>")
    body = extract.parse_raw(raw, 1_780_000_000).body
    assert "Pay by June 1" in body and "x()" not in body and "p{}" not in body


def test_tier():
    assert extract.tier({"box": "sent", "bulk": True}) == 1
    assert extract.tier({"box": "inbox", "bulk": False}) == 2
    assert extract.tier({"box": "inbox", "bulk": True, "subject": "Your property tax bill"}) == 3
    assert extract.tier({"box": "inbox", "bulk": True, "subject": "10 fall hikes you'll love"}) == 4
    assert extract.tier({"box": "inbox", "bulk": True, "subject": "Fall sale: 30% off"}) is None


def test_tier_follows_mail_and_gmail_categories():
    # Mail's own category decides: Primary, Transactions, Updates in that order; Promotions never.
    assert [extract.tier({"box": "inbox", "category": c, "bulk": True}) for c in (0, 1, 2, 3)] == [2, 3, 4, None]
    # A Promotions category wins over a transactional-looking subject.
    assert extract.tier({"box": "inbox", "category": 3, "subject": "Your order shipped"}) is None
    # Gmail's Promotions, Social and Forums tabs are dropped even when Mail called it Primary.
    gmail = frozenset({"<p@shop.example>"})
    assert extract.tier({"box": "inbox", "category": 0, "rfc_message_id": "<p@shop.example>"}, gmail) is None
    assert extract.tier({"box": "inbox", "category": 0, "rfc_message_id": "<q@x>"}, gmail) == 2
    # The user's own sent mail is always read.
    assert extract.tier({"box": "sent", "category": 3, "rfc_message_id": "<p@shop.example>"}, gmail) == 1


def test_validate_keeps_only_verbatim_items():
    email = extract.parse_raw(RAW, 0)
    items = [
        {"what": "Give notice if not renewing", "date_phrase": "no later than July 2, 2027",
         "evidence": "Written notice must reach us no later than July 2, 2027"},
        {"what": "Invented", "date_phrase": "July 9, 2027", "evidence": "Written notice must reach us"},
        {"what": "Invented quote", "date_phrase": "July 2, 2027", "evidence": "pay $500 now"},
        {"what": "Lease ends", "date_phrase": "August 31, 2027", "evidence": "Your lease ends on August 31, 2027.",
         "suspicious": True},
    ]
    kept, dropped = extract.validate(items, email, tz=TZ, today=date(2026, 10, 26), month_first=True)
    assert [k["due"] for k in kept] == ["2027-07-02", "2027-08-31"]
    assert kept[1]["suspicious"] is True
    assert dropped == {"date_not_in_email": 1, "quote_not_in_email": 1}


def test_validate_refuses_links_and_tiny_quotes():
    email = extract.parse_raw(RAW, 0)
    base = {"date_phrase": "no later than July 2, 2027", "evidence": "Written notice must reach us no later than July 2, 2027"}
    kept, dropped = extract.validate([
        {**base, "what": "Visit harbor-pay.com to renew"},
        {**base, "what": "Email notice@harbor.example"},
        {**base, "what": "Give notice", "evidence": "notice"},
        {**base, "what": "Give notice", "date_phrase": "J"},
    ], email, tz=TZ, today=date(2026, 10, 26), month_first=True)
    assert kept == [] and dropped == {"link_in_title": 2, "quote_too_short": 2}


def test_validate_drops_long_past():
    email = extract.parse_raw(RAW.replace(b"no later than July 2, 2027", b"by June 1"), 0)
    kept, dropped = extract.validate(
        [{"what": "Notice", "date_phrase": "by June 1", "evidence": "Written notice must reach us by June 1"}],
        email, tz=TZ, today=date(2026, 10, 26), month_first=True)
    assert kept == [] and dropped == {"past": 1}


def test_parse_reply():
    assert extract.parse_reply('```json\n{"items": [{"what": "x"}, 3]}\n```') == [{"what": "x"}]
    assert extract.parse_reply('{"items": []}') == []
    assert extract.parse_reply("I could not find anything.") is None
    assert extract.parse_reply('{"items": "none"}') is None


def test_prompt_says_who_wrote_it_and_wraps_untrusted():
    email = extract.parse_raw(RAW, 0)
    inbox = extract.messages(email, sent_by_user=False, identity="The user is Sam.", today=date(2026, 10, 26))
    sent = extract.messages(email, sent_by_user=True, identity="", today=date(2026, 10, 26))
    assert inbox[0]["content"].startswith("The user is Sam.") and '"I" is the sender' in inbox[0]["content"]
    assert "WRITTEN BY the user" in sent[0]["content"]
    assert inbox[1]["content"].count("untrusted content") == 2
    assert "Today is 2026-10-26" in inbox[0]["content"]



async def test_gmail_tab_ids_cover_every_account_or_none(monkeypatch):
    from service import gmail_source as G
    monkeypatch.setattr(G, "connected_accounts", lambda: {"a@x": {}, "b@x": {}})
    queries = []

    async def list_ids(http, limit, *, query=""):
        queries.append(query)
        return ["1", "2"]

    async def ensure_meta(http, ids):
        account = G._context.get()[0]
        for i in ids:
            G._meta[(account, i)] = {"message_id": f"{account}-{i}@x" if i == "1" else f"<{account}-{i}@x>"}

    monkeypatch.setattr(G, "_list_ids", list_ids)
    monkeypatch.setattr(G, "_ensure_meta", ensure_meta)
    monkeypatch.setattr(G, "_meta", {})
    got = await G.tab_message_ids(90)
    assert got == {"<a@x-1@x>", "<a@x-2@x>", "<b@x-1@x>", "<b@x-2@x>"}
    assert queries[0] == "newer_than:90d {category:promotions category:social category:forums}"

    async def broken(http, ids):
        raise RuntimeError("quota")
    monkeypatch.setattr(G, "_ensure_meta", broken)
    assert await G.tab_message_ids(90) is None             # partial would let promotions through
    monkeypatch.setattr(G, "connected_accounts", lambda: {})
    assert await G.tab_message_ids(90) is None


async def test_gmail_exclusion_failure_falls_back_to_mail_categories(monkeypatch):
    from service import gmail_source as G

    async def boom(days):
        raise RuntimeError("offline")
    monkeypatch.setattr(G, "tab_message_ids", boom)
    assert await runner._gmail_excluded() == frozenset()

# --- store ------------------------------------------------------------------------

def _row(rowid, *, box="inbox", bulk=False, mid=None, ts=1_790_000_000.0, subject="Hello", category=-1, rfc=None):
    mid = mid or f"<m{rowid}@x>"
    return {"rowid": rowid, "account": "A", "box": box, "ts": ts, "sent_ts": ts, "message_id": mid,
            "conversation_id": 1, "subject": subject, "sender": "s@x.example", "sender_name": "S", "bulk": bulk,
            "category": category, "rfc_message_id": mid if rfc is None else rfc}


def test_store_keys_by_rfc_message_id_and_keeps_category(tmp_path):
    st = Store(tmp_path / "k.db")
    st.upsert_index([_row(1, rfc="<a@x>", category=2), _row(2, mid="12", rfc="")], extract.tier, now=1)
    assert st.message("<a@x>")["category"] == 2 and st.message("<a@x>")["tier"] == 4
    assert st.message("mail:12")["rowid"] == 2
    st.upsert_index([_row(1, rfc="<a@x>", category=3)], extract.tier, now=2)
    assert st.message("<a@x>")["category"] == 3                # refreshed; state is not re-decided


def test_store_file_is_private(tmp_path):
    st = Store(tmp_path / "mm" / "mail_memory.db")
    assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(st.path.parent).st_mode) == 0o700


def test_queue_order_rowid_refresh_and_retry(tmp_path):
    st = Store(tmp_path / "q.db")
    got = st.upsert_index([_row(1, ts=1.0), _row(2, box="sent", ts=0.5), _row(3, bulk=True, subject="Sale")],
                          extract.tier, now=10)
    assert got == {"added": 2, "skipped": 1, "seen": 3}
    assert st.next_pending(now=10)["rowid"] == 2              # Sent first
    # A rebuilt Mail index renumbers ROWIDs; the Message-ID keeps the row and its state.
    st.finish("<m2@x>", "done")
    st.upsert_index([_row(20, box="sent", mid="<m2@x>")], extract.tier, now=11)
    assert st.message("<m2@x>")["rowid"] == 20 and st.message("<m2@x>")["state"] == "done"
    st.fail("<m1@x>", "body_timeout", now=100)
    assert st.next_pending(now=100) is None                   # retried an hour later
    assert st.next_pending(now=100 + store_mod.RETRY_S)["rowid"] == 1
    st.fail("<m1@x>", "body_timeout", now=5000)
    assert st.message("<m1@x>")["state"] == "failed"


def test_items_once_and_decisions_stick(tmp_path):
    st = Store(tmp_path / "i.db")
    st.upsert_index([_row(1)], extract.tier, now=1)
    item = {"what": "Pay tax", "date_phrase": "Dec 10", "due": "2026-12-10", "precision": "day",
            "evidence": "due Dec 10", "important": True, "why": "money"}
    assert st.add_items("<m1@x>", [item, item], "v1") == 1
    assert st.add_items("<m1@x>", [{**item, "what": "Phish", "suspicious": True}], "v1") == 1
    assert st.add_items("<m1@x>", [{**item, "what": "Webinar", "important": False,
                                    "why": "held:newsletter_deadline"}], "v1") == 1
    shown = st.suggestions("2026-10-26", horizon="2026-12-31")
    assert [s["what"] for s in shown] == ["Pay tax"]           # suspicious and held items never shown
    assert st.stats()["why"] == {"money": 2, "held:newsletter_deadline": 1}
    assert st.decide(shown[0]["id"], "dismissed") and not st.decide(shown[0]["id"], "accepted")
    assert st.add_items("<m1@x>", [item], "v1") == 0           # a re-extraction can't bring it back
    assert st.suggestions("2026-10-26", horizon="2026-12-31") == []



def _judge(what, evidence, *, box="inbox", bulk=False, category=-1, subject="Hello", precision="day",
           suspicious=False):
    from service.mail_memory import importance
    return importance.judge({"what": what, "evidence": evidence, "date_phrase": "Nov 20", "precision": precision,
                             "suspicious": suspicious},
                            {"box": box, "bulk": bulk, "category": category, "subject": subject, "sender": "x@y"})


def test_importance_bar():
    # Shown: your own promise, a real person, money, an account or document, a booking.
    assert _judge("Send the forecast", "I'll send it Nov 20", box="sent", bulk=True) == (True, "yours")
    assert _judge("Reply to Dana", "can you confirm by Nov 20?", category=0) == (True, "person")
    assert _judge("Pay the water bill", "$42.10 due Nov 20", bulk=True, category=2) == (True, "money")
    assert _judge("Renew passport", "your passport expires Nov 20", bulk=True, category=2) == (True, "account")
    assert _judge("Dentist appointment", "appointment on Nov 20", bulk=True, category=1) == (True, "appointment")
    assert _judge("Return the form", "the form is due Nov 20", bulk=True, category=1) == (True, "deadline")
    # Held: newsletter deadlines, sales, nothing concrete, anything suspicious.
    assert _judge("Register for the webinar", "registration deadline Nov 20", bulk=True, category=2) == (
        False, "held:newsletter_deadline")
    assert _judge("Use the code", "offer ends Nov 20", bulk=True, category=2, subject="Fall sale: 30% off") == (
        False, "held:promotional")
    assert _judge("Read the update", "new features Nov 20", bulk=True, category=2) == (False, "held:no_signal")
    assert _judge("Reply to Dana", "can you confirm by Nov 20?", category=0, suspicious=True) == (
        False, "held:suspicious")
    # A person writing in a bulk-flagged thread is judged as automated mail.
    assert _judge("Lunch", "lunch Nov 20?", bulk=True, category=-1) == (False, "held:no_signal")


@pytest.mark.parametrize("what,evidence,kw,want", [
    # Noise the first bar let through (audit, 2026-10-26).
    ("Use the discount", "$20 off, ends Nov 20", {"subject": "Flash sale"}, "held:promotional"),
    ("Claim it", "save $20, offer ends Nov 20", {}, "held:promotional"),
    ("Read the policy", "our privacy policy changes Nov 20", {}, "held:no_signal"),
    ("Log in", "log in to see what's new by Nov 20", {}, "held:no_signal"),
    ("Workshop", "work-life balance workshop Nov 20", {}, "held:no_signal"),
    ("Webinar", "tax tips webinar Nov 20", {}, "held:no_signal"),
    ("Pay it forward", "pay it forward day is Nov 20", {}, "held:no_signal"),
    ("Visa card", "a Visa card for travelers, Nov 20", {}, "held:no_signal"),
    # Important mail the first bar held back.
    ("Jury duty", "your jury summons for Nov 20", {}, "appointment"),
    ("Permission slip", "return the permission slip by Nov 20", {}, "appointment"),
    ("Doctor", "your appointment on Nov 20 at 3 pm", {}, "appointment"),
    ("Flight", "check-in opens Nov 20", {}, "appointment"),
    ("Library book", "your book is overdue as of Nov 20", {}, "appointment"),
    ("File taxes", "file your return by Nov 20", {}, "money"),
    ("Water bill", "your bill of $42.10 is due Nov 20", {}, "money"),
    ("Renew licence", "your driver's license expires Nov 20", {}, "account"),
    ("Trial", "your free trial ends Nov 20", {}, "account"),
    ("Password", "your password expires Nov 20", {}, "account"),
])
def test_importance_on_automated_mail(what, evidence, kw, want):
    important, why = _judge(what, evidence, bulk=True, category=2, **kw)
    assert why == want and important == (not want.startswith("held:"))


def test_importance_people_and_no_reply_senders():
    from service.mail_memory import importance
    person = {"box": "inbox", "bulk": False, "category": 0, "subject": "Yard sale Saturday!", "sender": "dana@x.example"}
    item = {"what": "Yard sale", "evidence": "yard sale on Nov 20", "date_phrase": "Nov 20", "precision": "day"}
    assert importance.judge(item, person) == (True, "person")              # a friend's "sale" is not a promo
    robot = {**person, "sender": "no-reply@shop.example", "subject": "Hello"}
    assert importance.judge(item, robot) == (False, "held:promotional")


def test_store_adds_new_columns_to_an_older_file(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, key TEXT NOT NULL, what TEXT NOT NULL, "
               "date_phrase TEXT NOT NULL, due TEXT NOT NULL, precision TEXT NOT NULL, evidence TEXT NOT NULL, "
               "suspicious INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'new', created REAL NOT NULL, "
               "decided REAL, reminder TEXT NOT NULL DEFAULT '', extractor TEXT NOT NULL, UNIQUE(key, what, due))")
    db.commit()
    db.close()
    st = Store(path)
    cols = {r["name"] for r in st._db.execute("PRAGMA table_info(items)")}
    assert {"important", "why"} <= cols

def test_expire(tmp_path):
    st = Store(tmp_path / "e.db")
    st.upsert_index([_row(1)], extract.tier, now=1)
    st.add_items("<m1@x>", [{"what": "Old", "date_phrase": "x", "due": "2026-10-01", "precision": "day",
                             "evidence": "x"}], "v1")
    assert st.expire("2026-10-26") == 1
    assert st.stats()["items"] == {"expired": 1}


# --- bridge -----------------------------------------------------------------------

class _Hub:
    def __init__(self):
        self.has_subscribers = True
        self.events = []
        self.answer = None

    async def publish(self, event, *, durable=True, **_):
        assert durable is False, "reads must not be durable (no replay after a restart)"
        self.events.append(event)
        if self.answer:
            asyncio.get_running_loop().call_soon(lambda: bridge.deliver(self.answer(event)))
        return event


@pytest.fixture
def fake_hub(monkeypatch):
    import importlib
    hub_mod = importlib.import_module("service.assistant.hub")   # the package re-exports the instance
    hub = _Hub()
    monkeypatch.setattr(hub_mod, "hub", hub)
    return hub


async def test_bridge_round_trip_and_refusals(fake_hub):
    raw = b"Subject: x\r\n\r\nhello"
    fake_hub.answer = lambda e: {"request_id": e["request_id"], "kind": "body", "ok": True,
                                 "raw_b64": base64.b64encode(raw).decode()}
    assert await bridge.read_body(7) == (raw, "")
    event = fake_hub.events[-1]
    assert event["type"] == "mail_memory_read" and event["rowid"] == 7 and len(event["request_id"]) >= 16
    # A reply for an id nobody is waiting for, or of the wrong kind, is refused.
    assert bridge.deliver({"request_id": event["request_id"], "kind": "body", "ok": True}) is False
    fake_hub.answer = lambda e: {"request_id": e["request_id"], "kind": "index", "ok": True, "rows": []}
    with pytest.raises(bridge.BridgeError):
        await bridge.read_body(7, timeout=0.2)


async def test_bridge_index_rows_are_sanitized(fake_hub):
    fake_hub.answer = lambda e: {"request_id": e["request_id"], "kind": "index", "ok": True, "rows": [
        _row(1), {"rowid": -1, "box": "inbox", "ts": 1}, {"rowid": 2, "box": "junk", "ts": 1}, "x"]}
    rows, capped = await bridge.read_index(90)
    assert [r["rowid"] for r in rows] == [1] and capped is False


async def test_bridge_category_is_sanitized(fake_hub):
    fake_hub.answer = lambda e: {"request_id": e["request_id"], "kind": "index", "ok": True, "rows": [
        _row(1, category=2), _row(2, category=7), _row(3, category="0"), _row(4, category=True)]}
    rows, _ = await bridge.read_index(90)
    assert [r["category"] for r in rows] == [2, -1, -1, -1]


async def test_bridge_needs_the_app(fake_hub):
    fake_hub.has_subscribers = False
    with pytest.raises(bridge.BridgeError, match="app_not_connected"):
        await bridge.read_index(90)


# --- idle levels and settings ------------------------------------------------------

@pytest.fixture
def quiet(monkeypatch):
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 0)
    monkeypatch.setattr(idle, "last_used", lambda key: None)
    monkeypatch.setattr(runner, "_last_own_end", 0.0)
    return idle


def test_levels(quiet, monkeypatch):
    ac, battery_low, battery_ok = (True, 80, False), (False, 20, False), (False, 70, False)
    assert runner.level("m", hid_idle_s=5, power=ac) == "between"
    assert runner.level("m", hid_idle_s=900, power=ac) == "away"
    assert runner.level("m", hid_idle_s=900, power=battery_ok) == "away"
    assert runner.level("m", hid_idle_s=900, power=battery_low) == "busy"
    assert runner.level("m", hid_idle_s=900, power=(True, None, True)) == "busy"   # Low Power Mode
    monkeypatch.setattr(quiet, "last_used", lambda key: 1000.0)                    # someone else, just now
    assert runner.level("m", hid_idle_s=900, power=ac, now=1050.0) == "busy"
    monkeypatch.setattr(runner, "_last_own_end", 999.5)                            # ...that was our own call
    assert runner.level("m", hid_idle_s=900, power=ac, now=1050.0) == "away"
    monkeypatch.setattr(quiet, "foreground_busy", lambda: True)
    assert runner.level("m", hid_idle_s=900, power=ac) == "busy"


def test_mode_defaults_to_live_and_corrupt_means_off(tmp_path):
    path = tmp_path / "s" / "settings.json"
    assert runner.mode(path) == "live"                         # the user's choice for rc2
    runner.set_mode("shadow", path)
    assert runner.mode(path) == "shadow" and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    path.write_text("{not json")
    assert runner.mode(path) == "off"
    with pytest.raises(ValueError):
        runner.set_mode("loud", path)


# --- one full tick ------------------------------------------------------------------

@pytest.fixture
def ticking(tmp_path, monkeypatch, quiet):
    st = Store(tmp_path / "t.db")
    monkeypatch.setattr(store_mod, "_store", st)
    monkeypatch.setattr(runner, "store", lambda: st)
    from service.assistant import attention_runner
    monkeypatch.setattr(attention_runner, "current_zone", lambda: TZ)
    monkeypatch.setattr(runner, "_model_key", lambda: "m")
    monkeypatch.setattr(runner, "_hid_idle_s", lambda: 5.0)
    monkeypatch.setattr(runner, "_power", lambda: (True, 90, False))
    monkeypatch.setattr(runner, "_probe", {})

    async def no_gmail():
        return frozenset()
    monkeypatch.setattr(runner, "_gmail_excluded", no_gmail)
    from service.memory import identity
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **_: "The user is Sam.")
    from service.mail_memory import dates
    monkeypatch.setattr(dates, "month_first", lambda: True)
    return st


async def test_tick_indexes_then_extracts(ticking, monkeypatch):
    st = ticking
    now = datetime(2026, 10, 26, 18, tzinfo=timezone.utc).timestamp()

    async def index(days):
        assert days == runner.WINDOW_DAYS
        return [_row(5, mid="<lease-1@harbor.example>")], False

    async def body(rowid):
        assert rowid == 5
        return RAW, ""

    seen = {}

    async def reply(msgs):
        seen["msgs"] = msgs
        return ('{"items": [{"what": "Give notice if not renewing", "date_phrase": "no later than July 2, 2027",'
                ' "evidence": "Written notice must reach us no later than July 2, 2027", "suspicious": false}]}')

    monkeypatch.setattr(runner, "read_index", index)
    monkeypatch.setattr(runner, "read_body", body)
    monkeypatch.setattr(runner, "_reply", reply)
    assert await runner.tick(now=now) == runner.BETWEEN_GAP_S     # first tick only indexes
    assert st.stats()["queue"] == {"pending": 1}
    assert await runner.tick(now=now + 1) == runner.BETWEEN_GAP_S
    assert st.message("<lease-1@harbor.example>")["state"] == "done"
    shown = st.suggestions("2026-10-26", horizon="2027-12-31")
    assert [(s["due"], s["what"]) for s in shown] == [("2027-07-02", "Give notice if not renewing")]
    assert shown[0]["important"] == 1 and shown[0]["why"] == "person"    # a landlord writing, not a list
    assert "The user is Sam." in seen["msgs"][0]["content"]
    assert await runner.tick(now=now + 2) == runner.IDLE_DONE_GAP_S


async def test_tick_stands_down_without_spending_the_row(ticking, monkeypatch):
    st = ticking
    st.set_meta("last_index", str(NOW))
    st.upsert_index([_row(5, mid="<lease-1@harbor.example>")], extract.tier, now=1)

    async def body(rowid):
        return RAW, ""

    async def reply(msgs):
        return None                                                # a turn started

    monkeypatch.setattr(runner, "read_body", body)
    monkeypatch.setattr(runner, "_reply", reply)
    assert await runner.tick(now=NOW + 1) == runner.BUSY_GAP_S
    row = st.message("<lease-1@harbor.example>")
    assert row["state"] == "pending" and row["attempts"] == 0


@pytest.mark.parametrize("failure", [TimeoutError("slow"), ValueError("reply did not complete (length)")])
async def test_a_failing_email_is_retired_not_retried_forever(ticking, monkeypatch, failure):
    st = ticking
    st.set_meta("last_index", str(NOW))
    st.upsert_index([_row(5, mid="<lease-1@harbor.example>", ts=2.0), _row(6, ts=1.0)], extract.tier, now=1)

    async def body(rowid):
        return RAW, ""

    async def reply(msgs):
        raise failure

    monkeypatch.setattr(runner, "read_body", body)
    monkeypatch.setattr(runner, "_reply", reply)
    assert await runner.tick(now=NOW + 1) == runner.BETWEEN_GAP_S
    assert await runner.tick(now=NOW + 2 + store_mod.RETRY_S) == runner.BETWEEN_GAP_S
    assert st.message("<lease-1@harbor.example>")["state"] == "failed"
    assert st.next_pending(now=NOW + 3 + 2 * store_mod.RETRY_S)["rowid"] == 6      # the queue moves on


async def test_a_body_timeout_is_charged_to_that_email(ticking, monkeypatch):
    st = ticking
    st.set_meta("last_index", str(NOW))
    st.upsert_index([_row(5)], extract.tier, now=1)

    async def body(rowid):
        raise bridge.BridgeError("body_timeout")

    monkeypatch.setattr(runner, "read_body", body)
    assert await runner.tick(now=NOW + 1) == runner.BETWEEN_GAP_S
    assert st.message("<m5@x>")["attempts"] == 1


async def test_a_moved_rowid_refreshes_the_index_instead(ticking, monkeypatch):
    st = ticking
    st.set_meta("last_index", str(NOW))
    st.upsert_index([_row(5)], extract.tier, now=1)             # expects <m5@x>, Mail now has the lease there

    async def body(rowid):
        return RAW, ""

    monkeypatch.setattr(runner, "read_body", body)
    assert await runner.tick(now=NOW + 1) == runner.BETWEEN_GAP_S
    assert st.meta("last_index") == "0" and st.message("<m5@x>")["state"] == "pending"


async def test_reply_stands_down_for_remote_only_and_a_busy_model(monkeypatch, quiet):
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    assert await runner._reply([{"role": "user", "content": "x"}]) is None   # never sends mail off the Mac
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)

    class Client:
        async def loaded_models(self):
            return ["fast-model"]

        def keep_warm(self):
            return []

        async def chat(self, *a, **k):
            raise AssertionError("must not call the model")

    from service.assistant import brief
    from service import config
    monkeypatch.setattr(brief, "_c", lambda: Client())
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast-model")
    monkeypatch.setattr(runner, "_model_key", lambda: "fast-model")
    monkeypatch.setattr(quiet, "in_flight", lambda key: 1)                  # another caller is on the model
    assert await runner._reply([{"role": "user", "content": "x"}]) is None


async def test_tick_skips_mail_not_on_disk_and_waits_when_busy(ticking, monkeypatch, quiet):
    st = ticking
    st.set_meta("last_index", str(NOW))
    st.upsert_index([_row(5)], extract.tier, now=1)

    async def body(rowid):
        return None, "not_on_disk"

    monkeypatch.setattr(runner, "read_body", body)
    assert await runner.tick(now=NOW + 1) == runner.BETWEEN_GAP_S
    assert st.message("<m5@x>")["state"] == "skipped"
    monkeypatch.setattr(quiet, "foreground_busy", lambda: True)
    assert await runner.tick(now=NOW + 2) == runner.BUSY_GAP_S



def test_schedule_waits_for_other_background_model_work(monkeypatch, quiet):
    from service.assistant import mail_gists

    class Running:
        def done(self):
            return False

    monkeypatch.setattr(mail_gists, "_task", Running())
    assert runner._other_background_work()
    monkeypatch.setattr(mail_gists, "_task", None)
    assert not runner._other_background_work()

# --- endpoints ----------------------------------------------------------------------

@pytest.fixture
def client(ticking, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(runner, "SETTINGS_PATH", tmp_path / "mm" / "settings.json")

    async def changed():
        return None

    monkeypatch.setattr(api, "_changed", changed)
    monkeypatch.setattr(api, "store", lambda: ticking)
    app = FastAPI()
    app.include_router(api.router)
    st = ticking
    st.upsert_index([_row(5, box="sent")], extract.tier, now=1)
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    due = (datetime.now(ZoneInfo(TZ)).date() + timedelta(days=10)).isoformat()
    st.add_items("<m5@x>", [{"what": "Send the Q4 forecast", "date_phrase": "by November 20",
                             "due": due, "precision": "day", "evidence": "by November 20",
                             "important": True, "why": "yours"}], "v1")
    return TestClient(app)


J = {"content-type": "application/json"}


def test_shadow_shows_nothing(client):
    assert client.put("/assistant/mail_memory/settings", json={"mode": "shadow"}).json()["mode"] == "shadow"
    got = client.get("/assistant/mail_memory").json()
    assert got["mode"] == "shadow" and got["suggestions"] == [] and got["stats"]["items"] == {"new": 1}
    assert client.post("/assistant/mail_memory/accept", json={"id": 1}).status_code == 409


def test_live_accept_dismiss_and_guards(client, monkeypatch):
    assert client.put("/assistant/mail_memory/settings", json={"mode": "live"}).json()["mode"] == "live"
    shown = client.get("/assistant/mail_memory").json()["suggestions"]
    assert shown[0]["what"] == "Send the Q4 forecast" and shown[0]["from"] == "you"
    assert client.post("/assistant/mail_memory/accept", json={"id": 1},
                       headers={**J, "origin": "https://evil.example"}).status_code == 403
    assert client.post("/assistant/mail_memory/accept", content=b'{"id": 1}',
                       headers={"content-type": "text/plain"}).status_code == 415

    from service.assistant import outbox
    from service.tools import assistant_tools
    calls = []

    async def connected(*a, **k):
        return True

    async def add(title, when_iso, kind="reminder", all_day=False):
        calls.append((title, when_iso, all_day))
        return f"Reminder set: “{title}” — Nov 20 in Apple Reminders and Wisp."

    monkeypatch.setattr(outbox, "app_connected", connected)
    monkeypatch.setattr(assistant_tools, "add_reminder", add)
    monkeypatch.setattr(api, "_on_file", lambda row, tz: False)
    from service import safety
    monkeypatch.setattr(safety, "read_only", lambda: True)
    assert client.post("/assistant/mail_memory/accept", json={"id": 1}).status_code == 403
    monkeypatch.setattr(safety, "read_only", lambda: False)
    got = client.post("/assistant/mail_memory/accept", json={"id": 1}).json()
    assert got["decision"] == "accepted" and got["where"] == "apple_reminders"
    assert calls == [("Send the Q4 forecast", shown[0]["due"], True)]
    assert client.post("/assistant/mail_memory/accept", json={"id": 1}).status_code == 404   # once
    assert client.post("/assistant/mail_memory/dismiss", json={"id": 1}).status_code == 404


def test_accept_runs_once_and_a_failed_add_hands_it_back(client, monkeypatch):
    from service.assistant import outbox
    from service import safety
    from service.tools import assistant_tools

    async def connected(*a, **k):
        return True

    monkeypatch.setattr(outbox, "app_connected", connected)
    monkeypatch.setattr(safety, "read_only", lambda: False)
    monkeypatch.setattr(api, "_on_file", lambda row, tz: False)
    st = api.store()
    assert st.claim(1)                                          # an Add is already running
    assert client.post("/assistant/mail_memory/accept", json={"id": 1}).status_code == 409
    assert client.post("/assistant/mail_memory/dismiss", json={"id": 1}).status_code == 404
    st.release(1)

    async def refused(title, when_iso, kind="reminder", all_day=False):
        return "(Reminders refused it.)"

    monkeypatch.setattr(assistant_tools, "add_reminder", refused)
    assert client.post("/assistant/mail_memory/accept", json={"id": 1}).status_code == 502
    assert st.item(1)["state"] == "new"                         # still offered


def test_live_with_no_time_zone_shows_nothing(client, monkeypatch):
    from service.assistant import attention_runner
    monkeypatch.setattr(attention_runner, "current_zone", lambda: None)
    got = client.get("/assistant/mail_memory")
    assert got.status_code == 200 and got.json()["suggestions"] == []


async def test_gmail_tab_ids_refuse_a_truncated_listing(monkeypatch):
    from service import gmail_source as G
    monkeypatch.setattr(G, "connected_accounts", lambda: {"a@x": {}})

    async def full(http, limit, *, query=""):
        return [str(i) for i in range(limit)]

    async def ensure_meta(http, ids):
        return None

    monkeypatch.setattr(G, "_list_ids", full)
    monkeypatch.setattr(G, "_ensure_meta", ensure_meta)
    assert await G.tab_message_ids(90, limit=5) is None


def test_sync_accepts_only_waiting_requests(client):
    assert client.post("/assistant/sync/mail_memory", json={"request_id": "nobody-asked-for-this",
                                                            "kind": "body", "ok": True}).status_code == 404


# --- wiring -------------------------------------------------------------------------

def test_wiring():
    scheduler = (ROOT / "service/assistant/scheduler.py").read_text()
    main = (ROOT / "service/main.py").read_text()
    overlay = (ROOT / "app/Sources/WispApp/OverlayModel.swift").read_text()
    delegate = (ROOT / "app/Sources/WispApp/AppDelegate.swift").read_text()
    gate = (ROOT / "scripts/run_simulation_qa.py").read_text()
    assert "mail_memory.schedule_tick()" in scheduler
    assert "service.mail_memory.api import router" in main
    assert 'case "mail_memory_read":' in overlay
    assert 'appendingPathComponent("assistant/sync/mail_memory")' in delegate
    assert "tests/MailMemoryReaderRegression.swift" in gate
