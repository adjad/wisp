from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "build-support"))

import pipeline
from managed_live_qa import ARTIFACT_KIND, QA_PORT
from managed_live_qa.backend import ManagedQABackend
from managed_live_qa.harness import (OneShotHarness, QAError, canonical_manifest,
                                     install_synthetic_adapters, sanitized_report)
from managed_live_qa.staging import SOURCE_ALLOWLIST, assemble, reject_for_production, sha


class ManagedQAStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name) / "qa"

    def tearDown(self):
        self.temp.cleanup()

    def test_staging_is_separate_exact_and_production_rejected(self):
        stage = assemble(ROOT, self.destination, "9" * 40)
        marker = json.loads((self.destination / "artifact-kind.json").read_text())
        self.assertEqual(marker["artifact_kind"], ARTIFACT_KIND)
        self.assertFalse(marker["production_release_eligible"])
        manifest = json.loads((stage / "qa-build-manifest.json").read_text())
        self.assertEqual(manifest["port"], QA_PORT)
        self.assertEqual(set(manifest["source_hashes"]), set(SOURCE_ALLOWLIST))
        for rel, expected in manifest["source_hashes"].items():
            self.assertEqual(expected, sha(ROOT / rel))
            self.assertEqual((stage / rel).read_bytes(), (ROOT / rel).read_bytes())
        inventory = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
        self.assertFalse(any("AppDelegate.swift" in p or "PortGuard" in p or "Reader.swift" in p
                             for p in inventory))
        with self.assertRaisesRegex(ValueError, "never production"):
            reject_for_production(self.destination)
        with self.assertRaisesRegex(pipeline.BuildError, "excluded"):
            pipeline.verify_artifacts(self.destination)

    def test_production_sources_are_not_modified_by_assembly(self):
        before = {rel: sha(ROOT / rel) for rel in SOURCE_ALLOWLIST}
        assemble(ROOT, self.destination, "8" * 40)
        self.assertEqual(before, {rel: sha(ROOT / rel) for rel in SOURCE_ALLOWLIST})


class ManagedQAHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manifest_path = ROOT / "build-support/managed_live_qa/manifest.json"
        self.manifest, self.manifest_sha = canonical_manifest(self.manifest_path)

    def harness(self):
        return OneShotHarness(self.manifest, self.manifest_sha, "a" * 40)

    async def test_pass_is_one_shot_and_sanitized(self):
        harness = self.harness()
        async def run():
            return ["email-urgent", "message-family"], {"schemas_valid": True, "daily_model_free": True}
        report = await harness.consume(True, {k: True for k in self.manifest["required_readiness"]}, run)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["call_count"], 2)
        self.assertNotIn("raw", json.dumps(report).lower())
        with self.assertRaisesRegex(QAError, "spent"):
            await harness.consume(True, {}, run)

    async def test_missing_credential_and_busy_are_fail_closed(self):
        async def never():
            self.fail("run must not execute")
        invalid = await self.harness().consume(False, {k: True for k in self.manifest["required_readiness"]}, never)
        self.assertEqual((invalid["status"], invalid["reason_codes"]), ("FAIL", ["invalid_capability"]))
        for missing in self.manifest["required_readiness"]:
            readiness = {k: True for k in self.manifest["required_readiness"]}
            readiness[missing] = False
            blocked = await self.harness().consume(True, readiness, never)
            self.assertEqual(blocked["status"], "BLOCK")

    async def test_concurrency_replay_cancellation_and_redaction(self):
        harness = self.harness()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def slow():
            entered.set(); await release.wait(); return [], {"ok": True}
        first = asyncio.create_task(harness.consume(True, {k: True for k in self.manifest["required_readiness"]}, slow))
        await entered.wait()
        with self.assertRaisesRegex(QAError, "spent"):
            await harness.consume(True, {}, slow)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(harness.state, "TERMINAL")
        secret = "PRIVATE-TOKEN-should-never-appear"
        bad = self.harness()
        async def explode():
            raise RuntimeError(secret)
        report = await bad.consume(True, {k: True for k in self.manifest["required_readiness"]}, explode)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["reason_codes"], ["bounded_run_failed"])

    async def test_backend_rejects_extra_keys_and_unknown_fixture_ids(self):
        harness = self.harness()
        backend = ManagedQABackend(harness, self.manifest["manifest_id"], "cap")
        with self.assertRaisesRegex(QAError, "invalid_request"):
            await backend.post({"manifest_id": self.manifest["manifest_id"], "capability": "cap", "extra": 1}, None, {})
        async def run():
            return ["private-row"], {"schemas_valid": True}
        report = await backend.post({"manifest_id": self.manifest["manifest_id"], "capability": "cap"}, run,
                                    {k: True for k in self.manifest["required_readiness"]})
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["selected_ids"], [])

    async def test_timeout_consumes_state_without_retry(self):
        manifest = dict(self.manifest, run_timeout_seconds=0.001)
        harness = OneShotHarness(manifest, self.manifest_sha, "a" * 40)
        async def slow():
            await asyncio.sleep(1)
            return [], {}
        report = await harness.consume(True, {k: True for k in manifest["required_readiness"]}, slow)
        self.assertEqual((report["status"], harness.state), ("FAIL", "TERMINAL"))
        with self.assertRaises(QAError):
            await harness.consume(True, {}, slow)

    def test_synthetic_adapters_are_installed_before_summary_imports(self):
        names = ("service.tools.email_tools", "service.tools.imessage_tools", "service.assistant.brief",
                 "service.tools", "service.memory", "service.assistant", "service.tools.cache_store",
                 "service.memory.identity", "service.debug_capture")
        saved = {name: sys.modules.pop(name, None) for name in names}
        try:
            installed = install_synthetic_adapters(ROOT / "service")
            self.assertEqual(installed["service.tools.cache_store"].load("messages"), "")
            with self.assertRaises(QAError):
                installed["service.tools.cache_store"].save("messages", "private")
            with self.assertRaises(QAError):
                installed["service.tools.cache_store"].load("unknown")
        finally:
            for name in names:
                sys.modules.pop(name, None)
                if saved[name] is not None:
                    sys.modules[name] = saved[name]

    def test_report_contract_rejects_unbounded_shape(self):
        report = sanitized_report(manifest_sha="b" * 64, candidate_sha="a" * 40,
            model=self.manifest["model"], status="BLOCK", reason_codes=["readiness_unproven"],
            call_count=0, selected_ids=[], predicates={}, elapsed_ms=0)
        self.assertEqual(set(report), set(self.manifest["report_keys"]))
        with self.assertRaises(QAError):
            sanitized_report(manifest_sha="b" * 64, candidate_sha="a" * 40,
                model=self.manifest["model"], status="PASS", reason_codes=[], call_count=3,
                selected_ids=[], predicates={}, elapsed_ms=0)


if __name__ == "__main__":
    unittest.main()
