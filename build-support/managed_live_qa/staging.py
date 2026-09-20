"""Deterministic source assembly for the separate managed-live QA artifact."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from . import ARTIFACT_KIND, QA_PORT

SOURCE_ALLOWLIST = (
    "service/tools/email_tools.py", "service/tools/imessage_tools.py",
    "service/tools/message_digest.py", "service/tools/timeranges.py",
    "service/tools/registry.py", "service/assistant/brief.py",
    "service/inference/omlx_client.py", "service/inference/attributed_transport.py",
    "service/inference/local_peer.py", "service/credential_pipe.py",
    "service/config/credentials.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assemble(root: Path, destination: Path, candidate_sha: str) -> Path:
    if destination.exists():
        raise ValueError("QA destination must not exist")
    stage = destination / "Wisp Summary QA.app" / "Contents" / "Resources" / "qa"
    (stage / "service").mkdir(parents=True)
    hashes = {}
    for rel in SOURCE_ALLOWLIST:
        source = root / rel
        if not source.is_file():
            raise ValueError(f"missing reviewed QA source: {rel}")
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[rel] = sha(source)
    support = Path(__file__).resolve().parent
    for name in ("backend.py", "harness.py", "manifest.json", "native_main.swift"):
        shutil.copy2(support / name, stage / name)
    (stage / "service" / "__init__.py").write_text('"""QA-only staged service."""\n')
    shutil.copy2(support / "backend.py", stage / "service" / "main.py")
    shutil.copy2(support / "harness.py", stage / "service" / "qa_harness.py")
    manifest = json.loads((support / "manifest.json").read_text())
    manifest.update(candidate_sha=candidate_sha, artifact_kind=ARTIFACT_KIND,
                    port=QA_PORT, source_hashes=hashes,
                    retained_transport_sources=["service/credential_pipe.py",
                        "service/inference/attributed_transport.py", "service/inference/local_peer.py"])
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    (stage / "qa-build-manifest.json").write_text(canonical + "\n")
    (destination / "artifact-kind.json").write_text(json.dumps({
        "artifact_kind": ARTIFACT_KIND, "candidate_sha": candidate_sha,
        "manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "production_release_eligible": False}, sort_keys=True) + "\n")
    return stage


def reject_for_production(path: Path) -> None:
    marker = path / "artifact-kind.json"
    if marker.is_file() and json.loads(marker.read_text()).get("artifact_kind") == ARTIFACT_KIND:
        raise ValueError("managed-live QA artifacts are never production release candidates")
