"""Provider-backed email reliability. All accounts, HTTP and caches are synthetic."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-gmail-")
os.environ["WISP_HOME"] = _scratch.name

from service import gmail_source as G
from service.tools import cache_store, email_tools as E

ME = "me@gmail.com"
SCHOOL = "me@school.edu"


def msg(mid, subject="New email", unread=False, stamp=None):
    return {"id": mid, "internalDate": str(int((stamp or time.time()) * 1000)),
            "labelIds": ["INBOX"] + (["UNREAD"] if unread else []),
            "payload": {"mimeType": "text/plain", "body": {
                "data": base64.urlsafe_b64encode(b"Synthetic body").decode()},
                "headers": [{"name": "From", "value": "Ann <ann@example.test>"},
                            {"name": "To", "value": ME}, {"name": "Subject", "value": subject},
                            {"name": "Message-ID", "value": f"<{mid}@example.test>"}]}}


def header(account=ME, subject="Cached email", stamp=None, native_id=""):
    value = G.header_line(G.parse_metadata(msg("cached", subject, stamp=stamp)), account, False)
    parts = value.split("\x01")
    parts[4] = native_id or account
    return "\x01".join(parts)


class GmailSource(unittest.TestCase):
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
        self.accounts = {ME: {"client_id": "synthetic", "refresh_token": "fake-refresh"}}
        self.enterContext(patch.object(G, "connected_accounts", lambda cfg=None: dict(self.accounts)))
        E.configure_external_accounts(self.accounts)

    def publish(self, account=ME, **parts):
        E.cache_external_emails(account=account, **parts)

    def test_native_mail_memory_witness_uses_exact_live_snapshot_and_never_calls_getters(self):
        from service.publication_authority import PublicationBusy,publication
        raw = "\x01".join([str(time.time()),"U","Native","native-id","Sender","me@example.test",
                              "Subject","<synthetic>","A complete synthetic native body."])
        E.set_email_availability(True)
        E.cache_raw_emails(raw,{"accounts":["Native"],"failed_accounts":[],"complete":True})
        snapshot = E.raw_email_snapshot()
        self.assertIsNotNone(snapshot)
        witness = E.prepare_publication_witness(snapshot)
        self.assertTrue(witness.current())
        self.assertIsNone(E.prepare_publication_witness(dataclasses.replace(snapshot)))
        with publication(witness):
            with self.assertRaises(PublicationBusy):
                E.set_email_availability(False)
            with self.assertRaises(PublicationBusy):
                E.cache_raw_emails(raw,{"accounts":["Native"],"failed_accounts":[],"complete":True})
            with patch.object(E,"raw_email_snapshot",side_effect=AssertionError("Getter during publication")), \
                 patch.object(E.cache_store,"load",side_effect=AssertionError("Cache I/O during publication")):
                self.assertTrue(witness.current())
        E.set_email_availability(False)
        self.assertFalse(witness.current())
        self.assertIsNone(E.prepare_publication_witness(snapshot))
        E.set_email_availability(True)
        self.assertFalse(witness.current())  # Re-enabling never rearms the old availability stamp.
        self.assertTrue(E.prepare_publication_witness(snapshot).current())
        E.cache_raw_emails("",{"accounts":["Native"],"failed_accounts":[],"complete":True})
        self.assertIsNone(E.prepare_publication_witness(snapshot))

    def test_native_mail_memory_witness_does_not_renew_original_receipt(self):
        raw = "\x01".join([str(time.time()),"U","Native","native-id","Sender","me@example.test",
                              "Subject","<synthetic>","A complete synthetic native body."])
        E.set_email_availability(True)
        E.cache_raw_emails(raw,{"accounts":["Native"],"failed_accounts":[],"complete":True})
        snapshot = E.raw_email_snapshot()
        witness = E.prepare_publication_witness(snapshot)
        with patch.object(E.time,"time",return_value=snapshot.synced_at+1199):
            self.assertTrue(witness.current())
            self.assertTrue(E.prepare_publication_witness(snapshot).current())
        with patch.object(E.time,"time",return_value=snapshot.synced_at+1200):
            self.assertFalse(witness.current())
            self.assertIsNone(E.prepare_publication_witness(snapshot))

    def history_provider(self):
        import weakref
        from service import gmail_behavior_history as H
        self.enterContext(patch.object(G, "_behavior_history_sources", weakref.WeakSet()))
        scope = H.HistoryScope(100, 190, "inbox", 100)
        admission = H.Admission(H.AccountIdentity("gmail", "configured-opaque-id", "connection-1", "set-1"),
                                1, 1, True, True, True, scope.digest)
        provider = G.new_behavior_history(admission=lambda: admission,
            limits=H.Limits(80000, 8000000, 32000000, 100), clock=lambda: 200, monotonic=lambda: 0)
        self.addCleanup(provider.invalidate)
        return provider, scope

    def test_provider_history_fetches_beyond_recent_fifty_without_native_complete_promotion(self):
        from tests.test_gmail_behavior_history import FakeHTTP, message, token
        provider, scope = self.history_provider()
        http = FakeHTTP(pages=[{"messages": [{"id": str(i)} for i in range(55)]}],
                        messages={str(i): message(str(i)) for i in range(55)})
        provider.control("start", scope=scope)
        async def run():
            from service.gmail_behavior_history import StepBudget
            for _ in range(11):
                await provider.step(http=http, token=token, budget=StepBudget(100, 5, 6, 400000, 30))
        with patch.object(E, "_raw_mail_snapshot", None), patch.object(E, "_email_available", True):
            asyncio.run(run())
            snapshot = E.provider_history_snapshot(provider)
            self.assertEqual(len(snapshot.records), 55)
            self.assertIsNone(E.raw_email_snapshot())
            calls = len(http.calls)
            same = E.provider_history_snapshot(provider, expected_receipt=snapshot.receipt,
                                                required_refs=(snapshot.records[0].ref,))
            self.assertEqual(same.records[0], snapshot.records[0])
            self.assertEqual(len(http.calls), calls)
        self.assertEqual(G.RAW_LIMIT, 50)
        self.assertEqual(len(http.calls), 56)
        self.assertEqual((E._raw_emails, E._swift_raw), ("", ""))

    def test_provider_disconnect_withdraws_history_before_revocation_await(self):
        from tests.test_gmail_behavior_history import FakeHTTP, token
        from service.gmail_behavior_history import StepBudget
        provider, scope = self.history_provider()
        provider.control("start", scope=scope)
        asyncio.run(provider.step(http=FakeHTTP(), token=token, budget=StepBudget(100, 5, 6, 400000, 30)))
        receipt = provider.snapshot().receipt
        async def revoke(*args, **kwargs):
            self.assertIsNone(E.provider_history_snapshot(provider, expected_receipt=receipt))
            self.assertEqual(provider.status()["state"], "disconnected")
        with patch.object(G.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=revoke)
            asyncio.run(G.disconnect(ME))
            client.return_value.__aenter__.return_value.post.assert_awaited_once()
        self.assertEqual(provider.status()["state"], "disconnected")
        self.assertIsNone(provider.snapshot(expected_receipt=receipt))

    def test_history_forget_synchronously_withdraws_without_affecting_native_fallback(self):
        from tests.test_gmail_behavior_history import FakeHTTP, token
        from service.gmail_behavior_history import StepBudget
        provider, scope = self.history_provider()
        native = header("iCloud", "Native fallback preserved", native_id="native-cloud")
        E.cache_emails(native)
        provider.control("start", scope=scope)
        asyncio.run(provider.step(http=FakeHTTP(), token=token, budget=StepBudget(100, 5, 6, 400000, 30)))
        G.invalidate_behavior_history("forgotten")
        self.assertIsNone(E.provider_history_snapshot(provider))
        self.assertIn("Native fallback preserved", E._headers)
        self.assertEqual(E._swift_headers, native)

    def test_history_factory_and_snapshot_do_not_activate_or_require_gmail_presence(self):
        from service.gmail_behavior_history import Limits
        self.accounts.clear()
        provider = G.new_behavior_history(admission=lambda: None, limits=Limits(100, 100, 100, 100))
        self.addCleanup(provider.invalidate)
        self.assertFalse(G.is_connected())
        self.assertEqual(provider.status()["state"], "off")
        self.assertIsNone(E.provider_history_snapshot(provider))
        self.assertIsNone(E.provider_history_snapshot({"email_consent": True}))

    def test_header_roundtrip_and_plain_body(self):
        self.publish(headers=header(subject="Hi | there"))
        self.assertEqual(E._parse_header_records(E._headers)[0]["subject"], "Hi | there")
        self.assertEqual(G.extract_body(msg("a")["payload"]), "Synthetic body")

    def test_header_only_tool_gets_new_arrival_with_mail_closed(self):
        E.set_email_availability(False, reason="Mail is closed", read_source="local_index")
        calls = []
        async def get(http, path, params=None):
            calls.append((path, params))
            if path == "messages":
                return {"messages": [] if ("labelIds", "UNREAD") in params else [{"id": "new"}]}
            return msg("new", "Arrived while Mail was closed")
        async def run():
            with patch.object(G, "_get", get), patch("service.assistant.hub.hub.publish", AsyncMock()) as native:
                output = await E.summarize_emails(day="today")
                native.assert_not_awaited()
                return output
        output = asyncio.run(run())
        self.assertIn("Arrived while Mail was closed", output)
        self.assertNotIn("stale", output)
        self.assertFalse(any(("format", "full") in (params or []) for _, params in calls))
        self.assertTrue(E._external_fresh())

    def test_stale_direct_tool_refreshes_again(self):
        self.publish(headers=header(subject="Old"))
        G._last[ME] = {"headers": time.time() - 120}
        async def refresh(http, account):
            self.publish(headers=header(subject="New arrival"))
            G._last[ME]["headers"] = time.time()
        with patch.object(G, "_sync_headers", refresh):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("New arrival", output)
        self.assertNotIn("“Old”", output)

    def test_refresh_failure_preserves_snapshot_and_reports_its_age(self):
        self.publish(headers=header(subject="Retained cached message"))
        stamp = E._external_accounts[ME]["headers_at"]
        async def fail(*args):
            raise G.GmailError("Sign-in expired; reconnect")
        with patch.object(G, "_sync_headers", fail):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Retained cached message", output)
        self.assertIn("stale or unverified", output)
        self.assertIn("last successful refresh", output)
        self.assertIn("reconnect", output)
        self.assertEqual(E._external_accounts[ME]["headers_at"], stamp)

    def test_timeout_marks_cached_snapshot_stale(self):
        self.publish(headers=header())
        async def slow(*args):
            await asyncio.sleep(10)
        with patch.object(G, "_sync_headers", slow):
            asyncio.run(G.refresh_if_stale(timeout=0.01))
        self.assertIn("timed out", E.email_freshness_warning())
        self.assertIn("Cached email", E._headers)

    def test_fresh_empty_provider_is_distinct_from_stale_empty(self):
        self.publish(headers="")
        E._headers_sync_generation = 1
        fresh = asyncio.run(E.summarize_inbox_for_day("today"))
        self.assertIn("No emails", fresh)
        self.assertNotIn("couldn’t verify", fresh)
        E.external_email_error(ME, "headers", "Network unavailable")
        stale = asyncio.run(E.summarize_inbox_for_day("today"))
        self.assertIn("couldn’t verify", stale)
        self.assertIn("does not mean your inbox is empty", stale)

    def test_generic_google_label_does_not_shadow_another_account(self):
        E.cache_emails(header("Google", "Other mailbox", native_id="other-native-id"))
        self.publish(headers=header())
        self.assertIn("Other mailbox", E._headers)
        self.assertIn("Google", E.email_freshness_warning())

    def test_explicit_native_binding_deduplicates_rows_and_markers(self):
        self.accounts[ME]["mail_account_id"] = "my-native-id"
        E.configure_external_accounts(self.accounts)
        E.cache_emails(header("Personal", "Old native version", native_id="my-native-id") +
                      "\nC3\x01Personal\x01my-native-id\x01interrupted")
        self.publish(headers=header(subject="Fresh provider version"))
        self.assertNotIn("Old native", E._headers)
        self.assertNotIn("C3", E._headers)
        self.assertFalse(E.email_freshness_warning())
        E.cache_raw_emails("")  # Raw recomposition must not affect header freshness.
        self.assertFalse(E.email_freshness_warning())

    def test_two_connected_accounts_have_independent_freshness(self):
        self.accounts[SCHOOL] = {"client_id": "other", "refresh_token": "other-fake"}
        E.configure_external_accounts(self.accounts)
        self.publish(headers=header(subject="Personal fresh"))
        self.publish(SCHOOL, headers=header(SCHOOL, "School cached"))
        E.external_email_error(SCHOOL, "headers", "School offline")
        warning = E.email_freshness_warning()
        self.assertIn(SCHOOL, warning)
        self.assertNotIn(ME + ":", warning)
        self.assertEqual(len(E._parse_header_records(E._headers)), 2)
        self.assertFalse(E.email_freshness_warning(ME))

    def test_accountless_summary_is_labeled_per_linked_account(self):
        # "What's on my email" with a guest Google account linked: one section
        # per account, and neither inbox leaks into the other's section.
        guest = "guest@gmail.com"
        self.accounts = {guest: {"client_id": "synthetic", "refresh_token": "fake-guest"}}
        E.configure_external_accounts(self.accounts)
        E.cache_emails(header("Personal", "Native personal note", native_id="native-id"))
        self.publish(guest, headers=header(guest, "Guest inbox note"))
        G._last[guest] = {"headers": time.time()}
        async def no_refresh(http, account):
            G._last[account]["headers"] = time.time()
        with patch.object(G, "_sync_headers", no_refresh):
            output = asyncio.run(E.summarize_emails(day="today"))
        mine, theirs = output.split("## " + guest)
        self.assertIn("## Your accounts", mine)
        self.assertIn("Native personal note", mine)
        self.assertNotIn("Guest inbox note", mine)
        self.assertIn("Guest inbox note", theirs)
        self.assertNotIn("Native personal note", theirs)
        # A named account still answers for that account alone.
        named = asyncio.run(E.summarize_emails(day="today", account=guest))
        self.assertNotIn("## ", named)
        self.assertIn("Guest inbox note", named)

    def test_raw_failure_is_not_hidden_by_fresh_headers(self):
        self.publish(headers=header(), raw=G.raw_record(msg("raw"), ME))
        E.external_email_error(ME, "raw", "Body fetch failed")
        self.assertFalse(E.email_freshness_warning())
        self.assertIn("Body fetch failed", E.email_freshness_warning(part="raw"))

    def test_restart_preserves_data_age_but_requires_server_verification(self):
        self.publish(headers=header(subject="Saved snapshot"))
        stamp = E._external_accounts[ME]["headers_at"]
        E._external_accounts = {}
        E._restore_email_sources()
        E.configure_external_accounts(self.accounts)
        self.assertIn("Saved snapshot", E._headers)
        self.assertEqual(E._external_accounts[ME]["headers_at"], stamp)
        self.assertIn("stale or unverified", E.email_freshness_warning())

    def test_source_configuration_does_not_erase_native_fallback_on_failure(self):
        E.cache_emails(header(ME, "Native fallback"))
        E.configure_external_accounts(self.accounts)
        self.assertIn("Native fallback", E._headers)
        E.external_email_error(ME, "headers", "offline")
        self.assertIn("Native fallback", E._headers)

    def test_recompose_decides_provider_once_per_account_not_per_record(self):
        # A failed provider makes _provider_selected scan the whole native
        # snapshot; asking per record froze the backend on a large Mail history.
        self.accounts[SCHOOL] = {"client_id": "other", "refresh_token": "other"}
        E.configure_external_accounts(self.accounts)
        self.publish(headers=header(subject="Provider copy"))
        E.cache_emails("\n".join(header(ME, f"Native {i}", stamp=time.time() - i) for i in range(400)))
        E.external_email_error(ME, "headers", "offline")
        calls = []
        real = E._provider_selected
        with patch.object(E, "_provider_selected",
                          lambda *args: calls.append(args) or real(*args)):
            E.configure_external_accounts(self.accounts)
        self.assertLessEqual(len(calls), 3 * len(self.accounts))
        self.assertIn("Provider copy", E._headers)
        # The freshness disclosure on every email tool call has the same shape.
        calls.clear()
        with patch.object(E, "_provider_selected",
                          lambda *args: calls.append(args) or real(*args)):
            E.email_source_details()
        self.assertLessEqual(len(calls), len(self.accounts))

    def test_range_history_checks_each_account_once_not_per_row(self):
        rows = []
        for i in range(300):
            fields = header(ME, f"Native {i}", stamp=time.time() - i).split("\x01")
            fields[7], fields[-1] = f"<n{i}@example.test>", f"n{i}"
            rows.append("\x01".join(fields))
        E._history = "\n".join(rows)
        self.assertEqual(len(E._parse_header_records(E._history, snapshot="history")), 300)
        calls = []
        real = E.external_recent_complete
        with patch.object(E, "external_recent_complete",
                          lambda address: calls.append(address) or real(address)):
            E._range_history(0, time.time() + 1)
        self.assertLessEqual(len(calls), len(self.accounts))

    def test_metadata_is_namespaced_by_account(self):
        self.accounts[SCHOOL] = {"client_id": "other", "refresh_token": "other"}
        E.configure_external_accounts(self.accounts)
        async def get(http, path, params=None):
            if path == "messages":
                return {"messages": [] if ("labelIds", "UNREAD") in params else [{"id": "same-id"}]}
            return msg("same-id", G._current()[0])
        with patch.object(G, "_get", get):
            asyncio.run(G.sync())
        self.assertEqual(set(r["subject"] for r in E._parse_header_records(E._headers)), {ME, SCHOOL})

    def test_disconnect_during_fetch_cannot_republish(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            async def get(http, path, params=None):
                if path == "messages":
                    started.set()
                    await release.wait()
                    return {"messages": [{"id": "late"}]}
                return msg("late", "Must not be published")
            with patch.object(G, "_get", get), patch.object(httpx.AsyncClient, "post", AsyncMock()):
                pending = asyncio.create_task(G.sync())
                await started.wait()
                # Production disconnect reads the config through connected_accounts.
                self.accounts.clear()
                await G.disconnect()
                release.set()
                await pending
        asyncio.run(run())
        self.assertNotIn("Must not", E._headers)
        self.assertFalse(E._external_accounts)

    def test_config_has_private_permissions_even_for_existing_tmp(self):
        tmp = G.CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text("old")
        tmp.chmod(0o644)
        G._save_config({"client_id": "synthetic", "email": ME, "refresh_token": "fake"})
        self.assertEqual(G.CONFIG_PATH.stat().st_mode & 0o777, 0o600)
        actual = G.load_config()
        # Legacy single-account config remains recognized.
        with patch.object(G, "connected_accounts", GmailSource._original_accounts):
            self.assertIn(ME, G.connected_accounts(actual))

    def test_revoked_consent_preserves_rows_and_requires_reconnect(self):
        self.publish(headers=header(subject="Before revocation"))
        async def post(*args, **kwargs):
            return httpx.Response(400, json={"error": "invalid_grant"})
        with patch.object(httpx.AsyncClient, "post", post):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Before revocation", output)
        self.assertIn("reconnect", output)
        self.assertIn("stale or unverified", output)
        self.assertNotIn("fake-refresh", output)

    def test_real_disconnect_invalidates_inflight_publication(self):
        G._save_config({"client_id": "synthetic", "accounts": self.accounts})
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            async def get(http, path, params=None):
                if path == "messages":
                    started.set()
                    await release.wait()
                    return {"messages": [{"id": "late"}]}
                return msg("late", "Disconnected content")
            with patch.object(G, "connected_accounts", GmailSource._original_accounts), \
                 patch.object(G, "_get", get), patch.object(httpx.AsyncClient, "post", AsyncMock()):
                pending = asyncio.create_task(G.sync())
                await started.wait()
                await G.disconnect(ME)
                release.set()
                self.assertFalse(await pending)
                self.assertFalse(G.connected_accounts())
                self.assertNotIn(ME, E.get_identity_emails())
        asyncio.run(run())
        self.assertNotIn("Disconnected content", E._headers)
        self.assertNotIn("Disconnected content", cache_store.load("email_headers"))
        self.assertEqual(self.restarted_cache()["headers"], "")
        self.assertEqual(G._epochs[ME], 1)

    def test_status_never_exposes_credentials_and_marks_restored_data_unverified(self):
        self.publish(headers=header())
        E._external_accounts[ME]["headers_verified"] = False
        info = G.status()
        self.assertFalse(info["accounts"][0]["fresh"])
        self.assertIsNotNone(info["accounts"][0]["last_headers_sync"])
        self.assertNotIn("fake-refresh", json.dumps(info))

    def test_successful_empty_native_cache_is_preserved_on_restart(self):
        E.cache_emails("")
        self.assertEqual(E._restored_native("headers", header(subject="Obsolete")), "")

    def restarted_cache(self, accounts=None):
        code = """
import importlib, json, sys
from pathlib import Path
from service.tools import cache_store
cache_store.CACHE_DIR = Path(sys.argv[1])
from service.tools import email_tools as E
E = importlib.reload(E)
E.configure_external_accounts(json.loads(sys.argv[2]))
print(json.dumps({'headers': E._headers, 'history': E._history, 'raw': E._raw_emails,
                  'native': E._swift_headers, 'native_raw_at': E._native_raw_at,
                  'native_identity': E._swift_identity, 'identity': E.get_identity_emails()}))
"""
        result = subprocess.run([sys.executable, "-c", code, str(cache_store.CACHE_DIR),
                                 json.dumps(accounts or {})], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_provider_disconnect_cannot_resurrect_as_native_after_restart(self):
        self.publish(headers=header(subject="Disconnected private header"),
                     history=header(subject="Disconnected history"), raw=G.raw_record(msg("private"), ME))
        for part in ("headers", "history", "raw"):
            self.assertTrue(cache_store._path("email_mail_" + part).exists())
            self.assertEqual(cache_store.load("email_mail_" + part), "")
        E.configure_external_accounts({})
        self.assertEqual(self.restarted_cache(), {
            "headers": "", "history": "", "raw": "", "native": "", "native_raw_at": 0.0,
            "native_identity": [], "identity": []})
        self.assertNotIn("Disconnected", cache_store.load("email_headers"))
        self.assertNotIn("Synthetic body", cache_store.load("email_raw"))

    def test_disconnect_one_account_retains_other_and_genuine_native_after_restart(self):
        self.accounts[SCHOOL] = {"client_id": "other", "refresh_token": "other"}
        E.configure_external_accounts(self.accounts)
        E.cache_emails(header("Native", "Genuine native", native_id="native-independent"))
        self.publish(headers=header(subject="Removed personal"))
        self.publish(SCHOOL, headers=header(SCHOOL, "Retained school"))
        E.configure_external_accounts({SCHOOL: self.accounts[SCHOOL]})
        restored = self.restarted_cache({SCHOOL: self.accounts[SCHOOL]})
        self.assertNotIn("Removed personal", restored["headers"])
        self.assertIn("Retained school", restored["headers"])
        self.assertIn("Genuine native", restored["native"])
        self.assertNotIn("Retained school", restored["native"])

    def test_missing_native_backing_with_recorded_sources_is_unknown_not_display(self):
        self.publish(headers=header(subject="Provider display"))
        cache_store._path("email_mail_headers").unlink()
        self.assertEqual(E._restored_native("headers", cache_store.load("email_headers")), "")
        E.configure_external_accounts({})
        self.assertEqual(self.restarted_cache()["headers"], "")

    def test_legacy_mail_cache_migrates_with_original_body_age(self):
        cache_store._path("email_sources").unlink()
        for part in ("headers", "history", "raw"):
            cache_store._path("email_mail_" + part).unlink()
        legacy = header("Native", "Legacy Mail snapshot", native_id="native-legacy")
        cache_store.save("email_headers", legacy)
        cache_store.save("email_raw", G.raw_record(msg("legacy"), "Native"))
        original_age = time.time() - 6 * 86400
        os.utime(cache_store._path("email_raw"), (original_age, original_age))
        restored = self.restarted_cache()
        self.assertIn("Legacy Mail snapshot", restored["native"])
        self.assertAlmostEqual(restored["native_raw_at"], original_age, places=4)

    def test_failed_source_metadata_write_does_not_renew_or_publish_display(self):
        original = cache_store.save
        def fail_sources(name, value):
            if name != "email_sources":
                original(name, value)
        with patch.object(cache_store, "save", fail_sources):
            self.publish(headers=header(subject="Memory only metadata failure"))
        self.assertIn("Memory only metadata failure", E._headers)
        self.assertNotIn("Memory only metadata failure", cache_store.load("email_headers"))

    def test_failed_native_persistence_does_not_publish_unlabelled_provider_display(self):
        cache_store._path("email_mail_headers").unlink()
        original = cache_store.save
        def fail_native(name, value):
            if name != "email_mail_headers":
                original(name, value)
        with patch.object(cache_store, "save", fail_native):
            self.publish(headers=header(subject="Memory only provider"))
        self.assertIn("Memory only provider", E._headers)
        self.assertNotIn("Memory only provider", cache_store.load("email_headers"))

    def test_native_callbacks_cannot_bypass_failed_provider_provenance_barrier(self):
        for part in ("headers", "history", "raw"):
            cache_store._path("email_mail_" + part).unlink()
        cache_store._path("email_sources").unlink()
        original = cache_store.save
        def fail_provenance(name, value):
            if not name.startswith("email_mail_") and name != "email_sources":
                original(name, value)
        with patch.object(cache_store, "save", fail_provenance):
            self.publish(headers=header(subject="Provider secret header"),
                         history=header(subject="Provider secret history"),
                         raw=G.raw_record(msg("secret", "Provider secret body"), ME))
            E.cache_emails(header("Native", "Native header"))
            E.cache_history_headers(header("Native", "Native history"))
            E.cache_raw_emails(G.raw_record(msg("native", "Native body"), "Native"))
            # Exercise an expired native body while a provider body survives.
            E._native_raw_at = time.time() - 8 * 86400
            E._purge_raw_if_expired()
        for part in ("headers", "history", "raw"):
            self.assertNotIn("Provider secret", cache_store.load("email_" + part))
        restored = self.restarted_cache()
        for part in ("headers", "history", "raw", "native"):
            self.assertNotIn("Provider secret", restored[part])

    def test_fresh_identity_write_failure_cannot_promote_provider_after_cold_restart(self):
        for name in ("email_mail_identity", "email_sources", "identity_emails"):
            cache_store._path(name).unlink(missing_ok=True)
        original = cache_store.save
        def fail_ownership(name, value):
            if name not in {"email_mail_identity", "email_sources"}:
                original(name, value)
        with patch.object(cache_store, "save", fail_ownership):
            E.configure_external_accounts(self.accounts)
            self.assertIn(ME, E.get_identity_emails())
            E.cache_identity([])
        self.assertNotIn(ME, cache_store.load("identity_emails"))
        restored = self.restarted_cache()
        self.assertEqual(restored["native_identity"], [])
        self.assertEqual(restored["identity"], [])
        self.assertEqual(cache_store.load("email_mail_identity"), "")
        self.assertEqual(cache_store.load("identity_emails"), "")

    def test_identity_disconnect_preserves_genuine_native_and_other_provider(self):
        native = "synthetic.native@icloud.com"
        self.accounts[SCHOOL] = {"client_id": "other", "refresh_token": "other"}
        E.configure_external_accounts(self.accounts)
        E.cache_identity([native])
        E.configure_external_accounts({SCHOOL: self.accounts[SCHOOL]})
        restored = self.restarted_cache({SCHOOL: self.accounts[SCHOOL]})
        self.assertEqual(restored["native_identity"], [native])
        self.assertEqual(restored["identity"], [native, SCHOOL])
        self.assertNotIn(ME, cache_store.load("identity_emails"))

    def test_load_save_only_adapter_import_does_not_assume_native_provenance(self):
        code = """
import importlib, json, sys, types
from service.tools import email_tools as E
import service.tools
adapter = types.ModuleType('service.tools.cache_store')
values = {'email_headers': '1700000000 | R | provider@gmail.com | Sender | Unknown provenance',
          'email_history': '', 'email_raw': '', 'identity_emails': 'provider@gmail.com'}
def load(name):
    if name not in values:
        raise RuntimeError('Private key unavailable in staged adapter')
    return values[name]
adapter.load = load
def save(*args):
    raise AssertionError('Staged cache is read only')
adapter.save = save
sys.modules[adapter.__name__] = adapter
service.tools.cache_store = adapter
E = importlib.reload(E)
assert E._swift_headers == '' and E._swift_identity == [] and E._external_accounts == {}
assert E.email_sync_state() == 'syncing'
print('safe staged import')
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertIn("safe staged import", result.stdout)

    def test_registered_cold_unknown_body_is_rejected_in_staged_and_filesystem_cache(self):
        code = r'''
import asyncio, importlib, json, sys, types
from pathlib import Path
from unittest.mock import AsyncMock, patch
from service.tools import email_tools as E, cache_store
from service import gmail_source as G
from service.tools.registry import REGISTRY, run_tool
import service.tools
body = 'UNKNOWN_ORIGIN_BODY_MUST_NOT_BE_DISPLAYED'
values = {'email_headers': '', 'email_history': '',
          'email_raw': '\x01'.join(['1700000000', 'U', 'iCloud', 'native-id',
                        'Fixture <fixture@example.test>', 'me@icloud.com',
                        'Unknown body', '<unknown@example.test>', body]),
          'identity_emails': 'old-provider@example.test'}
loads = []
if sys.argv[1] == 'staged':
    adapter = types.ModuleType('service.tools.cache_store')
    def load(name):
        loads.append(name)
        assert name in values, 'Private adapter read: ' + name
        return values[name]
    def save(*args):
        raise AssertionError('Read-only adapter attempted persistence')
    adapter.load, adapter.save = load, save
    sys.modules[adapter.__name__] = adapter
    service.tools.cache_store = adapter
else:
    cache_store.CACHE_DIR = Path(sys.argv[2])
    for name, value in values.items():
        cache_store.save(name, value)
    cache_store.save('email_sources', '{}')
    assert not cache_store._path('email_mail_raw').exists()
E = importlib.reload(E)
assert E._swift_raw == '' and E._native_raw_at == 0
assert body in E._raw_emails
original_ensure = E._ensure_email_cache
async def ensure(*, want_raw=False):
    await original_ensure(want_raw=want_raw, timeout_seconds=0)
with patch.object(G, 'connected_accounts', return_value={}), \
     patch.object(G, 'refresh_if_stale', AsyncMock()), \
     patch('service.assistant.hub.hub.publish', AsyncMock()) as publish, \
     patch.object(E, '_ensure_email_cache', ensure):
    output = asyncio.run(run_tool(REGISTRY['view_emails'], {'query': 'Unknown body'}))
assert body not in output and E._raw_emails == ''
assert 'No raw email content cached' in output
publish.assert_awaited_once_with({'type': 'sync_emails_now'})
if sys.argv[1] == 'staged':
    assert set(loads) == set(values), loads
else:
    assert cache_store.load('email_raw') == ''
print('unknown body rejected')
'''
        for mode in ("staged", "filesystem"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="cold-body-") as folder:
                result = subprocess.run([sys.executable, "-c", code, mode, folder],
                                        check=True, capture_output=True, text=True)
                self.assertIn("unknown body rejected", result.stdout)

    def test_raw_admission_uses_each_source_receipt_without_renewal(self):
        for expired in ("native", "provider"):
            with self.subTest(expired=expired):
                E.cache_raw_emails(G.raw_record(msg("native", "Native retained body"), "iCloud"))
                self.publish(raw=G.raw_record(msg("provider", "Provider retained body"), ME))
                original = time.time() - 3600
                E._native_raw_at = original
                E._external_accounts[ME]["raw_at"] = original
                if expired == "native":
                    E._native_raw_at -= 8 * 86400
                else:
                    E._external_accounts[ME]["raw_at"] -= 8 * 86400
                E._raw_emails_at = time.time() - 20 * 86400
                E._purge_raw_if_expired()
                self.assertNotIn("Native retained body" if expired == "native" else "Provider retained body", E._raw_emails)
                self.assertIn("Provider retained body" if expired == "native" else "Native retained body", E._raw_emails)
                self.assertEqual(E._raw_emails_at, original)

    def test_modern_native_body_without_valid_receipt_cannot_inherit_display_age(self):
        for invalid in (0, time.time() + 3600, "invalid", float("nan")):
            with self.subTest(receipt=invalid):
                E.cache_raw_emails(G.raw_record(msg("native", "Unknown receipt body"), "iCloud"))
                self.publish(raw=G.raw_record(msg("provider", "Unknown provider receipt body"), ME))
                E._native_raw_at = invalid
                E._external_accounts[ME]["raw_at"] = invalid
                E._raw_emails_at = time.time()
                self.assertFalse(E._raw_ready())
                self.assertEqual(E._raw_emails, "")
                self.assertEqual(E._native_raw_at, 0)
                self.assertIsNone(E._external_accounts[ME]["raw"])

    def test_registered_body_lookup_rejects_corrupt_provider_age_before_composition(self):
        from service.tools.registry import REGISTRY, run_tool
        for invalid in ("broken-receipt", {}, float("nan"), float("inf"), 0,
                        -1, time.time() + 3600, time.time() - 8 * 86400):
            with self.subTest(receipt=invalid):
                E.cache_raw_emails(G.raw_record(msg("native", "Valid native fallback"), "iCloud"))
                self.publish(raw=G.raw_record(msg("provider", "Rejected provider body"), ME))
                original = E._native_raw_at = time.time() - 3600
                E._external_accounts[ME]["raw_at"] = invalid
                E._raw_emails_at = time.time()
                E.set_email_availability(False, reason="Synthetic native refresh failed", read_source="local_index")
                ensure = E._ensure_email_cache
                async def bounded(*, want_raw=False):
                    await ensure(want_raw=want_raw, timeout_seconds=0)
                with patch.object(E, "_ensure_email_cache", bounded), \
                     patch.object(G, "refresh_if_stale", AsyncMock()), \
                     patch("service.assistant.hub.hub.publish", AsyncMock()):
                    output = asyncio.run(run_tool(REGISTRY["view_emails"], {"count": 10}))
                self.assertIn("Valid native fallback", output)
                self.assertNotIn("Rejected provider body", output)
                self.assertNotIn("error running", output)
                self.assertEqual(E._native_raw_at, original)
                self.assertEqual(E._raw_emails_at, original)

    def test_corrupt_persisted_receipt_retains_native_body_in_fresh_process(self):
        E.cache_emails(header("iCloud", "Native header", native_id="native-cloud"))
        E.cache_raw_emails(G.raw_record(msg("native", "Persisted native fallback"), "iCloud"))
        self.publish(raw=G.raw_record(msg("provider", "Corrupt provider body"), ME))
        saved = json.loads(cache_store.load("email_sources"))
        original = time.time() - 3600
        saved["native_raw_at"] = original
        for part in ("headers", "history", "raw"):
            saved["accounts"][ME][part + "_at"] = "broken-receipt"
        cache_store.save("email_sources", json.dumps(saved))
        code = r'''
import asyncio, importlib, json, sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from service.tools import cache_store
cache_store.CACHE_DIR = Path(sys.argv[1])
from service.tools import email_tools as E
from service import gmail_source as G
from service.tools.registry import REGISTRY, run_tool
E = importlib.reload(E)
account = sys.argv[2]
assert E._external_accounts[account]['raw_at'] == 0
E.set_email_availability(False, reason='Synthetic native refresh failed', read_source='local_index')
ensure = E._ensure_email_cache
async def bounded(*, want_raw=False):
    await ensure(want_raw=want_raw, timeout_seconds=0)
with patch.object(G, 'connected_accounts', return_value={account: {'client_id': 'synthetic', 'refresh_token': 'fake'}}), \
     patch.object(G, 'refresh_if_stale', AsyncMock()), \
     patch('service.assistant.hub.hub.publish', AsyncMock()), \
     patch.object(E, '_ensure_email_cache', bounded):
    output = asyncio.run(run_tool(REGISTRY['view_emails'], {'count': 10}))
assert 'Persisted native fallback' in output and 'Corrupt provider body' not in output, output
assert 'error running' not in output, output
assert E._native_raw_at == float(sys.argv[3]) and E._raw_emails_at == float(sys.argv[3])
assert E._external_accounts[account]['raw'] is None
assert E.email_source_details(part='raw')[0]['last_success'] is None
assert 'Corrupt provider body' not in cache_store.load('email_raw')
print('fresh process retained original native fallback')
'''
        result = subprocess.run([sys.executable, "-c", code, str(cache_store.CACHE_DIR), ME, str(original)],
                                check=True, capture_output=True, text=True)
        self.assertIn("fresh process retained original native fallback", result.stdout)

    def test_invalid_header_receipt_is_unverified_without_reading_bodies(self):
        self.publish(headers=header(subject="Unverified header fallback"))
        for part in ("headers", "history", "raw"):
            E._external_accounts[ME][part + "_at"] = "broken-receipt"
        with patch.object(G, "refresh_if_stale", AsyncMock()), \
             patch.object(E, "_parse_raw", side_effect=AssertionError("Digest must not read bodies")), \
             patch("service.assistant.hub.hub.publish", AsyncMock()):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Unverified header fallback", output)
        self.assertIn("stale or unverified", output)
        self.assertFalse(E._external_fresh())
        self.assertFalse(E.external_recent_complete(ME))
        self.assertIsNone(E.email_source_details()[0]["last_success"])

    def test_registered_dated_unread_uses_current_identity_before_date_filter(self):
        self.accounts[ME]["mail_account_id"] = "native-A"
        E.configure_external_accounts(self.accounts)
        start, end, _ = E._day_bounds("yesterday")
        historical = header("Personal", "Stale historical unread", stamp=end - 60, native_id="native-A").split("\x01")
        historical[2] = "U"
        E.cache_history_headers("\x01".join(historical))
        self.publish(headers=header(subject="Current read copy") + f"\nC2\x01{ME}\x01{ME}\x01200\x010\x011")
        G._last[ME] = {"headers": time.time()}
        for scope in ({"day": "yesterday"}, {"period": "yesterday"}):
            with self.subTest(scope=scope):
                result = asyncio.run(E.summarize_emails(account=ME, unread=True, **scope))
                self.assertNotIn("Stale historical unread", result)
                self.assertNotIn("1 unread", result)
        historical[7] = "<historical-only@synthetic.invalid>"
        historical[8] = "Historical-only unread"
        E.cache_history_headers("\x01".join(historical))
        for scope in ({"day": "yesterday"}, {"period": "yesterday"}):
            with self.subTest(positive=scope):
                result = asyncio.run(E.summarize_emails(account=ME, unread=True, **scope))
                self.assertIn("Historical-only unread", result)

    def test_no_gmail_closed_mail_refreshes_headers_without_fetching_bodies(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_emails(header("iCloud", "Earlier native snapshot", native_id="native-cloud"))
        E.set_email_availability(True, read_source="local_index")
        async def native(event):
            self.assertEqual(event, {"type": "sync_assistant_sources_now", "sources": ["email"]})
            E.cache_emails(header("iCloud", "New local-index arrival", native_id="native-cloud"))
            E.set_email_availability(True, read_source="local_index")
        with patch("service.assistant.hub.hub.publish", native), patch.object(G, "_get", AsyncMock()) as get:
            output = asyncio.run(E.summarize_emails(day="today"))
            get.assert_not_awaited()
        self.assertIn("New local-index arrival", output)
        self.assertIn("server freshness is unverified", output)
        self.assertNotIn("Settings ▸ Google", output)

    def test_no_gmail_failed_native_refresh_displays_retained_headers_with_original_age(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_emails(header("Exchange", "Retained native message", native_id="native-exchange"))
        E.set_email_availability(True, read_source="mail_app")
        original_age = E._native_headers_at = time.time() - 3600
        E.set_email_availability(False, "Local Mail index could not be refreshed", read_source="local_index")
        with patch("service.assistant.hub.hub.publish", AsyncMock()):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Retained native message", output)
        self.assertIn("Local Mail index could not be refreshed", output)
        self.assertIn("last local read", output)
        self.assertIn("unverified", output)
        self.assertNotIn("Settings ▸ Google", output)
        self.assertEqual(E._native_headers_at, original_age)
        self.assertEqual(E.email_sync_state(), "unavailable")

    def test_optional_gmail_failure_does_not_disable_native_fallback(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_emails(header("iCloud", "Native without Google", native_id="native-cloud"))
        E.set_email_availability(True, read_source="mail_app")
        with patch.object(G, "refresh_if_stale", AsyncMock(side_effect=ImportError("Optional Google unavailable"))):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Native without Google", output)

    def test_missing_optional_gmail_import_still_uses_native_mail(self):
        import builtins
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_emails(header("iCloud", "Native with no Gmail module", native_id="native-cloud"))
        E.set_email_availability(True, read_source="mail_app")
        original = builtins.__import__
        def without_gmail(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "service" and "gmail_source" in fromlist:
                raise ModuleNotFoundError("Optional Gmail source unavailable")
            return original(name, globals, locals, fromlist, level)
        with patch("builtins.__import__", without_gmail):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Native with no Gmail module", output)

    def test_no_configured_gmail_does_not_keep_restored_provider_snapshots_or_identity(self):
        self.publish(headers=header(subject="Disconnected provider snapshot"))
        E.cache_emails(header("iCloud", "Permitted native fallback", native_id="native-cloud"))
        E.set_email_availability(True, read_source="mail_app")
        self.accounts.clear()  # Persisted account removal precedes source-cache cleanup.
        output = asyncio.run(E.summarize_emails())
        self.assertIn("Permitted native fallback", output)
        self.assertNotIn("Disconnected provider snapshot", output)
        self.assertFalse(E._external_accounts)
        self.assertNotIn(ME, E.get_identity_emails())

    def test_connected_google_does_not_hide_non_google_native_fallback(self):
        self.publish(headers=header(subject="Private Google message"))
        G._last[ME] = {"headers": time.time()}
        E.cache_emails(header("Exchange", "Retained Exchange message", native_id="native-exchange"))
        E.set_email_availability(False, "Exchange local refresh failed", read_source="local_index")
        with patch("service.assistant.hub.hub.publish", AsyncMock()):
            output = asyncio.run(E.summarize_emails(account="Exchange"))
        self.assertIn("Retained Exchange message", output)
        self.assertIn("Exchange local refresh failed", output)
        self.assertNotIn("Private Google message", output)
        self.assertNotIn("Settings ▸ Google", output)

    def test_failed_google_refresh_attempts_new_native_fallback_before_old_provider_cache(self):
        self.accounts[ME]["mail_account_id"] = "native-A"
        E.configure_external_accounts(self.accounts)
        E.cache_emails(header("Personal", "Old native copy", native_id="native-A"))
        E.set_email_availability(True, read_source="mail_app")
        E._native_headers_at = time.time() - 3600
        self.publish(headers=header(subject="Old provider copy"))
        async def native(event):
            self.assertEqual(event, {"type": "sync_assistant_sources_now", "sources": ["email"]})
            E.cache_emails(header("Personal", "New native fallback", native_id="native-A"))
            E.set_email_availability(True, read_source="mail_app")
        with patch.object(G, "_sync_headers", AsyncMock(side_effect=G.GmailError("Google offline"))), \
             patch("service.assistant.hub.hub.publish", native):
            output = asyncio.run(E.summarize_emails(account=ME))
        self.assertIn("New native fallback", output)
        self.assertNotIn("Old provider copy", output)
        self.assertIn("Google offline", output)

    def test_no_gmail_failed_empty_or_marker_only_cache_cannot_claim_empty_inbox(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        for snapshot in ("", "C3\x01iCloud\x01native-cloud\x01failed"):
            E.cache_emails(snapshot)
            E.set_email_availability(False, "Mail is closed and the local index is unavailable", read_source="local_index")
            with patch("service.assistant.hub.hub.publish", AsyncMock()):
                output = asyncio.run(E.summarize_emails(day="today"))
            self.assertIn("couldn’t verify your inbox", output)
            self.assertNotIn("No emails found.", output)
            self.assertNotIn("Grant Wisp", output)

    def test_explicit_body_request_refreshes_stale_native_fallback_without_gmail(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_emails(header("iCloud", "Old native headers"))
        E.cache_raw_emails(G.raw_record(msg("old", "Old native body"), "iCloud"))
        E.set_email_availability(True, read_source="local_index")
        original_age = E._native_raw_at = time.time() - 3600
        async def native(event):
            self.assertEqual(event, {"type": "sync_emails_now"})
            E.cache_raw_emails(G.raw_record(msg("new", "New native body"), "iCloud"))
            E.set_email_availability(True, read_source="local_index")
        with patch("service.assistant.hub.hub.publish", native):
            output = asyncio.run(E.view_emails(query="New native body"))
        self.assertIn("New native body", output)
        self.assertNotIn("Old native body", output)
        self.assertGreater(E._native_raw_at, original_age)
        self.assertIn("unverified", output)

    def test_no_gmail_body_refresh_failure_preserves_age_and_cannot_revive_expired_body(self):
        self.accounts.clear()
        E.configure_external_accounts({})
        E.cache_raw_emails(G.raw_record(msg("retained", "Retained native body"), "iCloud"))
        E.set_email_availability(False, "Mail unavailable", read_source="local_index")
        old = E._native_raw_at = time.time() - 3600
        with patch("service.assistant.hub.hub.publish", AsyncMock()) as publish:
            asyncio.run(E._ensure_email_cache(want_raw=True, timeout_seconds=0))
            publish.assert_awaited_once_with({"type": "sync_emails_now"})
        self.assertEqual(E._native_raw_at, old)
        self.assertIn("Retained native body", E._raw_emails)
        E._native_raw_at = time.time() - 8 * 86400
        with patch("service.assistant.hub.hub.publish", AsyncMock()):
            asyncio.run(E._ensure_email_cache(want_raw=True, timeout_seconds=0))
        self.assertFalse(E._raw_emails)
        self.assertNotIn("Retained native body", self.restarted_cache()["raw"])

    def test_failed_disconnect_display_write_never_relabels_provider_as_native(self):
        self.publish(headers=header(subject="Old provider display"))
        original = cache_store.save
        def fail_display(name, value):
            if name != "email_headers":
                original(name, value)
        with patch.object(cache_store, "save", fail_display):
            E.configure_external_accounts({})
        self.assertIn("Old provider display", cache_store.load("email_headers"))
        self.assertEqual(self.restarted_cache()["headers"], "")

    def test_recomposition_preserves_raw_age_and_purges_expired_data_before_saving(self):
        native = G.raw_record(msg("native"), "Native")
        E.cache_raw_emails(native)
        original_age = time.time() - 6 * 86400
        E._native_raw_at = E._raw_emails_at = original_age
        E.configure_external_accounts(self.accounts)
        self.assertEqual(self.restarted_cache(self.accounts)["native_raw_at"], original_age)
        self.publish(raw=G.raw_record(msg("provider"), ME))
        E._external_accounts[ME]["raw_at"] = time.time() - 8 * 86400
        E._native_raw_at = time.time() - 8 * 86400
        E.configure_external_accounts(self.accounts)
        restored = self.restarted_cache(self.accounts)
        self.assertEqual(restored["raw"], "")
        self.assertNotIn("Synthetic body", cache_store.load("email_sources"))

    def test_bound_rfc_duplicate_prefers_recent_read_state_and_preserves_other_accounts(self):
        self.accounts[ME]["mail_account_id"] = "native-A"
        E.configure_external_accounts(self.accounts)
        recent = E._parse_header_records(header(ME, "Current", stamp=100), snapshot="recent")[0]
        historical = E._parse_header_records(header("Personal", "Historical", stamp=200,
                                                    native_id="native-A"), snapshot="history")[0]
        recent["unread"], historical["unread"] = False, True
        other = dict(historical, account="Google", account_id="native-B")
        rows = E._unique_records([historical, recent, other])
        self.assertEqual(len(rows), 2)
        self.assertIn(recent, rows)
        self.assertIn(other, rows)
        self.assertNotIn(historical, rows)
        self.assertFalse(recent["unread"])

    def test_unbound_labels_and_producer_native_ids_do_not_collapse(self):
        one = E._parse_header_records(header("Google", native_id="native-A"), snapshot="recent")[0]
        two = dict(one, account_id="native-B", _snapshot="history")
        self.assertEqual(len(E._unique_records([one, two])), 2)
        one.update(message_id="", native_id="mail:123")
        two.update(message_id="", account_id="native-A", native_id="db:123", subject="Different")
        self.assertEqual(len(E._unique_records([one, two])), 2)

    @staticmethod
    def fake_oauth_server():
        from types import SimpleNamespace
        from unittest.mock import Mock
        callbacks = []
        server = SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 54321))], close=Mock())
        async def start(callback, *args):
            callbacks.append(callback)
            return server
        async def callback(query, index=0):
            reader = SimpleNamespace(readline=AsyncMock(return_value=f"GET /?{query} HTTP/1.1\r\n".encode()))
            writer = SimpleNamespace(close=Mock(), write=Mock(), drain=AsyncMock())
            await callbacks[index](reader, writer)
            return writer
        return start, callback

    def test_integration_removal_requires_explicit_native_json_before_any_effect(self):
        import ast
        from typing import Any
        from fastapi import FastAPI, HTTPException, Request
        from service import google_workspace as workspace
        source = Path(__file__).resolve().parents[1] / "service/main.py"
        names = {"gmail_disconnect", "google_workspace_disable", "_require_native_google_removal"}
        nodes = [n for n in ast.parse(source.read_text()).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(nodes), 3)
        app = FastAPI()
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"),
             {"app": app, "Any": Any, "Request": Request, "HTTPException": HTTPException})
        async def run():
            with patch.object(G, "disconnect", AsyncMock()) as disconnect, \
                 patch.object(workspace, "disable", AsyncMock(return_value={"ok": True})) as disable:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://127.0.0.1:8765") as client:
                    invalid = [
                        {},
                        {"content": "account=", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
                        {"content": "--fixture--", "headers": {"Content-Type": "multipart/form-data; boundary=fixture"}},
                        {"content": "{}", "headers": {"Content-Type": "text/plain"}},
                        {"content": "{}"},
                        {"content": "{", "headers": {"Content-Type": "application/json"}},
                        {"json": []}, {"content": "null", "headers": {"Content-Type": "application/json"}},
                        {"json": "account"}, {"json": 1},
                    ]
                    for origin in ("https://foreign.example.test", "null", "http://127.0.0.1:8765", ""):
                        invalid.append({"json": {}, "headers": {"Origin": origin}})
                    invalid.append({"json": {}, "headers": {
                        "Host": "forged.example.test", "Origin": "http://forged.example.test"}})
                    for path in ("/assistant/gmail/disconnect", "/assistant/google/workspace/disable"):
                        for request in invalid:
                            with self.subTest(path=path, request=request):
                                response = await client.post(path, **request)
                                self.assertIn(response.status_code, (403, 415, 422), response.text)
                                disconnect.assert_not_awaited()
                                disable.assert_not_awaited()
                    response = await client.post("/assistant/gmail/disconnect", json={"account": ME})
                    self.assertEqual(response.json(), {"ok": True})
                    disconnect.assert_awaited_once_with(ME)
                    disconnect.reset_mock()
                    response = await client.post("/assistant/gmail/disconnect", json={})
                    self.assertEqual(response.json(), {"ok": True})
                    disconnect.assert_awaited_once_with("")
                    response = await client.post("/assistant/google/workspace/disable", json={})
                    self.assertEqual(response.json(), {"ok": True})
                    disable.assert_awaited_once_with()
        asyncio.run(run())

    def test_atomic_connect_receipts_distinguish_superseding_http_flow(self):
        import ast
        from typing import Any
        from fastapi import FastAPI
        from service import google_workspace as workspace
        source = Path(__file__).resolve().parents[1] / "service/main.py"
        route = next(n for n in ast.parse(source.read_text()).body
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "gmail_connect")
        app = FastAPI()
        exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), "exec"), {"app": app, "Any": Any})
        async def run():
            start, callback = self.fake_oauth_server()
            with patch("asyncio.start_server", start), patch.object(workspace, "saved_credentials", return_value=("", "")), \
                 patch.object(G, "_finish", AsyncMock(return_value=SCHOOL)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid") as client:
                    first = (await client.post("/assistant/gmail/connect", json={"client_id": "synthetic"})).json()
                    second = (await client.post("/assistant/gmail/connect", json={"client_id": "synthetic"})).json()
                from urllib.parse import parse_qs, urlparse
                state = parse_qs(urlparse(second["auth_url"]).query)["state"][0]
                await callback("state=" + state + "&code=fake", index=1)
                await asyncio.sleep(0)
                status = G.status()["authorization"]
                self.assertTrue(first["ok"] and second["ok"])
                self.assertNotEqual(first["generation"], second["generation"])
                self.assertEqual(second["generation"], status["generation"])
                self.assertNotEqual(first["generation"], status["generation"])
                self.assertEqual((status["state"], status["email"]), ("connected", SCHOOL))
        asyncio.run(run())

    def test_superseded_listener_start_cannot_replace_or_close_newer_flow(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            old = SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 54321))], close=Mock())
            new = SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 54322))], close=Mock())
            calls = 0
            async def start(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    await release.wait()
                    return old
                return new
            with patch("asyncio.start_server", start):
                pending = asyncio.create_task(G.begin_authorization("first", include_receipt=True))
                await entered.wait()
                receipt = await G.begin_authorization("second", include_receipt=True)
                release.set()
                with self.assertRaisesRegex(G.GmailError, "replaced"):
                    await pending
                self.assertIs(G._pending_flow["server"], new)
                old.close.assert_called_once()
                new.close.assert_not_called()
                self.assertEqual(receipt["generation"], G._authorization["generation"])
                self.assertEqual(G._authorization["state"], "authorizing")
        asyncio.run(run())

    def test_failed_listener_start_has_terminal_feedback_without_pending_flow(self):
        async def run():
            with patch("asyncio.start_server", AsyncMock(side_effect=OSError("synthetic private detail"))):
                with self.assertRaisesRegex(G.GmailError, "start Google sign-in"):
                    await G.begin_authorization("synthetic", include_receipt=True)
            self.assertIsNone(G._pending_flow)
            self.assertEqual(G._authorization["state"], "failed")
            self.assertNotIn("private detail", G._authorization["error"])
        asyncio.run(run())

    def test_unrelated_oauth_callback_cannot_finish_or_cancel_active_flow(self):
        from urllib.parse import urlparse, parse_qs
        async def run():
            start, callback = self.fake_oauth_server()
            with patch("asyncio.start_server", start):
                url = await G.begin_authorization("synthetic", timeout=1)
                args = parse_qs(urlparse(url).query)
                with patch.object(G, "_finish", AsyncMock()) as finish:
                    await callback("state=incorrect&code=fake")
                    self.assertIsNotNone(G._pending_flow)
                    finish.assert_not_awaited()
                    await callback("state=" + args["state"][0] + "&error=access_denied")
                    await asyncio.sleep(0.01)
                    finish.assert_not_awaited()
                    self.assertIsNone(G._pending_flow)
                    self.assertEqual(G._authorization["state"], "failed")
        asyncio.run(run())

    def test_verified_empty_body_snapshot_does_not_request_native_mail(self):
        self.publish(headers="", raw="")
        G._last[ME] = {"headers": time.time(), "raw": time.time()}
        E._email_available = False
        with patch("service.assistant.hub.hub.publish", AsyncMock()) as native:
            output = asyncio.run(E.view_emails())
        native.assert_not_awaited()
        self.assertIn("No emails found", output)
        self.assertNotIn("permission", output.lower())

    def test_history_fetch_cannot_block_interactive_headers(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            async def history(*args):
                started.set()
                await release.wait()
            async def headers(http, account):
                self.publish(headers=header(subject="New header during history"))
            with patch.object(G, "_sync_history", history), patch.object(G, "_sync_headers", headers):
                pending = asyncio.create_task(G._sync_account(ME, ("history",)))
                await started.wait()
                self.assertTrue(await asyncio.wait_for(G._sync_account(ME, ("headers",)), 0.2))
                release.set()
                await pending
        asyncio.run(run())
        self.assertIn("New header during history", E._headers)

    def test_newer_native_success_is_not_hidden_by_failed_provider(self):
        self.publish(headers=header(subject="Old provider copy"))
        E.external_email_error(ME, "headers", "offline")
        E.cache_emails(header(subject="Newer native email"))
        E.set_email_availability(True, read_source="mail_app")
        self.assertIn("Newer native email", E._headers)
        self.assertNotIn("Old provider copy", E._headers)
        self.assertIn("offline", E.email_freshness_warning())

    def test_reconnect_during_fetch_rejects_previous_token_generation(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            async def get(http, path, params=None):
                if path == "messages":
                    started.set()
                    await release.wait()
                    return {"messages": [{"id": "old-session"}]}
                return msg("old-session", "Old session payload")
            with patch.object(G, "_get", get):
                pending = asyncio.create_task(G.sync())
                await started.wait()
                self.accounts[ME] = {**self.accounts[ME], "refresh_token": "replacement"}
                G._epochs[ME] = 1
                release.set()
                self.assertFalse(await pending)
        asyncio.run(run())
        self.assertNotIn("Old session payload", E._headers)

    def test_superseded_oauth_exchange_cannot_save_or_start_sync(self):
        token_body = {"refresh_token": "fake", "access_token": "fake", "scope": G.SCOPE}
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            async def profile(*args, **kwargs):
                started.set()
                await release.wait()
                return httpx.Response(200, json={"emailAddress": ME})
            with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json=token_body))), \
                 patch.object(httpx.AsyncClient, "get", profile), patch.object(G, "_save_config") as save:
                pending = asyncio.create_task(G._finish("fake", "fake", "code", "verifier", "http://127.0.0.1", generation=0))
                await started.wait()
                G._flow_generation = 1
                release.set()
                await pending
                save.assert_not_called()
        asyncio.run(run())

    def test_cli_waits_for_current_flow_with_an_existing_account(self):
        import io
        import runpy
        from contextlib import redirect_stdout
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/gmail_connect.py"))
        statuses = [{"connected": True, "authorizing": True, "accounts": [{"email": ME}]},
                    {"connected": True, "authorizing": False, "accounts": [{"email": ME}, {"email": SCHOOL}],
                     "authorization": {"state": "connected", "email": SCHOOL}}]
        output = io.StringIO()
        with patch.object(sys, "argv", ["gmail_connect.py", "synthetic"]), \
             patch.object(G, "begin_authorization", AsyncMock(return_value="https://accounts.google.com/synthetic")), \
             patch.object(G, "status", side_effect=statuses), patch("webbrowser.open"), \
             patch("asyncio.sleep", AsyncMock()), redirect_stdout(output):
            asyncio.run(module["main"]())
        self.assertIn(SCHOOL, output.getvalue())

    def test_first_refresh_failure_does_not_blame_mail_permissions(self):
        E._email_available = False
        async def fail(*args):
            raise G.GmailError("Google is offline")
        with patch.object(G, "_sync_headers", fail):
            output = asyncio.run(E.summarize_emails())
        self.assertIn("Google", output)
        self.assertIn("offline", output)
        self.assertNotIn("grant Wisp Automation", output)
        self.assertNotIn("No emails found", output)

    def test_unrelated_new_native_read_cannot_erase_provider_fallback(self):
        self.publish(headers=header(subject="Personal fallback"))
        E.external_email_error(ME, "headers", "offline")
        E.cache_emails(header(SCHOOL, "Native school message"))
        E.set_email_availability(True, read_source="mail_app")
        self.assertIn("Personal fallback", E._headers)
        self.assertIn("Native school message", E._headers)

    def test_expired_provider_body_is_removed_from_persisted_source_cache(self):
        self.publish(headers=header(), raw=G.raw_record(msg("private"), ME))
        E._external_accounts[ME]["raw_at"] = time.time() - 8 * 86400
        E._purge_raw_if_expired()
        self.assertIsNone(E._external_accounts[ME]["raw"])
        self.assertNotIn("Synthetic body", cache_store.load("email_sources"))

    def test_native_diagnostic_update_preserves_current_header_bytes_without_provider(self):
        E.configure_external_accounts({})
        E.cache_emails(header("Native", "Earlier native copy"))
        # Restored/current display bytes can precede the next native backing push.
        current = header("Native", "Current usable copy")
        E._headers = current
        generation = E._headers_sync_generation
        E.set_email_availability(True, read_source="local_index")
        self.assertEqual(E._headers, current)
        self.assertEqual(E._headers_sync_generation, generation)
        self.assertIn("local cache", E.email_freshness_warning())

    def test_bound_label_id_and_provider_address_reach_native_fallback(self):
        self.accounts[ME]["mail_account_id"] = "native-A"
        E.cache_emails(header("Personal", "Native fallback", native_id="native-A") +
                      "\nC2\x01Personal\x01native-A\x01200\x010\x011")
        E.configure_external_accounts(self.accounts)
        E.external_email_error(ME, "headers", "offline")
        for selector in ("Personal", "native-A", ME):
            with self.subTest(selector=selector):
                rows = E._filter_account_records(E._parse_header_records(E._headers), selector)
                self.assertEqual([r["subject"] for r in rows], ["Native fallback"])
                self.assertIn("Personal", E.header_scan_coverage(rows, raw_headers=E._headers, account=selector)["cap_accounts"])
                self.assertIn("offline", E.email_freshness_warning(selector))

    def test_duplicate_google_body_labels_stay_ambiguous_after_provider_replacement(self):
        self.accounts[ME]["mail_account_id"] = "native-A"
        E.cache_emails(header("Google", "Native A", native_id="native-A") + "\n" +
                      header("Google", "Native B", native_id="native-B"))
        E.configure_external_accounts(self.accounts)
        def native_body(native_id):
            parts = G.raw_record(msg(native_id), "Google").split("\x01")
            parts[3] = native_id
            return "\x01".join(parts)
        E.cache_raw_emails(native_body("native-A") + "\x02" + native_body("native-B"))
        for use_provider in (False, True):
            if use_provider:
                self.publish(raw=G.raw_record(msg("provider"), ME))
            selected, problem = E._raw_account_scope(E._parse_raw(), "Google")
            self.assertFalse(selected)
            self.assertIn("several email accounts", problem)
            for selector in ("native-A", ME):
                rows, problem = E._raw_account_scope(E._parse_raw(), selector)
                self.assertFalse(problem)
                self.assertEqual(len(rows), 1)
                self.assertNotEqual(rows[0]["account_id"], "native-B")

    def test_failure_immediately_selects_newer_native_after_provider_success(self):
        self.publish(headers=header(subject="Provider old"))
        E.cache_emails(header(subject="Native new"))
        E.set_email_availability(True, read_source="mail_app")
        self.assertIn("Provider old", E._headers)
        original = E._external_accounts[ME]["headers_at"]
        E.external_email_error(ME, "headers", "offline after native read")
        self.assertIn("Native new", E._headers)
        self.assertNotIn("Provider old", E._headers)
        self.assertEqual(E._external_accounts[ME]["headers_at"], original)

    def test_date_summary_discloses_history_error_while_recent_summary_stays_fresh(self):
        self.publish(headers=header() + "\nC2\x01" + ME + "\x01" + ME + "\x01200\x010\x011",
                     history=header(subject="Older cached history", stamp=time.time() - 86400))
        E.external_email_error(ME, "history", "history fetch failed")
        dated = asyncio.run(E.summarize_inbox_for_day("yesterday", unread=True))
        self.assertIn("Gmail history cache", dated)
        self.assertIn("history fetch failed", dated)
        self.assertNotIn("history fetch failed", asyncio.run(E.summarize_inbox_recent()))

    def test_missing_history_cannot_claim_empty_older_range(self):
        self.publish(headers=header() + "\nC2\x01" + ME + "\x01" + ME + "\x01200\x010\x011")
        output = asyncio.run(E.summarize_inbox_for_day("2025-01-01"))
        self.assertIn("couldn’t verify", output)
        self.assertIn("history cache", output)

    def test_complete_fresh_headers_do_not_consume_obsolete_history(self):
        self.publish(headers=header(subject="Current") + "\nC2\x01" + ME + "\x01" + ME + "\x011\x010\x010",
                     history=header(subject="Obsolete history entry", stamp=time.time() - 86400))
        E.external_email_error(ME, "history", "history offline")
        output = asyncio.run(E.summarize_inbox_for_day("yesterday"))
        self.assertNotIn("Obsolete history entry", output)
        self.assertNotIn("history offline", output)
        self.assertNotIn("couldn’t verify", output)

    def test_native_history_has_its_own_timestamp_and_snapshot(self):
        E.configure_external_accounts({})
        E.cache_emails(header("Native", "Recent"))
        E.cache_history_headers(header("Native", "Older", stamp=time.time() - 86400))
        E.set_email_availability(True, read_source="mail_app")
        E._native_history_at = time.time() - 3600
        history = E.email_source_details(part="history")[0]
        self.assertEqual(history["last_success"], E._native_history_at)
        self.assertFalse(history["fresh"])
        self.assertTrue(E.email_source_details()[0]["fresh"])

    def test_old_epoch_cannot_dispatch_cached_or_refresh_token(self):
        async def run(cached):
            token = G._context.set((ME, dict(self.accounts[ME]), 0))
            G._epochs[ME] = 1
            G._access[ME] = {"token": "old-access" if cached else "", "expires": time.time() + 3600}
            http = AsyncMock()
            try:
                with self.assertRaises(G.GmailError):
                    await G._access_token(http)
                http.post.assert_not_awaited()
                http.get.assert_not_awaited()
            finally:
                G._context.reset(token)
        asyncio.run(run(True))
        asyncio.run(run(False))

    def test_disconnect_during_backoff_prevents_another_api_dispatch(self):
        async def run():
            token = G._context.set((ME, dict(self.accounts[ME]), 0))
            G._access[ME] = {"token": "synthetic", "expires": time.time() + 3600}
            http = AsyncMock()
            http.get.return_value = httpx.Response(429)
            async def backoff(_):
                G._epochs[ME] = 1
            try:
                with patch("asyncio.sleep", backoff), self.assertRaises(G.GmailError):
                    await G._get(http, "messages")
                http.get.assert_awaited_once()
                http.post.assert_not_awaited()
            finally:
                G._context.reset(token)
        asyncio.run(run())

    def test_failed_current_cli_flow_does_not_report_older_account_success(self):
        import io
        import runpy
        from contextlib import redirect_stdout
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/gmail_connect.py"))
        info = {"connected": True, "authorizing": False, "accounts": [{"email": ME}],
                "authorization": {"state": "failed", "error": "Google sign-in declined"}}
        output = io.StringIO()
        with patch.object(sys, "argv", ["gmail_connect.py", "synthetic"]), \
             patch.object(G, "begin_authorization", AsyncMock(return_value="https://accounts.google.com/synthetic")), \
             patch.object(G, "status", return_value=info), patch("webbrowser.open"), \
             patch("asyncio.sleep", AsyncMock()), redirect_stdout(output), self.assertRaises(SystemExit):
            asyncio.run(module["main"]())
        self.assertNotIn("Connected:", output.getvalue())

    def test_exact_address_native_uuid_keeps_one_identity_before_and_after_provider(self):
        E.cache_emails(header(ME, "Native by exact address", native_id="native-A"))
        E.configure_external_accounts(self.accounts)
        for use_provider in (False, True):
            if use_provider:
                self.publish(headers=header(subject="Provider by exact address"))
            for selector in (ME, "native-A"):
                rows = E._filter_account_records(E._parse_header_records(E._headers), selector)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["subject"], "Provider by exact address" if use_provider else "Native by exact address")
        E.cache_emails(header(ME, "New native fallback", native_id="native-A"))
        E.set_email_availability(True, read_source="mail_app")
        E.external_email_error(ME, "headers", "offline")
        for selector in (ME, "native-A"):
            rows = E._filter_account_records(E._parse_header_records(E._headers), selector)
            self.assertEqual(rows[0]["subject"], "New native fallback")

    def test_queued_metadata_does_not_dispatch_after_disconnect_epoch(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            token = G._context.set((ME, dict(self.accounts[ME]), 0))
            G._access[ME] = {"token": "fake", "expires": time.time() + 3600}
            async def get(*args, **kwargs):
                started.set()
                await release.wait()
                return httpx.Response(200, json=msg("one"))
            http = AsyncMock()
            http.get.side_effect = get
            try:
                with patch.object(G, "FETCH_CONCURRENCY", 1):
                    pending = asyncio.create_task(G._ensure_meta(http, ["one", "two"]))
                    await started.wait()
                    G._epochs[ME] = 1
                    release.set()
                    with self.assertRaises(G.GmailError):
                        await pending
                    await asyncio.sleep(0)
                    http.get.assert_awaited_once()
                    http.post.assert_not_awaited()
            finally:
                G._context.reset(token)
        asyncio.run(run())

    def test_oauth_timeout_fences_inflight_exchange_and_profile_dispatch(self):
        from urllib.parse import parse_qs, urlparse
        async def run():
            start, callback = self.fake_oauth_server()
            started, release = asyncio.Event(), asyncio.Event()
            async def post(*args, **kwargs):
                started.set()
                await release.wait()
                return httpx.Response(200, json={"refresh_token": "fake", "access_token": "fake", "scope": G.SCOPE})
            with patch("asyncio.start_server", start), patch.object(httpx.AsyncClient, "post", post), \
                 patch.object(httpx.AsyncClient, "get", AsyncMock()) as get, patch.object(G, "_save_config") as save:
                url = await G.begin_authorization("synthetic", timeout=0.02)
                state = parse_qs(urlparse(url).query)["state"][0]
                pending = asyncio.create_task(callback("state=" + state + "&code=fake"))
                await started.wait()
                await asyncio.sleep(0.05)
                self.assertEqual(G._authorization["state"], "failed")
                release.set()
                await pending
                get.assert_not_awaited()
                save.assert_not_called()
                self.assertEqual(G._authorization["state"], "failed")
        asyncio.run(run())

    _original_accounts = staticmethod(G.connected_accounts)


class HistoryPublicBinding(unittest.TestCase):
    """Public bridge uses source config and actual fake token owner, no caller token."""
    def setUp(self):
        from service import gmail_behavior_history as H
        self.H = H
        self.config = {"client_id": "configured-client", "client_secret": "fake-secret",
                       "accounts": {ME: {"refresh_token": "configured-refresh"}}}
        self.enterContext(patch.object(G, "load_config", lambda: self.config))
        G.refresh_history_configuration()
        self.scope = H.HistoryScope(100, 190, "inbox", 100)
        self.permission = None
        self.app = G.install_history_application(probe=lambda: self.permission)
        self.addCleanup(self.app.revoke)
        self.bridge = G.new_public_history_bridge(application=self.app,
            limits=H.Limits(80000, 8000000, 32000000, 100), clock=lambda: self.now, monotonic=lambda: self.mono)
        self.addCleanup(self.bridge.invalidate)
        self.calls = []
        self.profile = ME
        self.hook = None
        self.token_scope = G.SCOPE
        self.token_status = 200
        self.message_status = 200
        self.pages = [{"messages": [{"id": "51"}]}]
        self.now, self.mono = 200, 0
        self.http = G.HistoryHTTPClient.synthetic(self.server)
        self.addCleanup(lambda client=self.http: asyncio.run(client.aclose()))
        self.budget = H.StepBudget(100, 5, 6, 400000, 30)

    async def server(self, request):
        from tests.test_gmail_behavior_history import message
        from urllib.parse import parse_qs
        self.calls.append(request)
        if self.hook:
            await self.hook(request)
        if str(request.url) == G.TOKEN_URL:
            form = parse_qs(request.content.decode())
            self.assertEqual(form["client_id"], ["configured-client"])
            self.assertEqual(form["refresh_token"], ["configured-refresh"])
            return httpx.Response(self.token_status, json={"access_token": "server-issued", "scope": self.token_scope,
                                             "token_type": "Bearer", "expires_in": 3600})
        self.assertEqual(request.headers["Authorization"], "Bearer server-issued")
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": self.profile})
        if request.url.path.endswith("/messages"):
            return httpx.Response(self.message_status, json=self.pages.pop(0) if self.pages else {})
        return httpx.Response(self.message_status, json=message(request.url.path.rsplit("/", 1)[1]))

    def admit(self):
        accounts = G.history_account_status()
        self.assertEqual(len(accounts), 1, "Configured source must publish a public account lease")
        lease = G.history_account_lease(accounts[0].account.account_id)
        self.assertIsNotNone(lease)
        self.permission = G.HistoryPermissionState((lease.account.account_id,), (self.scope.digest,),
            "server-session", 1, "gmail_history", 1, 1, True, True, True)
        grant = self.app.issue(lease, self.scope)
        self.bridge.start(grant)
        return grant

    def step(self):
        return asyncio.run(self.bridge.step(http=self.http, budget=self.budget))

    def test_default_off_public_metadata_and_step_do_not_touch_network(self):
        self.assertEqual(self.bridge.status()["state"], "off")
        self.assertIsNone(self.bridge.snapshot())
        self.step()
        self.assertEqual(self.calls, [])

    def test_actual_configured_source_refresh_profile_list_body_pipeline(self):
        self.admit()
        self.step()
        snapshot = self.bridge.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.records[0].resource_id, "51")
        self.assertEqual(len(self.calls), 4)

    def test_public_memory_witness_binds_source_application_config_and_recovery_without_io(self):
        from service.publication_authority import PublicationBusy,publication
        from service.config import quarantine
        self.admit(); self.step()
        snapshot = self.bridge.snapshot()
        witness = snapshot.publication_witness
        self.assertIsNotNone(witness)
        original = G._history_config_digest
        gate = quarantine.current_gate()
        with publication(witness):
            for operation in (self.app.revoke,lambda:self.bridge.control("pause"),
                              lambda:G._save_config(self.config),gate.invalidate):
                with self.assertRaises(PublicationBusy):
                    operation()
                self.assertTrue(witness.current())
            self.assertFalse(self.app._revoked or gate.blocked)
            self.assertEqual(G._history_config_digest,original)
            with patch.object(G,"load_config",side_effect=AssertionError("Config I/O")), \
                 patch.object(self.bridge,"snapshot",side_effect=AssertionError("Source getter")), \
                 patch.object(gate,"generation",side_effect=AssertionError("Recovery file I/O")), \
                 patch.object(self.app,"_probe",side_effect=AssertionError("Application callback")):
                self.assertTrue(witness.current())
        self.app.revoke()
        self.assertFalse(witness.current())
        self.assertIsNone(self.bridge.snapshot())

    def test_public_memory_witness_is_withdrawn_before_config_or_recovery_mutation_ack(self):
        from service.config import quarantine
        self.admit(); self.step()
        witness = self.bridge.snapshot().publication_witness
        G._withdraw_history_configuration()
        self.assertFalse(witness.current())
        # Re-admission requires current source/bootstrap state, not saved tokens.
        G.refresh_history_configuration()
        self.pages = [{"messages":[{"id":"51"}]}]
        self.admit(); self.step()
        witness = self.bridge.snapshot().publication_witness
        gate = quarantine.RecoveryGate(home=lambda:Path(os.environ["HOME"]))
        # Use a separate private gate; avoid latching the shared test gate.
        with patch.object(quarantine,"_gate",gate):
            G.refresh_history_configuration()
            self.assertFalse(witness.current())
            http = G.HistoryHTTPClient.synthetic(self.server)
            self.addCleanup(lambda:asyncio.run(http.aclose()))
            self.http = http
            self.pages = [{"messages":[{"id":"51"}]}]
            self.admit(); self.step()
            current = self.bridge.snapshot().publication_witness
            self.assertTrue(current.current())
            with self.assertRaises(quarantine.CredentialQuarantined):
                gate.invalidate()
            self.assertFalse(current.current())

    def test_wrong_actual_token_owner_is_rejected_even_when_grant_declares_right_account(self):
        self.admit()
        self.profile = "other@gmail.com"
        self.step()
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual([r.url.path.rsplit("/", 1)[1] for r in self.calls], ["token", "profile"])

    def test_public_status_snapshot_checkpoint_currentness_have_zero_io(self):
        self.admit()
        self.step()
        receipt = self.bridge.snapshot().receipt
        with patch.object(G, "load_config", side_effect=AssertionError("getter config I/O")), \
             patch.object(G.httpx.AsyncClient, "request", side_effect=AssertionError("getter network")), \
             patch("service.config.quarantine.check", side_effect=AssertionError("getter recovery I/O")):
            accounts = G.history_account_status()
            self.assertEqual(accounts[0].display_label, ME)
            self.assertIsNotNone(G.history_account_lease(accounts[0].account.account_id))
            for _ in range(3):
                self.bridge.status()
                self.bridge.checkpoint()
                current = E.provider_history_snapshot(self.bridge, expected_receipt=receipt,
                    required_refs=(receipt.refs[0],))
                self.assertTrue(current.complete)
        self.assertEqual(len(self.calls), 4)

    def test_payload_and_cloned_lease_grant_cannot_install_or_restart_permission(self):
        with self.assertRaises(ValueError):
            G.install_history_application(probe={"history_start": True})
        with self.assertRaises(ValueError):
            G.HistoryApplicationAuthority(probe=lambda: self.permission)
        with self.assertRaises(PermissionError):
            self.bridge.start({"account_id": "chosen", "history_start": True})
        grant = self.admit()
        with self.assertRaises(PermissionError):
            self.app.issue(dataclasses.replace(grant.account_lease), self.scope)
        with self.assertRaises(PermissionError):
            self.bridge.start(dataclasses.replace(grant))
        self.bridge.control("forget")
        with self.assertRaises(PermissionError):
            self.bridge.start(grant)
        self.assertEqual(self.calls, [])

    def test_no_source_account_or_application_permission_cannot_issue(self):
        lease = G.history_account_lease(G.history_account_status()[0].account.account_id)
        with self.assertRaises(PermissionError):
            self.app.issue(lease, self.scope)

        grant = self.admit()
        original = self.permission
        for updates in ({"global_learning": False}, {"email_consent": False}, {"history_start": False},
                        {"role": "other"}, {"account_ids": ()}, {"scope_digests": ()},
                        {"session_epoch": True}, {"session_id": ""}):
            self.permission = dataclasses.replace(original, **updates)
            with self.assertRaises(PermissionError):
                self.app.issue(lease, self.scope)
            self.assertIsNone(self.bridge.snapshot())
        self.permission = original
        G._withdraw_history_configuration()
        with self.assertRaises(PermissionError):
            self.app.issue(lease, self.scope)

    def test_multiple_account_scope_sets_must_not_imply_a_cross_product_grant(self):
        self.config["accounts"][SCHOOL] = {"refresh_token": "school-refresh"}
        G.refresh_history_configuration()
        accounts = G.history_account_status()
        second = dataclasses.replace(self.scope, start=90)
        self.permission = G.HistoryPermissionState(tuple(row.account.account_id for row in accounts),
            (self.scope.digest, second.digest), "server-session", 1, "gmail_history", 1, 1, True, True, True)
        first_lease = G.history_account_lease(accounts[0].account.account_id)
        second_lease = G.history_account_lease(accounts[1].account.account_id)
        with self.assertRaises(PermissionError):
            self.app.issue(first_lease, second)
        self.permission = dataclasses.replace(self.permission, account_scopes=(
            (first_lease.account.account_id, self.scope.digest), (second_lease.account.account_id, second.digest)))
        self.assertIsNotNone(self.app.issue(first_lease, self.scope))
        self.assertIsNotNone(self.app.issue(second_lease, second))
        with self.assertRaises(PermissionError):
            self.app.issue(first_lease, second)
        with self.assertRaises(PermissionError):
            self.app.issue(second_lease, self.scope)

    def test_token_post_profile_get_and_body_get_share_six_call_budget(self):
        self.pages = [{"messages": [{"id": str(i)} for i in range(5)]}]
        self.admit()
        self.step()
        self.assertEqual(len(self.calls), 6)
        status = self.bridge.status()
        self.assertEqual(status["http_calls"], 6)
        self.assertEqual(status["fetched"], 3)
        self.assertEqual(status["pending"], 2)
        self.step()  # Exact same verified token lease, no refresh/profile repeat.
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.bridge.status()["cached"], 5)

    def test_budget_exhaustion_before_profile_never_caches_unverified_token(self):
        self.admit()
        asyncio.run(self.bridge.step(http=self.http, budget=self.H.StepBudget(100, 5, 1, 400000, 30)))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.bridge.snapshot())
        self.step()
        self.assertEqual([r.url.path.rsplit("/", 1)[1] for r in self.calls],
                         ["token", "token", "profile", "messages", "51"])

    def test_invalid_scope_failed_refresh_and_read_revocation_fail_closed_without_retry(self):
        self.admit()
        self.token_scope = "https://www.googleapis.com/auth/gmail.metadata"
        self.step()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.bridge.snapshot())
        self.assertIn("authorization", self.bridge.status()["binding_error"])
        self.token_scope = G.SCOPE
        self.token_status = 400
        self.admit()
        self.step()
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.bridge.snapshot())
        self.token_status = 200
        self.message_status = 401
        self.admit()
        self.step()
        self.assertEqual(len(self.calls), 5)
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual(G.history_account_status(), ())

    def test_every_await_checks_role_session_consent_and_no_late_forget_recreation(self):
        for boundary in ("token", "profile", "messages", "51"):
            for mutation in ("session_epoch", "role_epoch", "consent_epoch", "session_id", "role",
                             "history_start", "global_learning", "email_consent", "forget", "pause", "cancel"):
                with self.subTest(boundary=boundary, mutation=mutation):
                    G.refresh_history_configuration()
                    self.admit()
                    self.pages = [{"messages": [{"id": "51"}]}]
                    fired = []
                    async def change(request):
                        if request.url.path.rsplit("/", 1)[1] == boundary:
                            fired.append(True)
                            if mutation in ("forget", "pause", "cancel"):
                                self.bridge.control(mutation)
                            elif mutation in ("history_start", "global_learning", "email_consent"):
                                self.permission = dataclasses.replace(self.permission, **{mutation: False})
                            elif mutation in ("session_id", "role"):
                                self.permission = dataclasses.replace(self.permission, **{mutation: "changed"})
                            else:
                                self.permission = dataclasses.replace(self.permission,
                                    **{mutation: getattr(self.permission, mutation) + 1})
                            await asyncio.sleep(0)
                    self.hook = change
                    before = len(self.calls)
                    self.step()
                    self.assertTrue(fired)
                    self.assertIsNone(self.bridge.snapshot())
                    self.assertEqual(self.bridge.status()["cached"], 0)
                    self.assertFalse(self.bridge.status()["fresh"])
                    self.assertEqual(len(self.calls) - before, {"token": 1, "profile": 2, "messages": 3, "51": 4}[boundary])
                    self.hook = None

    def test_client_credentials_accountset_mutations_at_each_await_revoke_old_source(self):
        for boundary in ("token", "profile", "messages", "51"):
            for mutation in ("client_id", "client_secret", "refresh_token", "accountset"):
                with self.subTest(boundary=boundary, mutation=mutation):
                    self.config = {"client_id": "configured-client", "client_secret": "fake-secret",
                                   "accounts": {ME: {"refresh_token": "configured-refresh"}}}
                    G.refresh_history_configuration()
                    old_grant = self.admit()
                    self.pages = [{"messages": [{"id": "51"}]}]
                    fired = []
                    async def change(request):
                        if request.url.path.rsplit("/", 1)[1] == boundary:
                            fired.append(True)
                            if mutation == "accountset":
                                self.config["accounts"][SCHOOL] = {"refresh_token": "school-refresh"}
                            elif mutation == "refresh_token":
                                self.config["accounts"][ME][mutation] = "changed"
                            else:
                                self.config[mutation] = "changed"
                            G.refresh_history_configuration()
                            await asyncio.sleep(0)
                    self.hook = change
                    before = len(self.calls)
                    self.step()
                    self.assertTrue(fired)
                    self.assertEqual(len(self.calls) - before, {"token": 1, "profile": 2, "messages": 3, "51": 4}[boundary])
                    self.assertIsNone(self.bridge.snapshot())
                    with self.assertRaises(PermissionError):
                        self.bridge.start(old_grant)
                    self.hook = None

    def test_explicit_application_refresh_and_revoke_withdraw_without_getter_or_await(self):
        self.admit()
        self.step()
        self.permission = dataclasses.replace(self.permission, global_learning=False)
        self.app.refresh()
        self.assertIsNone(self.bridge.snapshot())
        self.assertFalse(self.bridge.status()["fresh"])
        self.app.revoke()
        self.permission = dataclasses.replace(self.permission, global_learning=True)
        lease = G.history_account_lease(G.history_account_status()[0].account.account_id)
        with self.assertRaises(PermissionError):
            self.app.issue(lease, self.scope)

    def test_source_configuration_save_and_failed_save_withdraw_before_io(self):
        self.admit()
        self.step()
        with patch.object(G.os, "open", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                G._save_config(self.config)
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual(G.history_account_status(), ())

    def test_authorization_begin_withdraws_history_before_its_first_await(self):
        self.admit()
        self.step()
        observed = []
        async def listener(*args, **kwargs):
            observed.append(self.bridge.snapshot())
            self.assertEqual(G.history_account_status(), ())
            raise OSError("synthetic listener blocked")
        with patch.multiple(G, _pending_flow=None, _flow_generation=0, _authorization={}), \
             patch.object(G.asyncio, "start_server", listener):
            with self.assertRaises(G.GmailError):
                asyncio.run(G.begin_authorization("other-client"))
        self.assertEqual(observed, [None])

    def test_disconnect_withdraws_public_bridge_before_revoke_and_keeps_native_cache(self):
        self.admit()
        self.step()
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(G, "CONFIG_PATH", Path(folder) / "gmail.json"), \
             patch.object(G.httpx, "AsyncClient") as client:
            seen = []
            async def revoke(*args, **kwargs):
                seen.append(self.bridge.snapshot())
                return httpx.Response(200)
            client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=revoke)
            with patch.object(E, "_swift_headers", "genuine-native"):
                asyncio.run(G.disconnect(ME))
                self.assertEqual(E._swift_headers, "genuine-native")
        self.assertEqual(seen, [None])
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual(self.bridge.status()["state"], "disconnected")

    def test_cached_older_refs_and_v3_time_schema_stay_valid_after_unrelated_page(self):
        self.pages = [{"messages": [{"id": "51"}], "nextPageToken": "next"},
                      {"messages": [{"id": "52"}]}]
        self.admit()
        self.step()
        original = self.bridge.snapshot()
        receipt = self.H.HistoryReceipt.from_metadata(json.loads(json.dumps(original.receipt.to_metadata())))
        ref = self.H.RecordRef.from_metadata(json.loads(json.dumps(original.records[0].ref.to_metadata())))
        self.step()
        before = len(self.calls)
        older = E.provider_history_snapshot(self.bridge, expected_receipt=receipt, required_refs=(ref,))
        self.assertTrue(older.complete)
        self.assertEqual(len(self.calls), before)
        self.assertIsNone(older.records[0].event_at)
        self.assertFalse(older.records[0].time_qualification.relative_deadlines_supported)

    def test_unannounced_config_mutation_during_await_is_observed_before_publication(self):
        self.admit()
        async def mutate(request):
            if request.url.path.endswith("/51"):
                self.config["accounts"][ME]["refresh_token"] = "new-unannounced-credential"
        self.hook = mutate
        self.step()
        self.assertEqual(len(self.calls), 4)
        self.assertIsNone(self.bridge.snapshot())
        self.assertFalse(self.bridge.status()["fresh"])

    def test_sync_credential_recovery_and_gate_change_prevent_late_request_publication(self):
        from service.config import quarantine
        gate = quarantine.RecoveryGate(home=lambda: Path(os.environ["HOME"]))
        with patch.object(quarantine, "_gate", gate):
            self.http = G.HistoryHTTPClient.synthetic(self.server)
            G.refresh_history_configuration()
            self.admit()
            async def recover(request):
                if request.url.path.endswith("/51"):
                    try:
                        gate.invalidate()
                    except quarantine.CredentialQuarantined:
                        pass
            self.hook = recover
            self.step()
            self.assertIsNone(self.bridge.snapshot())
            self.assertEqual(G.history_account_status(), ())
            asyncio.run(self.http.aclose())
        # A client bound to an older gate cannot dispatch after authority replacement.
        self.hook = None
        G.refresh_history_configuration()
        self.admit()
        before = len(self.calls)
        with patch.object(quarantine, "_gate", quarantine.RecoveryGate(home=lambda: Path(os.environ["HOME"]))):
            self.step()
        self.assertEqual(len(self.calls), before)
        self.assertIsNone(self.bridge.snapshot())

    def test_replaced_recovery_authority_invalidates_pure_getters_without_disk_or_network(self):
        from service.config import quarantine
        self.admit()
        self.step()
        receipt = self.bridge.snapshot().receipt
        new_gate = quarantine.RecoveryGate(home=lambda: Path(os.environ["HOME"]))
        with patch.object(quarantine, "_gate", new_gate), \
             patch.object(G, "load_config", side_effect=AssertionError("getter config read")), \
             patch.object(new_gate, "check", side_effect=AssertionError("getter recovery read")):
            self.assertIsNone(self.bridge.snapshot(expected_receipt=receipt))
            self.assertEqual(G.history_account_status(), ())

    def test_expiry_does_not_renew_via_public_bridge_or_metadata_roundtrip(self):
        self.admit()
        self.step()
        receipt = self.bridge.snapshot().receipt
        self.now, self.mono = 299, 99
        self.assertEqual(self.bridge.snapshot().receipt.expires_at, receipt.expires_at)
        self.now, self.mono = 300, 100
        self.assertIsNone(self.bridge.snapshot(expected_receipt=receipt))
        before = len(self.calls)
        self.step()
        self.assertEqual(len(self.calls), before)

    def test_source_bridge_does_not_accept_caller_token_or_arbitrary_http_client(self):
        self.admit()
        with self.assertRaises(TypeError):
            asyncio.run(self.bridge.step(http=self.http, budget=self.budget, token="declared-right-account"))
        with self.assertRaises(ValueError):
            asyncio.run(self.bridge.step(http=object(), budget=self.budget))
        with self.assertRaises(ValueError):
            asyncio.run(self.bridge.step(http=object.__new__(G.HistoryHTTPClient), budget=self.budget))
        self.assertEqual(self.calls, [])

    def test_pure_source_readback_does_not_wait_for_configuration_io_mutex(self):
        self.admit()
        self.step()
        rows = []
        def readback():
            rows.append(E.provider_history_snapshot(self.bridge))
        thread = threading.Thread(target=readback, daemon=True)
        with G._history_config_lock:
            thread.start()
            thread.join(timeout=1)
            completed = not thread.is_alive()
        thread.join(timeout=1)
        self.assertTrue(completed, "Getter acquired configuration mutex under history guard")
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(rows[0])

    def test_source_owned_clients_do_not_follow_redirects_or_retry_errors(self):
        self.admit()
        async def redirect(request):
            self.calls.append(request)
            return httpx.Response(302, headers={"Location": "https://example.test/leak"})
        client = G.HistoryHTTPClient.synthetic(redirect)
        asyncio.run(self.bridge.step(http=client, budget=self.budget))
        asyncio.run(client.aclose())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(str(self.calls[0].url), G.TOKEN_URL)
        self.assertIsNone(self.bridge.snapshot())

    def test_cancelled_source_step_withdraws_verified_token_and_existing_records(self):
        self.pages = [{"messages": [{"id": "51"}], "nextPageToken": "next"}]
        self.admit()
        self.step()
        self.assertEqual(self.bridge.status()["cached"], 1)
        async def cancelled(request):
            raise asyncio.CancelledError()
        self.hook = cancelled
        with self.assertRaises(asyncio.CancelledError):
            self.step()
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual(self.bridge.status()["state"], "cancelled")

    def test_two_configured_accounts_use_their_actual_credentials_and_never_deduplicate(self):
        self.config["accounts"][SCHOOL] = {"refresh_token": "school-refresh"}
        G.refresh_history_configuration()
        accounts = G.history_account_status()
        self.permission = G.HistoryPermissionState(tuple(row.account.account_id for row in accounts),
            (self.scope.digest,), "server-session", 1, "gmail_history", 1, 1, True, True, True)
        captured = []
        async def configured_server(request):
            from urllib.parse import parse_qs
            from tests.test_gmail_behavior_history import message
            captured.append(request)
            if str(request.url) == G.TOKEN_URL:
                refresh = parse_qs(request.content.decode())["refresh_token"][0]
                return httpx.Response(200, json={"access_token": refresh, "scope": G.SCOPE,
                                                 "token_type": "Bearer", "expires_in": 3600})
            if request.url.path.endswith("/profile"):
                owner = SCHOOL if request.headers["Authorization"] == "Bearer school-refresh" else ME
                return httpx.Response(200, json={"emailAddress": owner})
            return httpx.Response(200, json={"messages": [{"id": "51"}]} if request.url.path.endswith("/messages") else message())
        client = G.HistoryHTTPClient.synthetic(configured_server)
        bridges = []
        for account in accounts:
            source = G.new_public_history_bridge(application=self.app, limits=self.H.Limits(80000, 8000000, 32000000, 100),
                clock=lambda: 200, monotonic=lambda: 0)
            self.addCleanup(source.invalidate)
            source.start(self.app.issue(G.history_account_lease(account.account.account_id), self.scope))
            asyncio.run(source.step(http=client, budget=self.budget))
            bridges.append(source)
        asyncio.run(client.aclose())
        rows = [source.snapshot().records[0] for source in bridges]
        self.assertEqual([row.resource_id for row in rows], ["51", "51"])
        self.assertNotEqual(rows[0].account.account_id, rows[1].account.account_id)
        self.assertNotEqual(rows[0].ref, rows[1].ref)
        self.assertEqual(len(captured), 8)


class HistoryContinuation(unittest.TestCase):
    setUp = HistoryPublicBinding.setUp
    server = HistoryPublicBinding.server
    admit = HistoryPublicBinding.admit
    step = HistoryPublicBinding.step

    def scan(self, *, cap=12):
        self.scope = self.H.HistoryScope(100, 190, "inbox", cap)
        self.pages = [{"messages": [{"id": str(i)} for i in range(1, min(cap, 8) + 1)],
                       "nextPageToken": "private-page-cursor"},
                      {"messages": [{"id": str(i)} for i in range(9, cap + 1)]}]
        self.old_grant = self.admit()
        self.step()

    def due(self):
        self.now += 75
        self.mono += 75

    def fresh_grant(self, *, scope=None):
        lease = G.history_account_lease(self.old_grant.account_lease.account.account_id)
        return self.app.issue(lease, scope or self.scope)

    def test_live_two_segment_scan_reauthenticates_and_never_renews_old_proof(self):
        self.scan()
        old = self.bridge.snapshot()
        self.assertEqual([r.resource_id for r in old.records], ["1", "2", "3"])
        self.assertTrue(callable(getattr(self.bridge, "prepare_continuation", None)),
                        "The source needs a safe live traversal handoff, not a longer job TTL")
        self.due()
        handle = self.bridge.prepare_continuation()
        before = len(self.calls)
        status = self.bridge.continue_scan(handle, grant=self.fresh_grant())
        self.assertEqual(len(self.calls), before, "handoff must perform zero HTTP")
        self.assertEqual((status["listed"], status["fetched"], status["pending"]), (8, 3, 5))
        self.assertEqual(status["cached"], 0)
        self.assertEqual(status["continuation"]["segment"], 2)
        self.assertIsNone(self.bridge.snapshot(expected_receipt=old.receipt, required_refs=old.receipt.refs))
        self.assertEqual(old.records[0].expires_at, 300)
        self.assertEqual(old.records[0].read_started_at, 200)
        self.step()
        recent = self.bridge.snapshot()
        self.assertEqual([r.resource_id for r in recent.records], ["4", "5", "6", "7"])
        self.assertTrue(all(r.read_started_at == 275 and r.expires_at == 375 for r in recent.records))
        self.assertEqual([r.url.path.rsplit("/", 1)[1] for r in self.calls[before:]][:2], ["token", "profile"])
        self.step()
        self.step()
        self.assertEqual(self.bridge.status()["fetched"], 12)
        self.assertEqual(self.bridge.status()["listed"], 12)
        self.assertTrue(self.bridge.status()["eof_observed"])
        self.assertEqual([r.url.params.get("pageToken") for r in self.calls if r.url.path.endswith("/messages")],
                         [None, "private-page-cursor"])
        ids = [r.url.path.rsplit("/", 1)[1] for r in self.calls if "/messages/" in r.url.path]
        self.assertEqual(len(ids), len(set(ids)))

    def test_handoff_requires_both_clocks_early_window_and_actual_pending_work(self):
        self.scan()
        for wall, mono in ((200, 0), (275, 0), (200, 75), (274, 74)):
            with self.subTest(wall=wall, mono=mono):
                self.now, self.mono = wall, mono
                self.assertFalse(self.bridge.status()["continuation"]["available"])
                with self.assertRaises(PermissionError):
                    self.bridge.prepare_continuation()
        self.now, self.mono = 275, 75
        self.assertTrue(self.bridge.status()["continuation"]["available"])
        self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
        self.step(); self.step(); self.step()
        self.due()
        with self.assertRaises(PermissionError):
            self.bridge.prepare_continuation()

    def test_old_expiry_or_clock_rollback_cannot_restore_handle(self):
        for wall, mono in ((300, 75), (275, 100), (199, 75), (275, -1)):
            with self.subTest(wall=wall, mono=mono):
                self.now, self.mono = 200, 0
                self.scan(); self.due()
                handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
                self.now, self.mono = wall, mono
                before = len(self.calls)
                with self.assertRaises(PermissionError):
                    self.bridge.continue_scan(handle, grant=grant)
                self.assertIsNone(self.bridge.snapshot())
                self.assertIsNone(self.bridge.status()["binding"])
                self.assertEqual(before, len(self.calls))

    def test_single_use_handle_cannot_be_cloned_serialized_forged_or_replayed(self):
        import copy, pickle
        self.scan(); self.due()
        handle = self.bridge.prepare_continuation()
        self.assertIs(handle, self.bridge.prepare_continuation())
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, json.dumps):
            with self.subTest(operation=operation.__name__), self.assertRaises(TypeError):
                operation(handle)
        for fake in ({"segment": 1}, object(), object.__new__(G.PublicContinuation)):
            with self.subTest(fake=type(fake).__name__), self.assertRaises(PermissionError):
                self.bridge.continue_scan(fake, grant=self.fresh_grant())
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=self.old_grant)
        self.bridge.continue_scan(handle, grant=self.fresh_grant())
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=self.fresh_grant())
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 2)

    def test_same_scope_lease_application_and_original_start_are_required(self):
        from dataclasses import replace
        self.scan(); self.due()
        handle, good = self.bridge.prepare_continuation(), self.fresh_grant()
        for fake in (replace(good), replace(good, scope=self.H.HistoryScope(101, 190, "inbox", 12)),
                     replace(good, account_lease=replace(good.account_lease))):
            with self.subTest(fake=fake.scope.digest), self.assertRaises(PermissionError):
                self.bridge.continue_scan(handle, grant=fake)
        other_app = G.install_history_application(probe=lambda: self.permission)
        self.addCleanup(other_app.revoke)
        other_grant = other_app.issue(good.account_lease, self.scope)
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=other_grant)
        # Even an otherwise-current new explicit Start severs the old lineage.
        self.bridge.start(good)
        self.due()
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=self.fresh_grant())

    def test_grant_already_attached_to_another_bridge_is_not_fresh(self):
        self.scan(); self.due()
        handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
        other = G.new_public_history_bridge(application=self.app,
            limits=self.H.Limits(80000, 8000000, 32000000, 100),
            clock=lambda: self.now, monotonic=lambda: self.mono)
        self.addCleanup(other.invalidate)
        other.start(grant)
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=grant)
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 1)

    def test_permission_session_role_scope_and_consent_changes_withdraw_handle(self):
        from dataclasses import replace
        changes = ({"session_id": "new-session"}, {"session_epoch": 2}, {"role_epoch": 2},
                   {"role": "other"}, {"consent_epoch": 2}, {"global_learning": False},
                   {"email_consent": False}, {"history_start": False}, {"account_ids": ()},
                   {"scope_digests": ()})
        for change in changes:
            with self.subTest(change=change):
                self.now, self.mono = 200, 0
                self.scan(); self.due()
                handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
                self.permission = replace(self.permission, **change)
                with self.assertRaises(PermissionError):
                    self.bridge.continue_scan(handle, grant=grant)
                self.assertIsNone(self.bridge.snapshot())
                self.assertIsNone(self.bridge.status()["binding"])

    def test_pause_reset_forget_terminal_revoke_and_config_change_reject_handoff(self):
        for action in ("pause", "cancel", "forget", "revoke", "new_start", "config"):
            with self.subTest(action=action):
                # Reinstall only trusted test bootstrap after terminal revoke.
                if action == "config":
                    G.refresh_history_configuration()
                self.app = G.install_history_application(probe=lambda: self.permission)
                self.addCleanup(self.app.revoke)
                self.bridge = G.new_public_history_bridge(application=self.app,
                    limits=self.H.Limits(80000, 8000000, 32000000, 100),
                    clock=lambda: self.now, monotonic=lambda: self.mono)
                self.addCleanup(self.bridge.invalidate)
                self.now, self.mono = 200, 0
                self.scan(); self.due()
                handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
                if action == "revoke": self.app.revoke()
                elif action == "new_start": self.bridge.start(grant)
                elif action == "config":
                    self.config["client_secret"] = "rotated-test-only"
                    G.refresh_history_configuration()
                else: self.bridge.control(action)
                with self.assertRaises(PermissionError):
                    self.bridge.continue_scan(handle, grant=grant)
                if action == "revoke":
                    self.assertIsNone(self.app.refresh())
                    with self.assertRaises(PermissionError): self.fresh_grant()

    def test_public_status_checkpoint_and_handle_do_not_expose_traversal_or_tokens(self):
        self.scan(); self.due()
        handle = self.bridge.prepare_continuation()
        before = len(self.calls)
        with patch.object(G, "load_config", side_effect=AssertionError("getter config I/O")), \
             patch.object(G.httpx.AsyncClient, "request", side_effect=AssertionError("getter HTTP")):
            encoded = json.dumps([self.bridge.status(), self.bridge.checkpoint()]) + repr(handle)
            self.bridge.continue_scan(handle, grant=self.fresh_grant())
        for private in ("private-page-cursor", "server-issued", "configured-refresh", "fake-secret",
                        "listed_ids", "pending_ids", '"next_page"'):
            self.assertNotIn(private, encoded)
        self.assertEqual(before, len(self.calls))

    def test_simultaneous_consumption_creates_only_one_new_segment(self):
        import concurrent.futures, threading
        self.scan(); self.due()
        handle = self.bridge.prepare_continuation()
        grants = (self.fresh_grant(), self.fresh_grant())
        barrier = threading.Barrier(2)
        def consume(grant):
            barrier.wait(timeout=5)
            try:
                self.bridge.continue_scan(handle, grant=grant)
                return "started"
            except PermissionError:
                return "rejected"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, grants))
        self.assertCountEqual(results, ["started", "rejected"])
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 2)
        self.assertEqual(len(self.calls), 6)

    def test_transition_failure_withdraws_both_segments_and_consumes_handle(self):
        self.scan(); self.due()
        old = self.bridge.snapshot()
        handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
        with patch.object(self.bridge._provider, "_continue_segment", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                self.bridge.continue_scan(handle, grant=grant)
        self.assertIsNone(self.bridge.snapshot())
        self.assertIsNone(self.bridge.snapshot(expected_receipt=old.receipt))
        self.assertIsNone(self.bridge.status()["binding"])
        with self.assertRaises(PermissionError):
            self.bridge.continue_scan(handle, grant=grant)

    def test_total_unique_scope_cap_is_not_reset_by_handoff(self):
        self.scan(cap=6); self.due()
        self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
        self.step()
        status = self.bridge.status()
        self.assertEqual((status["listed"], status["fetched"], status["pending"]), (6, 6, 0))
        self.assertTrue(status["capped"])
        self.assertFalse(status["eof_observed"])
        before = len(self.calls)
        for _ in range(3): self.step()
        self.assertEqual(before, len(self.calls))

    def test_segment_limit_and_no_progress_stop_partial_without_extra_dispatch(self):
        self.budget = self.H.StepBudget(100, 1, 6, 400000, 30)
        self.scope = self.H.HistoryScope(100, 190, "inbox", 100)
        self.pages = [{"messages": [{"id": str(i)} for i in range(1, 101)],
                       "nextPageToken": "never-follow-after-cap"}]
        self.old_grant = self.admit(); self.step()
        for segment in range(2, 33):
            self.due()
            self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
            self.step()
            self.assertEqual(self.bridge.status()["continuation"]["segment"], segment)
        self.due()
        before = len(self.calls)
        with self.assertRaises(PermissionError): self.bridge.prepare_continuation()
        self.assertEqual(self.bridge.status()["state"], "partial")
        self.assertEqual(self.bridge.status()["fetched"], 32)
        self.step()
        self.assertEqual(before, len(self.calls))
        # A fresh explicit Start gets its own finite scope, never old authority.
        self.now, self.mono = 200, 0
        self.scan(); self.due()
        self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
        self.due()  # No new list/body progress in the second segment.
        before = len(self.calls)
        with self.assertRaises(PermissionError): self.bridge.prepare_continuation()
        self.assertEqual(self.bridge.status()["state"], "partial")
        self.step()
        self.assertEqual(before, len(self.calls))

    def test_handoff_is_refused_while_source_http_is_in_flight(self):
        self.scan(); self.due()
        handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
        async def hook(request):
            if "/messages/" in request.url.path:
                self.assertFalse(self.bridge.status()["continuation"]["available"])
                with self.assertRaises(PermissionError):
                    self.bridge.continue_scan(handle, grant=grant)
        self.hook = hook
        self.step()
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 1)

    def test_new_segment_withdrawal_at_each_http_boundary_cannot_publish(self):
        for phase in ("token", "profile", "body"):
            with self.subTest(phase=phase):
                self.now, self.mono = 200, 0
                self.hook = None
                self.scan(); self.due()
                self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
                async def hook(request):
                    tail = request.url.path.rsplit("/", 1)[1]
                    if tail == phase or (phase == "body" and "/messages/" in request.url.path):
                        self.bridge.control("forget")
                self.hook = hook
                before = len(self.calls)
                self.step()
                self.assertIsNone(self.bridge.snapshot())
                self.assertIsNone(self.bridge.status()["binding"])
                self.assertEqual(len(self.calls) - before, {"token": 1, "profile": 2, "body": 3}[phase])

    def test_new_segment_wrong_profile_token_error_and_cancellation_fail_closed(self):
        for failure in ("profile", "token", "cancel"):
            with self.subTest(failure=failure):
                self.now, self.mono = 200, 0
                self.hook = None; self.profile = ME; self.token_status = 200
                self.scan(); self.due()
                self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
                if failure == "profile": self.profile = "wrong@gmail.com"
                elif failure == "token": self.token_status = 500
                else:
                    async def cancel(request): raise asyncio.CancelledError()
                    self.hook = cancel
                if failure == "cancel":
                    with self.assertRaises(asyncio.CancelledError): self.step()
                else: self.step()
                self.assertIsNone(self.bridge.snapshot())
                self.assertIsNone(self.bridge.status()["binding"])

    def test_full_2000_scope_advances_across_segments_with_original_step_caps(self):
        self.scope = self.H.HistoryScope(100, 190, "inbox", 2000)
        self.bridge = G.new_public_history_bridge(application=self.app,
            limits=self.H.Limits(80000, 8000000, 32000000, 1200),
            clock=lambda: self.now, monotonic=lambda: self.mono)
        self.addCleanup(self.bridge.invalidate)
        self.pages = [{"messages": [{"id": str(i)} for i in range(first, first + 100)],
                       **({"nextPageToken": f"cursor-{first + 100}"} if first < 1901 else {})}
                      for first in range(1, 2001, 100)]
        self.old_grant = self.admit()
        handoffs = 0
        for _ in range(600):
            state = self.bridge.status()
            if state["fetched"] == 2000: break
            self.assertIn(state["state"], ("running", "capped"))
            if state["continuation"]["available"]:
                old = self.bridge.snapshot()
                self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
                handoffs += 1
                self.assertEqual(self.bridge.status()["cached"], 0)
                self.assertIsNone(self.bridge.snapshot(expected_receipt=old.receipt))
            before = len(self.calls)
            self.step()
            dispatched = self.calls[before:]
            self.assertLessEqual(len(dispatched), 6)
            self.assertLessEqual(sum("/messages/" in r.url.path for r in dispatched), 5)
            self.now += 30; self.mono += 30
        status = self.bridge.status()
        self.assertEqual((status["listed"], status["fetched"]), (2000, 2000))
        self.assertEqual(status["http_calls"], len(self.calls))
        self.assertTrue(status["eof_observed"])
        self.assertFalse(status["continuation"]["incomplete"])
        self.assertGreater(handoffs, 1)
        self.assertLess(handoffs, 32)
        self.assertEqual(len([r for r in self.calls if r.url.path.endswith("/profile")]), handoffs + 1)
        body_ids = [r.url.path.rsplit("/", 1)[1] for r in self.calls if "/messages/" in r.url.path]
        self.assertEqual(len(set(body_ids)), 2000)
        self.assertEqual(status["semantic_processing"], "not_performed")

    def test_recovery_latch_invalidates_prepared_handle_without_restoring_credentials(self):
        from service.config import quarantine
        gate = quarantine.RecoveryGate(home=lambda: Path(os.environ["HOME"]))
        with patch.object(quarantine, "_gate", gate):
            client = G.HistoryHTTPClient.synthetic(self.server)
            self.addCleanup(lambda: asyncio.run(client.aclose()))
            self.http = client
            G.refresh_history_configuration()
            self.scan(); self.due()
            handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
            try: gate.invalidate()
            except quarantine.CredentialQuarantined: pass
            with self.assertRaises(PermissionError): self.bridge.continue_scan(handle, grant=grant)
            self.assertIsNone(self.bridge.snapshot())
            self.assertEqual(G.history_account_status(), ())

    def test_late_body_publication_after_handoff_and_forget_is_withdrawn(self):
        self.scan(); self.due()
        self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
        publish = self.bridge._provider._publish
        def withdraw_then_publish(*args, **kwargs):
            self.bridge.control("forget")
            return publish(*args, **kwargs)
        with patch.object(self.bridge._provider, "_publish", side_effect=withdraw_then_publish):
            self.step()
        self.assertIsNone(self.bridge.snapshot())
        self.assertEqual(self.bridge.status()["cached"], 0)
        self.assertEqual(self.bridge.status()["state"], "forgotten")

    def test_expiry_during_atomic_metadata_copy_does_not_create_fresh_job(self):
        self.scan(); self.due()
        handle, grant = self.bridge.prepare_continuation(), self.fresh_grant()
        original = self.bridge._provider._continuation_status
        def late_status():
            status = original()
            self.now, self.mono = 300, 100
            return status
        with patch.object(self.bridge._provider, "_continuation_status", side_effect=late_status):
            with self.assertRaises((PermissionError, self.H._Withdrawn)):
                self.bridge.continue_scan(handle, grant=grant)
        self.assertIsNone(self.bridge.status()["binding"])
        self.assertIsNone(self.bridge.snapshot())

    def test_duplicate_page_or_empty_progress_cannot_extend_traversal_forever(self):
        self.scan(); self.step()  # Drain first page before the handoff window.
        self.due()
        self.pages = [{"messages": [], "nextPageToken": "private-page-cursor"}]
        self.bridge.continue_scan(self.bridge.prepare_continuation(), grant=self.fresh_grant())
        self.step()
        self.assertTrue(self.bridge.status()["error"])
        with self.assertRaises(PermissionError): self.bridge.prepare_continuation()
        self.now, self.mono = 200, 0
        self.pages = [{"messages": [], "nextPageToken": "no-progress"}]
        self.old_grant = self.admit(); self.step(); self.due()
        before = len(self.calls)
        with self.assertRaises(PermissionError): self.bridge.prepare_continuation()
        self.assertEqual(self.bridge.status()["state"], "partial")
        self.step()
        self.assertEqual(before, len(self.calls))

    def test_other_configured_account_grant_cannot_consume_this_account_cursor(self):
        self.config["accounts"][SCHOOL] = {"refresh_token": "other-refresh"}
        accounts = G.refresh_history_configuration()
        self.permission = G.HistoryPermissionState(tuple(row.account.account_id for row in accounts),
            (self.scope.digest,), "server-session", 1, "gmail_history", 1, 1, True, True, True)
        leases = {row.display_label: G.history_account_lease(row.account.account_id) for row in accounts}
        self.old_grant = self.app.issue(leases[ME], self.scope)
        self.pages = [{"messages": [{"id": str(i)} for i in range(1, 9)], "nextPageToken": "same-scope-page"}]
        self.bridge.start(self.old_grant); self.step(); self.due()
        handle = self.bridge.prepare_continuation()
        other = self.app.issue(leases[SCHOOL], self.scope)
        with self.assertRaises(PermissionError): self.bridge.continue_scan(handle, grant=other)
        self.assertEqual(self.bridge.status()["fetched"], 3)
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 1)
        self.bridge.continue_scan(handle, grant=self.app.issue(leases[ME], self.scope))
        self.assertEqual(self.bridge.status()["continuation"]["segment"], 2)

    def test_same_account_handle_cannot_be_used_by_a_different_bridge(self):
        self.scan(); self.due()
        handle = self.bridge.prepare_continuation()
        other = G.new_public_history_bridge(application=self.app,
            limits=self.H.Limits(80000, 8000000, 32000000, 100),
            clock=lambda: self.now, monotonic=lambda: self.mono)
        self.addCleanup(other.invalidate)
        other.start(self.fresh_grant())
        with self.assertRaises(PermissionError): other.continue_scan(handle, grant=self.fresh_grant())
        self.assertEqual(other.status()["fetched"], 0)
        self.assertEqual(self.bridge.status()["fetched"], 3)



JUDGE = "judge@gmail.com"


class GoogleConnectAndErase(unittest.TestCase):
    """Calendar consent, the post-connect pipeline, and Disconnect & erase."""

    def setUp(self):
        import types
        from service import google_calendar_source as C
        from service.assistant import mail_gists
        from service.assistant.store import AssistantStore
        self.C, self.gists = C, mail_gists
        root = Path(_scratch.name) / ("erase-" + self._testMethodName)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.enterContext(patch.object(cache_store, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.object(G, "CONFIG_PATH", root / "gmail.json"))
        self.enterContext(patch.multiple(E, _headers="", _history="", _raw_emails="", _swift_headers="",
            _swift_history="", _swift_raw="", _external_accounts={}, _headers_sync_generation=0,
            _email_available=None, _email_read_source="", _email_reason="", _email_sync_pending=False,
            _native_headers_at=0.0, _native_raw_at=0.0, _native_source="", _swift_identity=[], _identity_emails=[]))
        self.enterContext(patch.multiple(G, _last={}, _errors={}, _access={}, _epochs={}, _locks={},
                                        _meta={}, _pending_flow=None, _flow_generation=0,
                                        _authorization={"state": "idle", "generation": 0, "error": ""}))
        self.store = AssistantStore(root / "assistant.db")
        self.enterContext(patch.object(C, "_store_override", self.store))
        self.enterContext(patch.multiple(C, _status={}, _locks={}))
        from service import google_workspace
        self.W = google_workspace
        self.enterContext(patch.object(google_workspace, "CREDENTIALS_DIR", root / "workspace-creds"))
        self.enterContext(patch.object(mail_gists, "_PATH", root / "cache" / "mail_gists.json"))
        self.enterContext(patch.object(mail_gists, "_cache", None))
        self.erased = []
        proactive_store = types.ModuleType("service.proactive.store")
        proactive_store.erase_account = self.erased.append
        self.analyzed = []
        cycle = types.ModuleType("service.proactive.cycle")
        cycle.request_analyze = lambda account=None: self.analyzed.append(account)
        self.cycle = cycle
        package = types.ModuleType("service.proactive")
        package.__path__ = []
        self.enterContext(patch.dict(sys.modules, {"service.proactive": package,
                                                   "service.proactive.store": proactive_store,
                                                   "service.proactive.cycle": cycle}))
        both = [G.SCOPE, G.CALENDAR_SCOPE]
        G._save_config({"client_id": "synthetic", "client_secret": "s", "accounts": {
            ME: {"client_id": "synthetic", "refresh_token": "me-refresh", "scopes": both},
            JUDGE: {"client_id": "synthetic", "refresh_token": "judge-refresh", "scopes": both}}})
        E.configure_external_accounts(G.connected_accounts())

    # --- consent scopes -----------------------------------------------------

    def consent_query(self):
        from urllib.parse import parse_qs, urlparse
        from unittest.mock import MagicMock
        server = MagicMock()
        server.sockets = [MagicMock(getsockname=lambda: ("127.0.0.1", 50123))]
        async def run():
            with patch.object(G.asyncio, "start_server", AsyncMock(return_value=server)):
                return await G.begin_authorization("synthetic", timeout=0.05)
        url = asyncio.run(run())
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A50123", url)
        return parse_qs(urlparse(url).query)

    def test_consent_asks_for_gmail_and_calendar_read_only(self):
        query = self.consent_query()
        # A first sign-in (and any sign-in while no account has Workspace tools
        # switched on) asks for mail and calendar, read-only, and nothing else.
        self.assertEqual(query["scope"][0].split(), [G.SCOPE, G.CALENDAR_SCOPE])
        self.assertNotIn("login_hint", query)

    def test_workspace_scopes_are_asked_only_after_the_user_switches_them_on(self):
        self.assertEqual(G.status()["workspace_consent"], [])
        result = G.set_workspace(ME, True)
        # Google grants the scopes only at a sign-in, so the switch says to reconnect.
        self.assertTrue(result["ok"] and result["reconnect"])
        self.assertEqual(G.status()["workspace_consent"], [ME])
        query = self.consent_query()
        scopes = query["scope"][0].split()
        self.assertEqual(set(scopes), {G.SCOPE, G.CALENDAR_SCOPE, *self.W.WORKSPACE_SCOPES})
        self.assertTrue(all(scope.endswith(".readonly") for scope in scopes))
        self.assertEqual(query["login_hint"], [ME])
        # Once that sign-in granted them, the next one is narrow again.
        cfg = G.load_config()
        cfg["accounts"][ME]["scopes"] = [G.SCOPE, G.CALENDAR_SCOPE, *self.W.WORKSPACE_SCOPES]
        G._save_config(cfg)
        self.W.credentials_path(ME).unlink(missing_ok=True)
        self.assertEqual(G.status()["workspace_consent"], [])
        self.assertEqual(self.consent_query()["scope"][0].split(), [G.SCOPE, G.CALENDAR_SCOPE])
        self.assertEqual(G.set_workspace(ME, True), {"ok": True})

    def finish(self, granted):
        token = {"refresh_token": "new-refresh", "access_token": "fake", "scope": " ".join(granted)}
        async def run():
            with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json=token))), \
                 patch.object(httpx.AsyncClient, "get",
                              AsyncMock(return_value=httpx.Response(200, json={"emailAddress": "New@Gmail.com"}))), \
                 patch.object(G, "_after_connect", AsyncMock()) as after:
                email = await G._finish("synthetic", "s", "code", "verifier", "http://127.0.0.1", generation=0)
                await asyncio.sleep(0)
                return email, after
        return asyncio.run(run())

    def test_finish_records_the_scopes_google_granted(self):
        email, after = self.finish([G.SCOPE, G.CALENDAR_SCOPE, "openid"])
        self.assertEqual(email, "new@gmail.com")
        saved = G.load_config()["accounts"][email]
        self.assertIn(G.CALENDAR_SCOPE, saved["scopes"])
        self.assertTrue(G.has_calendar(email))
        after.assert_awaited_once_with(email)
        self.assertEqual(G._authorization["phase"], "reading")
        self.assertEqual(G._authorization["phase_email"], email)

    def test_new_sign_in_leaves_workspace_tools_off_and_erase_removes_them(self):
        email, _ = self.finish([G.SCOPE, G.CALENDAR_SCOPE])
        # Off for a new account: its refresh token is not copied for the server.
        self.assertFalse(G.connected_accounts()[email]["workspace"])
        self.assertFalse(self.W.credentials_path(email).exists())
        self.assertEqual(self.W.enabled_accounts(), [])
        # The user switches them on, then reconnects to grant the scopes.
        self.assertTrue(G.set_workspace(email, True)["reconnect"])
        self.finish([G.SCOPE, G.CALENDAR_SCOPE, *self.W.WORKSPACE_SCOPES])
        self.assertTrue(G.connected_accounts()[email]["workspace"])
        creds = json.loads(self.W.credentials_path(email).read_text())
        self.assertEqual(creds["refresh_token"], "new-refresh")
        row = next(r for r in G.status()["accounts"] if r["email"] == email)
        self.assertTrue(row["workspace"] and row["workspace_full"])
        # Switching off removes the server's sign-in; on restores it.
        self.assertTrue(G.set_workspace(email, False)["ok"])
        self.assertFalse(self.W.credentials_path(email).exists())
        self.assertEqual(self.W.enabled_accounts(), [])
        G.set_workspace(email, True)
        self.assertEqual(self.W.enabled_accounts(), [email])
        self.assertFalse(G.set_workspace("nobody@gmail.com", True)["ok"])
        with patch("httpx.AsyncClient.post", AsyncMock()):
            result = asyncio.run(G.disconnect(email))
        self.assertEqual(result["erased"][email]["workspace"], "erased")
        self.assertFalse(self.W.credentials_path(email).exists())

    def test_reconnect_keeps_the_workspace_choice(self):
        email, _ = self.finish([G.SCOPE])
        G.set_workspace(email, False)
        self.finish([G.SCOPE])
        self.assertFalse(G.connected_accounts()[email]["workspace"])
        self.assertFalse(self.W.credentials_path(email).exists())

    def test_unticked_calendar_still_connects_mail(self):
        email, _ = self.finish([G.SCOPE])
        self.assertIn(email, G.connected_accounts())
        self.assertFalse(G.has_calendar(email))

    def test_calendar_alone_without_gmail_is_refused(self):
        with self.assertRaises(G.GmailError):
            self.finish([G.CALENDAR_SCOPE])
        self.assertNotIn("new@gmail.com", G.connected_accounts())

    def test_existing_token_without_scope_record_keeps_mail_only(self):
        cfg = G.load_config()
        cfg["accounts"][ME].pop("scopes")
        G._save_config(cfg)
        self.assertIn(ME, G.connected_accounts())
        self.assertEqual(G.granted_scopes(ME), {G.SCOPE})
        self.assertFalse(G.has_calendar(ME))
        self.assertFalse(next(r for r in G.status()["accounts"] if r["email"] == ME)["calendar"])

    # --- after connect ------------------------------------------------------

    def run_after_connect(self, account=JUDGE):
        G._authorization.update(phase="reading", phase_email=account)
        async def run():
            with patch.object(G, "_sync_account", AsyncMock(return_value=True)) as sync, \
                 patch.object(self.C, "refresh_account", AsyncMock(return_value=2)) as calendar:
                await G._after_connect(account)
                for _ in range(3):
                    await asyncio.sleep(0)
                return sync, calendar
        return asyncio.run(run())

    def test_after_connect_reads_mail_and_calendar_then_requests_analyze(self):
        sync, calendar = self.run_after_connect()
        self.assertEqual([c.args for c in sync.await_args_list], [(JUDGE, ("headers",)), (JUDGE, ("raw",))])
        calendar.assert_awaited_once_with(JUDGE)
        self.assertEqual(self.analyzed, [JUDGE])
        self.assertEqual(G._authorization["phase"], "ready")
        self.assertEqual(G._authorization["analyze"], "requested")

    def test_after_connect_schedules_an_async_analyze(self):
        seen = []
        async def request_analyze(account=None):
            seen.append(account)
        self.cycle.request_analyze = request_analyze
        self.run_after_connect()
        self.assertEqual(seen, [JUDGE])

    def test_after_connect_without_the_engine_still_finishes(self):
        with patch.dict(sys.modules, {"service.proactive.cycle": None}):
            self.run_after_connect()
        self.assertEqual(G._authorization["phase"], "ready")
        self.assertEqual(G._authorization["analyze"], "unavailable")

    def test_after_connect_skips_calendar_when_not_granted(self):
        cfg = G.load_config()
        cfg["accounts"][JUDGE]["scopes"] = [G.SCOPE]
        G._save_config(cfg)
        _, calendar = self.run_after_connect()
        calendar.assert_not_awaited()
        self.assertEqual(self.analyzed, [JUDGE])

    def test_after_connect_resets_the_brief_for_that_account_before_analyzing(self):
        order = []
        sys.modules["service.proactive.store"].erase_account = lambda a: order.append(("reset", a))
        self.cycle.request_analyze = lambda account=None: order.append(("analyze", account))
        self.run_after_connect()
        self.assertEqual(order, [("reset", JUDGE), ("analyze", JUDGE)])
        self.assertNotIn(ME, [a for _, a in order])

    def test_after_connect_still_analyzes_when_the_brief_reset_fails(self):
        def boom(account):
            raise RuntimeError("store locked")
        sys.modules["service.proactive.store"].erase_account = boom
        self.run_after_connect()
        self.assertEqual(self.analyzed, [JUDGE])
        self.assertEqual(G._authorization["phase"], "ready")

    def test_raw_sync_reads_the_whole_week_plus_the_newest_without_duplicate_fetches(self):
        calls, fetched = [], []
        async def fake_get(http, path, params=()):
            params = list(params)
            if path == "messages":
                calls.append(params)
                if ("q", f"newer_than:{G.WEEK_RAW_DAYS}d") in params:
                    return {"messages": [{"id": i} for i in ("b", "c", "d")]}
                return {"messages": [{"id": i} for i in ("a", "b")]}
            fetched.append(path.split("/")[-1])
            return msg(path.split("/")[-1])
        cached = []
        with patch.object(G, "_get", fake_get), patch.object(G, "_still_connected", lambda: True), \
             patch.object(E, "cache_external_emails", lambda **kw: cached.append(kw)):
            asyncio.run(G._sync_raw(None, ME))
        self.assertTrue(any(("q", "newer_than:7d") in c for c in calls))
        self.assertTrue(any(not any(k == "q" for k, _ in c) for c in calls))
        self.assertEqual(sorted(fetched), ["a", "b", "c", "d"])
        self.assertEqual(len(cached), 1)

    def test_sent_sync_lists_only_the_sent_label_and_erase_drops_it(self):
        calls = []
        async def fake_get(http, path, params=()):
            params = list(params)
            if path == "messages":
                calls.append(params)
                return {"messages": [{"id": "s1"}]}
            return msg(path.split("/")[-1], "Re: Thesis draft")
        token = G._context.set((ME, {}, 0))
        try:
            with patch.object(G, "_get", fake_get), patch.object(G, "_still_connected", lambda: True):
                asyncio.run(G._sync_sent(None, ME))
        finally:
            G._context.reset(token)
        self.assertEqual([("labelIds", "SENT")], [p for p in calls[0] if p[0] == "labelIds"])
        self.assertIn(("q", f"newer_than:{G.SENT_DAYS}d"), calls[0])
        self.assertEqual([r["subject"] for r in G._sent[ME]], ["Re: Thesis draft"])
        with patch.object(G, "connected_accounts", lambda *a: {ME: {}}):
            self.assertEqual([r["account"] for r in G.sent_rows()], [ME])
        with patch.object(G, "connected_accounts", lambda *a: {}):
            self.assertEqual(G.sent_rows(), [], "a disconnected account's sent mail is never read")
        G.erase_account_data(ME)
        self.assertNotIn(ME, G._sent)

    def test_late_phase_never_overwrites_a_newer_sign_in(self):
        G._authorization.update(phase="reading", phase_email="someone@else.test")
        G._set_phase(JUDGE, "ready")
        self.assertEqual(G._authorization["phase"], "reading")

    # --- disconnect & erase -------------------------------------------------

    def seed(self):
        def mail(account, tag):
            message = msg(f"{tag}-1", f"{tag} subject")
            headers = G.header_line(G.parse_metadata(message), account, True)
            E.cache_external_emails(account=account, headers=headers, history=headers,
                                    raw=G.raw_record(message, account))
            G._meta[(account, f"{tag}-1")] = G.parse_metadata(message)
        mail(ME, "MINE")
        mail(JUDGE, "JUDGESECRET")
        now = time.time()
        for account, title in ((ME, "My standup"), (JUDGE, "JUDGESECRET dinner")):
            self.C.store_events(account, [{"source_id": f"gcal:{account}:e1", "title": title, "kind": "meeting",
                                           "when_ts": now + 3600, "end_ts": now + 7200, "invitee_count": 2,
                                           "organizer_is_user": False}])
        cache = self.gists._load()
        for kind in ("summary", "inbox", "texts"):
            cache[kind] = {"gist": f"{kind} gist JUDGESECRET", "at": now, "key": "k"}
        self.gists._save()

    def cache_text(self):
        root = self.root / "cache"
        return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file()
                         and p.name != "mail_gists.json")

    def test_disconnect_erases_every_cache_of_that_account(self):
        self.seed()
        self.assertIn("JUDGESECRET", E._headers)
        self.assertIn("JUDGESECRET", E._raw_emails)
        self.assertIn("JUDGESECRET", E._history)
        self.assertIn("JUDGESECRET", self.cache_text())
        self.assertIn(JUDGE, E.get_identity_emails())
        revoke = AsyncMock(return_value=httpx.Response(200))
        with patch.object(httpx.AsyncClient, "post", revoke):
            result = asyncio.run(G.disconnect(JUDGE))
        self.assertEqual(result["removed"], [JUDGE])
        self.assertEqual(result["erased"][JUDGE]["proactive"], "erased")
        self.assertEqual(revoke.await_args.kwargs["data"], {"token": "judge-refresh"})
        # Mail: in memory and on disk.
        for text in (E._headers, E._history, E._raw_emails, self.cache_text()):
            self.assertNotIn("JUDGESECRET", text)
            self.assertNotIn(JUDGE, text)
        self.assertNotIn(JUDGE, E.get_identity_emails())
        self.assertNotIn(JUDGE, E._external_accounts)
        self.assertFalse(any(key[0] == JUDGE for key in G._meta))
        self.assertIn("MINE subject", E._headers)          # the other account is kept
        # Config.
        self.assertNotIn(JUDGE, G.CONFIG_PATH.read_text())
        self.assertIn(ME, G.connected_accounts())
        # Calendar.
        with self.store.transaction(write=False) as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM commitments")]
        self.assertFalse(any(JUDGE in str(r.get("source_id")) or r.get("account") == JUDGE for r in rows))
        self.assertEqual([r["title"] for r in rows], ["My standup"])
        # Mail overviews written from the combined inbox; texts gist is not mail.
        on_disk = json.loads(self.gists._PATH.read_text())
        self.assertEqual(set(on_disk), {"texts"})
        # Proactive cards/to-dos/threads.
        self.assertEqual(self.erased, [JUDGE])

    def test_erasing_the_last_account_keeps_the_oauth_client_for_the_next_connect(self):
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
            asyncio.run(G.disconnect(JUDGE))
            asyncio.run(G.disconnect(ME))
        cfg = G.load_config()
        self.assertEqual(cfg["client_id"], "synthetic")
        self.assertEqual(cfg["accounts"], {})
        self.assertFalse(G.is_connected())
        self.assertNotIn("refresh", G.CONFIG_PATH.read_text())
        self.assertNotIn(JUDGE, G.CONFIG_PATH.read_text())

    def test_erase_survives_a_restart_of_the_mail_cache(self):
        self.seed()
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
            asyncio.run(G.disconnect(JUDGE))
        for name in ("email_headers", "email_history", "email_raw", "email_sources", "identity_emails",
                     "email_mail_headers", "email_mail_history", "email_mail_raw"):
            self.assertNotIn("JUDGESECRET", cache_store.load(name), name)
            self.assertNotIn(JUDGE, cache_store.load(name), name)

    def test_erase_reports_a_missing_engine_without_failing(self):
        self.seed()
        with patch.dict(sys.modules, {"service.proactive.store": None}), \
             patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
            result = asyncio.run(G.disconnect(JUDGE))
        erased = result["erased"][JUDGE]
        self.assertEqual(erased["proactive"], "unavailable")
        self.assertEqual(erased["mail"], "erased")
        self.assertTrue(erased["calendar"].startswith("erased 1"))

    def test_revoke_failure_still_erases_locally(self):
        self.seed()
        with patch.object(httpx.AsyncClient, "post", AsyncMock(side_effect=httpx.ConnectError("offline"))):
            asyncio.run(G.disconnect(JUDGE))
        self.assertNotIn("JUDGESECRET", E._headers)
        self.assertNotIn(JUDGE, G.connected_accounts())
        self.assertEqual(self.erased, [JUDGE])

    def test_reconnect_without_calendar_erases_the_old_events(self):
        self.C.store_events("new@gmail.com", [{"source_id": "gcal:new@gmail.com:e1", "title": "Old",
                                               "when_ts": time.time() + 3600}])
        self.finish([G.SCOPE])
        with self.store.transaction(write=False) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0], 0)

    def test_disconnect_result_says_whether_everything_was_erased(self):
        self.seed()
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
            result = asyncio.run(G.disconnect(JUDGE))
        self.assertTrue(result["complete"])
        self.seed()
        with patch.dict(sys.modules, {"service.proactive.store": None}), \
             patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
            result = asyncio.run(G.disconnect(ME))
        self.assertFalse(result["complete"])
        self.assertEqual(result["erased"][ME]["proactive"], "unavailable")

    def test_disconnect_route_returns_the_erase_result(self):
        from starlette.requests import Request
        with patch.dict(sys.modules):
            for name in ("service.proactive", "service.proactive.store", "service.proactive.cycle"):
                sys.modules.pop(name, None)     # the real engine, not this class's stand-ins
            import service.main as M
        scope = {"type": "http", "method": "POST", "path": "/assistant/gmail/disconnect",
                 "headers": [(b"content-type", b"application/json")]}
        fake = AsyncMock(return_value={"removed": [JUDGE], "complete": False,
                                       "erased": {JUDGE: {"mail": "erased", "proactive": "failed: X"}}})
        with patch.object(G, "disconnect", fake):
            body = asyncio.run(M.gmail_disconnect(Request(scope), {"account": JUDGE}))
        self.assertEqual(body["ok"], False)
        self.assertTrue(body["partial"])
        self.assertEqual(body["erased"][JUDGE]["proactive"], "failed: X")
        self.assertEqual(body["removed"], [JUDGE])

    def test_gist_rewrite_in_flight_during_erase_is_dropped(self):
        self.seed()
        gists = self.gists
        async def synth(system, material):
            G.erase_account_data(JUDGE)          # erase lands while the model is writing
            return "JUDGESECRET is the summary of your inbox today and more words here."
        job = {"kind": "summary", "key": "k2", "lines": ["x"], "rules": "r", "paragraph": True,
               "ok": lambda text: True}
        with patch.object(gists, "_jobs", lambda now: [job]):
            wrote = asyncio.run(gists.refresh(synth=synth, abort=lambda: False))
        self.assertFalse(wrote)
        self.assertNotIn("summary", gists._load())
        self.assertNotIn("summary", json.loads(gists._PATH.read_text()))

    def test_status_reports_calendar_per_account(self):
        rows = {r["email"]: r for r in G.status()["accounts"]}
        self.assertTrue(rows[JUDGE]["calendar"])
        self.assertIn("calendar_error", rows[JUDGE])



# Synthetic stand-ins only: the real client lives in a CI secret, never here.
FAKE_BUILTIN = {"client_id": "builtin-fake.apps.googleusercontent.com", "client_secret": "FAKE-builtin-secret"}


class BuiltInGoogleClient(unittest.TestCase):
    """The release build's built-in OAuth client and its Testing-mode realities.
    Same synthetic state as GoogleConnectAndErase, without re-running its tests."""

    def setUp(self):
        GoogleConnectAndErase.setUp(self)
        self.builtin_path = self.root / "google_oauth_client.json"
        self.enterContext(patch.object(G, "BUILTIN_CLIENT_PATH", self.builtin_path))
        self.enterContext(patch.object(self.W, "saved_credentials", return_value=("", "")))

    def ship_builtin(self):
        self.builtin_path.write_text(json.dumps(FAKE_BUILTIN))

    def clear_user_client(self):
        cfg = G.load_config()
        cfg.pop("client_id", None)
        cfg.pop("client_secret", None)
        G._save_config(cfg)

    def route(self, *names):
        import ast
        from typing import Any
        from fastapi import FastAPI
        source = Path(__file__).resolve().parents[1] / "service/main.py"
        nodes = [n for n in ast.parse(source.read_text()).body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        app = FastAPI()
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), {"app": app, "Any": Any})
        return app

    def post(self, app, path, body):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://synthetic.invalid") as client:
                return (await client.post(path, json=body)).json()
        return asyncio.run(run())

    # --- precedence and source ---------------------------------------------

    def test_user_client_wins_over_the_built_in_one(self):
        self.ship_builtin()
        self.assertEqual(G.resolve_client(), ("synthetic", "s", "user"))
        self.assertEqual(G.resolve_client(client_id="typed", client_secret="t"), ("typed", "t", "user"))
        self.clear_user_client()
        self.assertEqual(G.resolve_client(), (FAKE_BUILTIN["client_id"], FAKE_BUILTIN["client_secret"], "builtin"))
        # The Workspace setup's client is the user's own too.
        with patch.object(self.W, "saved_credentials", return_value=("ws-client", "ws-secret")):
            self.assertEqual(G.resolve_client(), ("ws-client", "ws-secret", "user"))
        # ...unless it was set up with the built-in client.
        with patch.object(self.W, "saved_credentials",
                          return_value=(FAKE_BUILTIN["client_id"], FAKE_BUILTIN["client_secret"])):
            self.assertEqual(G.resolve_client()[2], "builtin")

    def test_source_is_none_without_any_client(self):
        self.clear_user_client()
        self.assertEqual(G.builtin_client(), ("", ""))
        self.assertEqual(G.resolve_client(), ("", "", "none"))
        self.assertEqual(G.status()["client_source"], "none")
        for broken in ("not json", "[]", json.dumps({"client_id": "only-id"})):
            self.builtin_path.write_text(broken)
            self.assertEqual(G.resolve_client()[2], "none")

    def test_status_reports_the_source_and_never_the_built_in_secret(self):
        self.ship_builtin()
        info = G.status()
        self.assertEqual(info["client_source"], "user")
        self.assertEqual(info["consent_notice"], "")
        self.clear_user_client()
        info = G.status()
        self.assertEqual(info["client_source"], "builtin")
        self.assertTrue(info["builtin_client"])
        self.assertEqual(info["client_id"], "")          # only the user's own id is ever shown
        self.assertFalse(info["has_secret"])
        self.assertEqual(info["consent_notice"], "Google will warn that Wisp isn't verified yet. Choose Continue.")
        text = json.dumps(info)
        self.assertNotIn(FAKE_BUILTIN["client_secret"], text)
        self.assertNotIn(FAKE_BUILTIN["client_id"], text)
        # The Workspace status hides it the same way.
        with patch.object(self.W, "_entry", return_value={"env": {
                "GOOGLE_OAUTH_CLIENT_ID": FAKE_BUILTIN["client_id"],
                "GOOGLE_OAUTH_CLIENT_SECRET": FAKE_BUILTIN["client_secret"]}}):
            ws = self.W.status()
        self.assertEqual((ws["client_id"], ws["client_source"]), ("", "builtin"))
        self.assertNotIn(FAKE_BUILTIN["client_secret"], json.dumps(ws))

    # --- connect with only the built-in client ------------------------------

    def test_connect_route_uses_the_built_in_client_when_nothing_else_is_set(self):
        from urllib.parse import parse_qs, urlparse
        app = self.route("gmail_connect")
        self.clear_user_client()
        start, callback = GmailSource.fake_oauth_server()
        with patch("asyncio.start_server", start):
            empty = self.post(app, "/assistant/gmail/connect", {})
        self.assertEqual(empty, {"ok": False, "error": "client_id is required"})
        self.ship_builtin()
        start, callback = GmailSource.fake_oauth_server()
        finish = AsyncMock(return_value="new@gmail.com")
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://synthetic.invalid") as client:
                r = (await client.post("/assistant/gmail/connect", json={})).json()
            state = parse_qs(urlparse(r["auth_url"]).query)["state"][0]
            await callback("state=" + state + "&code=fake")
            return r
        with patch("asyncio.start_server", start), patch.object(G, "_finish", finish):
            r = asyncio.run(run())
        self.assertTrue(r["ok"])
        self.assertEqual(r["client_source"], "builtin")
        self.assertIn("isn't verified yet", r["consent_notice"])
        self.assertEqual(parse_qs(urlparse(r["auth_url"]).query)["client_id"], [FAKE_BUILTIN["client_id"]])
        self.assertNotIn(FAKE_BUILTIN["client_secret"], json.dumps(r))
        args, kwargs = finish.await_args
        self.assertEqual(args[:2], (FAKE_BUILTIN["client_id"], FAKE_BUILTIN["client_secret"]))
        self.assertEqual(kwargs["client_source"], "builtin")

    def test_built_in_sign_in_is_not_saved_as_the_users_own_client(self):
        self.ship_builtin()
        self.clear_user_client()
        token = {"refresh_token": "new-refresh", "access_token": "fake", "scope": f"{G.SCOPE} {G.CALENDAR_SCOPE}"}
        async def run():
            with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json=token))), \
                 patch.object(httpx.AsyncClient, "get",
                              AsyncMock(return_value=httpx.Response(200, json={"emailAddress": "tester@gmail.com"}))), \
                 patch.object(G, "_after_connect", AsyncMock()):
                return await G._finish(FAKE_BUILTIN["client_id"], FAKE_BUILTIN["client_secret"], "code", "v",
                                       "http://127.0.0.1", generation=0, client_source="builtin")
        email = asyncio.run(run())
        cfg = G.load_config()
        self.assertEqual(cfg.get("client_id", ""), "")
        self.assertEqual(cfg["accounts"][email]["client_source"], "builtin")
        self.assertIn(email, G.connected_accounts())
        info = G.status()
        self.assertEqual(info["client_source"], "builtin")
        row = next(r for r in info["accounts"] if r["email"] == email)
        self.assertEqual(row["client_source"], "builtin")
        self.assertNotIn(FAKE_BUILTIN["client_secret"], json.dumps(info))
        self.assertEqual(G.reconnect_client(email), (FAKE_BUILTIN["client_id"], FAKE_BUILTIN["client_secret"], "builtin"))

    # --- Testing-mode errors -------------------------------------------------

    def test_access_denied_callback_says_the_account_is_not_a_tester(self):
        start, callback = GmailSource.fake_oauth_server()
        async def run():
            with patch("asyncio.start_server", start):
                receipt = await G.begin_authorization("synthetic", include_receipt=True, timeout=5)
                from urllib.parse import parse_qs, urlparse
                state = parse_qs(urlparse(receipt["auth_url"]).query)["state"][0]
                writer = await callback("error=access_denied&state=" + state)
                return writer
        writer = asyncio.run(run())
        expected = "This Google account isn't on Wisp's tester list yet. Ask whoever invited you to add it."
        self.assertEqual(G._authorization["state"], "failed")
        self.assertEqual(G._authorization["error"], expected)
        self.assertIn(b"tester list", writer.write.call_args[0][0])

    def test_other_callback_errors_keep_the_declined_message(self):
        start, callback = GmailSource.fake_oauth_server()
        async def run():
            with patch("asyncio.start_server", start):
                receipt = await G.begin_authorization("synthetic", include_receipt=True, timeout=5)
                from urllib.parse import parse_qs, urlparse
                state = parse_qs(urlparse(receipt["auth_url"]).query)["state"][0]
                await callback("error=server_error&state=" + state)
        asyncio.run(run())
        self.assertIn("declined or cancelled", G._authorization["error"])

    def test_reconnect_passes_the_account_as_login_hint(self):
        from urllib.parse import parse_qs, urlparse
        app = self.route("gmail_connect")
        start, _ = GmailSource.fake_oauth_server()
        with patch("asyncio.start_server", start):
            r = self.post(app, "/assistant/gmail/connect", {"reconnect": JUDGE.upper()})
            missing = self.post(app, "/assistant/gmail/connect", {"reconnect": "nobody@gmail.com"})
        self.assertTrue(r["ok"])
        query = parse_qs(urlparse(r["auth_url"]).query)
        self.assertEqual(query["login_hint"], [JUDGE])
        self.assertEqual(query["client_id"], ["synthetic"])
        self.assertEqual(r["client_source"], "user")
        self.assertFalse(missing["ok"])

    def expire(self, account=ME, responses=1):
        announced = []
        cfg = G.connected_accounts()[account]
        async def run():
            with patch.object(httpx.AsyncClient, "post",
                              AsyncMock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))), \
                 patch.object(G, "_announce_expired", lambda email, stamp: announced.append((email, stamp))):
                for _ in range(responses):
                    with G.bound_account(account):
                        async with httpx.AsyncClient() as http:
                            with self.assertRaises(G.GmailError) as caught:
                                await G.access_token(http)
                    self.assertIn("expired", str(caught.exception))
        asyncio.run(run())
        return announced, cfg

    def test_invalid_grant_marks_the_account_expired_and_announces_once(self):
        announced, _ = self.expire(ME, responses=3)
        self.assertEqual([email for email, _ in announced], [ME])     # one notice per expiry
        row = next(r for r in G.status()["accounts"] if r["email"] == ME)
        self.assertEqual(row["state"], "expired")
        self.assertTrue(row["expired"])
        self.assertFalse(row["fresh"])
        self.assertEqual(row["last_error"], "Google sign-in expired. Reconnect in Settings ▸ Google.")
        other = next(r for r in G.status()["accounts"] if r["email"] == JUDGE)
        self.assertEqual(other["state"], "ok")
        self.assertNotIn("me-refresh", json.dumps(G.status()))

    def test_reconnect_clears_the_expired_state(self):
        self.expire(ME)
        token = {"refresh_token": "fresh-refresh", "access_token": "fake", "scope": G.SCOPE}
        async def run():
            with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json=token))), \
                 patch.object(httpx.AsyncClient, "get",
                              AsyncMock(return_value=httpx.Response(200, json={"emailAddress": ME}))), \
                 patch.object(G, "_after_connect", AsyncMock()):
                return await G._finish("synthetic", "s", "code", "v", "http://127.0.0.1", generation=0)
        asyncio.run(run())
        row = next(r for r in G.status()["accounts"] if r["email"] == ME)
        self.assertFalse(row["expired"])
        self.assertNotEqual(row["state"], "expired")
        # A later expiry of the new sign-in is announced again.
        announced, _ = self.expire(ME)
        self.assertEqual(len(announced), 1)

    def test_expiry_of_a_replaced_sign_in_is_not_recorded(self):
        snapshot = dict(G.connected_accounts()[ME])
        cfg = G.load_config()
        cfg["accounts"][ME]["refresh_token"] = "newer-refresh"
        G._save_config(cfg)
        with patch.object(G, "_announce_expired") as announce:
            G._mark_expired(ME, snapshot)
        announce.assert_not_called()
        self.assertNotIn("auth_expired", G.load_config()["accounts"][ME])

    def test_expiry_event_is_durable_and_deduplicated(self):
        published = []
        import importlib
        hub_module = importlib.import_module("service.assistant.hub")
        class FakeHub:
            def publish_sync(self, event, **kwargs):
                published.append((event, kwargs))
        with patch.object(hub_module, "hub", FakeHub()):
            G._announce_expired(ME, 1700000000)
        event, kwargs = published[0]
        self.assertEqual(event["type"], "google_signin_expired")
        self.assertEqual(event["account"], ME)
        self.assertEqual(kwargs["dedupe_key"], f"google-signin-expired:{ME}:1700000000")
        self.assertEqual(event["title"] + ". " + event["body"],
                         "Google sign-in expired. Reconnect in Settings ▸ Google.")


if __name__ == "__main__":
    unittest.main()
