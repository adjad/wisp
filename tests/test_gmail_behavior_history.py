"""Disabled Gmail history provider contract; synthetic accounts/HTTP/clock only."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import gc
import json
import threading
import unittest
from unittest.mock import patch

import httpx

from service import gmail_behavior_history as H


def message(mid="51", text="older than the recent fifty", stamp=150):
    raw = text.encode()
    return {"id": mid, "internalDate": str(stamp * 1000), "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "body": {
                "data": base64.urlsafe_b64encode(raw).decode(), "size": len(raw)},
                "headers": [{"name": "Message-ID", "value": "<shared@example.test>"}]}}


class FakeHTTP:
    def __init__(self, pages=None, messages=None, hook=None):
        self.pages = list(pages if pages is not None else [{"messages": [{"id": "51"}]}])
        self.messages = messages or {"51": message()}
        self.calls = []
        self.hook = hook

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.hook:
            await self.hook(method, url)
        if url.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fake"})
        data = self.pages.pop(0) if url.endswith("/messages") else self.messages[url.rsplit("/", 1)[1]]
        if isinstance(data, Exception):
            raise data
        return httpx.Response(200, json=data)


async def token(http):
    return H.HistoryAccessToken(http.admission, "fake-token")


class ProviderContract(unittest.TestCase):
    def setUp(self):
        self.now = 200.0
        self.mono = 0.0
        self.scope = H.HistoryScope(start=100, end=190, selection="inbox", message_cap=100)
        self.account = H.AccountIdentity("gmail", "configured-account-1", "connection-1", "set-1")
        self.admission = H.Admission(self.account, 1, 1, True, True, True, self.scope.digest)
        self.limits = H.Limits(body_bytes=80000, account_cache_bytes=8000000,
                               global_cache_bytes=32000000, ttl_seconds=100)
        self.budget = H.StepBudget(page_size=100, full_bodies=5, http_calls=6,
                                   accepted_body_bytes=400000, dispatch_seconds=30)
        self.provider = self.make_provider()
        self.http = FakeHTTP()

    def make_provider(self, **kwargs):
        provider = H.HistoryProvider(admission=kwargs.get("admission", lambda: self.admission), limits=kwargs.get("limits", self.limits),
                                     clock=lambda: self.now, monotonic=lambda: self.mono)
        self.addCleanup(provider.invalidate)
        return provider

    def start(self):
        self.provider.control("start", scope=self.scope)

    def step(self, **kwargs):
        return asyncio.run(self.provider.step(http=kwargs.get("http", self.http),
                                              token=kwargs.get("token", token),
                                              budget=kwargs.get("budget", self.budget)))

    def test_default_off_cannot_fetch_from_payload_or_getter(self):
        self.assertIsNone(self.provider.snapshot())
        self.assertEqual(self.provider.status()["state"], "off")
        self.step()
        self.assertEqual(self.http.calls, [])
        self.assertNotIn("body", json.dumps(self.provider.checkpoint()))

    def test_public_source_guard_serializes_withdrawal_before_cached_readback(self):
        self.start()
        self.step()
        started = threading.Event()
        finished = threading.Event()
        read = []
        def capture():
            started.set()
            read.append(self.provider.snapshot())
            finished.set()
        thread = threading.Thread(target=capture, daemon=True)
        with H.history_source_guard():
            thread.start()
            self.assertTrue(started.wait(1))
            was_blocked = not finished.wait(0.03)
            self.provider.control("forget")
        thread.join(timeout=1)
        self.assertTrue(was_blocked, "Source guard did not serialize the core readback")
        self.assertFalse(thread.is_alive())
        self.assertEqual(read, [None])

    def test_explicit_history_fetch_publishes_resource_identity_and_exact_body(self):
        self.start()
        self.step()
        snapshot = self.provider.snapshot()
        self.assertIsNotNone(snapshot)
        row = snapshot.records[0]
        self.assertEqual(row.resource_id, "51")
        self.assertEqual(row.rfc_message_id, "<shared@example.test>")
        self.assertEqual(row.body.text, "older than the recent fifty")
        self.assertEqual(row.body.status, "complete")
        self.assertEqual(row.provider_internal_at, 150)
        self.assertIsNone(row.event_at)
        self.assertEqual(row.event_origin, "unknown")
        self.assertFalse(row.time_qualification.relative_deadlines_supported)
        self.assertEqual(row.body.end_offset, len(row.body.text))
        self.assertEqual(len(self.http.calls), 2)

    def test_revocation_at_body_await_prevents_late_publication(self):
        async def revoke(method, url):
            if not url.endswith("/messages"):
                self.admission = dataclasses.replace(self.admission, email_consent=False, consent_epoch=2)
        self.http.hook = revoke
        self.start()
        self.step()
        self.assertIsNone(self.provider.snapshot())
        self.assertEqual(self.provider.status()["cached"], 0)

    def test_no_consent_or_payload_can_grant_admission(self):
        for field in ("explicit_start", "global_learning", "email_consent"):
            with self.subTest(field=field):
                self.admission = dataclasses.replace(self.admission, **{field: False})
                with self.assertRaises(PermissionError):
                    self.start()
                self.step()
                self.assertEqual(self.http.calls, [])
                self.admission = dataclasses.replace(self.admission, **{field: True})
        self.admission = dataclasses.replace(self.admission, scope_digest="payload-only")
        with self.assertRaises(PermissionError):
            self.start()
        with self.assertRaises(ValueError):
            H.HistoryProvider(admission={"email_consent": True}, limits=self.limits)

    def test_callback_failure_and_non_boolean_consent_fail_closed(self):
        for value in (None, {}, dataclasses.replace(self.admission, email_consent=1)):
            provider = H.HistoryProvider(admission=lambda: value, limits=self.limits)
            with self.assertRaises(PermissionError):
                provider.control("start", scope=self.scope)
        provider = H.HistoryProvider(admission=lambda: 1 / 0, limits=self.limits)
        with self.assertRaises(PermissionError):
            provider.control("start", scope=self.scope)

    def test_limits_validate_all_fields_with_nan_bool_zero_and_upper_envelopes(self):
        for cls, valid in ((H.Limits, self.limits), (H.StepBudget, self.budget)):
            for field in dataclasses.fields(valid):
                for invalid in (0, -1, float("nan"), float("inf"), True, "1", 10**10):
                    with self.subTest(cls=cls.__name__, field=field.name, invalid=invalid):
                        with self.assertRaises(ValueError):
                            dataclasses.replace(valid, **{field.name: invalid})
        for updates in ({"end": 100}, {"end": 100 + 731 * 86400},
                        {"selection": "all-mail-with-attachments"}, {"message_cap": 2001}):
            with self.assertRaises(ValueError):
                dataclasses.replace(self.scope, **updates)

    def test_offsets_and_carriers_are_immutable_and_receipt_cannot_be_forged(self):
        self.start()
        self.step()
        snapshot = self.provider.snapshot()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.records[0].body.text = "replacement"
        forged = dataclasses.replace(snapshot.receipt, expires_at=9999, proof_digest="payload proof")
        self.assertIsNone(self.provider.snapshot(expected_receipt=forged))
        wrong = dataclasses.replace(snapshot.records[0].ref, resource_id="forged")
        self.assertIsNone(self.provider.snapshot(required_refs=(wrong,)))

    def test_public_receipt_and_ref_json_roundtrip_are_body_free_current_recheck_inputs(self):
        self.start()
        self.step()
        snapshot = self.provider.snapshot()
        receipt_json = json.loads(json.dumps(snapshot.receipt.to_metadata()))
        ref_json = json.loads(json.dumps(snapshot.records[0].ref.to_metadata()))
        self.assertNotIn("older than", json.dumps(receipt_json))
        self.assertNotIn("shared@example.test", json.dumps(receipt_json))
        receipt = H.HistoryReceipt.from_metadata(receipt_json)
        ref = H.RecordRef.from_metadata(ref_json)
        self.assertEqual(receipt, snapshot.receipt)
        self.assertEqual(ref, snapshot.records[0].ref)
        calls = len(self.http.calls)
        self.assertTrue(self.provider.snapshot(expected_receipt=receipt, required_refs=(ref,)).complete)
        self.assertEqual(len(self.http.calls), calls)
        restarted = self.make_provider()
        restarted.control("start", scope=self.scope)
        self.assertIsNone(restarted.snapshot(expected_receipt=receipt, required_refs=(ref,)))

    def test_time_qualification_never_upgrades_provider_ordering_to_utterance_or_deadline(self):
        self.http = FakeHTTP(messages={"51": message(text="Reply tomorrow")})
        self.start()
        self.step()
        row = self.provider.snapshot().records[0]
        qualifier = row.time_qualification
        self.assertEqual(qualifier.provider_internal_at, 150)
        self.assertEqual(qualifier.provider_basis, "provider_internal_date_unverified")
        self.assertIsNone(qualifier.verified_event_at)
        self.assertEqual(qualifier.event_origin, "unknown")
        metadata = json.loads(json.dumps(qualifier.to_metadata()))
        self.assertEqual(H.TimeQualification.from_metadata(metadata), qualifier)
        metadata["value"]["verified_event_at"] = 150
        with self.assertRaises(ValueError):
            H.TimeQualification.from_metadata(metadata)
        metadata["value"]["verified_event_at"] = None
        metadata["value"]["relative_deadlines_supported"] = True
        with self.assertRaises(ValueError):
            H.TimeQualification.from_metadata(metadata)

    def test_metadata_unknown_version_extra_fields_and_malformed_refs_are_rejected(self):
        self.start()
        self.step()
        original = self.provider.snapshot().receipt.to_metadata()
        for field, value in (("schema", 2), ("schema", True), ("kind", "native_snapshot"), ("value", [])):
            data = {**original, field: value}
            with self.assertRaises(ValueError):
                H.HistoryReceipt.from_metadata(data)
        with self.assertRaises(ValueError):
            H.HistoryReceipt.from_metadata({**original, "email_consent": True})
        data = json.loads(json.dumps(original))
        data["value"]["refs"][0]["resource_id"] = "../../outside"
        with self.assertRaises(ValueError):
            H.HistoryReceipt.from_metadata(data)
        malformed = dataclasses.replace(self.provider.snapshot().receipt, expires_at=None)
        self.assertIsNone(self.provider.snapshot(expected_receipt=malformed))
        self.assertIsNone(self.provider.snapshot(required_refs=None))

    def test_getters_are_zero_network_and_ttl_never_renews(self):
        self.start()
        self.step()
        receipt = self.provider.snapshot().receipt
        calls = len(self.http.calls)
        for _ in range(10):
            self.now += 1
            self.mono += 1
            snapshot = self.provider.snapshot(expected_receipt=receipt)
            self.assertEqual(snapshot.receipt.expires_at, 300)
            self.provider.status()
            self.provider.checkpoint()
        self.assertEqual(len(self.http.calls), calls)
        self.now, self.mono = 300, 100
        self.assertIsNone(self.provider.snapshot(expected_receipt=receipt))
        self.step()
        self.assertEqual(len(self.http.calls), calls)

    def test_clock_rollback_and_monotonic_expiry_withdraw(self):
        for now, mono in ((199, 0), (200, 100), (float("nan"), 0)):
            self.now, self.mono = 200, 0
            self.start()
            self.http = FakeHTTP()
            self.step()
            self.now, self.mono = now, mono
            self.assertIsNone(self.provider.snapshot())

    def test_new_start_invalidates_old_receipts_and_checkpoint_cannot_restore_authority(self):
        self.start()
        self.step()
        old = self.provider.snapshot().receipt
        checkpoint = self.provider.checkpoint()
        encoded = json.dumps(checkpoint)
        self.assertNotIn("older than", encoded)
        self.assertNotIn("shared@example", encoded)
        self.assertNotIn(old.job_token, encoded)
        fresh = self.make_provider()
        self.assertIsNone(fresh.snapshot(expected_receipt=old))
        self.assertEqual(fresh.status()["state"], "off")
        self.start()
        self.assertIsNone(self.provider.snapshot(expected_receipt=old))
        self.assertEqual(self.provider.status()["cached"], 0)

    def test_each_identity_and_consent_epoch_change_withdraws_before_dispatch(self):
        original = self.admission
        variants = [dataclasses.replace(original, consent_epoch=2),
                    dataclasses.replace(original, session_epoch=2)]
        for field in ("account_id", "connection_revision", "account_set_revision"):
            variants.append(dataclasses.replace(original, account=dataclasses.replace(self.account, **{field: "changed"})))
        for changed in variants:
            with self.subTest(admission=changed):
                self.admission = original
                self.start()
                self.admission = changed
                self.step()
                self.assertEqual(self.http.calls, [])
                self.assertIsNone(self.provider.snapshot())

    def test_sync_control_withdraws_old_records_for_every_action(self):
        for action in ("pause", "cancel", "revoke", "forget"):
            self.start()
            self.http = FakeHTTP()
            self.step()
            receipt = self.provider.snapshot().receipt
            self.provider.control(action)
            self.assertIsNone(self.provider.snapshot(expected_receipt=receipt))
            calls = len(self.http.calls)
            self.step()
            self.assertEqual(len(self.http.calls), calls)

    def test_cancel_forget_and_epoch_changes_at_token_list_and_body_awaits(self):
        for boundary in ("token", "list", "body"):
            for action in ("pause", "cancel", "revoke", "forget", "epoch", "account"):
                with self.subTest(boundary=boundary, action=action):
                    self.admission = dataclasses.replace(self.admission, email_consent=True)
                    self.start()
                    fired = []
                    def withdraw():
                        fired.append(True)
                        if action == "epoch":
                            self.admission = dataclasses.replace(self.admission, consent_epoch=self.admission.consent_epoch + 1)
                        elif action == "account":
                            self.admission = dataclasses.replace(self.admission, account=dataclasses.replace(self.admission.account, connection_revision="new-" + str(len(self.http.calls))))
                        else:
                            self.provider.control(action)
                    async def hook(method, url):
                        if (boundary == "list" and url.endswith("/messages")) or (boundary == "body" and not url.endswith("/messages")):
                            withdraw()
                    async def acquire(http):
                        if boundary == "token":
                            withdraw()
                        await asyncio.sleep(0)
                        return H.HistoryAccessToken(http.admission, "fake")
                    self.http = FakeHTTP(hook=hook)
                    self.step(token=acquire)
                    self.assertTrue(fired)
                    self.assertIsNone(self.provider.snapshot())
                    self.assertEqual(self.provider.status()["cached"], 0)
                    self.assertEqual(len(self.http.calls), {"token": 0, "list": 1, "body": 2}[boundary])

    def test_revocation_during_token_http_prevents_get(self):
        async def hook(method, url):
            self.provider.control("forget")
        async def acquire(http):
            response = await http.post(H.TOKEN_URL, data={"refresh_token": "synthetic"})
            return H.HistoryAccessToken(http.admission, response.json()["access_token"])
        self.http.hook = hook
        self.start()
        self.step(token=acquire)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0][0], "POST")
        self.assertIsNone(self.provider.snapshot())

    def test_cancelled_task_clears_existing_cache_and_propagates(self):
        self.http = FakeHTTP(pages=[{"messages": [{"id": "51"}], "nextPageToken": "2"}])
        self.start()
        self.step()
        self.assertEqual(self.provider.status()["cached"], 1)
        async def acquire(http):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.step(token=acquire)
        self.assertIsNone(self.provider.snapshot())
        self.assertEqual(self.provider.status()["state"], "cancelled")

    def test_before_publication_rechecks_admission_and_does_not_resurrect_after_forget(self):
        self.start()
        parse = H.parse_history_body
        def forget(*args, **kwargs):
            result = parse(*args, **kwargs)
            self.provider.control("forget")
            return result
        with patch.object(H, "parse_history_body", forget):
            self.step()
        self.assertIsNone(self.provider.snapshot())
        self.assertEqual(self.provider.status()["cached"], 0)

    def test_forget_at_cache_commit_and_receipt_commit_never_restores_freshness(self):
        self.start()
        original_put = H._CACHE.put
        def forget_at_put(owner, row):
            owner.control("forget")
            return original_put(owner, row)
        with patch.object(H._CACHE, "put", forget_at_put):
            self.step()
        self.assertIsNone(self.provider.snapshot())
        self.assertFalse(self.provider.status()["fresh"])
        self.assertEqual(self.provider.status()["cached"], 0)
        self.http = FakeHTTP()
        self.start()
        original_rows = H._CACHE.rows
        fired = []
        def forget_at_rows(owner):
            # First rows inside the page receipt publication, after its guard.
            if not fired:
                fired.append(True)
                owner.control("forget")
            return original_rows(owner)
        with patch.object(H._CACHE, "rows", forget_at_rows):
            self.step()
        self.assertTrue(fired)
        self.assertIsNone(self.provider.snapshot())
        self.assertFalse(self.provider.status()["fresh"])

    def test_pagination_overlaps_empty_intermediate_and_original_leases_survive(self):
        self.http = FakeHTTP(pages=[{"messages": [{"id": "51"}], "nextPageToken": "p2"},
                                   {"messages": [], "nextPageToken": "p3"},
                                   {"messages": [{"id": "51"}, {"id": "52"}]}],
                             messages={"51": message(), "52": message("52", "page three")})
        self.start()
        self.step()
        original = self.provider.snapshot()
        self.now, self.mono = 210, 10
        self.step()
        self.assertFalse(self.provider.status()["eof_observed"])
        self.step()
        current = self.provider.snapshot()
        self.assertEqual([r.resource_id for r in current.records], ["51", "52"])
        self.assertEqual(self.provider.status()["listed"], 2)
        self.assertEqual(self.provider.status()["fetched"], 2)
        self.assertTrue(self.provider.status()["eof_observed"])
        older = self.provider.snapshot(expected_receipt=original.receipt, required_refs=(original.records[0].ref,))
        self.assertTrue(older.complete)
        self.assertEqual(older.records[0].published_at, 200)
        self.assertEqual(older.records[0].expires_at, 300)
        rehydrated = self.provider.snapshot(
            expected_receipt=H.HistoryReceipt.from_metadata(json.loads(json.dumps(original.receipt.to_metadata()))),
            required_refs=(H.RecordRef.from_metadata(json.loads(json.dumps(original.records[0].ref.to_metadata()))),))
        self.assertEqual(rehydrated.records, older.records)
        self.assertEqual([r.rfc_message_id for r in current.records], ["<shared@example.test>"] * 2)

    def test_query_caps_and_estimate_are_not_exhaustion(self):
        self.scope = dataclasses.replace(self.scope, message_cap=1)
        self.admission = dataclasses.replace(self.admission, scope_digest=self.scope.digest)
        self.http = FakeHTTP(pages=[{"messages": [{"id": "51"}], "nextPageToken": "more", "resultSizeEstimate": 1}])
        self.start()
        self.step()
        status = self.provider.status()
        self.assertTrue(status["capped"])
        self.assertFalse(status["eof_observed"])
        self.assertEqual(status["semantic_processing"], "not_performed")
        self.step()
        self.assertEqual(len(self.http.calls), 2)

    def test_query_page_budget_and_mailbox_scope_parameters(self):
        self.scope = dataclasses.replace(self.scope, selection="mailbox")
        self.admission = dataclasses.replace(self.admission, scope_digest=self.scope.digest)
        self.start()
        self.step(budget=dataclasses.replace(self.budget, page_size=1))
        params = self.http.calls[0][2]["params"]
        self.assertEqual(params["q"], "after:100 before:190")
        self.assertEqual(params["includeSpamTrash"], "false")
        self.assertNotIn("labelIds", params)

    def test_invalid_page_cycle_and_provider_identity_do_not_publish_false_coverage(self):
        self.http = FakeHTTP(pages=[{"nextPageToken": "cycle"}, {"nextPageToken": "cycle"}])
        self.start()
        self.step()
        self.step()
        self.assertEqual(self.provider.status()["failed"], 1)
        self.assertFalse(self.provider.status()["eof_observed"])
        self.start()
        self.http = FakeHTTP(messages={"51": message("wrong")})
        self.step()
        self.assertEqual(self.provider.status()["failed"], 1)
        self.assertEqual(self.provider.snapshot().records, ())

    def test_failed_body_keeps_previous_page_and_pending_without_automatic_retry(self):
        self.http = FakeHTTP(pages=[{"messages": [{"id": "51"}, {"id": "52"}]}],
                             messages={"51": message(), "52": RuntimeError("private body and token")})
        self.start()
        self.step()
        status = self.provider.status()
        self.assertEqual((status["listed"], status["fetched"], status["failed"], status["pending"]), (2, 1, 1, 1))
        self.assertEqual(len(self.http.calls), 3)
        self.assertNotIn("private", json.dumps(status))
        self.assertEqual(self.provider.snapshot().records[0].resource_id, "51")
        self.http.messages["52"] = message("52")
        self.step()  # Explicit caller action, not automatic retry.
        self.assertEqual(len(self.provider.snapshot().records), 2)

    def test_token_http_counts_toward_step_budget_and_no_retry_on_429(self):
        async def acquire(http):
            return H.HistoryAccessToken(http.admission, (await http.post(H.TOKEN_URL, data={})).json()["access_token"])
        self.start()
        self.step(token=acquire, budget=dataclasses.replace(self.budget, http_calls=1))
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.provider.status()["http_calls"], 1)
        self.assertEqual(self.provider.status()["listed"], 0)
        self.start()
        class Busy(FakeHTTP):
            async def request(inner, method, url, **kwargs):
                inner.calls.append((method, url, kwargs))
                return httpx.Response(429, json={"error": "private"})
        self.http = Busy()
        self.step()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.provider.status()["failed"], 1)

    def test_wrong_account_token_and_unbound_string_never_dispatch(self):
        wrong = dataclasses.replace(self.admission, account=dataclasses.replace(self.account, account_id="other"))
        for result in ("unbound-token", H.HistoryAccessToken(wrong, "wrong-account-token")):
            self.start()
            async def acquire(http):
                return result
            self.step(token=acquire)
            self.assertEqual(self.http.calls, [])
            self.assertEqual(self.provider.status()["failed"], 1)
            self.assertIsNone(self.provider.snapshot())

    def test_pause_then_new_start_while_response_inflight_preserves_new_job(self):
        fired = []
        async def hook(method, url):
            if not fired:
                fired.append(True)
                self.provider.control("pause")
                self.start()
        self.http.hook = hook
        self.start()
        self.step()
        self.assertEqual(self.provider.status()["state"], "running")
        self.assertEqual(self.provider.status()["listed"], 0)
        self.assertIsNone(self.provider.snapshot())
        self.http = FakeHTTP()
        self.step()
        self.assertEqual(self.provider.status()["cached"], 1)

    def test_empty_page_ledger_has_bounded_memory_and_does_not_claim_eof(self):
        self.scope = dataclasses.replace(self.scope, message_cap=1)
        self.admission = dataclasses.replace(self.admission, scope_digest=self.scope.digest)
        self.http = FakeHTTP(pages=[{"nextPageToken": "2"}, {"nextPageToken": "3"}])
        self.start()
        for _ in range(4):
            self.step()
        status = self.provider.status()
        self.assertTrue(status["capped"])
        self.assertFalse(status["eof_observed"])
        self.assertEqual(status["listed"], 0)
        self.assertEqual(len(self.http.calls), 2)
        self.assertIn("pagination", status["error"])

    def test_transport_endpoint_allowlist_and_redirects_are_enforced(self):
        async def acquire(http):
            await http.post("https://example.test/send", data={})
            return H.HistoryAccessToken(http.admission, "fake")
        self.start()
        self.step(token=acquire)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.provider.status()["failed"], 1)
        self.start()
        self.step()
        self.assertTrue(all(call[2]["follow_redirects"] is False for call in self.http.calls))

    def test_deadline_stops_new_dispatch_after_inflight_response(self):
        async def hook(method, url):
            self.mono = 31
        self.http.hook = hook
        self.start()
        self.step()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.provider.status()["pending"], 1)
        self.assertEqual(self.provider.snapshot().records, ())

    def test_reentrant_step_does_not_double_dispatch(self):
        async def hook(method, url):
            status = await self.provider.step(http=self.http, token=token, budget=self.budget)
            self.assertEqual(status["state"], "running")
        self.http.hook = hook
        self.start()
        self.step()
        self.assertEqual(len(self.http.calls), 2)

    def test_body_and_call_budgets_are_hard_ceilings(self):
        self.http = FakeHTTP(pages=[{"messages": [{"id": str(i)} for i in range(10)]}],
                             messages={str(i): message(str(i), "x" * 10) for i in range(10)})
        self.start()
        self.step(budget=dataclasses.replace(self.budget, full_bodies=2))
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(self.provider.status()["pending"], 8)
        self.step(budget=dataclasses.replace(self.budget, http_calls=1))
        self.assertEqual(len(self.http.calls), 4)
        self.assertEqual(self.provider.status()["pending"], 7)
        self.step(budget=dataclasses.replace(self.budget, accepted_body_bytes=79999))
        self.assertEqual(len(self.http.calls), 4)

    def test_eviction_reports_missing_ref_without_fetching(self):
        self.provider = self.make_provider(limits=dataclasses.replace(self.limits, account_cache_bytes=30))
        self.http = FakeHTTP(pages=[{"messages": [{"id": "51"}], "nextPageToken": "2"},
                                   {"messages": [{"id": "52"}]}],
                             messages={"51": message(), "52": message("52", "y" * 26)})
        self.start()
        self.step()
        original = self.provider.snapshot()
        self.step()
        partial = self.provider.snapshot(expected_receipt=original.receipt)
        self.assertFalse(partial.complete)
        self.assertEqual(partial.missing_refs, (original.records[0].ref,))
        self.assertEqual(self.provider.status()["evicted"], 1)
        self.assertEqual(len(self.http.calls), 4)

    def test_global_cache_cap_across_sources_does_not_cross_account_deduplicate(self):
        gc.collect()
        limits = dataclasses.replace(self.limits, global_cache_bytes=30)
        first_admission = self.admission
        self.provider = self.make_provider(limits=limits, admission=lambda: first_admission)
        self.start()
        self.step()
        old = self.provider.snapshot()
        self.admission = dataclasses.replace(self.admission, account=dataclasses.replace(self.account, account_id="second"))
        second = self.make_provider(limits=limits)
        second.control("start", scope=self.scope)
        asyncio.run(second.step(http=FakeHTTP(), token=token, budget=self.budget))
        partial = self.provider.snapshot(expected_receipt=old.receipt)
        self.assertFalse(partial.complete)
        self.assertEqual(second.snapshot().records[0].account.account_id, "second")
        self.assertEqual(self.provider.status()["evicted"], 1)

    def test_out_of_scope_unknown_date_spam_trash_and_malformed_body_are_ineligible(self):
        variants = [dict(message(), internalDate="not a timestamp"), dict(message(), internalDate="100000"),
                    dict(message(), labelIds=["INBOX", "SPAM"]), dict(message(), labelIds=["TRASH"]),
                    dict(message(), labelIds=["SENT"])]
        for data in variants:
            self.start()
            self.http = FakeHTTP(messages={"51": data})
            self.step()
            snap = self.provider.snapshot()
            self.assertFalse(snap.complete)
            self.assertEqual(snap.records[0].body.status, "omitted")
            self.assertEqual(self.provider.status()["cached_eligible"], 0)
            if data["internalDate"] == "not a timestamp":
                self.assertIsNone(snap.records[0].time_qualification.provider_internal_at)
                self.assertEqual(snap.records[0].time_qualification.provider_basis, "unknown")


class StrictMIME(unittest.TestCase):
    def parse(self, payload, cap=80000):
        return H.parse_history_body(payload, byte_cap=cap)

    def test_history_body_beyond_display_cap_and_exact_unicode_offsets(self):
        text = "α" * 15000 + "\r\nlast"
        body = self.parse(message(text=text)["payload"])
        self.assertEqual(body.status, "complete")
        self.assertEqual(body.text, "α" * 15000 + "\nlast")
        self.assertGreater(len(body.text.encode()), 20000)
        self.assertEqual(body.end_offset, 15005)

    def test_strict_decode_size_and_charset_errors_are_never_complete(self):
        for body in ({"data": "%%%", "size": 3}, {"data": "_w", "size": 1},
                     {"data": "aGk", "size": 999}, {"data": "aGk"}, {"attachmentId": "external", "size": 20}):
            with self.subTest(body=body):
                parsed = self.parse({"mimeType": "text/plain", "body": body})
                self.assertNotEqual(parsed.status, "complete")
                self.assertFalse(parsed.eligible)
        payload = message()["payload"]
        payload["headers"].append({"name": "Content-Type", "value": "text/plain; charset=koi8-r"})
        self.assertEqual(self.parse(payload).status, "unsupported")

    def test_html_missing_unknown_and_nested_malformed_are_explicitly_partial(self):
        plain = message(text="known")['payload']
        for bad in (None, {}, {"mimeType": "text/html", "body": {"data": "aGk", "size": 2}},
                    {"mimeType": "multipart/mixed", "parts": [plain, {"mimeType": "unknown"}]}):
            body = self.parse(bad)
            self.assertNotEqual(body.status, "complete")
            self.assertFalse(body.eligible)

    def test_truncated_utf8_prefix_is_not_eligible(self):
        body = self.parse(message(text="αβγ")["payload"], cap=5)
        self.assertEqual(body.text, "αβ")
        self.assertEqual(body.status, "truncated")
        self.assertFalse(body.eligible)
        huge = self.parse(message(text="x" * 90000)["payload"])
        self.assertEqual(huge.status, "truncated")
        multipart = {"mimeType": "multipart/mixed", "parts": [message(text="12345")["payload"],
                                                               message(text="more")["payload"]]}
        boundary = self.parse(multipart, cap=5)
        self.assertLessEqual(len(boundary.text.encode()), 5)
        self.assertEqual(boundary.status, "truncated")

    def test_plain_alternative_attachment_and_empty_body_have_honest_coverage(self):
        plain = message(text="plain")["payload"]
        html = {"mimeType": "text/html", "body": {"data": "aGk", "size": 2}}
        alternative = self.parse({"mimeType": "multipart/alternative", "parts": [html, plain]})
        self.assertEqual(alternative.text, "plain")
        self.assertEqual(alternative.omissions, ("alternative_representations",))
        attached = self.parse({"mimeType": "multipart/mixed", "parts": [plain,
                              {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x"}}]})
        self.assertEqual(attached.status, "complete")
        self.assertIn("attachment", attached.omissions)
        empty = self.parse(message(text="")["payload"])
        self.assertEqual(empty.status, "complete")
        self.assertFalse(empty.eligible)


if __name__ == "__main__":
    unittest.main()
