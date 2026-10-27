"""Brief cards stay put, and a dismissed one stays gone.

Reported on rc2: "one moment I see three cards, the next 2, another moment 4",
and a dismissed card or a cleared Needs you row coming back. The causes:

- every cycle re-ranked from scratch and a small model does not rank the same
  way twice, so the shown set flipped between runs;
- a dismissal was keyed by the item's content hash, which moves without any
  new message (a text conversation's window slides, a mail body arrives after
  its subject, the sent-mail list arrives late);
- a cycle writes back the cards it read when it started, so a card dismissed
  while it ran was written back;
- clearing a to-do or a thread did not touch the card for the same ask;
- a Needs you text row was named by the first text listed, which changes when
  an older one ages out.

Fake model, fixture sources, scratch store; no live service, no network.
"""
import asyncio
import os
import re
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-card-stability-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest  # noqa: E402

from service.assistant.daily_workspace import needs_you  # noqa: E402
from service.proactive import cycle, sources, stable  # noqa: E402
from service.proactive.store import ProactiveStore  # noqa: E402
from tests.test_proactive_engine import (ADDRESSES, ME, JUDGE, NOW, SCHEDULE, TZ,  # noqa: E402
                                         FakeModel, fake_freeze, mail)

ADDRESSES["m-sam2"] = "sam@example.com"      # fake_freeze looks a message id up here
SAM_BODY = "Hey! Let's go to a coffee chat this week, I'd love to hear about your project."


def sam_rows(body=SAM_BODY, ago_h=20):
    return [mail("m-sam", "Sam Lee", "sam@example.com", "Coffee chat?", body, ago_h)]


def mom_texts(extra_before=(), newest=NOW - 7200):
    return [*extra_before, (newest, "Mom", "Mom: let's talk tmrw")]


def items_for(rows=None, texts=None):
    return (sources.mail_items(rows if rows is not None else sam_rows(), mine={ME}, now=NOW, tz=TZ,
                               linked={JUDGE}, own=ME)
            + sources.text_items(texts if texts is not None else mom_texts(), now=NOW, own=ME))


class Counting(FakeModel):
    """FakeModel that counts each stage and can leave Sam out of the judge's answer."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = {"triage": 0, "judge": 0, "draft": 0}
        self.skip_sam_after = None            # judge call number after which Sam is dropped
        self.during_judge = None              # a callback run while the judge "thinks"

    async def __call__(self, system, material, abort, tokens):
        if "ONE email thread or text conversation" in system:
            self.calls["triage"] += 1
        elif "You pick what the user should act on next" in system:
            self.calls["judge"] += 1
            if self.during_judge is not None:
                self.during_judge()
        elif "You write a short reply" in system:
            self.calls["draft"] += 1
        return await super().__call__(system, material, abort, tokens)

    def _judge(self, material):
        out = super()._judge(material)
        if self.skip_sam_after is not None and self.calls["judge"] > self.skip_sam_after:
            out["items"] = [i for i in out["items"] if "coffee" not in i["title"].casefold()]
        return out


@pytest.fixture
def store(tmp_path):
    cycle._status.update(state="idle", progress={"done": 0, "total": 0}, error="")
    return ProactiveStore(tmp_path / "proactive")


def run(store, model, items, *, now=NOW, force=False, resolve=None, **kw):
    return asyncio.run(cycle.run_cycle(
        now=now, tz=TZ, force=force, reply_fn=model, abort=lambda: False,
        collect=lambda n, z: list(items), schedule_fn=lambda n, z: SCHEDULE, store=store,
        resolve=resolve or (lambda item: "+15551234567" if item.get("title") == "Mom" else None),
        linked={JUDGE}, own=ME, allowed=set(), freeze=fake_freeze, **kw))


def cards(store):
    return store.state().get("cards") or []


def who(store):
    return sorted(c["sources"][0]["who"] for c in cards(store))


def reopen(store):
    """A backend restart: the same files, a fresh process."""
    return ProactiveStore(store.base)


# --- churn ------------------------------------------------------------------------------------


def test_same_inputs_two_cycles_same_cards(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    first = [(c["id"], c["title"], c["action"]["draft"]) for c in cards(store)]
    assert len(first) == 2
    run(store, model, items_for(), force=True)
    assert [(c["id"], c["title"], c["action"]["draft"]) for c in cards(store)] == first


PEOPLE = ["Ann", "Ben", "Cam", "Dee", "Eli", "Fay"]


class Reshuffling(Counting):
    """Answers every cycle with a different idea of who is most urgent."""

    def _judge(self, material):
        lines = re.findall(r"- \[(S\d+)\] (.+)", material)
        ordered = lines if self.calls["judge"] % 2 else list(reversed(lines))
        items = [{"title": "Reply", "action": "reply", "why": "", "due": "", "sources": [sid],
                  "urgency": "high" if n == 0 else "medium"} for n, (sid, _line) in enumerate(ordered)]
        return {"summary": "", "items": items}


def six_people(order=PEOPLE):
    return [(NOW - 600 * (n + 1), name, f"{name}: can you call me tonight?") for n, name in enumerate(order)]


def anyone(item):
    return "+1555000" + str(sum(map(ord, item.get("title") or "")) % 10000).zfill(4)


def test_the_shown_set_does_not_reshuffle_when_the_model_ranks_differently(store):
    """Six people wait, five cards fit; the model changes its mind each cycle."""
    model = Reshuffling()
    items = items_for(rows=[], texts=six_people())
    run(store, model, items, force=True, resolve=anyone)
    first = [c["id"] for c in cards(store)]
    assert len(first) == 5
    for later in range(3):
        run(store, model, items, force=True, resolve=anyone, now=NOW + 60 * (later + 1))
        assert [c["id"] for c in cards(store)] == first, "the cards were reshuffled by a new ranking"


def test_new_cards_are_added_after_the_ones_already_shown(store):
    model = Counting()
    run(store, model, items_for(texts=[]), force=True)
    before = [c["id"] for c in cards(store)]
    assert len(before) == 1
    run(store, model, items_for(), force=True)
    after = [c["id"] for c in cards(store)]
    assert after[0] == before[0] and len(after) == 2


def test_card_id_does_not_move_when_only_the_content_hash_does(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    sam = next(c for c in cards(store) if c["sources"][0]["who"] == "Sam Lee")
    # The mail body arrives after the subject: a different hash, the same ask.
    changed = items_for(rows=sam_rows(SAM_BODY + " Also bring the poster."))
    assert changed[0]["hash"] != next(i for i in items_for() if i["id"] == changed[0]["id"])["hash"]
    run(store, model, changed, force=True)
    again = next(c for c in cards(store) if c["sources"][0]["who"] == "Sam Lee")
    assert again["id"] == sam["id"]
    assert len(cards(store)) == 2


def test_an_unchanged_brief_is_not_judged_or_drafted_again(store):
    model = Counting()
    run(store, model, items_for())
    assert model.calls["judge"] == 1 and model.calls["draft"] == 2
    shown = [(c["id"], c["action"]["draft"]) for c in cards(store)]
    # Half an hour on, nothing new: the judge's reply is checked against now, not asked again.
    run(store, model, items_for(), now=NOW + 31 * 60)
    assert model.calls["judge"] == 1, "the model was asked to rank an unchanged Brief"
    assert model.calls["draft"] == 2, "a reply that was already written was written again"
    assert [(c["id"], c["action"]["draft"]) for c in cards(store)] == shown


def test_new_mail_still_reaches_the_model(store):
    model = Counting()
    run(store, model, items_for(texts=[]))
    assert model.calls["judge"] == 1
    run(store, model, items_for(), now=NOW + 31 * 60)
    assert model.calls["judge"] == 2
    assert who(store) == ["Mom", "Sam Lee"]


def test_a_source_that_drops_out_of_one_read_keeps_its_card_for_a_while(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    assert len(cards(store)) == 2
    run(store, model, [], force=True, now=NOW + 3600)                 # the read came back empty
    assert len(cards(store)) == 2
    run(store, model, items_for(), force=True, now=NOW + 7200)        # and it is back
    assert len(cards(store)) == 2
    run(store, model, [], force=True, now=NOW + 7200 + 3 * 3600)      # gone for good
    run(store, model, [], force=True, now=NOW + 7200 + 3 * 3600 + 60)
    assert cards(store) == []


def test_a_card_is_dropped_once_its_source_reads_as_withheld(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    run(store, model, items_for(rows=sam_rows("Please confirm your SSN 123-45-6789 today.")), force=True)
    assert "Sam Lee" not in who(store)


# --- dismissal --------------------------------------------------------------------------------


def dismiss(store, name):
    card = next(c for c in cards(store) if c["sources"][0]["who"] == name)
    store.dismiss_card(card)
    return card


def test_a_dismissed_card_stays_gone_across_cycles_a_reload_and_a_moved_hash(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    dismissed = dismiss(store, "Sam Lee")
    assert who(store) == ["Mom"]
    run(store, model, items_for(), force=True)
    assert who(store) == ["Mom"]
    store = reopen(store)                                          # a backend restart
    run(store, model, items_for(), force=True)
    assert who(store) == ["Mom"]
    # The hash moves with no new message; the dismissal does not.
    moved = items_for(rows=sam_rows(SAM_BODY + " Also bring the poster."))
    run(store, model, moved, force=True)
    assert who(store) == ["Mom"]
    assert dismissed["asks"] and dismissed["asks"][0] in store.suppressed_fingerprints()
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: SCHEDULE, own=ME, linked={JUDGE})
    assert [c["sources"][0]["who"] for c in view["cards"]] == ["Mom"]
    assert all("Sam" not in t["title"] for t in view["todos"])


def test_a_new_message_in_a_dismissed_thread_is_a_new_card(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    dismiss(store, "Sam Lee")
    store = reopen(store)
    newer = [*sam_rows(), mail("m-sam2", "Sam Lee", "sam@example.com", "Re: Coffee chat?",
                               "Actually, could we do a coffee chat on Thursday instead?", 1)]
    run(store, model, items_for(rows=newer), force=True)
    assert "Sam Lee" in who(store)


def test_a_dismissed_text_card_survives_the_window_sliding(store):
    """A text conversation's hash covers its last lines in a 4 day window: it
    moves when an old line ages out, with no new text."""
    older = (NOW - 3 * 86400, "Mom", "Me: ok mom")
    before = items_for(texts=mom_texts(extra_before=[older]))
    after = items_for(texts=mom_texts())
    mom_before = next(i for i in before if i["source"] == "messages")
    mom_after = next(i for i in after if i["source"] == "messages")
    assert mom_before["hash"] != mom_after["hash"] and mom_before["id"] == mom_after["id"]
    model = Counting()
    run(store, model, before, force=True)
    dismiss(store, "Mom")
    store = reopen(store)
    run(store, model, after, force=True)
    assert "Mom" not in who(store)
    run(store, model, items_for(texts=mom_texts(newest=NOW - 600)), force=True)   # she writes again
    assert "Mom" in who(store)


def test_a_card_dismissed_while_a_cycle_runs_is_not_written_back(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    sam = next(c for c in cards(store) if c["sources"][0]["who"] == "Sam Lee")

    async def tap_lands_late(*_args, **_kw):
        store.dismiss_card(sam)              # after the cycle chose its cards, before it saves them
        return {"tap": [], "items": set()}

    run(store, model, items_for(), force=True, autoadd_fn=tap_lands_late)
    assert "Sam Lee" not in who(store)


def test_clearing_a_todo_or_thread_takes_the_card_for_that_ask_with_it(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    state = store.state()
    sam_todo = next(t for t in state["todos"] if t["who"] == "Sam Lee")
    sam_thread = next(t for t in state["threads"] if t["who"] == "Sam Lee")
    mom_thread = next(t for t in state["threads"] if t["who"] == "Mom")
    assert sam_todo["asks"] and sam_thread["asks"] == sam_todo["asks"]
    # Sam is cleared from the to-do list, Mom from Needs attention.
    store.clear_item(sam_todo["id"], "fp-sam", ask=sam_todo["asks"][0])
    store.clear_item(mom_thread["id"], "fp-mom", ask=mom_thread["asks"][0])
    store = reopen(store)

    def view():
        return cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: SCHEDULE, own=ME, linked={JUDGE})

    seen = view()
    assert seen["cards"] == [], "a cleared ask came back as a card"
    for name in ("Sam Lee", "Mom"):
        assert not [t for t in seen["todos"] if t["who"] == name]
        assert not [t for t in seen["threads"] if t["who"] == name]
    # And through the next cycle, which words its to-dos its own way.
    run(store, model, items_for(), force=True)
    assert who(store) == []
    assert view()["cards"] == [] and not [t for t in view()["todos"] if t["who"] in ("Sam Lee", "Mom")]
    # A new message from Sam is a new ask.
    newer = [*sam_rows(), mail("m-sam2", "Sam Lee", "sam@example.com", "Re: Coffee chat?",
                               "Actually, could we do a coffee chat on Thursday instead?", 1)]
    run(store, model, items_for(rows=newer), force=True)
    assert who(store) == ["Sam Lee"]


def test_the_public_view_does_not_carry_internal_keys(store):
    run(store, Counting(), items_for(), force=True)
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: SCHEDULE, own=ME, linked={JUDGE})
    for row in view["cards"] + view["todos"] + view["threads"]:
        assert not {"asks", "seen_at", "fingerprints"} & set(row)


# --- Needs you (the daily workspace) ----------------------------------------------------------


def text_row(ts, ident, sender="Mom"):
    return dict(id=ident, source="messages", title=f"{sender}: hi", due_ts=None, sender=sender, person=True,
                received_ts=ts, reason="Important conversation context")


def pick(items, dismissed=None):
    return needs_you(items, now=NOW, day_end=NOW + 86400, dismissed=dismissed)


def test_a_cleared_text_row_stays_cleared_when_an_older_text_ages_out():
    both = [text_row(NOW - 3600, "messages:aaaa"), text_row(NOW - 600, "messages:zzzz")]
    row = pick(both)["items"][0]
    assert row["text"] == "2 texts from Mom" and row["id"] == "messages:zzzz"
    cleared = {row["id"]: row["fingerprint"]}
    assert pick(both, cleared)["items"] == []
    # The older text leaves the window: one text now, no news.
    assert pick([text_row(NOW - 600, "messages:zzzz")], cleared)["items"] == []
    # The other order, too: the first one listed is not the row's name.
    assert pick(list(reversed(both)), cleared)["items"] == []


def test_a_new_text_brings_a_cleared_row_back():
    row = pick([text_row(NOW - 3600, "messages:aaaa")])["items"][0]
    cleared = {row["id"]: row["fingerprint"]}
    again = pick([text_row(NOW - 3600, "messages:aaaa"), text_row(NOW - 300, "messages:bbbb")], cleared)
    assert again["items"] and again["items"][0]["text"] == "2 texts from Mom"


def test_ask_key_names_the_conversation_and_its_latest_incoming_message():
    item = dict(id="messages:abc", received_ts=1000.9, hash="h1")
    assert stable.ask_key(item) == "ask:messages:abc@1000"
    assert stable.ask_key({**item, "hash": "h2"}) == stable.ask_key(item)
    assert stable.ask_key({**item, "received_ts": 2000}) != stable.ask_key(item)
    assert stable.ask_key({"id": "x"}) is None and stable.ask_key(None) is None


# --- through the routes the app calls ---------------------------------------------------------


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service.proactive import api
    from service.proactive import store as store_mod
    monkeypatch.setattr(store_mod, "_store", store)
    real = cycle.snapshot
    monkeypatch.setattr(cycle, "snapshot", lambda account=None, **_: real(
        now=NOW, tz=TZ, store=store, own=ME, linked={JUDGE}, account=account, schedule_fn=lambda n, z: SCHEDULE))
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_the_clear_and_dismiss_routes_make_the_ask_permanent(store, client):
    model = Counting()
    run(store, model, items_for(), force=True)
    view = cycle.snapshot()
    sam_card = next(c for c in view["cards"] if c["sources"][0]["who"] == "Sam Lee")
    mom_thread = next(t for t in view["threads"] if t["who"] == "Mom")
    assert client.post(f"/assistant/proactive/cards/{sam_card['id']}/dismiss", json={}).status_code == 200
    assert client.post(f"/assistant/proactive/threads/{mom_thread['id']}/clear",
                       json={"fingerprint": mom_thread["fingerprint"]}).status_code == 200
    for _ in range(2):                                          # a cycle, then a restart and a cycle
        run(store, model, items_for(), force=True)
        store = reopen(store)
    after = cycle.snapshot()
    assert after["cards"] == []
    assert not [t for t in after["todos"] if t["who"] in ("Sam Lee", "Mom")]
    assert not [t for t in after["threads"] if t["who"] == "Mom"]
    # A dismissed card is not a verdict on the thread: that has its own x.
    assert [t["who"] for t in after["threads"]] == ["Sam Lee"]


def test_unchecking_a_to_do_that_was_done_brings_it_back(store):
    model = Counting()
    run(store, model, items_for(), force=True)
    sam = next(t for t in store.state()["todos"] if t["who"] == "Sam Lee")
    store.set_todo_done(sam, True)
    run(store, model, items_for(), force=True)
    assert "Sam Lee" not in who(store)
    store.set_todo_done(sam, False)
    run(store, model, items_for(), force=True)
    assert "Sam Lee" in who(store)
