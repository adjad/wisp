"""Separately approval-gated publisher signing; no production identity is included.

Supply an already-open private RSA key descriptor with --key-fd. Key bytes never
appear in argv, environment, output, or temporary files. This command is not run
by ordinary artifact builds. The approved publisher supplies a bounded metadata
lifetime and monotonically increasing release sequence.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/mac-mini"))
from artifact_signature import canonical, sign_statement, statement, verify_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--key-fd", type=int, required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--issued-at", type=int, required=True)
    parser.add_argument("--expires-at", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.approved or os.environ.get("WISP_MINI_SIGNING_APPROVED") != "true":
            raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
            raise ValueError()
        def git(*parts):
            return subprocess.run(["/usr/bin/git", "-C", str(ROOT), *parts], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
        if git("rev-parse", "HEAD") != args.source_sha.encode() or git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError()
        with open(args.bundle, "rb") as source:
            raw = source.read(256 * 1024 * 1024 + 1)
        from node_prep import validate_bundle_bytes
        bundle_sha256 = hashlib.sha256(raw).hexdigest()
        if bundle_sha256 != args.bundle_sha256:
            raise ValueError()
        manifest, _ = validate_bundle_bytes(raw, bundle_sha256, args.source_sha)
        if manifest.get("artifact_type") != "offline-runtime" or manifest.get("provenance", {}).get("strict_toolchain") is not True:
            raise ValueError()
        value = statement(key_id=args.key_id, source_commit=args.source_sha,
                          bundle_sha256=bundle_sha256, release_sequence=args.sequence,
                          issued_at=args.issued_at, expires_at=args.expires_at)
        if git("rev-parse", "HEAD") != args.source_sha.encode() or git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError()
        signed = sign_statement(value, private_key_fd=args.key_fd)
        public = Path(args.public_key).read_text(encoding="ascii")
        trust = canonical({"schema_version": 1, "keys": {args.key_id: public}})
        verify_artifact(raw, signed, trust, trust_sha256=hashlib.sha256(trust).hexdigest(),
                        key_id=args.key_id, source_commit=args.source_sha,
                        bundle_sha256=bundle_sha256, release_sequence=args.sequence, now=int(time.time()))
        # Never overwrite an existing statement or follow an output symlink.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(signed + b"\n")
            output.flush()
            os.fsync(output.fileno())
        print('{"schema_version":1,"status":"signed"}')
        return 0
    except Exception:
        print('{"schema_version":1,"status":"blocked","error":"publisher_signing_refused"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
