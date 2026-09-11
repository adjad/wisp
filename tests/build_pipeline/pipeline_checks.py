from __future__ import annotations

from argparse import Namespace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "build-support"))
import pipeline as p
import release


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.meta = {"version": p.CONFIG["version"], "build_number": "12", "commit": "a" * 40,
                     "source_epoch": 1700000000, "dirty": False, "architecture": "arm64", "minimum_macos": "14.0"}

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        bundle = self.root / "Wisp.app"
        for rel in ("Contents/MacOS/Wisp", "Contents/Resources/AppIcon.icns", "Contents/Resources/build-info.json",
                    "Contents/Resources/backend/.venv/bin/python3", "Contents/Resources/backend/service/main.py",
                    "Contents/Resources/backend/service/config/models.yaml", "Contents/Resources/backend/service/config/policy.yaml",
                    "Contents/Resources/backend/service/skills/bundled/interview-me/SKILL.md",
                    "Contents/Resources/backend/service/skills/bundled/idea-refine/SKILL.md"):
            path = bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        (bundle / "Contents/MacOS/Wisp").chmod(0o755)
        (bundle / "Contents/Resources/backend/.venv/bin/python3").chmod(0o755)
        (bundle / "Contents/Resources/backend/.venv/bin/python").symlink_to("python3")
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(p.plist(self.meta)))
        return bundle

    def test_valid_bundle(self):
        p.validate_structure(self.fixture(), self.meta)

    def test_missing_resource(self):
        bundle = self.fixture()
        (bundle / "Contents/Resources/backend/service/config/policy.yaml").unlink()
        with self.assertRaisesRegex(p.BuildError, "Missing"):
            p.validate_structure(bundle, self.meta)

    def test_absolute_symlink(self):
        bundle = self.fixture()
        (bundle / "escape").symlink_to("/usr/bin/python3")
        with self.assertRaisesRegex(p.BuildError, "symlink"):
            p.validate_structure(bundle, self.meta)

    def test_relative_symlink_escape(self):
        bundle = self.fixture()
        (self.root / "outside").write_text("outside")
        (bundle / "escape").symlink_to("../outside")
        with self.assertRaisesRegex(p.BuildError, "symlink"):
            p.validate_structure(bundle, self.meta)

    def test_reject_state_credentials_and_host_venv(self):
        for name in ("private.db", "identity.p12", "AuthKey.p8", "pyvenv.cfg", ".env"):
            with self.subTest(name=name):
                bundle = self.fixture() if not (self.root / "Wisp.app").exists() else self.root / "Wisp.app"
                path = bundle / name
                path.write_text("must be rejected")
                with self.assertRaisesRegex(p.BuildError, "Unwanted"):
                    p.validate_structure(bundle, self.meta)
                path.unlink()

    def test_version_mismatch(self):
        bundle = self.fixture()
        with self.assertRaisesRegex(p.BuildError, "Info.plist mismatch"):
            p.validate_structure(bundle, dict(self.meta, version="999.0.0"))

    def test_mode_validation(self):
        bundle = self.fixture()
        (bundle / "Contents/MacOS/Wisp").chmod(0o644)
        with self.assertRaisesRegex(p.BuildError, "not executable"):
            p.validate_structure(bundle, self.meta)

    def test_archive_is_repeatable_and_preserves_symlinks(self):
        bundle = self.fixture()
        first, second = self.root / "a.zip", self.root / "b.zip"
        p.archive(bundle, first, self.meta["source_epoch"])
        os.utime(bundle / "Contents/Info.plist", (1800000000, 1800000000))
        p.archive(bundle, second, self.meta["source_epoch"])
        self.assertEqual(p.digest(first), p.digest(second))
        with zipfile.ZipFile(first) as z:
            info = z.getinfo("Wisp.app/Contents/Resources/backend/.venv/bin/python")
            self.assertTrue(stat.S_ISLNK(info.external_attr >> 16))
            self.assertEqual(z.read(info), b"python3")

    def artifact(self):
        bundle = self.fixture()
        p.json_write(self.root / "simulation-qa.json", self.qa_report())
        p.json_write(self.root / "provenance.json", {"source": self.meta,
            "simulation_sha256": p.digest(self.root / "simulation-qa.json")})
        p.json_write(self.root / "bundle-manifest.json", p.inventory(bundle))
        p.archive(bundle, self.root / "Wisp.zip", self.meta["source_epoch"])
        p.checksums(self.root)
        return bundle

    def qa_report(self):
        return {"schema_version": 3, "status": "PASS", "candidate_sha": self.meta["commit"],
                "ending_sha": self.meta["commit"], "sha_stable": True, "worktree_clean": True,
                "dirty_allowed": False, "profiles": ["full"], "safety_mode": "offline",
                "native_mode": "included", "totals": {"gates": 2, "passed_gates": 2,
                "failed_gates": 0, "blocked_gates": 0}}

    def test_candidate_rejects_partial_stale_or_dirty_qa(self):
        for change in ({"candidate_sha": "b" * 40}, {"status": "FAIL"},
                       {"native_mode": "skip"}, {"profiles": ["routing"]},
                       {"dirty_allowed": True}, {"worktree_clean": False},
                       {"totals": {"gates": 2, "passed_gates": 1, "failed_gates": 0, "blocked_gates": 1}}):
            with self.subTest(change=change), self.assertRaises(p.BuildError):
                p.validate_simulation(dict(self.qa_report(), **change), self.meta["commit"])

    def test_archive_roundtrip_never_calls_release_signing(self):
        bundle = self.fixture()
        archive = self.root / "Wisp.zip"
        p.archive(bundle, archive, self.meta["source_epoch"])
        calls = []
        class FakeRunner:
            logs = self.root
            def run(inner, label, command):
                calls.append(command)
                if label == "extract-distribution":
                    import shutil
                    shutil.copytree(bundle, Path(command[-1]) / "Wisp.app", symlinks=True)
        with patch.object(p, "relocation_smoke"):
            p.distribution_roundtrip(FakeRunner(), archive, bundle, self.meta)
        self.assertFalse(any("codesign" in c for c in calls))

    def test_adhoc_sign_seals_bundle_for_strict_verification(self):
        import shutil
        bundle = self.fixture()
        # A real Mach-O main executable; the fixture's placeholder text cannot be sealed.
        shutil.copyfile("/bin/echo", bundle / "Contents/MacOS/Wisp")
        (bundle / "Contents/MacOS/Wisp").chmod(0o755)

        class DirectRunner:
            logs = self.root

            def run(inner, label, command, **kwargs):
                subprocess.run([str(c) for c in command], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Fails loudly if the bundle cannot be sealed or strictly verified.
        p.adhoc_sign(DirectRunner(), bundle)
        self.assertTrue((bundle / "Contents/_CodeSignature/CodeResources").is_file())
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        described = subprocess.run(["codesign", "-dvv", str(bundle)], capture_output=True, text=True).stderr
        self.assertIn("Signature=adhoc", described)
        self.assertNotIn("Sealed Resources=none", described)
        self.assertNotIn("TeamIdentifier=", described.replace("TeamIdentifier=not set", ""))

    def test_adhoc_sign_never_uses_identity_keychain_or_notarization(self):
        bundle = self.fixture()
        calls = []

        class FakeRunner:
            logs = self.root

            def run(inner, label, command, **kwargs):
                calls.append([str(c) for c in command])

        p.adhoc_sign(FakeRunner(), bundle)
        signing = [c for c in calls if "--sign" in c]
        self.assertTrue(signing)
        for command in signing:
            # Ad-hoc identity only, and no credential, entitlement, or network timestamp.
            self.assertEqual(command[command.index("--sign") + 1], "-")
            self.assertIn("--timestamp=none", command)
        for command in calls:
            self.assertEqual(command[0], "codesign")
            for forbidden in ("--keychain", "--entitlements", "--options"):
                self.assertNotIn(forbidden, command)
        joined = " ".join(" ".join(c) for c in calls)
        for forbidden in ("notarytool", "stapler", "security", "WISP_SIGNING", "Developer ID"):
            self.assertNotIn(forbidden, joined)
        self.assertTrue(any(c[:2] == ["codesign", "--verify"] for c in calls))

    def test_bootstrap_rejects_corrupt_downloads(self):
        import bootstrap_uv
        with patch.object(bootstrap_uv, "download", return_value=b"corrupt"), patch.object(bootstrap_uv.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "archive checksum"):
                bootstrap_uv.ensure_uv(p.CONFIG, self.root)
            with self.assertRaisesRegex(RuntimeError, "archive checksum"):
                bootstrap_uv.ensure_python_archive(p.CONFIG, self.root)

    def test_runtime_extractor_rejects_traversal_and_escaping_links(self):
        import bootstrap_uv
        import tarfile
        state = self.root / "state"
        archive = state / "python-downloads/fixture/runtime.tar.gz"
        archive.parent.mkdir(parents=True)
        for name, target in (("../escape", None), ("python/link", "../../escape")):
            with self.subTest(name=name):
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    if target:
                        member.type = tarfile.SYMTYPE
                        member.linkname = target
                    tar.addfile(member)
                config = dict(p.CONFIG, python_build="fixture",
                              python_archive_url="https://example.invalid/runtime.tar.gz",
                              python_archive_sha256=p.digest(archive))
                with self.assertRaisesRegex(RuntimeError, "Unsafe Python archive"):
                    bootstrap_uv.unpack_runtime(config, state, self.root / "runtime")
                self.assertFalse((self.root / "runtime").exists())
                self.assertFalse((self.root / "escape").exists())

    def runtime_archive(self, entries):
        import io
        import tarfile
        archive = self.root / "state/python-downloads/fixture/runtime.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tar:
            for name, kind, value in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                member.type = kind
                if kind == tarfile.REGTYPE:
                    data = value.encode()
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
                else:
                    member.linkname = value
                    tar.addfile(member)
        return dict(p.CONFIG, python_build="fixture",
                    python_archive_url="https://example.invalid/runtime.tar.gz",
                    python_archive_sha256=p.digest(archive))

    def test_runtime_symlink_graph_rejects_chained_escape_before_writes(self):
        import bootstrap_uv
        import tarfile
        config = self.runtime_archive([
            *[(name, tarfile.DIRTYPE, "") for name in ("python", "python/a", "python/x", "python/bin")],
            ("python/a/b", tarfile.SYMTYPE, "../x"),
            ("python/unsafe", tarfile.SYMTYPE, "a/b/../../outside"),
            ("python/bin/python3", tarfile.SYMTYPE, "../a/b/../../outside"),
        ])
        with patch.object(bootstrap_uv.tempfile, "TemporaryDirectory") as scratch:
            with self.assertRaisesRegex(RuntimeError, "escaping symlink chain"):
                bootstrap_uv.unpack_runtime(config, self.root / "state", self.root / "runtime")
            scratch.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())
        self.assertFalse((self.root / "outside").exists())

    def test_runtime_graph_rejects_missing_loop_special_and_link_parent(self):
        import bootstrap_uv
        import tarfile
        cases = [
            [("python/a", tarfile.SYMTYPE, "missing")],
            [("python/a", tarfile.SYMTYPE, "/absolute")],
            [("python/a", tarfile.SYMTYPE, "")],
            [("python/a", tarfile.SYMTYPE, "a")],
            [("python/a", tarfile.SYMTYPE, "b"), ("python/b", tarfile.SYMTYPE, "a")],
            [("python/a", tarfile.FIFOTYPE, ""), ("python/b", tarfile.SYMTYPE, "a")],
            [("python/a", tarfile.CHRTYPE, "")],
            [("python/a", tarfile.LNKTYPE, "python/b")],
            [("python/a", tarfile.SYMTYPE, "x"), ("python/a/child", tarfile.REGTYPE, "fixture")],
            [("python/a", tarfile.REGTYPE, "fixture"), ("python/b", tarfile.SYMTYPE, "a/../a")],
            [("python/a", tarfile.REGTYPE, "first"), ("python/./a", tarfile.REGTYPE, "second")],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                config = self.runtime_archive(entries)
                with patch.object(bootstrap_uv.tempfile, "TemporaryDirectory") as scratch:
                    with self.assertRaisesRegex(RuntimeError, "Unsafe Python archive"):
                        bootstrap_uv.unpack_runtime(config, self.root / "state", self.root / "runtime")
                    scratch.assert_not_called()

    def test_runtime_preserves_relative_chains_without_tar_extraction_filters(self):
        import bootstrap_uv
        import tarfile
        config = self.runtime_archive([
            ("python/a", tarfile.DIRTYPE, ""),
            ("python/x", tarfile.DIRTYPE, ""),
            ("python/bin/python3", tarfile.SYMTYPE, "../a/b/../real"),
            ("python/a/b", tarfile.SYMTYPE, "../x"),
            ("python/bin/python", tarfile.SYMTYPE, "python3"),
            ("python/real", tarfile.REGTYPE, "fixture interpreter bytes"),
        ])
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("version-dependent extraction")):
            bootstrap_uv.unpack_runtime(config, self.root / "state", self.root / "runtime")
        runtime = self.root / "runtime"
        self.assertEqual(os.readlink(runtime / "bin/python3"), "../a/b/../real")
        self.assertEqual((runtime / "bin/python").read_text(), "fixture interpreter bytes")
        self.assertEqual((runtime / "bin/python").resolve(), (runtime / "real").resolve())
        self.assertTrue(os.access(runtime / "bin/python", os.X_OK))

    def test_interpreter_read_roots_refuses_a_home_wide_prefix(self):
        home = self.root / "home"
        python = home / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("fixture, never executed")
        with patch.object(Path, "home", return_value=home):
            with self.assertRaisesRegex(p.BuildError, "dedicated runtime prefix"):
                p.interpreter_read_roots(python)

    def test_external_virtualenv_is_readable_without_opening_private_home(self):
        self.external_virtualenv_contract(execute=False)

    def check_external_virtualenv_sandbox(self):
        # Explicit integration entry point: Seatbelt cannot be nested. The build
        # driver requires this check before starting the sandboxed full QA run.
        self.external_virtualenv_contract(execute=True)

    def external_virtualenv_contract(self, *, execute):
        home = self.root / "home"
        venv = home / "external-venv"
        scratch = self.root / "qa-scratch"
        scratch.mkdir()
        subprocess.run([sys.executable, "-I", "-B", "-m", "venv", "--without-pip", str(venv)],
                       check=True, capture_output=True)
        packages = venv / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages"
        (packages / "wisp_prefix_fixture.py").write_text("VALUE = 'fixture-only'\n")
        (home / "private.txt").write_text("must remain unreadable")
        alias = home / "venv-alias"
        alias.symlink_to(venv, target_is_directory=True)
        selected = alias / "bin/python"
        self.assertTrue(selected.is_symlink())
        metadata = lambda command, **kwargs: str(self.root / ("developer" if command[0] == "xcode-select" else "os-temp"))
        with patch.object(Path, "home", return_value=home), patch.object(p, "git", return_value=str(ROOT / ".git")), patch.object(p.subprocess, "check_output", side_effect=metadata):
            profile = p.simulation_profile(scratch, selected)
            roots = p.interpreter_read_roots(selected)
            launcher = home / "launcher"
            launcher.symlink_to(venv / "bin/python")
            self.assertIn(venv.resolve(), p.interpreter_read_roots(launcher))
        self.assertIn(venv.resolve(), roots)
        self.assertNotIn(home.resolve(), roots)
        for rule in ("(deny network*)", "(deny appleevent-send)", "(deny file-write*)"):
            self.assertIn(rule, profile)
        if not execute:
            return
        probe = f"""
from pathlib import Path
import sys, wisp_prefix_fixture
assert wisp_prefix_fixture.VALUE == 'fixture-only'
assert Path(sys.prefix).resolve() == Path({str(venv)!r}).resolve()
assert (Path(sys.prefix)/'pyvenv.cfg').read_text()
for action in (lambda: Path({str(home / 'private.txt')!r}).read_text(),
               lambda: Path({str(venv / 'forbidden-write')!r}).write_text('forbidden')):
    try: action()
    except PermissionError: pass
    else: raise AssertionError('sandbox boundary opened')
Path({str(scratch / 'allowed-write')!r}).write_text('fixture')
print('external venv readable; private home and writes denied')
"""
        result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, str(selected), "-I", "-B", "-c", probe],
                                env=p.clean_env(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((venv / "forbidden-write").exists())
        self.assertTrue((scratch / "allowed-write").exists())

    def test_artifact_manifest(self):
        self.artifact()
        p.verify_artifacts(self.root)

    def test_archive_tampering(self):
        self.artifact()
        (self.root / "Wisp.zip").write_bytes(b"corrupt")
        with self.assertRaisesRegex(p.BuildError, "checksum mismatch"):
            p.verify_artifacts(self.root)

    def test_bundle_tampering(self):
        bundle = self.artifact()
        (bundle / "Contents/Resources/backend/service/main.py").write_text("modified")
        with self.assertRaisesRegex(p.BuildError, "Bundle contents differ"):
            p.verify_artifacts(self.root)

    def test_manifest_cannot_reference_parent(self):
        self.artifact()
        (self.root / "SHA256SUMS").write_text("a" * 64 + "  ../outside\n")
        with self.assertRaisesRegex(p.BuildError, "Malformed"):
            p.verify_artifacts(self.root)

    def test_manifest_covers_every_artifact(self):
        self.artifact()
        (self.root / "extra.txt").write_text("unlisted")
        with self.assertRaisesRegex(p.BuildError, "Incomplete"):
            p.verify_artifacts(self.root)

    def test_adapter_keeps_product_manifest_and_adds_build_checks(self):
        import simulation
        discovered = {str(f.relative_to(ROOT)) for f in (ROOT / "tests").rglob("test_*.py")}
        self.assertEqual(set(simulation.qa._selected_tests(["full"])), discovered)
        self.assertEqual(set(simulation.selected_tests(["full"])), discovered | simulation.BUILD_TESTS)
        self.assertIn("tests/build_pipeline/pipeline_checks.py", simulation.BUILD_TESTS)

    def native_runner(self, architecture="arm64", minimum="11.0", dependency="/usr/lib/libSystem.B.dylib"):
        root = self.root
        class FakeRunner:
            logs = root
            def run(self, label, command):
                log = root / (label + ".log")
                if label.startswith("macho-"):
                    log.write_text(architecture)
                elif label.startswith("load-commands-"):
                    log.write_text("cmd LC_BUILD_VERSION\ncmdsize 32\nplatform 1\nminos " + minimum)
                else:
                    log.write_text("binary:\n\t" + dependency + " (compatibility version 1.0.0)\n")
                return 0, log
        return FakeRunner()

    def test_native_inventory_rejects_newer_os_and_external_dylibs(self):
        bundle = self.fixture()
        for name in ("Contents/MacOS/Wisp", "Contents/Resources/backend/.venv/bin/python3"):
            (bundle / name).write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
        p.validate_native(self.native_runner(), bundle, self.meta)
        for runner, message in ((self.native_runner(architecture="x86_64"), "arm64"),
                                (self.native_runner(minimum="26.0"), "advertised minimum"),
                                (self.native_runner(dependency="/opt/homebrew/lib/libfixture.dylib"), "Nonportable")):
            with self.subTest(message=message), self.assertRaisesRegex(p.BuildError, message):
                p.validate_native(runner, bundle, self.meta)


    def test_native_inventory_distinguishes_dylib_identity_from_loads(self):
        bundle = self.fixture()
        for name in ("Contents/MacOS/Wisp", "Contents/Resources/backend/.venv/bin/python3"):
            (bundle / name).write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
        runner = self.native_runner(dependency="/wheel-build/library.dylib")
        original = runner.run
        def run(label, command):
            code, log = original(label, command)
            if label.startswith("load-commands-"):
                log.write_text(log.read_text() + "\ncmd LC_ID_DYLIB\ncmdsize 88\nname /wheel-build/library.dylib (offset 24)\n")
            return code, log
        runner.run = run
        p.validate_native(runner, bundle, self.meta)

    def test_native_inventory_rejects_active_absolute_search_path(self):
        bundle = self.fixture()
        for name in ("Contents/MacOS/Wisp", "Contents/Resources/backend/.venv/bin/python3"):
            (bundle / name).write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
        for dependency in ("/usr/lib/libSystem.B.dylib", "@rpath/library.dylib"):
            runner = self.native_runner(dependency=dependency)
            original = runner.run
            def run(label, command):
                code, log = original(label, command)
                if label.startswith("load-commands-"):
                    log.write_text(log.read_text() + "\ncmd LC_RPATH\ncmdsize 88\npath /wheel-build/lib (offset 12)\n")
                return code, log
            runner.run = run
            if dependency.startswith("@rpath"):
                with self.assertRaisesRegex(p.BuildError, "Absolute rpath"):
                    p.validate_native(runner, bundle, self.meta)
            else:
                p.validate_native(runner, bundle, self.meta)

    def test_dry_run_does_not_write_or_run_stage(self):
        with patch.object(sys, "argv", ["pipeline.py", "release", "--dry-run"]), patch.object(p, "Runner") as runner:
            self.assertEqual(p.main(), 0)
            runner.assert_not_called()

    def test_environment_has_no_credentials_or_injection(self):
        with patch.dict(os.environ, {"GH_TOKEN": "test", "UV_INDEX_URL": "https://untrusted.invalid", "PYTHONPATH": "/bad", "WISP_LIVE_REMINDER_TEST": "1"}):
            env = p.clean_env()
        for key in ("GH_TOKEN", "UV_INDEX_URL", "PYTHONPATH", "WISP_LIVE_REMINDER_TEST"):
            self.assertNotIn(key, env)
        self.assertEqual(env.get("HOME"), os.environ.get("HOME"))

    def test_release_preflight_has_no_side_effects_when_credentials_absent(self):
        args = Namespace(allow_dirty=False, test_python=None, offline=False, output=self.root)
        env = {"GITHUB_ACTIONS": "true", "WISP_RELEASE_APPROVED": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/tags/v" + p.CONFIG["version"]}
        with patch("release.subprocess.run") as run:
            with self.assertRaisesRegex(p.BuildError, "Missing required CI secrets"):
                release.preflight(args, env)
            run.assert_not_called()

    def test_release_cli_fails_cleanly_without_running_external_steps(self):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")}
        result = subprocess.run([sys.executable, "-B", str(p.SUPPORT / "pipeline.py"), "release", "--output", str(self.root)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("BUILD FAILED", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("→", result.stdout)

    def test_release_rejects_local_execution(self):
        args = Namespace(allow_dirty=False, test_python=None, offline=False, output=self.root)
        with self.assertRaisesRegex(p.BuildError, "protected"):
            release.preflight(args, {})

    def test_release_rejects_non_tag_dispatch_and_preview(self):
        args = Namespace(allow_dirty=False, test_python=None, offline=False, output=self.root)
        env = {"GITHUB_ACTIONS": "true", "WISP_RELEASE_APPROVED": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}
        with self.assertRaisesRegex(p.BuildError, "exact version tag"):
            release.preflight(args, env)
        args.allow_dirty = True
        with self.assertRaisesRegex(p.BuildError, "preview"):
            release.preflight(args, env)


    def test_git_source_list_handles_terminating_nul(self):
        with patch.object(p, "git", return_value="service/main.py\0"):
            self.assertEqual(p.source_files(), [ROOT / "service/main.py"])

    def test_private_command_timeout_cannot_expose_password(self):
        import traceback
        command = ["security", "import", "fixture.p12", "-P", "SENTINEL-PRIVATE-PASSWORD"]
        with patch("release.subprocess.run", side_effect=subprocess.TimeoutExpired(command, 120)):
            try:
                release.secret_run(command)
            except p.BuildError:
                output = traceback.format_exc()
            else:
                self.fail("Timeout must fail")
        self.assertNotIn("SENTINEL-PRIVATE-PASSWORD", output)
        self.assertIn("suppressed", output)

    def test_guard_allows_scratch_sqlite_uri_and_cleanup(self):
        snippet = f"""
import sys, sqlite3, shutil
sys.path.insert(0, {str(p.SUPPORT)!r})
from pathlib import Path
import test_worker
scratch=Path({str(self.root)!r})
violations=test_worker.install_guard(Path({str(ROOT)!r}), scratch, Path(sys.base_prefix))
folder=scratch/'data'
folder.mkdir()
with sqlite3.connect(str(folder/'fixture.db')) as db:
    db.execute('create table fixture (name text)')
with sqlite3.connect(f'file:{{folder}}/fixture.db?mode=ro', uri=True) as db:
    assert db.execute('select count(*) from fixture').fetchone()[0] == 0
shutil.rmtree(folder)
assert not violations, violations
"""
        proc = subprocess.run([sys.executable, "-B", "-c", snippet], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bootstrap_never_executes_bad_binary(self):
        import bootstrap_uv
        wrong = self.root / "wrong-uv"
        wrong.write_text("not the pinned binary")
        with patch("bootstrap_uv.shutil.which", return_value=str(wrong)):
            with self.assertRaisesRegex(RuntimeError, "Offline bootstrap"):
                bootstrap_uv.ensure_uv(p.CONFIG, self.root, offline=True)


    def test_guard_caught_denial_still_fails(self):
        scratch = self.root / "scratch"
        scratch.mkdir()
        forbidden = self.root / "outside.txt"
        proc = subprocess.run([sys.executable, "-B", str(p.SUPPORT / "test_worker.py"), "guard-probe", str(forbidden),
            "--root", str(ROOT), "--scratch", str(scratch)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 86, proc.stderr)
        self.assertFalse(forbidden.exists())
        self.assertIn("ISOLATION VIOLATIONS", proc.stderr)

    def test_guard_denies_network_sqlite_and_subprocess(self):
        # Run in a child because Python audit hooks cannot be removed.
        snippet = f'''
import sys
sys.path.insert(0, {str(p.SUPPORT)!r})
from pathlib import Path
import test_worker, socket, sqlite3, subprocess
violations=test_worker.install_guard(Path({str(ROOT)!r}), Path({str(self.root)!r}), Path(sys.base_prefix))
for action in (lambda: socket.getaddrinfo("example.invalid", 80), lambda: sqlite3.connect("/tmp/wisp-forbidden.db"), lambda: subprocess.run(["/usr/bin/true"])):
    try: action()
    except PermissionError: pass
assert len(violations)==3, violations
'''
        proc = subprocess.run([sys.executable, "-B", "-c", snippet], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
