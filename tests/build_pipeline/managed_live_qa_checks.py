from __future__ import annotations
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "build-support"))
import pipeline
import simulation
from managed_live_qa import ARTIFACT_KIND, QA_PORT
from managed_live_qa.backend import ManagedQABackend, derive_capability
from managed_live_qa.harness import (OneShotHarness, PREDICATE_KEYS, QAError,
    canonical_manifest, install_synthetic_adapters, sanitized_report)
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
        for relative, expected in manifest["source_hashes"].items():
            self.assertEqual(expected, sha(ROOT / relative))
        for relative, expected in manifest["staged_hashes"].items():
            self.assertEqual(expected, sha(stage / relative))
        credentials = (stage / "app/Sources/WispApp/BackendCredentials.swift").read_text()
        self.assertIn("com.wisp.summary-qa.inference", credentials)
        self.assertNotIn("WISP_MINI_INFERENCE_KEY", credentials)
        self.assertIn("Bundle.main.bundleURL.path", credentials)
        inventory = {path.relative_to(stage).as_posix() for path in stage.rglob("*")}
        self.assertFalse(any("AppDelegate.swift" in path or "PortGuard" in path
                             or "Reader.swift" in path for path in inventory))
        native = (stage / "native_main.swift").read_text()
        self.assertIn("BackendCredentials.writePipe", native)
        self.assertIn("process.waitUntilExit()", native)
        self.assertNotIn("WISP_QA_RUN_CAPABILITY", native)
        self.assertNotIn("exit(78)", native)
        with self.assertRaisesRegex(ValueError, "never production"):
            reject_for_production(self.destination)
        with self.assertRaisesRegex(pipeline.BuildError, "excluded"):
            pipeline.verify_artifacts(self.destination)

    def test_production_sources_are_not_modified_by_assembly(self):
        before = {relative: sha(ROOT / relative) for relative in SOURCE_ALLOWLIST}
        assemble(ROOT, self.destination, "8" * 40)
        self.assertEqual(before, {relative: sha(ROOT / relative) for relative in SOURCE_ALLOWLIST})

    def test_authoritative_gate_includes_qa_contracts(self):
        self.assertIn("tests/build_pipeline/managed_live_qa_checks.py", simulation.BUILD_TESTS)


class ManagedQAHarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manifest, self.manifest_sha = canonical_manifest(
            ROOT / "build-support/managed_live_qa/manifest.json")

    def harness(self):
        return OneShotHarness(self.manifest, self.manifest_sha, "a" * 40)

    def predicates(self, value=True):
        return {key: value for key in PREDICATE_KEYS}

    async def test_pass_is_one_shot_and_sanitized(self):
        harness = self.harness()
        async def run():
            return list(self.manifest["expected_selected_ids"]), self.predicates(), 2
        report = await harness.consume(
            True, {key: True for key in self.manifest["required_readiness"]}, run)
        self.assertEqual(report["status"], "PASS")
        self.assertNotIn("raw", json.dumps(report).lower())
        with self.assertRaisesRegex(QAError, "spent"):
            await harness.consume(True, {}, run)

    async def test_missing_credential_and_busy_fail_closed(self):
        async def never():
            self.fail("run must not execute")
        invalid = await self.harness().consume(
            False, {key: True for key in self.manifest["required_readiness"]}, never)
        self.assertEqual(invalid["reason_codes"], ["invalid_capability"])
        for missing in self.manifest["required_readiness"]:
            readiness = {key: True for key in self.manifest["required_readiness"]}
            readiness[missing] = False
            self.assertEqual((await self.harness().consume(True, readiness, never))["status"],
                             "BLOCK")

    async def test_replay_cancellation_and_redaction(self):
        harness, entered, release = self.harness(), asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return [], self.predicates(), 0
        task = asyncio.create_task(harness.consume(
            True, {key: True for key in self.manifest["required_readiness"]}, slow))
        await entered.wait()
        with self.assertRaisesRegex(QAError, "spent"):
            await harness.consume(True, {}, slow)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        secret, bad = "PRIVATE-TOKEN-should-never-appear", self.harness()
        async def explode():
            raise RuntimeError(secret)
        report = await bad.consume(
            True, {key: True for key in self.manifest["required_readiness"]}, explode)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["reason_codes"], ["bounded_run_failed"])

    async def test_backend_rejects_extra_keys_and_unknown_ids(self):
        capability = derive_capability("1" * 64, "a" * 40, "run")
        backend = ManagedQABackend(self.harness(), self.manifest["manifest_id"], capability)
        with self.assertRaisesRegex(QAError, "invalid_request"):
            await backend.post({"manifest_id": self.manifest["manifest_id"],
                "capability": capability, "extra": 1}, None, {})
        async def run():
            return ["private-row"], self.predicates(), 2
        report = await backend.post({"manifest_id": self.manifest["manifest_id"],
            "capability": capability}, run,
            {key: True for key in self.manifest["required_readiness"]})
        self.assertEqual(report["reason_codes"], ["unknown_fixture_id"])

    async def test_timeout_consumes_state(self):
        manifest = dict(self.manifest, run_timeout_seconds=0.001)
        harness = OneShotHarness(manifest, self.manifest_sha, "a" * 40)
        async def slow():
            await asyncio.sleep(1)
            return [], self.predicates(), 0
        report = await harness.consume(
            True, {key: True for key in manifest["required_readiness"]}, slow)
        self.assertEqual((report["status"], harness.state), ("FAIL", "TERMINAL"))

    def test_adapters_precede_imports(self):
        names = ("service.tools.email_tools", "service.tools.imessage_tools",
                 "service.assistant.brief", "service.tools", "service.memory",
                 "service.assistant", "service.tools.cache_store",
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

    def test_report_contract_is_bounded(self):
        report = sanitized_report(manifest_sha="b" * 64, candidate_sha="a" * 40,
            model=self.manifest["model"], status="BLOCK",
            reason_codes=["readiness_unproven"], call_count=0, selected_ids=[],
            predicates=self.predicates(False), elapsed_ms=0)
        self.assertEqual(set(report), set(self.manifest["report_keys"]))
        with self.assertRaises(QAError):
            sanitized_report(manifest_sha="b" * 64, candidate_sha="a" * 40,
                model=self.manifest["model"], status="PASS", reason_codes=[],
                call_count=3, selected_ids=[], predicates=self.predicates(), elapsed_ms=0)


if __name__ == "__main__":
    unittest.main()
