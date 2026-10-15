"""Provider-backed email reliability. All accounts, HTTP and caches are synthetic."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
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
                                        _meta={}, _pending_flow=None, _flow_generation=0))
        self.accounts = {ME: {"client_id": "synthetic", "refresh_token": "fake-refresh"}}
        self.enterContext(patch.object(G, "connected_accounts", lambda cfg=None: dict(self.accounts)))
        E.configure_external_accounts(self.accounts)

    def publish(self, account=ME, **parts):
        E.cache_external_emails(account=account, **parts)

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
        self.assertEqual(G._epochs[ME], 1)

    def test_status_never_exposes_credentials_and_marks_restored_data_unverified(self):
        self.publish(headers=header())
        E._external_accounts[ME]["verified"] = False
        info = G.status()
        self.assertFalse(info["accounts"][0]["fresh"])
        self.assertIsNotNone(info["accounts"][0]["last_headers_sync"])
        self.assertNotIn("fake-refresh", json.dumps(info))

    def test_successful_empty_native_cache_is_preserved_on_restart(self):
        E.cache_emails("")
        self.assertEqual(E._restored_native("headers", header(subject="Obsolete")), "")

    def test_unrelated_oauth_callback_cannot_finish_or_cancel_active_flow(self):
        from urllib.parse import urlparse, parse_qs
        async def run():
            url = await G.begin_authorization("synthetic", timeout=1)
            args = parse_qs(urlparse(url).query)
            redirect = urlparse(args["redirect_uri"][0])
            async def callback(query):
                reader, writer = await asyncio.open_connection(redirect.hostname, redirect.port)
                writer.write(f"GET /?{query} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                await writer.drain()
                await reader.read()
                writer.close()
                await writer.wait_closed()
            with patch.object(G, "_finish", AsyncMock()) as finish:
                await callback("state=incorrect&code=fake")
                self.assertIsNotNone(G._pending_flow)
                finish.assert_not_awaited()
                await callback("state=" + args["state"][0] + "&error=access_denied")
                await asyncio.sleep(0.01)
                finish.assert_not_awaited()
                self.assertIsNone(G._pending_flow)
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
                    {"connected": True, "authorizing": False, "accounts": [{"email": ME}, {"email": SCHOOL}]}]
        output = io.StringIO()
        with patch.object(sys, "argv", ["gmail_connect.py", "synthetic"]), \
             patch.object(G, "begin_authorization", AsyncMock(return_value="https://accounts.google.com/synthetic")), \
             patch.object(G, "status", side_effect=statuses), patch("webbrowser.open"), \
             patch("asyncio.sleep", AsyncMock()), redirect_stdout(output):
            asyncio.run(module["main"]())
        self.assertIn(SCHOOL, output.getvalue())

    _original_accounts = staticmethod(G.connected_accounts)


if __name__ == "__main__":
    unittest.main()
