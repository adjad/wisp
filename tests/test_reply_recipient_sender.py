"""Spoofed mail senders: the Brief's tap-to-send reply, Gmail rows, gists and list detection.

A display name can be dressed up as someone else's address. Every reader here goes through
service.mail_sender. Synthetic rows only: no Mail, no Gmail, no model, no network.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if "service.paths" not in sys.modules:
    _scratch = tempfile.TemporaryDirectory(prefix="wisp-reply-sender-")
    os.environ["WISP_HOME"] = _scratch.name

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from service import gmail_source as G  # noqa: E402
from service.assistant import email_reply_api as api  # noqa: E402
from service.assistant import mail_gists  # noqa: E402
from service.tools import action_tools  # noqa: E402
from service.tools import email_tools as E  # noqa: E402

EVIL = "x@evil.com"
MID = "<one@fixture>"
REF = {"message_id": MID, "account": "Work", "account_id": "acct-1"}
ROW = {"message_id": MID, "account": "Work", "account_id": "acct-1", "sender": "Maya Chen <maya@friend.com>",
       "to": "me@work.example", "subject": "Coffee chat?", "ts": 1_790_000_000.0, "body": "Coffee Thursday?"}
UNSURE = "Wisp couldn't tell who this email is really from, so it won't reply for you. Open it in Mail."


class Bridge:
    """Fake outbox: prepare echoes `env` with the body; reply confirms the exact envelope."""

    def __init__(self, env):
        self.env, self.calls = dict(env), []

    async def __call__(self, kind, payload, **_):
        self.calls.append((kind, dict(payload)))
        if kind == "prepare_email_reply":
            return {"ok": True, "reply": {**self.env, "content": payload["body"] + "\n\nOn … Maya wrote:"}}
        if kind == "reply_to_email":
            return {"ok": True, "accepted": True, "status": "succeeded", "reply": payload["expected_reply"]}
        raise AssertionError(kind)

    def sends(self):
        return [p for k, p in self.calls if k == "reply_to_email"]


@pytest.fixture
def world(monkeypatch):
    state = {"rows": [dict(ROW)], "headers": []}
    bridge = Bridge({"message_id": MID, "account": "Work", "account_id": "acct-1", "from": "me@work.example",
                     "to": ["maya@friend.com"], "cc": [], "bcc": [], "subject": "Re: Coffee chat?"})

    async def model(system, material):
        return "Hi Maya, Thursday works."

    monkeypatch.setattr(action_tools, "app_request", bridge)
    monkeypatch.setattr(api, "_mail_rows", lambda: [dict(r) for r in state["rows"]])
    monkeypatch.setattr(api, "_header_rows", lambda: [dict(r) for r in state["headers"]])
    monkeypatch.setattr(api, "_native_accounts", lambda: [{"id": "acct-1", "name": "Work"}])
    monkeypatch.setattr(api, "_external_accounts", lambda: {})
    monkeypatch.setattr(api, "_model_reply", model)
    monkeypatch.setattr(api, "_audit", lambda event, **f: None)
    monkeypatch.setattr(api, "_policy_denies", lambda args: "")
    monkeypatch.setattr(api, "_spent", {})
    monkeypatch.setattr(api, "_in_flight", set())
    monkeypatch.setattr(api, "_user_first_name", lambda: "")
    app = FastAPI()
    app.include_router(api.router)
    return type("W", (), {"state": state, "bridge": bridge, "client": TestClient(app)})()


def _draft(w):
    r = w.client.post("/assistant/email_reply/draft", json={"reply_ref": REF})
    assert r.status_code == 200
    return r.json()


def _send(w, envelope, body):
    r = w.client.post("/assistant/email_reply/send", json={"envelope": envelope, "body": body})
    assert r.status_code == 200
    return r.json()


def _refused(view):
    assert view["ok"] and not view["can_send"] and view["send_unavailable"]
    assert view["envelope"] is None and view["reason"] == UNSURE
    assert view["open_label"] == "Open in Mail"


# --- the reply recipient ----------------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"sender_address": "maya@friend.com"}])
def test_an_ordinary_sender_is_replied_to_exactly_as_before(world, extra):
    world.state["rows"] = [{**ROW, **extra}]
    view = _draft(world)
    assert view["can_send"] and view["envelope"] and view["to"] == ["maya@friend.com"] and view["cc"] == []
    out = _send(world, view["envelope"], "Thursday works.")
    assert out["status"] == "sent" and world.bridge.sends()[0]["expected_reply"]["to"] == ["maya@friend.com"]


def test_an_ordinary_address_keeps_its_case():
    row = {**ROW, "sender": "Maya Chen <Maya@Friend.com>"}
    assert api._predicted_envelope(REF, row, ("Work", "acct-1"))["to"] == ["Maya@Friend.com"]


def test_a_header_only_row_whose_name_poses_as_an_address_is_refused(world):
    world.state["rows"] = []
    world.state["headers"] = [{"message_id": MID, "account": "Work", "account_id": "acct-1",
                               "sender": "Maya <maya@friend.com>", "sender_address": EVIL, "subject": "Hi"}]
    view = _draft(world)
    _refused(view)
    assert "maya@friend.com" not in view["to"]


@pytest.mark.parametrize("sender, to", [
    ('Dean <dean@school.edu> <"x>"@evil.com>', []),            # Apple Mail's raw From, posed
    ("Maya <maya@friend.com> <x@evil.com>", [EVIL]),           # Gmail body row, quotes dropped
    ('"Maya <maya@friend.com>" <x@evil.com>', [EVIL]),
    ('"x>"@evil.com', []),                                     # a quoted local part: no clean address
])
def test_a_posed_or_unreadable_sender_is_refused_and_never_shows_the_posed_address(world, sender, to):
    world.state["rows"] = [{**ROW, "sender": sender}]
    view = _draft(world)
    _refused(view)
    assert view["to"] == to
    assert not any("friend.com" in a or "school.edu" in a for a in view["to"])
    assert api.freeze_reply(REF)["envelope"] is None
    assert world.bridge.calls == []


def test_a_posed_sender_is_not_greeted_by_the_posed_name(monkeypatch):
    monkeypatch.setattr(api, "_user_first_name", lambda: "adi")
    posed = {"sender": "Maya <maya@friend.com> <x@evil.com>"}
    assert api._fix_greeting("Hi Adi, sounds good.", posed) == "sounds good."
    assert api._fix_greeting("Hi Adi, sounds good.", {"sender": "Maya Chen <maya@friend.com>"}).startswith("Hi Maya,")


def test_reply_all_on_an_ordinary_thread_still_shows_and_sends_mails_recipients(world):
    world.bridge.env.update(cc=["sam@friend.com"])
    view = _draft(world)
    first = _send(world, view["envelope"], "Thursday works.")
    assert first["status"] == "confirm" and first["cc"] == ["sam@friend.com"] and world.bridge.sends() == []
    out = _send(world, first["envelope"], "Thursday works.")
    assert out["status"] == "sent"
    sent = world.bridge.sends()[0]
    assert sent["expected_reply"]["cc"] == ["sam@friend.com"] and sent["expected_reply"]["to"] == ["maya@friend.com"]
    # An envelope sealed as reply-all carries the flag to Mail.
    token = api.seal_envelope({**world.bridge.env}, verified=True, reply_all=True)
    assert _send(world, token, "All of you: Thursday.")["status"] == "sent"
    assert world.bridge.sends()[-1]["reply_all"] is True


# --- Gmail rows carry sender_address -----------------------------------------------------


def _gmail(sender):
    return {"id": "g1", "internalDate": "1790000000000", "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Synthetic").decode()},
                        "headers": [{"name": "From", "value": sender}, {"name": "To", "value": "me@gmail.com"},
                                    {"name": "Subject", "value": "Hello"},
                                    {"name": "Message-ID", "value": "<g1@example.test>"}]}}


@pytest.mark.parametrize("raw_from, address", [
    ("Maya Chen <Maya@Friend.com>", "maya@friend.com"),
    ('"Maya <maya@friend.com>" <x@evil.com>', EVIL),
    ("=?utf-8?b?TWF5YSBDaGVu?= <maya@friend.com>", "maya@friend.com"),          # "Maya Chen", encoded
    ("=?utf-8?q?Maya_=3Cmaya=40friend=2Ecom=3E?= <x@evil.com>", EVIL),           # decodes to a posed name
    ("Nobody", ""),
])
def test_gmail_rows_carry_the_address_parsed_from_the_raw_from_header(raw_from, address):
    message = _gmail(raw_from)
    meta = G.parse_metadata(message)
    assert meta["sender_address"] == address
    # Header rows: the H2 address field is that address, and email_tools reads it back.
    header = E._parse_header_records(G.header_line(meta, "me@gmail.com", False))
    assert header and header[0]["sender_address"] == address
    # Attachment rows are dicts with a sender: they carry it too.
    assert G._attachment_message("me@gmail.com", message)["sender_address"] == address
    # Body rows keep the sender string's format; the real address still reads correctly from it.
    body = E._parse_raw_text(G.raw_record(message, "me@gmail.com"))[0]
    if address and "=?" not in raw_from:
        from service import mail_sender
        assert mail_sender.real_address(body["sender"]) == address


def test_an_ordinary_gmail_sender_string_is_unchanged():
    message = _gmail("Maya Chen <maya@friend.com>")
    assert E._parse_raw_text(G.raw_record(message, "me@gmail.com"))[0]["sender"] == "Maya Chen <maya@friend.com>"
    assert G._attachment_message("me@gmail.com", message)["sender"] == "Maya Chen <maya@friend.com>"


# --- gists and list detection --------------------------------------------------------------


def test_from_user_reads_the_real_address_and_never_a_posed_one():
    mine = {"me@school.edu"}
    assert mail_gists._from_user({"sender": "Me <me@school.edu>"}, mine)
    assert mail_gists._from_user({"sender": "Me", "sender_address": "me@school.edu"}, mine)
    assert not mail_gists._from_user({"sender": "Maya <maya@friend.com>"}, mine)
    for posed in ({"sender": "Me <me@school.edu> <x@evil.com>"}, {"sender": '"me@school.edu" <x@evil.com>'},
                  {"sender": "Me <me@school.edu>", "sender_address": EVIL},
                  {"sender": "Dean <dean@school.edu> <me@school.edu>"}):
        assert not mail_gists._from_user(posed, mine), posed


def test_broadcast_and_list_detection_ignore_a_posed_name():
    assert E.is_broadcast({"sender": "Vercel <ship@info.vercel.com>"})
    assert not E.is_broadcast({"sender": "Maya Chen <maya@friend.com>"})
    # A broadcast address written into the name does not make a person's mail broadcast...
    assert not E.is_broadcast({"sender": '"News <events@mail.shop.com>" <maya@friend.com>'})
    # ...and a person's address written into the name does not hide a real broadcast.
    assert E.is_broadcast({"sender": "Maya <maya@friend.com> <events@mail.shop.com>"})
    assert E.is_broadcast({"sender": "Maya <maya@friend.com>", "sender_address": "events@mail.shop.com"})
    ordinary = {"sender": "Registrar <records@school.edu>", "subject": "Your schedule", "body": "unsubscribe"}
    assert not E._is_list_mail(ordinary)
    # A posed .edu address in the name can't vouch for a shop's footered mail.
    posed = {"sender": '"Registrar <records@school.edu>" <deals@shop.com>', "subject": "Your schedule",
             "body": "unsubscribe"}
    assert E._is_list_mail(posed)
    unreadable = {"sender": 'Registrar <records@school.edu> <"x>"@shop.com>', "subject": "Hi", "body": "unsubscribe"}
    assert E._is_list_mail(unreadable)


def test_this_file_is_in_the_simulation_manifest():
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_reply_recipient_sender.py"' in manifest
