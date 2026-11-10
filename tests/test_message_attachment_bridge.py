"""Files texted in Messages over the mail-memory bridge: what is asked and how replies are cleaned.
Fakes only (the app is replaced by a stub `_ask`); no Messages, no model."""
import base64
import math

import pytest

from service.mail_memory import bridge


def row(**over):
    base = {"id": "42", "ts": 1_700_000_000.5, "chat": "Book Club", "sender": "+15551234567", "from_me": False,
            "name": "lease.pdf", "mime": "application/pdf", "size": 10, "on_disk": True}
    base.update(over)
    return base


def fake_ask(monkeypatch, replies):
    seen = []

    async def ask(kind, timeout, **fields):
        seen.append((kind, timeout, fields))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(bridge, "_ask", ask)
    return seen


async def test_index_request_shape(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "rows": [row()], "capped": True}])
    rows, capped = await bridge.read_message_attachment_index(["lease", "pdf"], ["+1555"], 5, 9, limit=50)
    assert seen == [("message_attachments", 10.0, {"terms": ["lease", "pdf"], "handles": ["+1555"], "since": 5.0,
                                                    "until": 9.0, "limit": 50})]
    assert capped is True and rows == [row()]


async def test_index_request_is_cleaned_and_clamped(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "rows": []}] * 3)
    handles = [f"h{i}" for i in range(15)]
    await bridge.read_message_attachment_index([" a ", "", "  ", "b" * 200, "c", "d", "e", "f", "g"], handles + ["h1", "h1"],
                                               None, 0, limit=99999)
    fields = seen[0][2]
    assert fields["terms"] == ["a", "b" * 80, "c", "d", "e"] and bridge.ATTACHMENT_TERMS_MAX == 5
    assert fields["handles"] == handles[:10] and bridge.MESSAGE_HANDLES_MAX == 10
    assert fields["limit"] == 400 == bridge.MESSAGE_ATTACHMENT_LIMIT_MAX
    assert fields["since"] == 0.0 and fields["until"] == 0.0
    await bridge.read_message_attachment_index([], ["", " ", "a\nb", "x" * 150, "ok", "ok", "\x00"], 0, 0, limit=0)
    fields = seen[1][2]
    assert fields["handles"] == ["x" * 100, "ok"] and fields["limit"] == 1 and fields["terms"] == []
    await bridge.read_message_attachment_index(None, None, 0, 0)
    assert seen[2][2]["terms"] == [] and seen[2][2]["handles"] == [] and seen[2][2]["limit"] == 200


async def test_index_bad_arguments_never_raise_and_are_clamped(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "rows": []}] * 12)
    cases = [
        (float("nan"), 0.0), (float("inf"), 0.0), (float("-inf"), 0.0), (-5, 0.0), (-0.5, 0.0), (True, 0.0),
        ("12", 0.0), (None, 0.0), ([1], 0.0), (10**400, 0.0), (12.5, 12.5), (7, 7.0),
    ]
    for since, _ in cases:
        await bridge.read_message_attachment_index([], [], since, since)
    assert len(seen) == len(cases)
    for (_, want), (kind, _timeout, fields) in zip(cases, seen):
        assert kind == "message_attachments"
        assert fields["since"] == want and fields["until"] == want and type(fields["since"]) is float


@pytest.mark.parametrize("limit, want", [
    (5, 5), (400, 400), (401, 400), (10**30, 400), (0, 1), (-3, 1), (7.0, 7), (7.5, 200), (float("nan"), 200),
    (float("inf"), 200), (True, 200), (False, 200), ("9", 200), (None, 200), ([3], 200), (b"3", 200),
])
async def test_index_limit_is_int_like_or_the_default(monkeypatch, limit, want):
    seen = fake_ask(monkeypatch, [{"ok": True, "rows": []}])
    await bridge.read_message_attachment_index([], [], 0, 0, limit=limit)
    assert seen[0][2]["limit"] == want and type(seen[0][2]["limit"]) is int


async def test_index_terms_and_handles_are_cleaned_for_the_apps_strict_parser(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "rows": []}] * 5)
    await bridge.read_message_attachment_index(["a\nb", "\x00", "\x07x", "t" * 150, 5, None, "lease  "], [5, None, "ok"], 0, 0)
    fields = seen[0][2]
    assert fields["terms"] == ["a b", "x", "t" * 80, "lease"]
    assert all(1 <= len(t) <= 100 and not bridge._has_control(t) for t in fields["terms"])
    assert fields["handles"] == ["ok"]
    for not_a_list in ("lease", 5, {"a": 1}, b"abc"):
        await bridge.read_message_attachment_index(not_a_list, not_a_list, 0, 0)
    assert len(seen) == 5
    assert all(s[2]["terms"] == [] and s[2]["handles"] == [] for s in seen[1:])


async def test_index_rows_are_revalidated(monkeypatch):
    good = row()
    bad = [
        "junk", None, 5, {},
        row(id="12a"), row(id=""), row(id="1" * 19), row(id=42), row(id="4\n"), row(id="-1"), row(id="١٢"),
        row(ts=float("nan")), row(ts=math.inf), row(ts=-1), row(ts="1"), row(ts=True), row(ts=None),
        row(chat=5), row(sender=None), row(name=""), row(name=None), row(mime=3),
        row(from_me=1), row(from_me="false"), row(on_disk=1), row(on_disk=None),
        row(size=-1), row(size=1.5), row(size="9"), row(size=True), row(size=None),
    ]
    missing = row()
    del missing["mime"]
    fake_ask(monkeypatch, [{"ok": True, "rows": bad + [missing, good]}])
    rows, capped = await bridge.read_message_attachment_index([], [], 0, 0)
    assert rows == [good] and capped is False


async def test_index_rows_are_clamped(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True, "rows": [
        row(chat="c" * 500, sender="s" * 500, name="n" * 500, mime="m" * 500, ts=7)], "capped": "yes"}])
    rows, capped = await bridge.read_message_attachment_index([], [], 0, 0)
    assert len(rows[0]["chat"]) == 120 and len(rows[0]["sender"]) == 200
    assert len(rows[0]["name"]) == 255 and len(rows[0]["mime"]) == 100
    assert rows[0]["ts"] == 7.0 and type(rows[0]["ts"]) is float
    assert capped is False
    assert set(rows[0]) == {"id", "ts", "chat", "sender", "from_me", "name", "mime", "size", "on_disk"}


async def test_index_from_me_row_keeps_empty_sender(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True, "rows": [row(sender="", from_me=True)]}])
    rows, _ = await bridge.read_message_attachment_index([], [], 0, 0)
    assert rows[0]["sender"] == "" and rows[0]["from_me"] is True


async def test_index_rows_missing_or_not_a_list(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True}, {"ok": True, "rows": None}])
    assert await bridge.read_message_attachment_index([], [], 0, 0) == ([], False)
    assert await bridge.read_message_attachment_index([], [], 0, 0) == ([], False)


async def test_index_rows_truthy_but_not_a_list(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True, "rows": 5, "capped": False}, {"ok": True, "rows": {"id": "1"}},
                           {"ok": True, "rows": "abc"}])
    for _ in range(3):
        assert await bridge.read_message_attachment_index([], [], 0, 0) == ([], False)


async def test_index_errors(monkeypatch):
    fake_ask(monkeypatch, [{"ok": False, "error": "no_messages_db"}, {"ok": False}, {"error": "x"},
                           {"ok": "true", "rows": [row()]}, bridge.BridgeError("app_not_connected")])
    with pytest.raises(bridge.BridgeError, match="^no_messages_db$"):
        await bridge.read_message_attachment_index([], [], 0, 0)
    with pytest.raises(bridge.BridgeError, match="^message_attachments_failed$"):
        await bridge.read_message_attachment_index([], [], 0, 0)
    with pytest.raises(bridge.BridgeError, match="^x$"):
        await bridge.read_message_attachment_index([], [], 0, 0)
    with pytest.raises(bridge.BridgeError, match="^message_attachments_failed$"):
        await bridge.read_message_attachment_index([], [], 0, 0)
    with pytest.raises(bridge.BridgeError, match="app_not_connected"):
        await bridge.read_message_attachment_index([], [], 0, 0)


async def test_read_request_shape_and_success(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "name": "a.pdf", "size": 3, "data_b64": base64.b64encode(b"abc").decode()}])
    assert await bridge.read_message_attachment("42") == (b"abc", "a.pdf", "")
    assert seen == [("message_attachment", 45.0, {"attachment_id": "42"})]


async def test_read_never_sends_a_path_or_bad_id(monkeypatch):
    seen = fake_ask(monkeypatch, [])
    for bad in ("", "../x", "1/2", "12a", "1" * 19, "4\n", "-1", "١٢", " 1", None, 5, b"1"):
        assert await bridge.read_message_attachment(bad) == (None, "", "not_found")
    assert seen == []


@pytest.mark.parametrize("why", ["not_found", "not_on_disk", "too_large", "unreadable", "no_messages_db"])
async def test_read_maps_the_apps_reason(monkeypatch, why):
    fake_ask(monkeypatch, [{"ok": False, "error": why}])
    assert await bridge.read_message_attachment("7") == (None, "", why)


async def test_read_unknown_reasons_are_unreadable(monkeypatch):
    fake_ask(monkeypatch, [{"ok": False, "error": "weird"}, {"ok": False}, {"ok": False, "error": "/etc/passwd"},
                           {"ok": False, "error": 5}, {"ok": "yes", "data_b64": "AAAA"}])
    for _ in range(5):
        assert await bridge.read_message_attachment("7") == (None, "", "unreadable")


async def test_read_base64_errors(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True, "data_b64": "!!!"}, {"ok": True, "data_b64": "abc"},
                           {"ok": True, "data_b64": "YQ==\n"}, {"ok": True, "data_b64": 7}])
    for _ in range(4):
        assert await bridge.read_message_attachment("7") == (None, "", "unreadable")


async def test_read_missing_data_is_empty_bytes_and_name_is_cut(monkeypatch):
    fake_ask(monkeypatch, [{"ok": True}, {"ok": True, "name": "n" * 400, "data_b64": base64.b64encode(b"x").decode()},
                           {"ok": True, "name": 5, "data_b64": ""}])
    assert await bridge.read_message_attachment("7") == (b"", "", "")
    data, name, why = await bridge.read_message_attachment("7")
    assert data == b"x" and len(name) == 255 and why == ""
    assert (await bridge.read_message_attachment("7"))[1] == "5"


async def test_read_bridge_error_propagates(monkeypatch):
    fake_ask(monkeypatch, [bridge.BridgeError("message_attachment_timeout")])
    with pytest.raises(bridge.BridgeError, match="message_attachment_timeout"):
        await bridge.read_message_attachment("7")
