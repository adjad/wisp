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
        p.json_write(self.root / "provenance.json", {"source": self.meta})
        p.json_write(self.root / "bundle-manifest.json", p.inventory(bundle))
        p.archive(bundle, self.root / "Wisp.zip", self.meta["source_epoch"])
        p.checksums(self.root)
        return bundle

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

    def test_matrix_covers_all_tests(self):
        matrix = json.loads((p.SUPPORT / "test-matrix.json").read_text())
        self.assertEqual(set(matrix), {str(f.relative_to(ROOT)) for f in (ROOT / "tests").glob("test_*.py")})
        self.assertEqual(sum(s["runner"] == "excluded" for s in matrix.values()), 2)


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
