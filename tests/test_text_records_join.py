"""How a text is tied to its chat and delivery service: the production join in
sources.text_records(), the service allowlist in the V3 feed parser, and the
per-line facts text_items() keeps for deciders (text_origin).

Synthetic rows only: no real Messages, no subprocesses, network or sleeps.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

_scratch = tempfile.TemporaryDirectory(prefix="wisp-text-join-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import sources

NOW = 1_792_051_200.0
ME = "me@example.com"
HANDLE = "+15551230001"
CHAT = "iMessage;-;" + HANDLE
OTHER = "iMessage;-;+15551230002"
GROUP = "iMessage;+;chat123456"


# --- the production join in text_records() -------------------------------------------------------

def _native(ts, conversation, service):
    return {"timestamp": ts, "conversation": conversation, "service": service}


@pytest.fixture
def feed(monkeypatch):
    from service.tools import imessage_tools as im

    def install(plain, native):
        monkeypatch.setattr(im, "messages_sync_state", lambda: "ready")
        monkeypatch.setattr(im, "_parse_records", lambda: list(plain))
        monkeypatch.setattr(im, "structured_messages_snapshot", lambda: {"records": list(native)})
        return sources.text_records()
    return install


def test_each_plain_row_takes_the_chat_and_service_of_its_one_native_record(feed):
    iso = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    out = feed(
        [(NOW - 300, "chat:1", "Maya", "Maya: hi", True),
         (NOW - 200, "chat:2", "Sam", "Sam: yo", False),
         (NOW - 100, "chat:1", "Maya", "Maya: tea?", True),
         (NOW - 60, "chat:1", "Maya", "Maya: at 4", None),
         (NOW - 30, "chat:3", "Lee", "Lee: lost", True)],
        [_native(NOW - 300, CHAT, "iMessage"),
         _native(NOW - 200, OTHER, "SMS"),
         _native(NOW - 100, CHAT, "iMessage"), _native(NOW - 100, OTHER, "SMS"),  # two at one stamp
         _native(iso, CHAT, "RCS")])                                              # ISO timestamp
    assert out == [
        (NOW - 300, "Maya", "Maya: hi", CHAT, True, "iMessage"),
        (NOW - 200, "Sam", "Sam: yo", OTHER, False, "SMS"),
        (NOW - 100, "Maya", "Maya: tea?", "", True, ""),        # disagreeing records: nothing guessed
        (NOW - 60, "Maya", "Maya: at 4", CHAT, None, "RCS"),
        (NOW - 30, "Lee", "Lee: lost", "", True, ""),           # no native record
    ]


def test_two_native_records_that_agree_still_place_the_row(feed):
    out = feed([(NOW - 10, "chat:1", "Maya", "Maya: hi", True)],
               [_native(NOW - 10, CHAT, "iMessage"), _native(NOW - 10, CHAT, "iMessage")])
    assert out == [(NOW - 10, "Maya", "Maya: hi", CHAT, True, "iMessage")]


def test_a_broken_feed_leaves_every_row_unplaced(feed, monkeypatch):
    from service.tools import imessage_tools as im
    out = feed([(NOW - 10, "chat:1", "Maya", "Maya: hi", True)], [])
    assert out[0][3:] == ("", True, "")
    monkeypatch.setattr(im, "structured_messages_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert sources.text_records()[0][3] == "" and sources.text_records()[0][5] == ""


def test_nothing_is_read_before_messages_are_ready(monkeypatch):
    from service.tools import imessage_tools as im
    monkeypatch.setattr(im, "messages_sync_state", lambda: "syncing")
    monkeypatch.setattr(im, "_parse_records", lambda: pytest.fail("read before ready"))
    assert sources.text_records() == []


# --- the service allowlist in the V3 feed parser ---------------------------------------------------

def _carrier(services):
    rows = []
    for i, service in enumerate(services):
        raw = {"guid": f"g{i}", "conversation": CHAT, "sender": HANDLE, "direction": "incoming",
               "timestamp": "2026-09-25T17:30:00Z", "text": f"Synthetic message {i}", "links": []}
        source = {"kind": "messages", "message_guid": raw["guid"], "chat_guid": CHAT, "navigation_url": None}
        if service is not _MISSING:
            source["service"] = service
        rows.append(dict(version=1, kind="record", record=raw, source=source,
                         coverage=dict(text="complete", links="complete")))
    meta = dict(version=1, kind="coverage", attempted=len(rows), emitted=len(rows), skipped=0, truncated=0,
                limit=2000, byte_limit=4_000_000, window_days=365, row_limit=20000, reached_row_limit=False)
    return "\n".join("V3 | " + json.dumps(v) for v in [meta, *rows]) + "\nV2 | synthetic legacy carrier"


_MISSING = object()


@pytest.fixture
def messages(monkeypatch):
    from service.tools import cache_store
    monkeypatch.setattr(cache_store, "load", lambda *_a, **_k: "")
    monkeypatch.setattr(cache_store, "save", lambda *_a, **_k: None)
    from service.tools import imessage_tools as im
    for name in ("_lines", "_available", "_unavailable_reason", "_unavailable_kind", "_sync_completed",
                 "_snapshot_generation", "_snapshot_captured_at", "_publication_expires_monotonic",
                 "_issued_snapshot", "_records_memo"):
        monkeypatch.setattr(im, name, getattr(im, name))
    return im


def test_the_feed_keeps_only_the_three_known_services(messages):
    given = ["iMessage", "SMS", "RCS", None, "imessage", "Telegram", 5, _MISSING]
    messages.cache_messages(_carrier(given), True)
    records = messages.structured_messages_snapshot()["records"]
    assert [r["guid"] for r in records] == [f"g{i}" for i in range(len(given))]
    assert [r["service"] for r in records] == ["iMessage", "SMS", "RCS", "", "", "", "", ""]


# --- text_items per-line facts and text_origin ----------------------------------------------------

def _rec(ago_min, line, chat=CHAT, service="iMessage", who="Maya Chen"):
    return (NOW - ago_min * 60, who, f"{line}" if line.startswith("Me:") else f"{who}: {line}", chat, True, service)


def _item(records):
    [item] = sources.text_items(records, now=NOW, own=ME)
    return item


def test_one_imessage_chat_gives_its_handle_and_service():
    item = _item([_rec(50, "hi"), _rec(40, "Me: hey"), _rec(30, "tea?"), _rec(20, "at 4")])
    assert item["lines"] == ["hi", "tea?", "at 4"]
    assert item["line_chat"] == [CHAT] * 3 and item["line_service"] == ["iMessage"] * 3
    assert len(item["line_ts"]) == len(item["lines"])
    assert sources.text_origin(item) == (HANDLE, "iMessage")
    assert item["handle"] == HANDLE and item["service"] == "iMessage"


def test_one_sms_among_the_last_three_spoils_the_origin():
    item = _item([_rec(30, "a"), _rec(20, "b", service="SMS"), _rec(10, "c")])
    assert item["line_service"] == ["iMessage", "SMS", "iMessage"]
    assert sources.text_origin(item) == ("", "")


@pytest.mark.parametrize("chat,service", [("", ""), ("", "iMessage"), (CHAT, ""), (CHAT, "Telegram")])
def test_an_unknown_line_among_the_last_three_spoils_the_origin(chat, service):
    item = _item([_rec(30, "a"), _rec(20, "b", chat=chat, service=service), _rec(10, "c")])
    assert sources.text_origin(item) == ("", "")


@pytest.mark.parametrize("chat,service", [(CHAT, "SMS"), ("", "")])
def test_an_older_sms_or_unknown_line_does_not_spoil_the_newest_three(chat, service):
    item = _item([_rec(40, "old", chat=chat, service=service), _rec(30, "a"), _rec(20, "b"), _rec(10, "c")])
    assert sources.text_origin(item) == (HANDLE, "iMessage")
    assert item["service"] == ""                  # the whole-window summary still says "mixed"


def test_two_chats_among_the_last_three_give_no_origin():
    item = _item([_rec(30, "a"), _rec(20, "b", chat=OTHER), _rec(10, "c")])
    assert sources.text_origin(item) == ("", "")
    assert item["handle"] == ""


def test_a_group_chat_gives_no_origin_and_no_handle():
    records = [_rec(m, t, chat=GROUP, who="Group \"Book club\"") for m, t in ((30, "a"), (20, "b"), (10, "c"))]
    item = _item(records)
    assert item["group"] is True and item["line_chat"] == [GROUP] * 3
    assert sources.text_origin(item) == ("", "") and item["handle"] == ""


def test_reply_cards_keep_the_handle_when_an_older_line_is_unplaced():
    item = _item([_rec(40, "old", chat="", service=""), _rec(30, "a"), _rec(20, "b"), _rec(10, "c")])
    assert item["handle"] == HANDLE
    assert item["line_chat"][0] == "" and item["line_service"][0] == ""


def test_text_origin_refuses_lists_that_do_not_line_up():
    item = _item([_rec(30, "a"), _rec(20, "b"), _rec(10, "c")])
    assert sources.text_origin(dict(item, line_chat=item["line_chat"][1:])) == ("", "")
    assert sources.text_origin({k: v for k, v in item.items() if k != "line_service"}) == ("", "")
    assert sources.text_origin(item, count=2) == (HANDLE, "iMessage")


def test_older_five_field_records_have_unknown_per_line_service():
    item = _item([(NOW - 600, "Maya Chen", "Maya Chen: hi", CHAT, True)])
    assert item["line_chat"] == [CHAT] and item["line_service"] == [""]
    assert sources.text_origin(item) == ("", "")


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_text_records_join.py"' in manifest
