"""Build an explicit, deterministic mini-only archive; never read runtime state."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import subprocess
import re

FILES = ("__init__.py", "__main__.py", "http.py", "gateway.py", "node.py", "protocol.py",
         "store.py", "resources.py", "runtime.py", "adapters.py", "backup.py",
         "resource-contract.json", "RUNBOOK.md", "requirements.txt", "README.md")


def build(output, *, payload=None, provenance=None, expected_sha=None):
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "bundle.json").read_text())
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if expected_sha is not None and sha != expected_sha:
        raise ValueError("Candidate SHA mismatch")
    manifest.pop("base_commit", None)
    manifest.update(source_commit=sha, artifact_type="source")
    blobs = {"mini/" + name: (root / name).read_bytes() for name in FILES}
    modes = {name: 0o600 for name in blobs}
    if payload is not None:
        if not provenance or provenance.get("source_commit") != sha:
            raise ValueError("Missing candidate provenance")
        manifest.update(artifact_type="offline-runtime", provenance=provenance)
        for file in sorted(Path(payload).rglob("*")):
            if file.is_dir():
                continue
            if file.is_symlink() and not file.resolve().is_relative_to(Path(payload).resolve()):
                raise ValueError("External runtime link")
            name = "mini/payload/" + file.relative_to(payload).as_posix()
            blobs[name] = file.read_bytes()
            modes[name] = 0o700 if file.stat().st_mode & 0o111 else 0o600
    manifest["files"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "mode": modes[path]}
                         for path, data in sorted(blobs.items())]
    blobs["mini/bundle.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with Path(output).open("xb") as dest, gzip.GzipFile(fileobj=dest, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path, data in sorted(blobs.items()):
                entry = tarfile.TarInfo(path)
                entry.size, entry.mode, entry.mtime = len(data), modes.get(path, 0o600), 0
                archive.addfile(entry, io.BytesIO(data))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
