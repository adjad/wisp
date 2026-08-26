"""tests/fixtures/wire.py — round-trip every renderer through the REAL
backend parsers (service/tools/{imessage,email,notes}_tools.py), not a
reimplementation of them. This is what stops the wire format silently
drifting from what the Swift readers actually send, which is exactly what
happened before this module existed (see wire.py's docstring): the old
wisp_testdata.py fixtures used "Group: Name" where the real parser expects
'Group "Name"', and had no read/unread flag in the header format at all.

What must keep holding:
  * every field survives a render -> parse round trip losslessly;
  * a subject/body containing the field separator (" | ", or the raw
    format's \\x01/\\x02) doesn't corrupt adjacent fields;
  * a body with embedded newlines survives (messages flattens them to
    spaces like the real reader; notes/email raw don't, since \\x01/\\x02
    never collide with \\n);
  * all three group-context shapes parse the way the real backend expects.

    .venv/bin/python tests/test_sandbox_wire.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# Redirect WISP_HOME before importing service.* — cache_messages/cache_emails/
# cache_notes persist to CACHE_DIR (service/tools/cache_store.py, itself under
# service.paths.MOE_DIR). Without this the test would read/write the user's
# real ~/.moe/cache.
SCRATCH = tempfile.mkdtemp(prefix="wisp-wire-test-")
os.environ["WISP_HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import persona, wire  # noqa: E402
from service.tools import email_tools, imessage_tools, notes_tools  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


NOW = 1_800_000_000.0  # fixed reference time — deterministic test data


def test_messages_round_trip() -> None:
    print("\nmessages: render -> imessage_tools._parse_lines")
    rows = persona.message_rows(NOW)
    rendered = wire.messages_lines(rows)
    check("no trailing newline", not rendered.endswith("\n"), repr(rendered[-20:]))

    imessage_tools.cache_contacts(persona.RAW_CONTACTS)
    imessage_tools.cache_messages(rendered, available=True)
    parsed = imessage_tools._parse_lines()
    check("same row count", len(parsed) == len(rows),
          f"got {len(parsed)}, expected {len(rows)}")

    by_text = {r["text"]: r for r in rows}
    for ts, ctx, who_text in parsed:
        who, _, text = who_text.partition(": ")
        check(f"text preserved: {text[:30]!r}", text in by_text,
              f"not found among source texts")

    # names visible in the rendered contexts.
    contexts = {r["context"] for r in rows}
    parsed_contexts = {ctx for _, ctx, _ in parsed}
    check("group context 'Grad School GC' parsed",
          any('Group "Grad School GC"' == c for c in parsed_contexts),
          str(parsed_contexts))
    check("1:1 context 'Mom' parsed", "Mom" in parsed_contexts, str(parsed_contexts))
    check("unsaved handle context preserved", "+19998887777" in parsed_contexts,
          str(parsed_contexts))


def test_messages_pipe_in_body_and_embedded_newline() -> None:
    print("\nmessages: a body containing ' | ' and embedded newlines")
    row = {"ts": NOW, "context": "Mom", "who": "Mom",
          "text": "call me at 5 | or just text, ok?\nsecond line"}
    rendered = wire.messages_lines([row])
    imessage_tools.cache_contacts(persona.RAW_CONTACTS)
    imessage_tools.cache_messages(rendered, available=True)
    parsed = imessage_tools._parse_lines()
    check("exactly one row parsed", len(parsed) == 1, str(parsed))
    if parsed:
        ts, ctx, who_text = parsed[0]
        check("context still 'Mom' (unaffected by body's ' | ')", ctx == "Mom", ctx)
        check("embedded newline flattened to space",
              "\n" not in who_text and "second line" in who_text, repr(who_text))
        # The " | " inside the body is NOT safe past the 3-field split — this
        # documents that constraint rather than papering over it: everything
        # after the first two " | " delimiters is the third field verbatim.
        check("body content after the delimiter still present",
              "or just text, ok?" in who_text, repr(who_text))


def test_thread_context_three_shapes() -> None:
    print("\nthread_context: the three shapes MessagesReader.swift produces")
    named_1on1 = wire.thread_context(None, ["Mom"], is_group=False)
    check("named 1:1 -> bare name", named_1on1 == "Mom", named_1on1)

    named_group = wire.thread_context("Grad School GC", [], is_group=True)
    check("named group -> Group \"Name\"", named_group == 'Group "Grad School GC"',
          named_group)

    unnamed_small = wire.thread_context(None, ["Priya", "Alex Chen"], is_group=True)
    check("unnamed group (<=4) -> Group of N (a, b)",
          unnamed_small == "Group of 2 (Alex Chen, Priya)", unnamed_small)

    unnamed_big = wire.thread_context(
        None, ["Priya", "Alex Chen", "Mom", "Dad", "Jordan Ellis", "Dr. Patel"],
        is_group=True)
    check("unnamed group (>4) -> ', +K more' suffix",
          unnamed_big.endswith(", +2 more)"), unnamed_big)
    m = imessage_tools._GROUP_MEMBERS_RE.match(unnamed_big)
    check("matches the real backend's _GROUP_MEMBERS_RE", m is not None, unnamed_big)


def test_email_headers_round_trip() -> None:
    print("\nemail headers: render -> email_tools._parse_pipe_lines")
    rows = persona.email_header_rows(NOW, deep_history=True)
    rendered = wire.email_headers(rows)
    check("trailing newline present", rendered.endswith("\n"), repr(rendered[-5:]))

    email_tools.cache_emails(rendered)
    parsed = email_tools._parse_lines()
    check("same row count", len(parsed) == len(rows),
          f"got {len(parsed)}, expected {len(rows)}")

    by_subject = {r["subject"]: r for r in rows}
    for ts, account, sender, subject, unread in parsed:
        src = by_subject.get(subject)
        check(f"subject matched: {subject[:30]!r}", src is not None, subject)
        if src is None:
            continue
        check(f"account preserved for {subject[:20]!r}", account == src["account"],
              f"got {account!r}")
        check(f"sender preserved for {subject[:20]!r}",
              sender == f"{src['sender_name']} <{src['sender_addr']}>", sender)
        check(f"unread flag preserved for {subject[:20]!r}",
              unread == bool(src["unread"]), f"got {unread}")


def test_email_header_subject_with_pipe() -> None:
    print("\nemail headers: subject containing ' | ' survives (it's the last field)")
    row = {"ts": NOW, "unread": True, "account": "Personal",
          "sender_name": "Test Sender", "sender_addr": "t@example.test",
          "subject": "Quarterly report | Q3 numbers | final"}
    rendered = wire.email_headers([row])
    email_tools.cache_emails(rendered)
    parsed = email_tools._parse_lines()
    check("exactly one row", len(parsed) == 1, str(parsed))
    if parsed:
        check("full subject with embedded ' | ' preserved",
              parsed[0][3] == row["subject"], parsed[0][3])
        check("unread flag still correct despite pipe-heavy subject",
              parsed[0][4] is True, str(parsed[0][4]))


def test_email_raw_round_trip() -> None:
    print("\nemail raw: render -> email_tools._parse_raw")
    rows = persona.email_raw_rows(NOW)
    rendered = wire.email_raw(rows)

    email_tools.cache_raw_emails(rendered)
    parsed = email_tools._parse_raw()
    check("same row count", len(parsed) == len(rows),
          f"got {len(parsed)}, expected {len(rows)}")

    by_msgid = {r["message_id"]: r for r in rows}
    for p in parsed:
        src = by_msgid.get(p["message_id"])
        check(f"message_id matched: {p['message_id']}", src is not None, p["message_id"])
        if src is None:
            continue
        check(f"body preserved for {p['message_id']}", p["body"] == src["body"],
              p["body"][:60])
        check(f"to preserved for {p['message_id']}",
              p["to"] == ", ".join(src["to"]), p["to"])
        check(f"unread preserved for {p['message_id']}",
              p["unread"] == bool(src["unread"]), str(p["unread"]))


def test_email_raw_body_with_control_chars_absent() -> None:
    print("\nemail raw: a body with newlines and pipes (not \\x01/\\x02) survives")
    row = {"ts": NOW, "unread": False, "account": "Work",
          "sender_name": "S", "sender_addr": "s@example.test", "to": ["t@example.test"],
          "subject": "multi | line",
          "message_id": "<edge-case@sandbox.wisp.test>",
          "body": "line one\nline two | with a pipe\nline three"}
    rendered = wire.email_raw([row])
    email_tools.cache_raw_emails(rendered)
    parsed = email_tools._parse_raw()
    check("exactly one record", len(parsed) == 1, str(parsed))
    if parsed:
        check("body byte-identical including newlines and pipes",
              parsed[0]["body"] == row["body"], repr(parsed[0]["body"]))
        check("subject with pipe preserved (raw format has no delimiter ambiguity)",
              parsed[0]["subject"] == row["subject"], parsed[0]["subject"])


def test_notes_round_trip() -> None:
    print("\nnotes: render -> notes_tools._parse")
    rows = persona.note_rows(NOW)
    rendered = wire.notes_raw(rows)

    notes_tools.cache_notes(rendered)
    parsed = notes_tools._parse()
    check("same row count", len(parsed) == len(rows),
          f"got {len(parsed)}, expected {len(rows)}")

    by_title = {r["title"]: r for r in rows}
    for p in parsed:
        src = by_title.get(p["title"])
        check(f"title matched: {p['title']!r}", src is not None, p["title"])
        if src is None:
            continue
        check(f"body with embedded newlines preserved for {p['title']!r}",
              p["body"] == src["body"], repr(p["body"][:60]))
        check(f"folder preserved for {p['title']!r}", p["folder"] == src["folder"],
              p["folder"])


def test_contacts_maps() -> None:
    print("\ncontacts_map / contact_handles_map")
    norm, raw = persona.contacts()
    check("norm map non-empty", len(norm) == len(persona.RAW_CONTACTS), str(len(norm)))
    for handle, name in persona.RAW_CONTACTS.items():
        n = imessage_tools._norm_handle(handle)
        check(f"norm handle for {handle!r} maps to {name!r}", norm.get(n) == name,
              f"got {norm.get(n)!r}")

    handles_map = persona.contact_handles()
    check("Priya has 2 handles (phone + email)",
          len(handles_map.get("priya", [])) == 2, str(handles_map.get("priya")))


def main() -> int:
    print(f"scratch: {SCRATCH}")
    test_messages_round_trip()
    test_messages_pipe_in_body_and_embedded_newline()
    test_thread_context_three_shapes()
    test_email_headers_round_trip()
    test_email_header_subject_with_pipe()
    test_email_raw_round_trip()
    test_email_raw_body_with_control_chars_absent()
    test_notes_round_trip()
    test_contacts_maps()
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
