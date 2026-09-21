#!/usr/bin/env python3
"""Wisp's source-to-artifact pipeline. Stdlib driver; no app launch or install."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "build-support"
CONFIG = json.loads((SUPPORT / "toolchain.json").read_text())
STATE = ROOT / ".wisp-build"
MACH_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


class BuildError(RuntimeError):
    pass


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        result = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
        return result.hexdigest()


def json_write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def metadata(allow_dirty=False, build_number=None) -> dict:
    version = CONFIG["version"]
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise BuildError("toolchain.json version must be a numeric major.minor.patch")
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    if dirty and not allow_dirty:
        raise BuildError("Commit source changes first, or use --allow-dirty for a local preview.")
    if git("rev-parse", "--is-shallow-repository") == "true":
        raise BuildError("Full Git history is required for build numbers and release notes (fetch-depth: 0).")
    number = build_number or git("rev-list", "--count", "HEAD")
    if not re.fullmatch(r"[1-9]\d{0,3}(?:\.(?:0|[1-9]\d?)){0,2}", number):
        raise BuildError("Build number must fit Apple's 4.2.2 digit CFBundleVersion format.")
    commit = git("rev-parse", "HEAD")
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/") and ref != "refs/tags/v" + version:
        raise BuildError("Release tag must exactly match v + toolchain.json version.")
    return {"version": version, "build_number": number, "commit": commit,
            "source_epoch": int(git("show", "-s", "--format=%ct", "HEAD")),
            "dirty": dirty, "architecture": CONFIG["architecture"],
            "minimum_macos": CONFIG["minimum_macos"]}


def source_fingerprint():
    value = hashlib.sha256()
    paths = [name for name in git("ls-files", "-z").split("\0") if name]
    for name in sorted(paths):
        path = ROOT / name
        value.update(name.encode() + b"\0")
        value.update((os.readlink(path) if path.is_symlink() else digest(path) if path.is_file() else "<deleted>").encode())
    return value.hexdigest()


def clean_env() -> dict[str, str]:
    # Do not inherit package indexes, Python injection, signing or app credentials.
    keep = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "DEVELOPER_DIR", "SDKROOT", "LANG"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONHASHSEED="0",
               UV_CACHE_DIR=str(STATE / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(STATE / "python"),
               UV_LINK_MODE="copy", UV_KEYRING_PROVIDER="disabled", UV_NO_CONFIG="1",
               CLANG_MODULE_CACHE_PATH=str(STATE / "module-cache"),
               SWIFTPM_MODULECACHE_OVERRIDE=str(STATE / "module-cache"),
               MACOSX_DEPLOYMENT_TARGET=CONFIG["minimum_macos"], TZ="UTC")
    return env


class Runner:
    def __init__(self):
        STATE.mkdir(exist_ok=True)
        self.logs = Path(tempfile.mkdtemp(prefix="run-", dir=STATE))
        self.records = []
        self.env = clean_env()

    def run(self, label, command, *, cwd=ROOT, env=None, timeout=1200, check=True,
            pass_fds=()):
        log = self.logs / (re.sub(r"[^a-zA-Z0-9_.-]", "-", label) + ".log")
        print(f"→ {label}", flush=True)
        started = time.monotonic()
        with log.open("w") as output:
            try:
                proc = subprocess.run([str(c) for c in command], cwd=cwd, env=env or self.env,
                                      stdout=output, stderr=subprocess.STDOUT, timeout=timeout,
                                      pass_fds=tuple(pass_fds))
                code = proc.returncode
            except subprocess.TimeoutExpired:
                code = 124
                output.write(f"\nTIMEOUT after {timeout}s\n")
        self.records.append({"step": label, "exit_code": code, "seconds": round(time.monotonic()-started, 2), "log": log.name})
        json_write(self.logs / "results.json", self.records)
        if code:
            print(log.read_text(errors="replace")[-7000:], file=sys.stderr)
            if check:
                raise BuildError(f"{label} failed ({code}); diagnostics: {log}")
        return code, log

    def uv(self, label, args, *, offline=False):
        return self.run(label, ["uv", *args, "--no-config", "--cache-dir", STATE / "uv-cache", *(["--offline"] if offline else [])])


def doctor(runner: Runner, strict=False, offline=False) -> dict:
    if platform.system() != "Darwin" or platform.machine() != CONFIG["architecture"]:
        raise BuildError("This pipeline targets Apple Silicon macOS only; cross-compilation is not supported.")
    from bootstrap_uv import ensure_uv
    try:
        uv_path = ensure_uv(CONFIG, STATE, offline)
    except (RuntimeError, OSError) as exc:
        raise BuildError(str(exc)) from None
    runner.env["PATH"] = str(uv_path.parent) + os.pathsep + runner.env.get("PATH", "")
    for tool in ("git", "swift", "swiftc", "xcrun", "iconutil", "codesign", "otool", "lipo", "sandbox-exec"):
        if not shutil.which(tool):
            raise BuildError(f"Missing {tool}; see docs/build-release.md bootstrap prerequisites.")
    _, log = runner.run("uv-version", ["uv", "--version"])
    if log.read_text().split()[1] != CONFIG["uv"]:
        raise BuildError(f"Use uv {CONFIG['uv']} exactly; tool versions are release inputs.")
    _, log = runner.run("swift-version", ["swift", "--version"])
    swift = log.read_text().strip()
    runner.run("sdk", ["xcrun", "--show-sdk-version"])
    xcode = "Command Line Tools"
    code, log = runner.run("xcode-version", ["xcodebuild", "-version"], check=False)
    if code == 0:
        xcode = log.read_text().strip()
    else:
        runner.records[-1]["optional"] = True
    if strict and (f"Xcode {CONFIG['ci_xcode']}\n" not in xcode + "\n" or f"Swift version {CONFIG['ci_swift']} " not in swift):
        raise BuildError("Release builds require the exact Xcode/Swift versions in toolchain.json.")
    runner.run("test-sandbox-available", ["sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", "/usr/bin/true"])
    result = {"uv": CONFIG["uv"], "swift": swift, "xcode": xcode, "macos": platform.mac_ver()[0], "strict_toolchain": strict}
    json_write(runner.logs / "toolchain.json", result)
    return result


def lock_inputs() -> list[Path]:
    return [ROOT / "requirements-runtime.txt", SUPPORT / "requirements-test.in", SUPPORT / "toolchain.json"]


def check_locks() -> None:
    path = SUPPORT / "locks.json"
    if not path.exists():
        raise BuildError("Dependency locks are absent. Run ./scripts/wisp-build lock.")
    expected = json.loads(path.read_text())
    for name, sha in expected.items():
        file = ROOT / name
        if not file.is_file() or digest(file) != sha:
            raise BuildError(f"Dependency lock drift: {name}; regenerate and review with wisp-build lock.")
    required = {str(p.relative_to(ROOT)) for p in lock_inputs()} | {
        "build-support/requirements-runtime.lock", "build-support/requirements-test.lock"}
    if set(expected) != required:
        raise BuildError("locks.json must cover exactly the dependency and toolchain inputs.")
    for name in ("runtime", "test"):
        content = (SUPPORT / f"requirements-{name}.lock").read_text()
        if "--hash=sha256:" not in content or "--index-url" in content or " @ " in content:
            raise BuildError("Dependency locks must contain hashes and only registry-pinned requirements.")


def lock(runner: Runner, offline=False) -> None:
    for name, source in (("runtime", ROOT / "requirements-runtime.txt"), ("test", SUPPORT / "requirements-test.in")):
        runner.uv("lock-" + name, ["pip", "compile", source, "--python-version", CONFIG["python"],
                 "--python-platform", "aarch64-apple-darwin", "--generate-hashes", "--no-header",
                 "--no-emit-index-url", "--default-index", "https://pypi.org/simple", "--only-binary", ":all:",
                 "-o", SUPPORT / f"requirements-{name}.lock"], offline=offline)
    inputs = lock_inputs() + [SUPPORT / f"requirements-{n}.lock" for n in ("runtime", "test")]
    json_write(SUPPORT / "locks.json", {str(p.relative_to(ROOT)): digest(p) for p in inputs})
    check_locks()


def bootstrap(runner: Runner, offline=False) -> Path:
    check_locks()
    from bootstrap_uv import ensure_python_archive
    try:
        mirror = ensure_python_archive(CONFIG, STATE, offline)
    except (RuntimeError, OSError) as exc:
        raise BuildError(str(exc)) from None
    runner.uv("python-bootstrap", ["python", "install", CONFIG["python"], "--mirror", mirror,
                                  "--no-bin", "--install-dir", STATE / "python"], offline=offline)
    runtime = STATE / "python" / f"cpython-{CONFIG['python']}-macos-aarch64-none"
    if not (runtime / "BUILD").is_file() or (runtime / "BUILD").read_text().strip() != CONFIG["python_build"]:
        raise BuildError("Unexpected python-build-standalone build; refuse to package an unpinned runtime.")
    python = runtime / "bin" / "python3"
    runner.run("python-version", [python, "-I", "-B", "-c", f"import platform; assert platform.python_version() == {CONFIG['python']!r}"])
    test_env = STATE / "test-env"
    runner.uv("test-environment", ["venv", "--python", python, "--no-python-downloads", "--allow-existing", test_env], offline=offline)
    test_python = test_env / "bin" / "python"
    runner.uv("test-dependencies", ["pip", "sync", "--python", test_python, "--no-python-downloads", "--require-hashes",
              "--only-binary", ":all:", "--strict", "--default-index", "https://pypi.org/simple", SUPPORT / "requirements-test.lock"], offline=offline)
    return test_python


def sandbox_profile(scratch: Path, root: Path, python: Path, *, executables=()) -> str:
    def q(path):
        return json.dumps(str(Path(path).resolve()))
    # OS enforcement covers SQLite/native extensions; Python guard adds diagnostics.
    runtime = python.resolve().parent.parent
    framework_launcher = runtime / "Resources/Python.app/Contents/MacOS/Python"
    if framework_launcher.is_file():
        executables = (*executables, framework_launcher)
    paths = [root, scratch, runtime, python.absolute().parent.parent, STATE / "test-env"]
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)", "(deny process-exec)",
        "(allow process-exec " + " ".join(f"(literal {q(p)})" for p in [python, *executables]) + ")",
        "(deny file-write*)", f"(allow file-write* (subpath {q(scratch)}) (literal \"/dev/null\"))",
        f"(deny file-read-data (subpath {q(Path.home())}))",
        "(allow file-read-data " + " ".join(f"(subpath {q(p)})" for p in paths) + ")",
    ])


def guarded(runner, label, python, kind, target, *, root=ROOT, timeout=180):
    scratch = Path(tempfile.mkdtemp(prefix="test-", dir=runner.logs)).resolve()
    env = dict(runner.env, WISP_HOME=str(scratch / "moe"), TMPDIR=str(scratch),
               WISP_SANDBOX_HOME=str(scratch / "sandbox"), WISP_LING_TEMPLATE=str(scratch / "absent-template"),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", TZ="America/Los_Angeles")
    worker = scratch / "test_worker.py"
    shutil.copy2(SUPPORT / "test_worker.py", worker)
    profile = sandbox_profile(scratch, root, python)
    result = runner.run(label, ["sandbox-exec", "-p", profile, python, "-B", worker, kind, target,
                        "--root", root, "--scratch", scratch], cwd=root, env=env, timeout=timeout, check=False)
    # Never retain databases/fixtures in CI logs; only textual test output.
    shutil.rmtree(scratch)
    return result[0]


def interpreter_read_roots(python):
    """Base runtime plus explicitly selected virtualenv prefixes, never HOME."""
    selected = python.absolute()
    roots = {selected.resolve(strict=True).parent.parent}
    seen = set()
    while selected not in seen:
        seen.add(selected)
        # Resolve directory aliases without losing the invocation's venv prefix
        # when bin/python itself links to an interpreter in a different runtime.
        parent = selected.parent.resolve(strict=True)
        for prefix in (parent, parent.parent):
            if (prefix / "pyvenv.cfg").is_file():
                roots.add(prefix)
        if not selected.is_symlink():
            break
        target = Path(os.readlink(selected))
        selected = target if target.is_absolute() else selected.parent / target
    if any(Path.home().resolve().is_relative_to(prefix) for prefix in roots):
        raise BuildError("Interpreter requires a dedicated runtime prefix, not private HOME or its ancestors")
    return sorted(roots)


def simulation_profile(scratch, python, *, local_signing=False):
    def q(path):
        return json.dumps(str(Path(path).resolve()))
    developer = subprocess.check_output(["xcode-select", "-p"], text=True).strip()
    shared_git = Path(git("rev-parse", "--git-common-dir"))
    if not shared_git.is_absolute():
        shared_git = ROOT / shared_git
    runtime = python.resolve().parent.parent
    readable = [ROOT, scratch, shared_git, *interpreter_read_roots(python)]
    # Foundation atomic writes stage beside the OS user temp directory. Permit
    # only replacement folders bearing this run's unique executable prefix.
    native_temp = Path(subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()).resolve() / "TemporaryItems"
    fixture_prefix = "wispqa-" + scratch.name.rsplit("-", 1)[-1]
    replacements = json.dumps("^" + re.escape(str(native_temp)) + "/NSIRD_" + re.escape(fixture_prefix) + "-[^/]+_[^/]+(/|$)")
    executables = [python, "/bin/bash", "/bin/sh", "/bin/ls", "/bin/cat", "/bin/pwd",
                   "/bin/echo", "/bin/rm", "/usr/bin/dirname", "/usr/bin/mktemp",
                   "/usr/bin/env", "/usr/bin/git", "/usr/bin/swiftc", "/usr/bin/swift", "/usr/bin/xcrun",
                   "/usr/bin/osacompile", "/usr/bin/head", "/usr/bin/tail", "/usr/bin/wc",
                   "/usr/bin/uname", "/usr/bin/sandbox-exec", "/usr/bin/openssl"]
    # Publisher verification/signing tests use the system crypto executable on
    # synthetic files only. Network, Apple events and private HOME remain denied.
    if local_signing:
        executables.append("/usr/bin/codesign")
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)", "(deny appleevent-send)",
        "(deny process-exec)",
        "(allow process-exec " + " ".join(f"(literal {q(p)})" for p in executables)
        + f" (subpath {q(developer)}) (subpath {q(scratch)}) (subpath {q(runtime)}))",
        "(deny file-write*)",
        f'(allow file-write* (subpath {q(scratch)}) (subpath {q(STATE)}) (literal "/dev/null") (literal {q(native_temp)}) (regex {replacements}))',
        f"(deny file-read-data (subpath {q(Path.home())}))",
        "(allow file-read-data " + " ".join(f"(subpath {q(p)})" for p in readable) + ")",
    ])


def simulation_tests(runner, python, *, allow_dirty=False, native_only=False):
    # Applying Seatbelt twice is prohibited on macOS. Exercise the real external
    # venv read/deny boundary as a mandatory isolated probe before full QA enters
    # its outer sandbox; its fixture setup touches only disposable test paths.
    runner.run("external-venv-sandbox-contract", [python, "-B",
               ROOT / "tests/build_pipeline/pipeline_checks.py",
               "PipelineTests.check_external_virtualenv_sandbox", "-q"], timeout=180)
    runner.run("local-signing-sandbox-contract", [python, "-B",
               ROOT / "tests/build_pipeline/pipeline_checks.py",
               "PipelineTests.check_local_signing_sandbox", "-q"], timeout=180)
    with tempfile.TemporaryDirectory(prefix="wisp-build-qa-") as tmp:
        scratch = Path(tmp).resolve()
        report = scratch / "simulation.json"
        # Real socket ownership checks need their own least-privilege boundary;
        # never relax the outer no-network Simulation profile for other tests.
        native_report = scratch / 'native-peer.json'
        native_command = [python, '-B', SUPPORT / 'native_peer_gate.py', '--expected-sha',
                          git('rev-parse','HEAD'), '--report', native_report]
        if allow_dirty:native_command.append('--allow-dirty')
        native_code,_=runner.run('native-disposable-peer-security', native_command, timeout=240,check=False)
        if native_report.is_file():shutil.copyfile(native_report,runner.logs/'native-peer.json')
        if native_code or not native_report.is_file():raise BuildError('Native peer security gate failed; inspect native-peer.json')
        from native_peer_gate import validate as validate_native_peer
        validate_native_peer(json.loads(native_report.read_text()),git('rev-parse','HEAD'),allow_dirty=allow_dirty)
        command = ["sandbox-exec", "-p", simulation_profile(scratch, python), python, "-B",
                   SUPPORT / "simulation.py", "--expected-sha", git("rev-parse", "HEAD"),
                   "--profile", "full", "--report", report]
        if allow_dirty:
            command.append("--allow-dirty")
        if native_only:
            command.append("--only-native")
        code, _ = runner.run("simulation-qa", command, env=dict(runner.env, TMPDIR=str(scratch), WISP_BUILD_FIXTURE_PREFIX="wispqa-" + scratch.name.rsplit("-", 1)[-1],
                             PEER_TEST_GATE_REPORT=str(native_report), PEER_TEST_GATE_SHA256=digest(native_report)),
                             timeout=2400, check=False)
        if report.is_file():
            shutil.copyfile(report, runner.logs / "simulation-qa.json")
        if code or not report.is_file():
            raise BuildError("Simulation QA failed; inspect simulation-qa.log and simulation-qa.json")
        validate_simulation(json.loads(report.read_text()), git("rev-parse", "HEAD"),
                            allow_dirty=allow_dirty, native_only=native_only)


def validate_simulation(report, commit, *, allow_dirty=False, native_only=False):
    totals = report.get("totals", {})
    if (report.get("schema_version") != 3 or report.get("status") != "PASS"
        or report.get("candidate_sha") != commit or report.get("ending_sha") != commit
        or not report.get("sha_stable") or report.get("profiles") != ["full"]
        or report.get("safety_mode") != "offline"
        or report.get("native_mode") != ("only" if native_only else "included")
        or not totals.get("gates") or totals.get("failed_gates") != 0
        or totals.get("blocked_gates") != 0 or totals.get("passed_gates") != totals.get("gates")
        or (not allow_dirty and (report.get("dirty_allowed") or not report.get("worktree_clean")))):
        raise BuildError("Simulation QA evidence does not cover this candidate")
    if not native_only:
        from native_peer_gate import MODULE, validate as validate_native_peer
        gates=[r for r in report.get('results',[]) if r.get('name')==MODULE]
        if len(gates)!=1:raise BuildError('Missing mandatory native peer evidence')
        if any(gates[0].get(k)!=v for k,v in {'status':'PASS','returncode':0,'passed':9,'failed':0,'skipped':0}.items()):
            raise BuildError('Incomplete mandatory native peer evidence')
        try:
            validate_native_peer(json.loads(gates[0]['stdout'])['native_gate'],commit,allow_dirty=allow_dirty)
        except (KeyError,TypeError,ValueError):
            raise BuildError('Invalid mandatory native peer evidence') from None


def production_backend_source(source):
    # The development checkout fallback must never ship in the packaged app.
    pattern = (r'\n        let devRoot = URL\(fileURLWithPath: "[^"\n]+"\)\n'
               r'        if FileManager\.default\.fileExists\(atPath: devRoot\.appendingPathComponent\("service/main\.py"\)\.path\) \{\n'
               r'            return devRoot\n        \}\n')
    result, count = re.subn(pattern, "\n", source)
    if count != 1:
        raise BuildError("Review the production BackendManager staging rule: expected one development fallback")
    return result


def stage_swift_sources():
    stage = STATE / "swift-source"
    if stage.exists():
        shutil.rmtree(stage)
    for name in filter(None, git("ls-files", "-z", "--", "app").split("\0")):
        source = ROOT / name
        if source.is_symlink() or not source.is_file():
            raise BuildError("Unsafe Swift source entry: " + name)
        target = stage / Path(name).relative_to("app")
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "app/Sources/WispApp/BackendManager.swift":
            target.write_text(production_backend_source(source.read_text()))
        else:
            shutil.copyfile(source, target)
    return stage


def build_swift(runner: Runner) -> Path:
    stage = stage_swift_sources()
    args = ["swift", "build", "--package-path", stage, "--scratch-path", STATE / "swift",
            "--disable-sandbox", "-c", "release", "--arch", "arm64"]
    for source, replacement in ((stage, "/wisp/source/app"), (ROOT, "/wisp/source")):
        for flag in ("-debug-prefix-map", "-file-prefix-map"):
            args.extend(["-Xswiftc", flag, "-Xswiftc", f"{source}={replacement}"])
    runner.run("swift-release-build", args, timeout=1800)
    _, log = runner.run("swift-binary-path", [*args, "--show-bin-path"])
    binary = Path(log.read_text().strip()) / "WispApp"
    if not binary.is_file():
        raise BuildError("Swift did not produce WispApp")
    return binary


def plist(meta: dict) -> dict:
    return {
        "CFBundleName": "Wisp", "CFBundleDisplayName": "Wisp", "CFBundleIdentifier": CONFIG["bundle_identifier"],
        "CFBundleVersion": meta["build_number"], "CFBundleShortVersionString": meta["version"],
        "CFBundleExecutable": "Wisp", "CFBundleIconFile": "AppIcon", "CFBundlePackageType": "APPL",
        "LSUIElement": True, "LSMinimumSystemVersion": CONFIG["minimum_macos"], "NSHighResolutionCapable": True,
        "NSCalendarsFullAccessUsageDescription": "Wisp reads your calendar to show upcoming events and remind you before they start.",
        "NSCalendarsUsageDescription": "Wisp reads your calendar to show upcoming events and remind you before they start.",
        "NSRemindersFullAccessUsageDescription": "Wisp mirrors the reminders you set into the macOS Reminders app.",
        "NSRemindersUsageDescription": "Wisp mirrors the reminders you set into the macOS Reminders app.",
        "NSAppleEventsUsageDescription": "Wisp reads Mail and Notes for summaries and runs email, message and Terminal actions you approve.",
        "NSContactsUsageDescription": "Wisp reads your contacts to show names instead of phone numbers in messages.",
        "WispSourceCommit": meta["commit"], "WispPreviewBuild": meta["dirty"],
    }


def source_files() -> list[Path]:
    files = [name for name in git("ls-files", "-z", "--", "service").split("\0") if name]
    if not files or not files[0]:
        raise BuildError("No tracked backend source")
    result = []
    for name in files:
        p = ROOT / name
        if p.is_symlink() or not p.is_file() or p.suffix in (".db", ".pyc"):
            raise BuildError(f"Unsafe backend source entry: {name}")
        result.append(p)
    return result


def assemble(runner, binary, destination, meta, offline=False):
    bundle = destination / "Wisp.app"
    contents = bundle / "Contents"
    resources = contents / "Resources"
    backend = resources / "backend"
    (contents / "MacOS").mkdir(parents=True)
    backend.mkdir(parents=True)
    shutil.copy2(binary, contents / "MacOS" / "Wisp")
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump(plist(meta), output, sort_keys=True)
    iconset = runner.logs / "Wisp.iconset"
    runner.run("generate-icon", ["swift", "-module-cache-path", STATE / "module-cache", ROOT / "scripts/make_icon.swift", iconset])
    runner.run("compile-icon", ["iconutil", "-c", "icns", iconset, "-o", resources / "AppIcon.icns"])
    shutil.rmtree(iconset)
    for path in source_files():
        dest = backend / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    embedded = backend / ".venv"
    from bootstrap_uv import unpack_runtime
    unpack_runtime(CONFIG, STATE, embedded)
    python = embedded / "bin" / "python3"
    # This is an owned copy, not the uv-managed bootstrap interpreter.
    for marker in embedded.glob("lib/python*/EXTERNALLY-MANAGED"):
        marker.unlink()
    runner.run("verify-runtime-prefix", [python, "-I", "-B", "-c",
        f"import sys; from pathlib import Path; assert Path(sys.prefix).resolve() == Path({str(embedded)!r}).resolve(), sys.prefix"])
    runner.uv("bundle-dependencies", ["pip", "sync", "--python", python, "--prefix", embedded, "--no-python-downloads", "--require-hashes",
              "--only-binary", ":all:", "--strict", "--link-mode", "copy", "--default-index", "https://pypi.org/simple",
              SUPPORT / "requirements-runtime.lock"], offline=offline)
    # Console scripts encode the build prefix and are not used by BackendManager (-m uvicorn).
    for entry in (embedded / "bin").iterdir():
        if entry.name not in ("python", "python3", "python" + ".".join(CONFIG["python"].split(".")[:2])):
            if entry.is_dir():
                raise BuildError("Unexpected directory in runtime bin")
            entry.unlink()
    if not (embedded / "bin/python").exists():
        (embedded / "bin/python").symlink_to("python3")
    docs = resources / "Documentation"
    docs.mkdir()
    for name in ("CHANGELOG.md", "docs/build-release.md"):
        shutil.copyfile(ROOT / name, docs / Path(name).name)
    shutil.copyfile(SUPPORT / "requirements-runtime.lock", docs / "requirements-runtime.lock")
    json_write(resources / "build-info.json", meta)
    # Normalize modes before signing so umask does not change candidate archives.
    for path in bundle.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)
    # RECORD paths for installed wheel files remain intact; keep upstream licenses.
    runner.run("bundle-dependency-inventory", [python, "-I", "-B", "-c",
        "import importlib.metadata as m,json; print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License','')} for d in m.distributions()],key=lambda d:d['name']),indent=2))"])
    shutil.copyfile(runner.logs / "bundle-dependency-inventory.log", destination / "dependencies.json")
    return bundle


def is_macho(path):
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as f:
        return f.read(4) in MACH_MAGICS


def validate_structure(bundle: Path, meta: dict):
    bundle = bundle.resolve()
    required = ["Contents/MacOS/Wisp", "Contents/Info.plist", "Contents/Resources/AppIcon.icns",
                "Contents/Resources/build-info.json", "Contents/Resources/backend/.venv/bin/python",
                "Contents/Resources/backend/service/main.py", "Contents/Resources/backend/service/config/models.yaml",
                "Contents/Resources/backend/service/config/policy.yaml",
                "Contents/Resources/backend/service/skills/bundled/interview-me/SKILL.md",
                "Contents/Resources/backend/service/skills/bundled/idea-refine/SKILL.md"]
    for name in required:
        if not (bundle / name).is_file():
            raise BuildError(f"Missing bundle resource: {name}")
    actual = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    for key, value in plist(meta).items():
        if actual.get(key) != value:
            raise BuildError(f"Info.plist mismatch: {key}")
    for path in bundle.rglob("*"):
        if path.is_symlink():
            if os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(bundle) or not path.exists():
                raise BuildError(f"Non-relocatable symlink: {path.relative_to(bundle)}")
        elif path.is_file() and (path.stat().st_mode & 0o6000):
            raise BuildError("setuid/setgid file in artifact")
        if path.name in (".DS_Store", "__pycache__", "pyvenv.cfg", ".env", ".git") or path.suffix in (".db", ".pyc", ".p12", ".p8"):
            raise BuildError(f"Unwanted bundle file: {path.relative_to(bundle)}")
    for name in ("Contents/MacOS/Wisp", "Contents/Resources/backend/.venv/bin/python"):
        if not os.access(bundle / name, os.X_OK):
            raise BuildError(f"Bundle program is not executable: {name}")


def validate_native(runner, bundle, meta):
    rows = []
    minimum = tuple(map(int, CONFIG["minimum_macos"].split(".")))
    for i, path in enumerate(p for p in bundle.rglob("*") if is_macho(p)):
        _, log = runner.run(f"macho-{i}", ["lipo", "-archs", path])
        if "arm64" not in log.read_text().split():
            raise BuildError(f"Missing arm64 slice: {path.relative_to(bundle)}")
        _, log = runner.run(f"load-commands-{i}", ["otool", "-arch", "arm64", "-l", path])
        text = log.read_text()
        versions = re.findall(r"\bminos (\d+(?:\.\d+)+)", text)
        versions += re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+)+)", text)
        if not versions:
            raise BuildError(f"No macOS deployment target: {path.relative_to(bundle)}")
        for version in versions:
            nums = tuple(map(int, version.split(".")))
            if (nums + (0, 0, 0))[:3] > (minimum + (0, 0, 0))[:3]:
                raise BuildError(f"{path.relative_to(bundle)} requires macOS {version}, above advertised minimum")
        _, log = runner.run(f"dylibs-{i}", ["otool", "-arch", "arm64", "-L", path])
        # otool -L prints a dylib's own LC_ID_DYLIB before its dependencies.
        # That identity is not a library loaded at runtime (wheel builders often
        # leave an absolute build label there). Validate actual loads separately.
        identity = re.search(r"cmd LC_ID_DYLIB\s+cmdsize \d+\s+name (.+?) \(offset \d+\)", text)
        dependencies = []
        for index, line in enumerate(log.read_text().splitlines()[1:]):
            dependency = line.strip().split(" (", 1)[0]
            if index == 0 and identity and dependency == identity.group(1):
                continue
            dependencies.append(dependency)
            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise BuildError(f"Nonportable dylib dependency {dependency}")
        rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (\S+)", text)
        absolute_rpaths = [entry for entry in rpaths if entry.startswith("/")]
        # Wheel builds can retain an unused search path on a leaf dylib. It
        # cannot affect this library's declared system-only dependencies.
        system_only = all(dep.startswith(("/usr/lib/", "/System/Library/")) for dep in dependencies)
        if absolute_rpaths and not system_only:
            raise BuildError(f"Absolute rpath with non-system dependencies in {path.relative_to(bundle)}")
        rows.append({"path": str(path.relative_to(bundle)), "minimum_macos": versions,
                     "install_id": identity.group(1) if identity else None,
                     "dependencies": dependencies, "unused_rpaths": absolute_rpaths})
    if len(rows) < 2:
        raise BuildError("Expected native app and Python interpreter")
    json_write(runner.logs / "native-inventory.json", rows)


def scan_host_paths(bundle):
    """Reject this host's checkout/home paths in every distributed Mach-O."""
    source = (ROOT / "app/Sources/WispApp/BackendManager.swift").read_text()
    fallback = re.search(r'let devRoot = URL\(fileURLWithPath: "([^"\n]+)"\)', source)
    markers = {str(ROOT), str(STATE), str(Path(tempfile.gettempdir()).resolve()) + "/"}
    if fallback:
        markers.add(fallback.group(1))
        parts = Path(fallback.group(1)).parts
        if len(parts) >= 3 and parts[1] in ("Users", "home"):
            markers.add(str(Path(*parts[:3])) + "/")
    for path in bundle.rglob("*"):
        if is_macho(path):
            scoped = markers | ({"/Users/", "/home/", "/private/var/folders/"}
                                if path == bundle / "Contents/MacOS/Wisp" else set())
            needles = {value.encode(encoding) for value in scoped for encoding in ("utf-8", "utf-16-le", "utf-16-be")}
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                raise BuildError(f"Host build path in distributable Mach-O: {path.relative_to(bundle)}")


def prepare_native_for_signing(runner, bundle):
    for i, path in enumerate(p for p in bundle.rglob("*") if is_macho(p)):
        runner.run(f"strip-debug-{i}", ["/usr/bin/strip", "-S", path])
        _, log = runner.run(f"signing-load-commands-{i}", ["otool", "-arch", "arm64", "-l", path])
        commands = log.read_text()
        rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (\S+)", commands)
        absolute = [rpath for rpath in rpaths if rpath.startswith("/")]
        if absolute:
            loads = re.findall(r"cmd LC_(?:LOAD|LOAD_WEAK|REEXPORT|LOAD_UPWARD)_DYLIB\s+cmdsize \d+\s+name (.+?) \(offset \d+\)", commands)
            if not all(dep.startswith(("/usr/lib/", "/System/Library/")) for dep in loads):
                raise BuildError("Cannot remove an absolute search path used by non-system dependencies")
            for rpath in absolute:
                runner.run(f"remove-unused-rpath-{i}", ["/usr/bin/install_name_tool", "-delete_rpath", rpath, path])
    scan_host_paths(bundle)


def signing_targets(bundle):
    targets = [path for path in bundle.rglob("*") if not path.is_symlink() and
               (is_macho(path) or (path.is_dir() and path.suffix in (".app", ".framework", ".xpc", ".appex", ".bundle")))]
    return sorted(targets, key=lambda path: (-len(path.parts), str(path))) + [bundle]


def verify_bundle_signature(bundle, expected, runner=None, prefix="bundle-signature"):
    if expected not in ("ad-hoc", "Developer ID Application"):
        raise BuildError("Unsupported candidate signature state")
    commands = (["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle],
                ["/usr/bin/codesign", "--display", "--verbose=4", bundle])
    description = ""
    for i, command in enumerate(commands):
        if runner is not None:
            _, log = runner.run(f"{prefix}-{i}", command)
            description = log.read_text()
        else:
            result = subprocess.run([str(value) for value in command], env=clean_env(),
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise BuildError("Strict bundle signature verification failed: " + result.stderr[-1000:])
            description = result.stdout + result.stderr
    if expected == "ad-hoc":
        valid = "Signature=adhoc" in description and "Authority=" not in description and "TeamIdentifier=not set" in description
    else:
        valid = "Authority=Developer ID Application:" in description and "TeamIdentifier=not set" not in description
    if not valid:
        raise BuildError("Actual bundle signature differs from provenance")


def sign_adhoc(runner, bundle):
    scan_host_paths(bundle)
    for i, target in enumerate(signing_targets(bundle)):
        runner.run(f"adhoc-sign-{i}", ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", target])
    verify_bundle_signature(bundle, "ad-hoc", runner, "adhoc-verification")


def relocation_smoke(runner, bundle):
    # Spaces and a changed prefix catch scripts/symlinks that retain a build path.
    with tempfile.TemporaryDirectory(prefix="relocation-", dir=runner.logs) as tmp:
        moved = Path(tmp) / "A Folder With Spaces" / "Wisp.app"
        moved.parent.mkdir()
        shutil.copytree(bundle, moved, symlinks=True)
        backend = moved / "Contents/Resources/backend"
        if guarded(runner, "relocated-backend-smoke", backend / ".venv/bin/python", "smoke", "-", root=backend):
            raise BuildError("Relocated bundled backend import failed")


def inventory(bundle):
    bundle = Path(bundle)
    root = bundle.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise BuildError("Bundle inventory root must be a directory")
    rows = {".": {"directory": True, "mode": stat.S_IMODE(root.st_mode)}}
    for p in sorted(bundle.rglob("*")):
        rel = str(p.relative_to(bundle))
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode):
            rows[rel] = {"symlink": os.readlink(p)}
        elif stat.S_ISDIR(info.st_mode):
            rows[rel] = {"directory": True, "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            rows[rel] = {"sha256": digest(p), "mode": stat.S_IMODE(info.st_mode),
                         "size": info.st_size}
        else:
            raise BuildError("Unsupported bundle inventory entry")
    return rows


def archive(bundle: Path, output: Path, epoch: int):
    # Stable order, timestamps, permissions; store symlinks as symlinks for Finder.
    stamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in [bundle, *sorted(bundle.rglob("*"))]:
            name = str(p.relative_to(bundle.parent)) + ("/" if p.is_dir() and not p.is_symlink() else "")
            info = zipfile.ZipInfo(name, stamp)
            info.create_system = 3
            info.external_attr = p.lstat().st_mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = os.readlink(p).encode() if p.is_symlink() else b"" if p.is_dir() else p.read_bytes()
            z.writestr(info, data)
    with zipfile.ZipFile(output) as z:
        if z.testzip():
            raise BuildError("ZIP integrity check failed")


def notes(meta):
    previous = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--match", "v[0-9]*", "--abbrev=0", "HEAD^"], capture_output=True, text=True)
    base = previous.stdout.strip() if previous.returncode == 0 else None
    history = git("log", "--no-merges", "--format=- %s (%h)", f"{base}..HEAD" if base else "HEAD")
    return f"# Wisp {meta['version']} (build {meta['build_number']})\n\nSource: `{meta['commit']}`\n\nApple Silicon, macOS {meta['minimum_macos']} or newer. Requires a separately installed oMLX server and models.\n\n" + ("LOCAL PREVIEW — uncommitted source; do not distribute.\n\n" if meta['dirty'] else "") + f"Changes since {base or 'repository creation'}:\n\n{history}\n"


def finalize(runner, bundle, destination, meta, toolchain):
    scan_host_paths(bundle)
    verify_bundle_signature(bundle, "ad-hoc")
    json_write(destination / "bundle-manifest.json", inventory(bundle))
    (destination / "release-notes.md").write_text(notes(meta))
    provenance = {"schema": 1, "source": meta, "toolchain": toolchain, "inputs": json.loads((SUPPORT / "locks.json").read_text()),
                  "signature": "ad-hoc", "notarized": False, "tests": runner.records,
                  "reproducibility": "Pinned inputs and normalized archive; bit-for-bit Swift/SDK and signed outputs are not guaranteed."}
    prefix = f"Wisp-{meta['version']}-{meta['build_number']}-arm64" + ("-preview" if meta["dirty"] else "-candidate")
    zip_path = destination / (prefix + ".zip")
    archive(bundle, zip_path, meta["source_epoch"])
    distribution_roundtrip(runner, zip_path, bundle, meta)
    shutil.copyfile(runner.logs / "simulation-qa.json", destination / "simulation-qa.json")
    provenance["simulation_sha256"] = digest(destination / "simulation-qa.json")
    json_write(destination / "provenance.json", provenance)
    checksums(destination)



def distribution_roundtrip(runner, zip_path, bundle, meta, notarized=False,
                           archive_descriptor=None):
    with tempfile.TemporaryDirectory(prefix="distribution-", dir=runner.logs) as tmp:
        target = Path(tmp)
        archive_input = zip_path
        pass_fds = ()
        if archive_descriptor is not None:
            info = os.fstat(archive_descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise BuildError("Bound distribution archive is not a regular file")
            archive_input = f"/dev/fd/{archive_descriptor}"
            pass_fds = (archive_descriptor,)
        command = ["ditto", "-x", "-k", archive_input, target]
        if pass_fds:
            runner.run("extract-distribution", command, pass_fds=pass_fds)
        else:
            runner.run("extract-distribution", command)
        extracted = target / "Wisp.app"
        validate_structure(extracted, meta)
        if inventory(extracted) != inventory(bundle):
            raise BuildError("Archive roundtrip changed files, permissions, or symlinks")
        scan_host_paths(extracted)
        verify_bundle_signature(extracted, "Developer ID Application" if notarized else "ad-hoc", runner, "distributed-signature")
        if notarized:
            runner.run("verify-distributed-ticket", ["xcrun", "stapler", "validate", extracted])
            runner.run("verify-distributed-gatekeeper", ["spctl", "--assess", "--type", "execute", extracted])
        relocation_smoke(runner, extracted)


def _descriptor_bytes(descriptor):
    result, offset = bytearray(), 0
    while block := os.pread(descriptor, 1_048_576, offset):
        result.extend(block)
        offset += len(block)
    return bytes(result)


def _descriptor_digest(descriptor):
    value, offset = hashlib.sha256(), 0
    while block := os.pread(descriptor, 1_048_576, offset):
        value.update(block)
        offset += len(block)
    return value.hexdigest()


def _open_regular_asset(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise BuildError("Release assets must be single-link regular files")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    after = os.fstat(descriptor)
    if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)):
        os.close(descriptor)
        raise BuildError("Release asset identity changed while opening")
    return descriptor


class BoundReleaseAssets:
    """No-follow descriptors shared by verification and release upload."""
    def __init__(self, destination):
        self.destination = Path(destination).resolve(strict=True)
        self.descriptors = {}
        self.digests = {}
        self.identities = {}
        self.expected_files = set()

    def __enter__(self):
        try:
            for path in sorted(self.destination.iterdir()):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise BuildError("Artifact directories may not contain top-level symlinks")
                if stat.S_ISREG(info.st_mode):
                    descriptor = _open_regular_asset(path)
                    self.descriptors[path.name] = descriptor
                    self.digests[path.name] = _descriptor_digest(descriptor)
                    opened = os.fstat(descriptor)
                    self.identities[path.name] = (opened.st_dev, opened.st_ino)
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors.clear()
        self.identities.clear()

    def __exit__(self, *_args):
        self.close()

    def read_bytes(self, name):
        try:
            self.assert_paths_unchanged()
            return _descriptor_bytes(self.descriptors[name])
        except KeyError:
            raise BuildError(f"Missing verified release asset: {name}") from None

    def read_text(self, name):
        try:
            return self.read_bytes(name).decode()
        except UnicodeError:
            raise BuildError(f"Invalid text release asset: {name}") from None

    def contains_markers(self, name, markers):
        self.assert_paths_unchanged()
        data, carry, overlap, offset = b"", b"", max(map(len, markers)) - 1, 0
        descriptor = self.descriptors[name]
        while data := os.pread(descriptor, 1_048_576, offset):
            sample = carry + data
            if any(marker in sample for marker in markers):
                return True
            carry, offset = sample[-overlap:], offset + len(data)
        return False

    def assert_paths_unchanged(self):
        actual = set()
        for path in self.destination.iterdir():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BuildError("Artifact directories may not contain top-level symlinks")
            if stat.S_ISREG(info.st_mode):
                actual.add(path.name)
                if self.identities.get(path.name) != (info.st_dev, info.st_ino):
                    raise BuildError(f"Release asset identity changed: {path.name}")
        if actual != set(self.descriptors):
            raise BuildError("Release asset set changed after binding")
        for name, descriptor in self.descriptors.items():
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or self.identities.get(name) != (info.st_dev, info.st_ino)
                    or _descriptor_digest(descriptor) != self.digests.get(name)):
                raise BuildError(f"Release asset content changed: {name}")

    def write_checksums(self, *, exclude=()):
        if "SHA256SUMS" in self.descriptors or (self.destination / "SHA256SUMS").exists():
            raise BuildError("Artifact checksum manifest already exists")
        exclude = set(exclude)
        if not exclude.issubset({"release-notes.md"}) or not exclude.issubset(self.descriptors):
            raise BuildError("Invalid unpublished checksum exclusion")
        self.assert_paths_unchanged()
        body = "".join(f"{self.digests[name]}  {name}\n"
                       for name in sorted(self.descriptors) if name not in exclude).encode()
        path = self.destination / "SHA256SUMS"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644)
        try:
            offset = 0
            while offset < len(body):
                count = os.write(descriptor, body[offset:])
                if count <= 0:
                    raise BuildError("Could not write artifact checksum manifest")
                offset += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        descriptor = _open_regular_asset(path)
        self.descriptors[path.name] = descriptor
        self.digests[path.name] = _descriptor_digest(descriptor)
        opened = os.fstat(descriptor)
        self.identities[path.name] = (opened.st_dev, opened.st_ino)
        self.assert_paths_unchanged()

    def validate_checksums(self, *, allow_unpublished_notes=False):
        self.assert_paths_unchanged()
        if "SHA256SUMS" not in self.descriptors:
            raise BuildError("Missing artifact checksum manifest")
        self.expected_files.clear()
        try:
            lines = self.read_text("SHA256SUMS").splitlines()
            for line in lines:
                sha, name = line.split("  ", 1)
                if (Path(name).name != name or not re.fullmatch(r"[a-f0-9]{64}", sha)
                        or name in self.expected_files):
                    raise BuildError("Malformed checksum manifest")
                self.expected_files.add(name)
                if self.digests.get(name) != sha:
                    raise BuildError(f"Artifact checksum mismatch: {name}")
        except ValueError:
            raise BuildError("Malformed checksum manifest") from None
        actual = set(self.descriptors) - {"SHA256SUMS"}
        required = actual - ({"release-notes.md"} if allow_unpublished_notes else set())
        if self.expected_files != required or not any(
                name.endswith(".zip") for name in self.expected_files):
            raise BuildError("Incomplete artifact checksum manifest")

    def upload_assets(self):
        self.assert_paths_unchanged()
        return [(name, release_asset_content_type(name), self.descriptors[name], self.digests[name],
                 os.fstat(self.descriptors[name]).st_size)
                for name in sorted(self.descriptors) if name != "release-notes.md"]


def checksums(destination, *, exclude=()):
    destination = Path(destination)
    manifest = destination / "SHA256SUMS"
    if manifest.exists() or manifest.is_symlink():
        info = manifest.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BuildError("Artifact checksum manifest must be a regular file")
        manifest.unlink()
    with BoundReleaseAssets(destination) as assets:
        assets.write_checksums(exclude=exclude)


def release_asset_content_type(name):
    explicit = {
        ".json": "application/json",
        ".md": "text/markdown; charset=utf-8",
        ".zip": "application/zip",
    }
    if name == "SHA256SUMS":
        return "text/plain; charset=utf-8"
    return explicit.get(Path(name).suffix.lower(),
                        mimetypes.guess_type(name)[0] or "application/octet-stream")


class GitHubReleaseUploader:
    """Upload explicitly named assets while reading only retained descriptors."""
    retryable_statuses = {429, 500, 502, 503, 504}

    def __init__(self, repository, release_id, token, *, attempts=3,
                 connection_factory=http.client.HTTPSConnection, sleeper=time.sleep):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not str(release_id).isdigit() or int(release_id) <= 0 or not token
                or attempts < 1):
            raise BuildError("Invalid GitHub release upload configuration")
        self.repository = repository
        self.release_id = str(release_id)
        self.token = token
        self.attempts = attempts
        self.connection_factory = connection_factory
        self.sleeper = sleeper

    def upload(self, name, content_type, descriptor, expected_digest, expected_size):
        if Path(name).name != name or not name or "\0" in name:
            raise BuildError("Invalid release asset name")
        if (not re.fullmatch(r"[a-f0-9]{64}", expected_digest)
                or not isinstance(expected_size, int) or expected_size < 0):
            raise BuildError("Invalid bound release asset identity")
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size != expected_size):
            raise BuildError("Release upload source is not a bound regular file")
        endpoint = (f"/repos/{self.repository}/releases/{self.release_id}/assets?"
                    + urlencode({"name": name}, quote_via=quote))
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "Content-Length": str(expected_size),
            "Content-Type": content_type,
            "User-Agent": "wisp-release-pipeline",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        last_error = None
        try:
            for attempt in range(self.attempts):
                connection = None
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_size != expected_size):
                        raise BuildError("Release upload source changed")
                    body = tempfile.SpooledTemporaryFile(max_size=8 * 1_048_576, mode="w+b")
                    digest, offset = hashlib.sha256(), 0
                    while block := os.pread(descriptor, 1_048_576, offset):
                        body.write(block)
                        digest.update(block)
                        offset += len(block)
                    if offset != expected_size or digest.hexdigest() != expected_digest:
                        body.close()
                        raise BuildError("Release upload source content changed")
                    body.seek(0)
                    connection = self.connection_factory("uploads.github.com", timeout=120)
                    with body:
                        connection.request("POST", endpoint, body=body, headers=headers)
                    response = connection.getresponse()
                    response_body = response.read(65_537)
                    if 200 <= response.status < 300 and len(response_body) <= 65_536:
                        return
                    if response.status not in self.retryable_statuses:
                        raise BuildError(f"GitHub rejected release asset {name} ({response.status})")
                    last_error = f"HTTP {response.status}"
                except BuildError:
                    raise
                except (OSError, http.client.HTTPException) as exc:
                    last_error = type(exc).__name__
                finally:
                    if connection is not None:
                        connection.close()
                if attempt + 1 < self.attempts:
                    self.sleeper(min(2 ** attempt, 8))
        finally:
            pass
        raise BuildError(f"GitHub release asset upload failed after retries: {name} ({last_error})")


def verify_artifacts(destination, bound_assets=None, *, publication_asset_set=False):
    if bound_assets is None:
        with BoundReleaseAssets(destination) as assets:
            return verify_artifacts(destination, bound_assets=assets,
                                    publication_asset_set=publication_asset_set)
    destination = Path(destination)
    assets = bound_assets
    if assets.destination != destination.resolve(strict=True):
        raise BuildError("Verified release assets belong to a different directory")
    qa_markers = (b"wisp-managed-summary-qa-v1", b"Wisp Summary QA",
                  b"com.wisp.app.summary-qa", b"com.wisp.summary-qa.inference")
    artifact_root = destination.resolve(strict=True)
    for path in destination.rglob("*"):
        try:
            info = path.lstat()
        except OSError as exc:
            raise BuildError("Could not inspect candidate filesystem entries") from exc
        if stat.S_ISLNK(info.st_mode):
            try:
                target_text = os.readlink(path)
                target = path.resolve(strict=True)
            except OSError as exc:
                raise BuildError("Artifact symlink is dangling or unreadable") from exc
            if (os.path.isabs(target_text) or not target.is_relative_to(artifact_root)):
                raise BuildError("Artifact symlink escapes the verified directory")
            if ("Summary QA" in path.name
                    or any(marker in os.fsencode(target_text) for marker in qa_markers)):
                raise BuildError("Managed-live QA identity is excluded from production verification")
            if target.is_file():
                try:
                    from managed_live_qa.staging import contains_marker
                    contains_qa = contains_marker(target, qa_markers)
                except OSError as exc:
                    raise BuildError("Could not inspect candidate for QA identity") from exc
                if contains_qa:
                    raise BuildError("Managed-live QA content is excluded from production verification")
            elif not target.is_dir():
                raise BuildError("Artifact symlink target is unsupported")
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if "Summary QA" in path.name:
            raise BuildError("Managed-live QA identity is excluded from production verification")
        try:
            from managed_live_qa.staging import contains_marker
            contains_qa = (assets.contains_markers(path.name, qa_markers)
                           if path.parent == destination and path.name in assets.descriptors
                           else contains_marker(path, qa_markers))
        except OSError as exc:
            raise BuildError("Could not inspect candidate for QA identity") from exc
        if contains_qa:
            raise BuildError("Managed-live QA content is excluded from production verification")
    marker = destination / "artifact-kind.json"
    if "artifact-kind.json" in assets.descriptors:
        value = json.loads(assets.read_text("artifact-kind.json"))
        if value.get("artifact_kind") == "wisp-managed-summary-qa-v1":
            raise BuildError("Managed-live QA artifacts are excluded from production verification")
    assets.validate_checksums(allow_unpublished_notes=publication_asset_set)
    provenance = json.loads(assets.read_text("provenance.json"))
    meta = provenance["source"]
    if assets.digests.get("simulation-qa.json") != provenance.get("simulation_sha256"):
        raise BuildError("Missing or mismatched Simulation QA evidence")
    validate_simulation(json.loads(assets.read_text("simulation-qa.json")), meta["commit"],
                        allow_dirty=meta["dirty"])
    bundle = destination / "Wisp.app"
    validate_structure(bundle, meta)
    if inventory(bundle) != json.loads(assets.read_text("bundle-manifest.json")):
        raise BuildError("Bundle contents differ from the verified manifest")
    signature = provenance.get("signature")
    if signature == "ad-hoc" and provenance.get("notarized") is not False:
        raise BuildError("Ad-hoc candidates must not claim notarization")
    scan_host_paths(bundle)
    verify_bundle_signature(bundle, signature)


def main():
    if sys.version_info < (3, 9):
        print("Wisp build driver requires Python 3.9 or newer", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", nargs="?", default="all", choices=("all", "doctor", "lock", "bootstrap", "test", "swift", "verify", "release", "qa-assemble"))
    p.add_argument("--dry-run", action="store_true", help="Print plan without downloads, writes or external release calls")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--allow-dirty", action="store_true", help="Local preview only; never eligible for publication")
    p.add_argument("--strict-toolchain", action="store_true")
    p.add_argument("--build-number")
    p.add_argument("--output", type=Path)
    p.add_argument("--test-python", type=Path, help="Existing interpreter for test/swift auditing only")
    p.add_argument("--qa-runtime", type=Path,
                   help="Closed Python runtime tree for the separate managed-QA artifact")
    p.add_argument("--qa-runtime-inventory-sha256",
                   help="Independently reviewed canonical inventory digest for --qa-runtime")
    p.add_argument("--production-sha",
                   help="Exact production candidate SHA qualified by managed QA")
    args = p.parse_args()
    if args.dry_run:
        print(json.dumps({"command": args.command, "toolchain": CONFIG, "stages": ["preflight and lock validation", "pinned runtime and hash-checked binary dependencies", "pipeline contracts + isolated Python suites + Swift contracts", "Swift release build and icon generation", "tracked source + relocatable runtime assembly", "ad-hoc sealed native/resource/relocation verification", "normalized ZIP, checksums, dependency inventory, provenance, release notes"],
                          "external_release": "release requires protected CI, an exact version tag, credentials, and explicit dispatch", "installs_app": False, "offline": args.offline}, indent=2))
        return 0
    if args.command == "qa-assemble":
        if args.allow_dirty:
            print("BUILD FAILED: managed QA requires clean committed source", file=sys.stderr)
            return 1
        if not args.output:
            print("BUILD FAILED: qa-assemble requires --output", file=sys.stderr)
            return 1
        if not args.qa_runtime or not args.qa_runtime_inventory_sha256 or not args.production_sha:
            print("BUILD FAILED: qa-assemble requires --qa-runtime, "
                  "--qa-runtime-inventory-sha256, and --production-sha",
                  file=sys.stderr)
            return 1
        try:
            from managed_live_qa.staging import build as build_managed_qa
            output = args.output.resolve()
            if not output.is_relative_to((ROOT / "dist").resolve()):
                raise BuildError("QA output must be below this checkout's dist/")
            meta = metadata(False, args.build_number)
            source_start = source_fingerprint()
            build_managed_qa(ROOT, output, meta["commit"], args.production_sha,
                             runtime_source=args.qa_runtime,
                             runtime_inventory_sha256=args.qa_runtime_inventory_sha256)
            if (git("rev-parse", "HEAD") != meta["commit"]
                    or git("status", "--porcelain", "--untracked-files=all")
                    or source_fingerprint() != source_start):
                raise BuildError("QA source changed while sealing the artifact")
            print(f"Built separate managed QA application: {output / 'Wisp Summary QA.app'}")
            return 0
        except (BuildError, OSError, ValueError) as exc:
            print(f"BUILD FAILED: {exc}", file=sys.stderr)
            return 1
    runner = Runner()
    try:
        if args.command == "verify":
            if not args.output:
                raise BuildError("verify requires --output <artifact-directory>")
            verify_artifacts(args.output.resolve())
            return 0
        if args.command == "release":
            from release import release
            release(runner, args)
            return 0
        if args.command in ("all", "bootstrap") or (args.command == "test" and not args.test_python):
            check_locks()
        toolchain = doctor(runner, args.strict_toolchain, args.offline)
        if args.command == "doctor":
            check_locks()
        elif args.command == "lock":
            lock(runner, args.offline)
        elif args.command == "swift":
            python = args.test_python.absolute() if args.test_python else bootstrap(runner, args.offline)
            simulation_tests(runner, python, allow_dirty=args.allow_dirty, native_only=True)
            build_swift(runner)
        else:
            if args.test_python and args.command != "test":
                raise BuildError("--test-python is only allowed for test auditing, never packaging")
            meta = metadata(args.allow_dirty, args.build_number) if args.command == "all" else None
            source_start = source_fingerprint() if meta else None
            if meta:
                meta["tracked_source_sha256"] = source_start
            python = args.test_python.absolute() if args.test_python else bootstrap(runner, args.offline)
            if args.command == "bootstrap":
                return 0
            simulation_tests(runner, python, allow_dirty=args.allow_dirty)
            if args.command == "all":
                binary = build_swift(runner)
                output = (args.output or ROOT / "dist" / f"{meta['version']}-{meta['build_number']}-{meta['commit'][:12]}" ).resolve()
                if not output.is_relative_to((ROOT / "dist").resolve()) or output == (ROOT / "dist").resolve():
                    raise BuildError("Output must be a new directory below this checkout's dist/")
                output.mkdir(parents=True, exist_ok=False)
                bundle = assemble(runner, binary, output, meta, args.offline)
                validate_structure(bundle, meta)
                prepare_native_for_signing(runner, bundle)
                validate_native(runner, bundle, meta)
                sign_adhoc(runner, bundle)
                relocation_smoke(runner, bundle)
                if git("rev-parse", "HEAD") != meta["commit"] or source_fingerprint() != source_start:
                    raise BuildError("Source changed during build; discard candidate and rerun from a stable checkout")
                finalize(runner, bundle, output, meta, toolchain)
                verify_artifacts(output)
                print(f"Verified candidate: {output}")
        print(f"Diagnostics: {runner.logs}")
        return 0
    except (BuildError, OSError, subprocess.CalledProcessError) as exc:
        print(f"BUILD FAILED: {exc}\nDiagnostics: {runner.logs}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # The release extension imports this module; keep one BuildError/type identity.
    sys.modules["pipeline"] = sys.modules[__name__]
    raise SystemExit(main())
