"""rc1 email fixes: a Gmail-only inbox reads as one inbox, and dedup is linear.

1. With only a linked Google account (no Mail.app inbox), an account-less
   summary used to open with an empty "## Your accounts" section before the
   real one. Now the first linked account is the user's own inbox: one linked
   account gets one unlabeled summary; several get one heading each.
2. _unique_records paired every history row against every recent row, so a
   long first sync made each summary quadratic. Pairing is now indexed; a
   reference copy of the old scan pins that the result is identical.
3. Ranged reads re-parsed the whole history string every time. The parse is
   cached per history string, and callers get copies they may mark.

All accounts and mail here are synthetic.
"""
from __future__ import annotations

import asyncio
import base64
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-gmail-only-")
os.environ["WISP_HOME"] = _scratch.name

from service import gmail_source as G  # noqa: E402
from service.tools import cache_store, email_tools as E  # noqa: E402

ME = "me@gmail.com"
SCHOOL = "me@school.edu"


def _msg(mid, subject, stamp=None):
    return {"id": mid, "internalDate": str(int((stamp or time.time()) * 1000)),
            "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "body": {
                "data": base64.urlsafe_b64encode(b"Synthetic body").decode()},
                "headers": [{"name": "From", "value": "Ann <ann@example.test>"},
                            {"name": "To", "value": ME}, {"name": "Subject", "value": subject},
                            {"name": "Message-ID", "value": f"<{mid}@example.test>"}]}}


def _header(account, subject):
    value = G.header_line(G.parse_metadata(_msg("cached-" + subject.replace(" ", "-"), subject)),
                          account, False)
    parts = value.split("\x01")
    parts[4] = account
    return "\x01".join(parts)


class GmailOnlySummary(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cache_store, "CACHE_DIR", Path(_scratch.name) / self._testMethodName))
        self.enterContext(patch.object(G, "CONFIG_PATH", Path(_scratch.name) / (self._testMethodName + ".json")))
        self.enterContext(patch.multiple(E, _headers="", _history="", _raw_emails="", _swift_headers="",
            _swift_history="", _swift_raw="", _external_accounts={}, _headers_sync_generation=0,
            _email_available=None, _email_read_source="", _email_reason="", _email_sync_pending=False,
            _native_headers_at=0.0, _native_raw_at=0.0, _native_source="", _swift_identity=[], _identity_emails=[]))
        self.enterContext(patch.multiple(G, _last={}, _errors={}, _access={}, _epochs={}, _locks={},
                                        _meta={}, _pending_flow=None, _flow_generation=0,
                                        _authorization={"state": "idle", "generation": 0, "error": ""}))
        self.enterContext(patch.object(E, "_native_catalog_inventory", lambda: []))
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

    def test_one_gmail_account_is_one_inbox(self):
        self.link(ME)
        E.cache_external_emails(account=ME, headers=_header(ME, "Gmail only note"))
        output = asyncio.run(E.summarize_emails(day="today"))
        self.assertIn("Gmail only note", output)
        self.assertNotIn("## ", output)
        self.assertNotIn("Your accounts", output)

    def test_several_gmail_accounts_get_one_heading_each_and_no_empty_section(self):
        self.link(ME, SCHOOL)
        E.cache_external_emails(account=ME, headers=_header(ME, "Personal note"))
        E.cache_external_emails(account=SCHOOL, headers=_header(SCHOOL, "School note"))
        output = asyncio.run(E.summarize_emails(day="today"))
        self.assertNotIn("Your accounts", output)
        first, second = output.split("## " + SCHOOL)
        self.assertIn("## " + ME, first)
        self.assertIn("Personal note", first)
        self.assertNotIn("School note", first)
        self.assertIn("School note", second)
        self.assertNotIn("Personal note", second)

    def test_native_inbox_keeps_the_your_accounts_section(self):
        self.link(ME)
        E.cache_emails(_header("Personal", "Native personal note").replace(
            "\x01Personal\x01Personal\x01", "\x01Personal\x01native-id\x01"))
        E.cache_external_emails(account=ME, headers=_header(ME, "Linked note"))
        output = asyncio.run(E.summarize_emails(day="today"))
        self.assertIn("## Your accounts", output)
        self.assertIn("## " + ME, output)


# ------------------------------------------------------------ dedup pairing

def _old_pairing(result, fingerprint, producer):
    """The pre-rc1 O(recent x history) scan, verbatim in behavior."""
    recent = [row for row in result if row.get("_snapshot") == "recent"]
    matched, paired = set(), []
    for row in result:
        if row.get("_snapshot") == "history":
            row_account = row.get("account_id", "")
            candidates = [(i, o) for i, o in enumerate(recent)
                          if i not in matched and o["account"].casefold() == row["account"].casefold()
                          and row_account and o.get("account_id") == row_account
                          and (producer(o) != producer(row) or not producer(o) or not producer(row))]
            partner = next((i for i, o in candidates if o.get("message_id") and row.get("message_id")
                            and o["message_id"].casefold() == row["message_id"].casefold()), None)
            if partner is None:
                partner = next((i for i, o in candidates if fingerprint(o) == fingerprint(row)
                                and not (o.get("message_id") and row.get("message_id")
                                         and o["message_id"].casefold() != row["message_id"].casefold())),
                               None)
            if partner is not None:
                matched.add(partner)
                recent[partner]["_cross_source_matched"] = True
                continue
            for o in recent:
                if (o["account"].casefold() == row["account"].casefold()
                        and o.get("account_id") != row_account
                        and ((o.get("message_id") and row.get("message_id")
                              and o["message_id"].casefold() == row["message_id"].casefold())
                             or (fingerprint(o) == fingerprint(row)
                                 and not (o.get("message_id") and row.get("message_id")
                                          and o["message_id"].casefold() != row["message_id"].casefold())))):
                    o["_cross_source_uncertain"] = True
                    row["_cross_source_uncertain"] = True
        paired.append(row)
    return paired


def _random_rows(rng, n):
    rows = []
    for _ in range(n):
        rows.append({
            "ts": float(rng.choice(range(1_700_000_000, 1_700_000_040))),
            "account": rng.choice(["Work", "work", "Home"]),
            "account_id": rng.choice(["", "acct-1", "acct-2"]),
            "sender": "Ann", "sender_address": rng.choice(["", "ann@example.test", "bo@example.test"]),
            "subject": rng.choice(["Hi", "Lunch", "Invoice"]),
            "message_id": rng.choice(["", "<a@x>", "<A@x>", "<b@x>"]),
            "native_id": rng.choice(["", "mail:1", "sqlite:1", "mail:2", "sqlite:9"]),
            "unread": rng.choice([True, False]),
            "_snapshot": rng.choice(["recent", "history"]),
        })
    return rows


def _flags(rows):
    return [(r["ts"], r["account"], r["account_id"], r["message_id"], r["native_id"], r["_snapshot"],
             r.get("_cross_source_matched", False), r.get("_cross_source_uncertain", False))
            for r in rows]


class Pairing(unittest.TestCase):
    def test_indexed_pairing_matches_the_old_scan(self):
        rng = random.Random(20261026)

        def fingerprint(row):
            return (row["ts"], row["account"].casefold(),
                    row["sender_address"] or row["sender"].casefold(), row["subject"])

        def producer(row):
            return row.get("native_id", "").split(":", 1)[0]

        for _ in range(300):
            rows = _random_rows(rng, rng.randint(0, 40))
            new = E._unique_records([dict(r) for r in rows])
            # Recompute the old answer from the same first-pass dedup.
            first_pass = _first_pass([dict(r) for r in rows])
            old = _old_pairing(first_pass, fingerprint, producer)
            self.assertEqual(_flags(new), _flags(old))

    def test_large_first_sync_stays_fast(self):
        recent = [{"ts": 1_700_000_000.0 + i, "account": "Work", "account_id": "acct-1",
                   "sender": "Ann", "sender_address": "ann@example.test", "subject": f"S{i}",
                   "message_id": f"<{i}@x>", "native_id": f"mail:{i}", "unread": False,
                   "_snapshot": "recent"} for i in range(4000)]
        history = [{**row, "native_id": f"sqlite:{row['ts']}", "account_id": "acct-1",
                    "message_id": f"<h{row['ts']}@x>", "_snapshot": "history"} for row in recent]
        started = time.monotonic()
        E._unique_records(recent + history)
        self.assertLess(time.monotonic() - started, 2.0)


def _first_pass(rows):
    """_unique_records up to (not including) cross-snapshot pairing."""
    seen, result = set(), []
    for row in sorted(rows, key=lambda r: (r.get("_snapshot") == "recent", r["ts"]), reverse=True):
        account = row["account_id"] or row["account"].casefold()
        identities = []
        if account and row.get("message_id"):
            identities.append(("message", E._account_group(row["account"], row["account_id"]),
                               row["message_id"].casefold()))
        if account and row.get("native_id"):
            identities.append(("native", account, row["native_id"].casefold()))
        if not identities:
            identities.append(row.get("_fallback_key", (
                "exact", row["ts"], row["account"], row["account_id"],
                row["sender"], row["sender_address"], row["subject"], row["unread"])))
        duplicate = any(key in seen for key in identities)
        seen.update(identities)
        if not duplicate:
            result.append(row)
    result.sort(key=lambda r: r["ts"], reverse=True)
    return result


class HistoryParseCache(unittest.TestCase):
    def test_parse_once_per_history_string_and_hand_out_copies(self):
        line = _header("Work", "Cached history note")
        calls = []
        real = E._parse_header_records

        def counting(text, **kwargs):
            calls.append(kwargs.get("snapshot"))
            return real(text, **kwargs)
        with patch.object(E, "_history", line), patch.object(E, "_HISTORY_PARSE", (None, [])), \
                patch.object(E, "_parse_header_records", counting):
            first = E._history_records()
            first[0]["_cross_source_matched"] = True
            second = E._history_records()
            self.assertEqual(calls, ["history"])
            self.assertNotIn("_cross_source_matched", second[0])
            self.assertEqual(second[0]["subject"], "Cached history note")
            E._history = line + "\n"            # any new history string misses the cache
            E._history_records()
            self.assertEqual(calls, ["history", "history"])


if __name__ == "__main__":
    unittest.main()
