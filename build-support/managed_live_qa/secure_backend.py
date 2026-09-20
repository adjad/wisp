"""Attested, anonymous-pipe backend primitives for Summary QA.

The shipped QA manifest remains fail-closed because oMLX exposes no reviewed
exclusive-session lease.  This module deliberately performs no credential read
or inference when that proof is unavailable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct

FRAME_MAGIC = b"WQARPT1\n"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
BUILD_KEYS = frozenset({"schema_version", "artifact_kind", "bundle_id",
    "artifact_sha", "production_sha", "ipc_protocol", "inference_endpoint",
    "exclusive_proof_protocol", "python_relative", "manifest_sha256",
    "source_inventory_sha256", "runtime_inventory_sha256", "source_archive_sha256"})


class IntegrityError(RuntimeError):
    """Fixed-code integrity failure; never includes source or credential data."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            value.update(block)
    return value.hexdigest()


def _safe_file(root: Path, relative: str, expected: str) -> bool:
    try:
        if (not isinstance(relative, str) or relative.startswith("/")
                or ".." in Path(relative).parts or not HEX64.fullmatch(expected)):
            return False
        path = root / relative
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                and info.st_nlink == 1 and not info.st_mode & 0o022
                and path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
                and sha(path) == expected)
    except OSError:
        return False


def verify_inventory(root: Path, inventory_path: Path) -> tuple[bool, str]:
    """Verify exact regular-file membership and all digests under ``root``."""
    try:
        values = json.loads(inventory_path.read_text())
        if (not isinstance(values, dict) or not values
                or any(not _safe_file(root, key, value) for key, value in values.items())):
            return False, ""
        actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.is_file() and not path.is_symlink()}
        if actual != set(values) or any(path.is_symlink() for path in root.rglob("*")):
            return False, ""
        return True, hashlib.sha256(canonical(values)).hexdigest()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False, ""


def attest_stage(stage: Path, environment: dict[str, str]) -> dict:
    """Validate the signed parent's expected manifests before stdin is read."""
    try:
        build_path = stage / "qa-build-manifest.json"
        attestation_path = stage / "qa-attestation.json"
        if (sha(build_path) != environment.get("WISP_QA_BUILD_MANIFEST_SHA256")
                or sha(attestation_path) != environment.get("WISP_QA_ATTESTATION_SHA256")):
            raise IntegrityError("attestation_mismatch")
        build = json.loads(build_path.read_text())
        attestation = json.loads(attestation_path.read_text())
        if set(build) != BUILD_KEYS or build.get("schema_version") != 2:
            raise IntegrityError("build_manifest_invalid")
        if (not HEX40.fullmatch(build.get("artifact_sha", ""))
                or not HEX40.fullmatch(build.get("production_sha", ""))
                or build.get("ipc_protocol") != "anonymous-pipes-v1"
                or build.get("exclusive_proof_protocol") not in {
                    "unavailable", "server-lease-v1"}):
            raise IntegrityError("build_manifest_invalid")
        expected_keys = {"schema_version", "artifact_sha", "production_sha",
            "build_manifest_sha256", "manifest_sha256",
            "source_inventory_sha256", "runtime_inventory_sha256",
            "source_archive_sha256", "native_sha256"}
        if (set(attestation) != expected_keys or attestation.get("schema_version") != 1
                or attestation.get("artifact_sha") != build["artifact_sha"]
                or attestation.get("production_sha") != build["production_sha"]
                or attestation.get("build_manifest_sha256") != sha(build_path)):
            raise IntegrityError("attestation_mismatch")
        for key in expected_keys - {"schema_version", "artifact_sha", "production_sha"}:
            if not HEX64.fullmatch(str(attestation.get(key, ""))):
                raise IntegrityError("attestation_mismatch")
        source_ok, source_digest = verify_inventory(
            stage / "source", stage / "qa-source-inventory.json")
        runtime_ok, runtime_digest = verify_inventory(
            stage / "runtime", stage / "qa-runtime-inventory.json")
        if (not source_ok or not runtime_ok
                or source_digest != build["source_inventory_sha256"]
                or runtime_digest != build["runtime_inventory_sha256"]
                or source_digest != attestation["source_inventory_sha256"]
                or runtime_digest != attestation["runtime_inventory_sha256"]
                or sha(stage / "qa-source.pyz") != build["source_archive_sha256"]
                or build["source_archive_sha256"] != attestation["source_archive_sha256"]
                or sha(stage / "source/service/qa-manifest.json")
                != build["manifest_sha256"]
                or build["manifest_sha256"] != attestation["manifest_sha256"]):
            raise IntegrityError("inventory_mismatch")
        return {"build": build, "attestation": attestation}
    except IntegrityError:
        raise
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise IntegrityError("attestation_unavailable") from None


def derive_session_key(credential: str, nonce: str, *, artifact_sha: str,
                       production_sha: str, parent_pid: int, child_pid: int,
                       attestation_sha: str) -> bytes:
    if (not HEX64.fullmatch(credential) or not HEX64.fullmatch(nonce)
            or not HEX40.fullmatch(artifact_sha) or not HEX40.fullmatch(production_sha)
            or not HEX64.fullmatch(attestation_sha)
            or type(parent_pid) is not int or parent_pid <= 0
            or type(child_pid) is not int or child_pid <= 0):
        raise IntegrityError("launch_binding_invalid")
    context = canonical({"protocol": "anonymous-pipes-v1", "nonce": nonce,
        "artifact_sha": artifact_sha, "production_sha": production_sha,
        "parent_pid": parent_pid, "child_pid": child_pid,
        "attestation_sha256": attestation_sha})
    return hmac.new(bytes.fromhex(credential), context, hashlib.sha256).digest()


def encode_authenticated_report(report: dict, session_key: bytes) -> bytes:
    body = canonical(report)
    if len(session_key) != 32 or len(body) > 32_768:
        raise IntegrityError("report_frame_invalid")
    mac = hmac.new(session_key, body, hashlib.sha256).digest()
    return FRAME_MAGIC + struct.pack("!I", len(body)) + body + mac


def decode_authenticated_report(frame: bytes, session_key: bytes) -> dict:
    if (len(session_key) != 32 or len(frame) < 44 or frame[:8] != FRAME_MAGIC
            or len(frame) > 32_812):
        raise IntegrityError("report_frame_invalid")
    size = struct.unpack("!I", frame[8:12])[0]
    if size > 32_768 or len(frame) != 12 + size + 32:
        raise IntegrityError("report_frame_invalid")
    body, supplied = frame[12:12 + size], frame[12 + size:]
    expected = hmac.new(session_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise IntegrityError("report_authentication_failed")
    try:
        result = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise IntegrityError("report_frame_invalid") from None
    if not isinstance(result, dict):
        raise IntegrityError("report_frame_invalid")
    return result


def main() -> int:
    try:
        stage_reference = os.environ.pop("WISP_QA_STAGE_ROOT", "")
        if not re.fullmatch(r"/dev/fd/[0-9]+", stage_reference):
            raise IntegrityError("attestation_unavailable")
        stage = Path(stage_reference)
        evidence = attest_stage(stage, dict(os.environ))
        if evidence["build"]["exclusive_proof_protocol"] != "server-lease-v1":
            return 78
        # No verifier is supplied because oMLX has no reviewed lease protocol.
        # Adding one requires an independently reviewed server contract.
        return 78
    except IntegrityError:
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
