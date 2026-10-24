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

TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=ZoneInfo(TZ)).timestamp()
ME = "adnjain@ucsc.edu"
LINKED = "adijain888@gmail.com"
NAMES = ["Adi Jain", "Adi", "Jain"]


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
    assert result["summary"] == "You told Trishe they would keep the Wisp idea."


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
