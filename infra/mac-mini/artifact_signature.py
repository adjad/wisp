"""Publisher-authenticated mini artifacts; no built-in production signing identity.

The caller must obtain trust_sha256, key_id, source SHA and release sequence from
its independently reviewed authorization, never from the artifact itself. Verify
and consume BEFORE credential access. A failed attempt after consumption requires
new publisher metadata with a larger sequence; replay is never silently retried.
"""
import base64
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


class SignatureRefused(ValueError):
    pass


def refused():
    raise SignatureRefused("artifact_authentication_refused")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                refused()
            result[key] = value
        return result
    try:
        if not isinstance(raw, bytes) or len(raw) > 65536:
            refused()
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError):
        refused()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def hex_value(value, length):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{%d}" % length, value) is not None


def statement(*, key_id, source_commit, bundle_sha256, release_sequence, issued_at, expires_at):
    result = {"schema_version": 1, "purpose": "wisp-mini-offline-runtime",
              "algorithm": "rsa-sha256", "key_id": key_id,
              "source_commit": source_commit, "bundle_sha256": bundle_sha256,
              "release_sequence": release_sequence, "issued_at": issued_at, "expires_at": expires_at}
    validate_statement(result)
    return result


def validate_statement(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "purpose", "algorithm", "key_id", "source_commit", "bundle_sha256", "release_sequence", "issued_at", "expires_at"}:
        refused()
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["purpose"] != "wisp-mini-offline-runtime" or value["algorithm"] != "rsa-sha256"
            or not hex_value(value["key_id"], 64) or not hex_value(value["source_commit"], 40)
            or not hex_value(value["bundle_sha256"], 64)
            or any(type(value[k]) is not int for k in ("release_sequence", "issued_at", "expires_at"))
            or not 0 < value["release_sequence"] < 2**63
            or not 0 <= value["issued_at"] < value["expires_at"] < 2**63
            or value["expires_at"] - value["issued_at"] > 7 * 86400):
        refused()


def openssl(argv, *, data=None, pass_fds=()):
    try:
        result = subprocess.run(["/usr/bin/openssl", *argv], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                pass_fds=pass_fds, timeout=30,
                                env={"PATH": "/usr/bin:/bin"})
        if result.returncode:
            refused()
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        refused()


@dataclass(frozen=True)
class VerifiedArtifact:
    key_id: str
    source_commit: str
    bundle_sha256: str
    release_sequence: int
    statement_sha256: str
    expires_at: int


def verify_artifact(raw, envelope_bytes, trust_bytes, *, trust_sha256, key_id,
                    source_commit, bundle_sha256, release_sequence, now):
    """Verify immutable bytes using independently pinned public trust and approval.

    Trust format: {"schema_version":1,"keys":{SHA256_OF_PEM: PUBLIC_PEM}}.
    Public keys are not secrets. The exact trust-file SHA must be independently
    authorized; no default trust root or production identity is supplied here.
    """
    if (not isinstance(raw, bytes) or len(raw) > 256 * 1024 * 1024
            or not isinstance(trust_bytes, bytes) or not hex_value(trust_sha256, 64) or digest(trust_bytes) != trust_sha256
            or not hex_value(key_id, 64) or not hex_value(source_commit, 40)
            or not hex_value(bundle_sha256, 64) or digest(raw) != bundle_sha256
            or type(release_sequence) is not int or type(now) is not int):
        refused()
    trust, envelope = strict_json(trust_bytes), strict_json(envelope_bytes)
    if (not isinstance(trust, dict) or set(trust) != {"schema_version", "keys"}
            or type(trust["schema_version"]) is not int or trust["schema_version"] != 1
            or not isinstance(trust["keys"], dict) or not trust["keys"]
            or len(trust["keys"]) > 32
            or not isinstance(envelope, dict) or set(envelope) != {"statement", "signature"}):
        refused()
    for identity, pem in trust["keys"].items():
        if not hex_value(identity, 64) or not isinstance(pem, str) or len(pem) > 16384:
            refused()
        try:
            if digest(pem.encode("ascii")) != identity:
                refused()
        except UnicodeError:
            refused()
    value = envelope["statement"]
    validate_statement(value)
    if (value["key_id"] != key_id or key_id not in trust["keys"]
            or value["source_commit"] != source_commit or value["bundle_sha256"] != bundle_sha256
            or value["release_sequence"] != release_sequence
            or not value["issued_at"] <= now < value["expires_at"]):
        refused()
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (ValueError, TypeError):
        refused()
    if not 256 <= len(signature) <= 1024:
        refused()
    with tempfile.TemporaryDirectory(prefix="wisp-public-verification-") as temporary:
        root = Path(temporary)
        public, detached = root / "publisher.pem", root / "signature.bin"
        public.write_text(trust["keys"][key_id], encoding="ascii")
        detached.write_bytes(signature)
        # Reject non-RSA keys rather than dispatching based on an envelope name.
        openssl(["rsa", "-pubin", "-in", str(public), "-noout"])
        openssl(["dgst", "-sha256", "-verify", str(public), "-signature", str(detached)], data=canonical(value))
    return VerifiedArtifact(key_id, source_commit, bundle_sha256, release_sequence,
                            digest(canonical(value)), value["expires_at"])


def sign_statement(value, *, private_key_fd):
    """Sign only through a caller-owned inherited fd. Never accept key argv/text.

    The descriptor must refer to unencrypted RSA private material supplied by an
    approved publisher. The signer neither stores it nor emits child diagnostics.
    """
    validate_statement(value)
    if type(private_key_fd) is not int or private_key_fd <= 2:
        refused()
    signature = openssl(["dgst", "-sha256", "-passin", "pass:", "-sign", "/dev/fd/" + str(private_key_fd)],
                        data=canonical(value), pass_fds=(private_key_fd,))
    if not 256 <= len(signature) <= 1024:
        refused()
    return canonical({"statement": value, "signature": base64.b64encode(signature).decode("ascii")})


def private_regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def consume_release(verified, ledger_path, *, now):
    """Durably burn the global release sequence before any credential export.

    Caller owns a pre-existing mode-0700 directory. Symlinks, missing/corrupt
    existing state, sequence reuse and downgrade fail closed. This ledger stores
    no credentials. All publisher keys share a monotonic sequence, preventing a
    key rotation from resetting replay protection.
    """
    if not isinstance(verified, VerifiedArtifact) or type(now) is not int or now >= verified.expires_at:
        refused()
    path = Path(ledger_path)
    parent = path.parent
    try:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            refused()
        # Holding a directory fd prevents path substitution during the update.
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            lock_fd = os.open(path.name + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(lock_fd, "r+b") as lock:
                if not private_regular(os.fstat(lock.fileno())):
                    refused()
                fcntl.flock(lock, fcntl.LOCK_EX)
                previous = 0
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                except FileNotFoundError:
                    # A nonempty lock is a durable initialized sentinel. Deleting
                    # the ledger cannot reset the sequence to a first release.
                    if lock.read(1):
                        refused()
                else:
                    with os.fdopen(fd, "rb") as source:
                        if not private_regular(os.fstat(source.fileno())):
                            refused()
                        old = strict_json(source.read(65537))
                    if (not isinstance(old, dict) or set(old) != {"schema_version", "release_sequence", "statement_sha256"}
                            or type(old["schema_version"]) is not int or old["schema_version"] != 1 or type(old["release_sequence"]) is not int
                            or old["release_sequence"] <= 0 or not hex_value(old["statement_sha256"], 64)):
                        refused()
                    previous = old["release_sequence"]
                if verified.release_sequence <= previous:
                    refused()
                # Mark initialized first: interrupted initial creation fails
                # closed instead of accepting a possibly consumed release again.
                lock.seek(0)
                lock.write(b"1")
                lock.flush()
                os.fsync(lock.fileno())
                temporary = ".signature-ledger-" + os.urandom(16).hex()
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(canonical({"schema_version": 1, "release_sequence": verified.release_sequence,
                                                "statement_sha256": verified.statement_sha256}))
                        output.flush()
                        os.fsync(output.fileno())
                    os.rename(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError):
        refused()
