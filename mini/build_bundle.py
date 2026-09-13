"""Build an explicit, deterministic mini-only archive; never read runtime state."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

FILES = ("__init__.py", "__main__.py", "http.py", "gateway.py", "node.py", "protocol.py",
         "store.py", "requirements.txt", "README.md")


def build(output):
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "bundle.json").read_text())
    blobs = {"mini/" + name: (root / name).read_bytes() for name in FILES}
    manifest["files"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                         for path, data in sorted(blobs.items())]
    blobs["mini/bundle.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with Path(output).open("xb") as dest, gzip.GzipFile(fileobj=dest, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path, data in sorted(blobs.items()):
                entry = tarfile.TarInfo(path)
                entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
                archive.addfile(entry, io.BytesIO(data))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
