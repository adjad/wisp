"""Version-two closed-runtime staging used by the public staging shim."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess
import tempfile
import uuid

from . import ARTIFACT_KIND

PRODUCTION_TARGET = "916fafa68e738396374176bb337db5e1f20ee46c"
SOURCE_ALLOWLIST_V2 = (
    "service/credential_pipe.py", "service/config/quarantine.py",
    "service/tools/email_tools.py", "service/tools/imessage_tools.py",
    "service/tools/message_digest.py", "service/assistant/brief.py",
    "service/inference/omlx_client.py", "service/inference/attributed_transport.py",
    "service/inference/local_peer.py", "service/inference/inference_errors.py",
    "app/Sources/WispApp/BackendCredentials.swift",
)
SUPPORT_FILES = ("secure_backend.py", "secure_harness.py", "manifest-v2.json",
                 "native_pipe_main.swift")
STARTUP_HOOKS = {"sitecustomize.py", "usercustomize.py", "pyvenv.cfg"}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def qa_quarantine(source):
    text = source.decode()
    old = 'return Path(self.home()) / ".moe"'
    new = 'return Path(self.home()) / ".wisp-summary-qa"'
    if text.count(old) != 1:
        raise ValueError("unexpected quarantine transform input")
    return text.replace(old, new).encode()


def _inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise ValueError("QA runtime/source tree is not owner-safe")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("QA runtime/source tree contains unsupported entries")
        if path.name in STARTUP_HOOKS or path.suffix == ".pth":
            raise ValueError("QA runtime contains Python startup hooks")
        result[path.relative_to(root).as_posix()] = sha(path)
    if not result:
        raise ValueError("QA runtime/source inventory is empty")
    return result


def _copy_runtime(runtime_source, target):
    runtime_source = Path(runtime_source).resolve(strict=True)
    _inventory(runtime_source)
    shutil.copytree(runtime_source, target, symlinks=False)
    python = target / "bin/python3"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("QA runtime requires executable bin/python3")
    return _inventory(target)


def assemble(root, destination, artifact_sha, production_sha=PRODUCTION_TARGET, *,
             runtime_source):
    if destination.exists():
        raise ValueError("QA destination must not exist")
    hex40 = __import__("re").fullmatch
    if not hex40(r"[0-9a-f]{40}", artifact_sha) or not hex40(
            r"[0-9a-f]{40}", production_sha):
        raise ValueError("artifact and production SHAs must be exact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
    temporary.chmod(0o700)
    try:
        stage = temporary / "Wisp Summary QA.app/Contents/Resources/qa"
        source_root = stage / "source"
        source_root.mkdir(parents=True)
        original_hashes = {}
        for relative in SOURCE_ALLOWLIST_V2:
            source = root / relative
            if not source.is_file():
                raise ValueError(f"missing reviewed QA source: {relative}")
            if relative.endswith("BackendCredentials.swift"):
                data = qa_credentials(source.read_bytes())
            elif relative == "service/config/quarantine.py":
                data = qa_quarantine(source.read_bytes())
            else:
                data = source.read_bytes()
            target = source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            original_hashes[relative] = sha(source)
        support = Path(__file__).resolve().parent
        for name in SUPPORT_FILES:
            shutil.copy2(support / name, source_root / name)
        for package in ("service", "service/tools", "service/assistant",
                        "service/inference", "service/config", "service/memory"):
            init = source_root / package / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text('"""QA-only staged package."""\n')
        shutil.copy2(support / "secure_backend.py", source_root / "service/main.py")
        shutil.copy2(support / "secure_harness.py", source_root / "service/qa_harness.py")
        shutil.copy2(support / "manifest-v2.json", source_root / "service/qa-manifest.json")
        runtime_inventory = _copy_runtime(runtime_source, stage / "runtime")
        source_inventory = _inventory(source_root)
        (stage / "qa-source-inventory.json").write_text(
            json.dumps(source_inventory, sort_keys=True, separators=(",", ":")) + "\n")
        (stage / "qa-runtime-inventory.json").write_text(
            json.dumps(runtime_inventory, sort_keys=True, separators=(",", ":")) + "\n")
        build_manifest = {"schema_version": 2, "artifact_kind": ARTIFACT_KIND,
            "bundle_id": "com.wisp.app.summary-qa", "artifact_sha": artifact_sha,
            "production_sha": production_sha, "ipc_protocol": "anonymous-pipes-v1",
            "inference_endpoint": "http://127.0.0.1:8000",
            "exclusive_proof_protocol": "unavailable",
            "python_relative": "runtime/bin/python3",
            "manifest_sha256": sha(source_root / "service/qa-manifest.json"),
            "source_inventory_sha256": canonical_digest(source_inventory),
            "runtime_inventory_sha256": canonical_digest(runtime_inventory)}
        (stage / "qa-build-manifest.json").write_text(json.dumps(
            build_manifest, sort_keys=True, separators=(",", ":")) + "\n")
        (temporary / "artifact-kind.json").write_text(json.dumps({
            "artifact_kind": ARTIFACT_KIND, "bundle_id": "com.wisp.app.summary-qa",
            "artifact_sha": artifact_sha, "production_sha": production_sha,
            "build_manifest_sha256": sha(stage / "qa-build-manifest.json"),
            "source_originals": original_hashes,
            "production_release_eligible": False}, sort_keys=True) + "\n")
        if any(path.is_symlink() for path in temporary.rglob("*")):
            raise ValueError("QA assembly may not contain links")
        os.replace(temporary, destination)
        return destination / "Wisp Summary QA.app/Contents/Resources/qa"
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C",
             "CLANG_MODULE_CACHE_PATH": "/private/tmp/wisp-summary-qa-clang-cache",
             "SWIFT_MODULECACHE_PATH": "/private/tmp/wisp-summary-qa-swift-cache"})
    if result.returncode:
        raise ValueError("QA native build failed")


def macho_canonical_sha(path):
    data = bytearray(path.read_bytes())
    if len(data) < 32 or struct.unpack_from("<I", data)[0] != 0xFEEDFACF:
        raise ValueError("QA native executable must be thin Mach-O 64")
    commands, offset, signature = struct.unpack_from("<I", data, 16)[0], 32, None
    for _ in range(commands):
        if offset + 8 > len(data):
            raise ValueError("Malformed Mach-O commands")
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            raise ValueError("Malformed Mach-O commands")
        if command == 0x1D:
            if size < 16 or signature is not None:
                raise ValueError("Malformed Mach-O signature command")
            data_offset, data_size = struct.unpack_from("<II", data, offset + 8)
            if data_offset + data_size > len(data):
                raise ValueError("Malformed Mach-O signature range")
            signature = (data_offset, data_size)
            data[offset + 8:offset + 16] = b"\0" * 8
        offset += size
    if signature is None:
        raise ValueError("QA native executable has no signature command")
    start, size = signature
    return hashlib.sha256(data[:start] + data[start + size:]).hexdigest()


def build(root, destination, artifact_sha, production_sha=PRODUCTION_TARGET, *,
          runtime_source):
    if destination.exists():
        raise ValueError("QA destination must not exist")
    work = destination.parent / f".{destination.name}.build-{uuid.uuid4().hex}"
    try:
        stage = assemble(root, work, artifact_sha, production_sha,
                         runtime_source=runtime_source)
        app = work / "Wisp Summary QA.app"
        executable = app / "Contents/MacOS/Wisp Summary QA"
        executable.parent.mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as output:
            plistlib.dump({"CFBundleDevelopmentRegion": "en",
                "CFBundleExecutable": "Wisp Summary QA",
                "CFBundleIdentifier": "com.wisp.app.summary-qa",
                "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": "Wisp Summary QA",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1", "LSBackgroundOnly": True}, output, sort_keys=True)
        _run(["/usr/bin/xcrun", "--sdk", "macosx", "swiftc", "-O",
              str(stage / "source/native_pipe_main.swift"),
              str(stage / "source/app/Sources/WispApp/BackendCredentials.swift"),
              "-framework", "Security", "-framework", "LocalAuthentication",
              "-o", str(executable)])
        executable.chmod(0o500)
        _run(["/usr/bin/codesign", "--force", "--sign", "-", str(executable)])
        build_path = stage / "qa-build-manifest.json"
        manifest = json.loads(build_path.read_text())
        attestation = {"schema_version": 1, "artifact_sha": artifact_sha,
            "production_sha": production_sha, "build_manifest_sha256": sha(build_path),
            "manifest_sha256": manifest["manifest_sha256"],
            "source_inventory_sha256": manifest["source_inventory_sha256"],
            "runtime_inventory_sha256": manifest["runtime_inventory_sha256"],
            "native_sha256": macho_canonical_sha(executable)}
        (stage / "qa-attestation.json").write_text(json.dumps(
            attestation, sort_keys=True, separators=(",", ":")) + "\n")
        _run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
        if macho_canonical_sha(executable) != attestation["native_sha256"]:
            raise ValueError("QA native identity changed while sealing bundle")
        os.replace(work, destination)
        return destination / "Wisp Summary QA.app"
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
