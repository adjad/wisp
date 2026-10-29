"""Critical account mail (an exposed secret, a vulnerability alert, a new
sign-in, a failed payment) reaches "Needs my attention" and "Before you ask",
and promos that merely look alike do not.

Real failure: "[adjad/Wisp_hackathon123] Possible valid secrets detected" from
GitHub <support@github.com> was dropped as bulk mail by Before you ask and
filed under "news" by Needs you, while an MLH "is starting" promo took a slot.

Fake model and fixture rows only; nothing native, nothing live.
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-critical-mail-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.assistant import daily_workspace as dw
from service.proactive import cycle, prepare, sources, triage
from service.proactive.store import ProactiveStore
from service.tools import email_tools

TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=ZoneInfo(TZ)).timestamp()
ME = "adnjain@ucsc.edu"
NAMES = ["Adi Jain", "Adi"]
HUB = "Git" + "Hub"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FOOTER = "\n\nYou're receiving this because of your account settings. Unsubscribe"


@pytest.fixture(autouse=True)
def user_is_adi(monkeypatch):
    monkeypatch.setattr(triage, "user_names", lambda: list(NAMES))
    import service.memory.identity as identity
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")
    monkeypatch.setattr(identity, "user_emails", lambda: [ME])


@pytest.fixture
def store(tmp_path):
    cycle._status.update(state="idle", progress={"done": 0, "total": 0}, error="")
    return ProactiveStore(tmp_path / "proactive")


def mail(mid, sender, address, subject, body, ago_h=2, **extra):
    return dict(ts=NOW - ago_h * 3600, account=ME, account_id="acct-1", sender=f"{sender} <{address}>",
                sender_address=address, to=ME, subject=subject, message_id=mid, body=body, unread=False) | extra


CRITICAL = [
    mail("<c1@x>", HUB, f"support@{HUB.lower()}.com", "[adjad/Wisp_hackathon123] Possible valid secrets detected",
         f"We found a {HUB} Personal Access Token {TOKEN} in commit "
         f"https://{HUB.lower()}.com/adjad/Wisp_hackathon123/commit/1 . Revoke it now." + FOOTER),
    mail("<c2@x>", HUB, f"noreply@{HUB.lower()}.com", "[adjad/Wisp] Dependabot alerts: 2 vulnerabilities found",
         "A critical severity vulnerability affects a dependency." + FOOTER, ago_h=3),
    mail("<c3@x>", "Google", "no-reply@accounts.google.com", "New sign-in to your Google Account",
         "We noticed a new sign-in on a Mac. If this was you, you don't need to do anything." + FOOTER, ago_h=4),
    mail("<c4@x>", "Spotify", "no-reply@spotify.com", "Your payment was declined",
         "We couldn't process your payment. Update your card to keep Premium." + FOOTER, ago_h=5),
]
PROMOS = [
    mail("<p1@x>", "Devpost", "support@devpost.com", "New hackathon: build secure AI agents, register now",
         "Join thousands of hackers. Prizes up to $10k." + FOOTER),
    mail("<p2@x>", "Major League Hacking", "hi@mlh.io", "Hacktoberfest 2026 is starting!",
         "Hacktoberfest is here. Make your first pull request." + FOOTER, ago_h=3),
    mail("<p3@x>", "Indeed", "alert@indeed.com", "Security engineer jobs near you",
         "New jobs for you. Apply now." + FOOTER, ago_h=4),
    mail("<p4@x>", "The Hacker News", "newsletter@thehackernews.com",
         "Security week: leaked tokens, a new password manager deal and more",
         "This week in security news." + FOOTER, ago_h=5),
]


def items_for(rows):
    return sources.mail_items(rows, mine={ME}, now=NOW, tz=TZ, linked=set(), own=ME)


def attention_item(row):
    from service.tools.imessage_tools import redact_summary_codes
    subject = redact_summary_codes(row["subject"])
    item = dict(id="email:" + row["message_id"], source="email", title=subject, quote=subject, due_ts=None,
                reason="Review this source", **dw._sender_fields(row))
    return {k: v for k, v in item.items() if v not in (None, "")}


def picked(attention):
    end = datetime(2026, 10, 24, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp()
    return dw.needs_you(attention, now=NOW, day_end=end)


# --- the detector ----------------------------------------------------------------------------------


@pytest.mark.parametrize("row", CRITICAL, ids=["secret-scanning", "dependabot", "new-sign-in", "payment-declined"])
def test_critical_alerts_are_attention_kept_and_listed(row):
    assert email_tools.critical_account_mail(row)
    assert email_tools.digest_category(row) == "attention"
    [item] = items_for([row])
    assert item["critical"] is True and item["person"] is False
    assert item["lines"] == [] and TOKEN not in json.dumps(item) and "https://" not in item["text"]
    [entry] = picked([attention_item(row)])["items"]
    assert entry["kind"] == "alert" and entry["text"] == item["title"].removesuffix(" — review")


def test_the_reported_secret_alert_has_a_fixed_safe_title():
    [item] = items_for([CRITICAL[0]])
    assert item["title"] == f"{HUB} flagged a possible exposed secret in adjad/Wisp_hackathon123 — review"
    assert item["secret"] is True                     # the raw body carries the token itself


@pytest.mark.parametrize("row", PROMOS, ids=["devpost", "mlh", "indeed", "newsletter"])
def test_promos_are_not_critical_and_still_dropped(row):
    assert email_tools.critical_account_mail(row) is None
    assert email_tools.digest_category(row) != "attention"
    assert items_for([row]) == []
    fields = dw._sender_fields(row)
    assert fields["person"] is False and "critical" not in fields
    assert picked([attention_item(row)])["items"] == []


def test_an_mlh_promo_no_longer_takes_a_needs_you_slot_from_people():
    people = [dict(id=f"email:{n}", source="email", title=f"Question {n}", due_ts=None, sender=f"Sam {n}",
                   person=True, received_ts=NOW - n * 60, reason="Review this source") for n in range(1, 4)]
    result = picked([attention_item(PROMOS[1]), attention_item(CRITICAL[0])] + people)
    kinds = [i["kind"] for i in result["items"]]
    assert kinds == ["alert", "mail", "mail", "mail"] and result["more"] == 0


def test_important_email_rows_admit_a_critical_alert_mass_mail_would_skip():
    from types import SimpleNamespace
    rows = dw.important_email_rows(SimpleNamespace(rows=[dict(r) for r in CRITICAL + PROMOS]))
    subjects = {r["subject"] for r, _ in rows}
    assert {r["subject"] for r in CRITICAL} <= subjects
    assert not subjects & {r["subject"] for r in PROMOS}


# --- withholding ---------------------------------------------------------------------------------


def test_a_placeholder_only_thread_is_not_withheld():
    row = mail("<s1@x>", "Sam Lee", "sam@example.com", "Study room",
               "Can you sign in to the portal and check the room schedule before Friday?")
    [item] = items_for([row])
    assert "details omitted" in item["text"]          # the redaction placeholder is in the excerpt
    assert item["secret"] is False and triage.withheld(item) is False


def test_a_thread_with_a_real_token_is_withheld():
    row = mail("<s2@x>", "Sam Lee", "sam@example.com", "Deploy",
               f"Here is the deploy token: {TOKEN} can you push tonight?")
    [item] = items_for([row])
    assert item["secret"] is True and triage.withheld(item) is True
    calls = []

    async def model(*a):
        calls.append(a)
        return "{}"
    state, _ = asyncio.run(triage.triage_item(item, model, lambda: False))
    assert state == "sensitive" and calls == []


def test_items_without_the_raw_flag_keep_the_placeholder_rule():
    assert triage.withheld({"text": "Sam: Authentication details omitted."}) is True


# --- cards ------------------------------------------------------------------------------------------


def _run(store, items, model):
    return asyncio.run(cycle.run_cycle(
        now=NOW, tz=TZ, force=True, reply_fn=model, abort=lambda: False, collect=lambda n, z: list(items),
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked=set(), own=ME, allowed=set(NAMES), freeze=lambda ref: None))


class Model:
    def __init__(self):
        self.seen = []

    async def __call__(self, system, material, abort, tokens):
        self.seen.append(material)
        if "ONE email thread or text conversation" in system:
            return json.dumps({"title": "x", "summary": "", "junk": True, "sensitive": False, "asks_of_me": ""})
        return json.dumps({"summary": "", "items": []})


def test_a_critical_alert_becomes_a_review_card_and_todo_without_a_draft(store):
    items = items_for([CRITICAL[0]] + PROMOS)
    model = Model()
    _run(store, items, model)
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, own=ME, linked=set(),
                          schedule_fn=lambda n, z: {"calendar": [], "reminders": []})
    [card] = view["cards"]
    assert card["title"] == f"{HUB} flagged a possible exposed secret in adjad/Wisp_hackathon123 — review"
    assert card["action"]["method"] == "read" and card["action"]["draft"] == ""
    assert card["action"]["recipient"] == "" and card["sources"][0]["quote"] == ""
    assert any(t["title"] == card["title"] for t in view["todos"])
    blob = json.dumps(view)
    assert TOKEN not in blob and "commit/1" not in blob
    # The alert never reached the model: only its fixed sentence did, in the judge's list.
    assert not any(TOKEN in m or "Revoke it" in m for m in model.seen)


def test_the_five_card_cap_holds_with_six_critical_alerts(store):
    rows = [mail(f"<k{n}@x>", HUB, f"support@{HUB.lower()}.com", f"[adjad/repo{n}] Possible valid secrets detected",
                 "We found a token." + FOOTER, ago_h=n + 1) for n in range(6)]
    items = items_for(rows)
    assert len(items) == 6 and len({i["id"] for i in items}) == 6
    _run(store, items, Model())
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, own=ME, linked=set(),
                          schedule_fn=lambda n, z: {"calendar": [], "reminders": []})
    assert len(view["cards"]) == prepare.MAX_CARDS == 5
    assert all(c["action"]["method"] == "read" for c in view["cards"])
    result = picked([attention_item(r) for r in rows])
    assert len(result["items"]) == dw.NEEDS_YOU_SHOWN and result["more"] == 6 - dw.NEEDS_YOU_SHOWN


def test_alert_cards_come_before_people_and_deadlines():
    from service.proactive import cycle

    def card(i, method, critical=False):
        c = {"id": f"c{i}", "action": {"method": method}}
        if critical:
            c["critical"] = True
        return c

    replies = [card(i, "text_reply") for i in range(5)]
    alert = card(9, "read", critical=True)
    ordered = cycle.order_cards(replies + [alert], [card(8, "block_time")])
    assert ordered[0]["id"] == "c9" and len(ordered) == 5


def test_one_email_seen_through_mail_and_gmail_is_one_card(store):
    """2026-10-29: the same GitHub alert showed as card 1 of 4 (Mail) and 4 of 4 (the
    linked Gmail account). One Message-ID is one card."""
    native = CRITICAL[0]
    gmail = dict(native, account="adnjain.personal@gmail.com", account_id="google:adnjain.personal@gmail.com")
    items = items_for([native, gmail])
    _run(store, items, Model())
    view = cycle.snapshot(now=NOW, tz=TZ, store=store, own=ME, linked=set(),
                          schedule_fn=lambda n, z: {"calendar": [], "reminders": []})
    alerts = [c for c in view["cards"] if "exposed secret" in c["title"]]
    assert len(alerts) == 1


def test_message_key_is_the_id_and_its_sender():
    assert prepare._message_key({"reply_ref": {"message_id": "<C1@X>"}, "sender": "Hub <Support@Hub.com>"}) \
        == "c1@x|support@hub.com"
    assert prepare._message_key({"reply_ref": " <c1@x> ", "sender_address": "a@b.c"}) == "c1@x|a@b.c"
    # Either part unknown: no key, so nothing is merged on a guess.
    assert prepare._message_key({"reply_ref": {"message_id": "<c1@x>"}}) == ""
    assert prepare._message_key({"sender_address": "a@b.c"}) == ""


def _judged(mid, sender, *, critical, title, primary):
    raw = {"reply_ref": {"message_id": mid, "account": ME, "account_id": "acct-1"},
           "ref": {"kind": "email", "message_id": mid, "to": sender}, "person": True, "replied": False}
    if critical:
        raw["critical"] = True
    return {"source": "mail", "kind": "reply", "title": title, "who": "Sender", "item": raw,
            "primary": primary, "fingerprints": [primary], "asks": [], "due_ts": None, "why": ""}


def _cards(items):
    view = {"ok": True, "open_url": "message://%3Cx%3E", "open_label": "Open in Mail"}
    state, cards = asyncio.run(prepare.prepare(items, {"calendar": [], "reminders": []}, None, lambda: False,
                                               now=NOW, tz=TZ, freeze=lambda ref: view))
    assert state == "ok"
    return cards


def test_a_reused_message_id_cannot_hide_a_critical_alert():
    """Release Auditor, 6c3d68d4: a Message-ID is whatever the sender wrote. An
    ordinary mail that reuses a real alert's ID and ranks first must not take the
    alert's card away, whether it comes from another address or spoofs the same one."""
    alert = _judged("<c1@x>", "support@github.com", critical=True, title="Possible valid secrets detected",
                    primary="email:alert")
    for decoy_sender in ("sam@example.com", "support@github.com"):
        decoy = _judged("<c1@x>", decoy_sender, critical=False, title="Can you check this?", primary="email:decoy")
        cards = _cards([decoy, alert])
        assert any(c.get("critical") for c in cards), decoy_sender
    # The same alert seen twice (Mail and Gmail) is still one card.
    twin = _judged("<C1@x>", "Support@GitHub.com", critical=True, title="Possible valid secrets detected",
                   primary="email:twin")
    assert sum(1 for c in _cards([alert, twin]) if c.get("critical")) == 1


def test_open_in_mail_hands_back_the_emails_own_link():
    """2026-10-29: "Open in Mail?" marked the card done and opened nothing: the read
    fire returned no link. It now returns Wisp's own link to that email."""
    from service.proactive import executor

    def fire(url):
        card = {"id": "c", "action": {"method": "read", "open_url": url}}
        return asyncio.run(executor._run(card, "read", "", None, {}, NOW))

    mail_link = "message://%3Cc1%40x%3E"
    assert fire(mail_link) == {"ok": True, "status": "succeeded", "open_url": mail_link, "result": "open_in_mail"}
    gmail_link = "https://mail.google.com/mail/u/me%40gmail.com/#search/rfc822msgid%3Ac1%40x"
    assert fire(gmail_link)["result"] == "open_in_gmail" and fire(gmail_link)["open_url"] == gmail_link
    # Only the shapes Wisp builds itself: never a link that came from somewhere else.
    for other in ("https://evil.example/x", "file:///etc/passwd", "", None):
        assert "open_url" not in fire(other)
