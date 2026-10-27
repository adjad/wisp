"""A bare "email summary" / "text summary" is a read, not a send — regression.

Debug export 2026-10-22: "email summary" took the grounded-outbound route
(summarize_emails -> lookup_contact -> send_email) because the imperative
"email <word>" compose branch and web_request's delivery detector both read
"summary" as a recipient. The model then fabricated a weekly summary addressed
to Mom's phone number. "text summary" fell to the compiler's "couldn't tell
what to send" question instead of summarizing messages.

    .venv/bin/python tests/test_bare_summary_is_read.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.router import router as r  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print(f"{'ok  ' if ok else 'FAIL'} {name} {detail if not ok else ''}")


def test_bare_summary_reads() -> None:
    for text, want in (("email summary", "summarize_emails"),
                       ("Email summary", "summarize_emails"),
                       ("email summaries", "summarize_emails"),
                       ("text summary", "summarize_messages"),
                       ("Text summary", "summarize_messages")):
        d = asyncio.run(r.route(text))
        sub = set(d.tool_subset or [])
        check(f"{text!r} routes to a read", want in sub, f"-> {d.reason} {sorted(sub)}")
        check(f"{text!r} offers no send tool",
              not sub & {"send_email", "send_message", "lookup_contact"}, f"-> {sorted(sub)}")


def test_real_sends_still_route() -> None:
    for text in ("email summary to mom", "text mom a summary of my unread emails",
                 "email mom hi"):
        d = asyncio.run(r.route(text))
        sub = set(d.tool_subset or [])
        check(f"{text!r} still offers a send", bool(sub & {"send_email", "send_message"}),
              f"-> {d.reason}")


if __name__ == "__main__":
    test_bare_summary_reads()
    test_real_sends_still_route()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
