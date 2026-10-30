"""rc2 duplicated email summaries (build 2879 debug export).

1. Two linked Google accounts, each also read through Mail.app: the digest was
   printed twice (a combined "Your accounts" digest on top of the per-account
   ones) and the account status lines repeated.
2. The same message arriving from the Gmail API and from Mail.app's cache was
   listed twice inside one account, because Gmail reports ``<id@host>`` and
   Mail's SQLite reader an opaque hash, with timestamps a few seconds apart.

All accounts and mail here are synthetic.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-summary-dedupe-")
os.environ["WISP_HOME"] = _scratch.name
os.environ["MOE_DIR"] = _scratch.name

from service import gmail_source as G  # noqa: E402
from service.tools import cache_store, email_tools as E  # noqa: E402

FS = "\x01"
ME = "me@gmail.test"
SCHOOL = "me@school.test"
NOW = time.time()


def gmail_row(account, ts, sender, address, subject, mid, gmail_id, unread=True):
    return FS.join(["H2", str(int(ts)), "U" if unread else "R", account, account,
                    sender, address, f"<{mid}>", subject, gmail_id])


def mail_row(label, account_id, ts, sender, address, subject, db_id, unread=True, mid=""):
    # Mail's SQLite reader: an opaque numeric hash where the Message-ID goes.
    return FS.join(["H2", str(int(ts)), "U" if unread else "R", label, account_id,
                    sender, address, mid or str(900000 + db_id), subject, f"db:{db_id}"])


def digests(output):
    return output.count("Inbox digest")


class SummaryDedupe(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cache_store, "CACHE_DIR", Path(_scratch.name) / self._testMethodName))
        self.enterContext(patch.multiple(
            E, _headers="", _history="", _raw_emails="", _swift_headers="", _swift_history="",
            _swift_raw="", _external_accounts={}, _headers_sync_generation=0, _email_available=None,
            _email_read_source="", _email_reason="", _email_sync_pending=False,
            _native_headers_at=0.0, _native_raw_at=0.0, _native_source="", _swift_identity=[],
            _identity_emails=[]))
        self.enterContext(patch.object(E, "_native_catalog_inventory", lambda: []))
        self.enterContext(patch.multiple(G, _last={}, _errors={}, _access={}, _epochs={}, _locks={},
                                        _meta={}, _pending_flow=None, _flow_generation=0,
                                        _authorization={"state": "idle", "generation": 0, "error": ""}))
        self.accounts = {}

        async def no_refresh(http, account):
            G._last.setdefault(account, {})["headers"] = time.time()
        self.enterContext(patch.object(G, "_sync_headers", no_refresh))
        self.enterContext(patch.object(G, "connected_accounts", lambda cfg=None: dict(self.accounts)))

    def link(self, *addresses):
        self.accounts = {a: {"client_id": "synthetic", "refresh_token": "fake-" + a} for a in addresses}
        E.configure_external_accounts(self.accounts)
        for address in addresses:
            G._last[address] = {"headers": time.time()}

    def native(self, *rows):
        E.cache_emails("\n".join(rows))

    # ------------------------------------------------------------- bug 2

    def test_same_message_from_gmail_and_mail_is_one_row(self):
        gmail = E._parse_header_records(
            gmail_row(SCHOOL, NOW, "Prof Ada", "ada@school.test", "Exam reminder", "m1@school.test", "g1"),
            snapshot="recent")
        # Three seconds later on Mail's clock, hash instead of Message-ID, other native namespace.
        mail = E._parse_header_records(
            mail_row(SCHOOL, SCHOOL, NOW + 3, "Prof Ada", "ada@school.test", "Exam reminder", 7),
            snapshot="history")
        rows = E._unique_records(gmail + mail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_snapshot"], "recent")

    def test_bracketed_and_bare_message_ids_are_the_same_message(self):
        a = E._parse_header_records(
            gmail_row(SCHOOL, NOW, "Ann", "ann@x.test", "Hi", "same@host.test", "g1"), snapshot="recent")
        b = E._parse_header_records(
            mail_row(SCHOOL, SCHOOL, NOW + 600, "Ann", "ann@x.test", "Hi", 3, mid="SAME@host.test"),
            snapshot="history")
        self.assertEqual(len(E._unique_records(a + b)), 1)

    def test_distinct_messages_with_the_same_subject_stay_distinct(self):
        # Two real receipts from one reader, and a conflicting RFC id across readers.
        a = [gmail_row(SCHOOL, NOW, "Pay", "pay@x.test", "Receipt", f"r{i}@x.test", f"g{i}") for i in (1, 2)]
        rows = E._unique_records(E._parse_header_records("\n".join(a), snapshot="recent"))
        self.assertEqual(len(rows), 2)
        other = E._parse_header_records(
            mail_row(SCHOOL, SCHOOL, NOW + 1, "Pay", "pay@x.test", "Receipt", 5, mid="r9@x.test"),
            snapshot="history")
        self.assertEqual(len(E._unique_records(rows + other)), 3)

    def test_collapse_is_one_to_one_and_counts_match_the_lines(self):
        gmail = "\n".join(gmail_row(SCHOOL, NOW - i, "Ed", "ed@ed.test", "Exam reminder",
                                    f"e{i}@ed.test", f"g{i}") for i in range(4))
        mail = "\n".join(mail_row(SCHOOL, SCHOOL, NOW - i + 2, "Ed", "ed@ed.test", "Exam reminder", i)
                         for i in range(4))
        rows = E._parse_header_records(gmail, snapshot="recent") + E._parse_header_records(
            mail, snapshot="history")
        out = E.sender_digest(rows, "today")
        self.assertIn("4 emails", out)
        self.assertIn("4 need attention", out)
        self.assertIn("(+1 more from this sender)", out)
        self.assertEqual(out.count("Exam reminder"), 3)

    def test_day_summary_lists_a_cross_source_message_once(self):
        self.link(SCHOOL)
        E.cache_external_emails(account=SCHOOL, headers=gmail_row(
            SCHOOL, NOW, "Prof Ada", "ada@school.test", "Exam reminder", "m1@school.test", "g1"))
        E.cache_history_headers(
            mail_row(SCHOOL, SCHOOL, NOW + 4, "Prof Ada", "ada@school.test", "Exam reminder", 7))
        out = asyncio.run(E.summarize_emails(day="today"))
        self.assertEqual(out.count("Exam reminder"), 1)
        self.assertIn("1 email", out)
        self.assertIn("Scanned 2 headers; represented 1 message", out)

    # ------------------------------------------------------------- bug 1

    def two_accounts(self):
        self.link(ME, SCHOOL)
        # ME's Gmail-API copy; Mail.app holds the same messages under an account
        # labelled "Google" that is not bound to the address.
        E.cache_external_emails(account=ME, headers="\n".join([
            gmail_row(ME, NOW - 10, "Paula", "paula@pay.test", "Your statement", "p1@pay.test", "g1"),
            gmail_row(ME, NOW - 20, "Newsletter", "news@letters.test", "Weekly issue", "n1@letters.test", "g2")]))
        E.cache_external_emails(account=SCHOOL, headers=gmail_row(
            SCHOOL, NOW - 30, "Prof Ada", "ada@school.test", "Exam reminder", "m1@school.test", "g3"))
        self.native(
            mail_row("Google", "uuid-google", NOW - 9, "Paula", "paula@pay.test", "Your statement", 1),
            mail_row("Google", "uuid-google", NOW - 19, "Newsletter", "news@letters.test", "Weekly issue", 2))

    def test_two_linked_accounts_get_one_digest_each_and_no_combined_one(self):
        self.two_accounts()
        out = asyncio.run(E.summarize_emails(day="today"))
        self.assertEqual(digests(out), 2)
        self.assertNotIn("Your accounts", out)
        self.assertIn("## " + ME, out)
        self.assertIn("## " + SCHOOL, out)
        self.assertEqual(out.count("Your statement"), 1)
        self.assertEqual(out.count("Exam reminder"), 1)

    def test_unlinked_mail_account_still_gets_its_own_section(self):
        self.two_accounts()
        self.native(mail_row("iCloud", "uuid-icloud", NOW - 5, "Bo", "bo@friend.test", "Lunch plans", 3))
        out = asyncio.run(E.summarize_emails(day="today"))
        self.assertEqual(digests(out), 3)
        head, rest = out.split("## " + ME, 1)
        self.assertIn("## Your accounts", head)
        self.assertIn("Lunch plans", head)
        self.assertNotIn("Your statement", head)
        self.assertNotIn("Paula", head)

    def test_each_status_line_appears_once(self):
        self.two_accounts()
        E._external_accounts[ME]["headers_verified"] = False
        E._external_accounts[SCHOOL]["headers_verified"] = False
        out = asyncio.run(E.summarize_emails(day="today"))
        lines = [line for line in out.splitlines() if line.startswith((ME + ":", SCHOOL + ":", "Google:"))]
        self.assertTrue(lines)
        self.assertEqual(len(lines), len(set(lines)), lines)
        # The Mail-cache status of the unbound "Google" account survives its
        # digest being folded into the linked account's.
        self.assertTrue(any(line.startswith("Google:") for line in lines))

    def test_a_single_linked_account_keeps_a_single_digest(self):
        self.link(ME)
        E.cache_external_emails(account=ME, headers=gmail_row(
            ME, NOW - 10, "Paula", "paula@pay.test", "Your statement", "p1@pay.test", "g1"))
        self.native(mail_row("Google", "uuid-google", NOW - 9, "Paula", "paula@pay.test", "Your statement", 1))
        out = asyncio.run(E.summarize_emails(day="today"))
        self.assertEqual(digests(out), 1)
        self.assertEqual(out.count("Your statement"), 1)

    # --------------------------------------------- repair round: M1 / M2

    def parse(self, line, snapshot="recent"):
        return E._parse_header_records(line, snapshot=snapshot)[0]

    def test_incomplete_account_less_scan_is_not_dropped(self):
        self.two_accounts()
        # An unlinked Mail account with nothing today, whose scan skipped headers.
        self.native(
            mail_row("Google", "uuid-google", NOW - 9, "Paula", "paula@pay.test", "Your statement", 1),
            mail_row("iCloud", "uuid-icloud", NOW - 90000, "Bo", "bo@friend.test", "Old note", 3),
            FS.join(["C2", "iCloud", "uuid-icloud", "5", "2", "0"]))
        # Mail verified fresh, so the only caveat left is the scan coverage itself.
        self.enterContext(patch.multiple(E, _email_read_source="mail_app", _email_available=True,
                                         _native_headers_at=time.time(), _native_history_at=time.time()))
        out = asyncio.run(E.summarize_emails(day="today"))
        head = out.split("## " + ME, 1)[0]
        self.assertIn("## Your accounts", head)
        self.assertIn("skipped 2 malformed headers", head)
        self.assertEqual(digests(out), 2)

    def test_plain_empty_bodies_are_the_only_ones_dropped(self):
        for text in ("No emails found for Tuesday, October 27.",
                     "No emails in Tuesday (Tue Oct 27, 12:00 AM to Wed Oct 28, 12:00 AM). Scanned 0 matching "
                     "headers; represented 0 messages; truncated 0 messages.",
                     "No unread email found in your recent inbox."):
            self.assertTrue(E._is_plain_empty(text), text)
        for text in ("No parseable emails in the available recent header cache; total coverage is unknown.",
                     "No unread email found in the available recent cache; total coverage is unknown.",
                     "No matching headers in the available cache for today. Recent header scan did not "
                     "complete for iCloud; requested mail may be outside the cache.",
                     "No emails found for today.\n\nRead-status filter: 3 cached message(s) lack read status "
                     "and are excluded; their unread state is unknown.",
                     "\U0001F4EC **Inbox digest \u2014 today**\n1 email"):
            self.assertFalse(E._is_plain_empty(text), text)

    def test_same_reader_kind_never_counts_as_a_linked_duplicate(self):
        shown = (self.parse(mail_row(ME, ME, NOW, "Sec", "alerts@bank.test", "Your verification code", 1)),)
        other = [self.parse(mail_row("iCloud", "uuid-icloud", NOW + 60, "Sec", "alerts@bank.test",
                                     "Your verification code", 2))]
        excluded = frozenset({E._account_group(ME, ME)})
        self.assertEqual(len(E._drop_linked_duplicates(other, excluded, shown)), 1)

    def test_a_different_reader_copy_is_dropped_once_per_shown_row(self):
        shown = (self.parse(gmail_row(ME, NOW, "Sec", "alerts@bank.test", "Your verification code",
                                      "c1@bank.test", "g1")),)
        copies = [self.parse(mail_row("Google", "uuid-google", NOW + 5, "Sec", "alerts@bank.test",
                                      "Your verification code", i)) for i in (1, 2)]
        excluded = frozenset({E._account_group(ME, ME)})
        # One shown message accounts for one copy; the second is a different email.
        self.assertEqual(len(E._drop_linked_duplicates(copies, excluded, shown)), 1)
        # A copy of something the linked section did NOT show is never dropped.
        self.assertEqual(len(E._drop_linked_duplicates(copies, excluded, ())), 2)

    def test_unread_summary_keeps_the_unread_copy_when_the_linked_copy_is_read(self):
        self.link(ME, SCHOOL)
        E.cache_external_emails(account=ME, headers=gmail_row(
            ME, NOW - 10, "Paula", "paula@pay.test", "Your statement", "p1@pay.test", "g1", unread=False))
        E.cache_external_emails(account=SCHOOL, headers=gmail_row(
            SCHOOL, NOW - 30, "Prof Ada", "ada@school.test", "Exam reminder", "m1@school.test", "g3"))
        self.native(mail_row("Google", "uuid-google", NOW - 9, "Paula", "paula@pay.test",
                             "Your statement", 1, unread=True))
        out = asyncio.run(E.summarize_emails(day="today", unread=True))
        # The linked section hides its (read) copy, so the unread one must show somewhere.
        self.assertEqual(out.count("Your statement"), 1)
        self.assertIn("Your accounts", out)


if __name__ == "__main__":
    unittest.main()
