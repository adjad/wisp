"""Gmail connector: no network, no user data (patched config + caches)."""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-gmail-")
os.environ["WISP_HOME"] = _scratch.name

from service import gmail_source as G
from service.tools import cache_store, email_tools as E

ME = "me@gmail.com"


def _msg(mid, ts, subject, unread=False, sender="Ann <ann@x.com>", body="hello there"):
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "internalDate": str(ts * 1000),
            "labelIds": ["INBOX"] + (["UNREAD"] if unread else []),
            "payload": {"mimeType": "text/plain", "body": {"data": data},
                        "headers": [{"name": "From", "value": sender},
                                    {"name": "To", "value": ME},
                                    {"name": "Subject", "value": subject},
                                    {"name": "Message-ID", "value": f"<{mid}@x>"}]}}


class GmailSource(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cache_store, "save"))
        self.enterContext(patch.multiple(
            E, _headers="", _history="", _raw_emails="", _swift_headers="",
            _swift_history="", _swift_raw="", _ext={"headers": "", "history": "", "raw": ""},
            _ext_address="", _ext_ready_at=0.0, _headers_sync_generation=0,
            _email_available=None, _email_read_source=""))
        G._meta.clear()

    def test_header_line_round_trips_through_the_parser(self):
        meta = G.parse_metadata(_msg("a1", 1_700_000_000, "Hi | there", unread=True))
        E.cache_external_emails(headers=G.header_line(meta, ME, True))
        E.set_external_account(ME)
        rows = E._parse_pipe_lines(E._headers)
        self.assertEqual(rows, [(1_700_000_000.0, ME, "Ann", "Hi | there", True)])

    def test_body_extraction_prefers_plain_and_strips_html(self):
        html = base64.urlsafe_b64encode(b"<p>Hi <b>you</b></p><script>x()</script>").decode()
        self.assertEqual(G.extract_body({"mimeType": "text/html", "body": {"data": html}}), "Hi you")

    def test_swift_rows_for_covered_account_are_replaced_not_doubled(self):
        E.set_external_account(ME)
        E.cache_emails("1700000100 | R | me@gmail.com | Old | old mail\n"
                       "1700000200 | R | School | Reg | other account")
        E.cache_external_emails(headers=G.header_line(
            G.parse_metadata(_msg("a1", 1_700_000_300, "New")), ME, False))
        subjects = [r[3] for r in E._parse_pipe_lines(E._headers)]
        self.assertEqual(subjects, ["New", "other account"])
        # a later Swift push cannot resurrect the covered account's rows
        E.cache_emails("1700000100 | R | me@gmail.com | Old | old mail")
        self.assertEqual([r[3] for r in E._parse_pipe_lines(E._headers)], ["New"])

    def test_fresh_gmail_clears_mail_open_warning_and_unavailable_state(self):
        E._email_available, E._email_read_source = False, "local_index"
        E.set_external_account(ME)
        self.assertEqual(E.email_sync_state(), "unavailable")
        E.cache_external_emails(headers="")
        self.assertEqual(E.email_sync_state(), "ready")
        self.assertEqual(E.email_freshness_warning(), "")
        E._ext_ready_at = time.time() - 3600           # connector stalled
        self.assertEqual(E.email_sync_state(), "unavailable")

    def test_sync_headers_fetches_only_uncached_metadata(self):
        calls = []

        async def fake_get(http, path, params=None):
            calls.append(path)
            if path == "messages":
                unread = ("labelIds", "UNREAD") in params
                return {"messages": [{"id": "m2"}] if unread else [{"id": "m2"}, {"id": "m1"}]}
            return _msg(path.split("/")[1], 1_700_000_000 + int(path[-1]), f"s{path[-1]}")

        async def run():
            with patch.object(G, "_get", fake_get):
                await G._sync_headers(None, ME)
                await G._sync_headers(None, ME)

        E.set_external_account(ME)
        asyncio.run(run())
        self.assertEqual(sum(c.startswith("messages/") for c in calls), 2)  # cached on pass 2
        rows = E._parse_pipe_lines(E._headers)
        self.assertEqual([(r[3], r[4]) for r in rows], [("s2", True), ("s1", False)])

    def test_config_is_private_and_status_reflects_connection(self):
        with patch.object(G, "CONFIG_PATH", Path(_scratch.name) / "gmail.json"):
            self.assertFalse(G.is_connected())
            G._save_config({"client_id": "c", "email": ME, "refresh_token": "r"})
            self.assertTrue(G.is_connected())
            self.assertEqual(os.stat(G.CONFIG_PATH).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
