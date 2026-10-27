"""Which account an item belongs to. The owner's texts, Notes, Reminders,
EventKit calendar and Mail.app mail are the owner's bucket ("me"), even when
a Google account is linked under the owner's own address; a linked account
shows only the mail Google sign-in fetched for it and its gcal: events."""
import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-accounts-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import cycle, sources
from service.proactive.store import ProactiveStore

TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo(TZ)).timestamp()
UCSC = "adnjain@ucsc.edu"          # the owner's own address, also signed in through Google
LINKED = {UCSC}
OWNER = sources.OWN


def row(mid, sender, address, subject, body, *, account, account_id, native_id=None, ago_h=2):
    r = dict(ts=NOW - ago_h * 3600, account=account, account_id=account_id, sender=f"{sender} <{address}>",
             sender_address=address, to=UCSC, subject=subject, message_id=mid, body=body, unread=True)
    if native_id:
        r["native_id"] = native_id
    return r


GMAIL_ROW = row("g1", "Pat Kim", "pat@example.org", "Coffee chat?", "Coffee this week?",
                account=UCSC, account_id=UCSC, native_id="1a16bc55")
MAILAPP_ROW = row("n1", "Sam Lee", "sam@example.com", "Project files?", "Can you send the files?",
                  account=UCSC, account_id="51C0D266-6961-46DD-B8AB-6B4614CA0B95", native_id="mail:22882")
TEXTS = [(NOW - 600, "Sriram Kannan", "Sriram Kannan: are you coming tonight?")]


class Commitments:
    def upcoming(self, start, days=16):
        return [{"id": "c-ek", "title": "Office hours", "when_ts": NOW + 3600, "all_day": 0, "source": "calendar",
                 "kind": "meeting", "account": UCSC, "source_id": "ABC-EVENTKIT-ID"},
                {"id": "c-gcal", "title": "Judge sync", "when_ts": NOW + 7200, "all_day": 0, "source": "calendar",
                 "kind": "meeting", "account": UCSC, "source_id": f"gcal:{UCSC}:evt1"},
                {"id": "r-1", "title": "Call the bank", "when_ts": NOW + 9000, "all_day": 0, "source": "reminders",
                 "kind": "reminder", "account": UCSC, "source_id": "x-apple-reminder://1"}]

    def hidden_commitment_ids(self, now):
        return set()

    def undated_reminders(self):
        return [{"source_id": "u1", "title": "Buy stamps"}]


def test_untagged_and_native_items_land_in_the_owner_bucket():
    assert sources.own_account() == OWNER == "me"
    texts = sources.text_items(TEXTS, now=NOW)
    assert [t["account"] for t in texts] == [OWNER]
    mail = sources.mail_items([GMAIL_ROW, MAILAPP_ROW], mine=set(), now=NOW, tz=TZ, linked=LINKED, own=OWNER)
    by_who = {m["who"]: m["account"] for m in mail}
    assert by_who == {"Pat Kim": UCSC, "Sam Lee": OWNER}
    sched = sources.schedule(Commitments(), NOW, TZ, own=OWNER, linked=LINKED)
    accounts = {r["title"]: r["account"] for r in sched["calendar"] + sched["reminders"]}
    assert accounts == {"Office hours": OWNER, "Judge sync": UCSC, "Call the bank": OWNER, "Buy stamps": OWNER}
    assert cycle.account_list(OWNER, LINKED, [UCSC, "someone@old-build.example"]) == [OWNER, UCSC]


@pytest.fixture
def store(tmp_path):
    cycle._status.update(state="idle", progress={"done": 0, "total": 0}, error="")
    return ProactiveStore(tmp_path / "proactive")


def _run(store):
    async def no_model(system, material, abort, tokens):
        return None
    items = (sources.mail_items([GMAIL_ROW, MAILAPP_ROW], mine=set(), now=NOW, tz=TZ, linked=LINKED, own=OWNER)
             + sources.text_items(TEXTS, now=NOW, own=OWNER)
             + [{"id": "note:1", "source": "notes", "account": OWNER, "who": "", "title": "renew passport",
                 "text": "renew passport", "received_ts": None, "hash": "n", "replied": False}])
    sched = sources.schedule(Commitments(), NOW, TZ, own=OWNER, linked=LINKED)
    asyncio.run(cycle.run_cycle(now=NOW, tz=TZ, force=True, reply_fn=no_model, abort=lambda: False,
                                collect=lambda n, z: list(items), schedule_fn=lambda n, z: sched, store=store,
                                linked=LINKED, own=OWNER, allowed=set()))
    return sched


def _kinds(view):
    return ({c["sources"][0]["kind"] for c in view["cards"]} | {t["source"] for t in view["todos"]}
            | {t["source"] for t in view["threads"]})


def test_a_linked_accounts_view_has_only_its_gmail_and_gcal(store):
    sched = _run(store)
    snap = lambda **kw: cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: sched,  # noqa: E731
                                       own=OWNER, linked=LINKED, **kw)
    whole = snap()
    assert whole["accounts"] == [OWNER, UCSC] and whole["owner"]["id"] == OWNER
    judge = snap(account=UCSC)
    assert _kinds(judge) <= {"mail", "calendar"}
    assert not any(t["who"] in ("Sriram Kannan", "Sam Lee") for t in judge["todos"] + judge["threads"])
    assert [t["title"] for t in judge["todos"] if t["source"] == "calendar"] == ["Judge sync"]
    assert any(t["who"] == "Pat Kim" for t in judge["threads"])
    mine = snap(account=OWNER)
    assert {"messages", "notes", "reminders"} <= _kinds(mine)
    assert not any(t["who"] == "Pat Kim" for t in mine["todos"] + mine["threads"])


def test_an_older_builds_tags_never_leak_texts_into_a_linked_view(store):
    sched = _run(store)
    state = store.state()
    # What the live dry-run saw: the owner's texts tagged with the owner's own address.
    store.save_state(todos=[{**t, "account": UCSC} for t in state["todos"]],
                     threads=[{**t, "account": UCSC} for t in state["threads"]])
    judge = cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: sched, own=OWNER,
                           linked=LINKED, account=UCSC)
    assert not ({"messages", "notes", "reminders"} & _kinds(judge))


def test_erasing_a_linked_account_never_touches_the_owner_bucket(store):
    sched = _run(store)
    before = [t for t in store.state()["todos"] if t["account"] == OWNER]
    store.erase_account(OWNER)
    store.erase_account(UCSC)
    after = store.state()["todos"]
    assert before and [t for t in after if t["account"] == OWNER] == before
    assert not any(t["account"] == UCSC for t in after + store.state()["threads"])
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: sched, own=OWNER, linked=LINKED)
    assert view["accounts"] == [OWNER]
