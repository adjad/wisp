"""Proactive engine quality and audit repairs, from the first real-data run.

Fake model and fixture sources only; nothing native, nothing live.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-quality-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import cycle, executor, guard, judge, prepare, sources, triage
from service.proactive.store import ProactiveStore

_REAL_USER_NAMES = triage.user_names        # the autouse fixture patches it per test

TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=ZoneInfo(TZ)).timestamp()
ME = "adnjain@ucsc.edu"
LINKED = "adijain888@gmail.com"
NAMES = ["Adi Jain", "Adi"]


@pytest.fixture(autouse=True)
def user_is_adi(monkeypatch):
    monkeypatch.setattr(triage, "user_names", lambda: list(NAMES))
    import service.memory.identity as identity
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")
    monkeypatch.setattr(identity, "user_emails", lambda: [ME, LINKED])


@pytest.fixture
def store(tmp_path):
    cycle._status.update(state="idle", progress={"done": 0, "total": 0}, error="")
    return ProactiveStore(tmp_path / "proactive")


def mail(mid, sender, address, subject, body, ago_h, account="adnjain@ucsc.edu", to=ME, **extra):
    return dict(ts=NOW - ago_h * 3600, account=account, account_id="acct-1" if account != LINKED else "",
                sender=f"{sender} <{address}>", sender_address=address, to=to, subject=subject,
                message_id=mid, body=body, unread=True) | extra


GUID_MOM = "iMessage;-;+15551234567"


def mom_texts():
    return [(NOW - 7300, "Mom", "Me: Hey Mom, want to get coffee this week?", GUID_MOM),
            (NOW - 7200, "Mom", "Mom: yes! let's talk tmrw", GUID_MOM)]


class Model:
    def __init__(self, triage_reply=None, judge_reply=None, draft="Sounds good, talk tomorrow!"):
        self.triage_reply, self.judge_reply, self.draft = triage_reply, judge_reply, draft

    async def __call__(self, system, material, abort, tokens):
        if "ONE email thread or text conversation" in system:
            return json.dumps(self.triage_reply(material))
        if "You pick what the user should act on next" in system:
            return json.dumps(self.judge_reply(material))
        return self.draft


def run(store, model, items, sched=None, resolve=None, freeze=None):
    return asyncio.run(cycle.run_cycle(
        now=NOW, tz=TZ, force=True, reply_fn=model, abort=lambda: False, collect=lambda n, z: list(items),
        schedule_fn=lambda n, z: sched or {"calendar": [], "reminders": []}, store=store,
        resolve=resolve or (lambda item: item.get("handle") or None), linked={LINKED}, own=ME, allowed=set(NAMES),
        freeze=freeze or (lambda ref: None)))


def snap(store, **kw):
    return cycle.snapshot(now=NOW, tz=TZ, store=store, own=ME, linked={LINKED},
                          schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, **kw)


# --- 1. the reply target is the other party, never the user ----------------------------


def test_reply_card_names_the_other_party_not_the_user(store):
    """Real failure: "Reply to Adi about the coffee chat" with "Text Mom back?"."""
    items = sources.text_items(mom_texts(), now=NOW, own=ME)
    model = Model(
        triage_reply=lambda m: {"title": "Coffee chat", "summary": "Adi asked Mom about coffee; Mom wants to talk tomorrow.",
                                "junk": False, "sensitive": False, "asks_of_me": "Talk with Mom tomorrow"},
        judge_reply=lambda m: {"summary": "", "items": [
            {"title": "Reply to Adi about the coffee chat", "action": "reply", "why": "Adi asked about coffee.",
             "due": "tmrw", "sources": ["S1"], "urgency": "high"}]})
    run(store, model, items)
    view = snap(store)
    card = next(c for c in view["cards"] if c["action"]["method"] == "text_reply")
    assert card["title"].startswith("Reply to Mom")
    assert card["action"]["button"] == "Text Mom back?"
    assert card["action"]["recipient"] == "+15551234567"
    blob = json.dumps(view)
    assert "Reply to Adi" not in blob and "Adi asked" not in blob
    todo = next(t for t in view["todos"] if t["source"] == "messages")
    assert todo["title"].startswith("Reply to Mom")


def test_a_card_whose_only_party_is_the_user_is_dropped():
    item = {"title": "Reply to Adi", "who": "Adi Jain", "source": "messages", "kind": "reply", "primary": "x",
            "item": {"person": True, "source": "messages", "text": "", "replied": False}, "due_ts": None,
            "fingerprints": ["x:1"], "account": ME}
    state, cards = asyncio.run(prepare.prepare([item], {"calendar": []}, Model(), lambda: False, now=NOW, tz=TZ,
                                               resolve_handle=lambda i: "+15551234567"))
    assert cards == []


# --- 2. second person -----------------------------------------------------------------------


@pytest.mark.parametrize("said, want", [
    ("The user posted a job for a snack bar attendant.", "You posted a job for a snack bar attendant."),
    ("Adi told Trishe they would keep the Wisp idea.", "You told Trishe they would keep the Wisp idea."),
    ("Andrea Hailey asks Adi to help reach more neighbors.", "Andrea Hailey asks you to help reach more neighbors."),
    ("Andrea says Adi's gift will be doubled.", "Andrea says your gift will be doubled."),
    ("The user reminds everyone about the exam.", "You remind everyone about the exam."),
])
def test_summaries_are_written_to_the_user(said, want):
    assert guard.second_person(said, NAMES) == want


def test_triage_summaries_come_back_in_second_person():
    item = sources.text_items([(NOW - 60, "Trishe", "Me: I'll keep the Wisp idea", ""),
                               (NOW - 30, "Trishe", "Trishe: ok sounds good, call me later", "")], now=NOW, own=ME)[0]
    reply = json.dumps({"title": "Wisp idea", "summary": "Adi told Trishe they would keep the Wisp idea.",
                        "junk": False, "sensitive": False, "asks_of_me": "Call Trishe later"})
    result = triage.validate(reply, item, allowed=set(NAMES))
    assert result["summary"] == "You told Trishe you would keep the Wisp idea."


# --- 3. marketing never becomes a thread, to-do or card -------------------------------------------


def test_marketing_and_campaign_texts_are_never_items():
    rows = [
        mail("m1", "Indeed", "alert@indeed.com", "Snack bar attendant at Discovery Bay Golf Club",
             "New jobs for you. Apply now.", 3),
        mail("m2", "Kaggle", "no-reply@kaggle.com", "Competition Launch: Google Quantum AI", "A new competition!", 4),
        mail("m3", "The Princeton Review", "info@review.com", "Spring tests aren't far away.", "Save 20% today.", 5),
        mail("m4", "Vanessa Birrueta-Hernandez", "vanessa@navigateslugsuccess.ucsc.edu",
             "Want to Study Smarter? Attend This Week's Workshops!", "Sign up through the portal.", 6),
        mail("m5", "Psych Advising", "psych@ucsc.edu", "[psychugrads] EVENT: Resource Fair", "Join us Monday.", 7),
        mail("m6", "Sam Lee", "sam@example.com", "Coffee chat?", "Let's go to a coffee chat this week?", 8),
    ]
    items = sources.mail_items(rows, mine={ME, LINKED}, now=NOW, tz=TZ, linked={LINKED}, own=ME)
    assert [i["who"] for i in items] == ["Sam Lee"]
    texts = sources.text_items([(NOW - 60, "36726 (conversation 2)", "36726: Vote YES on Measure I by Nov. 3!", "")],
                               now=NOW, own=ME)
    assert texts == []


def test_organization_mail_without_a_deadline_is_junk_even_if_the_model_says_otherwise():
    item = {"source": "mail", "who": "Tuesday Newsday", "title": "Newsletter", "person": False,
            "text": "Subject: Nicotine and birds\nFrom Tuesday Newsday:\nThe lifelong impacts of nicotine.", "lines": []}
    reply = json.dumps({"title": "Nicotine article", "summary": "Tuesday Newsday covers nicotine.", "junk": False,
                        "sensitive": False, "asks_of_me": "Read the article"})
    assert triage.validate(reply, item, allowed=set())["junk"] is True


# --- 4. a Gmail copy uses the Mail.app row, and threads dedupe ------------------------------------------


def test_gmail_copy_of_a_mail_app_message_replies_through_mail_app():
    body = "Hey! Let's go to a coffee chat this week?"
    rows = [mail("<abc@x>", "Sam Lee", "sam@example.com", "Coffee chat?", body, 5, account=LINKED),
            mail("<abc@x>", "Sam Lee", "sam@example.com", "Coffee chat?", body, 5, account="Google")]
    deduped = sources.dedupe_copies(rows, {LINKED})
    assert len(deduped) == 1 and deduped[0]["account"] == "Google"
    items = sources.mail_items(deduped, mine={ME, LINKED}, now=NOW, tz=TZ, linked={LINKED}, own=ME)
    assert len(items) == 1
    assert items[0]["reply_ref"]["account"] == "Google" and items[0]["account"] == ME


# --- audit 1: the text policy floor ---------------------------------------------------------------------


def _text_card(store, **action):
    card = {"id": "card:t", "title": "Reply to Mom", "account": ME, "sources": [{"kind": "messages", "ref": "m"}],
            "todo_id": "todo:t", "fingerprints": ["m:1"],
            "action": {"method": "text_reply", "recipient": "+15551234567", "draft": "On it", "envelope": {"kind": "text"},
                       **action}}
    store.save_state(cards=[card], todos=[])
    return card


def test_view_only_deny_refuses_a_text_send(store, monkeypatch):
    _text_card(store)
    import service.safety as safety
    from service.safety import Tier
    monkeypatch.setattr(safety, "decide", lambda *a, **k: types.SimpleNamespace(tier=Tier.DENY, reason="View-only mode."))
    import service.tools.action_tools as actions

    async def never(**kw):
        raise AssertionError("must not send")

    monkeypatch.setattr(actions, "send_message", never)
    result = asyncio.run(executor.fire("card:t", "On it", store=store))
    assert result["ok"] is False and result["status"] == "blocked" and "View-only" in result["error"]
    assert store.card("card:t") is not None


# --- audit 2: no second send after an unknown outcome ------------------------------------------------------


def test_a_second_tap_after_an_unknown_text_is_refused(store, monkeypatch):
    _text_card(store)
    calls = []

    async def unknown(card, words):
        calls.append(words)
        return {"ok": False, "status": "unknown"}

    first = asyncio.run(executor.fire("card:t", "On it", store=store, methods={"text_reply": unknown}))
    assert first["status"] == "unknown" and store.card("card:t")["action"]["locked"] == "unknown"
    second = asyncio.run(executor.fire("card:t", "On it", store=store, methods={"text_reply": unknown}))
    assert second["status"] == "locked" and "Check Messages" in second["error"] and calls == ["On it"]


def test_a_second_tap_after_an_unknown_email_is_refused(store, monkeypatch):
    card = {"id": "card:e", "title": "Reply to Sam", "account": ME, "sources": [{"kind": "mail", "ref": "m"}],
            "todo_id": "todo:e", "fingerprints": ["m:1"],
            "action": {"method": "email_reply", "recipient": "sam@example.com", "to": ["sam@example.com"], "cc": [],
                       "subject": "Re: Coffee", "draft": "Hi", "reply_ref": {"message_id": "m", "account": "Google",
                                                                            "account_id": "a"},
                       "envelope": {"kind": "email_reply_ref"}}}
    store.save_state(cards=[card], todos=[])
    executor.remember("card:e", "sealed:1", NOW + 10_000_000)
    sent = []
    module = types.ModuleType("service.assistant.email_reply_api")
    module.freeze_reply = lambda ref: {"ok": True, "can_send": True, "envelope": "sealed:2", "to": ["sam@example.com"],
                                       "cc": [], "subject": "Re: Coffee", "expires_at": NOW + 10_000_000}

    async def send_reviewed_reply(envelope, body):
        sent.append(envelope)
        return {"ok": False, "status": "unknown", "error": "unconfirmed"}

    module.send_reviewed_reply = send_reviewed_reply
    monkeypatch.setitem(sys.modules, "service.assistant.email_reply_api", module)
    first = asyncio.run(executor.fire("card:e", "Hi", store=store))
    second = asyncio.run(executor.fire("card:e", "Hi", store=store))
    assert first["status"] == "unknown" and second["status"] == "locked" and "Check Mail" in second["error"]
    assert sent == ["sealed:1"]


def test_an_outbound_unknown_text_result_is_reported_unknown(store, monkeypatch):
    _text_card(store)
    import service.safety as safety
    from service.safety import Tier
    monkeypatch.setattr(safety, "decide", lambda *a, **k: types.SimpleNamespace(tier=Tier.CONFIRM, reason=""))
    import service.tools.action_tools as actions
    from service.tools.registry import _OutboundUnknownResult

    async def maybe(**kw):
        return _OutboundUnknownResult("Outcome unknown.", operation="send_message", action_id="a")

    monkeypatch.setattr(actions, "send_message", maybe)
    result = asyncio.run(executor.fire("card:t", "On it", store=store))
    assert result["status"] == "unknown" and store.card("card:t")["action"]["locked"] == "unknown"


# --- audit 3: the text recipient is the chat's own participant -----------------------------------------------


def test_text_recipient_comes_from_the_chat_participant():
    item = sources.text_items(mom_texts(), now=NOW, own=ME)[0]
    assert item["handle"] == "+15551234567"
    one = lambda name: [{"name": "Mom", "handles": ["(555) 123-4567"], "preferred": "+15551234567"}]  # noqa: E731
    assert cycle.resolve_handle(item, find_contacts=one) == "+15551234567"
    other = lambda name: [{"name": "Mom", "handles": ["+15559990000"], "preferred": "+15559990000"}]  # noqa: E731
    assert cycle.resolve_handle(item, find_contacts=other) is None


def test_same_name_contacts_get_no_text_card():
    item = sources.text_items(mom_texts(), now=NOW, own=ME)[0]
    two = lambda name: [{"name": "Mom", "handles": ["+15551234567"], "preferred": "+15551234567"},  # noqa: E731
                        {"name": "Mom", "handles": ["+15557654321"], "preferred": "+15557654321"}]
    assert cycle.resolve_handle(item, find_contacts=two) is None


def test_a_single_member_named_chat_gets_no_text_card():
    rows = [(NOW - 60, "Book club", "Priya: are you coming tmrw?", "iMessage;+;chat123456789")]
    item = sources.text_items(rows, now=NOW, own=ME)[0]
    assert item["handle"] == ""
    any_contact = lambda name: [{"name": "Book club", "handles": ["+15551234567"], "preferred": "+15551234567"}]  # noqa: E731
    assert cycle.resolve_handle(item, find_contacts=any_contact) is None
    assert sources.chat_handle({"iMessage;-;chat99"}) == ""
    assert sources.chat_handle({"iMessage;-;+15551234567", "SMS;-;+15551234567"}) == ""


# --- audit 4: native JSON requests only -------------------------------------------------------------------------


def test_fire_refuses_an_origin_header_and_a_non_json_body(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.proactive import api, store as store_mod
    monkeypatch.setattr(store_mod, "_store", store)
    _text_card(store)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    path = "/assistant/proactive/cards/card:t/fire"
    assert client.post(path, json={"draft": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(path, json={"draft": "x"}, headers={"Origin": "null"}).status_code == 403
    assert client.post(path, content="draft=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    for other in ("/assistant/proactive/cards/card:t/dismiss", "/assistant/proactive/todos/todo:t",
                  "/assistant/proactive/analyze"):
        assert client.post(other, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post(other, content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert store.card("card:t") is not None


# --- the day overview -----------------------------------------------------------------------------------------------


def test_day_summary_reads_naturally():
    todos = [{"source": "calendar", "due_ts": NOW + 6300, "title": "CruzHacks Hack Day", "done": False, "timed": True},
             {"source": "calendar", "due_ts": NOW + 7200, "title": "Hacktoberfest x CruzHacks (Hack Day Santa Cruz)",
              "done": False, "timed": True},
             {"source": "calendar", "due_ts": NOW - 36000, "title": "Week 4 Reading", "done": False, "timed": False}]
    threads = [{"who": "Mom", "needs_reply": True}, {"who": "Sam", "needs_reply": True}, {"who": "Pat", "needs_reply": True}]
    text = cycle._day_summary([], todos, threads, NOW, TZ)
    assert text == ("Two events today — CruzHacks Hack Day at 11:45 and Hacktoberfest x CruzHacks at noon. "
                    "Three people are waiting on a reply.")


# --- re-audit: the unknown lock survives the next cycle ----------------------------------------------


def _cycle_items():
    rows = [mail("<sam@x>", "Sam Lee", "sam@example.com", "Coffee chat?", "Let's go to a coffee chat this week?", 5,
                 account="Google")]
    return (sources.mail_items(rows, mine={ME, LINKED}, now=NOW, tz=TZ, linked={LINKED}, own=ME)
            + sources.text_items(mom_texts(), now=NOW, own=ME))


def _cycle_model():
    def tri(m):
        if "coffee" in m.casefold():
            return {"title": "Coffee chat", "summary": "Sam Lee wants a coffee chat this week.", "junk": False,
                    "sensitive": False, "asks_of_me": "Reply to Sam about the coffee chat"}
        return {"title": "Talk tomorrow", "summary": "Mom wants to talk tomorrow.", "junk": False,
                "sensitive": False, "asks_of_me": "Reply to Mom"}
    return Model(triage_reply=tri, judge_reply=lambda m: {"summary": "", "items": [
        {"title": "Reply", "action": "reply", "why": "", "due": "", "sources": [s], "urgency": "high"}
        for s in ("S1", "S2")]})


def _freeze(ref):
    return {"ok": True, "can_send": True, "envelope": "sealed:" + ref["message_id"], "to": ["sam@example.com"],
            "cc": [], "subject": "Re: Coffee chat?", "expires_at": NOW + 10_000_000}


@pytest.mark.parametrize("method", ["email_reply", "text_reply"])
def test_an_unknown_lock_survives_the_next_cycle(store, monkeypatch, method):
    run(store, _cycle_model(), _cycle_items(), freeze=_freeze)
    card = next(c for c in snap(store)["cards"] if c["action"]["method"] == method)
    sends = []

    async def unknown(card, words):
        sends.append(words)
        return {"ok": False, "status": "unknown"}

    assert asyncio.run(executor.fire(card["id"], "Hi", store=store, methods={method: unknown}))["status"] == "unknown"
    run(store, _cycle_model(), _cycle_items(), freeze=_freeze)          # a re-judge rebuilds every card
    rebuilt = next(c for c in snap(store)["cards"] if c["id"] == card["id"])
    assert rebuilt["action"]["locked"] == "unknown"
    again = asyncio.run(executor.fire(card["id"], "Hi", store=store, methods={method: unknown}))
    assert again["status"] == "locked" and sends == ["Hi"]
    # Dismiss clears it (and the card is gone for good).
    store.dismiss_card(store.card(card["id"]))
    assert card["id"] not in store.unknown_cards()


def test_mark_handled_clears_the_unknown_lock(store):
    card = _text_card(store)
    store.mark_unknown(card)
    store.set_todo_done({"id": "todo:t", "fingerprints": ["m:1"]}, True)
    assert "card:t" not in store.unknown_cards()


def test_surname_is_never_rewritten(monkeypatch):
    import service.memory.identity as identity
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")     # restored after the test
    real = _REAL_USER_NAMES()
    assert real == ["Adi Jain", "Adi"]
    assert guard.second_person("Trishala Jain asked Adi to call on Fridays.", real) == \
        "Trishala Jain asked you to call on Fridays."
    assert guard.second_person("Adi Fridays are busy.", ["Adi"]) == "You Fridays are busy."


# --- live run 2479 ------------------------------------------------------------------------------------------


def test_old_checkpoints_are_read_again_under_new_rules(store, monkeypatch):
    items = sources.text_items(mom_texts(), now=NOW, own=ME)
    store.triage_put(items[0]["id"], items[0]["hash"], {"junk": False, "sensitive": False, "title": "Old",
                     "summary": "Adi told Mom something.", "asks_of_me": ""}, status="ok")   # pre-version key
    seen = []

    async def model(system, material, abort, tokens):
        seen.append("ONE email thread or text conversation" in system)
        return await _cycle_model()(system, material, abort, tokens)

    run(store, model, items)
    assert any(seen)                     # the pre-version checkpoint was not served: it was read again
    thread = snap(store)["threads"][0]
    assert "Adi" not in thread["summary"]


def test_incoming_mail_credits_the_sender_not_you():
    item = {"source": "mail", "who": "Ed", "title": "Exam", "person": False, "user_wrote": False,
            "text": "Subject: Exam reminder\nFrom Ed:\nExam 1 is next Wednesday, closed-book. Exam prep is due.",
            "lines": []}
    reply = json.dumps({"title": "Exam reminder", "summary": "The user sent a reminder that Exam 1 is next Wednesday.",
                        "junk": False, "sensitive": False, "asks_of_me": ""})
    assert triage.validate(reply, item, allowed=set())["summary"] == "Ed sent a reminder that Exam 1 is next Wednesday."
    assert triage.attribute("The user told Mom hi.", NAMES, "Mom", True) == "You told Mom hi."


def test_meta_summaries_are_dropped():
    item = sources.text_items(mom_texts(), now=NOW, own=ME)[0]
    reply = json.dumps({"title": "Talk", "summary": "Mom sent this to 3 people and it is not sensitive.",
                        "junk": False, "sensitive": False, "asks_of_me": "Talk with Mom tomorrow"})
    assert triage.validate(reply, item, allowed=set())["summary"] == ""


def test_a_waiting_contact_gets_a_reply_card_even_when_the_judge_omits_it(store):
    items = sources.text_items(mom_texts(), now=NOW, own=ME)
    model = Model(triage_reply=lambda m: {"title": "Chat", "summary": "Mom wants to talk tomorrow.", "junk": True,
                                          "sensitive": False, "asks_of_me": "Talk with Mom tomorrow"},
                  judge_reply=lambda m: {"summary": "", "items": []})
    run(store, model, items)
    card = next(c for c in snap(store)["cards"])
    assert card["action"]["method"] == "text_reply" and card["title"].startswith("Reply to Mom")
    assert card["action"]["button"] == "Text Mom back?"


def test_vague_read_the_email_todos_are_dropped():
    assert judge._VAGUE.match("Read the email from Dhruv")
    item = {"source": "mail", "who": "Dhruv Kolte", "title": "Floor updates", "person": True, "replied": False}
    assert judge.action_title("task", item, {"asks_of_me": "Read the email from Dhruv", "title": "Floor updates"},
                              NAMES) == ""


# --- audit follow-up: withheld chats, per-conversation lock --------------------------------------------------


def test_withheld_conversations_never_become_cards_or_quotes(store):
    rows = [(NOW - 120, "Mom", "Mom: Authentication details omitted.", GUID_MOM),
            (NOW - 60, "Mom", "Mom: Private details omitted.", GUID_MOM)]
    items = sources.text_items(rows, now=NOW, own=ME)
    assert cycle._any_result(items[0], store, {}) is None

    async def offline(system, material, abort, tokens):
        return None

    run(store, offline, items)
    assert snap(store)["cards"] == []
    assert "details omitted" not in "".join(p.read_text() for p in store.base.glob("*.json"))
    assert judge._best_quote(["Your code is 482913", "Private details omitted."], "code") == ""


def test_an_unknown_send_locks_the_conversation_for_an_hour(store):
    run(store, _cycle_model(), _cycle_items(), freeze=_freeze)
    card = next(c for c in snap(store)["cards"] if c["action"]["method"] == "text_reply")

    async def unknown(card, words):
        return {"ok": False, "status": "unknown"}

    asyncio.run(executor.fire(card["id"], "Hi", store=store, methods={"text_reply": unknown}, now=NOW))
    # A different card for the same conversation (another method, a new id) stays locked for the hour.
    fresh = {**card, "id": "card:new", "action": {**card["action"], "locked": None}}
    store.save_state(cards=[fresh])
    sent = []

    async def ok(card, words):
        sent.append(words)
        return {"ok": True, "status": "succeeded"}

    locked = asyncio.run(executor.fire("card:new", "Hi", store=store, methods={"text_reply": ok}, now=NOW + 600))
    assert locked["status"] == "locked" and sent == []
    assert snap(store)["cards"][0]["action"]["locked"] == "unknown"
    later = asyncio.run(executor.fire("card:new", "Hi", store=store, methods={"text_reply": ok}, now=NOW + 3700))
    assert later["ok"] is True and sent == ["Hi"]


def test_dismiss_clears_the_conversation_lock(store):
    card = _text_card(store)
    store.mark_unknown(card, NOW)
    store.dismiss_card(store.card("card:t"), NOW + 1)
    assert not store.locked({**card, "id": "card:other"}, NOW + 2)


# --- build 2489 pass ----------------------------------------------------------------------------------------


def test_to_artifact_and_they_for_the_user_are_fixed():
    assert triage.attribute("You (to Trishe) said they downgraded ChatGPT.", NAMES, "Trishe", True) == \
        "You told Trishe you downgraded ChatGPT."


@pytest.mark.parametrize("bad", [
    "You agree that undecided is a good idea, and asks Sriram to look into it.",
    "You says hi to Sriram.",
    "You (to Trishe) said hi.",
    "I agree, I'd look into it more.",
    "They remind everyone about the exam.",
])
def test_summaries_that_read_badly_are_not_sane(bad):
    assert not triage.sane(bad, {"user_wrote": True})


def test_incoming_mail_never_says_you_asked():
    assert not triage.sane("You asked about the exam and calculators.", {"user_wrote": False})
    assert triage.sane("Ed answered your question about the exam.", {"user_wrote": False})


def test_a_bad_summary_is_reasked_once_then_templated():
    items = sources.text_items([(NOW - 120, "Sriram Kannan", "Me: maybe undecided in L and S?", "iMessage;-;+15550001111"),
                                (NOW - 60, "Sriram Kannan", "Sriram Kannan: I agree, look into it more", "iMessage;-;+15550001111")],
                               now=NOW, own=ME)
    calls = []

    async def model(system, material, abort, tokens):
        calls.append("STRICT" in system)
        return json.dumps({"title": "Undecided in L and S", "summary": "You agree, and asks Sriram to look.",
                           "junk": False, "sensitive": False, "asks_of_me": ""})

    state, result = asyncio.run(triage.triage_item(items[0], model, lambda: False))
    assert calls == [False, True]
    assert result["summary"] == "Sriram Kannan texted about undecided in L and S."
    assert triage.template_summary({"who": "Ed", "source": "mail", "title": "Exam reminder"}, {}) == \
        "Ed emailed: Exam reminder."


def test_dropped_waiting_people_leave_a_reason(store):
    rows = [(NOW - 120, "Mom", "Mom: call me?", GUID_MOM), (NOW - 60, "Mom", "Me: sure, tonight", GUID_MOM)]
    run(store, Model(triage_reply=lambda m: {"title": "Call", "summary": "Mom asked you to call.", "junk": False,
                                             "sensitive": False, "asks_of_me": "Call Mom"},
                     judge_reply=lambda m: {"summary": "", "items": []}),
        sources.text_items(rows, now=NOW, own=ME))
    debug = snap(store)["debug"]["waiting"]
    assert debug == [{"who": "Mom", "waiting": False, "reason": "you answered last"}]


def test_a_friend_texting_from_an_unsaved_number_is_an_item_but_a_campaign_is_not():
    friend = sources.text_items([(NOW - 60, "+14083298560", "+14083298560: Please study for the exam", "")],
                                now=NOW, own=ME)
    assert [t["who"] for t in friend] == ["(408) 329-8560"]
    campaign = sources.text_items(
        [(NOW - 60, "+15412303649", "+15412303649: Priority deadline Nov 3. Apply: https://beav.es/xKX", ""),
         (NOW - 50, "+15102917003", "+15102917003: Reply STOP to opt out of city updates", "")],
        now=NOW, own=ME)
    assert campaign == []


# --- read state: a card for an unread action item, or one read a day ago and still unanswered ----------------


def _read_mom(age, unread):
    return [(NOW - age - 100, "Mom", "Me: Hey Mom, want to get coffee this week?", GUID_MOM, False),
            (NOW - age, "Mom", "Mom: can you call me tonight?", GUID_MOM, unread)]


def _asks_call():
    return Model(triage_reply=lambda m: {"title": "Call", "summary": "Mom asked you to call tonight.", "junk": False,
                                         "sensitive": False, "asks_of_me": "Call Mom tonight"},
                 judge_reply=lambda m: {"summary": "", "items": []})


def test_text_items_carry_the_latest_incoming_read_state():
    assert sources.text_items(_read_mom(60, True), now=NOW, own=ME)[0]["unread"] is True
    assert sources.text_items(_read_mom(60, False), now=NOW, own=ME)[0]["unread"] is False
    assert sources.text_items(mom_texts(), now=NOW, own=ME)[0]["unread"] is None


def test_an_unread_action_item_is_a_card(store):
    run(store, _asks_call(), sources.text_items(_read_mom(600, True), now=NOW, own=ME))
    assert [c["action"]["method"] for c in snap(store)["cards"]] == ["text_reply"]


def test_a_read_message_under_a_day_old_is_a_todo_not_a_card(store):
    run(store, _asks_call(), sources.text_items(_read_mom(3600, False), now=NOW, own=ME))
    state = snap(store)
    assert state["cards"] == []
    assert {"who": "Mom", "waiting": False, "reason": "read, under a day"} in state["debug"]["waiting"]


def test_a_read_message_unanswered_for_a_day_is_a_card(store):
    run(store, _asks_call(), sources.text_items(_read_mom(90000, False), now=NOW, own=ME))
    assert [c["action"]["method"] for c in snap(store)["cards"]] == ["text_reply"]


def test_an_unread_text_with_no_action_item_is_not_a_card(store):
    rows = [(NOW - 600, "Mom", "Mom: lol that's so funny", GUID_MOM, True)]
    run(store, Model(triage_reply=lambda m: {"title": "Joke", "summary": "Mom laughed.", "junk": True,
                                             "sensitive": False, "asks_of_me": ""},
                     judge_reply=lambda m: {"summary": "", "items": []}),
        sources.text_items(rows, now=NOW, own=ME))
    state = snap(store)
    assert state["cards"] == []
    assert state["debug"]["waiting"] == [{"who": "Mom", "waiting": False, "reason": "no action item"}]


def test_the_gate_leaves_school_deadlines_and_unknown_read_state_alone():
    assert cycle.card_due({"source": "mail", "person": False, "unread": False, "received_ts": NOW}, NOW)
    assert cycle.card_due({"source": "messages", "person": True, "unread": None, "received_ts": NOW}, NOW)
    assert not cycle.card_due({"source": "mail", "person": True, "unread": False, "received_ts": NOW - 60}, NOW)
    assert cycle.card_due({"source": "mail", "person": True, "unread": False, "received_ts": NOW - 86400}, NOW)
