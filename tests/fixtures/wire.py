"""The wire formats real Swift readers push to /assistant/sync/* — as pure
render functions, no I/O, no clock.

This is the SINGLE implementation of every format documented across
MailReader.swift / MessagesReader.swift / NotesReader.swift /
BrowserHistoryReader.swift, shared by scripts/wisp_testdata.py (in-place
DB/cache seeding) and the sandbox (service/fixtures used to follow this
plan — see the sandbox package once it lands). Two formats had drifted
between the fixtures and what the real backend parsers actually expect
(group-chat context labels, the email read/unread flag) before this module
existed; keeping one implementation is what stops that happening again.

Every function takes plain dicts with ALREADY-RESOLVED epoch timestamps —
the caller owns the clock — and returns the exact string
service/tools/{imessage,email,notes,browser_history}_tools.py's parsers
expect. Each format is pinned against those real parsers in
tests/test_sandbox_wire.py.
"""
from __future__ import annotations

from service.tools.imessage_tools import _norm_handle

FS = "\x01"  # field separator (email_raw, notes)
RS = "\x02"  # record separator (email_raw, notes)


def thread_context(name: str | None, members: list[str], is_group: bool) -> str:
    """Matches MessagesReader.swift's `label(...)` exactly:
      - named group   -> Group "Name"
      - unnamed group -> Group of N (a, b, c, d, +K more)
      - named 1:1     -> the contact/handle name
    `members` should be the OTHER participants' display names/handles (not
    including "Me"). imessage_tools._label_members's regex depends on the
    exact "Group of N (...)" shape, including the ", +K more" suffix only
    appearing past 4 members.
    """
    name = (name or "").strip()
    if name:
        return f'Group "{name}"' if is_group else name
    if is_group:
        shown = sorted(members)[:4]
        more = f", +{len(members) - 4} more" if len(members) > 4 else ""
        return f"Group of {len(members)} ({', '.join(shown)}{more})"
    return members[0] if members else "Unknown"


def messages_lines(rows: list[dict]) -> str:
    """rows: {ts, context, who, text} -> "{ts} | {context} | {who}: {text}"
    joined by "\\n", NO trailing newline, newest-first — matches
    MessagesReader.swift's own merge order. `text` has embedded newlines
    flattened to spaces, same as `oneLine` in the Swift reader.

    Parsed by imessage_tools._parse_lines: `line.split(" | ", 2)` then the
    third field is split again on the FIRST ":" to recover who/text — so a
    colon inside the body text is safe (only the label's own colon matters),
    but a " | " inside `text` or `who` is NOT — same constraint the real
    reader has.
    """
    ordered = sorted(rows, key=lambda r: r["ts"], reverse=True)
    lines = []
    for r in ordered:
        text = str(r["text"]).replace("\r", " ").replace("\n", " ")
        lines.append(f"{r['ts']} | {r['context']} | {r['who']}: {text}")
    return "\n".join(lines)


def contacts_map(raw: dict[str, str]) -> dict[str, str]:
    """raw handle (any format) -> name  =>  normalized handle -> name, the
    shape cache_contacts()/imessage_tools._contacts expects."""
    return {_norm_handle(h): name for h, name in raw.items() if h and name}


def contact_handles_map(raw: dict[str, str]) -> dict[str, list[str]]:
    """raw handle -> name  =>  lowercased name -> [original-form handles],
    the shape _name_handles expects (send_message needs the UN-normalized
    handle to actually address Messages/Mail)."""
    by_name: dict[str, list[str]] = {}
    for handle, name in raw.items():
        if handle and name:
            by_name.setdefault(str(name).strip().lower(), []).append(str(handle).strip())
    return by_name


def email_header_line(e: dict) -> str:
    """e: {ts, unread, account, sender_name, sender_addr, subject}
    -> "{ts} | {R|U} | {account} | {name} <{addr}> | {subject}"

    R/U is field 2 (not last) so subject can safely contain " | " — see
    email_tools._parse_pipe_lines's docstring for why."""
    flag = "U" if e.get("unread") else "R"
    sender = f"{e['sender_name']} <{e['sender_addr']}>"
    return f"{e['ts']} | {flag} | {e['account']} | {sender} | {e['subject']}"


def email_headers(rows: list[dict]) -> str:
    """Newest-first, joined "\\n", WITH a trailing "\\n" — matches
    MailReader.swift's mergeHeaderChunks exactly (headers and history share
    this renderer; the only difference is which rows the caller passes)."""
    ordered = sorted(rows, key=lambda r: r["ts"], reverse=True)
    if not ordered:
        return ""
    return "\n".join(email_header_line(e) for e in ordered) + "\n"


def email_raw(rows: list[dict]) -> str:
    """e: {ts, unread, account, sender_name, sender_addr, to: [addr,...],
    subject, message_id, body, account_id?} -> 9 FS-separated fields when a
    native account ID is supplied, otherwise the legacy 8 fields. Each
    record RS-TERMINATED (a trailing RS on the last record is correct —
    email_tools._parse_raw splits on RS and strips each piece).

    Field order: ts, R|U, account, optional account_id, "name <addr>", to(", "-joined), subject,
    message_id, body — matches MailReader.swift's rawScript exactly."""
    parts = []
    for e in rows:
        flag = "U" if e.get("unread") else "R"
        sender = f"{e['sender_name']} <{e['sender_addr']}>"
        to = ", ".join(e.get("to") or [])
        fields = [str(e["ts"]), flag, e["account"], sender, to,
                  e["subject"], e.get("message_id", ""), e["body"]]
        if "account_id" in e:
            fields.insert(3, e["account_id"])
        parts.append(FS.join(fields) + RS)
    return "".join(parts)


def notes_raw(rows: list[dict]) -> str:
    """n: {ts, title, folder, body} -> 4 FS-separated fields per record,
    each record RS-terminated. Matches NotesReader.swift's AppleScript
    exactly — no sort/merge, notes_tools._parse doesn't require one either."""
    parts = []
    for n in rows:
        fields = [str(n["ts"]), n["title"], n.get("folder", ""), n["body"]]
        parts.append(FS.join(fields) + RS)
    return "".join(parts)


def browser_lines(rows: list[dict]) -> str:
    """b: {ts, host, path, title} -> "{ts} | {host} | {path} | {title}"
    joined "\\n", newest-first — matches BrowserHistoryReader.swift's
    formatLine. Parsed via `line.split(" | ", 3)`."""
    ordered = sorted(rows, key=lambda r: r["ts"], reverse=True)
    return "\n".join(f"{r['ts']} | {r['host']} | {r['path']} | {r['title']}"
                     for r in ordered)
