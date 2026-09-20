from __future__ import annotations
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import types
import importlib.util
import os
import shutil
import stat
import struct
import subprocess
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "build-support"))
import pipeline
import simulation
from managed_live_qa import ARTIFACT_KIND, QA_PORT
from managed_live_qa.backend import ManagedQABackend, derive_capability
from managed_live_qa.harness import (OneShotHarness, PREDICATE_KEYS, QAError,
    canonical_manifest, install_synthetic_adapters, sanitized_report,
    LiveSummaryRunner, _CountingClient)
from managed_live_qa.staging import SOURCE_ALLOWLIST, assemble, reject_for_production, sha
from managed_live_qa.secure_backend import (IntegrityError, decode_authenticated_report,
    derive_session_key, encode_authenticated_report, verify_inventory)
from managed_live_qa.secure_harness import (LiveSummaryRunner as SecureLiveSummaryRunner,
    PREDICATE_KEYS as SECURE_PREDICATE_KEYS, QAError as SecureQAError,
    READINESS_KEYS as SECURE_READINESS_KEYS, ReportIdentity,
    canonical_manifest as secure_manifest, install_synthetic_adapters as secure_adapters,
    sanitized_report as secure_report)
from managed_live_qa.secure_staging import (PRODUCTION_TARGET, SOURCE_ALLOWLIST_V2,
    SUPPORT_FILES, _checkout_state)


class ManagedQAStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name) / "qa"
        self.checkout = Path(self.temp.name) / "checkout"
        self.checkout.mkdir()
        for relative in SOURCE_ALLOWLIST_V2:
            target = self.checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        for name in SUPPORT_FILES:
            relative = Path("build-support/managed_live_qa") / name
            target = self.checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "QA Fixture",
                   "GIT_AUTHOR_EMAIL": "qa@example.invalid",
                   "GIT_COMMITTER_NAME": "QA Fixture",
                   "GIT_COMMITTER_EMAIL": "qa@example.invalid"}
        for command in (["git", "init", "-q"], ["git", "add", "."],
                        ["git", "commit", "-q", "-m", "fixture"]):
            subprocess.run(command, cwd=self.checkout, env=git_env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.artifact = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.checkout, text=True).strip()
        self.runtime = Path(self.temp.name) / "runtime"
        (self.runtime / "bin").mkdir(parents=True)
        python = self.runtime / "bin/python3"
        python.write_bytes(b"closed-runtime-fixture")
        python.chmod(0o500)
        (self.runtime / "lib").mkdir()
        (self.runtime / "lib/stdlib.fixture").write_bytes(b"stdlib")

    def assemble(self, artifact=None):
        return assemble(self.checkout, self.destination, artifact or self.artifact,
                        PRODUCTION_TARGET,
                        runtime_source=self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def test_staging_is_separate_exact_and_production_rejected(self):
        stage = self.assemble()
        marker = json.loads((self.destination / "artifact-kind.json").read_text())
        self.assertEqual(marker["artifact_kind"], ARTIFACT_KIND)
        self.assertFalse(marker["production_release_eligible"])
        manifest = json.loads((stage / "qa-build-manifest.json").read_text())
        self.assertEqual(manifest["ipc_protocol"], "anonymous-pipes-v1")
        self.assertEqual(manifest["exclusive_proof_protocol"], "unavailable")
        self.assertEqual(manifest["production_sha"], PRODUCTION_TARGET)
        self.assertEqual(manifest["artifact_sha"], self.artifact)
        source_inventory = json.loads((stage / "qa-source-inventory.json").read_text())
        runtime_inventory = json.loads((stage / "qa-runtime-inventory.json").read_text())
        self.assertTrue(source_inventory)
        self.assertEqual(set(runtime_inventory), {"bin/python3", "lib/stdlib.fixture"})
        credentials = (stage / "source/app/Sources/WispApp/BackendCredentials.swift").read_text()
        self.assertIn("com.wisp.summary-qa.inference", credentials)
        self.assertNotIn("WISP_MINI_INFERENCE_KEY", credentials)
        self.assertIn("Bundle.main.bundleURL.path", credentials)
        inventory = {path.relative_to(stage).as_posix() for path in stage.rglob("*")}
        self.assertFalse(any("AppDelegate.swift" in path or "PortGuard" in path
                             or "Reader.swift" in path for path in inventory))
        native = (stage / "source/native_pipe_main.swift").read_text()
        self.assertIn("BackendCredentials.writePipe", native)
        self.assertIn("posix_spawn", native)
        self.assertIn("cleanupProcessGroup", native)
        self.assertIn(manifest["runtime_inventory_sha256"], native)
        self.assertNotIn("__RUNTIME_INVENTORY_SHA256__", native)
        self.assertIn("verifyNativeInventories", native)
        self.assertIn("renameatx_np", native)
        self.assertIn("SecStaticCodeCheckValidity", native)
        self.assertNotIn("URLSession", native)
        self.assertNotIn("lsof", native)
        self.assertNotIn("18765", native)
        with self.assertRaisesRegex(ValueError, "never production"):
            reject_for_production(self.destination)
        with self.assertRaisesRegex(pipeline.BuildError, "excluded"):
            pipeline.verify_artifacts(self.destination)

    def test_production_sources_are_not_modified_by_assembly(self):
        before = {relative: sha(ROOT / relative) for relative in SOURCE_ALLOWLIST}
        self.assemble()
        self.assertEqual(before, {relative: sha(ROOT / relative) for relative in SOURCE_ALLOWLIST})

    def test_staging_uses_declared_git_blobs_and_detects_checkout_drift(self):
        relative = SOURCE_ALLOWLIST_V2[0]
        committed = subprocess.check_output(
            ["git", "show", f"{self.artifact}:{relative}"], cwd=self.checkout)
        (self.checkout / relative).write_bytes(b"mutable-worktree-substitution")
        stage = self.assemble()
        self.assertEqual((stage / "source" / relative).read_bytes(), committed)
        with self.assertRaisesRegex(ValueError, "changed while sealing"):
            _checkout_state(self.checkout, self.artifact)

    def test_authoritative_gate_includes_qa_contracts(self):
        self.assertIn("tests/build_pipeline/managed_live_qa_checks.py", simulation.BUILD_TESTS)

    def test_full_file_exclusion_and_chunk_boundary(self):
        self.destination.mkdir()
        candidate = self.destination / "renamed.bin"
        for padding in (2_100_000, 3 * 1_048_576 - 5):
            candidate.write_bytes(b"x" * padding + b"com.wisp.app.summary-qa")
            with self.assertRaisesRegex(pipeline.BuildError, "excluded"):
                pipeline.verify_artifacts(self.destination)

    def test_production_verification_rejects_all_symlink_shapes(self):
        outside = Path(self.temp.name) / "marker.bin"
        outside.write_bytes(b"com.wisp.app.summary-qa")
        for name, target in (("top-link", outside),
                             ("dangling-link", Path(self.temp.name) / "missing")):
            with self.subTest(name=name):
                candidate = Path(self.temp.name) / name
                candidate.mkdir()
                (candidate / "payload").symlink_to(target)
                with self.assertRaisesRegex(pipeline.BuildError, "symlinks"):
                    pipeline.verify_artifacts(candidate)
        nested = Path(self.temp.name) / "nested-link"
        (nested / "deep").mkdir(parents=True)
        link = nested / "deep/payload"
        link.symlink_to(outside)
        outside.write_bytes(b"benign")
        with self.assertRaisesRegex(pipeline.BuildError, "symlinks"):
            pipeline.verify_artifacts(nested)
        outside.write_bytes(b"com.wisp.summary-qa.inference")
        with self.assertRaisesRegex(pipeline.BuildError, "symlinks"):
            pipeline.verify_artifacts(nested)

    def test_dirty_qa_switch_refused_before_assembly(self):
        with patch.object(sys, "argv", ["pipeline.py", "qa-assemble", "--allow-dirty",
                                       "--output", str(self.destination)]), \
                patch("managed_live_qa.staging.build") as build:
            self.assertEqual(pipeline.main(), 1)
            build.assert_not_called()

    def test_staged_credential_parser_is_exact_production_source(self):
        stage = self.assemble()
        self.assertEqual((stage / "source/service/credential_pipe.py").read_bytes(),
                         (ROOT / "service/credential_pipe.py").read_bytes())
        reader = (stage / "source/app/Sources/WispApp/BackendCredentials.swift").read_text()
        self.assertIn('let directory = home + "/.wisp-summary-qa"', reader)
        self.assertNotIn('let directory = home + "/.moe"', reader)

    def test_runtime_inventory_detects_mutation_and_startup_hooks(self):
        stage = self.assemble()
        valid, _digest = verify_inventory(stage / "runtime",
                                          stage / "qa-runtime-inventory.json")
        self.assertTrue(valid)
        (stage / "runtime/lib/stdlib.fixture").write_bytes(b"changed")
        self.assertFalse(verify_inventory(stage / "runtime",
                         stage / "qa-runtime-inventory.json")[0])
        other = Path(self.temp.name) / "qa-hook"
        (self.runtime / "lib/evil.pth").write_text("import bad")
        with self.assertRaisesRegex(ValueError, "startup hooks"):
            assemble(self.checkout, other, self.artifact, PRODUCTION_TARGET,
                     runtime_source=self.runtime)

    def test_secure_template_preflights_exclusivity_before_keychain(self):
        native = (ROOT / "build-support/managed_live_qa/native_pipe_main.swift").read_text()
        gate = native.index('exclusive_proof_protocol"] as? String != "server-lease-v1"')
        keychain = native.index("BackendCredentials.loadForBackend()")
        self.assertLess(gate, keychain)
        inventory = native.index("verifyNativeInventories")
        self.assertLess(inventory, gate)

    def test_cleanup_kills_descendant_after_group_leader_exits(self):
        helper = Path(self.temp.name) / "cleanup-helper.swift"
        helper.write_text(r'''import Darwin
import Foundation
@_silgen_name("fork") func fixtureFork() -> pid_t
@main enum Probe {
  static func main() {
    var fds = [Int32](repeating: -1, count: 2)
    guard pipe(&fds) == 0 else { exit(2) }
    let leader = fixtureFork()
    if leader == 0 {
      close(fds[0]); _ = setpgid(0, 0)
      let descendant = fixtureFork()
      if descendant == 0 {
        _ = signal(SIGTERM, SIG_IGN)
        while true { pause() }
      }
      var value = descendant
      _ = withUnsafePointer(to: &value) {
        write(fds[1], $0, MemoryLayout<pid_t>.size)
      }
      close(fds[1]); exit(0)
    }
    close(fds[1]); var descendant: pid_t = 0
    _ = withUnsafeMutablePointer(to: &descendant) {
      read(fds[0], $0, MemoryLayout<pid_t>.size)
    }
    close(fds[0]); usleep(100_000)
    let cleaned = cleanupProcessGroup(leader, graceMilliseconds: 100,
                                      killMilliseconds: 3_000)
    usleep(100_000)
    exit(cleaned && kill(descendant, 0) != 0 && errno == ESRCH ? 0 : 1)
  }
}
''')
        executable = Path(self.temp.name) / "cleanup-helper"
        swift_env = {**os.environ,
            "CLANG_MODULE_CACHE_PATH": str(Path(self.temp.name) / "clang-module-cache"),
            "SWIFT_MODULECACHE_PATH": str(Path(self.temp.name) / "swift-module-cache")}
        compile_result = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "swiftc",
            str(ROOT / "build-support/managed_live_qa/process_group_cleanup.swift"),
            str(helper), "-o", str(executable)], env=swift_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        subprocess.run([str(executable)], check=True, timeout=10,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_staged_summary_import_closure_uses_only_synthetic_cache(self):
        stage = self.assemble()
        saved = {name: module for name, module in sys.modules.items()
                 if name == "service" or name.startswith("service.")}
        attributes = {name: vars(module).copy() for name, module in saved.items()}
        try:
            secure_adapters(stage / "source/service", endpoint="http://127.0.0.1:8000")
            for name in ("service.tools.email_tools", "service.tools.imessage_tools",
                         "service.assistant.brief"):
                sys.modules.pop(name, None)
            email = importlib.import_module("service.tools.email_tools")
            messages = importlib.import_module("service.tools.imessage_tools")
            brief = importlib.import_module("service.assistant.brief")
            self.assertEqual((email._headers, messages._lines), ("", ""))
            self.assertIsNone(email._client)
            self.assertIsNone(messages._client)
            self.assertTrue(callable(brief._sections))
            self.assertEqual(sys.modules["service.config"].user_facing_summary_kwargs(
                "Ling-3.0-tiny-oQ4e"), {})
            quarantine = importlib.import_module("service.config.quarantine")
            self.assertIn(".wisp-summary-qa", str(quarantine.RecoveryGate().directory))
        finally:
            for name in list(sys.modules):
                if name == "service" or name.startswith("service."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved)
            for name, module in saved.items():
                vars(module).clear()
                vars(module).update(attributes[name])


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
        saved = {name: module for name, module in sys.modules.items()
                 if name == "service" or name.startswith("service.")}
        attributes = {name: vars(module).copy() for name, module in saved.items()}
        try:
            installed = install_synthetic_adapters(ROOT / "service")
            self.assertEqual(installed["service.tools.cache_store"].load("messages"), "")
            with self.assertRaises(QAError):
                installed["service.tools.cache_store"].save("messages", "private")
            with self.assertRaises(QAError):
                installed["service.tools.cache_store"].load("unknown")
        finally:
            for name in list(sys.modules):
                if name == "service" or name.startswith("service."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved)
            for name, module in saved.items():
                vars(module).clear()
                vars(module).update(attributes[name])
        for name, module in saved.items():
            self.assertIs(sys.modules[name], module)
            self.assertEqual(vars(module).keys(), attributes[name].keys())

    async def test_live_runner_requires_external_proof_before_any_client_operation(self):
        class RejectClient:
            def __getattr__(self, name):
                raise AssertionError("unexpected client operation: " + name)
        runner = object.__new__(LiveSummaryRunner)
        runner.manifest = self.manifest
        runner.client = RejectClient()
        self.assertFalse(any((await runner.qualify()).values()))
        with self.assertRaisesRegex(QAError, "external_exclusivity_required"):
            await runner.run()
        with patch("managed_live_qa.harness.install_synthetic_adapters") as adapters:
            with self.assertRaisesRegex(QAError, "external_exclusivity_required"):
                LiveSummaryRunner(self.manifest, "a" * 64, ROOT / "service",
                                  source_hashes_valid=True)
            adapters.assert_not_called()

    async def test_residency_assertion_never_calls_admission_or_eviction(self):
        model = self.manifest["model"]
        class Client:
            async def loaded_models(self):
                return [model]
            def __getattr__(self, name):
                raise AssertionError("unexpected mutation: " + name)
        client = _CountingClient(Client(), self.manifest)
        await client.ensure_only(model, exclusive=True)
        for action in (client.load, client.unload):
            with self.assertRaisesRegex(QAError, "model_mutation_forbidden"):
                await action(model)

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


class SecureManagedQAContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest, manifest_sha = secure_manifest(
            ROOT / "build-support/managed_live_qa/manifest-v2.json")
        self.identity = ReportIdentity(
            manifest_sha256=manifest_sha, production_sha=PRODUCTION_TARGET,
            artifact_sha="a" * 40, build_manifest_sha256="b" * 64,
            source_inventory_sha256="c" * 64, runtime_inventory_sha256="d" * 64,
            native_sha256="e" * 64, launch_nonce="f" * 64,
            child_pid=4321, ipc_authenticated=True)

    def test_nonce_bound_authenticated_report_rejects_replay_and_tampering(self):
        first = derive_session_key("1" * 64, "2" * 64, artifact_sha="a" * 40,
            production_sha=PRODUCTION_TARGET, parent_pid=123, child_pid=4321,
            attestation_sha="3" * 64)
        second = derive_session_key("1" * 64, "4" * 64, artifact_sha="a" * 40,
            production_sha=PRODUCTION_TARGET, parent_pid=123, child_pid=4321,
            attestation_sha="3" * 64)
        frame = encode_authenticated_report({"status": "BLOCK", "nonce": "2" * 64}, first)
        self.assertEqual(decode_authenticated_report(frame, first)["status"], "BLOCK")
        with self.assertRaisesRegex(IntegrityError, "authentication"):
            decode_authenticated_report(frame, second)
        damaged = bytearray(frame)
        damaged[15] ^= 1
        with self.assertRaises(IntegrityError):
            decode_authenticated_report(bytes(damaged), first)

    def test_complete_report_contract_and_cross_field_invariants(self):
        predicates = {key: True for key in SECURE_PREDICATE_KEYS}
        readiness = {key: True for key in SECURE_READINESS_KEYS}
        report = secure_report(self.identity, model=self.manifest["model"], status="PASS",
            reason_codes=[], call_count=2,
            selected_ids=list(self.manifest["expected_selected_ids"]),
            predicates=predicates, readiness=readiness, elapsed_ms=12)
        self.assertEqual(set(report), set(self.manifest["report_keys"]))
        with self.assertRaisesRegex(SecureQAError, "invalid_report"):
            secure_report(self.identity, model=self.manifest["model"], status="PASS",
                reason_codes=[], call_count=1, selected_ids=[], predicates=predicates,
                readiness=readiness, elapsed_ms=12)
        blocked_identity = ReportIdentity(**{
            **self.identity.__dict__, "child_pid": 0, "ipc_authenticated": False})
        blocked = secure_report(blocked_identity, model=self.manifest["model"],
            status="BLOCK", reason_codes=["external_exclusivity_required"], call_count=0,
            selected_ids=[], predicates={key: False for key in SECURE_PREDICATE_KEYS},
            readiness={key: False for key in SECURE_READINESS_KEYS}, elapsed_ms=0)
        self.assertEqual(blocked["status"], "BLOCK")

    def test_no_lease_blocks_before_adapter_or_client_construction(self):
        with patch("managed_live_qa.secure_harness.install_synthetic_adapters") as adapters:
            with self.assertRaisesRegex(SecureQAError, "external_exclusivity_required"):
                SecureLiveSummaryRunner(self.manifest, "1" * 64, ROOT / "service",
                                        source_hashes_valid=True)
            adapters.assert_not_called()


class CredentialFrameTests(unittest.TestCase):
    def consume(self, payload=None, *, trailing=b"", writable=False, timeout=False):
        spec = importlib.util.spec_from_file_location("qa_parser_fixture",
                                                      ROOT / "service/credential_pipe.py")
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        document = {"version": 1, "pid": os.getpid(), "uid": os.getuid(),
                    "role": "primary", "generation": "absent",
                    "credentials": {"WISP_LOCAL_OMLX_KEY": "a" * 64}}
        body = payload if payload is not None else json.dumps(document).encode()
        frame = b"WISPCP1\n" + struct.pack("!I", len(body)) + body + trailing
        info = types.SimpleNamespace(st_mode=stat.S_IFIFO, st_uid=os.getuid(),
                                     st_dev=123, st_ino=456)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,
                {"WISP_CREDENTIAL_PIPE": "v1:123:456"}, clear=True))
            stack.enter_context(patch.object(parser.os, "fstat", return_value=info))
            stack.enter_context(patch.object(parser.fcntl, "fcntl",
                return_value=os.O_WRONLY if writable else os.O_RDONLY))
            inherited = stack.enter_context(patch.object(parser.os, "set_inheritable"))
            stack.enter_context(patch.object(parser.os, "close"))
            stack.enter_context(patch.object(parser.select, "select",
                return_value=([], [], []) if timeout else ([0], [], [])))
            reader = stack.enter_context(patch.object(parser.os, "read",
                                                      side_effect=[frame, b""]))
            result = parser.consume("primary")
            inherited.assert_called_once_with(0, False)
            self.assertEqual(reader.call_count, 2)
            return result

    def test_production_parser_requires_eof_and_noninheritable_reader(self):
        self.assertEqual(self.consume(), ({"WISP_LOCAL_OMLX_KEY": "a" * 64}, "absent"))

    def test_production_parser_rejects_trailing_duplicate_writable_and_timeout(self):
        for arguments in ({"trailing": b"extra"}, {"payload": b'{"version":1,"version":1}'},
                          {"writable": True}, {"timeout": True}):
            with self.subTest(arguments=tuple(arguments)):
                with self.assertRaisesRegex(ValueError, "Native credential pipe unavailable"):
                    self.consume(**arguments)


if __name__ == "__main__":
    unittest.main()
