"""Transactional builder for the separate, never-production Summary QA app."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

from . import ARTIFACT_KIND, QA_PORT

SOURCE_ALLOWLIST = (
    "service/credential_pipe.py",
    "service/tools/email_tools.py", "service/tools/imessage_tools.py",
    "service/tools/message_digest.py", "service/assistant/brief.py",
    "service/inference/omlx_client.py", "service/inference/attributed_transport.py",
    "service/inference/local_peer.py", "service/inference/inference_errors.py",
    "app/Sources/WispApp/BackendCredentials.swift",
)
SUPPORT_FILES = ("backend.py", "harness.py", "manifest.json", "native_main.swift")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def qa_credentials(source):
    text = source.decode()
    replacements = {
        'let directory = home + "/.moe"':
            'let directory = home + "/.wisp-summary-qa"',
        'static let service = "com.wisp.inference"':
            'static let service = "com.wisp.summary-qa.inference"',
        '        "mini-inference": "WISP_MINI_INFERENCE_KEY",\n'
        '        "mini-node": "WISP_MINI_NODE_KEY",\n': "",
        'return [NSHomeDirectory() + "/.moe/provisioning/wisp-keychain-helper", '
        '"/Applications/Wisp.app"]': "return [Bundle.main.bundleURL.path]",
        '        let allowed = role == "primary" ? Set(accounts.values) :\n'
        '            (role == "gateway" ? Set(["WISP_LOCAL_OMLX_KEY", '
        '"WISP_MINI_INFERENCE_KEY"]) : Set(["WISP_MINI_NODE_KEY"]))\n'
        '        guard ["primary", "gateway", "node"].contains(role), pid > 0,\n':
            '        let allowed = Set(accounts.values)\n'
            '        guard role == "primary", pid > 0,\n'}
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise ValueError("unexpected BackendCredentials transform input")
        text = text.replace(old, new)
    if any(value in text for value in (
            "com.wisp.inference", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")):
        raise ValueError("QA credential scope was not reduced")
    return text.encode()


def assemble(root, destination, candidate_sha, *, python_executable=None):
    if destination.exists():
        raise ValueError("QA destination must not exist")
    if len(candidate_sha) != 40 or any(ch not in "0123456789abcdef" for ch in candidate_sha):
        raise ValueError("candidate SHA must be exact")
    python_executable = (python_executable or Path(sys.executable)).resolve(strict=True)
    info = python_executable.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002
            or info.st_uid not in (0, os.getuid())
            or os.access(python_executable, os.W_OK) and info.st_uid != os.getuid()):
        raise ValueError("QA Python interpreter is not qualified")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
    temporary.chmod(0o700)
    try:
        stage = temporary / "Wisp Summary QA.app/Contents/Resources/qa"
        stage.mkdir(parents=True)
        source_hashes = {}
        for relative in SOURCE_ALLOWLIST:
            source = root / relative
            if not source.is_file():
                raise ValueError(f"missing reviewed QA source: {relative}")
            data = qa_credentials(source.read_bytes()) if relative.endswith(
                "BackendCredentials.swift") else source.read_bytes()
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            source_hashes[relative] = sha(source)
        support = Path(__file__).resolve().parent
        for name in SUPPORT_FILES:
            shutil.copy2(support / name, stage / name)
        for package in ("service", "service/tools", "service/assistant", "service/inference"):
            init = stage / package / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text('"""QA-only staged package."""\n')
        shutil.copy2(support / "backend.py", stage / "service/main.py")
        shutil.copy2(support / "harness.py", stage / "service/qa_harness.py")
        shutil.copy2(support / "manifest.json", stage / "service/qa-manifest.json")
        staged_hashes = {path.relative_to(stage).as_posix(): sha(path)
                         for path in sorted(stage.rglob("*")) if path.is_file()}
        manifest = json.loads((support / "manifest.json").read_text())
        manifest.update(candidate_sha=candidate_sha, source_hashes=source_hashes,
                        staged_hashes=staged_hashes,
                        python_executable=str(python_executable),
                        python_sha256=sha(python_executable),
                        retained_transport_sources=[
                            "service/inference/omlx_client.py",
                            "service/inference/attributed_transport.py",
                            "service/inference/local_peer.py"])
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        (stage / "qa-build-manifest.json").write_text(canonical + "\n")
        (temporary / "artifact-kind.json").write_text(json.dumps({
            "artifact_kind": ARTIFACT_KIND, "bundle_id": "com.wisp.app.summary-qa",
            "candidate_sha": candidate_sha,
            "manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "production_release_eligible": False}, sort_keys=True) + "\n")
        if any(path.is_symlink() for path in temporary.rglob("*")):
            raise ValueError("QA assembly may not contain links")
        os.replace(temporary, destination)
        return destination / "Wisp Summary QA.app/Contents/Resources/qa"
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C",
             "CLANG_MODULE_CACHE_PATH": "/private/tmp/wisp-summary-qa-clang-cache",
             "SWIFT_MODULECACHE_PATH": "/private/tmp/wisp-summary-qa-swift-cache"})
    if result.returncode:
        raise ValueError("QA native build failed")


def build(root, destination, candidate_sha, *, python_executable=None):
    if destination.exists():
        raise ValueError("QA destination must not exist")
    work = destination.parent / f".{destination.name}.build-{uuid.uuid4().hex}"
    try:
        stage = assemble(root, work, candidate_sha, python_executable=python_executable)
        app = work / "Wisp Summary QA.app"
        executable = app / "Contents/MacOS/Wisp Summary QA"
        executable.parent.mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as output:
            plistlib.dump({"CFBundleDevelopmentRegion": "en",
                "CFBundleExecutable": "Wisp Summary QA",
                "CFBundleIdentifier": "com.wisp.app.summary-qa",
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": "Wisp Summary QA", "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
                "LSBackgroundOnly": True}, output, sort_keys=True)
        run(["/usr/bin/xcrun", "--sdk", "macosx", "swiftc", "-O",
             str(stage / "native_main.swift"),
             str(stage / "app/Sources/WispApp/BackendCredentials.swift"),
             "-framework", "Security", "-framework", "LocalAuthentication",
             "-o", str(executable)])
        executable.chmod(0o500)
        run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
        os.replace(work, destination)
        return destination / "Wisp Summary QA.app"
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def contains_marker(path, markers):
    overlap, carry = max(map(len, markers)) - 1, b""
    with path.open("rb") as source:
        while block := source.read(1_048_576):
            sample = carry + block
            if any(marker in sample for marker in markers):
                return True
            carry = sample[-overlap:]
    return False


def reject_for_production(path):
    markers = (b"wisp-managed-summary-qa-v1", b"Wisp Summary QA",
               b"com.wisp.app.summary-qa", b"com.wisp.summary-qa.inference")
    for item in path.rglob("*"):
        if "Summary QA" in item.name:
            raise ValueError("Managed QA artifacts are never production release inputs")
        if item.is_file() and not item.is_symlink() and contains_marker(item, markers):
            raise ValueError("Managed QA artifacts are never production release inputs")


# The original PR56 staging functions above remain as review history for the
# five-file repair.  Artifact construction is deliberately delegated to the
# closed-runtime v2 implementation; the legacy TCP/native templates are never
# copied by this entry point.
from .secure_staging import (  # noqa: E402
    SOURCE_ALLOWLIST_V2 as SOURCE_ALLOWLIST,
    assemble,
    build,
)
