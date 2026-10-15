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
        from test_gmail_behavior_history import FakeHTTP, message, token
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
        from test_gmail_behavior_history import FakeHTTP, token
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
        from test_gmail_behavior_history import FakeHTTP, token
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
        from test_gmail_behavior_history import message
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
            from test_gmail_behavior_history import message
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


if __name__ == "__main__":
    unittest.main()
